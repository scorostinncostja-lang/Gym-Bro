"""Загрузка статических данных: упражнения, шаблоны программ, добавки."""
import json
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import quote_plus

from bot import sources
from bot.config import DATA_DIR

PLACES = {
    "gym": "🏋️ Зал",
    "home_db": "🏠 Дома с гантелями",
    "home_bw": "🤸 Дома без инвентаря",
}

# Какой инвентарь доступен в каждом месте.
EQUIPMENT_BY_PLACE = {
    "gym": {"machine", "dumbbell", "bodyweight"},
    "home_db": {"dumbbell", "bodyweight"},
    "home_bw": {"bodyweight"},
}

UNITS = {"weight": "кг", "reps": "повт.", "time": "сек"}


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

    @property
    def images(self) -> list[str]:
        """Запасные картинки (старт/финиш), если нет GIF из hasaneyldrm."""
        return sources.fedb_image_urls(self.fedb)

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
            kind=e["kind"], step=float(e["step"]), fedb=e["fedb"], hasan=e.get("hasan"),
            cues=tuple(e["cues"]), range=rng,  # type: ignore[arg-type]
        )
    return out


def get_exercise(ex_id: str) -> Exercise | None:
    return exercises().get(ex_id)


def find_exercises(query: str) -> list[Exercise]:
    q = query.lower().strip()
    return [e for e in exercises().values() if q in e.name.lower()]


def program_templates(days_per_week: int) -> list[dict]:
    p = _programs()
    split = p["splits"][str(days_per_week)]
    return p["templates"][split]


def families() -> list[list[str]]:
    return _programs()["families"]


@lru_cache
def supplements() -> dict[str, dict]:
    raw = json.loads((DATA_DIR / "supplements.json").read_text(encoding="utf-8"))
    return {s["id"]: s for s in raw}
