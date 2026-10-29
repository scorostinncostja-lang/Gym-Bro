"""SQLite-хранилище. Тонкая обёртка над aiosqlite без ORM."""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

STALE_WORKOUT_HOURS = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id INTEGER PRIMARY KEY,
    name TEXT,
    age INTEGER,
    height_cm REAL,
    weight_kg REAL,
    goal TEXT,
    level TEXT,
    place TEXT,                -- основное место (по нему строится программа)
    places TEXT,               -- все доступные места: "gym,home_db,home_bw"
    days_per_week INTEGER,
    days TEXT,                 -- weekday numbers, Mon=0: "0,2,4"
    train_time TEXT,           -- "18:00"
    tz TEXT,
    rotation_idx INTEGER NOT NULL DEFAULT 0,
    supp_time TEXT NOT NULL DEFAULT '09:00',
    supp_remind INTEGER NOT NULL DEFAULT 0,
    onboarded INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    kind TEXT NOT NULL,
    value REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_meas ON measurements(tg_id, kind, day);
CREATE TABLE IF NOT EXISTS workouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    day_name TEXT NOT NULL,
    place TEXT NOT NULL,
    plan TEXT NOT NULL,        -- JSON list of plan items
    ex_i INTEGER NOT NULL DEFAULT 0,
    set_i INTEGER NOT NULL DEFAULT 0,
    pend_set_id INTEGER,       -- подход, ждущий оценки ощущений
    cur_weight REAL,           -- рабочая подсказка для следующего подхода
    cur_reps INTEGER,
    status TEXT NOT NULL,      -- planned | active | done | skipped | missed
    nagged INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_workouts ON workouts(tg_id, day);
CREATE TABLE IF NOT EXISTS sets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workout_id INTEGER NOT NULL,
    tg_id INTEGER NOT NULL,
    ex_id TEXT NOT NULL,
    set_no INTEGER NOT NULL,
    weight REAL NOT NULL,
    reps INTEGER NOT NULL,
    feel INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sets ON sets(tg_id, ex_id, workout_id);
