"""Клиент внешней базы упражнений (hasaneyldrm/exercises-dataset).

Индекс скачивается с GitHub по закреплённому коммиту и держится в памяти, из него оставляем
только нужное (английское название, русские шаги, ссылки на GIF). Кэш на диске — по SHA,
поэтому он не устаревает. Если GitHub недоступен и кэша нет, бот работает без базы
(картинки из free-exercise-db и наши подсказки по технике остаются).
"""
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from bot import sources

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DbExercise:
    id: str
    name: str
    equipment: str
    muscle: str
    part: str
    secondary: tuple[str, ...]
    steps_ru: tuple[str, ...]
    steps_en: tuple[str, ...]
    gif: str | None
    image: str | None


def _steps(rec: dict, lang: str) -> tuple[str, ...]:
    steps = (rec.get("instruction_steps") or {}).get(lang)
    if isinstance(steps, list) and steps:
        return tuple(str(s).strip() for s in steps if str(s).strip())
    text = (rec.get("instructions") or {}).get(lang)
    if isinstance(text, str) and text.strip():
        parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text) if p.strip()]
        return tuple(parts)
    return ()


def _compact(rec: dict) -> dict:
    """Урезаем запись до нужных полей, чтобы кэш был маленьким."""
    return {
        "id": str(rec["id"]),
        "name": str(rec.get("name", "")).replace("в°", "°").replace("В°", "°"),
        "equipment": rec.get("equipment", ""),
        "muscle": rec.get("muscle_group") or rec.get("target") or "",
        "part": rec.get("body_part") or rec.get("category") or "",
        "secondary": list(rec.get("secondary_muscles") or []),
        "steps_ru": list(_steps(rec, "ru")),
        "steps_en": list(_steps(rec, "en")),
        "gif": rec.get("gif_url") or None,
        "image": rec.get("image") or None,
    }


class ExerciseDB:
    def __init__(self, cache_dir: Path | None = None):
        self.cache_dir = cache_dir
        self.items: dict[str, DbExercise] = {}

    @property
    def ready(self) -> bool:
        return bool(self.items)

    def _cache_file(self) -> Path | None:
        if not self.cache_dir:
            return None
        return self.cache_dir / f"hasan-{sources.HASAN_REF[:12]}-v2.json"

    async def load(self) -> None:
        compact = self._read_cache()
        if compact is None:
            compact = await self._fetch()
            if compact is not None:
                self._write_cache(compact)
        if compact is None:
            log.warning("База упражнений недоступна, работаем без GIF и русских инструкций из базы")
            return
        self.items = {c["id"]: self._to_obj(c) for c in compact}
        log.info("База упражнений: %d записей", len(self.items))

    @staticmethod
    def _to_obj(c: dict) -> DbExercise:
        return DbExercise(
            id=c["id"], name=c["name"], equipment=c["equipment"], muscle=c["muscle"],
            part=c["part"], secondary=tuple(c["secondary"]), steps_ru=tuple(c["steps_ru"]),
            steps_en=tuple(c["steps_en"]), gif=c["gif"], image=c["image"],
        )

    def _read_cache(self) -> list[dict] | None:
        f = self._cache_file()
        if f and f.exists():
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                log.warning("Кэш базы упражнений повреждён, скачаю заново")
        return None

    def _write_cache(self, compact: list[dict]) -> None:
        f = self._cache_file()
        if not f:
            return
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(compact, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            log.warning("Не удалось записать кэш: %r", e)

    async def _fetch(self) -> list[dict] | None:
        url = sources.hasan_index_url()
        try:
            timeout = aiohttp.ClientTimeout(total=90)
            async with aiohttp.ClientSession(timeout=timeout) as s, s.get(url) as r:
                r.raise_for_status()
                raw = json.loads(await r.read())
            return [_compact(rec) for rec in raw if "id" in rec]
        except Exception as e:  # noqa: BLE001 — сеть/формат: деградируем, но не падаем
            log.warning("Не удалось загрузить базу упражнений %s: %r", url, e)
            return None

    # ---------- доступ ----------
    def get(self, ex_id: str | None) -> DbExercise | None:
        return self.items.get(ex_id) if ex_id else None

    def gif_url(self, ex_id: str | None) -> str | None:
        e = self.get(ex_id)
        return sources.hasan_media_url(e.gif) if e and e.gif else None

    def image_url(self, ex_id: str | None) -> str | None:
        e = self.get(ex_id)
        return sources.hasan_media_url(e.image) if e and e.image else None

    def search(self, query: str, limit: int = 12) -> list[DbExercise]:
        tokens = [t for t in re.split(r"\W+", query.lower()) if t]
        if not tokens:
            return []
        hits = []
        for e in self.items.values():
            hay = f"{e.name} {e.equipment} {e.part}".lower()
            if all(t in hay for t in tokens):
                hits.append(e)
        hits.sort(key=lambda e: (len(e.name), e.name))
        return hits[:limit]

    def parts(self) -> list[str]:
        return sorted({e.part for e in self.items.values() if e.part})

    def by_part(self, part: str, limit: int = 24) -> list[DbExercise]:
        hits = [e for e in self.items.values() if e.part == part]
        hits.sort(key=lambda e: (len(e.name), e.name))
        return hits[:limit]
