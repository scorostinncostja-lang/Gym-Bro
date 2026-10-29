"""Кардио: бег, дорожка, велосипед и велодорожки, велотренажёр, скакалка, ходьба.

Объём растёт по правилам (минуты), новичку бег даётся интервалами «бег / шаг». ИИ не участвует.
"""
from bot.data import Exercise, exercises
from bot.services.equipment import tags

# Порядок предпочтения видов кардио.
_GYM_ORDER = ["treadmill", "bike_stationary", "elliptical", "run_outdoor", "bike_outdoor", "jump_rope",
              "walk_incline", "walk_fast"]
_HOME_ORDER = ["bike_outdoor", "run_outdoor", "bike_stationary", "treadmill", "jump_rope", "elliptical",
               "walk_incline", "walk_fast"]

# Бег-ходьба для новичков: (бег, мин; ходьба, мин; повторов). Шаг вперёд после каждой выполненной сессии.
RUN_WALK = [(1, 2, 6), (1.5, 2, 6), (2, 2, 5), (3, 2, 4), (5, 2, 3), (8, 2, 2), (10, 1, 2), (15, 0, 1), (20, 0, 1)]
START_MINUTES = {"new": 15, "some": 20}


def available(user: dict, place: str) -> list[Exercise]:
    """Виды кардио, доступные в этом месте, по порядку предпочтения."""
    allowed = tags(user, place)
    order = _GYM_ORDER if place == "gym" else _HOME_ORDER
    mine = {e.id: e for e in exercises().values() if e.pattern == "cardio" and e.equip in allowed}
    return [mine[i] for i in order if i in mine]


def pick(user: dict, place: str, index: int = 0) -> Exercise | None:
    modes = available(user, place)
    return modes[index % len(modes)] if modes else None


def hr_zone(age: int) -> tuple[int, int]:
    """Пульс разговорной зоны: 60–70% от (220 − возраст)."""
    mx = 220 - age
    return round(mx * 0.6), round(mx * 0.7)


def run_walk_text(n_done: int) -> str:
    run, walk, rounds = RUN_WALK[min(n_done, len(RUN_WALK) - 1)]
    total = (run + walk) * rounds
    fmt = lambda x: f"{x:g}"  # noqa: E731
    if walk == 0:
        return f"Бегай непрерывно {fmt(run)} мин в разговорном темпе. Устал — перейди на шаг, потом снова."
    return (
        f"Бег-ходьба: {rounds} × (бег {fmt(run)} мин + шаг {fmt(walk)} мин), всего ≈ {fmt(total)} мин. "
        "Перед этим 5 минут ходьбы, после 3–5 минут шагом."
    )


def card_note(ex: Exercise, user: dict, n_done: int, level: str | None) -> str:
    """Что делать в кардио-сессии: протокол и пульсовая зона."""
    lo, hi = hr_zone(int(user.get("age") or 20))
    parts = []
    if ex.id in ("run_outdoor", "treadmill") and (level or "new") == "new":
        parts.append(run_walk_text(n_done))
    parts.append(f"Темп разговорный: пульс ≈ {lo}–{hi} уд/мин. Можешь говорить фразами, иначе замедлись.")
    if ex.id == "bike_outdoor":
        parts.append("Шлем обязателен. Велодорожка лучше дороги.")
    return " ".join(parts)


def start_minutes(level: str | None) -> int:
    return START_MINUTES.get(level or "new", 15)