CREATE TABLE IF NOT EXISTS usage (
    tg_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    kind TEXT NOT NULL,
    n INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tg_id, day, kind)
);
CREATE TABLE IF NOT EXISTS stack (
    tg_id INTEGER NOT NULL,
    supp_id TEXT NOT NULL,
    PRIMARY KEY (tg_id, supp_id)
);
CREATE TABLE IF NOT EXISTS progress_media (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    kind TEXT NOT NULL,        -- photo | video_note | video
    label TEXT NOT NULL,       -- front | side | back | flex | clip
    file_id TEXT NOT NULL      -- файл лежит у Telegram, у нас только ссылка
);
CREATE INDEX IF NOT EXISTS ix_media ON progress_media(tg_id, day);
CREATE TABLE IF NOT EXISTS chat (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_chat ON chat(tg_id, id);
"""

USER_FIELDS = {
    "name", "age", "height_cm", "weight_kg", "goal", "level", "place", "places", "days_per_week",
    "days", "train_time", "tz", "rotation_idx", "supp_time", "supp_remind", "onboarded",
}
WORKOUT_FIELDS = {
    "ex_i", "set_i", "pend_set_id", "cur_weight", "cur_reps", "status", "nagged",
    "started_at", "finished_at", "plan", "place",
}

_TABLES_BY_USER = ("measurements", "workouts", "sets", "usage", "stack", "chat", "progress_media", "users")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._migrate()
        await self._db.commit()

    async def _migrate(self) -> None:
        """Старые базы: добавляем колонки, которых ещё не было."""
        async with self.db.execute("PRAGMA table_info(users)") as cur:
            cols = {r["name"] for r in await cur.fetchall()}
        if "places" not in cols:
            await self.db.execute("ALTER TABLE users ADD COLUMN places TEXT")
        await self.db.execute("UPDATE users SET places=place WHERE places IS NULL AND place IS NOT NULL")

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Database.connect() не вызван"
        return self._db

    async def _one(self, sql: str, args: tuple = ()) -> dict | None:
        async with self.db.execute(sql, args) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def _all(self, sql: str, args: tuple = ()) -> list[dict]:
        async with self.db.execute(sql, args) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def _exec(self, sql: str, args: tuple = ()) -> int:
        cur = await self.db.execute(sql, args)
        await self.db.commit()
        return cur.lastrowid or 0

    # ---------- users ----------
    async def get_user(self, tg_id: int) -> dict | None:
        return await self._one("SELECT * FROM users WHERE tg_id=?", (tg_id,))

    async def ensure_user(self, tg_id: int, name: str | None = None) -> dict:
        await self._exec(
            "INSERT OR IGNORE INTO users(tg_id, name, created_at) VALUES(?,?,?)",
            (tg_id, name, _now()),
        )
        user = await self.get_user(tg_id)
        assert user
        return user

    async def update_user(self, tg_id: int, **fields: Any) -> None:
        bad = set(fields) - USER_FIELDS
        if bad:
            raise ValueError(f"неизвестные поля: {bad}")
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        await self._exec(f"UPDATE users SET {cols} WHERE tg_id=?", (*fields.values(), tg_id))

    async def onboarded_users(self) -> list[dict]:
        return await self._all("SELECT * FROM users WHERE onboarded=1")

    async def delete_user(self, tg_id: int) -> None:
        for table in _TABLES_BY_USER:
            await self.db.execute(f"DELETE FROM {table} WHERE tg_id=?", (tg_id,))
        await self.db.commit()

    # ---------- прогресс-фото ----------
    async def add_progress_media(self, tg_id: int, day: str, items: list[tuple[str, str, str]]) -> None:
        """items: (kind, label, file_id). Повторная съёмка в тот же день заменяет прежнюю."""
        await self.db.execute("DELETE FROM progress_media WHERE tg_id=? AND day=?", (tg_id, day))
        for kind, label, file_id in items:
            await self.db.execute(
                "INSERT INTO progress_media(tg_id, day, kind, label, file_id) VALUES(?,?,?,?,?)",
                (tg_id, day, kind, label, file_id),
            )
        await self.db.commit()

    async def progress_days(self, tg_id: int) -> list[str]:
        rows = await self._all(
            "SELECT DISTINCT day FROM progress_media WHERE tg_id=? ORDER BY day", (tg_id,)
        )
        return [r["day"] for r in rows]

    async def progress_media_on(self, tg_id: int, day: str) -> list[dict]:
        return await self._all(
            "SELECT kind, label, file_id FROM progress_media WHERE tg_id=? AND day=? ORDER BY id", (tg_id, day)
        )

    # ---------- measurements ----------
    async def add_measurements(self, tg_id: int, values: dict[str, float], day: str) -> None:
        # Повторный ввод в тот же день заменяет прежний.
        for kind, value in values.items():
            await self.db.execute(
                "DELETE FROM measurements WHERE tg_id=? AND kind=? AND day=?", (tg_id, kind, day)
            )
            await self.db.execute(
                "INSERT INTO measurements(tg_id, day, kind, value) VALUES(?,?,?,?)",
                (tg_id, day, kind, value),
            )
        await self.db.commit()

    async def measurement_series(self, tg_id: int) -> dict[str, list[tuple[str, float]]]:
        rows = await self._all(
            "SELECT kind, day, value FROM measurements WHERE tg_id=? ORDER BY day, id", (tg_id,)
        )
        out: dict[str, list[tuple[str, float]]] = {}
        for r in rows:
            out.setdefault(r["kind"], []).append((r["day"], r["value"]))
        return out

    # ---------- workouts ----------
    async def create_workout(self, tg_id: int, day: str, day_name: str, place: str,
                             plan: list[dict], status: str = "planned") -> int:
        return await self._exec(
            "INSERT INTO workouts(tg_id, day, day_name, place, plan, status) VALUES(?,?,?,?,?,?)",
            (tg_id, day, day_name, place, json.dumps(plan, ensure_ascii=False), status),
        )

    async def get_workout(self, workout_id: int) -> dict | None:
        w = await self._one("SELECT * FROM workouts WHERE id=?", (workout_id,))
        if w:
            w["plan"] = json.loads(w["plan"])
        return w

    async def update_workout(self, workout_id: int, **fields: Any) -> None:
        bad = set(fields) - WORKOUT_FIELDS
        if bad:
            raise ValueError(f"неизвестные поля: {bad}")
        if "plan" in fields and not isinstance(fields["plan"], str):
            fields["plan"] = json.dumps(fields["plan"], ensure_ascii=False)
        cols = ", ".join(f"{k}=?" for k in fields)
        await self._exec(f"UPDATE workouts SET {cols} WHERE id=?", (*fields.values(), workout_id))

    async def active_workout(self, tg_id: int) -> dict | None:
        w = await self._one(
            "SELECT * FROM workouts WHERE tg_id=? AND status='active' ORDER BY id DESC LIMIT 1", (tg_id,)
        )
        if not w:
            return None
        if self._is_stale(w):
            await self._close_stale(w)
            return None
        w["plan"] = json.loads(w["plan"])
        return w

    @staticmethod
    def _is_stale(w: dict) -> bool:
        """Забытая тренировка: иначе она перехватывала бы любые числа в чате."""
        try:
            started = datetime.fromisoformat(w["started_at"])
        except (TypeError, ValueError):
            return False
        return datetime.now(timezone.utc) - started > timedelta(hours=STALE_WORKOUT_HOURS)

    async def _close_stale(self, w: dict) -> None:
        if await self.workout_sets(w["id"]):
            await self.update_workout(w["id"], status="done", finished_at=w["started_at"])
            await self._exec("UPDATE users SET rotation_idx=rotation_idx+1 WHERE tg_id=?", (w["tg_id"],))
        else:
            await self.update_workout(w["id"], status="missed")

    async def workout_on(self, tg_id: int, day: str) -> dict | None:
        w = await self._one(
            "SELECT * FROM workouts WHERE tg_id=? AND day=? ORDER BY id DESC LIMIT 1", (tg_id, day)
        )
        if w:
            w["plan"] = json.loads(w["plan"])
        return w

    async def mark_missed(self, tg_id: int, before_day: str) -> None:
        """Тренировки, на которые не ответили, помечаются пропущенными."""
        await self._exec(
            "UPDATE workouts SET status='missed' WHERE tg_id=? AND status IN ('planned','active') AND day<?",
            (tg_id, before_day),
        )

    async def next_planned_workout(self, tg_id: int) -> dict | None:
        return await self._one(
            "SELECT * FROM workouts WHERE tg_id=? AND status='planned' ORDER BY day LIMIT 1", (tg_id,)
        )

    # ---------- sets ----------
    async def add_set(self, workout_id: int, tg_id: int, ex_id: str, set_no: int,
                      weight: float, reps: int, feel: int | None) -> int:
        return await self._exec(
            "INSERT INTO sets(workout_id, tg_id, ex_id, set_no, weight, reps, feel, created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (workout_id, tg_id, ex_id, set_no, weight, reps, feel, _now()),
        )

    async def set_feel(self, set_id: int, feel: int) -> None:
        await self._exec("UPDATE sets SET feel=? WHERE id=?", (feel, set_id))

    async def get_set(self, set_id: int) -> dict | None:
        return await self._one("SELECT * FROM sets WHERE id=?", (set_id,))

    async def last_sets(self, tg_id: int, ex_id: str, exclude_workout: int | None = None) -> list[dict]:
        """Подходы упражнения из последней тренировки, где оно было сделано."""
        row = await self._one(
            "SELECT workout_id FROM sets WHERE tg_id=? AND ex_id=? AND feel IS NOT NULL "
            "AND workout_id != ? ORDER BY workout_id DESC LIMIT 1",
            (tg_id, ex_id, exclude_workout or -1),
        )
        if not row:
            return []
        return await self._all(
            "SELECT * FROM sets WHERE workout_id=? AND ex_id=? AND feel IS NOT NULL ORDER BY set_no",
            (row["workout_id"], ex_id),
        )

    async def workout_sets(self, workout_id: int) -> list[dict]:
        return await self._all("SELECT * FROM sets WHERE workout_id=? ORDER BY id", (workout_id,))

    async def best_lifts(self, tg_id: int) -> list[dict]:
        """Максимальный рабочий вес по каждому упражнению с весом."""
        return await self._all(
            "SELECT ex_id, MAX(weight) AS best_w, COUNT(DISTINCT workout_id) AS sessions "
            "FROM sets WHERE tg_id=? AND weight>0 AND feel IS NOT NULL GROUP BY ex_id ORDER BY sessions DESC",
            (tg_id,),
        )

    # ---------- stats ----------
    async def streak(self, tg_id: int) -> int:
        rows = await self._all(
            "SELECT status FROM workouts WHERE tg_id=? AND status IN ('done','skipped','missed') "
            "ORDER BY day DESC, id DESC",
            (tg_id,),
        )
        n = 0
        for r in rows:
            if r["status"] != "done":
                break
            n += 1
        return n

    async def done_count(self, tg_id: int, since: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS c FROM workouts WHERE tg_id=? AND status='done'"
        args: tuple = (tg_id,)
        if since:
            sql += " AND day>=?"
            args = (tg_id, since)
        row = await self._one(sql, args)
        return row["c"] if row else 0

    # ---------- usage limits ----------
    async def take_quota(self, tg_id: int, kind: str, limit: int) -> bool:
        """True и счётчик +1, если дневной лимит не исчерпан."""
        day = date.today().isoformat()
        row = await self._one("SELECT n FROM usage WHERE tg_id=? AND day=? AND kind=?", (tg_id, day, kind))
        used = row["n"] if row else 0
        if used >= limit:
            return False
        await self.db.execute(
            "INSERT INTO usage(tg_id, day, kind, n) VALUES(?,?,?,1) "
            "ON CONFLICT(tg_id, day, kind) DO UPDATE SET n=n+1",
            (tg_id, day, kind),
        )
        await self.db.execute(
            "DELETE FROM usage WHERE day<?", ((date.today() - timedelta(days=3)).isoformat(),)
        )
        await self.db.commit()
        return True

    # ---------- supplement stack ----------
    async def stack_add(self, tg_id: int, supp_id: str) -> None:
        await self._exec("INSERT OR IGNORE INTO stack(tg_id, supp_id) VALUES(?,?)", (tg_id, supp_id))

    async def stack_remove(self, tg_id: int, supp_id: str) -> None:
        await self._exec("DELETE FROM stack WHERE tg_id=? AND supp_id=?", (tg_id, supp_id))

    async def stack_list(self, tg_id: int) -> list[str]:
        rows = await self._all("SELECT supp_id FROM stack WHERE tg_id=? ORDER BY supp_id", (tg_id,))
        return [r["supp_id"] for r in rows]

    # ---------- chat history ----------
    async def chat_add(self, tg_id: int, role: str, content: str) -> None:
        await self.db.execute(
            "INSERT INTO chat(tg_id, role, content) VALUES(?,?,?)", (tg_id, role, content[:4000])
        )
        # Храним только последние 20 реплик.
        await self.db.execute(
            "DELETE FROM chat WHERE tg_id=? AND id NOT IN "
            "(SELECT id FROM chat WHERE tg_id=? ORDER BY id DESC LIMIT 20)",
            (tg_id, tg_id),
        )
        await self.db.commit()

    async def chat_recent(self, tg_id: int, n: int = 8) -> list[tuple[str, str]]:
        rows = await self._all(
            "SELECT role, content FROM chat WHERE tg_id=? ORDER BY id DESC LIMIT ?", (tg_id, n)
        )
        return [(r["role"], r["content"]) for r in reversed(rows)]

    async def chat_clear(self, tg_id: int) -> None:
        await self._exec("DELETE FROM chat WHERE tg_id=?", (tg_id,))
