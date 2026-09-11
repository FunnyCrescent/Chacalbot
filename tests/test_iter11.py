"""ИТЕРАЦИЯ 11 — авто-переключение провайдеров на «средовых» ошибках.

Реальный кейс пользователя: /cymeriad упал с «вердикт: ERROR. API 400:
FAILED_PRECONDITION — User location is not supported for the API use»,
хотя в LLM_PROVIDERS было 4 рабочих API. Причина: по ТЗ Раздела 8 ответ
400 считался ошибкой ЗАПРОСА и уходил наружу БЕЗ перебора. Но гео-блок,
невалидный ключ, биллинг и suspended-аккаунт — это уровень ПРОВАЙДЕРА:
он не обслужит ни текущую, ни любую другую модель. Теперь такой ответ
уводит перебор к следующему провайдеру, пропуская его оставшиеся модели.

Заодно: creu/llm.py переведён с собственного aiohttp-вызова (без перебора)
на OpenAIClient — авто-переключение теперь у ВСЕХ LLM-вызовов бота.
"""
import asyncio
import json

import pytest


# Реальное тело ошибки из лога пользователя (Google-совместимый шлюз
# заворачивает ошибку в массив — раньше парсер тела это не понимал).
GEO_BODY = json.dumps([{
    "error": {
        "code": 400,
        "message": "User location is not supported for the API use.",
        "status": "FAILED_PRECONDITION",
    }
}])

# Честная ошибка ЗАПРОСА — перебор НЕ должен продолжаться.
PLAIN_400_BODY = json.dumps({
    "error": {"code": 400,
              "message": "Invalid request schema: 'messages' must be a list"},
})

OK_BODY = json.dumps({
    "choices": [{"message": {"content": "ок", "role": "assistant"}}],
    "usage": {"total_tokens": 0},
})

EMBED_OK = json.dumps({
    "data": [{"embedding": [0.1, 0.2], "index": 0}],
    "usage": {"total_tokens": 0},
})


# ═══════════════════════════════════════════════════════════════
# Классификация ошибок
# ═══════════════════════════════════════════════════════════════

class TestErrorClassification:
    def test_geo_block_body_is_provider_level(self):
        from libs.ai.client import _is_provider_level_error
        assert _is_provider_level_error(400, GEO_BODY)

    def test_patterns_case_insensitive_and_status_agnostic(self):
        from libs.ai.client import _is_provider_level_error
        assert _is_provider_level_error(403, "BILLING_HAS_NOT_BEEN_ENABLED")
        assert _is_provider_level_error(
            400, "API key not valid. Please pass a valid API key.")
        assert _is_provider_level_error(404, "Permission denied for this project")

    def test_plain_request_errors_stay_request_level(self):
        from libs.ai.client import _is_provider_level_error
        assert not _is_provider_level_error(400, PLAIN_400_BODY)
        assert not _is_provider_level_error(404, "Model gemma-4-31b not found")
        assert not _is_provider_level_error(422, "max_tokens too large")
        assert not _is_provider_level_error(400, "")
        assert not _is_provider_level_error(400, "cannot enter battle now")

    def test_parse_list_wrapped_error_body(self):
        """Тело из реального лога: [{\"error\": {...}}] — сообщение извлекается,
        а не отдаётся как сырой JSON."""
        from libs.ai.client import _parse_api_error_body
        parsed = _parse_api_error_body(GEO_BODY)
        assert parsed["message"] == "User location is not supported for the API use."

    def test_parse_plain_dict_body_still_works(self):
        from libs.ai.client import _parse_api_error_body
        parsed = _parse_api_error_body(PLAIN_400_BODY)
        assert "Invalid request schema" in parsed["message"]


# ═══════════════════════════════════════════════════════════════
# FakeHTTP — подмена aiohttp.ClientSession внутри libs.ai.client
# ═══════════════════════════════════════════════════════════════

class _FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    async def text(self, encoding=None):
        return self._body


class _PostCtx:
    def __init__(self, status, body):
        self._resp = _FakeResponse(status, body)

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *args):
        return False


