"""Места тренировок: у человека их может быть несколько (зал, дом с гантелями, дом без инвентаря)."""
from bot.data import PLACES

ORDER = ["gym", "home_db", "street", "home_bw"]  # приоритет основного места
NAME = {"gym": "зал", "home_db": "дома, с инвентарём", "street": "улица", "home_bw": "дома, без инвентаря"}
SHORT = {"gym": "🏋️ В зал", "home_db": "🏠 Дома с инвентарём", "street": "🌳 На улицу", "home_bw": "🤸 Без инвентаря"}


def user_places(user: dict) -> list[str]:
    """Все доступные места пользователя в порядке приоритета."""
    raw = (user.get("places") or user.get("place") or "").split(",")
    have = {p for p in raw if p in PLACES}
    out = [p for p in ORDER if p in have]
    return out or ["gym"]


def primary_place(places: list[str]) -> str:
    """Основное место: зал, если он есть, иначе дом с гантелями, иначе без инвентаря."""
    for p in ORDER:
        if p in places:
            return p
    return "gym"


def places_label(places: list[str]) -> str:
    return " + ".join(PLACES[p] for p in places)
