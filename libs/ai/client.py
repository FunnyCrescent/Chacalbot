"""OpenAIClient — universal OpenAI-compatible async HTTP client.

ИТЕРАЦИЯ 10 (Раздел 8): трёхуровневый перебор провайдер → модели БЕЗ памяти
между запросами. Каждый вызов chat()/embed() начинает перебор заново с
провайдера №1 / модели №1 (см. config_legacy.get_providers_for_role).
Переключение на следующую пару — при 401/402/403 или после исчерпания
ретраев (429/5xx/сеть). 400/404 — ошибка ЗАПРОСА, не провайдера — сразу наружу.

ИТЕРАЦИЯ 11: 400/403/404 с «средовым» телом (гео-блок FAILED_PRECONDITION,
невалидный ключ, биллинг, suspended-аккаунт — см.
config_legacy.PROVIDER_LEVEL_ERROR_PATTERNS) — это НЕ ошибка запроса:
провайдер не обслужит ни одну модель. Такой ответ переключает перебор на
СЛЕДУЮЩЕГО ПРОВАЙДЕРА, пропуская его оставшиеся модели. Реальный кейс:
«400 User location is not supported for the API use» валил /cymeriad,
хотя остальные API из LLM_PROVIDERS были рабочие.

ИТЕРАЦИЯ 10 (Раздел 6): после каждого успешного вызова читаем
response["usage"]["total_tokens"] и отдаём в usage_ledger (реальное
списание токенов биллингом).
"""
import json
import logging
import asyncio
from typing import Dict, List, Optional

import aiohttp

from libs.config_legacy import (
    OPENAI_BASE_URL, OPENAI_API_KEY, EMBEDDING_ENDPOINT, get_thinking_payload,
    get_providers_for_role, PROVIDER_SWITCH_STATUSES,
    PROVIDER_LEVEL_ERROR_PATTERNS,
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


class AllProvidersExhausted(Exception):
    """Раздел 8: все пары провайдер+модель из конфигурации роли перепробованы —
    ни одна не сработала. Сообщение — человекочитаемое, для пользователя."""
    def __init__(self, role: str, attempts: int, last_error: Optional[Exception] = None):
        self.role = role
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            f"LLM-провайдеры для роли '{role}' исчерпаны ({attempts} попыток). "
            f"Последняя ошибка: {last_error}"
        )


class _ProviderSwitch(Exception):
    """Internal signal: эта пара провайдер+модель провалилась ошибкой, которая
    ДОСТОЙНА переключения (401/402/403, исчерпание ретраев или «средовая»
    400/404 — см. _is_provider_level_error). Ловится вложенным циклом
    chat()/embed() и приводит к следующей модели/провайдеру.
    Наружу никогда не выходит — наружу выходит AllProvidersExhausted или
    исходная непереключаемая ошибка.

    provider_level=True — виноват ПРОВАЙДЕР целиком (гео-блок/ключ/биллинг),
    его оставшиеся модели перебирать бессмысленно: цикл сразу переходит к
    следующему провайдеру."""
    def __init__(self, provider_name: str, model: str, reason: str,
                 original: Optional[Exception] = None,
                 provider_level: bool = False):
        self.provider_name = provider_name
        self.model = model
        self.reason = reason
        self.original = original
        self.provider_level = provider_level
        super().__init__(f"{provider_name}/{model}: {reason}")


