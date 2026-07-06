"""
Character Parser — imports D&D 5e characters from .txt and .md files
Supports: markdown tables, key-value pairs, freeform text, Russian & English
"""

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class ParsedCharacter:
    """Parsed character data"""
    name: str = ""
    race: str = ""
    class_name: str = ""
    level: int = 1
    background: str = ""
    alignment: str = ""

    strength: int = 10
    dexterity: int = 10
    constitution: int = 10
    intelligence: int = 10
    wisdom: int = 10
    charisma: int = 10

    hp: int = 0
    max_hp: int = 0
    ac: int = 10
    speed: int = 30
    hit_dice: int = 8
    gold: int = 0

    proficiencies: List[str] = field(default_factory=list)
    skills: List[str] = field(default_factory=list)
    languages: List[str] = field(default_factory=list)
    inventory: List[str] = field(default_factory=list)
    spells: List[str] = field(default_factory=list)
    features: List[str] = field(default_factory=list)

    backstory: str = ""
    personality: str = ""
    ideals: str = ""
    bonds: str = ""
    flaws: str = ""

    def get_stats_json(self) -> str:
        return json.dumps({
            "strength": self.strength,
            "dexterity": self.dexterity,
            "constitution": self.constitution,
            "intelligence": self.intelligence,
            "wisdom": self.wisdom,
            "charisma": self.charisma,
        })

    def get_modifier(self, stat: str) -> int:
        val = getattr(self, stat.lower(), 10)
        return (val - 10) // 2


