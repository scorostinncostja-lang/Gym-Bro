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


CAP_EXTRA = 8  # на сколько повторов можно выйти за верх диапазона, когда веса больше нет


def _up(w: float, step: float, ladder: list[float] | None) -> float | None:
    """Следующий вес вверх. None = в твоём инвентаре тяжелее нет."""
    if ladder:
        higher = [x for x in ladder if x > w + 1e-9]
        return min(higher) if higher else None
    return w + step


def _down(w: float, step: float, ladder: list[float] | None) -> float:
    if ladder:
        lower = [x for x in ladder if x < w - 1e-9]
        return max(lower) if lower else w
    return max(step, w - step)


CAP_NOTE = (
    "Это самый тяжёлый вес, который у тебя есть. Добавляй повторы или опускай вес медленно, 3 секунды."
)


def next_target(
    last: list[SetResult], lo: int, hi: int, kind: str, step: float, start_weight: float | None = None,
    ladder: list[float] | None = None,
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
        if kind == "cardio":
            return Target(0, lo, f"Первый раз: {lo} мин в лёгком темпе. Главное закончить без одышки.")
        return Target(0, lo, "Первый раз: делай чисто и без спешки.")

    weight = max(s.weight for s in last)
    min_reps = min(s.reps for s in last)
    avg_feel = sum(s.feel for s in last) / len(last)
    any_max = any(s.feel == Feel.MAX for s in last)

    if kind == "cardio":
        minutes = max(s.reps for s in last)
        if avg_feel >= 3.5:
            return Target(0, max(lo, minutes - 2), "Прошлый раз было на пределе: на 2 минуты меньше и спокойнее.")
        bump = 3 if avg_feel <= 2.0 else 2 if avg_feel <= 2.5 else 0
        new = min(hi, minutes + bump)
        note = f"Прибавляем: {new} мин." if new > minutes else "Закрепи этот объём ещё раз."
        return Target(0, max(lo, new), note)

    if kind == "weight":
        if min_reps < lo - 1:
            new_w = _down(weight, step, ladder)
            return Target(new_w, lo, f"Прошлый раз было мало повторов, снижаем вес до {_fmt(new_w)} кг.")
        if min_reps >= hi and not any_max and avg_feel <= 2.5:
            up = _up(weight, step, ladder)
            if up is None:
                return Target(weight, min(hi + CAP_EXTRA, min_reps + 1), CAP_NOTE)
            return Target(up, lo, f"Все подходы по {hi} и было не на пределе: пора прибавить, {_fmt(up)} кг.")
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


def set_hint(
    res: SetResult, lo: int, hi: int, kind: str, step: float, calibrating: bool = False,
    ladder: list[float] | None = None,
) -> Hint:
    """Подсказка после подхода: что менять в следующем подходе.

    calibrating: упражнение выполняется впервые, стартовый вес был оценкой, поэтому «легко»
    сразу добавляет вес, не дожидаясь верха диапазона.
    """
    w = res.weight if kind == "weight" else None
    if kind == "cardio":
        if res.feel == Feel.EASY:
            return Hint("Легко. В следующий раз добавим 3 минуты. Сейчас 3–5 минут ходьбы и растяжка.", None, res.reps)
        if res.feel in (Feel.HARD, Feel.MAX):
            return Hint("Темп был высоким. В следующий раз тот же объём, но медленнее. 3–5 минут ходьбы и растяжка.", None, res.reps)
        return Hint("Хорошая работа. 3–5 минут ходьбы, чтобы пульс упал, и растяжка.", None, res.reps)
    if res.feel == Feel.MAX:
        return Hint(
            "Это был предел. Отдых 2–3 минуты, вес не добавляем. "
            "Боль в суставе или спине (не жжение в мышце) — сразу стоп.",
            w, max(lo, res.reps - 1),
        )
    if kind == "weight" and res.reps < lo - 1:
        new_w = _down(res.weight, step, ladder)
        return Hint(f"Повторов мало. В следующем подходе снизь вес до {_fmt(new_w)} кг.", new_w, lo)
    if res.feel == Feel.EASY:
        if kind == "weight" and (calibrating or res.reps >= hi):
            up = _up(res.weight, step, ladder)
            if up is None:
                return Hint(CAP_NOTE, res.weight, min(hi + CAP_EXTRA, res.reps + 1))
            why = "Для старта слишком легко." if calibrating else "Легко и повторов выше нормы."
            return Hint(f"{why} В следующем подходе {_fmt(up)} кг.", up, lo)
        nxt = res.reps + (5 if kind == "time" else 1)
        return Hint(f"Легко. Сделай ещё {'на 5 секунд дольше' if kind == 'time' else 'на 1 повтор больше'} в следующем подходе.", w, nxt)
    if res.feel == Feel.OK:
        return Hint("Хороший рабочий подход. Отдых 60–90 секунд, повтори то же самое.", w, res.reps)
    return Hint("Тяжело, это нормально для последних повторов. Отдых 90 секунд, держи тот же вес.", w, res.reps)
