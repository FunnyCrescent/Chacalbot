"""ИТЕРАЦИЯ 14 — само-лечение payload: «Unknown name "reasoning"».

Живой репорт: /dndcychwyn падал с «API 400: Invalid JSON payload received.
Unknown name "reasoning": Cannot find field». Причина: thinking-режим мастера
добавляет в payload {"reasoning": {"enabled": true}} (формат OpenRouter), а
Google-совместимый шлюз отклоняет незнакомое поле честной 400, которая по
Разделу 8 уходит наружу без перебора. Но провайдер ЗДОРОВЫЙ — виновато
опциональное поле payload.

Фикс: при 400 «Unknown name "X"» на НЕстандартном поле — повтор ТОГО ЖЕ
провайдера/модели без всех нестандартных полей + in-process memo (последующие
вызовы пары сразу шлют чистый payload). Стандартные поля (messages/tools/…)
не трогаются: Unknown name на них — честная ошибка запроса.
"""
import asyncio
import json

import pytest


REASONING_400_BODY = json.dumps([{
    "error": {
        "code": 400,
        "message": 'Invalid JSON payload received. Unknown name "reasoning": Cannot find field.',
        "status": "INVALID_ARGUMENT",
    }
}])

TOOLS_400_BODY = json.dumps({
    "error": {"code": 400,
              "message": 'Invalid JSON payload received. Unknown name "tools": Cannot find field.'},
})

PLAIN_400_BODY = json.dumps({
    "error": {"code": 400, "message": "Invalid request schema: 'messages' must be a list"},
})

OK_BODY = json.dumps({
    "choices": [{"message": {"content": "ок", "role": "assistant"}}],
    "usage": {"total_tokens": 0},
})


# ═══════════════════════════════════════════════════════════════
# FakeHTTP — как в test_iter11, но журнал хранит ПОЛНЫЙ payload
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
    """Подменяет aiohttp.ClientSession в libs.ai.client.
    requests — журнал (url, payload_dict) всех POST-ов."""

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
                outer.requests.append((url, dict(json or {})))
                status, body = outer.queue.pop(0)
                return _PostCtx(status, body)

        import libs.ai.client as client_mod
        monkeypatch.setattr(client_mod.aiohttp, "ClientSession", FakeSession)


@pytest.fixture(autouse=True)
def _clean_memo():
    """In-process memo пар не должен перетекать между тестами."""
    import libs.ai.client as client_mod
    client_mod._PAYLOAD_STRIPE.clear()
    yield
    client_mod._PAYLOAD_STRIPE.clear()


def _one_provider():
    return [{"name": "google1", "base_url": "http://g1/v1",
             "api_key": "k1", "models": ["gem-a"]}]


# ═══════════════════════════════════════════════════════════════
# Хелперы: парсинг Unknown name + снятие опциональных полей
# ═══════════════════════════════════════════════════════════════

class TestUnknownFieldHelpers:

    def test_single_field(self):
        from libs.ai.client import _unknown_field_names
        assert _unknown_field_names(REASONING_400_BODY) == ["reasoning"]

    def test_multiple_fields(self):
        from libs.ai.client import _unknown_field_names
        body = 'Unknown name "reasoning": Cannot find field; Unknown name "thinking": ...'
        assert _unknown_field_names(body) == ["reasoning", "thinking"]

    def test_case_insensitive(self):
        from libs.ai.client import _unknown_field_names
        assert _unknown_field_names('unknown name "Reasoning": nope') == ["Reasoning"]

    def test_no_match(self):
        from libs.ai.client import _unknown_field_names
        assert _unknown_field_names(PLAIN_400_BODY) == []
        assert _unknown_field_names("") == []
        assert _unknown_field_names(None) == []

    def test_strip_removes_only_extras(self):
        from libs.ai.client import _strip_optional_extras
        payload = {
            "model": "gem-a", "messages": [], "temperature": 0.7,
            "max_tokens": 100, "stream": False,
            "reasoning": {"enabled": True},
            "response_format": {"type": "json_object"},
            "custom_knob": 1,
        }
        stripped = _strip_optional_extras(payload)
        assert set(stripped) == {"model", "messages", "temperature", "max_tokens", "stream"}

    def test_strip_keeps_tools(self):
        from libs.ai.client import _strip_optional_extras
        payload = {"model": "m", "messages": [], "tools": [{"x": 1}], "tool_choice": "auto"}
        assert _strip_optional_extras(payload) == payload


# ═══════════════════════════════════════════════════════════════
# Сквозные сценарии chat() на FakeHTTP
# ═══════════════════════════════════════════════════════════════

