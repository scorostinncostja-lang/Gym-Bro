"""Подбор тренировки по шаблону, инвентарю и цели. Без ИИ: только правила и данные."""
from dataclasses import asdict, dataclass

from bot.data import Exercise, exercises, families, flex_template, program_templates
from bot.services import cardio, goals
from bot.services.equipment import single_dumbbell, tags

# Если подходит несколько упражнений на паттерн, сначала берём то, что даёт больше нагрузки.
EQUIP_PRIORITY = ["machine", "barbell", "dumbbell", "kettlebell", "pullup_bar", "bars", "band", "bodyweight"]
# Сколько недель держим один набор упражнений: хватает, чтобы веса успели вырасти, и не надоедает.
BLOCK_WEEKS = 4


@dataclass
class PlanItem:
    ex_id: str
    sets: int
    lo: int
    hi: int
    kind: str
    step: float
    uni: bool = False  # каждая сторона отдельно: подход = правая + левая

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


def is_unilateral(ex: Exercise, user: dict | None = None, place: str | None = None) -> bool:
    """Делается по одной стороне: так задумано или дома всего одна гантель."""
    if ex.uni:
        return True
    return bool(user and place and ex.equip == "dumbbell" and not ex.two_hand and single_dumbbell(user, place))


def make_item(
    ex: Exercise, sets: int, goal_list: list[str] | str | None, age: int | None, compound_index: int | None = None,
    uni: bool | None = None,
) -> PlanItem:
    lo, hi = goals.rep_range(goal_list, ex.kind, ex.range, age, ex.pattern, compound_index)
    if ex.kind in ("time", "cardio"):
        sets = min(sets, 3) if ex.kind == "time" else 1
    return PlanItem(ex.id, sets, lo, hi, ex.kind, ex.step, ex.uni if uni is None else uni)


def cardio_item(user: dict, place: str, minutes: int | None = None, index: int = 0) -> PlanItem | None:
    """Кардио-блок: полноценная сессия (minutes=None) или финишер после силовой."""
    ex = cardio.pick(user, place, index)
    if not ex:
        return None
    lo_default, hi = ex.range
    if minutes is None:
        return PlanItem(ex.id, 1, cardio.start_minutes(user.get("level")), hi, "cardio", 1)
    return PlanItem(ex.id, 1, minutes, max(minutes + 10, hi // 2), "cardio", 1)


def _pick(
    patterns: list[str], allowed: set[str], chosen: set[str], avoid: set[str], level: str | None, variant: int,
) -> Exercise | None:
    """Упражнение на слот. Сначала то, чего нет в других днях цикла (avoid), потом повтор.

    variant сдвигает выбор среди равноценных вариантов: так каждые BLOCK_WEEKS недель набор меняется.
    Равноценные — того же типа, что лучший кандидат: упражнение с весом не меняем на «без веса»,
    лучше повторить его из другого дня.
    """
    fallback: list[Exercise] = []
    kind = None
    for pattern in patterns:
        cands = _candidates(pattern, allowed, chosen, level)
        if not cands:
            continue
        kind = kind or cands[0].kind
        pool = [e for e in cands if e.kind == kind]
        fresh = [e for e in pool if e.id not in avoid]
        if fresh:
            return fresh[variant % len(fresh)]
        fallback = fallback or pool
    return fallback[variant % len(fallback)] if fallback else None


def build_day(
    template: dict, user: dict, place: str | None = None, finisher: bool = True,
    avoid: set[str] | None = None, variant: int = 0, day_index: int = 0,
) -> list[PlanItem]:
    """Заполняет слоты шаблона упражнениями под инвентарь, уровень и цель."""
    place = place or user["place"]
    allowed = tags(user, place)
    level, age = user.get("level"), user.get("age")
    goal_list = goals.user_goals(user)
    chosen: set[str] = set()
    plan: list[PlanItem] = []
    compounds = 0
    for slot in template["slots"]:
        ex = _pick([slot["p"], *slot.get("fb", [])], allowed, chosen, avoid or set(), level, variant)
        if not ex:
            continue
        chosen.add(ex.id)
        idx = compounds if ex.pattern in goals.COMPOUND else None
        plan.append(make_item(ex, slot["sets"], goal_list, age, idx, is_unilateral(ex, user, place)))
        if idx is not None:
            compounds += 1
    if finisher:
        minutes = goals.finisher_minutes(goal_list)
        if minutes:  # день гибкости собирается с finisher=False
            fin = cardio_item(user, place, minutes, day_index + variant)
            if fin:
                plan.append(fin)
    return plan


def block_of(user: dict) -> int:
    """Номер блока: каждые BLOCK_WEEKS недель тренировок упражнения меняются на равноценные."""
    per_week = max(int(user.get("days_per_week") or 3), 1)
    return int(user.get("rotation_idx") or 0) // (per_week * BLOCK_WEEKS)


def build_cycle(user: dict, place: str | None = None, block: int | None = None) -> list[tuple[str, list[PlanItem]]]:
    """Все силовые дни цикла (A, B, C...). Дни собираются по порядку, чтобы не повторять упражнения."""
    block = block_of(user) if block is None else block
    used: set[str] = set()
    out: list[tuple[str, list[PlanItem]]] = []
    for n, tpl in enumerate(program_templates(user.get("days_per_week") or 3)):
        items = build_day(tpl, user, place, avoid=used, variant=block, day_index=n)
        used |= {i.ex_id for i in items if i.kind != "cardio"}
        out.append((tpl["name"], items))
    return out


def planned_day(user: dict, place: str | None = None) -> tuple[str, list[PlanItem]]:
    """Силовой день, который идёт по кругу следующим."""
    cycle = build_cycle(user, place)
    return cycle[int(user.get("rotation_idx") or 0) % len(cycle)]


def build_extra_day(user: dict, place: str | None = None, index: int = 0) -> tuple[str, list[PlanItem]]:
    """Дополнительный день: гибкость, если цель гибкость, иначе кардио."""
    place = place or user["place"]
    if goals.extra_kind(goals.user_goals(user)) == "flex":
        tpl = flex_template()
        return tpl["name"], build_day(tpl, user, place, finisher=False)
    item = cardio_item(user, place, None, index)
    return "Кардио", [item] if item else []


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
