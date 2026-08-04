"""
D&D Dice Parser — handles all dice rolling mechanics
Supports: d4, d6, d8, d10, d12, d20, d100
Advantage/Disadvantage, Critical hits/misses, modifiers
"""

import random
import re
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple


class RollType(Enum):
    NORMAL = "normal"
    ADVANTAGE = "advantage"
    DISADVANTAGE = "disadvantage"


@dataclass
class DiceRoll:
    """Result of a single dice roll"""
    count: int           # Number of dice
    sides: int           # Dice sides (4, 6, 8, 10, 12, 20, 100)
    modifier: int        # +/- modifier
    roll_type: RollType  # Normal/Advantage/Disadvantage
    
    # Results
    rolls: List[int] = None           # Individual dice results
    advantage_rolls: List[int] = None # Second roll for adv/dis
    total: int = 0
    
    # Metadata
    is_critical_success: bool = False
    is_critical_failure: bool = False
    natural_roll: int = 0  # Raw d20 roll before modifier
    
    def __post_init__(self):
        if self.rolls is None:
            self.rolls = []
        if self.advantage_rolls is None:
            self.advantage_rolls = []


@dataclass
class ParsedRoll:
    """Fully parsed roll request"""
    original_text: str
    rolls: List[DiceRoll]
    grand_total: int
    description: str = ""  # Optional description (e.g., "attack with sword")


