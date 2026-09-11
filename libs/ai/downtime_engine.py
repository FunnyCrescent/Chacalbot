"""DowntimeEngine — processes downtime activities and applies world changes.

Implements D&D 5e downtime activities (PHB p.187, XGtE p.61-75) with
world-state side effects: economy changes, faction reputation shifts,
NPC movements, and world events.

When players spend downtime, the world does NOT freeze. This engine:
1. Calculates mechanical results (training progress, crafting output, etc.)
2. Applies world changes (economy, reputation, events)
3. Generates narrative prompts for the Master about what changed
"""
import json
import logging
import random
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# DOWNTIME ACTIVITY DEFINITIONS
# ═══════════════════════════════════════════════════════════════════

DOWNTIME_ACTIVITIES = {
    "training": {
        "name_ru": "Тренировка",
        "description": "Изучение нового навыка, владения инструментом или языка.",
        "requirements": ["Учитель с нужным навыком", "250 gp на обучение", "Доступ к учебным материалам"],
        "base_days": 250,
        "base_cost_gp": 250,
        "max_days": 250,
        "world_impact": "Учитель получает доход; местная академия/гильдия может заинтересоваться",
        "rules": "250 дней + 250 gp за proficiency. Учитель обязателен. Прогресс = дни / 250.",
    },
    "crafting": {
        "name_ru": "Крафт",
        "description": "Создание предмета, владея нужными инструментами.",
        "requirements": ["Владение инструментами", "Доступ к инструментам", "Сырьё (половина стоимости предмета)"],
        "base_days": 0,  # Depends on item cost
        "base_cost_gp": 0,  # Half of item cost in raw materials
        "max_days": 999,
        "world_impact": "Предмет появится на местном рынке если продан; цены могут снизиться",
        "rules": "Прогресс = (бонус_владения × 5) gp/день в сторону стоимости предмета.",
    },
    "working": {
        "name_ru": "Работа",
        "description": "Заработок на жизнь по образу жизни.",
        "requirements": ["Выбор образа жизни"],
        "base_days": 1,
        "base_cost_gp": 0,
        "max_days": 999,
        "world_impact": "Добавляет в местную экономику; повышает известность в городе",
        "rules": "Образ жизни → заработок: Squalid 1sp, Poor 3sp, Modest 5sp, Comfortable 1gp, Wealthy 2gp, Aristocratic 10gp/день.",
    },
    "recuperating": {
        "name_ru": "Восстановление",
        "description": "Отдых для избавления от болезни или яда.",
        "requirements": ["3 дня отдыха", "DC 15 CON save для второй болезни/яда"],
        "base_days": 3,
        "base_cost_gp": 0,
        "max_days": 10,
        "world_impact": "Минимальный — персонаж неактивен в мире",
        "rules": "3 дня → убрать 1 болезнь/яд. DC 15 CON save за вторую. Два заболевания за 3 дня при успехе save.",
    },
    "carousing": {
        "name_ru": "Карусинг",
        "description": "Светская жизнь, застолья, развлечения с местной элитой.",
        "requirements": ["Выбор образа жизни (Comfortable минимум)", "Затраты по образу жизни"],
        "base_days": 5,
        "base_cost_gp": 0,  # Depends on lifestyle
        "max_days": 30,
        "world_impact": "Репутация с фракциями; новые связи; может привлечь внимание (плохое и хорошее)",
        "rules": "Репутация +1 за 5 дней. Осложнения при провале DC. Образ жизни определяет круг общения.",
    },
    "research": {
        "name_ru": "Исследование",
        "description": "Поиск информации в библиотеке или архиве.",
        "requirements": ["Доступ к библиотеке/архиву", "INT check DC 15"],
        "base_days": 7,
        "base_cost_gp": 0,
        "max_days": 60,
        "world_impact": "Может раскрыть лор, вызвать мировые события, привлечь внимание хранителей знаний",
        "rules": "1 неделя на улику. Библиотечный доступ обязателен. INT check DC 15 — успех раскрывает информацию.",
    },
    "religious_service": {
        "name_ru": "Религиозная служба",
        "description": "Служение в храме — ритуалы, помощь прихожанам, медитация.",
        "requirements": ["Храм нужного божества", "10 дней службы"],
        "base_days": 10,
        "base_cost_gp": 0,
        "max_days": 30,
        "world_impact": "Улучшение репутации с фракцией храма; возможное благословение или квест от жрецов",
        "rules": "10 дней в храме. Репутация с храмом +2. Жрецы могут дать квест или благословение.",
    },
}

