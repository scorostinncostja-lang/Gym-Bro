"""Профиль пользователя для ИИ: что известно о человеке и текущей тренировке."""
from bot.data import PLACES, get_exercise
from bot.db.database import Database
from bot.services.places import places_label, user_places


async def build_profile(db: Database, user: dict) -> dict:
    profile = {
        "age": user["age"], "height_cm": user["height_cm"], "weight_kg": user["weight_kg"],
        "goal": user["goal"], "level": user["level"],
        "place": places_label(user_places(user)) if user.get("place") in PLACES else user.get("place"),
        "days_per_week": user["days_per_week"], "streak": await db.streak(user["tg_id"]),
    }
    active = await db.active_workout(user["tg_id"])
    if active:
        item = active["plan"][active["ex_i"]] if active["ex_i"] < len(active["plan"]) else None
        ex = get_exercise(item["ex_id"]) if item else None
        profile["last_workout"] = f"идёт сейчас: {active['day_name']}" + (f", упражнение «{ex.name}»" if ex else "")
    return profile
