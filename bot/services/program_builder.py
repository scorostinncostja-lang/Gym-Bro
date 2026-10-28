"""Подбор тренировки по шаблону. Без ИИ: только правила и данные."""
from dataclasses import asdict, dataclass

from bot.data import EQUIPMENT_BY_PLACE, Exercise, exercises, families, program_templates


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


def _candidates(pattern: str, place: str, exclude: set[str]) -> list[Exercise]:
    allowed = EQUIPMENT_BY_PLACE[place]
    return [
        e for e in exercises().values()
        if e.pattern == pattern and e.equip in allowed and e.id not in exclude
    ]


def _item(ex: Exercise, sets: int) -> PlanItem:
    lo, hi = ex.range
    # Планку и подобные упражнения на время держим на 3 подходах максимум.
    if ex.kind == "time":
        sets = min(sets, 3)
    return PlanItem(ex.id, sets, lo, hi, ex.kind, ex.step)


def build_day(template: dict, place: str) -> list[PlanItem]:
    """Заполняет слоты шаблона упражнениями под доступный инвентарь."""
    chosen: set[str] = set()
    plan: list[PlanItem] = []
    for slot in template["slots"]:
        for pattern in [slot["p"], *slot.get("fb", [])]:
            cands = _candidates(pattern, place, chosen)
            if cands:
                ex = cands[0]
                chosen.add(ex.id)
                plan.append(_item(ex, slot["sets"]))
                break
    return plan


def day_count(days_per_week: int) -> int:
    return len(program_templates(days_per_week))


def template_for(days_per_week: int, rotation_idx: int) -> dict:
    tpls = program_templates(days_per_week)
    return tpls[rotation_idx % len(tpls)]


def alternatives(ex_id: str, place: str, exclude: set[str]) -> list[Exercise]:
    """Замены для упражнения: то же семейство движений, доступное в этом месте."""
    base = exercises()[ex_id]
    family = next((f for f in families() if base.pattern in f), [base.pattern])
    out: list[Exercise] = []
    for pattern in family:
        out.extend(_candidates(pattern, place, exclude | {ex_id}))
    return out
