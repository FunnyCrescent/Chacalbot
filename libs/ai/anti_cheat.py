"""Anti-cheat validation system for D&D 5e (2014 PHB).

Validates character sheets against SRD reference data — races, classes, backgrounds,
feats, and backstory-claimed abilities.  The philosophy is **strict but not fascist**:
problems are flagged (WARNING or ERROR) but never block play; a DM can always override.

Typical usage::

    from libs.ai.anti_cheat import AntiCheatValidator

    v = AntiCheatValidator()
    result = v.validate_character(parsed_character)
    if not result.is_valid:
        for w in result.warnings:
            print(w.message)
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

from libs.character_parser import ParsedCharacter

logger = logging.getLogger(__name__)

# ───────────────────────────────────────────────────────────────
# Result data-classes
# ───────────────────────────────────────────────────────────────

@dataclass
class ValidationResult:
    """Result of a single validation check (race, class, background, etc.)."""
    is_valid: bool
    severity: str          # "error" | "warning" | "info"
    message: str
    suggestion: str = ""   # human-readable fix suggestion

    def __bool__(self) -> bool:
        return self.is_valid


@dataclass
class CharacterValidationResult:
    """Aggregate result of a full character validation."""
    is_valid: bool
    race_result: Optional[ValidationResult] = None
    class_result: Optional[ValidationResult] = None
    background_result: Optional[ValidationResult] = None
    backstory_result: Optional[ValidationResult] = None
    math_result: Optional[ValidationResult] = None
    warnings: List[ValidationResult] = field(default_factory=list)

    def all_issues(self) -> List[ValidationResult]:
        """Return all non-OK results (errors + warnings + info)."""
        issues: List[ValidationResult] = []
        for r in (self.race_result, self.class_result, self.background_result,
                  self.backstory_result, self.math_result):
            if r is not None and not r.is_valid:
                issues.append(r)
        issues.extend(self.warnings)
        return issues

    def errors(self) -> List[ValidationResult]:
        return [i for i in self.all_issues() if i.severity == "error"]

    def warnings_only(self) -> List[ValidationResult]:
        return [i for i in self.all_issues() if i.severity == "warning"]


# ───────────────────────────────────────────────────────────────
# SRD data loader
# ───────────────────────────────────────────────────────────────

_SRD_DIR = Path(__file__).resolve().parent.parent / "srd"


def _load_json(filename: str) -> Dict[str, Any]:
    """Load an SRD reference JSON file.  Returns empty dict on failure."""
    path = _SRD_DIR / filename
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        logger.error(f"Failed to load SRD data from {path}: {exc}")
        return {}


# ───────────────────────────────────────────────────────────────
# Known homebrew prefixes / markers
# ───────────────────────────────────────────────────────────────

_HOMEBREW_PREFIXES = (
    "homebrew:", "hb:", "custom:", "custom ", "homebrew ",
    "[hb]", "[homebrew]", "[custom]",
)

# Race names that are clearly trying to combine two SRD races for double bonuses
_SRD_RACE_WORDS = r"(dragonborn|dwarf|elf|orc|halfling|gnome|tiefling)"
_HALF_PREFIX = r"(?:half[-\s]?|semi[-\s]?)?"
_HYBRID_RACE_PATTERNS = [
    # Pattern with explicit connector: "Elf and Dwarf", "Dragonborn/Dwarf"
    re.compile(
        _HALF_PREFIX + _SRD_RACE_WORDS + r"\s+(?:and|\/|&|\+|or)\s+" + _HALF_PREFIX + _SRD_RACE_WORDS,
        re.IGNORECASE,
    ),
    # Pattern with two race words adjacent: "Elf Dwarf", "Dragonborn Elf"
    re.compile(
        _HALF_PREFIX + _SRD_RACE_WORDS + r"\s+" + _HALF_PREFIX + _SRD_RACE_WORDS,
        re.IGNORECASE,
    ),
]

# ───────────────────────────────────────────────────────────────
# Backstory mechanical-ability extraction
# ───────────────────────────────────────────────────────────────

# Patterns that indicate mechanical abilities claimed in backstory
_BACKSTORY_ABILITY_PATTERNS = [
    # Immunities
    (re.compile(r"\bimmun(?:e|ity|ities)\b", re.IGNORECASE), "immunity",
     "Immunity should come from class features, racial traits, or magic items — not backstory alone."),
    # Resistances
    (re.compile(r"\bresist(?:ance|ant|s)?\b", re.IGNORECASE), "resistance",
     "Damage resistance should come from racial traits (e.g. Dragonborn, Tiefling), class features, or magic items."),
    # Spell-like abilities
    (re.compile(r"\bcan\s+cast\b", re.IGNORECASE), "spell_like",
     "Spellcasting ability should come from class levels, racial traits (e.g. Drow), or feats (e.g. Magic Initiate)."),
    (re.compile(r"\binnate\s+spell(?:casting)?\b", re.IGNORECASE), "spell_like",
     "Innate spellcasting is a monster trait, not normally available to PCs without a racial feature."),
    (re.compile(r"\b(?:at[- ]?will)\b.*\b(?:spell|cantrip|magic)\b", re.IGNORECASE), "spell_like",
     "At-will spellcasting is extremely powerful and not available to standard PCs."),
    # Flying
    (re.compile(r"\b(?:natural|innate|permanent)\s+flight\b", re.IGNORECASE), "flight",
     "Permanent natural flight is not a standard backstory ability. Aarakocra/Winged Tiefling are racial traits."),
    (re.compile(r"\bcan\s+fly\b", re.IGNORECASE), "flight",
     "Flight ability should come from racial traits, class features, or magic items."),
    # Telepathy
    (re.compile(r"\btelepathy\b", re.IGNORECASE), "telepathy",
     "Telepathy is a racial/monster trait, not normally granted by backstory alone."),
    # Legendary resistances / actions
    (re.compile(r"\blegendary\s+(?:resistance|action)\b", re.IGNORECASE), "legendary",
     "Legendary resistances/actions are monster traits and should NEVER appear on a PC."),
    # Regeneration
    (re.compile(r"\bregenerat(?:e|es|ed|ion|ing)\b", re.IGNORECASE), "regeneration",
     "Regeneration is a monster trait. PCs can only regain HP through class features, spells, or items."),
    # Condition immunities
    (re.compile(r"\bimmune\s+to\s+(?:charmed?|frightened?|paralyzed?|petrified?|poisoned?|stunned?|blinded?|deafened?)\b", re.IGNORECASE), "condition_immunity",
     "Condition immunity should come from class features (e.g. Paladin Aura of Courage) or racial traits, not backstory."),
]

# Legitimate justifications that make backstory abilities acceptable
_JUSTIFICATION_PATTERNS = [
    re.compile(r"\bcursed?\b", re.IGNORECASE),
    re.compile(r"\bblessed?\b", re.IGNORECASE),
    re.compile(r"\bdivine\s+(?:gift|favor|blessing)\b", re.IGNORECASE),
    re.compile(r"\bpact\b", re.IGNORECASE),
    re.compile(r"\b(?:warlock|patron)\b", re.IGNORECASE),
    re.compile(r"\bhomebrew\b", re.IGNORECASE),
    re.compile(r"\bDM\s+(?:ruling|approval|granted|allowed)\b", re.IGNORECASE),
    re.compile(r"\b(?:magic|cursed|enchanted)\s+(?:item|object|artifact|weapon|armor)\b", re.IGNORECASE),
    re.compile(r"\bbloodline\b", re.IGNORECASE),
    re.compile(r"\bancestral\b", re.IGNORECASE),
]

# ───────────────────────────────────────────────────────────────
# Multiclass prerequisites (PHB p. 163)
# ───────────────────────────────────────────────────────────────

_MULTICLASS_PREREQS: Dict[str, Dict[str, int]] = {
    "Barbarian":  {"strength": 13},
    "Bard":       {"charisma": 13},
    "Cleric":     {"wisdom": 13},
    "Druid":      {"wisdom": 13},
    "Fighter":    {"strength": 13, "dexterity": 13},  # either STR 13 OR DEX 13
    "Monk":       {"dexterity": 13, "wisdom": 13},
    "Paladin":    {"strength": 13, "charisma": 13},
    "Ranger":     {"dexterity": 13, "wisdom": 13},
    "Rogue":      {"dexterity": 13},
    "Sorcerer":   {"charisma": 13},
    "Warlock":    {"charisma": 13},
    "Wizard":     {"intelligence": 13},
}

# Fighter is special: STR 13 OR DEX 13 (not both)
_MULTICLASS_PREREQS_ANY: Dict[str, List[Dict[str, int]]] = {
    "Fighter": [{"strength": 13}, {"dexterity": 13}],
}


# ───────────────────────────────────────────────────────────────
# AntiCheatValidator
# ───────────────────────────────────────────────────────────────

class AntiCheatValidator:
    """Validates D&D 5e (2014 PHB) characters against SRD reference data.

    Loads SRD JSON on first use (lazy) so import is cheap.
    All methods are synchronous and pure (no DB / LLM calls).
    """

    def __init__(self) -> None:
        self._classes_data: Optional[Dict] = None
        self._races_data: Optional[Dict] = None
        self._backgrounds_data: Optional[Dict] = None
        self._feats_data: Optional[Dict] = None

        # Normalised lookup sets (built lazily)
        self._srd_class_names: Optional[set] = None
        self._srd_race_names: Optional[set] = None
        self._srd_subrace_names: Optional[set] = None
        self._srd_background_names: Optional[set] = None
        self._srd_feat_names: Optional[set] = None

        # Full class → subclass mapping
        self._class_to_subclasses: Optional[Dict[str, set]] = None
        # Full race → subrace mapping
        self._race_to_subraces: Optional[Dict[str, set]] = None

    # ── lazy loaders ──────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        if self._classes_data is not None:
            return
        self._classes_data = _load_json("classes_5e_2014.json")
        # Load extended races (40+ races from all official books) if available,
        # otherwise fall back to PHB-only (9 races)
        import os
        extended_path = _SRD_DIR / "races_5e_extended.json"
        if extended_path.exists():
            self._races_data = _load_json("races_5e_extended.json")
            logger.info(f"[anti-cheat] Loaded extended races (40+ races from all official books)")
        else:
            self._races_data = _load_json("races_5e_2014.json")
            logger.info(f"[anti-cheat] Loaded PHB-only races (9 races)")
        self._backgrounds_data = _load_json("backgrounds_5e_2014.json")
        self._feats_data = _load_json("feats_5e_2014.json")

        # Build lookup sets
        self._srd_class_names = set(self._classes_data.get("classes", {}).keys())
        self._srd_race_names = set(self._races_data.get("races", {}).keys())
        self._srd_background_names = set(self._backgrounds_data.get("backgrounds", {}).keys())
        self._srd_feat_names = set(self._feats_data.get("feats", {}).keys())

        self._srd_subrace_names = set()
        self._race_to_subraces = {}
        for race_name, race_info in self._races_data.get("races", {}).items():
            sub_names = set(race_info.get("subraces", {}).keys())
            self._srd_subrace_names.update(sub_names)
            self._race_to_subraces[race_name] = sub_names

        self._class_to_subclasses = {}
        for cls_name, cls_info in self._classes_data.get("classes", {}).items():
            self._class_to_subclasses[cls_name] = set(cls_info.get("subclasses", {}).keys())

    # ── helpers ───────────────────────────────────────────────

    @staticmethod
    def _norm(name: str) -> str:
        """Normalise a name for comparison: lowercase, strip, collapse whitespace."""
        return re.sub(r"\s+", " ", name.strip().lower())

    def _is_homebrew(self, name: str) -> bool:
        """Check if a name is explicitly marked as homebrew."""
        name_lower = name.lower().strip()
        for prefix in _HOMEBREW_PREFIXES:
            if name_lower.startswith(prefix):
                return True
        return False

    def _find_srd_race(self, race_name: str) -> Optional[Tuple[str, Optional[str]]]:
        """Try to match a race name to an SRD race (+ optional subrace).

        Returns (race, subrace) or None.
        Handles inputs like "Wood Elf", "High-Elf", "Drow (Dark Elf)", etc.
        """
        self._ensure_loaded()
        norm = self._norm(race_name)

        # Direct match on race
        for srd_race in self._srd_race_names:
            if norm == self._norm(srd_race):
                return (srd_race, None)

        # Direct match on subrace
        for srd_race, subraces in self._race_to_subraces.items():
            for srd_sub in subraces:
                if norm == self._norm(srd_sub):
                    return (srd_race, srd_sub)

        # Handle hyphenated FIRST: "Half-Elf", "High-Elf", "Half-Orc"
        # Replace hyphens with spaces and retry
        hyphen_norm = norm.replace("-", " ")
        if hyphen_norm != norm:
            result = self._find_srd_race(hyphen_norm)
            if result:
                return result

        # "Subrace Race" pattern  e.g. "Wood Elf", "Hill Dwarf", "Drow Elf"
        parts = norm.rsplit(" ", 1)
        if len(parts) == 2:
            first, last = parts
            # Check if last is the race and first is the subrace
            for srd_race in self._srd_race_names:
                if last == self._norm(srd_race):
                    for srd_sub in self._race_to_subraces.get(srd_race, set()):
                        if first == self._norm(srd_sub):
                            return (srd_race, srd_sub)
            # Check if first is the race and last is the subrace
            for srd_race in self._srd_race_names:
                if first == self._norm(srd_race):
                    for srd_sub in self._race_to_subraces.get(srd_race, set()):
                        if last == self._norm(srd_sub):
                            return (srd_race, srd_sub)

        # Parenthetical: "Elf (Drow)" or "Drow (Dark Elf)"
        m = re.match(r"^(.+?)\s*\((.+?)\)\s*$", norm)
        if m:
            main, sub = m.group(1).strip(), m.group(2).strip()
            # Case 1: main is race, sub is subrace — "Elf (Drow)"
            for srd_race in self._srd_race_names:
                if main == self._norm(srd_race):
                    for srd_sub in self._race_to_subraces.get(srd_race, set()):
                        if sub == self._norm(srd_sub):
                            return (srd_race, srd_sub)
            # Case 2: main is subrace — "Drow (Dark Elf)" — subrace is Drow, parenthetical is flavor
            for srd_race, subraces in self._race_to_subraces.items():
                for srd_sub in subraces:
                    if main == self._norm(srd_sub):
                        return (srd_race, srd_sub)

        return None

    def _find_srd_class(self, class_name: str) -> Optional[str]:
        """Match a class name to an SRD class. Returns class name or None."""
        self._ensure_loaded()
        norm = self._norm(class_name)

        # Direct match
        for srd_cls in self._srd_class_names:
            if norm == self._norm(srd_cls):
                return srd_cls

        # Common aliases / abbreviations
        aliases = {
            "wiz": "Wizard", "wizy": "Wizard",
            "sorc": "Sorcerer",
            "barb": "Barbarian",
            "fight": "Fighter", "ftr": "Fighter",
            "rog": "Rogue",
            "rgr": "Ranger",
            "pal": "Paladin",
            "mnk": "Monk",
            "clr": "Cleric",
            "drd": "Druid",
            "brd": "Bard",
            "lok": "Warlock", "lock": "Warlock",
        }
        if norm in aliases:
            return aliases[norm]

        return None

    def _parse_multiclass(self, class_name: str) -> Optional[List[Tuple[str, Optional[int]]]]:
        """Try to parse a multiclass string like "Fighter 3 / Rogue 2" or "Fighter/Rogue".

        Returns list of (class_name, level_or_None) or None if not multiclass.
        """
        # Split on / or |
        parts = re.split(r"\s*/\s*|\s*\|\s*", class_name.strip())
        if len(parts) < 2:
            return None

        result = []
        for part in parts:
            part = part.strip()
            if not part:
                continue
            # Try "ClassName N" or "ClassName (N)"
            m = re.match(r"^(.+?)\s+(?:\(?\s*(\d+)\s*\)?)$", part)
            if m:
                result.append((m.group(1).strip(), int(m.group(2))))
            else:
                result.append((part, None))

        return result if len(result) >= 2 else None

    # ── public validation methods ─────────────────────────────

    def validate_race(self, race_name: str) -> ValidationResult:
        """Validate a race name against SRD races.

        - SRD race → VALID (info)
        - Unknown but homebrew-prefixed → VALID with info
        - Unknown → WARNING (homebrew allowed but flagged)
        - Hybrid exploit (combining two SRD races) → ERROR
        """
        self._ensure_loaded()

        if not race_name or not race_name.strip():
            return ValidationResult(
                is_valid=False, severity="error",
                message="Race is empty or missing.",
                suggestion="Provide a valid D&D 5e race."
            )

        # Check for hybrid race exploits first
        for pattern in _HYBRID_RACE_PATTERNS:
            if pattern.search(race_name):
                return ValidationResult(
                    is_valid=False, severity="error",
                    message=f"'{race_name}' appears to combine two SRD races for double racial bonuses — this is an exploit.",
                    suggestion="Use a single SRD race, a legitimate homebrew race with [homebrew] prefix, or ask your DM for a custom race."
                )

        # Homebrew prefix → allow with info
        if self._is_homebrew(race_name):
            return ValidationResult(
                is_valid=True, severity="info",
                message=f"'{race_name}' is marked as homebrew.",
                suggestion=""
            )

        # SRD match
        match = self._find_srd_race(race_name)
        if match:
            race, subrace = match
            if subrace:
                return ValidationResult(
                    is_valid=True, severity="info",
                    message=f"'{race_name}' matches SRD race {race} ({subrace}).",
                    suggestion=""
                )
            return ValidationResult(
                is_valid=True, severity="info",
                message=f"'{race_name}' matches SRD race {race}.",
                suggestion=""
            )

        # Unknown → WARNING
        # Build list of similar races for suggestion
        suggestions = self._suggest_similar(race_name, self._srd_race_names | self._srd_subrace_names)
        suggestion_text = ""
        if suggestions:
            suggestion_text = f"Did you mean: {', '.join(suggestions)}? Otherwise, prefix with [homebrew] if this is intentional."

        return ValidationResult(
            is_valid=True, severity="warning",
            message=f"'{race_name}' is not a recognized SRD race. This may be homebrew.",
            suggestion=suggestion_text or "If this is homebrew, consider prefixing with [homebrew] to suppress this warning."
        )

    def validate_class(self, class_name: str) -> ValidationResult:
        """Validate a class name against SRD classes.

        - SRD class → VALID
        - Multiclass → check prerequisites
        - Homebrew-prefixed → VALID with info
        - Unknown → WARNING
        """
        self._ensure_loaded()

        if not class_name or not class_name.strip():
            return ValidationResult(
                is_valid=False, severity="error",
                message="Class is empty or missing.",
                suggestion="Provide a valid D&D 5e class."
            )

        # Homebrew prefix
        if self._is_homebrew(class_name):
            return ValidationResult(
                is_valid=True, severity="info",
                message=f"'{class_name}' is marked as homebrew.",
                suggestion=""
            )

        # Check for multiclass
        mc_parts = self._parse_multiclass(class_name)
        if mc_parts:
            return self._validate_multiclass(mc_parts)

        # SRD match
        match = self._find_srd_class(class_name)
        if match:
            return ValidationResult(
                is_valid=True, severity="info",
                message=f"'{class_name}' matches SRD class {match}.",
                suggestion=""
            )

        # Check if it contains an SRD subclass name (e.g. "Eldritch Knight")
        for srd_cls, subclasses in self._class_to_subclasses.items():
            for sub in subclasses:
                if self._norm(class_name) == self._norm(sub):
                    return ValidationResult(
                        is_valid=True, severity="info",
                        message=f"'{class_name}' matches SRD subclass {sub} of {srd_cls}.",
                        suggestion=f"Consider specifying the full class, e.g. '{srd_cls} ({sub})'."
                    )

        # Unknown → WARNING
        suggestions = self._suggest_similar(class_name, self._srd_class_names)
        suggestion_text = ""
        if suggestions:
            suggestion_text = f"Did you mean: {', '.join(suggestions)}?"

        return ValidationResult(
            is_valid=True, severity="warning",
            message=f"'{class_name}' is not a recognized SRD class. This may be homebrew.",
            suggestion=suggestion_text or "If this is homebrew, prefix with [homebrew]."
        )

    def _validate_multiclass(self, parts: List[Tuple[str, Optional[int]]]) -> ValidationResult:
        """Validate a multiclass combination. Returns WARNING if prerequisites might not be met."""
        issues: List[str] = []
        recognised_classes: List[str] = []

        for cls_name, _level in parts:
            match = self._find_srd_class(cls_name)
            if match:
                recognised_classes.append(match)
            else:
                issues.append(f"'{cls_name}' is not a recognized SRD class.")

        if len(recognised_classes) < 2 and not issues:
            # Only one SRD class found among parts — might not be a real multiclass
            return ValidationResult(
                is_valid=True, severity="warning",
                message=f"Multiclass '{parts}' has only one recognised SRD class.",
                suggestion="Check class syntax: 'Fighter 3 / Rogue 2'."
            )

        # We can't fully verify prereqs without stat scores, but we can note them
        prereq_notes: List[str] = []
        for cls in recognised_classes:
            if cls in _MULTICLASS_PREREQS:
                reqs = _MULTICLASS_PREREQS[cls]
                req_str = ", ".join(f"{stat.upper()} {val}" for stat, val in reqs.items())
                prereq_notes.append(f"{cls} requires {req_str}")

        if issues:
            return ValidationResult(
                is_valid=True, severity="warning",
                message=f"Multiclass validation: {'; '.join(issues)}",
                suggestion="Ensure all class names are spelled correctly."
            )

        if prereq_notes:
            return ValidationResult(
                is_valid=True, severity="info",
                message=f"Multiclass combination: {', '.join(recognised_classes)}. Prerequisites: {'; '.join(prereq_notes)}. Verify ability scores meet these requirements.",
                suggestion=""
            )

        return ValidationResult(
            is_valid=True, severity="info",
            message=f"Multiclass combination: {', '.join(recognised_classes)}.",
            suggestion=""
        )

    def validate_background(self, background_name: str) -> ValidationResult:
        """Validate a background name against SRD backgrounds.

        - SRD background → VALID
        - Homebrew-prefixed → VALID with info
        - Unknown → WARNING (homebrew allowed but flagged)
        """
        self._ensure_loaded()

        if not background_name or not background_name.strip():
            # Background is optional in some games
            return ValidationResult(
                is_valid=True, severity="info",
                message="No background specified.",
                suggestion="Consider adding a PHB background for skill proficiencies."
            )

        # Homebrew prefix
        if self._is_homebrew(background_name):
            return ValidationResult(
                is_valid=True, severity="info",
                message=f"'{background_name}' is marked as homebrew.",
                suggestion=""
            )

        # SRD match
        norm = self._norm(background_name)
        for srd_bg in self._srd_background_names:
            if norm == self._norm(srd_bg):
                return ValidationResult(
                    is_valid=True, severity="info",
                    message=f"'{background_name}' matches SRD background {srd_bg}.",
                    suggestion=""
                )

        # Partial match (e.g. "Criminal/Spy" for "Criminal")
        for srd_bg in self._srd_background_names:
            if self._norm(srd_bg) in norm or norm in self._norm(srd_bg):
                return ValidationResult(
                    is_valid=True, severity="info",
                    message=f"'{background_name}' partially matches SRD background {srd_bg}.",
                    suggestion=""
                )

        # Unknown → WARNING
        suggestions = self._suggest_similar(background_name, self._srd_background_names)
        suggestion_text = ""
        if suggestions:
            suggestion_text = f"Did you mean: {', '.join(suggestions)}?"

        return ValidationResult(
            is_valid=True, severity="warning",
            message=f"'{background_name}' is not a recognized SRD background. This may be homebrew.",
            suggestion=suggestion_text or "If this is homebrew, prefix with [homebrew]."
        )

    def validate_backstory_ability(
        self,
        backstory: str,
        claimed_abilities: Optional[List[str]] = None,
    ) -> ValidationResult:
        """Check if a backstory claims mechanical abilities it shouldn't have.

        Scans the backstory text for patterns like 'immune', 'resistant',
        'can cast', 'regeneration', etc.  If found WITHOUT a justification
        (cursed item, divine gift, pact, DM approval, etc.), flags as ERROR
        or WARNING.

        Args:
            backstory: The character's backstory text.
            claimed_abilities: Optional explicit list of claimed ability names
                              (e.g. ["fire resistance", "telepathy"]).
        """
        if not backstory or not backstory.strip():
            return ValidationResult(
                is_valid=True, severity="info",
                message="No backstory to validate.",
                suggestion=""
            )

        # Check for justifications first
        has_justification = any(pat.search(backstory) for pat in _JUSTIFICATION_PATTERNS)

        found_abilities: List[Tuple[str, str, str]] = []  # (matched_text, category, explanation)

        for pattern, category, explanation in _BACKSTORY_ABILITY_PATTERNS:
            match = pattern.search(backstory)
            if match:
                found_abilities.append((match.group(), category, explanation))

        # Also check explicitly claimed abilities
        if claimed_abilities:
            for ability in claimed_abilities:
                ability_lower = ability.lower().strip()
                if "immune" in ability_lower:
                    found_abilities.append((ability, "immunity", "Immunity should come from class features, racial traits, or magic items."))
                elif "resist" in ability_lower:
                    found_abilities.append((ability, "resistance", "Damage resistance should come from racial traits, class features, or magic items."))
                elif "fly" in ability_lower:
                    found_abilities.append((ability, "flight", "Flight should come from racial traits, class features, or magic items."))
                elif "telepath" in ability_lower:
                    found_abilities.append((ability, "telepathy", "Telepathy is a racial/monster trait."))
                elif "regenerat" in ability_lower:
                    found_abilities.append((ability, "regeneration", "Regeneration is a monster trait."))

        if not found_abilities:
            return ValidationResult(
                is_valid=True, severity="info",
                message="Backstory does not claim unsupported mechanical abilities.",
                suggestion=""
            )

        # Build results
        categories = set(cat for _, cat, _ in found_abilities)

        # Legendary stuff is always an error even with justification
        if "legendary" in categories:
            return ValidationResult(
                is_valid=False, severity="error",
                message=f"Backstory claims legendary resistance/actions: this is a MONSTER trait and should NEVER be on a PC.",
                suggestion="Remove legendary resistance/actions. If the DM specifically granted this, note 'DM ruling' in the backstory."
            )

        if has_justification:
            # With justification, downgrade to warning
            ability_list = ", ".join(f"'{m}' ({c})" for m, c, _ in found_abilities)
            return ValidationResult(
                is_valid=True, severity="warning",
                message=f"Backstory claims mechanical abilities ({ability_list}) but includes a justification. Verify with DM.",
                suggestion="Ensure the DM has approved these abilities."
            )

        # Without justification — error for serious stuff, warning for minor
        serious_cats = {"immunity", "regeneration", "legendary", "condition_immunity", "spell_like"}
        has_serious = bool(categories & serious_cats)

        ability_list = "; ".join(expl for _, _, expl in found_abilities)

        if has_serious:
            return ValidationResult(
                is_valid=False, severity="error",
                message=f"Backstory claims mechanical abilities without justification: {ability_list}",
                suggestion="Add a justification (e.g. 'cursed item', 'divine gift', 'pact with patron', 'DM ruling') or remove the ability claim."
            )

        return ValidationResult(
            is_valid=True, severity="warning",
            message=f"Backstory claims minor mechanical abilities without explicit justification: {ability_list}",
            suggestion="Consider adding a source for these abilities (racial trait, feat, magic item, or DM ruling)."
        )

    def validate_character(self, character: ParsedCharacter) -> CharacterValidationResult:
        """Comprehensive validation of a parsed character.

        Checks race, class, background, backstory abilities, and basic math.
        Returns a CharacterValidationResult with all individual results.
        """
        race_result = self.validate_race(character.race)
        class_result = self.validate_class(character.class_name)
        background_result = self.validate_background(getattr(character, "background", "") or "")
        backstory_result = self.validate_backstory_ability(character.backstory)

        # Math validation (basic stat range checks)
        math_result = self._validate_math(character)

        # Collect all warnings
        warnings: List[ValidationResult] = []
        for r in (race_result, class_result, background_result, backstory_result, math_result):
            if r is not None and r.severity == "warning":
                warnings.append(r)

        # Overall validity: any ERROR severity = not valid
        has_error = any(
            r is not None and not r.is_valid
            for r in (race_result, class_result, background_result, backstory_result, math_result)
        )

        return CharacterValidationResult(
            is_valid=not has_error,
            race_result=race_result,
            class_result=class_result,
            background_result=background_result,
            backstory_result=backstory_result,
            math_result=math_result,
            warnings=warnings,
        )

    def _validate_math(self, character: ParsedCharacter) -> ValidationResult:
        """Basic mathematical / range validation for stats.

        This is a lightweight check — the full arithmetic validation is done
        by the LLM-based validator.  Here we catch obviously impossible values.
        """
        issues: List[str] = []

        # Stat range check (1-30 for PCs, 1-20 normal)
        for stat_name in ("strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma"):
            val = getattr(character, stat_name, 10)
            if not isinstance(val, (int, float)):
                issues.append(f"{stat_name.title()} is not a number: {val!r}")
                continue
            if val < 1:
                issues.append(f"{stat_name.title()} = {val} is below minimum (1).")
            elif val > 30:
                issues.append(f"{stat_name.title()} = {val} is above maximum (30) — only artifacts reach this.")

        # Level check
        level = getattr(character, "level", 1)
        if not isinstance(level, (int, float)):
            issues.append(f"Level is not a number: {level!r}")
        elif level < 1:
            issues.append(f"Level = {level} is below minimum (1).")
        elif level > 20:
            issues.append(f"Level = {level} is above maximum (20) for standard play.")

        # HP check
        hp = getattr(character, "hp", 0)
        max_hp = getattr(character, "max_hp", 0)
        if isinstance(hp, (int, float)) and isinstance(max_hp, (int, float)):
            if hp < 0:
                issues.append(f"HP = {hp} is negative.")
            if max_hp < 1:
                issues.append(f"Max HP = {max_hp} is below minimum (1).")
            if hp > max_hp:
                issues.append(f"HP ({hp}) exceeds Max HP ({max_hp}).")

        # AC check
        ac = getattr(character, "ac", 10)
        if isinstance(ac, (int, float)):
            if ac < 1:
                issues.append(f"AC = {ac} is below minimum (1).")
            elif ac > 35:
                issues.append(f"AC = {ac} is suspiciously high (35+) for a PC.")

        if not issues:
            return ValidationResult(
                is_valid=True, severity="info",
                message="Basic math/range checks passed.",
                suggestion=""
            )

        return ValidationResult(
            is_valid=False, severity="error",
            message="Math/range issues: " + "; ".join(issues),
            suggestion="Fix the listed values to be within valid ranges."
        )

    def validate_multiclass_prerequisites(
        self,
        class_name: str,
        stats: Dict[str, int],
    ) -> ValidationResult:
        """Check if a character's stats meet the multiclass prerequisites for a given class.

        Args:
            class_name: The class to check prerequisites for.
            stats: Dict of ability scores, e.g. {"strength": 15, "dexterity": 14, ...}
        """
        self._ensure_loaded()

        srd_class = self._find_srd_class(class_name)
        if not srd_class:
            return ValidationResult(
                is_valid=True, severity="info",
                message=f"Cannot check multiclass prerequisites for unknown class '{class_name}'.",
                suggestion=""
            )

        # Check "any of" prerequisites first (Fighter)
        if srd_class in _MULTICLASS_PREREQS_ANY:
            any_met = False
            for req_set in _MULTICLASS_PREREQS_ANY[srd_class]:
                if all(stats.get(stat, 0) >= val for stat, val in req_set.items()):
                    any_met = True
                    break
            if not any_met:
                req_strs = []
                for req_set in _MULTICLASS_PREREQS_ANY[srd_class]:
                    req_strs.append(" or ".join(f"{s.upper()} {v}" for s, v in req_set.items()))
                return ValidationResult(
                    is_valid=False, severity="error",
                    message=f"Stats do not meet multiclass prerequisite for {srd_class}: need {' or '.join(req_strs)}.",
                    suggestion="Increase the relevant ability score(s)."
                )
            return ValidationResult(
                is_valid=True, severity="info",
                message=f"Stats meet multiclass prerequisites for {srd_class}.",
                suggestion=""
            )

        # Check "all of" prerequisites
        if srd_class in _MULTICLASS_PREREQS:
            reqs = _MULTICLASS_PREREQS[srd_class]
            missing = []
            for stat, val in reqs.items():
                if stats.get(stat, 0) < val:
                    missing.append(f"{stat.upper()} {val} (have {stats.get(stat, 0)})")
            if missing:
                return ValidationResult(
                    is_valid=False, severity="error",
                    message=f"Stats do not meet multiclass prerequisites for {srd_class}: need {', '.join(missing)}.",
                    suggestion="Increase the relevant ability score(s)."
                )
            return ValidationResult(
                is_valid=True, severity="info",
                message=f"Stats meet multiclass prerequisites for {srd_class}.",
                suggestion=""
            )

        return ValidationResult(
            is_valid=True, severity="info",
            message=f"No multiclass prerequisites defined for {srd_class}.",
            suggestion=""
        )

    # ── similarity / suggestion helper ────────────────────────

    @staticmethod
    def _suggest_similar(name: str, candidates: set, max_suggestions: int = 3) -> List[str]:
        """Return up to N similar candidate names using simple substring/edit-distance."""
        if not candidates:
            return []

        norm = name.lower().strip()
        scored: List[Tuple[float, str]] = []

        for candidate in candidates:
            c_norm = candidate.lower().strip()

            # Exact substring match → highest score
            if norm in c_norm or c_norm in norm:
                scored.append((-100.0, candidate))
                continue

            # Common words
            norm_words = set(norm.split())
            c_words = set(c_norm.split())
            overlap = len(norm_words & c_words)
            if overlap > 0:
                scored.append((-50.0 + overlap, candidate))
                continue

            # Simple character-level similarity (Levenshtein-like)
            # Using a cheap approximation: 1 - (edit_dist / max_len)
            max_len = max(len(norm), len(c_norm), 1)
            # Count matching characters in order
            matches = 0
            j = 0
            for ch in norm:
                while j < len(c_norm) and c_norm[j] != ch:
                    j += 1
                if j < len(c_norm):
                    matches += 1
                    j += 1
            similarity = matches / max_len
            if similarity > 0.4:
                scored.append((-similarity, candidate))

        scored.sort()
        return [c for _, c in scored[:max_suggestions]]

    # ── SRD data access for external consumers ────────────────

    def get_srd_class_info(self, class_name: str) -> Optional[Dict]:
        """Return SRD data for a class, or None."""
        self._ensure_loaded()
        match = self._find_srd_class(class_name)
        if match:
            return self._classes_data.get("classes", {}).get(match)
        return None

    def get_srd_race_info(self, race_name: str) -> Optional[Dict]:
        """Return SRD data for a race, or None."""
        self._ensure_loaded()
        match = self._find_srd_race(race_name)
        if match:
            race, _subrace = match
            return self._races_data.get("races", {}).get(race)
        return None

    def get_srd_background_info(self, background_name: str) -> Optional[Dict]:
        """Return SRD data for a background, or None."""
        self._ensure_loaded()
        norm = self._norm(background_name)
        for srd_bg, info in self._backgrounds_data.get("backgrounds", {}).items():
            if norm == self._norm(srd_bg):
                return info
        return None

    def get_srd_feat_info(self, feat_name: str) -> Optional[Dict]:
        """Return SRD data for a feat, or None."""
        self._ensure_loaded()
        norm = self._norm(feat_name)
        for srd_feat, info in self._feats_data.get("feats", {}).items():
            if norm == self._norm(srd_feat):
                return info
        return None

    def list_srd_classes(self) -> List[str]:
        self._ensure_loaded()
        return sorted(self._srd_class_names)

    def list_srd_races(self) -> List[str]:
        self._ensure_loaded()
        return sorted(self._srd_race_names)

    def list_srd_backgrounds(self) -> List[str]:
        self._ensure_loaded()
        return sorted(self._srd_background_names)

    def list_srd_feats(self) -> List[str]:
        self._ensure_loaded()
        return sorted(self._srd_feat_names)
