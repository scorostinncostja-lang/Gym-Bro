import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, available_timezones

WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
WEEKDAYS_FULL = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

TZ_PRESETS = [
    ("Москва (UTC+3)", "Europe/Moscow"),
    ("Киев (UTC+2/+3)", "Europe/Kyiv"),
    ("Минск (UTC+3)", "Europe/Minsk"),
    ("Екатеринбург (UTC+5)", "Asia/Yekaterinburg"),
    ("Алматы / Астана (UTC+5)", "Asia/Almaty"),
    ("Ташкент (UTC+5)", "Asia/Tashkent"),
    ("Новосибирск (UTC+7)", "Asia/Novosibirsk"),
    ("Владивосток (UTC+10)", "Asia/Vladivostok"),
]

_OFFSET_RE = re.compile(r"^(?:utc|gmt)?\s*([+-]?\d{1,2})(?::?(\d{2}))?$", re.I)
_TIME_RE = re.compile(r"^(\d{1,2})(?:[:.\s]?(\d{2}))?$")


def parse_tz(text: str) -> str | None:
    """«Europe/Moscow», «+5», «UTC-3» -> имя IANA-зоны или None."""
    t = text.strip()
    if t in available_timezones():
        return t
    m = _OFFSET_RE.match(t)
    if not m:
        return None
    hours, minutes = int(m.group(1)), int(m.group(2) or 0)
    if minutes or not -12 <= hours <= 14:
        return None
    if hours == 0:
        return "UTC"
    # В зонах Etc знак инвертирован: UTC+3 = Etc/GMT-3.
    return f"Etc/GMT{'-' if hours > 0 else '+'}{abs(hours)}"


def parse_time(text: str) -> str | None:
    """«18», «18:30», «1830», «18.30» -> «18:30»."""
    m = _TIME_RE.match(text.strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return f"{h:02d}:{mi:02d}"


def local_now(tz: str) -> datetime:
    return datetime.now(ZoneInfo(tz))


def local_today(tz: str) -> date:
    return local_now(tz).date()


def next_training(days: list[int], train_time: str, tz: str, now: datetime | None = None) -> datetime | None:
    """Ближайшая плановая тренировка после «сейчас»."""
    if not days:
        return None
    now = now or local_now(tz)
    h, m = map(int, train_time.split(":"))
    for offset in range(0, 8):
        d = (now + timedelta(days=offset)).date()
        if d.weekday() in days:
            dt = datetime(d.year, d.month, d.day, h, m, tzinfo=ZoneInfo(tz))
            if dt > now:
                return dt
    return None


def fmt_days(days: list[int]) -> str:
    return ", ".join(WEEKDAYS_RU[d] for d in sorted(days))


def parse_days(s: str | None) -> list[int]:
    return [int(x) for x in s.split(",")] if s else []


def pick_extra_days(strength: list[int], n: int) -> list[int]:
    """Дни для кардио или гибкости: как можно дальше от силовых и друг от друга."""
    free = [d for d in range(7) if d not in strength]
    chosen: list[int] = []
    for _ in range(min(n, len(free))):
        taken = [*strength, *chosen]
        best = max(free, key=lambda d: (min(min((d - x) % 7, (x - d) % 7) for x in taken) if taken else 3, -d))
        chosen.append(best)
        free.remove(best)
    return sorted(chosen)


def shift_time(hhmm: str, minutes: int) -> str:
    h, m = map(int, hhmm.split(":"))
    total = (h * 60 + m + minutes) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"