class DiceParser:
    """Parser and roller for D&D dice notation"""
    
    # Standard D&D dice
    VALID_DICE = {4, 6, 8, 10, 12, 20, 100}
    
    # Dice regex: NdM+X or NdM-X
    DICE_REGEX = re.compile(
        r"(?:(\d+)\s*[dD]\s*(\d+)(?:\s*([+\-])\s*(\d+))?)|"
        r"(?:[dD](\d+)(?:\s*([+\-])\s*(\d+))?)"
    )
    
    @classmethod
    def parse_and_roll(cls, text: str) -> Optional[ParsedRoll]:
        """
        Parse dice notation and roll.
        Returns None if no dice pattern found.
        
        Examples:
            "2d20+5" → rolls 2d20+5
            "d8" → rolls 1d8
            "1d20 with advantage" → rolls with advantage
            "attack: d20+4" → rolls d20+4 with description
        """
        text_lower = text.lower().strip()
        
        # Check for advantage/disadvantage
        roll_type = RollType.NORMAL
        if any(kw in text_lower for kw in ["adv", "advantage", "преим", "преимущество"]):
            roll_type = RollType.ADVANTAGE
        elif any(kw in text_lower for kw in ["dis", "disadvantage", "помеха", "помех"]):
            roll_type = RollType.DISADVANTAGE
        
        # Extract description (text before or after dice)
        description = ""
        
        # Find all dice patterns
        rolls: List[DiceRoll] = []
        
        # Match standard patterns: NdM+X or dM+X
        matches = cls.DICE_REGEX.findall(text)
        if not matches:
            return None
            
        for match in matches:
            if match[0] and match[1]:  # NdM format
                count = int(match[0])
                sides = int(match[1])
                modifier = 0
                if match[2] and match[3]:
                    modifier = int(match[3]) if match[2] == '+' else -int(match[3])
            elif match[4]:  # dM format (no count)
                count = 1
                sides = int(match[4])
                modifier = 0
                if match[5] and match[6]:
                    modifier = int(match[6]) if match[5] == '+' else -int(match[6])
            else:
                continue
            
            if sides not in cls.VALID_DICE:
                continue
                
            roll = cls._perform_roll(count, sides, modifier, roll_type)
            rolls.append(roll)
        
        if not rolls:
            return None
            
        grand_total = sum(r.total for r in rolls)
        
        return ParsedRoll(
            original_text=text,
            rolls=rolls,
            grand_total=grand_total,
            description=description
        )
    
    @classmethod
    def _perform_roll(cls, count: int, sides: int, modifier: int, 
                      roll_type: RollType) -> DiceRoll:
        """Perform the actual dice roll"""
        
        dice_roll = DiceRoll(
            count=count,
            sides=sides,
            modifier=modifier,
            roll_type=roll_type
        )
        
        # Roll the dice
        for _ in range(count):
            roll = random.randint(1, sides)
            dice_roll.rolls.append(roll)
        
        # Handle advantage/disadvantage for d20
        if sides == 20 and roll_type != RollType.NORMAL:
            for _ in range(count):
                roll2 = random.randint(1, 20)
                dice_roll.advantage_rolls.append(roll2)
        
        # Calculate total
        if sides == 20 and roll_type != RollType.NORMAL:
            # For adv/dis, pick appropriate rolls
            final_rolls = []
            for i, r1 in enumerate(dice_roll.rolls):
                r2 = dice_roll.advantage_rolls[i] if i < len(dice_roll.advantage_rolls) else r1
                if roll_type == RollType.ADVANTAGE:
                    final_rolls.append(max(r1, r2))
                else:
                    final_rolls.append(min(r1, r2))
            dice_roll.total = sum(final_rolls) + modifier
            dice_roll.natural_roll = final_rolls[0] if final_rolls else 0
        else:
            dice_roll.total = sum(dice_roll.rolls) + modifier
            dice_roll.natural_roll = dice_roll.rolls[0] if dice_roll.rolls else 0
        
        # Check criticals (only for d20)
        if sides == 20:
            if roll_type == RollType.NORMAL:
                dice_roll.is_critical_success = any(r == 20 for r in dice_roll.rolls)
                dice_roll.is_critical_failure = any(r == 1 for r in dice_roll.rolls)
            else:
                # For adv/dis, check the selected rolls
                for i, r1 in enumerate(dice_roll.rolls):
                    r2 = dice_roll.advantage_rolls[i] if i < len(dice_roll.advantage_rolls) else r1
                    selected = max(r1, r2) if roll_type == RollType.ADVANTAGE else min(r1, r2)
                    if selected == 20:
                        dice_roll.is_critical_success = True
                    if selected == 1:
                        dice_roll.is_critical_failure = True
        
        return dice_roll
    
    @classmethod
    def format_result(cls, parsed: ParsedRoll) -> str:
        """Format roll result for display"""
        lines = []
        
        for roll in parsed.rolls:
            # Build roll notation
            notation = f"{roll.count}d{roll.sides}"
            if roll.modifier > 0:
                notation += f"+{roll.modifier}"
            elif roll.modifier < 0:
                notation += f"{roll.modifier}"
            
            adv_label = ""
            if roll.roll_type == RollType.ADVANTAGE:
                adv_label = " (преимущество)"
            elif roll.roll_type == RollType.DISADVANTAGE:
                adv_label = " (помеха)"
            
            # Show advantage/disadvantage details for d20
            if roll.roll_type != RollType.NORMAL and roll.sides == 20 and roll.advantage_rolls:
                adv_details = []
                for i, r1 in enumerate(roll.rolls):
                    r2 = roll.advantage_rolls[i] if i < len(roll.advantage_rolls) else r1
                    if roll.roll_type == RollType.ADVANTAGE:
                        selected = max(r1, r2)
                        adv_details.append(f"[{r1},{r2}]→{selected}")
                    else:
                        selected = min(r1, r2)
                        adv_details.append(f"[{r1},{r2}]→{selected}")
                roll_details = " + ".join(adv_details)
            else:
                # Normal roll or non-d20
                roll_details = " + ".join(str(r) for r in roll.rolls)
            
            # Build result line
            modifier_str = ""
            if roll.modifier > 0:
                modifier_str = f" + {roll.modifier}"
            elif roll.modifier < 0:
                modifier_str = f" - {abs(roll.modifier)}"
            
            result_line = f"🎲 {notation}{adv_label}: {roll_details}{modifier_str} = **{roll.total}**"
            
            # Add critical indicators
            if roll.is_critical_success:
                result_line += " 💥 **КРИТИЧЕСКИЙ УСПЕХ!**"
            elif roll.is_critical_failure:
                result_line += " 💀 **КРИТИЧЕСКИЙ ПРОВАЛ!**"
            
            lines.append(result_line)
        
        if len(parsed.rolls) > 1:
            lines.append(f"\n📊 **Итого: {parsed.grand_total}**")
        
        return "\n".join(lines)
    
    @classmethod
    def roll_initiative(cls, modifier: int = 0) -> DiceRoll:
        """Roll initiative (d20 + modifier)"""
        return cls._perform_roll(1, 20, modifier, RollType.NORMAL)
    
    @classmethod
    def roll_death_save(cls) -> DiceRoll:
        """Roll death saving throw (flat d20)"""
        return cls._perform_roll(1, 20, 0, RollType.NORMAL)
    
    @classmethod
    def roll_hp(cls, hit_die: int, constitution_mod: int = 0, 
                level: int = 1) -> int:
        """Roll hit points for a character"""
        total = hit_die + constitution_mod  # Max at level 1
        for _ in range(level - 1):
            roll = random.randint(1, hit_die)
            total += max(roll, 1) + constitution_mod
        return total


