"""Подбор тренировки по шаблону, инвентарю и цели. Без ИИ: только правила и данные."""
from dataclasses import asdict, dataclass

from bot.data import Exercise, exercises, families, flex_template, program_templates
from bot.services import cardio, goals
from bot.services.equipment import tags

# Если подходит несколько упражнений на паттерн, сначала берём то, что даёт больше нагрузки.
EQUIP_PRIORITY = ["machine", "barbell", "dumbbell", "kettlebell", "pullup_bar", "bars", "band", "bodyweight"]


@dataclass
class PlanItem:
    ex_id: str
    sets: int
    lo: int
    hi: int
    kind: str
    step: float

    def to_dict(self) -> dict:
        return asdict(self)


def _prio(equip: str) -> int:
    return EQUIP_PRIORITY.index(equip) if equip in EQUIP_PRIORITY else len(EQUIP_PRIORITY)


def _candidates(pattern: str, allowed: set[str], exclude: set[str], level: str | None) -> list[Exercise]:
    out = [
        e for e in exercises().values()
        if e.pattern == pattern and e.equip in allowed and e.id not in exclude
        and (level == "some" or e.min_level == "new")
    ]
    return sorted(out, key=lambda e: _prio(e.equip))  # sorted стабилен: внутри приоритета порядок файла


def make_item(
    ex: Exercise, sets: int, goal_list: list[str] | str | None, age: int | None, compound_index: int | None = None,
) -> PlanItem:
    lo, hi = goals.rep_range(goal_list, ex.kind, ex.range, age, ex.pattern, compound_index)
    if ex.kind in ("time", "cardio"):
        sets = min(sets, 3) if ex.kind == "time" else 1
    return PlanItem(ex.id, sets, lo, hi, ex.kind, ex.step)


def cardio_item(user: dict, place: str, minutes: int | None = None, index: int = 0) -> PlanItem | None:
    """Кардио-блок: полноценная сессия (minutes=None) или финишер после силовой."""
    ex = cardio.pick(user, place, index)
    if not ex:
        return None
    lo_default, hi = ex.range
    if minutes is None:
        return PlanItem(ex.id, 1, cardio.start_minutes(user.get("level")), hi, "cardio", 1)
    return PlanItem(ex.id, 1, minutes, max(minutes + 10, hi // 2), "cardio", 1)


def build_day(template: dict, user: dict, place: str | None = None, finisher: bool = True) -> list[PlanItem]:
    """Заполняет слоты шаблона упражнениями под инвентарь, уровень и цель."""
    place = place or user["place"]
    allowed = tags(user, place)
    level, age = user.get("level"), user.get("age")
    goal_list = goals.user_goals(user)
    chosen: set[str] = set()
    plan: list[PlanItem] = []
    compounds = 0
    for slot in template["slots"]:
        for pattern in [slot["p"], *slot.get("fb", [])]:
            cands = _candidates(pattern, allowed, chosen, level)
            if cands:
                ex = cands[0]
                chosen.add(ex.id)
                idx = compounds if ex.pattern in goals.COMPOUND else None
                plan.append(make_item(ex, slot["sets"], goal_list, age, idx))
                if idx is not None:
                    compounds += 1
                break
    if finisher:
        minutes = goals.finisher_minutes(goal_list)
        if minutes and not any(i.kind == "time" for i in plan):  # у дня гибкости финишер не нужен
            fin = cardio_item(user, place, minutes)
            if fin:
                plan.append(fin)
    return plan


def build_extra_day(user: dict, place: str | None = None, index: int = 0) -> tuple[str, list[PlanItem]]:
    """Дополнительный день: гибкость, если цель гибкость, иначе кардио."""
    place = place or user["place"]
    if goals.extra_kind(goals.user_goals(user)) == "flex":
        tpl = flex_template()
        return tpl["name"], build_day(tpl, user, place, finisher=False)
    item = cardio_item(user, place, None, index)
    return "Кардио", [item] if item else []


def day_count(days_per_week: int) -> int:
    return len(program_templates(days_per_week))


def template_for(days_per_week: int, rotation_idx: int) -> dict:
    tpls = program_templates(days_per_week)
    return tpls[rotation_idx % len(tpls)]


def alternatives(ex_id: str, user: dict, place: str, exclude: set[str]) -> list[Exercise]:
    """Замены: то же семейство движений, доступное в этом месте."""
    base = exercises()[ex_id]
    family = next((f for f in families() if base.pattern in f), [base.pattern])
    allowed = tags(user, place)
    out: list[Exercise] = []
    for pattern in family:
        out.extend(_candidates(pattern, allowed, exclude | {ex_id}, user.get("level")))
    return out