class TestSelfHeal:

    def test_unknown_reasoning_retries_without_extras(self, monkeypatch):
        """Точный кейс пользователя: 400 Unknown name "reasoning" → тот же
        провайдер, повтор без опциональных полей → успех."""
        from libs.ai.client import OpenAIClient
        monkeypatch.setattr("libs.ai.client.get_providers_for_role",
                            lambda role, **kw: _one_provider())
        http = FakeHTTP(monkeypatch, [(400, REASONING_400_BODY), (200, OK_BODY)])
        c = OpenAIClient("gem-a", role="master")
        result = asyncio.run(c.chat([{"role": "user", "content": "привет"}]))
        assert result["choices"][0]["message"]["content"] == "ок"
        # Оба запроса — к ОДНОЙ паре провайдер+модель, без перебора чужих.
        assert [url for url, _ in http.requests] == [
            "http://g1/v1/chat/completions", "http://g1/v1/chat/completions"]
        # Первый запрос нёс thinking-экстру, второй — чистый.
        assert "reasoning" in http.requests[0][1]
        assert "reasoning" not in http.requests[1][1]
        assert http.requests[1][1]["messages"] == http.requests[0][1]["messages"]

    def test_memo_second_call_sends_clean_payload_immediately(self, monkeypatch):
        """После само-лечения пара запомнена: следующий вызов сразу шлёт
        чистый payload — лишнего round-trip нет."""
        from libs.ai.client import OpenAIClient
        monkeypatch.setattr("libs.ai.client.get_providers_for_role",
                            lambda role, **kw: _one_provider())
        FakeHTTP(monkeypatch, [(400, REASONING_400_BODY), (200, OK_BODY)])
        c = OpenAIClient("gem-a", role="master")
        asyncio.run(c.chat([{"role": "user", "content": "раз"}]))

        http2 = FakeHTTP(monkeypatch, [(200, OK_BODY)])
        result = asyncio.run(c.chat([{"role": "user", "content": "два"}]))
        assert result["choices"][0]["message"]["content"] == "ок"
        assert len(http2.requests) == 1
        assert "reasoning" not in http2.requests[0][1]

    def test_unknown_standard_field_still_raises(self, monkeypatch):
        """Unknown name на СТАНДАРТНОМ поле (tools) — честная ошибка запроса,
        снятие поля недопустимо (молча отключил бы инструменты Мастера)."""
        from libs.ai.client import OpenAIClient, ApiError
        monkeypatch.setattr("libs.ai.client.get_providers_for_role",
                            lambda role, **kw: _one_provider())
        http = FakeHTTP(monkeypatch, [(400, TOOLS_400_BODY)])
        c = OpenAIClient("gem-a", role="master")
        with pytest.raises(ApiError):
            asyncio.run(c.chat([{"role": "user", "content": "привет"}],
                               tools=[{"type": "function", "function": {"name": "f"}}]))
        assert len(http.requests) == 1

    def test_plain_400_without_unknown_name_untouched(self, monkeypatch):
        """Честная 400 без Unknown name — наружу без самодеятельности."""
        from libs.ai.client import OpenAIClient, ApiError
        monkeypatch.setattr("libs.ai.client.get_providers_for_role",
                            lambda role, **kw: _one_provider())
        http = FakeHTTP(monkeypatch, [(400, PLAIN_400_BODY)])
        c = OpenAIClient("gem-a", role="master")
        with pytest.raises(ApiError):
            asyncio.run(c.chat([{"role": "user", "content": "привет"}]))
        assert len(http.requests) == 1

    def test_unknown_reasoning_with_clean_payload_raises(self, monkeypatch):
        """Шлюз ругается на reasoning, но в payload его НЕТ (role=db без
        thinking) — снимать нечего, честная ошибка наружу (не мёмozoим)."""
        from libs.ai.client import OpenAIClient, ApiError
        monkeypatch.setattr("libs.ai.client.get_providers_for_role",
                            lambda role, **kw: _one_provider())
        http = FakeHTTP(monkeypatch, [(400, REASONING_400_BODY)])
        c = OpenAIClient("gem-a", role="db")
        with pytest.raises(ApiError):
            asyncio.run(c.chat([{"role": "user", "content": "привет"}]))
        assert len(http.requests) == 1

    def test_thinking_applied_when_supported(self, monkeypatch):
        """Регресс: если шлюз всё принимает, thinking-экстра ДОЛЖНА уходить
        (пользователь специально включил думающую модель)."""
        from libs.ai.client import OpenAIClient
        monkeypatch.setattr("libs.ai.client.get_providers_for_role",
                            lambda role, **kw: _one_provider())
        http = FakeHTTP(monkeypatch, [(200, OK_BODY)])
        c = OpenAIClient("gem-a", role="master")
        asyncio.run(c.chat([{"role": "user", "content": "привет"}]))
        assert http.requests[0][1].get("reasoning") == {"enabled": True}

    def test_provider_switch_still_works_after_selfheal_mark(self, monkeypatch):
        """Memo не ломает Раздел 8: после само-лечения пары 401 у провайдера №1
        по-прежнему переключает перебор на провайдера №2."""
        from libs.ai.client import OpenAIClient
        provs = _one_provider() + [
            {"name": "google2", "base_url": "http://g2/v1",
             "api_key": "k2", "models": ["gem-b"]}]
        monkeypatch.setattr("libs.ai.client.get_providers_for_role",
                            lambda role, **kw: provs)
        http = FakeHTTP(monkeypatch, [
            (400, REASONING_400_BODY),          # google1/gem-a → self-heal → OK
            (200, OK_BODY),                     # google1/gem-a OK (memo: чистый payload)
            (401, json.dumps({"error": {"message": "invalid key"}})),  # google1 → switch
            (200, OK_BODY),                     # google2/gem-b — успех
        ])
        c = OpenAIClient("gem-a", role="master")
        r1 = asyncio.run(c.chat([{"role": "user", "content": "раз"}]))
        assert "reasoning" not in http.requests[-1][1]  # второй вызов уже чистый (memo)
        r2 = asyncio.run(c.chat([{"role": "user", "content": "два"}],
                                retries=1))
        assert r1["choices"][0]["message"]["content"] == "ок"
        assert r2["choices"][0]["message"]["content"] == "ок"
        assert [u for u, _ in http.requests] == [
            "http://g1/v1/chat/completions",
            "http://g1/v1/chat/completions",
            "http://g1/v1/chat/completions",
            "http://g2/v1/chat/completions",
        ]
        # Провайдер №2 НЕ помечен — его вызов нёс thinking-экстру как обычно.
        assert http.requests[3][1].get("reasoning") == {"enabled": True}
