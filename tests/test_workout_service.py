import pytest

from bot.db.database import Database
from bot.services.exercise_db import ExerciseDB
from bot.services.progression import Feel
from bot.services.set_parser import ParsedSet
from bot.services.workout import WorkoutService
from datetime import date


@pytest.fixture
async def env():
    db = Database(":memory:")
    await db.connect()
    svc = WorkoutService(db, ExerciseDB())
    await db.ensure_user(1, "T")
    await db.update_user(1, age=15, weight_kg=60, height_cm=170, goal="muscle", level="new", place="gym",
                         days_per_week=3, days="0,2,4", train_time="18:00", tz="Europe/Moscow", onboarded=1)
    yield db, svc
    await db.close()


async def run_full_workout(db, svc, user, day, weight, reps, feel):
    w = await svc.ensure_planned(user, day)
    w = await svc.start(w["id"], "now")
    while True:
        w = await db.get_workout(w["id"])
        if w["ex_i"] >= len(w["plan"]):
            break
        item = w["plan"][w["ex_i"]]
        w_val = weight if item["kind"] == "weight" else 0
        _, outcome = await svc.log_set(w, ParsedSet(w_val, reps, feel))
        assert outcome is not None
        if outcome.state == "finished":
            break
    return await svc.finish(user, w["id"], "now")


async def test_full_flow_and_progression(env):
    db, svc = env
    user = await db.get_user(1)
    res = await run_full_workout(db, svc, user, date(2026, 1, 5), 20, 12, Feel.OK)
    assert res["sets"] > 10 and res["streak"] == 1
    user = await db.get_user(1)
    assert user["rotation_idx"] == 1  # следующая тренировка — день B

    # Вторая тренировка того же дня A: после 12 повторов вес должен вырасти.
    w = await svc.ensure_planned(user, date(2026, 1, 7))
    assert w["day_name"].endswith("B")
    user["rotation_idx"] = 2
    w2 = await svc.ensure_planned(user, date(2026, 1, 9))
    w2 = await svc.start(w2["id"], "now")
    item = w2["plan"][0]
    target, last = await svc.target_for(1, item, w2["id"])
    assert last, "история первого упражнения должна подтянуться"
    if item["kind"] == "weight":
        assert target.weight and target.weight > 20


async def test_pending_feel_flow_and_swap(env):
    db, svc = env
    user = await db.get_user(1)
    w = await svc.ensure_planned(user, date(2026, 1, 5))
    w = await svc.start(w["id"], "now")
    assert await svc.swap_exercise(w["id"])
    w = await db.get_workout(w["id"])
    sid, outcome = await svc.log_set(w, ParsedSet(30, 10, None))
    assert outcome is None
    out = await svc.complete_set(w["id"], sid, Feel.EASY)
    assert out.state == "next_set" and "слишком легко" in out.hint_text  # первое упражнение: калибровка веса
    # swap после начатого подхода запрещён
    assert not await svc.swap_exercise(w["id"])


async def test_missed_workout_and_streak_reset(env):
    db, svc = env
    user = await db.get_user(1)
    w1 = await svc.ensure_planned(user, date(2026, 1, 5))
    await svc.ensure_planned(user, date(2026, 1, 7))
    assert (await db.get_workout(w1["id"]))["status"] == "missed"
    assert await db.streak(1) == 0


async def test_rebuild_for_home(env):
    db, svc = env
    user = await db.get_user(1)
    w = await svc.ensure_planned(user, date(2026, 1, 5))
    w = await svc.rebuild_for_place(user, w["id"], "home_bw")
    assert w["place"] == "home_bw" and all(i["kind"] != "weight" for i in w["plan"])


async def test_quota_and_delete(env):
    db, _ = env
    assert await db.take_quota(1, "llm", 2) and await db.take_quota(1, "llm", 2)
    assert not await db.take_quota(1, "llm", 2)
    await db.chat_add(1, "user", "hi")
    await db.delete_user(1)
    assert await db.get_user(1) is None and await db.chat_recent(1) == []


async def test_stale_active_workout_is_closed(env):
    from datetime import datetime, timedelta, timezone

    db, svc = env
    user = await db.get_user(1)
    w = await svc.ensure_planned(user, date(2026, 1, 5))
    old = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat(timespec="seconds")
    w = await svc.start(w["id"], old)
    assert await db.active_workout(1) is None  # забытая тренировка больше не перехватывает чат
    assert (await db.get_workout(w["id"]))["status"] == "missed"

    fresh = await svc.ensure_planned(user, date(2026, 1, 7))
    await svc.start(fresh["id"], datetime.now(timezone.utc).isoformat(timespec="seconds"))
    assert (await db.active_workout(1))["id"] == fresh["id"]


async def test_first_session_gets_start_weight_from_body_weight(env):
    db, svc = env
    user = await db.get_user(1)  # 15 лет, 60 кг, новичок
    w = await svc.ensure_planned(user, date(2026, 1, 5))
    w = await svc.start(w["id"], "now")
    item = w["plan"][w["ex_i"]]
    if item["kind"] == "weight":
        assert w["cur_weight"] and w["cur_weight"] > 0  # не «подбери сам»
