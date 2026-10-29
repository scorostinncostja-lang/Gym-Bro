"""Цели тренировок: от них зависят диапазоны повторов, кардио и гибкость.

Целей может быть сколько угодно, порядок = приоритет. Программа смешанная: при «силе» базовые
упражнения идут в силовом диапазоне, остальные — под следующую цель (объём, рельеф, выносливость).
"""
import re

from bot.data import goals_data

ADULT_AGE = 18
COMPOUND = {"squat", "hinge", "push_h", "push_v", "pull_v", "pull_h", "lunge"}
# Какая цель задаёт повторы во вспомогательных упражнениях, если целей несколько.
_ACCESSORY_PRIORITY = ["muscle", "definition", "endurance", "lose", "fit", "flex", "strength"]


def _as_list(goals: list[str] | str | None, extra: str | None = None) -> list[str]:
    if goals is None:
        out: list[str] = []
    elif isinstance(goals, str):
        out = [g for g in goals.split(",") if g]
    else:
        out = [g for g in goals if g]
    if extra:
        out.append(extra)
    return [g for g in dict.fromkeys(out) if g in goals_data()]


def user_goals(user: dict | None) -> list[str]:
    """Все цели пользователя по приоритету. Старые профили: goal и goal2."""
    u = user or {}
    found = _as_list(u.get("goals")) or _as_list(u.get("goal"), u.get("goal2"))
    return found or ["fit"]


def goal_keys(age: int | None = None) -> list[str]:
    return [k for k, g in goals_data().items() if not (g.get("adult_only") and (age or 0) < ADULT_AGE)]


def goal_name(key: str | None) -> str:
    g = goals_data().get(key or "")
    return g["name"] if g else "—"


def goals_label(goals: list[str] | str | None) -> str:
    return " + ".join(goal_name(g) for g in _as_list(goals)) or "—"


def goal_about(key: str | None) -> str:
    g = goals_data().get(key or "")
    return g["about"] if g else ""


def parse_goals(text: str, age: int | None = None) -> list[str]:
    """«Сила, объём, выносливость, рельеф» -> все цели в порядке упоминания."""
    t = text.lower().replace("ё", "е")
    hits: list[tuple[int, str]] = []
    for key in goal_keys(age):
        m = re.search(goals_data()[key]["words"].replace("ё", "е"), t)
        if m:
            hits.append((m.start(), key))
    return [k for _, k in sorted(hits)]


def _range_of(goal: str, kind: str, age: int | None) -> tuple[int, int]:
    lo, hi = goals_data()[goal]["reps"][kind]
    if goal == "strength" and kind == "weight" and age is not None and age < ADULT_AGE:
        lo = 6  # подросткам без тяжёлых низкоповторных подходов
    return lo, hi


def rep_range(
    goals: list[str] | str | None, kind: str, default: tuple[int, int], age: int | None,
    pattern: str | None = None, compound_index: int | None = None,
) -> tuple[int, int]:
    """Диапазон повторов. compound_index — номер базового упражнения в дне (0, 1, ...)."""
    gl = _as_list(goals)
    if not gl or kind not in ("weight", "reps"):
        return default
    if "strength" in gl and (len(gl) == 1 or (pattern in COMPOUND and compound_index is not None and compound_index < 2)):
        return _range_of("strength", kind, age)
    main = next((g for g in _ACCESSORY_PRIORITY if g in gl and g != "strength"), gl[0])
    return _range_of(main, kind, age)


def finisher_minutes(goals: list[str] | str | None, goal2: str | None = None) -> int:
    return max((goals_data()[g].get("finisher", 0) for g in _as_list(goals, goal2)), default=0)


def extra_days(goals: list[str] | str | None, goal2: str | None = None) -> int:
    """Рекомендуемое число дополнительных дней: кардио или гибкость."""
    gl = _as_list(goals, goal2)
    if not gl:
        return 0
    first = goals_data()[gl[0]].get("extra", 0)
    others = max((goals_data()[g].get("extra", 0) for g in gl[1:]), default=0)
    return min(3, max(first, (others + 1) // 2))


def protein_per_kg(goals: list[str] | str | None) -> float:
    return max((goals_data()[g].get("protein", 1.4) for g in _as_list(goals)), default=1.4)


def extra_kind(goals: list[str] | str | None) -> str:
    """Что делаем в дополнительные дни: гибкость, если она главная цель, иначе кардио."""
    gl = _as_list(goals)
    return "flex" if gl and gl[0] == "flex" else "cardio"


def wants_flex(goals: list[str] | str | None) -> bool:
    return "flex" in _as_list(goals)


def conflict_note(goals: list[str] | str | None, age: int | None) -> str:
    """Честно о том, как цели мешают друг другу и как я их совмещаю."""
    gl = set(_as_list(goals))
    notes = []
    if "strength" in gl and len(gl) > 1:
        notes.append("Базовые упражнения (присед, жимы, тяги) делаем в силовом диапазоне, остальные под вторую цель.")
    if {"strength", "muscle"} & gl and "endurance" in gl:
        notes.append("Кардио ставлю отдельными днями и в конце тренировки: длинный бег перед силовой съедает силу.")
    if "definition" in gl and "muscle" in gl:
        notes.append(
            "Объём и рельеф одновременно у новичка растут первые месяцы. Дальше рельеф решает питание."
            + (" Тебе до 18: без дефицита калорий, только тренировки, сон и нормальная еда." if (age or 0) < ADULT_AGE else "")
        )
    if len(gl) >= 3:
        notes.append("Первые 3–6 месяцев прогресс будет по всем целям сразу. Потом выбери главную на 2–3 месяца.")
    return " ".join(notes)


_REST = {"strength": 150, "muscle": 90, "definition": 60, "endurance": 45, "lose": 60, "fit": 75, "flex": 45}


def rest_seconds(goals: list[str] | str | None, kind: str, pattern: str | None, feel: int | None) -> int:
    """Отдых до следующего подхода: силовой диапазон дольше, рельеф и выносливость короче."""
    gl = _as_list(goals) or ["fit"]
    if kind == "time":
        base = 30
    elif "strength" in gl and pattern in COMPOUND and kind == "weight":
        base = _REST["strength"]
    else:
        main = next((g for g in _ACCESSORY_PRIORITY if g in gl and g != "strength"), gl[0])
        base = _REST.get(main, 90)
    if feel == 4:  # на пределе
        base += 60
    elif feel == 3:
        base += 30
    return base
