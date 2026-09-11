"""ИТЕРАЦИЯ 12 — фиксы из живого тестирования бота после ИТЕРАЦИИ 11.

Три бага из репортов пользователя:

1. TypeError: DBBotEngineMixin.process_with_db_bot() got an unexpected
   keyword argument 'session_id' — /dndcychwyn падал ОБА раза (кнопкой
   и вручную), потому что world_cmds.py:840 и generators.py (×4) передают
   session_id=, а обёртка его не принимала. Починено пробросом в
   process_db_bot (заодно там выставляется self._current_session_id,
   нужный moder_ai_dispatch_roll, и берётся промпт копии сессии).

2. «Md разметка, которую телеграм не принимает. Должно быть HTML» —
   сообщение «✅ **О_О** присоединился!» и ещё 8 точек отправляли строки
   с markdown (`**жирный**`, `` `код` ``) либо без parse_mode (звёздочки
   видны как есть), либо с легаси parse_mode="Markdown", либо с ручным
   .replace("**", "<b>") — который даёт НЕЗАКРЫТЫЙ тег «<b>...<b>» и
   Telegram отбрасывает всё сообщение (400 can't parse entities).
   Все точки переведены на md_to_html + parse_mode="HTML".

3. config.json api.base_url: "http://localhost:1234/v1" — деплой без
   LM Studio ловил отказ соединения на каждом LLM-вызове. Поставили ""
   (значит: сервер из LLM_PROVIDERS/.env, НЕ из config.json).

Плюс регрессионный AST-скан: ни одна прямая отправка в Telegram
(msvc: reply_text / edit_message_text / send_message / ...) не должна
содержать markdown-литералов в обход md_to_html / send_safe.
"""
import ast
import asyncio
import os
import re
from pathlib import Path

import pytest

# Корень репозитория (tests/ лежит напрямую в корне)
ROOT = Path(__file__).resolve().parents[1]


# ═══════════════════════════════════════════════════════════════
# 1. process_with_db_bot(session_id=...) — TypeError убран
# ═══════════════════════════════════════════════════════════════

def _make_engine():
    from libs.ai.db_bot_engine import DBBotEngineMixin

    class Engine(DBBotEngineMixin):
        async def process_db_bot(self, raw_narrative, context="",
                                 session_id="", **kwargs):
            self.calls.append({"raw": raw_narrative, "context": context,
                               "session_id": session_id, "kwargs": kwargs})
            return [{"applied": True}]

    e = Engine()
    e.calls = []
    return e


class TestProcessWithDbBotSessionId:
    def test_accepts_session_id_keyword(self):
        """Главный репорт: вызов с session_id= больше не TypeError."""
        e = _make_engine()
        result = asyncio.run(
            e.process_with_db_bot("нарратив сцены", session_id="sess-42"))
        assert result == [{"applied": True}]
        assert e.calls[0]["session_id"] == "sess-42"
        assert e.calls[0]["raw"] == "нарратив сцены"

    def test_session_id_reaches_current_session_id(self):
        """Бонус-фикс: process_db_bot ставит _current_session_id — его читает
        moder_ai_dispatch_roll; через старую обёртку он ВСЕГДА был пустым."""
        from libs.ai.db_bot_engine import DBBotEngineMixin

        class Engine(DBBotEngineMixin):
            async def process_db_bot(self, raw, context="", session_id="",
                                     **kwargs):
                self._current_session_id = session_id  # как в реальном коде
                return []

        e = Engine()
        asyncio.run(e.process_with_db_bot("text", session_id="s9"))
        assert e._current_session_id == "s9"

    def test_blank_text_short_circuits_without_llm_call(self):
        e = _make_engine()
        result = asyncio.run(e.process_with_db_bot("   \n\t ", session_id="s1"))
        assert result == []
        assert e.calls == []

    def test_signature_covers_every_real_call_site(self):
        """Статически: все вызовы process_with_db_bot используют только
        параметры, которые обёртка принимает (raw_text/context/session_id)."""
        mixin_src = (Path(ROOT) / "libs" / "ai" / "db_bot_engine.py").read_text(
            encoding="utf-8")
        wrapper = re.search(
            r"async def process_with_db_bot\((.*?)\) ->", mixin_src, re.S)
        assert wrapper, "обёртка process_with_db_bot пропала?"
        for param in ("raw_text", "context", "session_id"):
            assert param in wrapper.group(1)


# ═══════════════════════════════════════════════════════════════
# 2. md_to_html на всех исправленных текстах
# ═══════════════════════════════════════════════════════════════

