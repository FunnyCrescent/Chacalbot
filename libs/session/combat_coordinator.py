"""CombatCoordinatorMixin — initiative combat, encounters, action groups."""
import json
import logging
import uuid
import asyncio
import random
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any

from libs.ai_client import DMEngine, strip_stray_tags
from libs.db import (
    Database, DatabaseManager, HistoryEntry, Player, QueueState, Session,
    Location, LocationPath, WorldNpc, NpcRelation, LoreArticle,
    MarketPrice, EconomicEvent, ActiveEffect, Timer, LocationRelation,
    CombatEncounter, Combatant, PlayerLanguage,
)
from libs.config_legacy import (
    COMBAT_INITIATIVE_ENABLED, DUAL_NARRATIVE_ENABLED,
    NPC_AI_ENABLED, NPC_AI_MODE,
)
from libs.memory_store import MemoryStore

logger = logging.getLogger(__name__)

# БОЙ-FIX v3: сессии, в которых рассинхрон боевого состояния (combat_active=True
# при отсутствии активного CombatEncounter) был обнаружен и аварийно вылечен.
# Нужен, чтобы _process_dn_action мог сообщить игрокам ПОЧЕМУ бой внезапно
# закончился (одно уведомление на инцидент, а не на каждый чих).
_COMBAT_DESYNC_HANDLED: set = set()


def _norm_name(s: str) -> str:
    """casefold + выбросить всё, кроме букв/цифр (пробелы, пунктуация, @, _)."""
    return re.sub(r"[\W_]+", "", (s or "").casefold(), flags=re.UNICODE)


def _name_tokens(s: str) -> List[str]:
    return [t for t in re.split(r"[\W_]+", (s or "").casefold(), flags=re.UNICODE) if t]


def match_participant_to_character(name: str, chars, players):
    """БОЙ-FIX: сопоставить имя участника из tool-call Мастера с персонажем сессии.

    Мастер передаёт имена из нарратива — они регулярно отличаются от листа:
    «Храфна Морвен» вместо «Храфна», @username вместо имени, лишняя
    пунктуация. Раньше был только точный name.lower() match: промах означал,
    что ЖИВОЙ ИГРОК регистрировался в бою как NPC с player_id=0 — бот считал
    его «не в бою», его ход уходил в параллельный dual-narrative резолвер,
    и получалась та самая гонка, из-за которой прыгали раунды и расходились HP.

    Порядок попыток (каждая следующая — только если предыдущие не дали ответа):
      1. точное совпадение имени персонажа (casefold);
      2. точное совпадение с @username / display_name игрока персонажа;
      3. нормализованное совпадение (без пунктуации/пробелов/регистра);
      4. покрытие токенов: токены имени персонажа встречаются в токенах
         имени участника, покрытие ≥ 0.5 и однозначное. «Храфна Морвен» →
         «Храфна» (0.5 ✓); «Воин с мечом СЛИЗЬ» → «СЛИЗЬ» (0.25 ✗) — монстр
         с именем игрока внутри больше НЕ притягивается к персонажу (тот же
         класс бага, что BUG #3 с «Воин с мечом СЛИЗЬ»).

    Возвращает персонажа или None. None = участник регистрируется как NPC.
    """
    target = (name or "").strip()
    if not target or not chars:
        return None
    low = target.casefold().lstrip("@")
    norm = _norm_name(target)
    target_tokens = _name_tokens(target)

    # 1. Точное совпадение имени персонажа.
    for c in chars:
        if c.name and c.name.casefold().lstrip("@") == low:
            return c

    # 2. Точное совпадение с @username / display_name игрока этого персонажа.
    player_by_id = {p.user_id: p for p in (players or [])}
    for c in chars:
        p = player_by_id.get(getattr(c, "player_id", 0))
        if not p:
            continue
        for alias in (getattr(p, "username", ""), getattr(p, "display_name", "")):
            if alias and alias.casefold().lstrip("@") == low:
                return c

    # 3. Нормализованное совпадение (регистр/пунктуация/пробелы не важны).
    if norm:
        for c in chars:
            if c.name and _norm_name(c.name) == norm:
                return c

    # 4. Покрытие токенов, ≥ 0.5, однозначное.
    best, best_cov, ties = None, 0.0, 0
    for c in chars:
        if not c.name:
            continue
        char_tokens = _name_tokens(c.name)
        if not char_tokens or not target_tokens:
            continue
        hit = sum(1 for t in char_tokens if t in target_tokens)
        cov = hit / len(target_tokens)
        if cov > best_cov:
            best, best_cov, ties = c, cov, 1
        elif cov == best_cov and cov > 0:
            ties += 1
    if best is not None and best_cov >= 0.5 and ties == 1:
        return best
    return None


