"""OpenAIClient — universal OpenAI-compatible async HTTP client."""
import json
import logging
import asyncio
from typing import Dict, List, Optional

import aiohttp

from libs.config_legacy import OPENAI_BASE_URL, OPENAI_API_KEY, EMBEDDING_ENDPOINT
from libs.proxy_helper import aiohttp_session_kwargs, aiohttp_request_kwargs

logger = logging.getLogger(__name__)

class OpenAIClient:
    """Universal OpenAI-compatible client"""

    def __init__(self, model: str, temperature: float = 0.8, max_tokens: int = 4096,
                 base_url: str = None, api_key: str = None):
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.base_url = (base_url or OPENAI_BASE_URL).rstrip("/")
        self.api_key = api_key or OPENAI_API_KEY
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://dnd-bot.local",
            "X-Title": "D&D Dark Fantasy Bot",
        }

    async def chat(self, messages: List[Dict], system_prompt: Optional[str] = None,
                   tools: Optional[List[Dict]] = None, tool_choice: Optional[str] = "auto",
                   max_tokens: Optional[int] = None, retries: int = 3) -> Dict:
        # Trim messages to avoid payload bloat (keep last 25 + system)
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

        # Log payload size for debugging
        raw_payload = json.dumps(payload, ensure_ascii=False)
        payload_size = len(raw_payload.encode('utf-8'))
        logger.info(f"[chat] Payload size: {payload_size} bytes, model: {self.model}, messages: {len(payload['messages'])}")
        logger.info(f"[PAYLOAD_RAW] {raw_payload[:4000]}")

        last_error = None
        sess_kwargs = aiohttp_session_kwargs()
        req_kwargs = aiohttp_request_kwargs()
        for attempt in range(retries):
            try:
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
                        # Read response fully with explicit encoding
                        text = await response.text(encoding='utf-8')
                        return json.loads(text)
            except (aiohttp.ClientError, aiohttp.http_exceptions.TransferEncodingError, 
                    aiohttp.ClientPayloadError, ConnectionResetError) as e:
                last_error = e
                wait = min(2 ** attempt, 30)  # cap at 30s
                logger.warning(f"[chat] Attempt {attempt+1}/{retries} failed: {type(e).__name__}: {e}. Retrying in {wait}s...")
                await asyncio.sleep(wait)
            except Exception as e:
                # Non-retryable error
                raise

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
