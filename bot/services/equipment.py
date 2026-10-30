"""Инвентарь пользователя: что есть дома, какие веса, чем ограничена программа.

Хранится в users.equipment как JSON: {"items": [{"id": "dumbbell", "count": 1, "weights": [5]}, ...],
"other": ["свободный текст"]}. Правила парсинга без ИИ покрывают типовые фразы, ИИ подключается,
когда текст нестандартный.
"""
import json
import re

from bot.data import EQUIPMENT_BY_PLACE, equipment_catalog

_CATALOG = {c["id"]: c for c in equipment_catalog()}
_NUM = r"\d+(?:[.,]\d+)?"
_FILLER = re.compile(
    r"\b(?:у меня|есть|имеется|еще|ещё|также|тоже|дома|вот|только|и|а|плюс|ну|вроде|такое|такой)\b|[.!?]"
)
_ADJUSTABLE = re.compile(r"разборн|наборн|сборн|регулируем")


def empty_gear() -> dict:
    return {"items": [], "other": []}


def load_gear(user: dict | None) -> dict:
    raw = (user or {}).get("equipment")
    if not raw:
        return empty_gear()
    try:
        data = json.loads(raw)
    except ValueError:
        return empty_gear()
    data.setdefault("items", [])
    data.setdefault("other", [])
    return data


def dump_gear(gear: dict) -> str:
    return json.dumps(gear, ensure_ascii=False)


def _find_item_id(part: str) -> str | None:
    for item in equipment_catalog():  # порядок каталога: велотренажёр раньше велосипеда и дорожки
        if re.search(item["kw"], part):
            return item["id"]
    return None


def _numbers(text: str) -> list[float]:
    return [float(x.replace(",", ".")) for x in re.findall(_NUM, text)]


def _parse_part(part: str, item_id: str) -> dict:
    cat = _CATALOG[item_id]
    seg = part
    out: dict = {"id": item_id}
    count = None
    m = re.search(rf"({_NUM})\s*(?:штук\w*|шт\b|гантел\w*|гир\w*|резин\w*|эспандер\w*)", seg)
    if m and float(m.group(1)) <= 10 and "кг" not in seg[m.end(1):m.end(1) + 5]:
        count = int(float(m.group(1)))
        seg = seg[:m.start(1)] + " " + seg[m.end(1):]
    if count is None:
        if re.search(r"\b(?:одн[аоуы]\w*|1)\b", seg) and not re.search(r"\b1\s*кг", seg):
            count = 1
        elif re.search(r"\b(?:две|двух|пара|пару)\b", seg):
            count = 2
    nums = [n for n in _numbers(seg) if 0 < n <= 300]
    if cat.get("weights") or cat.get("resist"):
        if _ADJUSTABLE.search(seg) or re.search(r"\bдо\s+" + _NUM, seg):
            m2 = re.search(rf"от\s+({_NUM})\s*до\s+({_NUM})", seg)
            if m2:
                lo, hi = float(m2.group(1).replace(",", ".")), float(m2.group(2).replace(",", "."))
            else:
                hi = max(nums) if nums else 20.0
                lo = 2.0 if hi > 4 else 1.0
            out.update(adjustable=True, min=lo, max=hi, step=2.5 if hi > 20 else 1.0)
        elif nums:
            out["weights"] = sorted({*nums})
    if count:
        out["count"] = count
    return out


def parse_gear_text(text: str) -> tuple[list[dict], list[str]]:
    """Свободный текст -> (предметы, непонятое). «Одна гантель 5 кг, турник, резинка 25 кг»."""
    t = text.lower().replace("ё", "е")
    items: dict[str, dict] = {}
    other: list[str] = []
    for clause in re.split(r"[;\n,]+", t):
        parts = re.split(r"\s+(?:и|а также|плюс|\+)\s+", clause.strip())
        merged: list[str] = []
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if merged and _find_item_id(part) is None and re.search(_NUM, part):
                merged[-1] += " " + part  # «гантели 5 и 10 кг»: число относится к прошлому предмету
            else:
                merged.append(part)
        for part in merged:
            item_id = _find_item_id(part)
            if item_id:
                items[item_id] = _parse_part(part, item_id)
                continue
            rest = _FILLER.sub(" ", part).strip()
            if len(rest) >= 3:
                other.append(rest)
    return list(items.values()), other


def merge_gear(gear: dict, new_items: list[dict], other: list[str] | None = None, replace: bool = False) -> dict:
    items = {} if replace else {i["id"]: i for i in gear["items"]}
    for i in new_items:
        items[i["id"]] = i
    notes = list(dict.fromkeys([*(gear.get("other") or []), *(other or [])]))
    return {"items": list(items.values()), "other": notes[:10]}


def item_label(i: dict) -> str:
    cat = _CATALOG.get(i["id"], {"name": i["id"]})
    parts = []
    if i.get("count"):
        parts.append(f"{i['count']} шт")
    if i.get("adjustable"):
        parts.append(f"разборные {i['min']:g}–{i['max']:g} кг")
    elif i.get("weights"):
        unit = "кг (сопротивление)" if cat.get("resist") else "кг"
        parts.append(", ".join(f"{w:g}" for w in i["weights"]) + f" {unit}")
    return cat["name"] + (": " + ", ".join(parts) if parts else "")


def render_gear(gear: dict) -> str:
    lines = [f"• {item_label(i)}" for i in gear["items"]]
    lines += [f"• {o} (запомнил, в программе учесть не могу)" for o in gear.get("other", [])]
    return "\n".join(lines) if lines else "Пока ничего не указано."


def tags(user: dict, place: str) -> set[str]:
    """Теги инвентаря, доступные в этом месте."""
    base = set(EQUIPMENT_BY_PLACE[place])
    gear = load_gear(user)
    if place == "home_db" and (gear["items"] or user.get("equipment") is not None):
        base = {"bodyweight", "run", "walk"}  # указан реальный инвентарь: вместо «гантели по умолчанию»
        base |= {_CATALOG[i["id"]]["tag"] for i in gear["items"] if i["id"] in _CATALOG}
        if "barbell" in base:
            base.discard("barbell")  # штанги в программе нет: оставляем как заметку
    elif place == "street":
        portable = {"bike", "rope", "band", "kettlebell"}
        base |= {_CATALOG[i["id"]]["tag"] for i in gear["items"] if i["id"] in _CATALOG} & portable
    return base


def ladder(user: dict, place: str, equip: str) -> list[float] | None:
    """Доступные веса гантелей или гири дома. None = ограничений нет (зал, тренажёры)."""
    if place != "home_db" or equip not in ("dumbbell", "kettlebell"):
        return None
    for i in load_gear(user)["items"]:
        if i["id"] != equip:
            continue
        if i.get("adjustable"):
            lo, hi, step = i["min"], i["max"], i.get("step", 1.0)
            out, w = [], lo
            while w <= hi + 1e-9:
                out.append(round(w, 2))
                w += step
            return out
        if i.get("weights"):
            return sorted(i["weights"])
    return None


def snap(weight: float, ladder_: list[float] | None) -> float:
    """Ближайший доступный вес."""
    if not ladder_:
        return weight
    return min(ladder_, key=lambda x: (abs(x - weight), x))


def single_dumbbell(user: dict, place: str) -> bool:
    if place != "home_db":
        return False
    return any(i["id"] == "dumbbell" and i.get("count") == 1 for i in load_gear(user)["items"])