class CombatCoordinatorMixin:
    """CombatCoordinatorMixin — initiative combat, encounters, action groups."""

    def start_combat(self, session_id: str) -> str:
        """БОЙ-FIX v3 (Причина #1 из ТЗ): легаси-обёртка над ЕДИНЫМ V10b-путём.

        Раньше этот метод ставил combat_active=True ПРЯМО в строку сессии,
        не создавая CombatEncounter/Combatant. Весь per-turn боевой движок
        (get_current_initiative_turn / get_combat_action_group / боевой цикл
        _start_combat_turn_loop) читает состояние ИЗ ЭТИХ ТАБЛИЦ — без них
        он отдавал заглушку «никто не заблокирован»: очередь рассинхронизиро-
        валась, раунд не резолвился НИКОГДА, бой зависал (см. ТЗ «Причина #1»).

        Теперь легаси-вызовы получают ровно то же состояние БД, что и старт
        боя из нарратива (start_combat_for_participants): encounter +
        combatants + серверная инициатива. Очередь НЕ создаётся — её ведёт
        боевой цикл (вызывается отдельно, см. combat_cmds.combat_cmd).
        Сообщение сохраняет старый формат (обратная совместимость)."""
        db = self.db_manager.get_db(session_id)
        session = db.get_session(session_id)
        if not session:
            return "No active session."
        if session.combat_active:
            # БОЙ-FIX v3: не глотаем отказ — идемпотентность повторного старта.
            return "Combat is already active — do not call start_combat again this fight."

        chars = [c for c in db.get_session_characters(session_id)
                 if getattr(c, "is_alive", True)]
        participants = [c.name for c in chars]
        if not participants:
            return "Error: no alive participants to start combat."

        result = self.start_combat_for_participants(
            session_id, participants, reason="по команде /combat")
        if result.startswith("Error"):
            return result

        # Формируем легаси-текст из свежего порядка инициативы.
        encounter = db.get_active_combat_encounter(session_id)
        order = db.get_initiative_order(encounter.id) if encounter else []
        lines = ["⚔️ **БОЙ НАЧИНАЕТСЯ!** ⚔️", "", "*Порядок ходов:*"]
        for i, e in enumerate(order, 1):
            lines.append(f"{i}. {e['name']} (Инициатива: {e['initiative']})")
        if order:
            lines.append(f"\n🎲 Раунд 1 — ход **{order[0]['name']}**!")
        return "\n".join(lines)


    async def _combat_starter_async(self, session_id: str, participant_names: List[str], reason: str, pc_initiatives: Dict = None, priorities: Dict = None) -> str:
        """Async wrapper so ai_client.py's Callable[..., Awaitable[str]] type holds —
        start_combat_for_participants itself is plain sync DB work, no I/O to await.
        pc_initiatives: dict of {character_name: {natural: int, total: int}} from player_roll_requester.
        priorities: тай-брейк инициативы {имя: int} — меньше = раньше (Раздел 4)."""
        return self.start_combat_for_participants(
            session_id, participant_names, reason,
            pc_initiatives=pc_initiatives, priorities=priorities)


    async def _combat_ender_async(self, session_id: str, reason: str) -> str:
        """Async wrapper for end_initiative_combat."""
        return self.end_initiative_combat(session_id, reason)

    # ═══════════════════════════════════════════════════════════
    # ДОПОЛНЕНИЕ 1: скрытый канал Мастер → DB о состоянии НПС
    # ═══════════════════════════════════════════════════════════
    # Мастер ОБЯЗАН передавать HP, состояния (сбит с ног и т.п.) и важную
    # секретную информацию о НПС в DB-слой СКРЫТНО. Хранилище:
    #   • в бою — строка Combatant активного encounter (hp/max_hp/
    #     current_conditions/is_alive/traits) — источник для боевой сводки;
    #   • вне боя — session_meta "npc_states" (скрытая записная книжка).
    # Игроки не видят ни вызов инструмента, ни результаты; раскрытие HP
    # игрокам — только через нарратив Мастера после законной проверки
    # (Внимательность/Расследование/Медицина и т.п.), см. промпты.

    NPC_STATES_META_KEY = "npc_states"

    def sync_npc_state(self, session_id: str, payload: Dict[str, Any]) -> str:
        """Persist hidden NPC state from the Master's diweddaru_npc tool call."""
        db = self.db_manager.get_db(session_id)
        name = (payload.get("npc_name") or "").strip()
        if not name:
            return "NPC state not saved: npc_name is empty."

        conditions = payload.get("conditions")
        conditions_list: Optional[List[str]] = None
        if isinstance(conditions, list):
            conditions_list = [str(c).strip() for c in conditions if str(c).strip()]

        hp_current = payload.get("hp_current")
        hp_max = payload.get("hp_max")
        is_alive = payload.get("is_alive")
        note = (payload.get("note") or "").strip()
        revealed = bool(payload.get("revealed", False))

        # 1) В бою — обновляем Combatant активного encounter (если найден).
        encounter = db.get_active_combat_encounter(session_id)
        if encounter:
            combatants = db.get_combatants(encounter.id)
            target = None
            name_lower = name.lower()
            for c in combatants:
                if c.name.lower() == name_lower:
                    target = c
                    break
            if target is None:
                # частичный матч — «Разбойник 2», «Гоблин-вожак» и т.п.
                for c in combatants:
                    if name_lower in c.name.lower() or c.name.lower() in name_lower:
                        target = c
                        break
            if target is not None:
                updates: Dict[str, Any] = {}
                if isinstance(hp_current, int):
                    max_hp = hp_max if isinstance(hp_max, int) and hp_max > 0 else (target.max_hp or hp_current)
                    updates["hp"] = max(0, min(int(hp_current), int(max_hp)))
                    if updates["hp"] <= 0:
                        updates["is_alive"] = False
                if isinstance(hp_max, int) and hp_max > 0:
                    updates["max_hp"] = int(hp_max)
                if conditions_list is not None:
                    updates["current_conditions"] = json.dumps(conditions_list, ensure_ascii=False)
                if note:
                    # секретные заметки хранятся в traits (JSON: note/revealed)
                    try:
                        traits = json.loads(target.traits) if target.traits else {}
                    except Exception:
                        traits = {}
                    if not isinstance(traits, dict):
                        traits = {}
                    traits["gm_note"] = note
                    traits["revealed"] = revealed
                    updates["traits"] = json.dumps(traits, ensure_ascii=False)
                elif revealed:
                    try:
                        traits = json.loads(target.traits) if target.traits else {}
                    except Exception:
                        traits = {}
                    if not isinstance(traits, dict):
                        traits = {}
                    traits["revealed"] = True
                    updates["traits"] = json.dumps(traits, ensure_ascii=False)
                if is_alive is False:
                    updates["is_alive"] = False
                    if "hp" not in updates and isinstance(hp_current, int) is False:
                        updates.setdefault("hp", 0)
                if updates:
                    db.update_combatant(target.id, **updates)
                    # мёртвые combatant'ы выпадают из инициативы — порядок
                    # перечитается сам при следующем get_initiative_order.
                return (f"СКРЫТО сохранено: {target.name} — "
                        f"HP {updates.get('hp', target.hp)}/{updates.get('max_hp', target.max_hp)}"
                        + (f", состояния: {', '.join(conditions_list)}" if conditions_list is not None else "")
                        + ("." if not note else f", заметка записана."))

        # 2) Вне боя (или НПС не участник боя) — скрытая записная книжка в мете.
        try:
            states = json.loads(db.get_meta(self.NPC_STATES_META_KEY, "{}"))
            if not isinstance(states, dict):
                states = {}
        except Exception:
            states = {}
        entry = states.get(name.lower(), {})
        if not isinstance(entry, dict):
            entry = {}
        if isinstance(hp_current, int):
            entry["hp"] = int(hp_current)
        if isinstance(hp_max, int) and hp_max > 0:
            entry["hp_max"] = int(hp_max)
        if conditions_list is not None:
            entry["conditions"] = conditions_list
        if is_alive is not None:
            entry["is_alive"] = bool(is_alive)
        if note:
            entry["note"] = note
        if revealed:
            entry["revealed"] = True
        entry["updated_at"] = datetime.now().isoformat(timespec="seconds")
        states[name.lower()] = entry
        db.set_meta(self.NPC_STATES_META_KEY, json.dumps(states, ensure_ascii=False))
        return f"СКРЫТО сохранено: {name} (вне боя)."

    async def _npc_state_syncer_async(self, session_id: str, payload: Dict[str, Any]) -> str:
        """Async wrapper for sync_npc_state (used as npc_state_syncer callback)."""
        return self.sync_npc_state(session_id, payload)

    def get_hidden_npc_states(self, session_id: str) -> Dict[str, Any]:
        """Read the hidden out-of-combat NPC notebook (for master context)."""
        db = self.db_manager.get_db(session_id)
        try:
            states = json.loads(db.get_meta(self.NPC_STATES_META_KEY, "{}"))
            return states if isinstance(states, dict) else {}
        except Exception:
            return {}

    # ═════════════════════════════════════════════════════════
    # БОЙ-FIX v2: вступление игрока в УЖЕ ИДУЩИЙ бой (ymuno_ymladd)
    # ═════════════════════════════════════════════════════════
    # Реальный кейс из лога сессии: бой Храфна+СЛИЗЬ уже идёт, Рими пишет
    # «Дн. бегу к слизи, наношу удары» — а механизма «добавь меня в бой»
    # НЕ СУЩЕСТВОВАЛО. Рими запирался в non-combat lane («Ты не в бою —
    # Мастер разрешит отдельно»), его атаки резолвились ПАРАЛЛЕЛЬНЫМ
    # вызовом Мастера (двойной нарратив одной сцены), а главная очередь
    # вечно ждала его («Ждём: Храфна Морвен, Рими») — дедлок.
    # Теперь Мастер имеет инструмент ymuno_ymladd: он по тексту действия
    # решает, что игрок вмешивается в бой, и система добавляет его в
    # инициативу, не ломая текущий ход.

    def join_active_combat(self, session_id: str, char) -> Dict[str, Any]:
        """Добавить персонажа-ИГРОКА (char с player_id > 0) в активный бой.

        Правила вставки, чтобы не сломать идущий раунд:
          • новый Combatant пишется в БД (pc, player_id > 0);
          • инициатива — серверный d20 + DEX-мод (кнопка тут неуместна:
            игрок уже отправил действие, его ход настанет, когда дойдёт очередь);
          • session.current_turn_index пересчитывается так, чтобы ХОДЯЩИЙ
            сейчас участник остался ходящим — вставка не скипает и не
            дублирует ничей ход (присоединившийся действует, когда до
            него дойдёт очередь, возможно всё ещё в текущем раунде).

        Возвращает dict: {joined: bool, reason/name/initiative/...}.
        """
        db = self.db_manager.get_db(session_id)
        session = db.get_session(session_id)
        if not session or not session.combat_active:
            return {"joined": False, "reason": "no_combat"}
        encounter = db.get_active_combat_encounter(session_id)
        if not encounter:
            return {"joined": False, "reason": "no_combat"}
        player_id = getattr(char, "player_id", 0) or 0
        if player_id <= 0:
            return {"joined": False, "reason": "unlinked", "name": getattr(char, "name", "?")}

        # Уже в бою?
        for c in db.get_combatants(encounter.id):
            if c.player_id == player_id and c.is_alive:
                return {"joined": False, "reason": "already", "name": c.name}

        # Кто ходит прямо сейчас — фиксируем ДО вставки, чтобы пересчитать
        # индекс после сортировки и не передать ход другому участнику.
        prev_current = self.get_current_initiative_turn(session_id)

        stats = json.loads(char.stats) if char.stats else {}
        dex_mod = (stats.get("dexterity", 10) - 10) // 2
        roll_nat = random.randint(1, 20)
        initiative = roll_nat + dex_mod

        existing = db.get_combatants(encounter.id)
        max_sort = max((c.sort_order for c in existing), default=0)
        combatant = Combatant(
            id=uuid.uuid4().hex[:12], encounter_id=encounter.id, session_id=session_id,
            name=char.name, entity_type="pc", player_id=player_id,
            initiative=initiative, natural_roll=roll_nat, dex_mod=dex_mod,
            hp=getattr(char, "hp", 0) or 0, max_hp=getattr(char, "max_hp", 0) or 0,
            ac=getattr(char, "ac", 10) or 10,
            sort_order=max_sort + 1,
        )
        db.add_combatant(combatant)

        # Пересобираем порядок инициативы и чиним индекс текущего хода.
        order = db.get_initiative_order(encounter.id)
        session.initiative_order = json.dumps([
            {"name": e["name"], "player_id": e["player_id"], "initiative": e["initiative"]}
            for e in order
        ])
        if prev_current:
            for i, e in enumerate(order):
                if (e["name"] == prev_current.get("name")
                        and e.get("player_id") == prev_current.get("player_id")):
                    session.current_turn_index = i
                    break
        db.update_session(session)

        db.add_history(HistoryEntry(
            session_id=session_id, author="DM",
            content=(f"{char.name} вступает в бой (инициатива {initiative}). "
                     f"Порядок: " + ", ".join(f"{e['name']} {e['initiative']}" for e in order)),
            entry_type="combat",
        ))

        logger.info(f"[combat-join] {char.name} (player {player_id}) joined combat in {session_id}: "
                    f"initiative {initiative} (d20={roll_nat}{dex_mod:+d})")
        return {"joined": True, "name": char.name, "initiative": initiative,
                "natural": roll_nat, "dex_mod": dex_mod, "order": order}

    def join_active_combat_by_name(self, session_id: str, name: str) -> str:
        """Версия для tool-call Мастера (ymuno_ymladd): имя из нарратива →
        персонаж сессии через match_participant_to_character → join_active_combat.
        Возвращает ТЕКСТ для tool-result (Мастер читает его и пишет нарратив)."""
        db = self.db_manager.get_db(session_id)
        session_chars = db.get_session_characters(session_id)
        players = db.get_players(session_id)
        alive = [c for c in session_chars if getattr(c, "is_alive", True)]
        char = match_participant_to_character(name, alive, players)
        if not char:
            return (f"Персонаж '{name}' не найден среди персонажей сессии — "
                    "добавить в бой нельзя. Разреши действие нарративно.")
        result = self.join_active_combat(session_id, char)
        if result.get("joined"):
            self.record_combat_join(session_id, result)
            return (f"{result['name']} добавлен(а) в бой! Инициатива: {result['initiative']} "
                    f"(d20={result['natural']}{result['dex_mod']:+d}). "
                    "Он(а) действует, когда до него(её) дойдёт очередь инициативы. "
                    "Броски этого игрока запрашивай через request_player_roll на ЕГО ходу.")
        if result.get("reason") == "already":
            return (f"{result.get('name', char.name)} уже участвует в этом бою — "
                    "повторно добавлять не нужно.")
        if result.get("reason") == "unlinked":
            return (f"Персонаж '{char.name}' не привязан к игроку — в бой добавить нельзя. "
                    "Разреши действие нарративно.")
        return ("Сейчас нет активного боя — добавлять некого. "
                "Если конфликт назрел, используй dechrauymladd.")

    async def _combat_joiner_async(self, session_id: str, character_name: str) -> str:
        """Async wrapper для передачи в process_master_turn как combat_joiner."""
        return self.join_active_combat_by_name(session_id, character_name)

    def record_combat_join(self, session_id: str, info: Dict[str, Any]) -> None:
        """Запомнить факт вступления в бой — движок анонсирует его игрокам
        после отправки нарратива (см. engine._resolve_non_combat_round).
        Ленивая инициализация — миксин может быть смонтирован на класс без
        BaseSessionMixin (см. тесты)."""
        if not hasattr(self, "_pending_combat_joins"):
            self._pending_combat_joins = {}
        if session_id not in self._pending_combat_joins:
            self._pending_combat_joins[session_id] = []
        self._pending_combat_joins[session_id].append({
            "name": info.get("name", "?"),
            "initiative": info.get("initiative"),
            "player_id": None,
        })

    def pop_combat_joins(self, session_id: str) -> List[Dict[str, Any]]:
        if not hasattr(self, "_pending_combat_joins"):
            return []
        return self._pending_combat_joins.pop(session_id, [])


    def start_combat_for_participants(self, session_id: str, participant_names: List[str], reason: str = "", pc_initiatives: Dict = None, priorities: Dict = None) -> str:
        """Called ONLY from the Master's start_combat tool call (see ai_client.py
        COMBAT_TOOLS) — combat begins from narrative (Дн.), never from an admin command.

        V10: PC initiatives come from pc_initiatives dict (rolled by players via
        request_player_roll with inline buttons). NPC initiatives are still rolled
        server-side with random.randint. If a PC has no pre-rolled initiative (timeout),
        falls back to server-side roll.

        V10b: Creates CombatEncounter + Combatant entries in the DB so that the
        per-turn combat loop (_start_combat_turn_loop in bot.py) can read them
        via get_current_initiative_turn / advance_initiative_turn. Does NOT call
        start_action_collection — the per-turn loop handles queue state.

        ИТЕРАЦИЯ 10 (Раздел 4): priorities — опциональный словарь
        {имя_участника: int} из tool-вызова dechrauymladd. Тай-брейк при РАВНЫХ
        инициативах: МЕНЬШЕ = ходит РАНЬШЕ. В нарративе игроки видят сырую
        инициативу (оба «15») — priority в показ не входит и не является
        игромеханическим бонусом."""
        db = self.db_manager.get_db(session_id)
        session = db.get_session(session_id)
        if not session:
            return "Error: no active session."
        if session.combat_active:
            return "Combat is already active — do not call start_combat again this fight."

        pc_initiatives = pc_initiatives or {}
        # Раздел 4: нормализация priorities — ключи в нижний регистр, значения int.
        try:
            priority_map = {str(k).strip().lower(): int(v)
                            for k, v in (priorities or {}).items()}
        except (TypeError, ValueError):
            priority_map = {}
        session_chars = db.get_session_characters(session_id)
        players = db.get_players(session_id)
        alive_chars = [c for c in session_chars if getattr(c, "is_alive", True)]

        # ── Create CombatEncounter in DB ──
        encounter_id = uuid.uuid4().hex[:12]
        encounter = CombatEncounter(
            id=encounter_id, session_id=session_id, reason=reason,
            created_at=datetime.utcnow().isoformat(),
        )
        db.create_combat_encounter(encounter)

        initiative_list = []
        results = []
        sort_order = 0

        for name in participant_names:
            combatant_id = uuid.uuid4().hex[:12]
            # БОЙ-FIX: устойчивый матчинг имени участника → персонаж сессии.
            # Раньше только chars_by_name.get(name.lower()): любое отклонение
            # имени в нарративе («Храфна Морвен», @username) превращало ЖИВОГО
            # игрока в NPC-комбатанта с player_id=0 — бот считал его «не в бою»
            # и его действия уходили в параллельный резолвер (гонка раундов).
            char = match_participant_to_character(name, alive_chars, players)

            if char:
                stats = json.loads(char.stats) if char.stats else {}
                dex_mod = (stats.get("dexterity", 10) - 10) // 2

                # V10: Use player-rolled initiative if available, else server roll.
                # BUG #3 FIX: previously this used fuzzy substring matching which
                # meant "СЛИЗЬ" could match "Воин с мечом СЛИЗЬ" — the wrong player
                # received the initiative roll. Switch to exact case-insensitive name
                # match, with a secondary match against player_id (the unique telegram
                # id) which is what request_player_roll actually keys on.
                pre_rolled = None
                for key, val in pc_initiatives.items():
                    if key.lower() == name.lower():
                        pre_rolled = val
                        break
                # If still not found, try the player_id-keyed entries (the Master
                # sometimes passes the player's telegram id instead of the name).
                if pre_rolled is None and str(char.player_id) in pc_initiatives:
                    pre_rolled = pc_initiatives[str(char.player_id)]

                if pre_rolled:
                    # ВНИМАНИЕ: не pre_rolled.get("natural", random.randint(...)) —
                    # default-выражение в dict.get вычисляется ВСЕГДА, лишний
                    # бросок ломал бы детерминизм тестов и жёг RNG впустую.
                    roll_nat = pre_rolled["natural"] if "natural" in pre_rolled else random.randint(1, 20)
                    roll_total = roll_nat + dex_mod
                    results.append(f"{char.name}: d20={roll_nat}{dex_mod:+d} = {roll_total} (player rolled)")
                else:
                    # Timeout or no player roll — server fallback
                    roll_nat = random.randint(1, 20)
                    roll_total = roll_nat + dex_mod
                    results.append(f"{char.name}: d20={roll_nat}{dex_mod:+d} = {roll_total} (server fallback)")

                # ── Create Combatant in DB ──
                pc_priority = priority_map.get(name.lower(), 0)
                combatant = Combatant(
                    id=combatant_id, encounter_id=encounter_id, session_id=session_id,
                    name=char.name, entity_type="pc", player_id=char.player_id,
                    initiative=roll_total, natural_roll=roll_nat, dex_mod=dex_mod,
                    hp=char.hp, max_hp=char.max_hp, ac=char.ac,
                    sort_order=sort_order, priority=pc_priority,
                )
                db.add_combatant(combatant)

                initiative_list.append({
                    "name": char.name, "player_id": char.player_id,
                    "initiative": roll_total, "natural": roll_nat, "dex_mod": dex_mod,
                    "priority": pc_priority,
                })
            else:
                # NPC/monster — server roll, hidden
                roll_nat = random.randint(1, 20)
                roll_total = roll_nat
                results.append(f"{name}: d20 = {roll_nat} (NPC, hidden)")

                # ── Create Combatant in DB ──
                npc_priority = priority_map.get(name.lower(), 0)
                combatant = Combatant(
                    id=combatant_id, encounter_id=encounter_id, session_id=session_id,
                    name=name, entity_type="npc", player_id=0,
                    initiative=roll_total, natural_roll=roll_nat, dex_mod=0,
                    sort_order=sort_order, priority=npc_priority,
                )
                db.add_combatant(combatant)

                initiative_list.append({
                    "name": name, "player_id": 0,
                    "initiative": roll_total, "natural": roll_nat, "dex_mod": 0,
                    "priority": npc_priority,
                })

            sort_order += 1

        if not initiative_list:
            return "Error: no valid participants supplied to start_combat."

        # ИТЕРАЦИЯ 10 (Раздел 4): python-сортировка должна совпадать с SQL-сортировкой
        # get_initiative_order: инициатива DESC → priority ASC (меньше = раньше).
        # Стабильность сортировки сохраняет порядок вставки при полном равенстве.
        initiative_list.sort(key=lambda x: (-x["initiative"], x.get("priority", 0)))

        # ── Update session state ──
        session.combat_active = True
        session.initiative_order = json.dumps(initiative_list)
        session.current_turn_index = 0
        session.round_number = 1
        db.update_session(session)

        db.add_history(HistoryEntry(
            session_id=session_id, author="DM",
            content=f"Бой начался ({reason}). Порядок: " + ", ".join(f"{e['name']} {e['initiative']}" for e in initiative_list),
            entry_type="combat",
        ))

        # V10b: Do NOT call start_action_collection here.
        # The per-turn orchestrator (_start_combat_turn_loop) manages queue state.

        order_str = "; ".join(results)
        return f"Combat started. Initiative rolled: {order_str}. First to act: {initiative_list[0]['name']}."


    def end_combat(self, session_id: str) -> str:
        """End combat"""
        db = self.db_manager.get_db(session_id)
        session = db.get_session(session_id)
        if not session:
            return "Нет активной сессии."

        # БОЙ-FIX v3: если бой стартовал по V10b — закрываем и encounter,
        # иначе в БД останется «зомби-encounter» (is_active=1), из которого
        # get_combat_action_group / is_non_combat_player будут читать мёртвое
        # состояние после перезапуска бота.
        try:
            encounter = db.get_active_combat_encounter(session_id)
            if encounter:
                db.end_combat_encounter(encounter.id)
        except Exception as e:
            # FakeDB в тестах может не иметь combat-поверхности — не валимся.
            logger.warning(f"[end-combat] could not close encounter: {e}")

        session.combat_active = False
        session.initiative_order = "[]"
        session.current_turn_index = 0
        session.round_number = 0
        db.update_session(session)
        db.clear_queue_state(session_id)

        return "🏳️ **Бой завершён.**"

    # ═══════════════════════════════════════════════════════════
    # БОЙ-FIX v3 (Причина #1 из ТЗ): defensive-check рассинхрона
    # ═══════════════════════════════════════════════════════════

    def _emergency_end_desynced_combat(self, session_id: str, where: str) -> None:
        """session.combat_active=True при отсутствии активного CombatEncounter —
        рассинхрон, с которым per-turn движок работать не может: заглушка
        «никто не заблокирован» означала, что бой зависает навсегда.

        Вместо тихой заглушки — ERROR в лог + аварийное завершение боя
        (end_combat: сброс флагов сессии, закрытие хвостов, очистка очереди).
        После этого игра продолжается в обычном режиме, а _process_dn_action
        сообщает игрокам, что произошло (через pop_combat_desync_flag)."""
        logger.error(
            f"[combat-desync] {where}: session.combat_active=True, но активный "
            f"CombatEncounter в {session_id} не найден — состояние повреждено "
            f"(легаси /combat или частичный сбой). Аварийно завершаю бой.")
        _COMBAT_DESYNC_HANDLED.add(session_id)
        try:
            self.end_combat(session_id)
        except Exception as e:
            logger.error(f"[combat-desync] emergency end_combat failed: {e}")

    def pop_combat_desync_flag(self, session_id: str) -> bool:
        """Вернуть (и снять) флаг «в этой сессии был аварийно завершён бой
        из-за рассинхрона». Одно уведомление на инцидент."""
        if session_id in _COMBAT_DESYNC_HANDLED:
            _COMBAT_DESYNC_HANDLED.discard(session_id)
            return True
        return False

    # ═══════════════════════════════════════════════════════════
    # INITIATIVE-BASED COMBAT (dechrauymladd)
    # ═══════════════════════════════════════════════════════════

    def start_initiative_combat(self, session_id: str, participants: list, reason: str, priorities: Dict = None) -> str:
        """Start initiative-based combat. Called by Master's dechrauymladd tool.
        ИТЕРАЦИЯ 10 (Раздел 4): priorities — опциональный тай-брейк {имя: int},
        меньше = раньше (см. start_combat_for_participants)."""
        db = self.db_manager.get_db(session_id)
        session = db.get_session(session_id)
        if not session:
            return "Error: session not found"
        try:
            priority_map = {str(k).strip().lower(): int(v)
                            for k, v in (priorities or {}).items()}
        except (TypeError, ValueError):
            priority_map = {}
        
        encounter_id = uuid.uuid4().hex[:12]
        encounter = CombatEncounter(
            id=encounter_id, session_id=session_id, reason=reason,
            created_at=datetime.utcnow().isoformat()
        )
        db.create_combat_encounter(encounter)
        
        players = db.get_players(session_id)
        player_map = {p.display_name: p for p in players}
        characters = [c for c in db.get_session_characters(session_id) if getattr(c, "is_alive", True)]
        char_map = {c.name: c for c in characters}
        
        initiative_results = []
        sort_order = 0
        
        for name in participants:
            combatant_id = uuid.uuid4().hex[:12]
            # БОЙ-FIX: тот же устойчивый матчинг, что и в
            # start_combat_for_participants — иначе живой игрок с именем
            # из нарратива регистрировался как NPC с player_id=0.
            char = match_participant_to_character(name, characters, players)
            if char:
                char_player_id = getattr(char, "player_id", 0) or 0
                player = next((p for p in players if p.user_id == char_player_id), None)
                if player is None and not char_player_id:
                    # Легаси-фолбэк: персонаж без привязки к игроку — пробуем
                    # сопоставить по display_name игрока (старое поведение).
                    player = player_map.get(name)
                    char_player_id = player.user_id if player else 0
                stats = json.loads(char.stats) if char and char.stats else {}
                dex = stats.get("dexterity", 10)
                dex_mod = (dex - 10) // 2
                roll = random.randint(1, 20)
                initiative = roll + dex_mod
                hp = char.hp if char else 0
                max_hp = char.max_hp if char else 0
                ac = char.ac if char else 10
                combatant = Combatant(
                    id=combatant_id, encounter_id=encounter_id, session_id=session_id,
                    name=name, entity_type="pc",
                    player_id=char_player_id,
                    initiative=initiative, natural_roll=roll, dex_mod=dex_mod,
                    hp=hp, max_hp=max_hp, ac=ac, sort_order=sort_order,
                    priority=priority_map.get(name.lower(), 0),
                )
            else:
                roll = random.randint(1, 20)
                initiative = roll
                combatant = Combatant(
                    id=combatant_id, encounter_id=encounter_id, session_id=session_id,
                    name=name, entity_type="npc", player_id=0,
                    initiative=initiative, natural_roll=roll, dex_mod=0,
                    sort_order=sort_order,
                    priority=priority_map.get(name.lower(), 0),
                )
            
            db.add_combatant(combatant)
            initiative_results.append(f"{'👤' if combatant.entity_type == 'pc' else '👹'} {name}: {roll}{'+' + str(dex_mod) if combatant.entity_type == 'pc' else ''} = **{initiative}**")
            sort_order += 1
        
        session.combat_active = True
        # Use the sorted initiative order (not insertion order) for session.initiative_order
        sorted_order = db.get_initiative_order(encounter_id)
        session.initiative_order = json.dumps([{"name": e["name"], "player_id": e["player_id"]} for e in sorted_order])
        session.current_turn_index = 0
        session.round_number = 1
        db.update_session(session)
        
        db.add_history(HistoryEntry(
            session_id=session_id, entry_type="gm_secret",
            content=f"COMBAT STARTED: {reason}\n" + "\n".join(initiative_results),
            author="SYSTEM"
        ))
        
        order = db.get_initiative_order(encounter_id)
        order_text = "\n".join(f"{i+1}. {e['name']} ({e['initiative']})" for i, e in enumerate(order))
        return f"COMBAT! Reason: {reason}\n\nInitiative order:\n{order_text}"


    def end_initiative_combat(self, session_id: str, reason: str) -> str:
        db = self.db_manager.get_db(session_id)
        encounter = db.get_active_combat_encounter(session_id)
        if not encounter:
            return "No active combat"
        db.end_combat_encounter(encounter.id)
        session = db.get_session(session_id)
        if session:
            session.combat_active = False
            session.initiative_order = "[]"
            session.current_turn_index = 0
            session.round_number = 0
            db.update_session(session)
        db.add_history(HistoryEntry(
            session_id=session_id, entry_type="narrative",
            content=f"Combat ended: {reason}", author="SYSTEM"
        ))
        # ── DUAL NARRATIVE: merge non-combat queue back into normal flow ──
        # Process any remaining non-combat actions in the next normal round.
        # БОЙ-FIX: раньше здесь стоял clear_non_combat_queue — оставшиеся
        # действия non-combat игроков ТЕРЯЛИСЬ, хотя комментарий обещал
        # «разрешим в следующем обычном раунде». Теперь очередь НЕ чистим:
        # resolve_round_master_only подмешивает остатки к первому же обычному
        # раунду (см. ResolutionMixin._resolve_round_master_only_locked).
        remaining = self.get_non_combat_queue(session_id)
        if remaining:
            logger.info(f"[dual-narrative] {len(remaining)} non-combat actions pending at combat end — will be merged into the next normal round")
        self.start_action_collection(session_id)
        return f"Combat ended: {reason}. Normal mode restored."


    def get_current_initiative_turn(self, session_id: str) -> Optional[Dict]:
        db = self.db_manager.get_db(session_id)
        encounter = db.get_active_combat_encounter(session_id)
        if not encounter:
            # БОЙ-FIX v3: combat_active без encounter — повреждённое состояние,
            # аварийно завершаем бой вместо тихого None.
            session = db.get_session(session_id)
            if session is not None and getattr(session, "combat_active", False):
                self._emergency_end_desynced_combat(
                    session_id, "get_current_initiative_turn")
            return None
        order = db.get_initiative_order(encounter.id)
        session = db.get_session(session_id)
        if not session or not order:
            return None
        idx = session.current_turn_index % len(order)
        return order[idx]


    def get_next_initiative_turns(self, session_id: str, count: int = 3) -> List[Dict]:
        db = self.db_manager.get_db(session_id)
        encounter = db.get_active_combat_encounter(session_id)
        if not encounter:
            return []
        order = db.get_initiative_order(encounter.id)
        session = db.get_session(session_id)
        if not session or not order:
            return []
        result = []
        for i in range(1, count + 1):
            idx = (session.current_turn_index + i) % len(order)
            result.append(order[idx])
        return result


    def advance_initiative_turn(self, session_id: str) -> Dict:
        db = self.db_manager.get_db(session_id)
        session = db.get_session(session_id)
        encounter = db.get_active_combat_encounter(session_id)
        if not session or not encounter:
            # БОЙ-FIX v3: тот же defensive-check, что и в геттерах выше.
            if session is not None and getattr(session, "combat_active", False):
                self._emergency_end_desynced_combat(
                    session_id, "advance_initiative_turn")
            return {}
        order = db.get_initiative_order(encounter.id)
        if not order:
            return {}
        new_idx = session.current_turn_index + 1
        if new_idx >= len(order):
            new_idx = 0
            session.round_number += 1
            db.increment_combat_round(encounter.id)
        session.current_turn_index = new_idx
        db.update_session(session)
        return order[new_idx]


    def get_combat_action_group(self, session_id: str) -> Dict:
        db = self.db_manager.get_db(session_id)
        encounter = db.get_active_combat_encounter(session_id)
        if not encounter:
            # БОЙ-FIX v3 (Причина #1): combat_active=True без encounter — это
            # состояние легаси /combat или частичного сбоя. Раньше здесь тихо
            # отдавалась заглушка {"mode": "normal", blocked_players: []} —
            # «никто не заблокирован»: игроки ходили через устаревшую очередь,
            # раунд не собирался, бой зависал навсегда. Теперь — ERROR в лог
            # и аварийное завершение боя.
            session = db.get_session(session_id)
            if session is not None and getattr(session, "combat_active", False):
                self._emergency_end_desynced_combat(
                    session_id, "get_combat_action_group")
            return {"mode": "normal", "waiting_for": [], "npc_turn": False, "blocked_players": []}
        order = db.get_initiative_order(encounter.id)
        session = db.get_session(session_id)
        if not session or not order:
            return {"mode": "normal", "waiting_for": [], "npc_turn": False, "blocked_players": []}
        idx = session.current_turn_index % len(order)
        current = order[idx]
        next_entries = []
        look_idx = (idx + 1) % len(order)
        while look_idx != idx:
            next_entries.append(order[look_idx])
            look_idx = (look_idx + 1) % len(order)
            if len(next_entries) >= len(order):
                break
        result = {
            "current": current,
            "next_up": next_entries[:3] if next_entries else [],
            "waiting_for": [],
            "npc_turn": current["entity_type"] != "pc",
            "blocked_players": [],
            "all_order": order,
        }
        if current["entity_type"] != "pc":
            npc_streak = [current]
            for entry in next_entries:
                if entry["entity_type"] != "pc":
                    npc_streak.append(entry)
                else:
                    break
            result["npc_streak"] = npc_streak
        else:
            # BUG #11 FIX: STRICT initiative order — only the CURRENT PC is in
            # waiting_for. The previous "pc_streak" logic added every consecutive
            # PC to waiting_for (so Eira + СЛИЗЬ would both be allowed to act),
            # which broke the >2 player scenario in the bug report: after Eira
            # acted, the queue was left in an inconsistent state and the bot
            # ended up re-prompting Eira instead of advancing to СЛИЗЬ. Now we
            # have ONE player per turn — exactly what the task spec requires.
            pc_player_id = current.get("player_id", 0)
            result["waiting_for"] = [pc_player_id] if pc_player_id > 0 else []
            result["simultaneous_players"] = [current]  # back-compat: a 1-element list
        all_pcs = [e for e in order if e["entity_type"] == "pc" and e.get("player_id", 0) > 0]
        waiting_ids = set(result.get("waiting_for", []))
        # BUG #11: every other PC (including the next ones in initiative order)
        # is "blocked" until the current PC finishes their turn. After the
        # current PC acts, _resolve_pc_combat_turn advances initiative to the
        # next combatant (PC OR NPC) and re-enters _start_combat_turn_loop, which
        # then calls _setup_pc_combat_turn for the new current PC.
        result["blocked_players"] = [p["player_id"] for p in all_pcs if p["player_id"] not in waiting_ids]
        return result

    def sync_combat_queue(self, session_id: str) -> Optional[int]:
        """БОЙ-FIX v2: во время боя очередь действий (QueueState) ОБЯЗАНА
        совпадать с боевой группой — одним текущим PC.

        Реальный кейс из лога: обычная all-players очередь (start_action_collection
        собирает ВСЕХ игроков сессии) была создана до/в момент старта боя и
        пережила его начало. Ход СЛИЗЬ ушёл в эту устаревшую очередь, после
        чего бот вечно показывал «Ждём: Храфна Морвен, Рими» — а Рими,
        запертый в non-combat lane, физически не мог подкрутить эту очередь:
        дедлок раунда. Теперь перед каждой подачей Дн. в бою очередь
        пересобирается под текущего PC, если она разошлась с боевой группой.

        Очередь НЕ трогается, если она консистентна:
          • waiting_for == [current_pc] — свежая single-player коллекция;
          • waiting_for == [] и current_pc уже в collected — игрок сходил,
            пересборка отменила бы его ход (двойной ход!).

        Возвращает player_id текущего боевого PC или None (вне боя/NPC-ход).
        """
        db = self.db_manager.get_db(session_id)
        session = db.get_session(session_id)
        if not session or not session.combat_active or not COMBAT_INITIATIVE_ENABLED:
            return None
        group = self.get_combat_action_group(session_id)
        if group.get("mode") == "normal" or group.get("npc_turn"):
            return None
        waiting_ids = group.get("waiting_for") or []
        if not waiting_ids:
            return None
        current_id = waiting_ids[0]

        consistent = False
        state = db.get_queue_state(session_id)
        if state:
            try:
                q_waiting = json.loads(state.waiting_for or "[]")
            except Exception:
                q_waiting = []
            try:
                q_collected = json.loads(state.collected_actions or "{}")
            except Exception:
                q_collected = {}
            extra = [uid for uid in q_waiting if uid != current_id]
            if not extra:
                if current_id in q_waiting:
                    consistent = True          # свежая single-player коллекция
                elif str(current_id) in q_collected:
                    consistent = True          # текущий PC уже сходил — не трогаем
        if not consistent:
            logger.warning(
                f"[combat-sync] Stale queue during combat in {session_id}: "
                f"rebuilding single-player collection for player {current_id}")
            self.start_single_player_collection(session_id, current_id)
        return current_id

    # ── V10b: Per-turn combat helpers ──


    def get_combat_context(self, session_id: str) -> str:
        """Build full combat state text for per-turn master context."""
        db = self.db_manager.get_db(session_id)
        session = db.get_session(session_id)
        encounter = db.get_active_combat_encounter(session_id)
        if not encounter or not session:
            return ""

        order = db.get_initiative_order(encounter.id)
        if not order:
            return ""

        chars = db.get_session_characters(session_id)
        char_map = {c.name.lower(): c for c in chars}

        # ДОПОЛНЕНИЕ 1: HP/состояния НПС из Combatant — секретные данные
        # для Мастера (в чат не попадают, _send_combat_turn_result режет сводку).
        combatants = db.get_combatants(encounter.id)
        npc_map = {}
        for c in combatants:
            if c.entity_type != "pc":
                npc_map[c.name.lower()] = c

        lines = [f"--- БОЙ: Раунд {session.round_number} ---"]
        lines.append("Порядок инициативы:")
        for i, e in enumerate(order):
            marker = " >>> " if i == session.current_turn_index else "     "
            status = ""
            if e["entity_type"] == "pc":
                ch = char_map.get(e["name"].lower())
                if ch:
                    status = f" | HP {ch.hp}/{ch.max_hp} | KB {ch.ac}"
            else:
                comb = npc_map.get(e["name"].lower())
                if comb is not None:
                    try:
                        conds = json.loads(comb.current_conditions) if comb.current_conditions else []
                    except Exception:
                        conds = []
                    if not isinstance(conds, list):
                        conds = []
                    cond_txt = f" | состояния: {', '.join(str(c) for c in conds)}" if conds else ""
                    hp_txt = f"{comb.hp}/{comb.max_hp}" if comb.max_hp else f"{comb.hp}/?"
                    gm_note = ""
                    try:
                        traits = json.loads(comb.traits) if comb.traits else {}
                    except Exception:
                        traits = {}
                    if isinstance(traits, dict):
                        note = (traits.get("gm_note") or "").strip()
                        if note:
                            gm_note = f" [ЗАМЕТКА: {note}]"
                        if traits.get("revealed"):
                            gm_note += " [HP УЖЕ РАСКРЫТО ИГРОКАМ]"
                    status = f" | HP (СКРЫТО): {hp_txt}{cond_txt}{gm_note}"
            lines.append(f"  {marker}{i+1}. {e['name']} (ini {e['initiative']}){status}")

        cur = order[session.current_turn_index % len(order)]
        lines.append(f"\nТЕКУЩИЙ ХОД: {cur['name']} ({cur['entity_type'].upper()})")
        lines.append(
            "\n🔒 СЕКРЕТНО (игроки этого не видят): «HP (СКРЫТО)», состояния и заметки НПС — "
            "твои скрытые данные. НЕ упоминай их в нарративе, ЕСЛИ игроки не узнали это "
            "законно: успешная Внимательность/Расследование/Медицина/Анализ в их действии, "
            "явное сюжетное событие — тогда назови точное HP/состояние. После изменения HP/состояния НПС "
            "вызывай diweddaru_npc, чтобы данные сохранялись.")
        # Скрытая записная книжка НПС (вне боя) — тоже для Мастера.
        hidden_states = self.get_hidden_npc_states(session_id)
        if hidden_states:
            lines.append("Скрытые состояния НПС вне боя (СЕКРЕТНО):")
            for nm, st in list(hidden_states.items())[:10]:
                if not isinstance(st, dict):
                    continue
                hp = st.get("hp", "?")
                hp_max = st.get("hp_max", "?")
                conds = st.get("conditions") or []
                note = (st.get("note") or "").strip()
                alive = "" if st.get("is_alive", True) else " [МЁРТВ/без сознания]"
                cond_txt = f", состояния: {', '.join(str(c) for c in conds)}" if conds else ""
                note_txt = f", заметка: {note}" if note else ""
                lines.append(f"  • {nm}: HP {hp}/{hp_max}{cond_txt}{note_txt}{alive}")
        return "\n".join(lines)


    def start_combat_turn_collection(self, session_id: str) -> Dict:
        group = self.get_combat_action_group(session_id)
        if group.get("npc_turn"):
            return group
        waiting_for = group.get("waiting_for", [])
        if waiting_for:
            db = self.db_manager.get_db(session_id)
            state = QueueState(
                session_id=session_id,
                waiting_for=json.dumps(waiting_for),
                collected_actions="{}",
                is_resolving=False,
            )
            db.set_queue_state(state)
        return group


    def get_current_turn(self, session_id: str) -> Optional[Dict]:
        """Get whose turn it is"""
        db = self.db_manager.get_db(session_id)
        session = db.get_session(session_id)
        if not session or not session.combat_active:
            return None

        initiative = json.loads(session.initiative_order)
        if not initiative:
            return None

        idx = session.current_turn_index % len(initiative)
        return initiative[idx]


    def advance_turn(self, session_id: str) -> Optional[Dict]:
        """Advance to next turn"""
        db = self.db_manager.get_db(session_id)
        session = db.get_session(session_id)
        if not session or not session.combat_active:
            return None

        initiative = json.loads(session.initiative_order)
        if not initiative:
            return None

        session.current_turn_index += 1

        if session.current_turn_index >= len(initiative):
            session.current_turn_index = 0
            session.round_number += 1

        db.update_session(session)
        return self.get_current_turn(session_id)

    # ═══════════════════════════════════════════════════════════
    # Queue System — The Core Mechanic
    # ═══════════════════════════════════════════════════════════


