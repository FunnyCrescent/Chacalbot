"""GeneratorsMixin — AI-coupled world/encounter/weather generators."""
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


class GeneratorsMixin:
    """GeneratorsMixin — AI-coupled world/encounter/weather generators."""

    def generate_loot_by_cr(self, cr: float, loot_type: str = "individual") -> str:
        """Generate loot based on D&D 5e tables by CR"""
        import random
        # Loot tables are global-ish; use first available DB
        active = self.db_manager.get_all_active_sessions()
        if active:
            db = self.db_manager.get_db(active[0].id)
        else:
            db = self.db_manager.get_db("_srd")
        table = db.get_loot_table(cr, loot_type)
        if table:
            import json
            entries = json.loads(table.entries)
            results = []
            for entry in entries:
                if random.random() * 100 <= entry.get("chance", 100):
                    qty = entry.get("qty", "1")
                    results.append(f"{qty} × {entry['name']}")
            return ", ".join(results) if results else "Ничего ценного"

        if cr <= 4:
            cp = random.randint(1, 6) * 10
            sp = random.randint(1, 4) * 10
            gp = random.randint(1, 6) if random.random() > 0.5 else 0
            return f"{cp} см, {sp} смм, {gp} зм"
        elif cr <= 10:
            gp = random.randint(2, 6) * 10
            pp = random.randint(1, 6) * 10
            return f"{gp} зм, {pp} пм"
        else:
            gp = random.randint(4, 6) * 100
            pp = random.randint(2, 6) * 100
            return f"{gp} зм, {pp} пм"

    # ═══════════════════════════════════════════════════════════
    # Encounters (#13)
    # ═══════════════════════════════════════════════════════════


    async def generate_encounter(self, session_id: str, terrain: str = "") -> dict:
        import asyncio
        db = self.db_manager.get_db(session_id)
        chars = db.get_session_characters(session_id)
        party_level = sum(c.level for c in chars) // max(len(chars), 1)
        party_size = len(chars)
        context = db.get_session(session_id).current_scene if db.get_session(session_id) else ""

        raw_text = await self.dm.generate_encounter(context, party_level, party_size, terrain)

        # Parallel DB-Bot + Renderer
        db_task = self.dm.process_with_db_bot(raw_text, context=f"Encounter terrain: {terrain}", session_id=session_id)
        render_task = self.dm.process_renderer(raw_text, session_id=session_id)
        game_actions, html_text = await asyncio.gather(db_task, render_task)

        if game_actions:
            applied, errors = self._apply_game_actions(session_id, game_actions)
            logger.info(f"Encounter: applied {applied} DB actions")

        return {"text": raw_text, "html": html_text}

    # ═══════════════════════════════════════════════════════════
    # Weather (#11)
    # ═══════════════════════════════════════════════════════════


    async def generate_weather(self, session_id: str) -> dict:
        import asyncio
        db = self.db_manager.get_db(session_id)
        gt = db.get_game_time(session_id)
        terrain = ""
        loc = db.get_all_locations(session_id)
        if loc:
            terrain = loc[0].location_name

        raw_text = await self.dm.generate_weather(gt.season, terrain, gt.weather)

        # Parallel DB-Bot + Renderer
        db_task = self.dm.process_with_db_bot(raw_text, context=f"Weather in {terrain}", session_id=session_id)
        render_task = self.dm.process_renderer(raw_text, session_id=session_id)
        game_actions, html_text = await asyncio.gather(db_task, render_task)

        if game_actions:
            applied, errors = self._apply_game_actions(session_id, game_actions)
            logger.info(f"Weather: applied {applied} DB actions")

        weather_types = ["ясно", "дождь", "ливень", "туман", "метель", "снег", "жара", "пыль", "град", "ураган", "ясная"]
        new_weather = gt.weather
        for w in weather_types:
            if w.lower() in raw_text.lower():
                new_weather = w
                break

        temps = ["жарко", "тепло", "прохладно", "холодно", "мороз"]
        new_temp = gt.temperature
        for t in temps:
            if t.lower() in raw_text.lower():
                new_temp = t
                break

        db.update_game_time(session_id, weather=new_weather, temperature=new_temp)
        return {"text": raw_text, "html": html_text}

    # ═══════════════════════════════════════════════════════════
    # World Pregeneration (#15)
    # ═══════════════════════════════════════════════════════════


    async def generate_world(self, session_id: str, theme: str = "dark fantasy") -> dict:
        """Generate world + apply DB. Returns dict with text and html (raw text from Master)."""
        db = self.db_manager.get_db(session_id)
        chars = db.get_session_characters(session_id)

        # F2: build short character briefs (race/class/backstory) so the generated world
        # is forced to acknowledge each PC's stated goals/enemies/factions instead of
        # inventing an unrelated setting.
        character_briefs = []
        for c in chars:
            brief = f"{c.name} — {c.race} {c.class_name}"
            if c.backstory:
                brief += f". Предыстория: {c.backstory[:400]}"
            character_briefs.append(brief)

        # Step 1: Master writes world narrative
        raw_text = await self.dm.generate_world_seed(theme, character_briefs=character_briefs or None)

        # Step 2: DB-Bot only
        game_actions = await self.dm.process_with_db_bot(raw_text, context=f"Session: {session_id}", session_id=session_id)

        # Step 3: Apply DB actions
        if game_actions:
            applied, errors = self._apply_game_actions(session_id, game_actions)
            if errors:
                logger.warning(f"World gen DB errors: {errors}")
            logger.info(f"World gen: applied {applied} DB actions")

        # VALIDATION: check if world was actually created in DB
        locations = db.get_locations(session_id)
        npcs = db.get_npcs(session_id)

        if not locations:
            logger.warning(f"World gen: no locations created! Creating fallback starter location.")
            db.create_location(Location(
                id=str(uuid.uuid4())[:8], session_id=session_id,
                name="Стартовая таверна", description="Грязная таверна на краю мира.",
                type="tavern", danger_level=1,
            ))
            locations = db.get_locations(session_id)

        # Auto-bind all characters to starter location
        if locations:
            starter = locations[0]
            for char in chars:
                db.set_location(session_id, char.id, starter.name, starter.description)
                logger.info(f"Auto-set {char.name} location to {starter.name}")

        session = db.get_session(session_id)
        if session:
            session.current_scene = raw_text[:500]
            db.update_session(session)

        # Auto-extract currency from world lore if session has no currency set
        if session and not session.currency_name and raw_text:
            try:
                import json as _json
                currency = await self.dm.extract_currency_from_world(raw_text)
                if currency and currency.get("name"):
                    session.currency_name = currency["name"]
                    session.currency_symbol = currency.get("symbol", currency["name"][:3].lower())
                    session.currency_plural = f"{session.currency_symbol} ({currency['name']})"
                    if currency.get("sub_name"):
                        session.currency_sub_name = currency["sub_name"]
                        session.currency_sub_symbol = currency.get("sub_symbol", currency["sub_name"][:3].lower())
                        session.currency_sub_plural = f"{session.currency_sub_symbol} ({currency['sub_name']})"
                        session.currency_sub_value = str(currency.get("sub_value", 0.1))
                    if currency.get("super_name"):
                        session.currency_super_name = currency["super_name"]
                        session.currency_super_symbol = currency.get("super_symbol", currency["super_name"][:3].lower())
                        session.currency_super_plural = f"{session.currency_super_symbol} ({currency['super_name']})"
                        session.currency_super_value = str(currency.get("super_value", 10.0))
                    db.update_session(session)
                    logger.info(f"[auto-currency] Set session currency to: {session.currency_name} ({session.currency_symbol})")
            except Exception as e:
                logger.warning(f"[auto-currency] extraction failed (non-fatal): {e}")

        # H-new: the DB-Bot's tool-calling loop during world-gen has a limited iteration
        # budget and picks whichever NPCs IT judges significant — named figures buried
        # deep in a long lore text could get silently skipped. This is a SEPARATE,
        # guaranteed structured pass over the same lore text that pulls out every named
        # NPC and creates any that are still missing, with a simple duplicate-name guard
        # so a slightly different phrasing of an NPC the DB-Bot already created doesn't
        # produce a second entry for the same person.
        try:
            extracted_npcs = await self.dm.extract_all_npcs_from_lore(raw_text)
            existing_names_lower = {n.name.lower() for n in npcs}
            created_count = 0
            skipped_spawn = 0
            for enpc in extracted_npcs:
                name = (enpc.get("name") or "").strip()
                if not name or name.lower() in existing_names_lower:
                    continue
                loc_id = ""
                loc_name = enpc.get("location_name", "")
                loc_type = ""
                if loc_name:
                    for l in locations:
                        if loc_name.lower() in l.name.lower() or l.name.lower() in loc_name.lower():
                            loc_id = l.id
                            loc_type = getattr(l, 'type', '') or getattr(l, 'location_type', '')
                            break

                # ── NPC SPAWN VALIDATION (occupation → location binding) ──
                occupation = enpc.get("occupation", "")
                if occupation and loc_type:
                    try:
                        from libs.ai.npc_spawn_engine import NpcSpawnEngine
                        spawn_engine = NpcSpawnEngine(db)
                        spawn_result = spawn_engine.validate_npc_spawn(
                            npc_occupation=occupation,
                            current_location_type=loc_type,
                            current_location_name=loc_name,
                        )
                        if not spawn_result.is_valid:
                            # Try relocating to a valid location
                            allowed_types = spawn_result.suggested_locations
                            relocated = False
                            if allowed_types:
                                for alt_l in locations:
                                    alt_type = getattr(alt_l, 'type', '') or getattr(alt_l, 'location_type', '')
                                    if alt_type.lower() in [t.lower() for t in allowed_types]:
                                        loc_id = alt_l.id
                                        logger.info(
                                            f"[lore-npc] '{name}' ({occupation}) relocated from "
                                            f"'{loc_type}' to '{alt_type}'"
                                        )
                                        relocated = True
                                        break
                            if not relocated:
                                logger.warning(
                                    f"[lore-npc] '{name}' ({occupation}) at '{loc_type}' invalid — "
                                    f"allowing without relocation"
                                )
                    except Exception as e:
                        logger.warning(f"[lore-npc] spawn validation failed (non-blocking): {e}")

                db.create_npc(WorldNpc(
                    id=str(uuid.uuid4())[:8], session_id=session_id,
                    name=name, race=enpc.get("race", ""), occupation=occupation,
                    location_id=loc_id, backstory=enpc.get("backstory", ""),
                    personality=json.dumps({"traits": enpc.get("personality", "")}),
                ))
                existing_names_lower.add(name.lower())
                created_count += 1
            if created_count:
                logger.info(f"[lore-npc-extraction] Created {created_count} additional NPCs from lore text")
                npcs = db.get_npcs(session_id)  # refresh for the blocks below
        except Exception as e:
            logger.warning(f"extract_all_npcs_from_lore failed (non-fatal): {e}")

        # F3 (repurposed): personal objectives pulled straight out of backstories
        # (revenge, finding family, serving a faction) are GOALS, not quests — a quest
        # needs a narratively confirmed NPC-given task, but these are exactly what a
        # goal is for: something the character already wants, no confirmation needed.
        # Previously these were fed into /quest, which is why players saw a "quest"
        # appear from pure backstory text with no NPC ever having assigned anything.
        if character_briefs:
            try:
                hooks = await self.dm.extract_quest_hooks(character_briefs)
                for hook in hooks:
                    title = hook.get("title", "")
                    brief_snippet = hook.get("character_brief", "")
                    if not title:
                        continue
                    matched_char = None
                    for c in chars:
                        if c.name and c.name in brief_snippet:
                            matched_char = c
                            break
                    if not matched_char:
                        continue  # a goal needs an owner, unlike an unassigned quest
                    self.add_character_goal(session_id, matched_char.id, matched_char.name, title, source="backstory")
                    logger.info(f"[F3] Auto-goal from backstory: '{title}' -> {matched_char.name}")
            except Exception as e:
                logger.warning(f"extract_quest_hooks failed (non-fatal): {e}")

        # H-new: without this, /npc and /city stay empty on session start even when the
        # generated lore text CLEARLY establishes a pre-existing relationship (e.g. an
        # NPC who raised the PC, or a city that already has wanted posters up) — those
        # facts only lived in narrative text, never in the relation tables the player-
        # facing commands actually read from. Pull explicit relations out and apply them.
        if character_briefs:
            try:
                npc_names = [n.name for n in npcs]
                location_names = [l.name for l in locations]
                relations = await self.dm.extract_backstory_relations(character_briefs, npc_names, location_names)
                applied_relations = 0
                for rel in relations:
                    rtype = rel.get("type")
                    char_name = rel.get("character_name", "")
                    matched_char = None
                    for c in chars:
                        if c.name and (c.name in char_name or char_name in c.name):
                            matched_char = c
                            break
                    if not matched_char:
                        continue

                    if rtype == "npc":
                        npc_name = rel.get("npc_name", "")
                        matched_npc = None
                        for n in npcs:
                            if npc_name.lower() in n.name.lower() or n.name.lower() in npc_name.lower():
                                matched_npc = n
                                break
                        if matched_npc:
                            db.set_npc_relation(NpcRelation(
                                session_id=session_id, npc_id=matched_npc.id, character_id=matched_char.id,
                                reputation=rel.get("reputation_delta", 0),
                                attitude=rel.get("attitude", "") or "neutral",
                                known_facts=rel.get("known_fact", ""),
                            ))
                            applied_relations += 1

                    elif rtype == "location":
                        loc_name = rel.get("location_name", "")
                        matched_loc = None
                        for l in locations:
                            if loc_name.lower() in l.name.lower() or l.name.lower() in loc_name.lower():
                                matched_loc = l
                                break
                        if matched_loc:
                            db.adjust_location_relation(
                                session_id, matched_loc.id, matched_loc.name, matched_char.id, matched_char.name,
                                fame_delta=rel.get("fame_delta", 0),
                                reputation_delta=rel.get("reputation_delta", 0),
                                is_wanted=rel.get("is_wanted"),
                                notoriety_note=rel.get("notoriety_note", ""),
                            )
                            applied_relations += 1

                if applied_relations:
                    logger.info(f"[backstory-relations] Primed {applied_relations} NPC/location relations from character backstories")
            except Exception as e:
                logger.warning(f"extract_backstory_relations application failed (non-fatal): {e}")

        return {"text": raw_text, "html": raw_text}


    async def generate_living_world_event(self, session_id: str) -> dict:
        import asyncio
        db = self.db_manager.get_db(session_id)
        session = db.get_session(session_id)
        context = session.summary if session and session.summary else session.current_scene if session else ""
        factions = db.get_factions(session_id)
        if factions:
            context += "\n\nФракции: " + ", ".join(f.name for f in factions)

        raw_text = await self.dm.generate_world_event(context)

        if raw_text:
            # Parallel DB-Bot + Renderer
            db_task = self.dm.process_with_db_bot(raw_text, context=f"Living world event. Factions: {[f.name for f in factions]}", session_id=session_id)
            render_task = self.dm.process_renderer(raw_text, session_id=session_id)
            game_actions, html_text = await asyncio.gather(db_task, render_task)

            if game_actions:
                applied, errors = self._apply_game_actions(session_id, game_actions)
                logger.info(f"Living event: applied {applied} DB actions")

            import re
            title_match = re.search(r'\*\*Событие\*\*:\s*(.+)', raw_text)
            title = title_match.group(1).strip() if title_match else "Событие в мире"
            db.add_world_event(session_id, "living_world", f"{title}\n{raw_text}")

        return {"text": raw_text, "html": html_text}

    async def auto_start(self, session_id: str) -> str:
        """Auto-start game when all players have uploaded character sheets."""
        import asyncio
        # BUG 3 FIX: поднимаем флаг «мир генерируется» (ленивый импорт — _state
        # импортирует session_manager, поэтому прямой импорт наверху зациклит).
        try:
            from libs.handlers._state import mark_world_generating, clear_world_generating
            mark_world_generating(session_id, 0)
        except Exception:
            mark_world_generating, clear_world_generating = (lambda *a, **k: None), (lambda *a, **k: None)

        db = self.db_manager.get_db(session_id)
        players = db.get_players(session_id)
        chars = db.get_session_characters(session_id)

        try:
            if len(chars) < len(players):
                missing = [p.display_name for p in players if not db.get_character_by_player(p.user_id, session_id)]
                return f"⏳ Ждём листы: {', '.join(missing)}"

            # All sheets loaded! Generate world and start
            logger.info(f"[AUTO_START] Session {session_id}: all {len(chars)} sheets loaded. Starting game...")

            # Generate world
            world_result = await self.generate_world(session_id, "dark fantasy")
            world_text = world_result.get("text", "")
            world_html = world_result.get("html", world_text)

            # Start action collection for first round
            self.start_action_collection(session_id)

            # Build welcome message
            char_names = [c.name for c in chars]
            welcome = (
                f"🌍 **Мир рождён!**\n\n"
                f"{world_html}\n\n"
                f"---\n\n"
                f"⚔️ **Игра начинается!**\n"
                f"Персонажи: {', '.join(char_names)}\n\n"
                f"📝 Пишите `Дн. ваше действие` — мастер разрешит, когда все сходят."
            )

            # Add to history
            db.add_history(HistoryEntry(
                session_id=session_id,
                author="SYSTEM",
                content="[AUTO_START] Game started automatically after all sheets loaded.",
                entry_type="system",
            ))

            return welcome
        finally:
            clear_world_generating(session_id)


