"""Справочник добавок для подростков. Источник один: data/supplements.json (поле teen).

Дозы выдаёт код, а не модель: ИИ не придумывает числа, он только пересказывает справочник.
"""
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
    "preworkout": r"предтрен|пре-?воркаут|pre-?workout",
    "fatburner": r"жиросжигат|fat\s*burner",
    "testobooster": r"тестобуст|бустер\s+тестостерон",
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


def find(text: str) -> list[dict]:
    """Добавки, упомянутые в тексте (в порядке справочника)."""
    found = []
    for sid, s in supplements().items():
        pattern = _KEYWORDS.get(sid)
        if pattern and re.search(pattern, text, re.I):
            found.append(s)
    return found


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
