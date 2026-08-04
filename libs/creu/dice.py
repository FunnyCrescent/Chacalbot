import random
import logging

logger = logging.getLogger(__name__)


def roll_d6() -> int:
    return random.randint(1, 6)


def roll_4d6_drop_lowest() -> int:
    rolls = sorted([roll_d6() for _ in range(4)])
    total = sum(rolls[1:])
    logger.debug(f"4d6 drop lowest: {rolls} -> {total}")
    return total


def roll_all_stats() -> list[int]:
    stats = [roll_4d6_drop_lowest() for _ in range(6)]
    logger.info(f"Бросок характеристик: {stats} (сумма: {sum(stats)})")
    return stats


def modifier(score: int) -> int:
    return (score - 10) // 2
