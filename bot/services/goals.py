"""Цели тренировок: от них зависят диапазоны повторов, кардио и гибкость."""
import re

from bot.data import goals_data

ADULT_AGE = 18


def goal_keys(age: int | None = None) -> list[str]:
    return [k for k, g in goals_data().items() if not (g.get("adult_only") and (age or 0) < ADULT_AGE)]


def goal_name(key: str | None) -> str:
    g = goals_data().get(key or "")
    return g["name"] if g else "—"


def goal_about(key: str | None) -> str:
    g = goals_data().get(key or "")
    return g["about"] if g else ""


def parse_goals(text: str, age: int | None = None) -> list[str]:
    """«Хочу рельеф и выносливость» -> ["definition", "endurance"] в порядке упоминания."""
    t = text.lower().replace("ё", "е")
    hits: list[tuple[int, str]] = []
    for key in goal_keys(age):
        m = re.search(goals_data()[key]["words"].replace("ё", "е"), t)
        if m:
            hits.append((m.start(), key))
    return [k for _, k in sorted(hits)]


def rep_range(goal: str | None, kind: str, default: tuple[int, int], age: int | None) -> tuple[int, int]:
    """Диапазон повторов под цель. Время и кардио не трогаем."""
    g = goals_data().get(goal or "")
    if not g or kind not in ("weight", "reps"):
        return default
    lo, hi = g["reps"][kind]
    if goal == "strength" and kind == "weight" and age is not None and age < ADULT_AGE:
        lo = 6  # подросткам без тяжёлых низкоповторных подходов
    return lo, hi


def finisher_minutes(goal: str | None, goal2: str | None = None) -> int:
    return max((goals_data().get(g or "", {}).get("finisher", 0) for g in (goal, goal2)), default=0)


def extra_days(goal: str | None, goal2: str | None = None) -> int:
    """Рекомендуемое число дополнительных дней: кардио или гибкость."""
    first = goals_data().get(goal or "", {}).get("extra", 0)
    second = goals_data().get(goal2 or "", {}).get("extra", 0)
    return min(3, max(first, (second + 1) // 2))


def protein_per_kg(goal: str | None) -> float:
    return goals_data().get(goal or "", {}).get("protein", 1.4)


def extra_kind(goal: str | None) -> str:
    """Что делаем в дополнительные дни: гибкость, если цель гибкость, иначе кардио."""
    return "flex" if goal == "flex" else "cardio"