# ─── Lifestyle Earnings Table ───
LIFESTYLE_EARNINGS = {
    "squalid":     {"min_gp_per_day": 0.0,  "actual_gp_per_day": 0.1,  "description": "Нищета — 1 серебряная монета/день"},
    "poor":        {"min_gp_per_day": 0.0,  "actual_gp_per_day": 0.3,  "description": "Бедность — 3 серебряных монеты/день"},
    "modest":      {"min_gp_per_day": 0.0,  "actual_gp_per_day": 0.5,  "description": "Скромность — 5 серебряных монет/день"},
    "comfortable": {"min_gp_per_day": 1.0,  "actual_gp_per_day": 1.0,  "description": "Комфорт — 1 золотая/день"},
    "wealthy":     {"min_gp_per_day": 2.0,  "actual_gp_per_day": 2.0,  "description": "Богатство — 2 золотых/день"},
    "aristocratic":{"min_gp_per_day": 10.0, "actual_gp_per_day": 10.0, "description": "Аристократизм — 10 золотых/день минимум"},
}

# ─── Carousing Complications Table (by lifestyle) ───
CAROUSING_COMPLICATIONS = {
    "modest": [
        ("Местный торговец обвиняет тебя в воровстве", 5),
        ("Пьяная драка в таверне — стража вмешалась", 5),
        ("Ты проснулся без кошелька", 4),
        ("Местный шут сделал тебя мишенью шуток", 3),
        ("Романтическая связь с местным жителем", 2),
    ],
    "comfortable": [
        ("Дворянин оскорблён твоим поведением", 5),
        ("Ты случайно оскорбил гильдию", 4),
        ("Кто-то пытается скомпрометировать тебя", 4),
        ("Ты заключил пари и проиграл", 3),
        ("Местная знать приглашает на праздник", 2),
    ],
    "wealthy": [
        ("Ты приобрёл врага среди знати", 5),
        ("Шпион следил за тобой на празднике", 4),
        ("Ты оскорбил влиятельного дворянина", 5),
        ("Твоя щедрость привлекла воров", 3),
        ("Ты завёл полезные связи при дворе", 2),
    ],
    "aristocratic": [
        ("Ты впутался в политический скандал", 6),
        ("Убийца подослан к тебе", 5),
        ("Ты обидел главу гильдии", 5),
        ("Коррупционный чиновник требует взятку", 4),
        ("Ты получил приглашение к правителю", 3),
    ],
}


# ═══════════════════════════════════════════════════════════════════
# RESULT DATACLASSES
# ═══════════════════════════════════════════════════════════════════

@dataclass
class DowntimeResult:
    """Result of processing a downtime activity."""
    success: bool
    activity_type: str
    days_spent: int
    world_changes: List[dict] = field(default_factory=list)
    character_changes: dict = field(default_factory=dict)
    narrative: str = ""
    gold_earned: float = 0.0
    gold_spent: float = 0.0
    progress: float = 0.0  # 0.0-1.0 for training/crafting


@dataclass
class DowntimeOption:
    """An available downtime activity option."""
    activity_type: str
    description: str
    requirements: List[str] = field(default_factory=list)
    max_days: int = 0
    world_impact: str = ""
    base_cost_gp: float = 0.0
    base_days: int = 0


@dataclass
class CarousingResult:
    """Result of a carousing downtime activity."""
    lifestyle: str
    expenses: float
    complications: List[str] = field(default_factory=list)
    relationships_gained: List[str] = field(default_factory=list)
    reputation_change: int = 0
    narrative: str = ""


@dataclass
class WorldChange:
    """A change to world state from a downtime activity."""
    change_type: str  # "economy", "reputation", "npc_move", "event", "price"
    description: str
    magnitude: float = 0.0  # How significant the change is
    affected_entity: str = ""  # What entity is affected (faction_id, location_id, npc_id)
    data: dict = field(default_factory=dict)  # Additional data