def _is_provider_level_error(status: int, error_text: str) -> bool:
    """ИТЕРАЦИЯ 11: гео-блок, невалидный ключ, биллинг, suspension приходят
    как 400/403/404, но это не ошибка ЗАПРОСА — провайдер не обслужит НИ одну
    модель (ни текущую, ни следующую). Проверяем сырое тело ответа на паттерны
    из config_legacy.PROVIDER_LEVEL_ERROR_PATTERNS (case-insensitive)."""
    if not error_text:
        return False
    lowered = error_text.lower()
    return any(pattern in lowered for pattern in PROVIDER_LEVEL_ERROR_PATTERNS)


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

    # ИТЕРАЦИЯ 11: часть агрегаторов (и Google-совместимые шлюзы) заворачивают
    # ошибку в массив: [{"error": {...}}]. Разворачиваем первый элемент.
    if isinstance(body, list):
        body = body[0] if body and isinstance(body[0], dict) else None

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
        self.model = model            # дефолт; при LLM_PROVIDERS перекрывается per-паре
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

    def _headers_for(self, api_key: str) -> Dict[str, str]:
        """Раздел 8: Authorization строится по api_key КОНКРЕТНОГО провайдера."""
        if not api_key or api_key == self.api_key:
            return self.headers
        headers = dict(self.headers)
        headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def _report_usage(self, data: Dict, model: str) -> None:
        """Раздел 6: достать usage.total_tokens из ответа и отдать в usage_ledger.
        Не бросает исключений (биллинг не должен ломать игру)."""
        try:
            usage = data.get("usage") or {}
            total = int(usage.get("total_tokens") or 0)
        except (TypeError, ValueError, AttributeError):
            return
        if total <= 0:
            return
        try:
            from libs.ai.usage_ledger import record
            record(self.role, model, total)
        except Exception:
            pass

    async def chat(self, messages: List[Dict], system_prompt: Optional[str] = None,
                   tools: Optional[List[Dict]] = None, tool_choice: Optional[str] = "auto",
                   max_tokens: Optional[int] = None, retries: int = 3,
                   extra_payload: Optional[Dict] = None) -> Dict:
        """Call /chat/completions with retries + provider/model fallback.

        BUG #6 + #13 FIX: HTTP 429/5xx retried with exponential backoff; trace_id
        extracted from the error body and logged separately.

        ИТЕРАЦИЯ 10 (Раздел 8): поверх retry-цикла — вложенный перебор
        провайдер → модели из статической конфигурации роли (LLM_PROVIDERS /
        <ROLE>_PROVIDERS). КАЖДЫЙ вызов начинает перебор ЗАНОВО с пары №1 —
        никакого состояния между запросами. Переключение пары: 401/402/403,
        исчерпание ретраев (429/5xx/сеть) или «средовая» 400/404
        (гео-блок/ключ/биллинг). Чистая ошибка запроса (400/404 без паттернов
        провайдера) — сразу наружу, перебор не продолжается.

        ИТЕРАЦИЯ 11: extra_payload — дополнительные поля JSON-payload
        поверх стандартных (например, {"response_format": {...}} для creu).
        """
        # Trim messages to avoid payload bloat (keep last 25 + system).
        # Note: this is a defensive trim — process_master_turn already trims to 30.
        trimmed_messages = messages[-25:] if len(messages) > 25 else messages

        base_payload = {
            "messages": [],
            "temperature": self.temperature,
            "max_tokens": max_tokens or self.max_tokens,
            "stream": False,
        }
        if system_prompt:
            base_payload["messages"].append({"role": "system", "content": system_prompt})
        base_payload["messages"].extend(trimmed_messages)
        if tools:
            base_payload["tools"] = tools
            base_payload["tool_choice"] = tool_choice
        if extra_payload:
            base_payload.update(extra_payload)
        # Thinking mode (config.json → thinking.style). Мержится в корень payload:
        # {"reasoning": {"enabled": true}} | {"chat_template_kwargs": {"enable_thinking": true}}
        # | {"thinking": {"type": "enabled"}} — зависит от агрегатора.
        if self.thinking_payload:
            base_payload.update(self.thinking_payload)

        providers = get_providers_for_role(
            self.role, default_model=self.model,
            default_base_url=self.base_url, default_api_key=self.api_key)

        last_switch: Optional[_ProviderSwitch] = None
        attempts = 0
        for provider in providers:
            for model in provider["models"]:
                attempts += 1
                payload = dict(base_payload)
                payload["model"] = model
                try:
                    result = await self._chat_once(provider, model, payload, retries)
                    # Раздел 8: логируем финальную пару, на которой запрос СРАБОТАЛ —
                    # без этого не видно, что «провайдер №1 стабильно проваливается».
                    logger.info(
                        f"[chat] OK role={self.role} provider={provider['name']} model={model}"
                    )
                    return result
                except _ProviderSwitch as sw:
                    last_switch = sw
                    logger.warning(
                        f"[chat] provider switch: role={self.role} "
                        f"provider={provider['name']} model={model} → {sw.reason}"
                    )
                    if sw.provider_level:
                        # ИТЕРАЦИЯ 11: виноват провайдер целиком (гео-блок/ключ/
                        # биллинг) — его оставшиеся модели заведомо упадут с той
                        # же ошибкой. Сразу к следующему провайдеру.
                        break
                    continue

        raise AllProvidersExhausted(self.role, attempts, last_switch or None)

    async def _chat_once(self, provider: Dict, model: str, payload: Dict,
                         retries: int) -> Dict:
        """ОДНА пара провайдер+модель: существующий retry/backoff-цикл.

        Успех → полный JSON-ответ. Ошибка, достойная переключения →
        _ProviderSwitch. Ошибка запроса (400/404/...) → исходное исключение
        наружу (перебор НЕ продолжается). Сетевые/неожиданные ошибки внутри
        ретраев обрабатываются как раньше.
        """
        base_url = provider["base_url"]
        headers = self._headers_for(provider.get("api_key", ""))

        # Log payload size for debugging
        raw_payload = json.dumps(payload, ensure_ascii=False)
        payload_size = len(raw_payload.encode('utf-8'))
        logger.info(f"[chat] Payload size: {payload_size} bytes, model: {model}, "
                    f"provider: {provider['name']}, messages: {len(payload['messages'])}")
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
                        f"{base_url}/chat/completions",
                        headers=headers,
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
                            elif response.status in PROVIDER_SWITCH_STATUSES:
                                # Раздел 8: 401/402/403 — auth/платёж/доступ. Ретраить
                                # бессмысленно — сразу переключаем пару. 401/402 —
                                # всегда уровень ПРОВАЙДЕРА (ключ не починится на
                                # другой модели); 403 может быть пер-модельным.
                                logger.error(
                                    f"[chat] HTTP {response.status} (provider-level). "
                                    f"trace_id={last_trace_id or 'n/a'}: {last_error_msg}"
                                )
                                raise _ProviderSwitch(
                                    provider["name"], model,
                                    f"HTTP {response.status}: {last_error_msg}",
                                    provider_level=response.status in (401, 402))
                            elif _is_provider_level_error(response.status, error_text):
                                # ИТЕРАЦИЯ 11: «средовая» 400/404 — гео-блок,
                                # невалидный ключ, биллинг, suspension. Это уровень
                                # ПРОВАЙДЕРА: пропускаем его модели целиком и идём
                                # к следующему провайдеру из LLM_PROVIDERS.
                                logger.error(
                                    f"[chat] HTTP {response.status} (provider-level "
                                    f"environmental). trace_id={last_trace_id or 'n/a'}: "
                                    f"{last_error_msg}"
                                )
                                raise _ProviderSwitch(
                                    provider["name"], model,
                                    f"HTTP {response.status}: {last_error_msg}",
                                    provider_level=True)
                            else:
                                # Non-retryable HTTP error (400/404/422 etc.) — ошибка
                                # ЗАПРОСА, не провайдера: перебор не продолжаем, чужой
                                # провайдер вернёт ту же 400. Наружу — как раньше.
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
                            data = json.loads(text)
                            self._report_usage(data, model)
                            return data
            except _ProviderSwitch:
                raise  # не глотать — сигнал переключения выше
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

        # All retries exhausted on THIS provider+model pair — Раздел 8: это
        # достойно переключения, а не немедленного raise.
        if last_trace_id:
            logger.error(
                f"[chat] API failed after {retries} attempts. "
                f"trace_id={last_trace_id} status={last_http_status} msg={last_error_msg}"
            )
            raise _ProviderSwitch(
                provider["name"], model,
                f"retries exhausted (HTTP {last_http_status}, "
                f"trace_id={last_trace_id}): {last_error_msg}")
        raise _ProviderSwitch(
            provider["name"], model,
            f"retries exhausted (network): {last_error}",
            original=last_error,
        )

    async def embed(self, texts: List[str], retries: int = 2) -> List[List[float]]:
        """Call the /embeddings endpoint. Returns one vector per input text, in order.
        Raises on failure — callers (MemoryStore) are expected to catch and fail soft,
        since semantic memory is an enhancement, not a hard dependency for the game to run.

        ИТЕРАЦИЯ 10 (Раздел 8): тот же перебор провайдер → модели, что и в chat()
        (EMBEDDING_PROVIDERS / LLM_PROVIDERS). Без памяти между вызовами.
        ИТЕРАЦИЯ 11: «средовые» ошибки (гео-блок/ключ/биллинг) пропускают
        провайдера целиком — как в chat().
        """
        if not texts:
            return []
        base_payload = {"input": texts}
        providers = get_providers_for_role(
            self.role, default_model=self.model,
            default_base_url=self.base_url, default_api_key=self.api_key)

        last_switch: Optional[_ProviderSwitch] = None
        attempts = 0
        for provider in providers:
            for model in provider["models"]:
                attempts += 1
                payload = dict(base_payload)
                payload["model"] = model
                try:
                    result = await self._embed_once(provider, model, payload, retries)
                    logger.info(
                        f"[embed] OK role={self.role} provider={provider['name']} model={model}"
                    )
                    return result
                except _ProviderSwitch as sw:
                    last_switch = sw
                    logger.warning(
                        f"[embed] provider switch: role={self.role} "
                        f"provider={provider['name']} model={model} → {sw.reason}"
                    )
                    if sw.provider_level:
                        break  # ИТЕРАЦИЯ 11: провайдер мёртв целиком — к следующему
                    continue
        raise AllProvidersExhausted(self.role, attempts, last_switch or None)

    async def _embed_once(self, provider: Dict, model: str, payload: Dict,
                          retries: int) -> List[List[float]]:
        """Одна пара провайдер+модель для /embeddings (retry внутри).
        Любое исчерпание ретраев → _ProviderSwitch (embeddings — enhancement,
        упрощённая классификация уместна: всё равно fail-soft у вызывающих).
        ИТЕРАЦИЯ 11: «средовая» ошибка (гео-блок/ключ/биллинг) — мгновенный
        _ProviderSwitch(provider_level=True) без сжигания ретраев."""
        base_url = provider["base_url"]
        headers = self._headers_for(provider.get("api_key", ""))
        last_error = None
        sess_kwargs = aiohttp_session_kwargs()
        req_kwargs = aiohttp_request_kwargs()
        for attempt in range(retries):
            try:
                timeout = aiohttp.ClientTimeout(total=60, connect=15, sock_read=45)
                async with aiohttp.ClientSession(timeout=timeout, **sess_kwargs) as session:
                    async with session.post(
                        f"{base_url}{EMBEDDING_ENDPOINT}",
                        headers=headers,
                        json=payload,
                        **req_kwargs,
                    ) as response:
                        if response.status != 200:
                            error_text = await response.text()
                            if _is_provider_level_error(response.status, error_text):
                                raise _ProviderSwitch(
                                    provider["name"], model,
                                    f"HTTP {response.status}: {error_text[:200]}",
                                    provider_level=True)
                            raise Exception(f"Embeddings API {response.status}: {error_text}")
                        data = json.loads(await response.text(encoding="utf-8"))
                        self._report_usage(data, model)
                        # OpenAI-style: data["data"] is a list of {"embedding": [...], "index": i}
                        items = sorted(data.get("data", []), key=lambda x: x.get("index", 0))
                        return [item["embedding"] for item in items]
            except _ProviderSwitch:
                raise  # ИТЕРАЦИЯ 11: не глотать — сигнал переключения (provider_level) выше
            except Exception as e:
                last_error = e
                logger.warning(f"[embed] Attempt {attempt+1}/{retries} failed: {e}")
                if attempt < retries - 1:
                    await asyncio.sleep(1)
        raise _ProviderSwitch(
            provider["name"], model,
            f"retries exhausted: {last_error}", original=last_error)

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
