"""Двойная прогрессия: сначала добавляем повторы, потом вес.

Все решения принимает код по правилам. ИИ сюда не заходит, чтобы не было
галлюцинаций с весами.
"""
from dataclasses import dataclass
from enum import IntEnum


class Feel(IntEnum):
    EASY = 1  # осталось 3+ повтора в запасе
    OK = 2  # осталось 2
    HARD = 3  # остался 1
    MAX = 4  # на пределе, 0 в запасе


FEEL_LABELS = {
    Feel.EASY: "😌 Легко",
    Feel.OK: "👌 Нормально",
    Feel.HARD: "😤 Тяжело",
    Feel.MAX: "🥵 На пределе",
}


@dataclass(frozen=True)
class SetResult:
    weight: float
    reps: int
    feel: Feel


@dataclass(frozen=True)
class Target:
    weight: float | None  # None = вес ещё не известен
    reps: int
    note: str


@dataclass(frozen=True)
class Hint:
    text: str
    next_weight: float | None
    next_reps: int


def _fmt(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


def next_target(
    last: list[SetResult], lo: int, hi: int, kind: str, step: float, start_weight: float | None = None,
) -> Target:
    """Цель на следующую тренировку по результатам прошлой."""
    if not last:
        if kind == "weight":
            if start_weight is not None:
                return Target(
                    start_weight, lo,
                    f"Первый раз. Стартовый вес {_fmt(start_weight)} кг — оценка по твоему весу тела. "
                    f"Делай {lo}–{hi} повторов и остановись, когда в запасе останется 2–3. "
                    "Слишком легко или тяжело — скажи после подхода, я подстрою вес.",
                )
            return Target(
                None, lo,
                f"Первый раз: подбери вес, с которым сделаешь {hi} повторов "
                f"и ещё 3 останутся в запасе. Лучше начни легко.",
            )
        return Target(0, lo, "Первый раз: делай чисто и без спешки.")

    weight = max(s.weight for s in last)
    min_reps = min(s.reps for s in last)
    avg_feel = sum(s.feel for s in last) / len(last)
    any_max = any(s.feel == Feel.MAX for s in last)

    if kind == "weight":
        if min_reps < lo - 1:
            new_w = max(step, weight - step)
            return Target(new_w, lo, f"Прошлый раз было мало повторов, снижаем вес до {_fmt(new_w)} кг.")
        if min_reps >= hi and not any_max and avg_feel <= 2.5:
            return Target(weight + step, lo, f"Все подходы по {hi} и было не на пределе: пора прибавить, {_fmt(weight + step)} кг.")
        if min_reps >= hi:
            return Target(weight, hi, "Диапазон закрыт, но было тяжело. Закрепи этот вес ещё раз.")
        reps = max(lo, min_reps) if any_max else min(hi, max(lo, min_reps + 1))
        return Target(weight, reps, "Тот же вес, цель — на повтор больше." if not any_max else "Тот же вес, без гонки за повторами.")

    # Упражнения без веса и на время: растим повторы/секунды.
    inc = 5 if kind == "time" else 1
    if min_reps >= hi:
        if avg_feel <= 2.5:
            return Target(0, hi, "Диапазон закрыт. Усложни: медленнее вниз (3 сек), пауза внизу или вариант сложнее.")
        return Target(0, hi, "Диапазон закрыт, но было тяжело. Закрепи.")
    reps = max(lo, min_reps) if any_max else min(hi, max(lo, min_reps + inc))
    return Target(0, reps, "Цель — чуть больше, чем в прошлый раз.")


def set_hint(res: SetResult, lo: int, hi: int, kind: str, step: float, calibrating: bool = False) -> Hint:
    """Подсказка после подхода: что менять в следующем подходе.

    calibrating: упражнение выполняется впервые, стартовый вес был оценкой, поэтому «легко»
    сразу добавляет вес, не дожидаясь верха диапазона.
    """
    w = res.weight if kind == "weight" else None
    if res.feel == Feel.MAX:
        return Hint(
            "Это был предел. Отдых 2–3 минуты, вес не добавляем. "
            "Боль в суставе или спине (не жжение в мышце) — сразу стоп.",
            w, max(lo, res.reps - 1),
        )
    if kind == "weight" and res.reps < lo - 1:
        new_w = max(step, res.weight - step)
        return Hint(f"Повторов мало. В следующем подходе снизь вес до {_fmt(new_w)} кг.", new_w, lo)
    if res.feel == Feel.EASY:
        if kind == "weight" and calibrating:
            return Hint(
                f"Для старта слишком легко. В следующем подходе +{_fmt(step)} кг: {_fmt(res.weight + step)} кг.",
                res.weight + step, lo,
            )
        if kind == "weight" and res.reps >= hi:
            return Hint(f"Легко и повторов выше нормы. В следующем подходе +{_fmt(step)} кг.", res.weight + step, lo)
        nxt = res.reps + (5 if kind == "time" else 1)
        return Hint(f"Легко. Сделай ещё {'на 5 секунд дольше' if kind == 'time' else 'на 1 повтор больше'} в следующем подходе.", w, nxt)
    if res.feel == Feel.OK:
        return Hint("Хороший рабочий подход. Отдых 60–90 секунд, повтори то же самое.", w, res.reps)
    return Hint("Тяжело, это нормально для последних повторов. Отдых 90 секунд, держи тот же вес.", w, res.reps)
