"""OpenAIClient — universal OpenAI-compatible async HTTP client."""
import json
import logging
import asyncio
from typing import Dict, List, Optional

import aiohttp

from libs.config_legacy import (
    OPENAI_BASE_URL, OPENAI_API_KEY, EMBEDDING_ENDPOINT, get_thinking_payload,
)
from libs.proxy_helper import aiohttp_session_kwargs, aiohttp_request_kwargs

logger = logging.getLogger(__name__)


class ApiError(Exception):
    """Structured API error from the LLM provider.

    BUG #13: raised instead of a generic `Exception("API 500: ...")` so callers can
    show a clean, user-facing message + log the trace_id separately. The original
    message string is preserved in `self.raw` for debugging if needed.
    """
    def __init__(self, status: int, message: str, trace_id: Optional[str] = None, raw: str = ""):
        self.status = status
        self.trace_id = trace_id
        self.raw = raw
        super().__init__(message)

    def __str__(self) -> str:
        if self.trace_id:
            return f"API {self.status}: {self.args[0]} (trace_id={self.trace_id})"
        return f"API {self.status}: {self.args[0]}"


def _parse_api_error_body(error_text: str) -> Dict[str, Optional[str]]:
    """BUG #13: extract a clean human-readable message + trace_id from the LLM
    provider's error response body. Tries a few common shapes:
    - {"error": {"code": ..., "message": ..., "traceid": "..."}}
    - {"error": {"message": "..."}, "trace_id": "..."}
    - {"detail": "...", "trace_id": "..."}
    Falls back to the raw body (truncated) if nothing matches.
    """
    result = {"message": None, "trace_id": None}
    if not error_text:
        return result
    try:
        body = json.loads(error_text)
    except (json.JSONDecodeError, TypeError):
        # Not JSON — treat the whole body as the message.
        result["message"] = error_text[:200]
        return result

    # Look for the message
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            result["message"] = err.get("message") or err.get("code")
        elif isinstance(err, str):
            result["message"] = err
        if not result["message"]:
            result["message"] = body.get("detail") or body.get("message")
        # Look for the trace_id (try several common field names)
        for key in ("traceid", "trace_id", "request_id", "requestId", "x-request-id"):
            value = None
            if isinstance(err, dict) and key in err:
                value = err[key]
            elif key in body:
                value = body[key]
            if value:
                result["trace_id"] = str(value)
                break
    return result