class CharacterParser:
    """Robust parser for D&D character files"""

    # ═══════════════════════════════════════════════════════════
    # STAT PATTERNS — supports many formats
    # ═══════════════════════════════════════════════════════════

    STAT_PATTERNS = {
        "strength": [
            # Box-drawing format with any dash type
            r"\bSTR\b\s+(\d+)\s*\([\-–—−]\d+\)",
            r"\bSTR\b\s+(\d+)",
            r"СИЛ\s+(\d+)\s*\([+-]?\d+\)",
            r"СИЛА\s+(\d+)\s*\([+-]?\d+\)",
            # Markdown tables
            r"\|\s*\*?Strength\*?\s*\|\s*(\d+)",
            r"\|\s*\*?STR\*?\s*\|\s*(\d+)",
            r"\|\s*\*?Сила\*?\s*\|\s*(\d+)",
            r"\|\s*STR\s*\|\s*(\d+)\s*\|\s*[+-]?\d+",
            # Key-value with modifier
            r"[Ss]trength\s*[:=]\s*(\d+)\s*\(?[+-]?\d*\)?",
            r"STR\s*[:=]\s*(\d+)",
            r"Сила\s*[:=]\s*(\d+)",
            r"СИЛ\s*[:=]\s*(\d+)",
            # Inline format: STR 16 (+3)
            r"\bSTR\b\s+(\d+)\s*\(?[+-]?\d*\)?",
            r"\bStrength\b\s+(\d+)",
            r"\bСила\b\s+(\d+)",
            # Bold markdown
            r"\*\*Strength\*\*\s*[:=]?\s*(\d+)",
            r"\*\*STR\*\*\s*[:=]?\s*(\d+)",
            r"\*\*Сила\*\*\s*[:=]?\s*(\d+)",
            # D&D Beyond style
            r"Strength\s*\n?\s*(\d+)\s*\n?\s*\(?[+-]?\d+\)?",
        ],
        "dexterity": [
            # Box-drawing format with any dash type
            r"\bDEX\b\s+(\d+)\s*\([\-–—−]\d+\)",
            r"\bDEX\b\s+(\d+)",
            r"ЛОВ\s+(\d+)\s*\([+-]?\d+\)",
            r"ЛОВКОСТЬ\s+(\d+)\s*\([+-]?\d+\)",
            r"\|\s*\*?Dexterity\*?\s*\|\s*(\d+)",
            r"\|\s*\*?DEX\*?\s*\|\s*(\d+)",
            r"\|\s*\*?Ловкость\*?\s*\|\s*(\d+)",
            r"\|\s*DEX\s*\|\s*(\d+)\s*\|\s*[+-]?\d+",
            r"[Dd]exterity\s*[:=]\s*(\d+)\s*\(?[+-]?\d*\)?",
            r"DEX\s*[:=]\s*(\d+)",
            r"Ловкость\s*[:=]\s*(\d+)",
            r"ЛОВ\s*[:=]\s*(\d+)",
            r"\bDEX\b\s+(\d+)\s*\(?[+-]?\d*\)?",
            r"\bDexterity\b\s+(\d+)",
            r"\bЛовкость\b\s+(\d+)",
            r"\*\*Dexterity\*\*\s*[:=]?\s*(\d+)",
            r"\*\*DEX\*\*\s*[:=]?\s*(\d+)",
            r"\*\*Ловкость\*\*\s*[:=]?\s*(\d+)",
            r"Dexterity\s*\n?\s*(\d+)\s*\n?\s*\(?[+-]?\d+\)?",
        ],
        "constitution": [
            # Box-drawing format with any dash type
            r"\bCON\b\s+(\d+)\s*\([\-–—−]\d+\)",
            r"\bCON\b\s+(\d+)",
            r"ТЕЛ\s+(\d+)\s*\([+-]?\d+\)",
            r"ТЕЛОСЛОЖЕНИЕ\s+(\d+)\s*\([+-]?\d+\)",
            r"\|\s*\*?Constitution\*?\s*\|\s*(\d+)",
            r"\|\s*\*?CON\*?\s*\|\s*(\d+)",
            r"\|\s*\*?Телосложение\*?\s*\|\s*(\d+)",
            r"\|\s*CON\s*\|\s*(\d+)\s*\|\s*[+-]?\d+",
            r"[Cc]onstitution\s*[:=]\s*(\d+)\s*\(?[+-]?\d*\)?",
            r"CON\s*[:=]\s*(\d+)",
            r"Телосложение\s*[:=]\s*(\d+)",
            r"ТЕЛ\s*[:=]\s*(\d+)",
            r"\bCON\b\s+(\d+)\s*\(?[+-]?\d*\)?",
            r"\bConstitution\b\s+(\d+)",
            r"\bТелосложение\b\s+(\d+)",
            r"\*\*Constitution\*\*\s*[:=]?\s*(\d+)",
            r"\*\*CON\*\*\s*[:=]?\s*(\d+)",
            r"\*\*Телосложение\*\*\s*[:=]?\s*(\d+)",
            r"Constitution\s*\n?\s*(\d+)\s*\n?\s*\(?[+-]?\d+\)?",
        ],
        "intelligence": [
            # Box-drawing format with any dash type
            r"\bINT\b\s+(\d+)\s*\([\-–—−]\d+\)",
            r"\bINT\b\s+(\d+)",
            r"ИНТ\s+(\d+)\s*\([+-]?\d+\)",
            r"ИНТЕЛЛЕКТ\s+(\d+)\s*\([+-]?\d+\)",
            r"\|\s*\*?Intelligence\*?\s*\|\s*(\d+)",
            r"\|\s*\*?INT\*?\s*\|\s*(\d+)",
            r"\|\s*\*?Интеллект\*?\s*\|\s*(\d+)",
            r"\|\s*INT\s*\|\s*(\d+)\s*\|\s*[+-]?\d+",
            r"[Ii]ntelligence\s*[:=]\s*(\d+)\s*\(?[+-]?\d*\)?",
            r"INT\s*[:=]\s*(\d+)",
            r"Интеллект\s*[:=]\s*(\d+)",
            r"ИНТ\s*[:=]\s*(\d+)",
            r"\bINT\b\s+(\d+)\s*\(?[+-]?\d*\)?",
            r"\bIntelligence\b\s+(\d+)",
            r"\bИнтеллект\b\s+(\d+)",
            r"\*\*Intelligence\*\*\s*[:=]?\s*(\d+)",
            r"\*\*INT\*\*\s*[:=]?\s*(\d+)",
            r"\*\*Интеллект\*\*\s*[:=]?\s*(\d+)",
            r"Intelligence\s*\n?\s*(\d+)\s*\n?\s*\(?[+-]?\d+\)?",
        ],
        "wisdom": [
            # Box-drawing format with any dash type
            r"\bWIS\b\s+(\d+)\s*\([\-–—−]\d+\)",
            r"\bWIS\b\s+(\d+)",
            r"МУД\s+(\d+)\s*\([+-]?\d+\)",
            r"МУДРОСТЬ\s+(\d+)\s*\([+-]?\d+\)",
            r"\|\s*\*?Wisdom\*?\s*\|\s*(\d+)",
            r"\|\s*\*?WIS\*?\s*\|\s*(\d+)",
            r"\|\s*\*?Мудрость\*?\s*\|\s*(\d+)",
            r"\|\s*WIS\s*\|\s*(\d+)\s*\|\s*[+-]?\d+",
            r"[Ww]isdom\s*[:=]\s*(\d+)\s*\(?[+-]?\d*\)?",
            r"WIS\s*[:=]\s*(\d+)",
            r"Мудрость\s*[:=]\s*(\d+)",
            r"МУД\s*[:=]\s*(\d+)",
            r"\bWIS\b\s+(\d+)\s*\(?[+-]?\d*\)?",
            r"\bWisdom\b\s+(\d+)",
            r"\bМудрость\b\s+(\d+)",
            r"\*\*Wisdom\*\*\s*[:=]?\s*(\d+)",
            r"\*\*WIS\*\*\s*[:=]?\s*(\d+)",
            r"\*\*Мудрость\*\*\s*[:=]?\s*(\d+)",
            r"Wisdom\s*\n?\s*(\d+)\s*\n?\s*\(?[+-]?\d+\)?",
        ],
        "charisma": [
            # Box-drawing format with any dash type
            r"\bCHA\b\s+(\d+)\s*\([\-–—−]\d+\)",
            r"\bCHA\b\s+(\d+)",
            r"ХАР\s+(\d+)\s*\([+-]?\d+\)",
            r"ХАРИЗМА\s+(\d+)\s*\([+-]?\d+\)",
            r"\|\s*\*?Charisma\*?\s*\|\s*(\d+)",
            r"\|\s*\*?CHA\*?\s*\|\s*(\d+)",
            r"\|\s*\*?Харизма\*?\s*\|\s*(\d+)",
            r"\|\s*CHA\s*\|\s*(\d+)\s*\|\s*[+-]?\d+",
            r"[Cc]harisma\s*[:=]\s*(\d+)\s*\(?[+-]?\d*\)?",
            r"CHA\s*[:=]\s*(\d+)",
            r"Харизма\s*[:=]\s*(\d+)",
            r"ХАР\s*[:=]\s*(\d+)",
            r"\bCHA\b\s+(\d+)\s*\(?[+-]?\d*\)?",
            r"\bCharisma\b\s+(\d+)",
            r"\bХаризма\b\s+(\d+)",
            r"\*\*Charisma\*\*\s*[:=]?\s*(\d+)",
            r"\*\*CHA\*\*\s*[:=]?\s*(\d+)",
            r"\*\*Харизма\*\*\s*[:=]?\s*(\d+)",
            r"Charisma\s*\n?\s*(\d+)\s*\n?\s*\(?[+-]?\d+\)?",
        ],
    }

    @classmethod
    def parse_file(cls, file_path: str) -> Optional[ParsedCharacter]:
        path = Path(file_path)
        if not path.exists():
            logger.error(f"File not found: {file_path}")
            return None
        try:
            content = path.read_text(encoding="utf-8")
            return cls.parse_text(content)
        except Exception as e:
            logger.error(f"Error reading character file: {e}")
            return None

    @classmethod
    def parse_text(cls, content: str) -> Optional[ParsedCharacter]:
        # Strip box-drawing characters for easier parsing
        content = re.sub(r'[║│╔╗╚╝╠╣╦╩═╬├┤┌┐└┘┬┴┼]', ' ', content)
        char = ParsedCharacter()

        # === LINE-BY-LINE STAT EXTRACTION ===
        # Handles formats like: "СИЛ   8   (–1)    │    ЛОВ  19   (+4)"
        stat_map = {
            'сил': 'strength', 'str': 'strength', 'strength': 'strength',
            'лов': 'dexterity', 'dex': 'dexterity', 'dexterity': 'dexterity', 'ловкость': 'dexterity',
            'тел': 'constitution', 'con': 'constitution', 'constitution': 'constitution', 'телосложение': 'constitution',
            'инт': 'intelligence', 'int': 'intelligence', 'intelligence': 'intelligence', 'интеллект': 'intelligence',
            'муд': 'wisdom', 'wis': 'wisdom', 'wisdom': 'wisdom', 'мудрость': 'wisdom',
            'хар': 'charisma', 'cha': 'charisma', 'charisma': 'charisma', 'харизма': 'charisma',
        }
        for line in content.splitlines():
            line_lower = line.lower()
            # Split by │ or multiple spaces
            parts = re.split(r'[│\|]', line)
            for part in parts:
                part_stripped = part.strip()
                part_lower = part_stripped.lower()
                for abbrev, stat_name in stat_map.items():
                    # Look for pattern: ABBREV number (modifier)
                    match = re.search(rf'{re.escape(abbrev)}\s+(\d+)', part_lower)
                    if match:
                        val = int(match.group(1))
                        if 1 <= val <= 30:
                            setattr(char, stat_name, val)
                            logger.info(f"Parsed {stat_name}: {val} from line")
                            break

        # Extract basic info
        char.name = cls._extract_name(content) or "Unknown"
        char.race = cls._extract_race(content) or ""
        char.class_name = cls._extract_class(content) or ""
        char.level = cls._extract_level(content) or 1
        char.background = cls._extract_background(content) or ""
        char.alignment = cls._extract_alignment(content) or ""

        # Extract ability scores — try ALL patterns for each stat
        for stat, patterns in cls.STAT_PATTERNS.items():
            value = cls._match_any_pattern(content, patterns)
            if value:
                setattr(char, stat, int(value))
                logger.info(f"Parsed {stat}: {value}")

        # Extract HP
        hp = cls._extract_hp(content)
        if hp:
            char.hp = hp
            char.max_hp = hp
        else:
            # Calculate from hit die + CON mod
            con_mod = char.get_modifier("constitution")
            char.hp = char.hit_dice + con_mod
            char.max_hp = char.hp

        # Extract AC
        ac = cls._extract_ac(content)
        if ac:
            char.ac = ac
        else:
            # Calculate base AC: 10 + DEX mod
            dex_mod = char.get_modifier("dexterity")
            char.ac = 10 + dex_mod

        # Extract gold
        gold = cls._extract_gold(content)
        if gold:
            char.gold = gold

        # Extract lists
        char.proficiencies = cls._extract_list(content, [
            r"[Pp]roficiencies[:\s]*\n((?:[-•*\d]\s*.+\n?)+)",
            r"[Pp]roficiency[:\s]*\n((?:[-•*\d]\s*.+\n?)+)",
            r"[Nn]aviki\s*[:\\s]*\n((?:[-•*\d]\s*.+\n?)+)",
            r"[Nn]avyki\s*[:\\s]*\n((?:[-•*\d]\s*.+\n?)+)",
        ])
        char.skills = cls._extract_list(content, [
            r"[Ss]kills[:\s]*\n((?:[-•*\d]\s*.+\n?)+)",
            r"[Nn]aviki\s*[:\\s]*\n((?:[-•*\d]\s*.+\n?)+)",
        ])
        char.languages = cls._extract_list(content, [
            r"[Ll]anguages[:\s]*\n((?:[-•*\d]\s*.+\n?)+)",
            r"Языки[:\s]*\n((?:[-•*\d]\s*.+\n?)+)",
        ])
        char.inventory = cls._extract_list(content, [
            r"[Ee]quipment[:\s]*\n((?:[-•*\d]\s*.+\n?)+)",
            r"[Ii]nventory[:\s]*\n((?:[-•*\d]\s*.+\n?)+)",
            r"[Ss]tarting [Ee]quipment[:\s]*\n((?:[-•*\d]\s*.+\n?)+)",
            r"Снаряжение[:\s]*\n((?:[-•*\d]\s*.+\n?)+)",
            r"Инвентарь[:\s]*\n((?:[-•*\d]\s*.+\n?)+)",
        ])
        char.spells = cls._extract_list(content, [
            r"[Ss]pells[:\s]*\n((?:[-•*\d]\s*.+\n?)+)",
            r"[Ss]pell [Ll]ist[:\s]*\n((?:[-•*\d]\s*.+\n?)+)",
            r"[Cc]antrips[:\s]*\n((?:[-•*\d]\s*.+\n?)+)",
            r"Заклинания[:\s]*\n((?:[-•*\d]\s*.+\n?)+)",
        ])
        char.features = cls._extract_list(content, [
            r"[Ff]eatures[:\s]*\n((?:[-•*\d]\s*.+\n?)+)",
            r"[Ff]eatures & [Tt]raits[:\s]*\n((?:[-•*\d]\s*.+\n?)+)",
            r"[Cc]lass [Ff]eatures[:\s]*\n((?:[-•*\d]\s*.+\n?)+)",
            r"[Rr]acial [Tt]raits[:\s]*\n((?:[-•*\d]\s*.+\n?)+)",
            r"Умения[:\s]*\n((?:[-•*\d]\s*.+\n?)+)",
            r"Способности[:\s]*\n((?:[-•*\d]\s*.+\n?)+)",
        ])

        # Extract text fields
        char.backstory = cls._extract_section(content, [
            r"[Bb]ackstory[:\s]*\n(.+?)(?=\n##|\n\n#{1,3}\s|\Z)",
            r"[Hh]istory[:\s]*\n(.+?)(?=\n##|\n\n#{1,3}\s|\Z)",
            r"[Ff]lavor[:\s]*\n(.+?)(?=\n##|\n\n#{1,3}\s|\Z)",
            r"Предыстория[:\s]*\n(.+?)(?=\n##|\n\n#{1,3}\s|\Z)",
        ])
        char.personality = cls._extract_section(content, [
            r"[Pp]ersonality[:\s]*\n(.+?)(?=\n##|\n\n#{1,3}\s|\Z)",
            r"Характер[:\s]*\n(.+?)(?=\n##|\n\n#{1,3}\s|\Z)",
        ])
        char.ideals = cls._extract_section(content, [
            r"[Ii]deals[:\s]*\n(.+?)(?=\n##|\n\n#{1,3}\s|\Z)",
            r"Идеалы[:\s]*\n(.+?)(?=\n##|\n\n#{1,3}\s|\Z)",
        ])
        char.bonds = cls._extract_section(content, [
            r"[Bb]onds[:\s]*\n(.+?)(?=\n##|\n\n#{1,3}\s|\Z)",
            r"Привязанности[:\s]*\n(.+?)(?=\n##|\n\n#{1,3}\s|\Z)",
        ])
        char.flaws = cls._extract_section(content, [
            r"[Ff]laws[:\s]*\n(.+?)(?=\n##|\n\n#{1,3}\s|\Z)",
            r"Пороки[:\s]*\n(.+?)(?=\n##|\n\n#{1,3}\s|\Z)",
        ])

        # Determine hit die from class
        char.hit_dice = cls._get_hit_die(char.class_name)

        return char

    @classmethod
    def _clean_md(cls, text: str) -> str:
        text = re.sub(r'\*\*', '', text)
        text = re.sub(r'\*', '', text)
        text = re.sub(r'`', '', text)
        text = re.sub(r'~~', '', text)
        return text.strip()

    @classmethod
    def _extract_name(cls, text: str) -> Optional[str]:
        patterns = [
            r"[Nn]ame[:\s]+\*?\*?(.+?)\*?\*?\s*$",
            r"[Cc]haracter [Nn]ame[:\s]+\*?\*?(.+?)\*?\*?\s*$",
            r"[Ии]мя[:\s]+\*?\*?(.+?)\*?\*?\s*$",
            r"[Пп]ерсонаж[:\s]+\*?\*?(.+?)\*?\*?\s*$",
            r"#+\s*(.+?)[\n\|]",
            r"^#\s*(.+?)$",
        ]
        for p in patterns:
            match = re.search(p, text, re.MULTILINE)
            if match:
                return cls._clean_md(match.group(1).strip())
        return None

    @classmethod
    def _extract_race(cls, text: str) -> Optional[str]:
        common_races = [
            "Human", "Elf", "Half-Elf", "Dwarf", "Halfling", "Gnome",
            "Tiefling", "Dragonborn", "Half-Orc", "Orc", "Aasimar",
            "Tabaxi", "Firbolg", "Goliath", "Kenku", "Lizardfolk",
            "Triton", "Genasi", "Aarakocra", "Bugbear", "Goblin",
            "Hobgoblin", "Kobold", "Yuan-ti", "Changeling", "Kalashtar",
            "Warforged", "Centaur", "Loxodon", "Minotaur", "Simic Hybrid",
            "Vedalken", "Leonin", "Satyr", "Owlin", "Fairy", "Harengon",
            "Plasmoid", "Autognome", "Giff", "Hadozee", "Thri-kreen",
            "Человек", "Эльф", "Полуэльф", "Дварф", "Полуорк",
            "Тифлинг", "Драконорожденный", "Гном", "Полурослик",
            "Полуэльф", "Полуорк", "Полурослик",
        ]

        match = re.search(r"[Rr]ace[:\s]+\*?\*?(.+?)\*?\*?\s*$", text, re.MULTILINE)
        if match:
            return cls._clean_md(match.group(1).strip())

        match = re.search(r"[Рр]аса[:\s]+\*?\*?(.+?)\*?\*?\s*$", text, re.MULTILINE)
        if match:
            return cls._clean_md(match.group(1).strip())

        text_lower = text.lower()
        for race in common_races:
            # Check for word boundaries
            pattern = r"\b" + re.escape(race.lower()) + r"\b"
            if re.search(pattern, text_lower):
                return race

        return None

    @classmethod
    def _extract_class(cls, text: str) -> Optional[str]:
        common_classes = [
            "Barbarian", "Bard", "Cleric", "Druid", "Fighter",
            "Monk", "Paladin", "Ranger", "Rogue", "Sorcerer",
            "Warlock", "Wizard", "Artificer", "Blood Hunter",
            "Варвар", "Бард", "Жрец", "Друид", "Воин",
            "Монах", "Паладин", "Следопыт", "Плут", "Чародей",
            "Колдун", "Маг", "Алхимик", "Следопыт",
        ]

        match = re.search(r"[Cc]lass[:\s]+\*?\*?(.+?)\*?\*?\s*$", text, re.MULTILINE)
        if match:
            return cls._clean_md(match.group(1).strip())

        match = re.search(r"[Кк]ласс[:\s]+\*?\*?(.+?)\*?\*?\s*$", text, re.MULTILINE)
        if match:
            return cls._clean_md(match.group(1).strip())

        match = re.search(r"[Cc]lass & [Ll]evel[:\s]+(\w+)", text)
        if match:
            return match.group(1).strip()

        text_lower = text.lower()
        for cls_name in common_classes:
            pattern = r"\b" + re.escape(cls_name.lower()) + r"\b"
            if re.search(pattern, text_lower):
                return cls_name

        return None

    @classmethod
    def _extract_level(cls, text: str) -> Optional[int]:
        patterns = [
            r"[Ll]evel[:\s]+\*?\*?(\d+)\*?\*?",
            r"[Cc]lass.*?\s+(\d+)",
            r"Lvl[:\s]+(\d+)",
            r"Уровень[:\s]+(\d+)",
            r"\*\*Level:\*\*\s*(\d+)",
            r"\*\*Уровень:\*\*\s*(\d+)",
            r"[Кк]ласс.*?\s+(\d+)",
            r"\bLevel\s+(\d+)\b",
            r"\bУровень\s+(\d+)\b",
        ]
        for p in patterns:
            match = re.search(p, text)
            if match:
                return int(match.group(1))
        return None

    @classmethod
    def _extract_background(cls, text: str) -> Optional[str]:
        match = re.search(r"[Bb]ackground[:\s]+\*?\*?(.+?)\*?\*?\s*$", text, re.MULTILINE)
        if match:
            return cls._clean_md(match.group(1).strip())
        match = re.search(r"[Пп]редыстория[:\s]+\*?\*?(.+?)\*?\*?\s*$", text, re.MULTILINE)
        if match:
            return cls._clean_md(match.group(1).strip())
        return None

    @classmethod
    def _extract_alignment(cls, text: str) -> Optional[str]:
        alignments = [
            "Lawful Good", "Neutral Good", "Chaotic Good",
            "Lawful Neutral", "True Neutral", "Neutral",
            "Chaotic Neutral", "Lawful Evil", "Neutral Evil", "Chaotic Evil",
            "Законно-добрый", "Нейтрально-добрый", "Хаотично-добрый",
            "Законно-нейтральный", "Истинно нейтральный", "Нейтральный",
            "Хаотично-нейтральный", "Законно-злой", "Нейтрально-злой", "Хаотично-злой",
        ]

        match = re.search(r"[Aa]lignment[:\s]+\*?\*?(.+?)\*?\*?\s*$", text, re.MULTILINE)
        if match:
            return cls._clean_md(match.group(1).strip())

        match = re.search(r"[Мм]ировоззрение[:\s]+\*?\*?(.+?)\*?\*?\s*$", text, re.MULTILINE)
        if match:
            return cls._clean_md(match.group(1).strip())

        text_lower = text.lower()
        for align in alignments:
            if align.lower() in text_lower:
                return align
        return None

    @classmethod
    def _extract_hp(cls, text: str) -> Optional[int]:
        patterns = [
            r"\*\*[Hh][Pp]:\*\*\s*(\d+)",
            r"[Hh][Pp][:\s]+\*?\*?(\d+)\*?\*?",
            r"[Hh]it [Pp]oints[:\s]+\*?\*?(\d+)\*?\*?",
            r"[Hh]ealth[:\s]+(\d+)",
            r"Здоровье[:\s]+(\d+)",
            r"Хиты[:\s]+(\d+)",
            r"[-*]\s*\*?[Hh][Pp]\*?[:\s]+\*?\*?(\d+)\*?\*?",
            r"\bHP\b[:\s]+(\d+)",
            r"\bХП\b[:\s]+(\d+)",
            r"\bHit Points\b[:\s]+(\d+)",
            r"\bМаксимум хитов\b[:\s]+(\d+)",
            r"\bMax HP\b[:\s]+(\d+)",
            r"\bCurrent HP\b[:\s]+(\d+)",
            r"\bТекущие хиты\b[:\s]+(\d+)",
        ]
        for p in patterns:
            match = re.search(p, text)
            if match:
                return int(match.group(1))

        match = re.search(r"[Mm]ax\s*[Hh][Pp][:\s]+(\d+)", text)
        if match:
            return int(match.group(1))

        return None

    @classmethod
    def _extract_ac(cls, text: str) -> Optional[int]:
        patterns = [
            r"\*\*[Aa][Cc]:\*\*\s*(\d+)",
            r"[Aa][Cc][:\s]+\*?\*?(\d+)\*?\*?",
            r"[Aa]rmor [Cc]lass[:\s]+\*?\*?(\d+)\*?\*?",
            r"Класс брони[:\s]+(\d+)",
            r"КБ[:\s]+(\d+)",
            r"[-*]\s*\*?[Aa][Cc]\*?[:\s]+\*?\*?(\d+)\*?\*?",
            r"\bAC\b[:\s]+(\d+)",
            r"\bArmor Class\b[:\s]+(\d+)",
            r"\bКласс Брони\b[:\s]+(\d+)",
            r"\bКБ\b[:\s]+(\d+)",
        ]
        for p in patterns:
            match = re.search(p, text)
            if match:
                return int(match.group(1))
        return None

    @classmethod
    def _extract_gold(cls, text: str) -> Optional[int]:
        patterns = [
            r"(\d+)\s*зм\s*стартового",
            r"(\d+)\s*зм",
            r"(\d+)\s*gp",
            r"(\d+)\s*золотых",
            r"(\d+)\s*gold",
        ]
        for p in patterns:
            match = re.search(p, text, re.IGNORECASE)
            if match:
                return int(match.group(1))
        return None

    @classmethod
    def _match_any_pattern(cls, text: str, patterns: List[str]) -> Optional[str]:
        """Try multiple patterns and return first match"""
        for p in patterns:
            match = re.search(p, text)
            if match:
                return match.group(1)
        return None

    @classmethod
    def _extract_list(cls, text: str, patterns: List[str]) -> List[str]:
        for p in patterns:
            match = re.search(p, text)
            if match:
                items_text = match.group(1)
                items = re.findall(r"[-•*]\s*(.+)", items_text)
                return [cls._clean_md(item.strip()) for item in items if item.strip()]
        return []

    @classmethod
    def _extract_section(cls, text: str, patterns: List[str]) -> str:
        for p in patterns:
            match = re.search(p, text, re.DOTALL)
            if match:
                return cls._clean_md(match.group(1).strip())
        return ""

    @classmethod
    def _get_hit_die(cls, class_name: str) -> int:
        hit_dice = {
            "sorcerer": 6, "wizard": 6, "чародей": 6, "маг": 6,
            "bard": 8, "cleric": 8, "druid": 8, "monk": 8, "rogue": 8, "warlock": 8,
            "бард": 8, "жрец": 8, "друид": 8, "монах": 8, "плут": 8, "колдун": 8,
            "fighter": 10, "paladin": 10, "ranger": 10, "artificer": 10,
            "воин": 10, "паладин": 10, "следопыт": 10,
            "barbarian": 12, "варвар": 12,
        }
        if not class_name:
            return 8
        return hit_dice.get(class_name.lower(), 8)

    @classmethod
    def validate_character(cls, char: ParsedCharacter) -> List[str]:
        issues = []

        if not char.name or char.name == "Unknown":
            issues.append("Character name not found")
        if not char.race:
            issues.append("Race not detected")
        if not char.class_name:
            issues.append("Class not detected")
        if char.level < 1 or char.level > 20:
            issues.append(f"Invalid level: {char.level}")

        for stat in ["strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma"]:
            val = getattr(char, stat)
            if val < 1 or val > 30:
                issues.append(f"Invalid {stat}: {val}")

        if char.hp <= 0:
            issues.append("HP must be greater than 0")
        if char.ac < 1:
            issues.append("AC must be greater than 0")

        return issues

    @classmethod
    def format_character_sheet(cls, char: ParsedCharacter) -> str:
        lines = [
            f"📜 **{char.name}**",
            f"{char.race} {char.class_name} {char.level}",
            f"HP: {char.hp}/{char.max_hp} | AC: {char.ac} | Speed: {char.speed}ft",
            "",
            "*Ability Scores:*",
        ]

        for stat in ["STR", "DEX", "CON", "INT", "WIS", "CHA"]:
            full = {"STR": "strength", "DEX": "dexterity", "CON": "constitution",
                    "INT": "intelligence", "WIS": "wisdom", "CHA": "charisma"}[stat]
            val = getattr(char, full)
            mod = (val - 10) // 2
            lines.append(f"{stat}: {val} ({mod:+d})")

        if char.proficiencies:
            lines.extend(["", "*Proficiencies:*", ", ".join(char.proficiencies[:10])])
        if char.skills:
            lines.extend(["", "*Skills:*", ", ".join(char.skills[:15])])
        if char.inventory:
            lines.extend(["", "*Inventory:*", ", ".join(char.inventory[:10])])
        if char.spells:
            lines.extend(["", "*Spells:*", ", ".join(char.spells[:10])])

        return "\n".join(lines)
