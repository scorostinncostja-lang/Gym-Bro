"""Стартовый вес по весу тела, чтобы новичок не гадал «сколько брать».

Это оценка, а не истина: дальше вес подстраивает прогрессия по ответам «легко / тяжело».
"""
import json
from functools import lru_cache

from bot.config import DATA_DIR

TEEN_FACTOR = 0.85  # подросткам начинаем осторожнее
EXPERIENCED_FACTOR = 1.25  # «занимаюсь 3–12 месяцев»


@lru_cache
def _table() -> dict[str, dict]:
    raw = json.loads((DATA_DIR / "start_weights.json").read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def is_hand(ex_id: str) -> bool:
    """Вес указывается для каждой гантели, а не суммарно."""
    return bool(_table().get(ex_id, {}).get("hand"))


def start_weight(ex_id: str, body_kg: float | None, age: int | None, level: str | None, step: float) -> float | None:
    """Стартовый вес в кг, округлённый до шага тренажёра/гантелей. None, если оценить нечем."""
    rec = _table().get(ex_id)
    if not rec or not body_kg:
        return None
    w = rec["r"] * body_kg
    if age is not None and age < 18:
        w *= TEEN_FACTOR
    if level == "some":
        w *= EXPERIENCED_FACTOR
    step = step or 1.0
    w = round(w / step) * step
    return float(max(w, rec["min"]))
