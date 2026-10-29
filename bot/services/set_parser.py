"""Разбор фраз вида «20 на 12», «20x12 тяжело», «12 повторов» в подход."""
import re
from dataclasses import dataclass

from bot.services.progression import Feel

# Порядок важен: сначала более сильные формулировки.
_FEEL_PATTERNS: list[tuple[Feel, re.Pattern]] = [
    (Feel.EASY, re.compile(r"\bне\s+(?:тяжело|трудно|сложно)|\b(?:очень\s+)?легко|\bизи\b")),
    (Feel.MAX, re.compile(r"на\s+пределе|предел|до\s+отказа|отказ|\bеле\b")),
    (Feel.HARD, re.compile(r"тяжело|трудно|сложно|тяжёл")),
    (Feel.OK, re.compile(r"нормально|\bнорм\b|средне|\bок\b|окей")),
]

# Боль в суставе или спине: сразу стоп. Мышечное жжение сюда не входит.
_PAIN = re.compile(
    r"\bболит|\bболь\b|болев|\bноет|\bныл[оаи]?\b|прострел|хруст|щёлк|щелк|\bстрельн|онемел|немеет|"
    r"защемил|потянул|круж\w+\s+голов|тошнит|темнеет\s+в\s+глазах"
)

_NUM = r"\d+(?:[.,]\d+)?"
_WEIGHT_RE = re.compile(rf"({_NUM})\s*(?:кг|kg)\b")
_REPS_RE = re.compile(r"(\d+)\s*(?:раз|повтор\w*|повт\b|р\b)")
_SEC_RE = re.compile(r"(\d+)\s*(?:сек\w*|с\b|s\b)")
_ANY_NUM = re.compile(_NUM)
_MIN_RE = re.compile(r"(\d+)\s*(?:мин\w*|м\b|minutes?)")
_KM_RE = re.compile(rf"({_NUM})\s*(?:км|km|километр\w*)")


@dataclass(frozen=True)
class ParsedSet:
    weight: float
    reps: int
    feel: Feel | None


def detect_feel(text: str) -> Feel | None:
    t = text.lower()
    for feel, pat in _FEEL_PATTERNS:
        if pat.search(t):
            return feel
    return None


def mentions_pain(text: str) -> bool:
    return bool(_PAIN.search(text.lower()))


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def parse_set(text: str, kind: str, default_weight: float | None = None) -> ParsedSet | None:
    feel = detect_feel(text)
    t = text.lower()
    max_reps = 600 if kind == "time" else 300 if kind == "cardio" else 100

    if kind == "cardio":
        # «25 мин 4 км»: минуты в reps, километры в weight (0, если не названы).
        km = _KM_RE.search(t)
        distance = _num(km.group(1)) if km else 0.0
        if km:
            t = t[: km.start()] + " " + t[km.end():]
        m = _MIN_RE.search(t)
        minutes = int(m.group(1)) if m else None
        if minutes is None:
            nums = _ANY_NUM.findall(t)
            minutes = int(_num(nums[0])) if nums else None
        if minutes is None or not (1 <= minutes <= max_reps):
            return None
        return ParsedSet(distance, minutes, feel)

    weight: float | None = None
    reps: int | None = None

    m = _WEIGHT_RE.search(t)
    if m:
        weight = _num(m.group(1))
        t = t[: m.start()] + " " + t[m.end():]
    m = (_SEC_RE if kind == "time" else _REPS_RE).search(t)
    if m:
        reps = int(m.group(1))
        t = t[: m.start()] + " " + t[m.end():]

    nums = [_num(x) for x in _ANY_NUM.findall(t)]

    if kind == "weight":
        if weight is None and reps is None:
            if len(nums) >= 2:
                weight, reps = nums[0], int(nums[1])
            elif len(nums) == 1 and default_weight is not None:
                weight, reps = default_weight, int(nums[0])
        elif weight is None and reps is not None:
            if nums:
                weight = nums[0]
            elif default_weight is not None:
                weight = default_weight
        elif weight is not None and reps is None:
            if nums:
                reps = int(nums[0])
    else:
        weight = 0.0
        if reps is None and nums:
            reps = int(nums[0])

    if weight is None or reps is None:
        return None
    if not (1 <= reps <= max_reps) or not (0 <= weight <= 500):
        return None
    return ParsedSet(float(weight), int(reps), feel)