class DowntimeEngine:
    """Processes downtime activities and applies world-state changes.

    Usage:
        engine = DowntimeEngine(db)
        result = engine.process_downtime_activity(
            session_id, character_id, "training", 30,
            {"skill": "Stealth", "teacher": "Goblin Thief"}
        )
    """

    def __init__(self, db=None):
        """Initialize with optional database reference."""
        self.db = db

    # ───────────────────────────────────────────────────────────────
    # Main Processing
    # ───────────────────────────────────────────────────────────────

    def process_downtime_activity(
        self,
        session_id: str,
        character_id: str,
        activity_type: str,
        duration_days: int,
        details: dict = None,
    ) -> DowntimeResult:
        """Process a downtime activity for a character.

        Args:
            session_id: Game session
            character_id: The character performing the activity
            activity_type: One of the DOWNTIME_ACTIVITIES keys
            duration_days: How many days to spend
            details: Additional details (skill, item, lifestyle, etc.)

        Returns:
            DowntimeResult with all changes
        """
        details = details or {}
        activity = DOWNTIME_ACTIVITIES.get(activity_type)

        if not activity:
            return DowntimeResult(
                success=False,
                activity_type=activity_type,
                days_spent=0,
                narrative=f"Неизвестный тип даунтайма: {activity_type}",
            )

        # Dispatch to specific handler
        handler_map = {
            "training": self._process_training,
            "crafting": self._process_crafting,
            "working": self._process_working,
            "recuperating": self._process_recuperating,
            "carousing": self._process_carousing,
            "research": self._process_research,
            "religious_service": self._process_religious_service,
        }

        handler = handler_map.get(activity_type)
        if not handler:
            return DowntimeResult(
                success=False,
                activity_type=activity_type,
                days_spent=0,
                narrative=f"Обработчик для {activity_type} не реализован",
            )

        result = handler(session_id, character_id, duration_days, details)

        # Apply world changes if successful
        if result.success and result.world_changes:
            events = self.apply_world_changes(session_id, result.world_changes)
            result.character_changes["world_events_created"] = len(events)

        # Advance game time
        if result.success and self.db is not None:
            self.db.advance_time(session_id, hours=duration_days * 24)

        return result

    # ───────────────────────────────────────────────────────────────
    # Specific Activity Handlers
    # ───────────────────────────────────────────────────────────────

    def _process_training(
        self,
        session_id: str,
        character_id: str,
        days: int,
        details: dict,
    ) -> DowntimeResult:
        """Process training downtime activity.

        Rules: 250 days + 250 gp per proficiency. Teacher required.
        Progress = days / 250.
        """
        skill = details.get("skill", "")
        tool = details.get("tool", "")
        language = details.get("language", "")
        teacher = details.get("teacher", "")
        target = skill or tool or language

        if not target:
            return DowntimeResult(
                success=False, activity_type="training", days_spent=0,
                narrative="Укажите навык, инструмент или язык для изучения.",
            )

        if not teacher:
            return DowntimeResult(
                success=False, activity_type="training", days_spent=0,
                narrative=f"Для изучения '{target}' нужен учитель. Укажите teacher в details.",
            )

        # Calculate progress
        total_days_needed = 250
        total_cost_gp = 250
        progress = min(days / total_days_needed, 1.0)
        cost_incurred = (days / total_days_needed) * total_cost_gp

        # Clamp days
        actual_days = min(days, total_days_needed)

        world_changes = []
        # Teacher gets income
        world_changes.append({
            "change_type": "economy",
            "description": f"Учитель {teacher} получил {cost_incurred:.0f} gp за обучение",
            "magnitude": cost_incurred / 100.0,
            "affected_entity": teacher,
        })
        # Local guild/academy may take interest
        if progress >= 0.5:
            world_changes.append({
                "change_type": "event",
                "description": f"Местная гильдия/академия заметила обучение {target}",
                "magnitude": 0.3,
            })

        completed = progress >= 1.0
        narrative_parts = [
            f"Тренировка '{target}' под руководством {teacher}:",
            f"  • Потрачено {actual_days} дней из {total_days_needed}",
            f"  • Прогресс: {progress * 100:.0f}%",
            f"  • Стоимость: {cost_incurred:.0f} gp из {total_cost_gp} gp",
        ]
        if completed:
            narrative_parts.append(f"  ✅ Владение '{target}' получено!")
        else:
            narrative_parts.append(f"  ⏳ Осталось {total_days_needed - actual_days} дней")

        return DowntimeResult(
            success=True,
            activity_type="training",
            days_spent=actual_days,
            world_changes=world_changes,
            character_changes={
                "skill": target,
                "progress": progress,
                "completed": completed,
                "gp_spent": cost_incurred,
            },
            narrative="\n".join(narrative_parts),
            gold_spent=cost_incurred,
            progress=progress,
        )

    def _process_crafting(
        self,
        session_id: str,
        character_id: str,
        days: int,
        details: dict,
    ) -> DowntimeResult:
        """Process crafting downtime activity.

        Rules: progress = (proficiency_bonus * 5) gp/day toward item cost.
        Need tool proficiency + access to tools.
        """
        item_name = details.get("item", "")
        item_cost_gp = details.get("item_cost_gp", 0)
        proficiency_bonus = details.get("proficiency_bonus", 2)

        if not item_name or not item_cost_gp:
            return DowntimeResult(
                success=False, activity_type="crafting", days_spent=0,
                narrative="Укажите предмет (item) и его стоимость (item_cost_gp).",
            )

        # Calculate crafting progress
        gp_per_day = proficiency_bonus * 5
        total_progress_gp = gp_per_day * days
        material_cost = item_cost_gp / 2  # Raw materials = half item cost
        progress = min(total_progress_gp / item_cost_gp, 1.0)
        completed = progress >= 1.0

        world_changes = []
        # If sold locally, affects market
        sell_locally = details.get("sell_locally", False)
        if sell_locally and completed:
            world_changes.append({
                "change_type": "price",
                "description": f"Предмет '{item_name}' появился на местном рынке",
                "magnitude": -0.05,  # Slight price decrease for this item type
                "affected_entity": item_name,
                "data": {"price_change_pct": -5},
            })

        narrative_parts = [
            f"Крафт '{item_name}' (стоимость {item_cost_gp} gp):",
            f"  • Скорость: {gp_per_day} gp/день (бонус владения +{proficiency_bonus})",
            f"  • Прогресс: {total_progress_gp:.0f} gp / {item_cost_gp} gp ({progress * 100:.0f}%)",
            f"  • Материалы: {material_cost:.0f} gp (половина стоимости)",
        ]
        if completed:
            narrative_parts.append(f"  ✅ Предмет '{item_name}' создан!")
        else:
            remaining_gp = item_cost_gp - total_progress_gp
            remaining_days = remaining_gp / gp_per_day
            narrative_parts.append(f"  ⏳ Осталось ~{remaining_days:.0f} дней ({remaining_gp:.0f} gp)")

        return DowntimeResult(
            success=True,
            activity_type="crafting",
            days_spent=days,
            world_changes=world_changes,
            character_changes={
                "item": item_name,
                "progress": progress,
                "completed": completed,
                "gp_spent_materials": material_cost,
            },
            narrative="\n".join(narrative_parts),
            gold_spent=material_cost,
            progress=progress,
        )

    def _process_working(
        self,
        session_id: str,
        character_id: str,
        days: int,
        details: dict,
    ) -> DowntimeResult:
        """Process working downtime activity.

        Rules: lifestyle-based earnings. Squalid 1sp/day → Aristocratic 10gp/day.
        """
        lifestyle = details.get("lifestyle", "modest").lower()
        lifestyle_data = LIFESTYLE_EARNINGS.get(lifestyle)

        if not lifestyle_data:
            return DowntimeResult(
                success=False, activity_type="working", days_spent=0,
                narrative=f"Неизвестный образ жизни: {lifestyle}. "
                         f"Доступные: {', '.join(LIFESTYLE_EARNINGS.keys())}",
            )

        # Calculate earnings
        gp_per_day = lifestyle_data["actual_gp_per_day"]
        total_earned = gp_per_day * days
        lifestyle_cost = lifestyle_data["min_gp_per_day"] * days

        world_changes = []
        # Working adds to local economy
        world_changes.append({
            "change_type": "economy",
            "description": f"Персонаж работает ({lifestyle}) {days} дней — добавляет в местную экономику",
            "magnitude": total_earned / 10.0,
        })
        # Local fame increase for extended work
        if days >= 10:
            world_changes.append({
                "change_type": "reputation",
                "description": f"Известность в городе выросла за {days} дней работы",
                "magnitude": days / 30.0,
            })

        narrative = (
            f"Работа ({lifestyle_data['description']}) {days} дней:\n"
            f"  • Заработано: {total_earned:.1f} gp\n"
            f"  • Образ жизни: {lifestyle}\n"
            f"  • Минимальные расходы: {lifestyle_cost:.1f} gp/день"
        )

        return DowntimeResult(
            success=True,
            activity_type="working",
            days_spent=days,
            world_changes=world_changes,
            character_changes={
                "lifestyle": lifestyle,
                "gp_earned": total_earned,
                "gp_lifestyle_cost": lifestyle_cost,
            },
            narrative=narrative,
            gold_earned=total_earned,
            gold_spent=lifestyle_cost,
        )

    def _process_recuperating(
        self,
        session_id: str,
        character_id: str,
        days: int,
        details: dict,
    ) -> DowntimeResult:
        """Process recuperating downtime activity.

        Rules: 3 days → remove 1 disease/poison. DC 15 CON save for second.
        """
        if days < 3:
            return DowntimeResult(
                success=False, activity_type="recuperating", days_spent=0,
                narrative="Восстановление требует минимум 3 дня отдыха.",
            )

        con_modifier = details.get("con_modifier", 0)
        diseases = details.get("diseases", [])
        poisons = details.get("poisons", [])
        conditions_to_cure = diseases + poisons

        cured = []
        # First condition: automatic after 3 days
        if conditions_to_cure:
            cured.append(conditions_to_cure[0])
            # Second condition: DC 15 CON save
            if len(conditions_to_cure) > 1:
                # Simulate the save (in real play, the player would roll)
                con_roll = random.randint(1, 20) + con_modifier
                if con_roll >= 15:
                    cured.append(conditions_to_cure[1])

        world_changes = []
        # Minimal world impact — character is resting
        if days >= 5:
            world_changes.append({
                "change_type": "event",
                "description": "Персонаж отдыхал несколько дней — мир продолжал вращаться",
                "magnitude": 0.1,
            })

        narrative_parts = [
            f"Восстановление: {days} дней отдыха.",
        ]
        if cured:
            narrative_parts.append(f"  ✅ Излечено: {', '.join(cured)}")
        if len(conditions_to_cure) > len(cured):
            remaining = [c for c in conditions_to_cure if c not in cured]
            narrative_parts.append(f"  ⏳ Осталось: {', '.join(remaining)} (нужен DC 15 CON save)")
        if not conditions_to_cure:
            narrative_parts.append("  ℹ️ Нет болезней или ядов для излечения — просто отдых.")

        return DowntimeResult(
            success=True,
            activity_type="recuperating",
            days_spent=days,
            world_changes=world_changes,
            character_changes={
                "cured_conditions": cured,
                "conditions_remaining": [c for c in conditions_to_cure if c not in cured],
            },
            narrative="\n".join(narrative_parts),
        )

    def _process_carousing(
        self,
        session_id: str,
        character_id: str,
        days: int,
        details: dict,
    ) -> DowntimeResult:
        """Process carousing downtime activity.

        Rules: lifestyle-based, complications on failure, reputation +1 per 5 days.
        """
        lifestyle = details.get("lifestyle", "comfortable").lower()

        if lifestyle not in ("comfortable", "wealthy", "aristocratic", "modest"):
            return DowntimeResult(
                success=False, activity_type="carousing", days_spent=0,
                narrative="Карусинг требует образ жизни Comfortable или выше.",
            )

        carousing_result = self.resolve_carousing(session_id, character_id, lifestyle, days)

        world_changes = []
        # Reputation changes
        if carousing_result.reputation_change != 0:
            world_changes.append({
                "change_type": "reputation",
                "description": f"Карусинг: репутация изменена на {carousing_result.reputation_change:+d}",
                "magnitude": abs(carousing_result.reputation_change),
            })
        # Relationships
        for rel in carousing_result.relationships_gained:
            world_changes.append({
                "change_type": "event",
                "description": f"Новая связь: {rel}",
                "magnitude": 0.3,
            })
        # Complications
        for comp in carousing_result.complications:
            world_changes.append({
                "change_type": "event",
                "description": f"Осложнение: {comp}",
                "magnitude": 0.5,
            })

        narrative_parts = [
            f"Карусинг ({lifestyle}) {days} дней:",
            f"  • Расходы: {carousing_result.expenses:.1f} gp",
        ]
        if carousing_result.reputation_change:
            narrative_parts.append(f"  • Репутация: {carousing_result.reputation_change:+d}")
        if carousing_result.relationships_gained:
            narrative_parts.append(f"  • Новые связи: {', '.join(carousing_result.relationships_gained)}")
        if carousing_result.complications:
            narrative_parts.append(f"  ⚠️ Осложнения: {'; '.join(carousing_result.complications)}")

        return DowntimeResult(
            success=True,
            activity_type="carousing",
            days_spent=days,
            world_changes=world_changes,
            character_changes={
                "lifestyle": lifestyle,
                "reputation_change": carousing_result.reputation_change,
                "relationships": carousing_result.relationships_gained,
                "complications": carousing_result.complications,
            },
            narrative="\n".join(narrative_parts) + "\n" + carousing_result.narrative,
            gold_spent=carousing_result.expenses,
        )

    def _process_research(
        self,
        session_id: str,
        character_id: str,
        days: int,
        details: dict,
    ) -> DowntimeResult:
        """Process research downtime activity.

        Rules: 1 week per lead, library access required, INT check DC 15.
        """
        topic = details.get("topic", "")
        int_modifier = details.get("int_modifier", 0)
        has_library = details.get("has_library", True)

        if not has_library:
            return DowntimeResult(
                success=False, activity_type="research", days_spent=0,
                narrative="Для исследования нужен доступ к библиотеке или архиву.",
            )

        if not topic:
            return DowntimeResult(
                success=False, activity_type="research", days_spent=0,
                narrative="Укажите тему исследования (topic).",
            )

        # Each lead takes 1 week (7 days)
        leads_investigated = days // 7
        if leads_investigated < 1:
            return DowntimeResult(
                success=False, activity_type="research", days_spent=0,
                narrative="Исследование требует минимум 7 дней (1 неделю) на улику.",
            )

        # INT checks for each lead
        successful_leads = 0
        for _ in range(leads_investigated):
            roll = random.randint(1, 20) + int_modifier
            if roll >= 15:
                successful_leads += 1

        world_changes = []
        lore_discovered = []
        if successful_leads > 0:
            world_changes.append({
                "change_type": "event",
                "description": f"Исследование '{topic}': раскрыто {successful_leads} из {leads_investigated} улик",
                "magnitude": successful_leads * 0.3,
            })
            # Research may trigger world events
            if successful_leads >= 3:
                world_changes.append({
                    "change_type": "event",
                    "description": f"Глубокое исследование '{topic}' привлекло внимание хранителей знаний",
                    "magnitude": 0.5,
                })

        narrative_parts = [
            f"Исследование '{topic}' {days} дней:",
            f"  • Улик исследовано: {leads_investigated} (по 7 дней на улику)",
            f"  • Успешных проверок INT (DC 15): {successful_leads} из {leads_investigated}",
        ]
        if successful_leads > 0:
            narrative_parts.append(f"  ✅ Раскрыта информация по {successful_leads} уликам")
        else:
            narrative_parts.append("  ❌ Исследование не дало результатов")

        return DowntimeResult(
            success=True,
            activity_type="research",
            days_spent=days,
            world_changes=world_changes,
            character_changes={
                "topic": topic,
                "leads_investigated": leads_investigated,
                "leads_successful": successful_leads,
            },
            narrative="\n".join(narrative_parts),
        )

    def _process_religious_service(
        self,
        session_id: str,
        character_id: str,
        days: int,
        details: dict,
    ) -> DowntimeResult:
        """Process religious service downtime activity.

        Rules: 10 days at temple, faction reputation +2 with temple.
        """
        deity = details.get("deity", "")
        temple_faction_id = details.get("temple_faction_id", 0)

        if not deity and not temple_faction_id:
            return DowntimeResult(
                success=False, activity_type="religious_service", days_spent=0,
                narrative="Укажите божество (deity) или фракцию храма (temple_faction_id).",
            )

        if days < 10:
            return DowntimeResult(
                success=False, activity_type="religious_service", days_spent=0,
                narrative="Религиозная служба требует минимум 10 дней.",
            )

        # Reputation gain with temple faction
        rep_gain = 2 + (days // 10 - 1)  # +2 for first 10 days, +1 per additional 10

        world_changes = []
        if temple_faction_id:
            world_changes.append({
                "change_type": "reputation",
                "description": f"Религиозная служба ({days} дней): репутация с храмом +{rep_gain}",
                "magnitude": rep_gain,
                "affected_entity": str(temple_faction_id),
                "data": {"faction_id": temple_faction_id, "delta": rep_gain},
            })

        # Possible blessing or quest
        blessing_chance = min(days / 30, 0.5)  # Up to 50% for 30+ days
        received_blessing = random.random() < blessing_chance
        if received_blessing:
            world_changes.append({
                "change_type": "event",
                "description": f"Жрецы {deity} предложили благословение или квест",
                "magnitude": 0.4,
            })

        deity_name = deity or f"храм (фракция {temple_faction_id})"
        narrative_parts = [
            f"Религиозная служба ({deity_name}) {days} дней:",
            f"  • Репутация с храмом: +{rep_gain}",
        ]
        if received_blessing:
            narrative_parts.append("  ✨ Жрецы предложили благословение или квест!")

        return DowntimeResult(
            success=True,
            activity_type="religious_service",
            days_spent=days,
            world_changes=world_changes,
            character_changes={
                "deity": deity,
                "temple_faction_id": temple_faction_id,
                "reputation_gain": rep_gain,
                "blessing_offered": received_blessing,
            },
            narrative="\n".join(narrative_parts),
        )

    # ───────────────────────────────────────────────────────────────
    # Carousing Resolution
    # ───────────────────────────────────────────────────────────────

    def resolve_carousing(
        self,
        session_id: str,
        character_id: str,
        lifestyle: str,
        days: int,
    ) -> CarousingResult:
        """Resolve carousing with complications and relationships.

        Args:
            session_id: Game session
            character_id: The character carousing
            lifestyle: Lifestyle level (modest/comfortable/wealthy/aristocratic)
            days: Number of days carousing

        Returns:
            CarousingResult with all outcomes
        """
        lifestyle_data = LIFESTYLE_EARNINGS.get(lifestyle, LIFESTYLE_EARNINGS["comfortable"])

        # Expenses based on lifestyle
        expenses = lifestyle_data["min_gp_per_day"] * days
        if expenses == 0:
            expenses = lifestyle_data["actual_gp_per_day"] * days

        # Reputation: +1 per 5 days
        reputation_change = days // 5

        # Complications: roll for each 5-day period
        complications = []
        complication_table = CAROUSING_COMPLICATIONS.get(lifestyle, CAROUSING_COMPLICATIONS["comfortable"])
        periods = days // 5
        for _ in range(periods):
            roll = random.randint(1, 20)
            if roll <= 5:  # 25% chance of complication per 5-day period
                comp_entry = random.choice(complication_table)
                complications.append(comp_entry[0])

        # Relationships gained (based on lifestyle and days)
        relationships_gained = []
        relationship_chance = {
            "modest": 0.1,
            "comfortable": 0.2,
            "wealthy": 0.3,
            "aristocratic": 0.4,
        }.get(lifestyle, 0.1)

        for _ in range(periods):
            if random.random() < relationship_chance:
                rel_type = random.choice([
                    "знакомый среди торговцев",
                    "контакт в гильдии",
                    "друг среди стражи",
                    "покровитель среди знати",
                    "информатор в трущобах",
                    "союзник среди жрецов",
                ])
                relationships_gained.append(rel_type)

        narrative = f"Карусинг ({lifestyle}, {days} дней): расходы {expenses:.1f} gp, репутация {reputation_change:+d}"
        if complications:
            narrative += f", {len(complications)} осложнений"
        if relationships_gained:
            narrative += f", {len(relationships_gained)} новых связей"

        return CarousingResult(
            lifestyle=lifestyle,
            expenses=expenses,
            complications=complications,
            relationships_gained=relationships_gained,
            reputation_change=reputation_change,
            narrative=narrative,
        )

    # ───────────────────────────────────────────────────────────────
    # World Change Application
    # ───────────────────────────────────────────────────────────────

    def apply_world_changes(
        self,
        session_id: str,
        changes: List[dict],
    ) -> list:
        """Apply downtime world changes to the database.

        Creates WorldEvent entries for significant changes, updates
        faction reputation, and adjusts market prices.

        Args:
            session_id: Game session
            changes: List of world change dicts from DowntimeResult

        Returns:
            List of created WorldEvent IDs
        """
        if self.db is None:
            logger.warning("No DB reference — cannot apply world changes")
            return []

        event_ids = []

        for change in changes:
            change_type = change.get("change_type", "")
            description = change.get("description", "")
            magnitude = change.get("magnitude", 0.0)
            affected_entity = change.get("affected_entity", "")
            data = change.get("data", {})

            # Always create a world event for significant changes
            if magnitude >= 0.3:
                event_id = self.db.add_world_event(
                    session_id,
                    event_type=f"downtime_{change_type}",
                    description=description,
                )
                event_ids.append(event_id)

            # Apply specific change types
            if change_type == "reputation" and affected_entity:
                # Update faction reputation
                try:
                    faction_id = int(affected_entity)
                    delta = data.get("delta", int(magnitude))
                    self.db.update_faction_reputation(session_id, faction_id, delta)
                    logger.info(f"Faction {faction_id} reputation {delta:+d} from downtime")
                except (ValueError, TypeError):
                    logger.warning(f"Cannot parse faction_id from '{affected_entity}'")

            elif change_type == "price" and affected_entity:
                # Create economic event for price changes
                try:
                    price_change_pct = data.get("price_change_pct", -5)
                    from libs.db.models import EconomicEvent
                    event = EconomicEvent(
                        session_id=session_id,
                        name=f"Ценовое изменение: {affected_entity}",
                        description=description,
                        price_multiplier=1.0 + (price_change_pct / 100.0),
                        duration_days=30,
                        is_active=True,
                    )
                    self.db.add_economic_event(event)
                    logger.info(f"Price change for {affected_entity}: {price_change_pct}%")
                except Exception as e:
                    logger.error(f"Failed to create economic event: {e}")

            elif change_type == "economy":
                # Economic contribution — creates a lighter event
                if magnitude >= 0.5:
                    event_id = self.db.add_world_event(
                        session_id,
                        event_type="economy_shift",
                        description=description,
                    )
                    event_ids.append(event_id)

        return event_ids

    # ───────────────────────────────────────────────────────────────
    # Downtime Options
    # ───────────────────────────────────────────────────────────────

    def get_downtime_options(
        self,
        session_id: str,
        character_id: str,
    ) -> List[DowntimeOption]:
        """Get all available downtime options for a character.

        Args:
            session_id: Game session
            character_id: The character

        Returns:
            List of DowntimeOption objects
        """
        options = []
        for activity_type, activity in DOWNTIME_ACTIVITIES.items():
            options.append(DowntimeOption(
                activity_type=activity_type,
                description=activity["description"],
                requirements=activity["requirements"],
                max_days=activity["max_days"],
                world_impact=activity["world_impact"],
                base_cost_gp=activity["base_cost_gp"],
                base_days=activity["base_days"],
            ))
        return options

    # ───────────────────────────────────────────────────────────────
    # Calculation Helpers
    # ───────────────────────────────────────────────────────────────

    def calculate_training_progress(
        self,
        character: Any,
        skill: str,
        days: int,
    ) -> int:
        """Calculate how many days of training remain for a skill.

        Returns:
            Days remaining (0 = complete)
        """
        total_needed = 250
        remaining = max(total_needed - days, 0)
        return remaining

    def calculate_crafting_progress(
        self,
        character: Any,
        item: dict,
        days: int,
    ) -> float:
        """Calculate crafting progress as fraction of item cost.

        Args:
            character: Character object (for proficiency bonus)
            item: Dict with 'cost_gp' and 'name'
            days: Days spent crafting

        Returns:
            Progress as 0.0-1.0
        """
        proficiency_bonus = 2  # Default
        if hasattr(character, 'level'):
            level = character.level or 1
            if level <= 4:
                proficiency_bonus = 2
            elif level <= 8:
                proficiency_bonus = 3
            elif level <= 12:
                proficiency_bonus = 4
            elif level <= 16:
                proficiency_bonus = 5
            else:
                proficiency_bonus = 6

        item_cost = item.get("cost_gp", 100)
        gp_per_day = proficiency_bonus * 5
        total_progress = gp_per_day * days
        return min(total_progress / item_cost, 1.0)

    def calculate_working_earnings(
        self,
        lifestyle: str,
        days: int,
    ) -> float:
        """Calculate gold earned from working at a given lifestyle.

        Returns:
            Total gp earned
        """
        lifestyle_data = LIFESTYLE_EARNINGS.get(lifestyle.lower())
        if not lifestyle_data:
            return 0.0
        return lifestyle_data["actual_gp_per_day"] * days

    # ───────────────────────────────────────────────────────────────
    # World State Delta (for Master narrative)
    # ───────────────────────────────────────────────────────────────

    def generate_world_delta_narrative(
        self,
        session_id: str,
        days_passed: int,
    ) -> str:
        """Generate a narrative description of what changed in the world
        during downtime. Used by the Master to describe the world state
        when players return from downtime.

        Args:
            session_id: Game session
            days_passed: How many days of downtime

        Returns:
            Narrative string describing world changes
        """
        if self.db is None:
            return ""

        changes = []

        # Check for world events
        events = self.db.get_world_events(session_id, unresolved_only=True)
        if events:
            recent = events[:5]  # Last 5 events
            for e in recent:
                changes.append(f"• {e.description}")

        # Check game time for season/weather changes
        gt = self.db.get_game_time(session_id)
        changes.append(f"• Сейчас: день {gt.day}, {gt.hour:02d}:{gt.minute:02d}, {gt.weather}, {gt.season}")

        # Check factions
        factions = self.db.get_factions(session_id)
        for f in factions[:5]:
            if f.reputation != 0:
                attitude_emoji = {"friendly": "😊", "helpful": "🙂", "neutral": "😐", "unfriendly": "😒", "hostile": "😠"}.get(f.attitude, "❓")
                changes.append(f"• {f.name}: репутация {f.reputation} {attitude_emoji}")

        if not changes:
            return f"За {days_passed} дней мир не изменился заметно."

        return f"За {days_passed} дней мир изменился:\n" + "\n".join(changes)
