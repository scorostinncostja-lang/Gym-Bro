"""Внешние репозитории с упражнениями. Мы их не копируем: бот берёт данные по ссылкам.

Версии зафиксированы коммитами, чтобы идентификаторы и медиа не поменялись у нас под ногами.
Обновить: подставь новый SHA и запусти `uv run python -m bot.tools.catalog_check`.
"""

# GIF-анимации, фото и инструкции на 10 языках (есть русский).
# Данные и тексты: MIT. Медиа: © Gym visual (https://gymvisual.com/), см. NOTICE.md репозитория.
HASAN_REPO = "hasaneyldrm/exercises-dataset"
HASAN_REF = "7455efae41b330c265e7cd4b78dfa848e7ce5ebd"

# Картинки старт/финиш движения. Unlicense (общественное достояние).
FEDB_REPO = "yuhonas/free-exercise-db"
FEDB_REF = "f00c92c7dcf1216a928a52c3706c7ce8e2f71ed5"

ATTRIBUTION = "Анимации: © Gym visual (gymvisual.com), через hasaneyldrm/exercises-dataset"


def raw_url(repo: str, ref: str, path: str) -> str:
    return f"https://raw.githubusercontent.com/{repo}/{ref}/{path.lstrip('/')}"


def hasan_index_url() -> str:
    return raw_url(HASAN_REPO, HASAN_REF, "data/exercises.json")


def hasan_media_url(path: str) -> str:
    if path.startswith("http"):
        return path
    return raw_url(HASAN_REPO, HASAN_REF, path)


def fedb_image_urls(fedb_id: str) -> list[str]:
    return [raw_url(FEDB_REPO, FEDB_REF, f"exercises/{fedb_id}/{i}.jpg") for i in (0, 1)]