class OpenAIClient:
    """Universal OpenAI-compatible client"""

    def __init__(self, model: str, temperature: float = 0.8, max_tokens: int = 4096,
                 base_url: str = None, api_key: str = None, role: str = ""):
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.base_url = (base_url or OPENAI_BASE_URL).rstrip("/")
        self.api_key = api_key or OPENAI_API_KEY
        # Фича: thinking mode по ролям (config.json → thinking). Роль "master"
        # (нарративщик) ВСЕГДА получает thinking — см. get_thinking_payload.
        self.role = role
        self.thinking_payload = get_thinking_payload(role) or None
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://dnd-bot.local",
            "X-Title": "D&D Dark Fantasy Bot",
        }

    async def chat(self, messages: List[Dict], system_prompt: Optional[str] = None,
                   tools: Optional[List[Dict]] = None, tool_choice: Optional[str] = "auto",
                   max_tokens: Optional[int] = None, retries: int = 3) -> Dict:
        """Call /chat/completions with retries.

        BUG #6 + #13 FIX:
        - HTTP 429 / 500 / 502 / 503 / 504 are now RETRIED (with exponential backoff
          and a cap of 30s between attempts). Previously 500 errors raised immediately,
          which propagated as `*[Ошибка Мастера: API 500: ...]*` into the chat with
          the trace_id buried in the error string.
        - The `trace_id` field (if present in the error JSON body) is extracted and
          logged separately at ERROR level so it can be cross-referenced with the
          LLM provider's support team.
        - Timeouts (network-side) are still retried, just like before.
        - The full error body is parsed for a human-readable message and the
          user-facing error message is short and informative (no leaked JSON dump).
        """
        # Trim messages to avoid payload bloat (keep last 25 + system).
        # Note: this is a defensive trim — process_master_turn already trims to 30.
        trimmed_messages = messages[-25:] if len(messages) > 25 else messages

        payload = {
            "model": self.model,
            "messages": [],
            "temperature": self.temperature,
            "max_tokens": max_tokens or self.max_tokens,
            "stream": False,
        }
        if system_prompt:
            payload["messages"].append({"role": "system", "content": system_prompt})
        payload["messages"].extend(trimmed_messages)
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
        # Thinking mode (config.json → thinking.style). Мержится в корень payload:
        # {"reasoning": {"enabled": true}} | {"chat_template_kwargs": {"enable_thinking": true}}
        # | {"thinking": {"type": "enabled"}} — зависит от агрегатора.
        if self.thinking_payload:
            payload.update(self.thinking_payload)

        # Log payload size for debugging
        raw_payload = json.dumps(payload, ensure_ascii=False)
        payload_size = len(raw_payload.encode('utf-8'))
        logger.info(f"[chat] Payload size: {payload_size} bytes, model: {self.model}, messages: {len(payload['messages'])}")
        # Truncate to 4000 chars to avoid log bloat — full payload only matters for
        # repro, and we already log payload_size above.
        logger.debug(f"[PAYLOAD_RAW] {raw_payload[:4000]}")

        # BUG #13: HTTP status codes that should trigger a retry. 429 = rate-limited,
        # 5xx = server error / gateway / service unavailable. 4xx (except 429) =
        # bad request, do not retry — we'd just keep getting the same error.
        RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}

        last_error: Optional[Exception] = None
        last_http_status: Optional[int] = None
        last_trace_id: Optional[str] = None
        last_error_msg: Optional[str] = None
        sess_kwargs = aiohttp_session_kwargs()
        req_kwargs = aiohttp_request_kwargs()
        for attempt in range(retries):
            try:
                # BUG #6: per-attempt timeout — the outer `for` loop already gives us
                # retries; we use a generous per-attempt timeout so a single stuck
                # connection doesn't hang the whole call. The original 600/30/300 was
                # fine for the happy path but masked retry opportunities.
                timeout = aiohttp.ClientTimeout(total=600, connect=30, sock_read=300)
                async with aiohttp.ClientSession(timeout=timeout, **sess_kwargs) as session:
                    async with session.post(
                        f"{self.base_url}/chat/completions",
                        headers=self.headers,
                        json=payload,
                        **req_kwargs,
                    ) as response:
                        if response.status != 200:
                            error_text = await response.text()
                            # BUG #13: parse the JSON body to extract trace_id and
                            # human-readable message, log them separately.
                            last_http_status = response.status
                            parsed = _parse_api_error_body(error_text)
                            last_trace_id = parsed.get("trace_id")
                            last_error_msg = parsed.get("message") or error_text[:200]

                            if response.status in RETRYABLE_HTTP_STATUSES:
                                # Build a retryable error so the loop below catches it.
                                err = ApiError(
                                    status=response.status,
                                    message=last_error_msg,
                                    trace_id=last_trace_id,
                                    raw=error_text,
                                )
                                logger.warning(
                                    f"[chat] Attempt {attempt+1}/{retries} HTTP {response.status} "
                                    f"(retryable). trace_id={last_trace_id or 'n/a'}: {last_error_msg}"
                                )
                                last_error = err
                            else:
                                # Non-retryable HTTP error (400/401/403/404 etc.) — log
                                # with trace_id and raise immediately so the user sees
                                # the real problem, not a misleading "API failed after N attempts".
                                logger.error(
                                    f"[chat] HTTP {response.status} (non-retryable). "
                                    f"trace_id={last_trace_id or 'n/a'}: {last_error_msg}"
                                )
                                raise ApiError(
                                    status=response.status,
                                    message=last_error_msg,
                                    trace_id=last_trace_id,
                                    raw=error_text,
                                )
                        else:
                            # Read response fully with explicit encoding
                            text = await response.text(encoding='utf-8')
                            return json.loads(text)
            except (aiohttp.ClientError, aiohttp.http_exceptions.TransferEncodingError,
                    aiohttp.ClientPayloadError, ConnectionResetError, asyncio.TimeoutError) as e:
                # BUG #6: network-side timeouts / connection issues — retry with backoff.
                last_error = e
                logger.warning(
                    f"[chat] Attempt {attempt+1}/{retries} network error: "
                    f"{type(e).__name__}: {e}"
                )
            except ApiError:
                # Already classified above — re-raise if non-retryable, fall through
                # to backoff sleep if retryable (last_error was set above).
                if last_error is None or not isinstance(last_error, ApiError) or \
                        (last_http_status and last_http_status not in RETRYABLE_HTTP_STATUSES):
                    raise
            except Exception as e:
                # Non-retryable, unexpected error — log with full info and raise.
                logger.error(f"[chat] Unexpected error: {type(e).__name__}: {e}")
                raise

            # Backoff before the next attempt (only if we're going to retry).
            if attempt < retries - 1:
                wait = min(2 ** attempt, 30)  # 1s, 2s, 4s, 8s, 16s, 30s cap
                logger.info(f"[chat] Retrying in {wait}s (attempt {attempt+2}/{retries})")
                await asyncio.sleep(wait)

        # All retries exhausted — raise a clean, user-facing error with trace_id.
        if last_trace_id:
            logger.error(
                f"[chat] API failed after {retries} attempts. "
                f"trace_id={last_trace_id} status={last_http_status} msg={last_error_msg}"
            )
            raise ApiError(
                status=last_http_status or 0,
                message=f"LLM-провайдер временно недоступен (trace_id={last_trace_id}). "
                        f"Попробуйте ещё раз через минуту.",
                trace_id=last_trace_id,
                raw=str(last_error),
            )
        raise Exception(f"API failed after {retries} attempts: {last_error}")

    async def embed(self, texts: List[str], retries: int = 2) -> List[List[float]]:
        """Call the /embeddings endpoint. Returns one vector per input text, in order.
        Raises on failure — callers (MemoryStore) are expected to catch and fail soft,
        since semantic memory is an enhancement, not a hard dependency for the game to run."""
        if not texts:
            return []
        payload = {"model": self.model, "input": texts}
        last_error = None
        sess_kwargs = aiohttp_session_kwargs()
        req_kwargs = aiohttp_request_kwargs()
        for attempt in range(retries):
            try:
                timeout = aiohttp.ClientTimeout(total=60, connect=15, sock_read=45)
                async with aiohttp.ClientSession(timeout=timeout, **sess_kwargs) as session:
                    async with session.post(
                        f"{self.base_url}{EMBEDDING_ENDPOINT}",
                        headers=self.headers,
                        json=payload,
                        **req_kwargs,
                    ) as response:
                        if response.status != 200:
                            error_text = await response.text()
                            raise Exception(f"Embeddings API {response.status}: {error_text}")
                        data = json.loads(await response.text(encoding="utf-8"))
                        # OpenAI-style: data["data"] is a list of {"embedding": [...], "index": i}
                        items = sorted(data.get("data", []), key=lambda x: x.get("index", 0))
                        return [item["embedding"] for item in items]
            except Exception as e:
                last_error = e
                logger.warning(f"[embed] Attempt {attempt+1}/{retries} failed: {e}")
                if attempt < retries - 1:
                    await asyncio.sleep(1)
        raise Exception(f"Embeddings API failed after {retries} attempts: {last_error}")

    async def stream_chat(self, messages: List[Dict], system_prompt: Optional[str] = None):
        trimmed_messages = messages[-25:] if len(messages) > 25 else messages
        payload = {
            "model": self.model,
            "messages": [],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": True,
        }
        if system_prompt:
            payload["messages"].append({"role": "system", "content": system_prompt})
        payload["messages"].extend(trimmed_messages)

        sess_kwargs = aiohttp_session_kwargs()
        req_kwargs = aiohttp_request_kwargs()
        timeout = aiohttp.ClientTimeout(total=600, connect=30, sock_read=300)
        async with aiohttp.ClientSession(timeout=timeout, **sess_kwargs) as session:
            async with session.post(
                f"{self.base_url}/chat/completions",
                headers=self.headers,
                json=payload,
                **req_kwargs,
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise Exception(f"API {response.status}: {error_text}")
                async for line in response.content:
                    line = line.decode("utf-8").strip()
                    if line.startswith("data: "):
                        data = line[6:]
                        if data == "[DONE]":
                            return
                        try:
                            chunk = json.loads(data)
                            if chunk["choices"] and chunk["choices"][0]["delta"].get("content"):
                                yield chunk["choices"][0]["delta"]["content"]
                        except (json.JSONDecodeError, KeyError):
                            continue
