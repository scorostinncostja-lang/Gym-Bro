"""Дозы добавок для подростков. Источник один: data/supplements.json (поле teen).

Дозы выдаёт код, а не модель: ИИ не придумывает числа, он только пересказывает справочник.
"""
import html
import re

from bot.data import supplements

TEEN_FOOTER = "Скажи родителям. Я не врач, это справка. Все добавки: /supplements"

_KEYWORDS = {
    "creatine": r"креатин|creatine",
    "whey": r"протеин|изолят|сывороточ|whey|protein",
    "omega3": r"омега|рыбий\s+жир|omega",
    "vitd": r"витамин\s*д\b|витамин\s*d\b|vitamin\s*d|\bд3\b|\bd3\b",
    "multivit": r"мультивит|витамины",
    "caffeine": r"кофеин|caffeine",
    "bcaa": r"bcaa|бцаа|аминокислот",
    "glutamine": r"глютамин|glutamine",
    "betaalanine": r"бета-?аланин",
    "citrulline": r"цитруллин",
    "zma": r"\bzma\b|зма|цинк|магни",
    "gainer": r"гейнер",
    "collagen": r"коллаген",
    "hmb": r"\bhmb\b|гмб",
    "electrolytes": r"электролит",
}


def teen_ok(s: dict) -> bool:
    return bool(s["teen"]["ok"])


def dose_for(s: dict, adult: bool) -> str | None:
    """Дозировка для возраста или None, если добавка этому возрасту не рекомендуется."""
    if adult:
        return s["dose"]
    return s["teen"]["dose"] if teen_ok(s) else None


def can_stack(s: dict, adult: bool) -> bool:
    """Можно ли добавить в личный набор: работает/условно, а подросткам ещё и teen.ok."""
    return s["verdict"] in ("works", "conditional") and (adult or teen_ok(s))


def find(text: str) -> list[dict]:
    """Добавки, упомянутые в тексте (в порядке справочника, не больше трёх)."""
    found = []
    for sid, s in supplements().items():
        pattern = _KEYWORDS.get(sid)
        if pattern and re.search(pattern, text, re.I):
            found.append(s)
    return found[:3]


def teen_block(s: dict) -> str:
    name = html.escape(s["name"])
    t = s["teen"]
    if t["ok"]:
        return f"<b>{name}</b>\n{html.escape(t['dose'])}\n<i>{html.escape(t['note'])}</i>"
    return f"<b>{name}</b>: {html.escape(t['note'])}"


def dose_reply(text: str) -> str:
    """Ответ подростку на вопрос про дозы: из справочника, без участия модели."""
    mentioned = find(text)
    if not mentioned:
        ok = [s for s in supplements().values() if teen_ok(s)]
        names = ", ".join(html.escape(s["name"].split(" (")[0].lower()) for s in ok)
        return (
            "Из добавок для твоего возраста имеет смысл только базовое: " + names + ". "
            "Всё остальное (стимуляторы, предтренировочные, жиросжигатели, тестобустеры, BCAA) — мусор или "
            "риск. Спроси про конкретную добавку, например «сколько креатина?», и я дам малую дозу.\n\n"
            + TEEN_FOOTER
        )
    return "Для твоего возраста (до 18) дозы маленькие и без «загрузок»:\n\n" + "\n\n".join(
        teen_block(s) for s in mentioned
    ) + "\n\n" + TEEN_FOOTER


def teen_reference() -> str:
    """Справочник для системного промпта: единственный источник доз для подростков."""
    lines = []
    for s in supplements().values():
        t = s["teen"]
        if t["ok"]:
            lines.append(f"- {s['name']}: {t['dose']} {t['note']}")
        else:
            lines.append(f"- {s['name']}: не рекомендовать. {t['note']}")
    return "\n".join(lines)
