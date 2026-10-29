"""Город, страна и часовой пояс из текста пользователя: «Алматы», «Москва», «+5», «Europe/Berlin»."""
import re
from dataclasses import dataclass

from bot.services.timeutil import parse_tz

# (регулярка по названию, IANA-зона, страна). Крупные города СНГ; остальное пользователь указывает смещением.
_CITIES: list[tuple[str, str, str]] = [
    (r"москв|подмосков", "Europe/Moscow", "RU"), (r"санкт|питер|спб|петербург", "Europe/Moscow", "RU"),
    (r"казан|нижн\w+ новгород|воронеж|ростов|краснодар|сочи|волгоград|самар|саратов|тула|ярославл|белгород", "Europe/Moscow", "RU"),
    (r"екатеринбург|челябинск|тюмен|пермь|уфа|курган|оренбург", "Asia/Yekaterinburg", "RU"),
    (r"омск", "Asia/Omsk", "RU"), (r"новосибирск|томск|кемерово|барнаул", "Asia/Novosibirsk", "RU"),
    (r"красноярск", "Asia/Krasnoyarsk", "RU"), (r"иркутск|улан-удэ", "Asia/Irkutsk", "RU"),
    (r"якутск", "Asia/Yakutsk", "RU"), (r"владивосток|хабаровск", "Asia/Vladivostok", "RU"),
    (r"калининград", "Europe/Kaliningrad", "RU"), (r"самара", "Europe/Samara", "RU"),
    (r"алмат|алма-ат", "Asia/Almaty", "KZ"), (r"астан|нур-султан|акмол", "Asia/Almaty", "KZ"),
    (r"шымкент|караганд|тараз|семей|павлодар|костанай|кокшетау|усть-каменогорск|петропавловск|талдыкорган|кызылорд|туркестан", "Asia/Almaty", "KZ"),
    (r"актобе|актау|атырау|уральск|орал", "Asia/Aqtobe", "KZ"),
    (r"минск|гомел|брест|гродно|витебск|могил", "Europe/Minsk", "BY"),
    (r"киев|киев|харьков|одесс|днепр|львов|запорож", "Europe/Kyiv", "UA"),
    (r"ташкент|самарканд|бухар|андижан|наманган|фергана", "Asia/Tashkent", "UZ"),
    (r"бишкек|ош\b", "Asia/Bishkek", "KG"), (r"душанбе|худжанд", "Asia/Dushanbe", "TJ"),
    (r"ашхабад", "Asia/Ashgabat", "TM"), (r"баку|гянджа", "Asia/Baku", "AZ"),
    (r"ереван|гюмри", "Asia/Yerevan", "AM"), (r"тбилиси|батуми", "Asia/Tbilisi", "GE"),
    (r"кишин[её]в|бельцы", "Europe/Chisinau", "MD"),
]
_COUNTRY_WORDS = [
    (r"росси|рф\b", "RU"), (r"казахстан|кз\b", "KZ"), (r"беларус|белорус", "BY"), (r"украин", "UA"),
    (r"узбекистан", "UZ"), (r"киргиз|кыргыз", "KG"), (r"таджикистан", "TJ"), (r"армени", "AM"),
    (r"грузи", "GE"), (r"азербайджан", "AZ"), (r"молдов", "MD"),
]
_TZ_COUNTRY = {
    "Europe/Moscow": "RU", "Asia/Yekaterinburg": "RU", "Asia/Omsk": "RU", "Asia/Novosibirsk": "RU",
    "Asia/Krasnoyarsk": "RU", "Asia/Irkutsk": "RU", "Asia/Yakutsk": "RU", "Asia/Vladivostok": "RU",
    "Europe/Kaliningrad": "RU", "Europe/Samara": "RU",
    "Asia/Almaty": "KZ", "Asia/Aqtobe": "KZ", "Asia/Qyzylorda": "KZ", "Europe/Minsk": "BY",
    "Europe/Kyiv": "UA", "Europe/Kiev": "UA", "Asia/Tashkent": "UZ", "Asia/Bishkek": "KG",
    "Asia/Dushanbe": "TJ", "Asia/Yerevan": "AM", "Asia/Tbilisi": "GE", "Asia/Baku": "AZ",
}
COUNTRY_NAME = {"RU": "Россия", "KZ": "Казахстан", "BY": "Беларусь", "UA": "Украина", "UZ": "Узбекистан",
                "KG": "Кыргызстан", "TJ": "Таджикистан", "TM": "Туркменистан", "AM": "Армения",
                "GE": "Грузия", "AZ": "Азербайджан", "MD": "Молдова"}


_COUNTRY_TZ = {"RU": "Europe/Moscow", "KZ": "Asia/Almaty", "BY": "Europe/Minsk", "UA": "Europe/Kyiv",
               "UZ": "Asia/Tashkent", "KG": "Asia/Bishkek", "TJ": "Asia/Dushanbe", "AM": "Asia/Yerevan",
               "GE": "Asia/Tbilisi", "AZ": "Asia/Baku", "MD": "Europe/Chisinau"}


@dataclass(frozen=True)
class Location:
    tz: str
    country: str | None
    city: str | None
    tz_known: bool = True  # False: назвали только страну, пояс взят по умолчанию


def parse_location(text: str) -> Location | None:
    """Город, часовой пояс или смещение. None, если ничего не понято."""
    t = text.strip()
    if not t:
        return None
    low = t.lower().replace("ё", "е")
    country = next((c for pat, c in _COUNTRY_WORDS if re.search(pat, low)), None)
    for pat, tz, c in _CITIES:
        if re.search(pat, low):
            city = re.sub(r"[^\w\s-]", "", t.split(",")[0]).strip().title()[:40]
            return Location(tz, country or c, city)
    tz = parse_tz(t) or parse_tz(re.sub(r"[^\w:+/-]", "", t))
    if not tz:
        m = re.search(r"(?<![\w])(?:utc|gmt)?\s*([+-]\d{1,2})(?![\d:])", low)
        tz = parse_tz(m.group(1)) if m else None
    if tz:
        return Location(tz, country or _TZ_COUNTRY.get(tz), None)
    if country in _COUNTRY_TZ:
        return Location(_COUNTRY_TZ[country], country, None, tz_known=False)
    return None


def country_of(user: dict) -> str:
    """Страна пользователя для подбора магазинов: из профиля, иначе по часовому поясу."""
    return user.get("country") or _TZ_COUNTRY.get(user.get("tz") or "", "") or "DEFAULT"
