"""Загрузка статических данных: упражнения, шаблоны программ, добавки."""
import json
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import quote_plus

from bot import sources
from bot.config import DATA_DIR

PLACES = {
    "gym": "🏋️ Зал",
    "home_db": "🏠 Дома с инвентарём",
    "street": "🌳 Улица, площадка",
    "home_bw": "🤸 Дома без инвентаря",
}

# Базовые теги инвентаря по местам. Дома с инвентарём добавляется то, что пользователь указал
# (см. bot/services/equipment.py). Бег и ходьба доступны везде.
_OUTDOOR = {"run", "walk"}
EQUIPMENT_BY_PLACE = {
    "gym": {"machine", "dumbbell", "bodyweight", "barbell", "kettlebell", "pullup_bar", "band",
            "treadmill", "bike_stationary", "elliptical", "rope"} | _OUTDOOR,
    "home_db": {"dumbbell", "bodyweight"} | _OUTDOOR,
    "street": {"bodyweight", "pullup_bar", "bars"} | _OUTDOOR,
    "home_bw": {"bodyweight"} | _OUTDOOR,
}

UNITS = {"weight": "кг", "reps": "повт.", "time": "сек", "cardio": "мин"}


@dataclass(frozen=True)
class Exercise:
    id: str
    name: str
    pattern: str
    equip: str
    kind: str  # weight | reps | time
    step: float
    fedb: str
    hasan: str | None
    cues: tuple[str, ...]
    range: tuple[int, int]
    min_level: str = "new"  # "some": только тем, кто уже занимается
    uni: bool = False  # по одной стороне: тяга одной рукой, мостик на одной ноге, растяжки «меняй ногу»
    two_hand: bool = False  # одна гантель держится двумя руками (гоблет, французский жим): с одной гантелью не по сторонам

    @property
    def images(self) -> list[str]:
        """Запасные картинки (старт/финиш), если нет GIF из hasaneyldrm."""
        return sources.fedb_image_urls(self.fedb) if self.fedb else []

    @property
    def video_url(self) -> str:
        q = quote_plus(f"{self.name} техника выполнения")
        return f"https://www.youtube.com/results?search_query={q}"


@lru_cache
def _programs() -> dict:
    return json.loads((DATA_DIR / "programs.json").read_text(encoding="utf-8"))


@lru_cache
def exercises() -> dict[str, Exercise]:
    raw = json.loads((DATA_DIR / "exercises.json").read_text(encoding="utf-8"))
    ranges = _programs()["default_ranges"]
    out: dict[str, Exercise] = {}
    for e in raw:
        rng = tuple(e.get("range") or ranges[e["kind"]])
        out[e["id"]] = Exercise(
            id=e["id"], name=e["name"], pattern=e["pattern"], equip=e["equip"],
            kind=e["kind"], step=float(e["step"]), fedb=e.get("fedb", ""), hasan=e.get("hasan"),
            cues=tuple(e["cues"]), range=rng, min_level=e.get("min_level", "new"),  # type: ignore[arg-type]
            uni=bool(e.get("uni")), two_hand=bool(e.get("two_hand")),
        )
    return out


def get_exercise(ex_id: str) -> Exercise | None:
    return exercises().get(ex_id)


def find_exercises(query: str) -> list[Exercise]:
    q = query.lower().strip()
    return [e for e in exercises().values() if q in e.name.lower()]


def program_templates(days_per_week: int) -> list[dict]:
    p = _programs()
    key = str(min(max(days_per_week or 3, 2), 4))  # вне 2–4 (старые записи) берём ближайший сплит
    name, n = p["splits"][key]
    return p["templates"][name][:n]


def families() -> list[list[str]]:
    return _programs()["families"]


@lru_cache
def supplements() -> dict[str, dict]:
    raw = json.loads((DATA_DIR / "supplements.json").read_text(encoding="utf-8"))
    return {s["id"]: s for s in raw}


def _load(name: str):
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


@lru_cache
def equipment_catalog() -> list[dict]:
    return _load("equipment.json")


@lru_cache
def goals_data() -> dict[str, dict]:
    return {k: v for k, v in _load("goals.json").items() if not k.startswith("_")}


@lru_cache
def warmup_data() -> dict:
    return _load("warmup.json")


@lru_cache
def shops_data() -> dict:
    return _load("shops.json")


def flex_template() -> dict:
    return _programs()["templates"]["flex"][0]


def is_extra_day(day_name: str) -> bool:
    """Доп. день (кардио или гибкость): не двигает круг силовых дней A → B → C."""
    return day_name in ("Кардио", flex_template()["name"])