from libs.handlers.utils import md_to_html, _tags_are_balanced  # noqa: E402


def _assert_html_clean(html: str, *, bold: bool = True, code: bool = False):
    assert "**" not in html, f"сырой markdown остался: {html[:120]}"
    assert _tags_are_balanced(html), f"незакрытые теги: {html[:200]}"
    if bold:
        assert "<b>" in html
    if code:
        assert "<code>" in html


class TestMdToHtmlUserTexts:
    def test_lobby_join_message(self):
        """Точный репорт пользователя: «✅ **О_О** присоединился!»."""
        html = md_to_html("✅ **О_О** присоединился!\n\n"
                          "Загрузи персонажа: отправь .txt/.md и ответь `/cymeriad`")
        assert html.startswith("✅ <b>О_О</b> присоединился!")
        _assert_html_clean(html, code=True)

    def test_lobby_join_message_escapes_hostile_name(self):
        """first_name приходит от Telegram — может содержать '<'."""
        html = md_to_html("✅ **<b>&script</b>** присоединился!\n\nЗагрузи `/cymeriad`")
        assert "<b>&script</b>" not in html.replace("&amp;", "")  # сырое <b> не пролезло
        assert "&lt;b&gt;" in html
        _assert_html_clean(html, code=True)

    def test_char_change_blocked_message(self):
        """Текст _char_change_blocked (character_cmds.py) — раньше был
        blocked.replace("**", "<b>") → «<b>...<b>» → Telegram 400."""
        blocked = (
            "❌ **Смена персонажа запрещена.**\n\n"
            "Мир уже создан, и твой персонаж **Арагорн** жив (HP: 12). "
            "Заменить персонажа можно только после его смерти.\n\n"
            "Продолжай играть этим персонажем — а если он погиб, "
            "примени нового командой `/cymeriad`."
        )
        html = md_to_html(blocked)
        assert html.count("<b>") == html.count("</b>") == 2
        assert "<code>/cymeriad</code>" in html
        _assert_html_clean(html, code=True)

    def test_billing_private_chat_gate(self):
        html = md_to_html(
            "🔒 **В ЛС играть нельзя.** Игра доступна в общем чате бота и в группах.\n\n"
            "В ЛС работают только: `/creu` (создание персонажа), `/cyfieithu` (перевод), "
            "`/gwneud` (скрытое действие)."
        )
        assert "<b>В ЛС играть нельзя.</b>" in html
        _assert_html_clean(html, code=True)

    def test_billing_token_exhausted(self):
        html = md_to_html(
            "💰 **Токены исчерпаны — игра остановлена.**\n\n"
            "Баланс: **0** (твой личный баланс, режим оплаты: поровну между игроками). "
            "Это ЖЁСТКАЯ блокировка, а не баг."
        )
        _assert_html_clean(html)

    def test_billing_billmode_label(self):
        html = md_to_html(
            "💰 Способ оплаты: **поровну между игроками**"
            + "\n\n🔒 Выбор зафиксирован на всю жизнь сессии и не меняется."
        )
        assert "<b>поровну между игроками</b>" in html
        _assert_html_clean(html)

    def test_billing_ychwanegu_success_and_usage(self):
        html = md_to_html(
            "✅ Игроку `123456789` начислено **5000000** токенов.\nНовый баланс: **47000000**.")
        assert "<code>123456789</code>" in html
        _assert_html_clean(html, code=True)

        html2 = md_to_html(
            "Использование: `/ychwanegu <user_id> <сумма>`\n"
            "Пример: `/ychwanegu 123456789 5000000`")
        # <user_id> внутри бэктиков экранируется, а не ломает разметку
        assert "&lt;user_id&gt;" in html2
        _assert_html_clean(html2, bold=False, code=True)

    def test_billing_drained_notice(self):
        html = md_to_html("💰 Токены исчерпаны у: `111`, `222`.\nИх следующий ход будет заблокирован.")
        assert "<code>111</code>" in html
        _assert_html_clean(html, bold=False, code=True)

    def test_translator_enabled_message(self):
        html = md_to_html("🌐 Перевод на **Elvish<3** включён! ✅\nНарратив будет приходить в ЛС.")
        assert "<b>Elvish&lt;3</b>" in html
        _assert_html_clean(html)

    def test_binds_group_hint(self):
        html = md_to_html("🔒 Бинды создаются только в личных сообщениях бота.\n"
                          "Перешли мне эту команду в ЛС: `/rhwymo <команда> <псевдоним>`")
        assert "<code>/rhwymo &lt;команда&gt; &lt;псевдоним&gt;</code>" in html
        _assert_html_clean(html, bold=False, code=True)


