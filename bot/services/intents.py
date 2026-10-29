"""Команды обычным текстом в чате: «напоминай в 19:30», «тренируюсь по пн, ср, пт», «у меня есть креатин».

Только правила, без ИИ: изменение настроек не должно зависеть от настроения модели.
"""
import re
from dataclasses import dataclass

from bot.services.supp_advice import find
from bot.services.timeutil import parse_time

_DAY_ABBR = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6}
_DAY_STEMS = [("понедельник", 0), ("вторник", 1), ("сред", 2), ("четверг", 3), ("пятниц", 4),
              ("суббот", 5), ("воскресень", 6)]

_CHANGE_VERB = re.compile(
    r"напоминай|напомни|поставь|поменяй|измени|смени|перенеси|переставь|переключи|давай|хочу|буду|теперь|сделай|тренируй|тренируюсь|тренироваться"
)
_TOPIC = re.compile(r"напомина|тренир|занима|расписан|время|по\s+(?:пн|вт|ср|чт|пт|сб|вс)|раз\w*\s+в\s+недел")
_PER_WEEK = re.compile(r"\b([2-4])\s*раз\w*\s+в\s+недел")

_OWN_WORDS = re.compile(r"у\s+меня\s+(?:есть|ещё|еще)|\bесть\b|купил|взял|заказал|принимаю|\bпью\b|начал\s+пить")
_DOSE_WORDS = re.compile(
    r"доз|сколько|как\s+(?:принимать|пить|употреблять)|когда\s+(?:принимать|пить)|план\s+приём|как\s+мне\s+(?:брать|принимать)"
)


@dataclass(frozen=True)
class ScheduleChange:
    time: str | None = None
    days: list[int] | None = None
    per_week: int | None = None


def parse_days_words(text: str) -> list[int]:
    found: set[int] = set()
    for tok in re.findall(r"[а-яё]+", text.lower()):
        if tok in _DAY_ABBR:
            found.add(_DAY_ABBR[tok])
            continue
        for stem, d in _DAY_STEMS:
            if tok.startswith(stem):
                found.add(d)
    return sorted(found)


def parse_time_phrase(text: str) -> str | None:
    t = text.lower()
    m = re.search(r"(?<!\d)(\d{1,2})[:.](\d{2})(?!\d)", t)
    if m:
        return parse_time(f"{m.group(1)}:{m.group(2)}")
    m = re.search(r"\bв\s+(\d{1,2})(?:\s*(утра|дня|вечера|ночи|час\w*|ч\b))?(?![\d:])", t)
    if not m:
        return None
    h, suffix = int(m.group(1)), m.group(2) or ""
    if suffix in ("дня", "вечера") and h < 12:
        h += 12
    return parse_time(f"{h}:00")


def parse_schedule_change(text: str) -> ScheduleChange | None:
    """Распознаёт просьбу поменять время или дни тренировок. Вопросы и длинные тексты не трогает."""
    t = text.lower().strip()
    if "?" in t or len(t) > 120:
        return None
    if not (_CHANGE_VERB.search(t) and _TOPIC.search(t)):
        return None
    time = parse_time_phrase(t)
    days = parse_days_words(t) or None
    m = _PER_WEEK.search(t)
    per_week = int(m.group(1)) if m else None
    if not (time or days or per_week):
        return None
    return ScheduleChange(time, days, per_week)


def parse_supp_intent(text: str) -> tuple[list[str], bool] | None:
    """«у меня есть креатин» -> (id, True: сохранить в набор); «сколько креатина пить» -> (id, False)."""
    t = text.lower()
    if len(t) > 200:
        return None
    mentioned = [s["id"] for s in find(t)]
    if not mentioned:
        return None
    own = bool(_OWN_WORDS.search(t))
    if own or _DOSE_WORDS.search(t):
        return mentioned, own
    return None
