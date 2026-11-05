from datetime import UTC, date

import pytest

from bot.db.database import Database
from bot.services.exercise_db import ExerciseDB
from bot.services.progression import Feel
from bot.services.set_parser import ParsedSet
from bot.services.workout import WorkoutService


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

    # Через круг (A→B→C→A) снова день A: после 12 повторов вес должен вырасти.
    w = await svc.ensure_planned(user, date(2026, 1, 7))
    assert w["day_name"].endswith("B")
    user["rotation_idx"] = 3
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
    from datetime import datetime, timedelta

    db, svc = env
    user = await db.get_user(1)
    w = await svc.ensure_planned(user, date(2026, 1, 5))
    old = (datetime.now(UTC) - timedelta(hours=12)).isoformat(timespec="seconds")
    w = await svc.start(w["id"], old)
    assert await db.active_workout(1) is None  # забытая тренировка больше не перехватывает чат
    assert (await db.get_workout(w["id"]))["status"] == "missed"

    fresh = await svc.ensure_planned(user, date(2026, 1, 7))
    await svc.start(fresh["id"], datetime.now(UTC).isoformat(timespec="seconds"))
    assert (await db.active_workout(1))["id"] == fresh["id"]


async def test_first_session_gets_start_weight_from_body_weight(env):
    db, svc = env
    user = await db.get_user(1)  # 15 лет, 60 кг, новичок
    w = await svc.ensure_planned(user, date(2026, 1, 5))
    w = await svc.start(w["id"], "now")
    item = w["plan"][w["ex_i"]]
    if item["kind"] == "weight":
        assert w["cur_weight"] and w["cur_weight"] > 0  # не «подбери сам»


async def test_unilateral_exercise_asks_both_sides(env):
    """Тяга одной рукой: каждый подход = правая + левая, иначе вторая сторона остаётся без работы."""
    db, svc = env
    user = await db.get_user(1)
    w = await svc.ensure_planned(user, date(2026, 1, 5))
    plan = [{"ex_id": "db_row", "sets": 3, "lo": 8, "hi": 12, "kind": "weight", "step": 2.0, "uni": True}]
    await db.update_workout(w["id"], plan=plan)
    w = await svc.start(w["id"], "now")
    states = []
    for _ in range(6):
        w = await db.get_workout(w["id"])
        right = w["set_i"] % 2 == 0
        _, out = await svc.log_set(w, ParsedSet(10, 10, None))
        if right:
            assert out is not None and out.state == "other_side"
            assert (await db.get_workout(w["id"]))["pend_set_id"] is None  # «как далось» спросим после левой
        else:
            assert out is None
            fresh = await db.get_workout(w["id"])
            out = await svc.complete_set(w["id"], fresh["pend_set_id"], Feel.OK)
        states.append(out.state)
    assert states == ["other_side", "next_set"] * 2 + ["other_side", "finished"]
    sets = await db.workout_sets(w["id"])
    assert len(sets) == 6 and all(s["feel"] is not None for s in sets)


async def test_unilateral_labels_and_old_plans():
    from bot.services.workout import is_uni, set_label, total_steps

    item = {"ex_id": "db_row", "sets": 3, "lo": 8, "hi": 12, "kind": "weight", "step": 2.0}  # план без поля uni
    assert is_uni(item) and total_steps(item) == 6
    assert set_label(item, 0) == "Подход 1/3 · правая сторона"
    assert set_label(item, 3) == "Подход 2/3 · левая сторона"
    assert set_label({**item, "ex_id": "goblet_squat"}, 1) == "Подход 2/3"


async def test_three_day_cycle_has_different_days(env):
    """Три тренировки в неделю: A, B, C разные, через круг снова A."""
    db, svc = env
    user = await db.get_user(1)
    names, plans = [], []
    for n, day in enumerate((date(2026, 1, 5), date(2026, 1, 7), date(2026, 1, 9), date(2026, 1, 12))):
        user["rotation_idx"] = n
        w = await svc.ensure_planned(user, day)
        names.append(w["day_name"])
        plans.append({i["ex_id"] for i in w["plan"] if i["kind"] != "cardio"})
    assert names == ["Всё тело A", "Всё тело B", "Всё тело C", "Всё тело A"]
    assert plans[0] == plans[3]
    assert len(plans[0] & plans[1]) == 0 and len(plans[1] & plans[2]) <= 1


async def test_unfinished_workout_counts_and_next_day_moves_on(env):
    """Не нажал «Закончить»: на следующий день тренировка засчитана, а план — уже день B, не снова A."""
    db, svc = env
    user = await db.get_user(1)
    w = await svc.ensure_planned(user, date(2026, 1, 5))
    w = await svc.start(w["id"], "now")
    await svc.log_set(w, ParsedSet(20, 10, Feel.OK))
    nxt = await svc.ensure_planned(user, date(2026, 1, 7))  # напоминание со старым dict пользователя
    assert (await db.get_workout(w["id"]))["status"] == "done"
    assert nxt["day_name"] == "Всё тело B"
    assert (await db.get_user(1))["rotation_idx"] == 1