class FakeHTTP:
    """Подменяет aiohttp.ClientSession в модуле libs.ai.client.
    responses — очередь (status, body): один POST = один элемент.
    requests — журнал (url, model) всех отправленных запросов."""

    def __init__(self, monkeypatch, responses):
        self.queue = list(responses)
        self.requests = []
        outer = self

        class FakeSession:
            def __init__(self, timeout=None, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def post(self, url, headers=None, json=None, **kwargs):
                outer.requests.append((url, (json or {}).get("model")))
                status, body = outer.queue.pop(0)
                return _PostCtx(status, body)

        import libs.ai.client as client_mod
        monkeypatch.setattr(client_mod.aiohttp, "ClientSession", FakeSession)


def _two_providers():
    return [
        {"name": "google1", "base_url": "http://g1/v1", "api_key": "k1",
         "models": ["gem-a", "gem-b"]},
        {"name": "google2", "base_url": "http://g2/v1", "api_key": "k2",
         "models": ["gem-c"]},
    ]


# ═══════════════════════════════════════════════════════════════
# chat(): гео-блок уводит перебор к следующему провайдеру
# ═══════════════════════════════════════════════════════════════

class TestChatGeoBlockSwitch:
    def test_geo_block_switches_to_next_provider(self, monkeypatch):
        from libs.ai.client import OpenAIClient
        provs = _two_providers()
        monkeypatch.setattr("libs.ai.client.get_providers_for_role",
                            lambda role, **kw: provs)
        http = FakeHTTP(monkeypatch, [
            (400, GEO_BODY),   # google1/gem-a — гео-блок (бывший fatal)
            (200, OK_BODY),    # google2/gem-c — успех
        ])
        c = OpenAIClient("gem-a", role="db")
        result = asyncio.run(c.chat([{"role": "user", "content": "привет"}]))
        assert result["choices"][0]["message"]["content"] == "ок"
        # google1/gem-b ПРОПУЩЕН: провайдер мёртв целиком, модель перебирать
        # бессмысленно — это и есть фикс реального кейса.
        assert http.requests == [
            ("http://g1/v1/chat/completions", "gem-a"),
            ("http://g2/v1/chat/completions", "gem-c"),
        ]

    def test_plain_400_still_raises_without_switch(self, monkeypatch):
        """Регресс Раздела 8: честная ошибка запроса наружу, перебор не жжёт
        чужих провайдеров."""
        from libs.ai.client import OpenAIClient, ApiError
        provs = _two_providers()
        monkeypatch.setattr("libs.ai.client.get_providers_for_role",
                            lambda role, **kw: provs)
        http = FakeHTTP(monkeypatch, [(400, PLAIN_400_BODY)])
        c = OpenAIClient("gem-a", role="db")
        with pytest.raises(ApiError):
            asyncio.run(c.chat([{"role": "user", "content": "привет"}]))
        assert http.requests == [("http://g1/v1/chat/completions", "gem-a")]

    def test_all_providers_geo_blocked_exhausted_with_clear_message(self, monkeypatch):
        """Все 4 API гео-блокнуты → AllProvidersExhausted с ПОСЛЕДНЕЙ ошибкой
        в сообщении (её увидит игрок через вердикт ERROR)."""
        from libs.ai.client import OpenAIClient, AllProvidersExhausted
        provs = [
            {"name": f"google{i}", "base_url": f"http://g{i}/v1",
             "api_key": f"k{i}", "models": ["gem"]} for i in (1, 2, 3)
        ]
        monkeypatch.setattr("libs.ai.client.get_providers_for_role",
                            lambda role, **kw: provs)
        FakeHTTP(monkeypatch, [(400, GEO_BODY)] * 3)
        c = OpenAIClient("gem", role="db")
        with pytest.raises(AllProvidersExhausted) as exc:
            asyncio.run(c.chat([{"role": "user", "content": "привет"}]))
        assert exc.value.role == "db"
        assert exc.value.attempts == 3
        assert "location is not supported" in str(exc.value)

    def test_401_skips_remaining_models_of_provider(self, monkeypatch):
        """401 — ключ мёртв для ВСЕХ моделей провайдера → провайдер целиком."""
        from libs.ai.client import OpenAIClient
        provs = _two_providers()
        monkeypatch.setattr("libs.ai.client.get_providers_for_role",
                            lambda role, **kw: provs)
        http = FakeHTTP(monkeypatch, [
            (401, json.dumps({"error": {"message": "invalid key"}})),
            (200, OK_BODY),
        ])
        c = OpenAIClient("gem-a", role="db")
        result = asyncio.run(c.chat([{"role": "user", "content": "привет"}]))
        assert result["choices"][0]["message"]["content"] == "ок"
        assert http.requests == [
            ("http://g1/v1/chat/completions", "gem-a"),
            ("http://g2/v1/chat/completions", "gem-c"),
        ]

    def test_403_without_patterns_switches_model_first(self, monkeypatch):
        """403 без «средовых» паттернов может быть пер-модельным — пробуем
        следующую модель ТОГО ЖЕ провайдера (поведение Раздела 8)."""
        from libs.ai.client import OpenAIClient
        provs = _two_providers()
        monkeypatch.setattr("libs.ai.client.get_providers_for_role",
                            lambda role, **kw: provs)
        http = FakeHTTP(monkeypatch, [
            (403, json.dumps({"error": {"message": "forbidden for tier"}})),
            (200, OK_BODY),  # google1/gem-b — успех
        ])
        c = OpenAIClient("gem-a", role="db")
        result = asyncio.run(c.chat([{"role": "user", "content": "привет"}]))
        assert result["choices"][0]["message"]["content"] == "ок"
        assert http.requests == [
            ("http://g1/v1/chat/completions", "gem-a"),
            ("http://g1/v1/chat/completions", "gem-b"),
        ]


# ═══════════════════════════════════════════════════════════════
# embed(): то же переключение для эмбеддингов
# ═══════════════════════════════════════════════════════════════

class TestEmbedGeoBlockSwitch:
    def test_geo_block_switches_provider_immediately(self, monkeypatch):
        from libs.ai.client import OpenAIClient
        provs = [
            {"name": "e1", "base_url": "http://e1/v1", "api_key": "k1",
             "models": ["emb-a", "emb-b"]},
            {"name": "e2", "base_url": "http://e2/v1", "api_key": "k2",
             "models": ["emb-c"]},
        ]
        monkeypatch.setattr("libs.ai.client.get_providers_for_role",
                            lambda role, **kw: provs)
        http = FakeHTTP(monkeypatch, [
            (400, GEO_BODY),
            (200, EMBED_OK),
        ])
        c = OpenAIClient("emb-a", role="memory")
        vectors = asyncio.run(c.embed(["тест"]))
        assert vectors == [[0.1, 0.2]]
        # emb-b пропущен (провайдер мёртв), у e2 успех с первого раза
        assert [r[0] for r in http.requests] == [
            "http://e1/v1/embeddings", "http://e2/v1/embeddings"]


# ═══════════════════════════════════════════════════════════════
# creu/llm.py через OpenAIClient
# ═══════════════════════════════════════════════════════════════

class TestCreuUsesSharedClient:
    def _patch_client(self, monkeypatch, fake_cls):
        monkeypatch.setattr("libs.ai.client.OpenAIClient", fake_cls)

    def test_chat_completion_routes_through_openai_client(self, monkeypatch):
        captured = {}

        class FakeClient:
            def __init__(self, model, temperature=0.8, max_tokens=4096,
                         base_url=None, api_key=None, role=""):
                captured.update(model=model, base_url=base_url,
                                api_key=api_key, role=role)

            async def chat(self, messages, extra_payload=None):
                captured["messages"] = messages
                captured["extra"] = extra_payload
                return {"choices": [{"message": {"content": "эльф"}}]}

        self._patch_client(monkeypatch, FakeClient)
        from libs.creu import llm
        out = asyncio.run(llm.chat_completion(
            [{"role": "user", "content": "раса?"}],
            response_format={"type": "json_object"}))
        assert out == "эльф"
        assert captured["role"] == "creu"
        assert captured["extra"] == {"response_format": {"type": "json_object"}}

    def test_error_in_200_raises_runtime_error(self, monkeypatch):
        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def chat(self, messages, extra_payload=None):
                return {"error": {"message": "boom"},
                        "choices": [{"message": {"content": "x"}}]}

        self._patch_client(monkeypatch, FakeClient)
        from libs.creu import llm
        with pytest.raises(RuntimeError, match="boom"):
            asyncio.run(llm.chat_completion([{"role": "user", "content": "x"}]))

    def test_get_options_fails_soft_when_all_exhausted(self, monkeypatch):
        """Гео-блок всех провайдеров НЕ должен ронять диалог /creu —
        get_options возвращает [] (кнопки просто не показываются)."""
        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def chat(self, messages, extra_payload=None):
                from libs.ai.client import AllProvidersExhausted
                raise AllProvidersExhausted("creu", 4)

        self._patch_client(monkeypatch, FakeClient)
        from libs.creu import llm
        out = asyncio.run(llm.get_options("перечисли расы"))
        assert out == []