# ═══════════════════════════════════════════════════════════════
# GM ROLL SYSTEM — Hidden rolls for NPCs and world
# ═══════════════════════════════════════════════════════════════

# Pattern to find [GM ROLL: XdY+Z] in text
GM_ROLL_PATTERN = re.compile(
    r"\[GM\s*ROLL:\s*(\d*)d(\d+)(?:\s*([+\-])\s*(\d+))?\s*\]",
    re.IGNORECASE,
)


def parse_gm_rolls(text: str) -> Tuple[str, Dict[str, DiceRoll]]:
    """
    Find and execute all [GM ROLL: ...] patterns in text.
    Returns (text_with_results, dict of roll_id -> result).
    
    The original [GM ROLL] tags are replaced with narrative placeholders
    so the DM can describe results without exposing numbers to players.
    
    Example:
        Input:  "The orc swings [GM ROLL: d20+5]"
        Output: ("The orc swings {gm_roll_0}", {"gm_roll_0": DiceRoll(...)})
    """
    roll_results: Dict[str, DiceRoll] = {}
    counter = 0
    
    def replacer(match):
        nonlocal counter
        count = int(match.group(1)) if match.group(1) else 1
        sides = int(match.group(2))
        modifier = 0
        if match.group(3) and match.group(4):
            mod_val = int(match.group(4))
            modifier = mod_val if match.group(3) == '+' else -mod_val
        
        roll_id = f"gm_roll_{counter}"
        counter += 1
        
        roll = DiceParser._perform_roll(count, sides, modifier, RollType.NORMAL)
        roll_results[roll_id] = roll
        
        # Return placeholder with hidden result info (for DM's eyes only in logs)
        nat = roll.natural_roll
        total = roll.total
        crit = " [CRIT!]" if roll.is_critical_success else " [FUMBLE!]" if roll.is_critical_failure else ""
        return f"{{GM:{roll_id}:nat{nat}:tot{total}{crit}}}"
    
    processed = GM_ROLL_PATTERN.sub(replacer, text)
    return processed, roll_results


def resolve_gm_rolls(text: str, roll_results: Dict[str, DiceRoll]) -> str:
    """
    Replace GM roll placeholders with narrative descriptions.
    The actual numbers are logged but NOT shown to players.
    
    This creates descriptions like:
        "The orc swings — you feel the wind as its blade narrowly misses your throat."
    Instead of:
        "The orc rolled 18, +5 = 23, misses your AC 18."
    """
    import re as re_local
    
    # Pattern: {GM:gm_roll_0:nat15:tot20}
    placeholder_pattern = re_local.compile(
        r"\{GM:(gm_roll_\d+):nat(\d+):tot(\d+)(?:\s*\[([^\]]+)\])?\}"
    )
    
    def narrative_replacer(match):
        roll_id = match.group(1)
        natural = int(match.group(2))
        total = int(match.group(3))
        crit = match.group(4) or ""
        
        roll = roll_results.get(roll_id)
        if not roll:
            return "[roll resolved]"
        
        # Build narrative based on roll quality and crit status
        # These are templates the DM will flesh out in narration
        if roll.is_critical_success:
            return "[GM: devastating strike]"
        elif roll.is_critical_failure:
            return "[GM: humiliating fumble]"
        elif total >= 20:
            return "[GM: solid hit]"
        elif total >= 15:
            return "[GM: glancing blow]"
        elif total >= 10:
            return "[GM: near miss]"
        else:
            return "[GM: pathetic failure]"
    
    return placeholder_pattern.sub(narrative_replacer, text)


def format_gm_roll_log(roll_results: Dict[str, DiceRoll]) -> str:
    """Format GM roll results for logging (DM eyes only)."""
    if not roll_results:
        return ""
    lines = ["🔒 GM ROLLS (hidden from players):"]
    for roll_id, roll in roll_results.items():
        notation = f"{roll.count}d{roll.sides}"
        if roll.modifier > 0:
            notation += f"+{roll.modifier}"
        elif roll.modifier < 0:
            notation += f"{roll.modifier}"
        
        details = f"nat{roll.natural_roll} = {roll.total}"
        if roll.is_critical_success:
            details += " 💥 CRIT"
        elif roll.is_critical_failure:
            details += " 💀 FUMBLE"
        
        lines.append(f"   {roll_id}: {notation} → {details}")
    return "\n".join(lines)


# Global dice parser instance
dice = DiceParser()