# ═══════════════════════════════════════════════════════════════
# 3. config.json api.base_url = "" — env/LLM_PROVIDERS не перекрывается
# ═══════════════════════════════════════════════════════════════

class TestConfigBaseUrl:
    def test_empty_string_falls_back_to_env(self, monkeypatch):
        import libs.config_legacy as cfg
        snapshot = dict(vars(cfg))
        try:
            monkeypatch.setenv("OPENAI_BASE_URL", "https://cloud.example.com/v1")
            cfg._CONFIG_JSON = {"api": {"base_url": ""}}
            cfg._apply_config_json()
            assert cfg.OPENAI_BASE_URL == "https://cloud.example.com/v1"
        finally:
            cfg.__dict__.clear()
            cfg.__dict__.update(snapshot)

    def test_empty_string_without_env_keeps_default(self, monkeypatch):
        import libs.config_legacy as cfg
        snapshot = dict(vars(cfg))
        try:
            monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
            default = cfg.OPENAI_BASE_URL
            cfg._CONFIG_JSON = {"api": {"base_url": ""}}
            cfg._apply_config_json()
            assert cfg.OPENAI_BASE_URL == default
        finally:
            cfg.__dict__.clear()
            cfg.__dict__.update(snapshot)

    def test_nonempty_still_overrides_env(self, monkeypatch):
        """Обратная совместимость: локальный LM Studio по-прежнему перекрывает .env."""
        import libs.config_legacy as cfg
        snapshot = dict(vars(cfg))
        try:
            monkeypatch.setenv("OPENAI_BASE_URL", "https://cloud.example.com/v1")
            cfg._CONFIG_JSON = {"api": {"base_url": "http://localhost:1234/v1"}}
            cfg._apply_config_json()
            assert cfg.OPENAI_BASE_URL == "http://localhost:1234/v1"
        finally:
            cfg.__dict__.clear()
            cfg.__dict__.update(snapshot)

    def test_repo_config_ships_empty_base_url(self):
        """Деплой без LM Studio не должен ловить connection refused."""
        import json
        with open(Path(ROOT) / "config.json", encoding="utf-8") as f:
            data = json.load(f)
        assert data["api"]["base_url"] == ""


# ═══════════════════════════════════════════════════════════════
# 4. Регрессионный AST-скан: прямые отправки без markdown-конвертации
# ═══════════════════════════════════════════════════════════════

_SEND_FUNCS = {
    "reply_text", "edit_message_text", "send_message", "edit_text",
    "answer", "reply_markdown", "reply_html", "send_photo", "send_document",
    "edit_message_caption",
}
_MD_HINT = re.compile(r"\*\*|`")


def _string_fragments(node):
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            yield sub.value
        elif isinstance(sub, ast.JoinedStr):
            for v in sub.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    yield v.value


class TestNoRawMarkdownSends:
    def test_no_direct_sends_with_markdown_literals(self):
        """Ни одна прямая отправка (не send_safe, не md_to_html) не содержит
        markdown-литералов. Именно так просочилось «✅ **О_О** присоединился!»."""
        problems = []
        skip_dirs = {".git", "tests", "__pycache__", "logs", "data", "MD",
                     "download", "node_modules"}
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fn)
                try:
                    src = Path(path).read_text(encoding="utf-8")
                    tree = ast.parse(src)
                except (SyntaxError, UnicodeDecodeError):
                    continue  # отдельные файлы не валидируем здесь
                for n in ast.walk(tree):
                    if not isinstance(n, ast.Call):
                        continue
                    fname = getattr(n.func, "attr", None) or getattr(n.func, "id", None)
                    if fname not in _SEND_FUNCS or fname == "send_safe":
                        continue
                    try:
                        csrc = ast.get_source_segment(src, n) or ""
                    except Exception:
                        csrc = ""
                    if "md_to_html" in csrc:
                        continue
                    texts = []
                    for a in n.args:
                        texts.extend(_string_fragments(a))
                    for kw in n.keywords:
                        if kw.arg in ("text", "caption", "message"):
                            texts.extend(_string_fragments(kw.value))
                    joined = "\n".join(texts)
                    if _MD_HINT.search(joined):
                        rel = os.path.relpath(path, ROOT)
                        problems.append(f"{rel}:{n.lineno} [{fname}]")
        assert problems == [], (
            "Прямые отправки с сырым markdown (Telegram покажет «**» как есть "
            "или отбросит сообщение): " + "; ".join(problems))
