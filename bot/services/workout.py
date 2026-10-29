"""Логика тренировки: план дня, подходы, прогрессия. Без Telegram."""
from dataclasses import dataclass
from datetime import date

from bot.data import get_exercise
from bot.db.database import Database
from bot.services.exercise_db import ExerciseDB
from bot.data import flex_template
from bot.services import equipment
from bot.services.program_builder import alternatives, build_day, build_extra_day, make_item, template_for
from bot.services.progression import Feel, SetResult, Target, next_target, set_hint
from bot.services.start_weights import start_weight
from bot.services.timeutil import local_today, parse_days
from bot.services.set_parser import ParsedSet


@dataclass
class SetOutcome:
    hint_text: str
    state: str  # next_set | next_ex | finished


class WorkoutService:
    def __init__(self, db: Database, edb: ExerciseDB):
        self.db = db
        self.edb = edb

    async def ensure_planned(self, user: dict, day: date) -> dict:
        """План на день: берём существующий или собираем по шаблону."""
        tg_id = user["tg_id"]
        existing = await self.db.workout_on(tg_id, day.isoformat())
        if existing and existing["status"] in ("planned", "active", "done", "skipped"):
            return existing
        await self.db.mark_missed(tg_id, day.isoformat())
        wd = day.weekday()
        if wd in parse_days(user.get("cardio_days")) and wd not in parse_days(user["days"]):
            name, items = build_extra_day(user, user["place"], index=await self.db.done_count(tg_id))
        else:
            tpl = template_for(user["days_per_week"], user["rotation_idx"])
            name, items = tpl["name"], build_day(tpl, user)
        wid = await self.db.create_workout(tg_id, day.isoformat(), name, user["place"], [i.to_dict() for i in items])
        w = await self.db.get_workout(wid)
        assert w
        return w

    async def rebuild_for_place(self, user: dict, workout_id: int, place: str) -> dict:
        """«Сегодня не могу в зал»: тот же день, но под другое место и инвентарь."""
        w = await self.db.get_workout(workout_id)
        assert w and w["status"] == "planned"
        if w["day_name"] == "Кардио" or w["day_name"] == flex_template()["name"]:
            name, items = build_extra_day(user, place, index=await self.db.done_count(user["tg_id"]))
        else:
            tpl = template_for(user["days_per_week"], user["rotation_idx"])
            items = build_day(tpl, user, place)
        await self.db.update_workout(workout_id, plan=[i.to_dict() for i in items], place=place)
        w = await self.db.get_workout(workout_id)
        assert w
        return w

    async def refresh_planned(self, user: dict) -> None:
        """Настройки изменились (цель, инвентарь, места): пересобираем сегодняшнюю, ещё не начатую тренировку."""
        w = await self.db.workout_on(user["tg_id"], local_today(user["tz"]).isoformat())
        if w and w["status"] == "planned":
            await self.rebuild_for_place(user, w["id"], w["place"])

    def _ladder(self, user: dict | None, w_place: str, ex_id: str) -> list[float] | None:
        ex = get_exercise(ex_id)
        if not user or not ex:
            return None
        return equipment.ladder(user, w_place, ex.equip)

    async def target_for(self, tg_id: int, item: dict, workout_id: int, place: str | None = None) -> tuple[Target, list[dict]]:
        last = await self.db.last_sets(tg_id, item["ex_id"], exclude_workout=workout_id)
        results = [SetResult(s["weight"], s["reps"], Feel(s["feel"])) for s in last]
        user = await self.db.get_user(tg_id)
        if place is None:
            w = await self.db.get_workout(workout_id)
            place = w["place"] if w else (user or {}).get("place", "gym")
        ladder = self._ladder(user, place, item["ex_id"])
        start = None
        if not last and item["kind"] == "weight" and user:
            start = start_weight(item["ex_id"], user["weight_kg"], user["age"], user["level"], item["step"])
            if start is not None:
                start = equipment.snap(start, ladder)
        return next_target(results, item["lo"], item["hi"], item["kind"], item["step"], start, ladder), last

    async def start(self, workout_id: int, now_iso: str) -> dict:
        w = await self.db.get_workout(workout_id)
        assert w
        await self.db.update_workout(workout_id, status="active", started_at=now_iso, ex_i=0, set_i=0, warm=0)
        return await self.enter_exercise(workout_id)

    async def enter_exercise(self, workout_id: int) -> dict:
        """Выставляет рабочую цель для текущего упражнения."""
        w = await self.db.get_workout(workout_id)
        assert w
        item = w["plan"][w["ex_i"]]
        target, _ = await self.target_for(w["tg_id"], item, workout_id, w["place"])
        await self.db.update_workout(
            workout_id, cur_weight=target.weight, cur_reps=target.reps, set_i=0, pend_set_id=None
        )
        w = await self.db.get_workout(workout_id)
        assert w
        return w

    async def log_set(self, w: dict, parsed: ParsedSet) -> tuple[int, SetOutcome | None]:
        """Записывает подход. Если ощущение уже названо, сразу завершает подход."""
        item = w["plan"][w["ex_i"]]
        # Незавершённый предыдущий подход закрываем нейтральной оценкой.
        if w.get("pend_set_id"):
            await self.db.set_feel(w["pend_set_id"], int(Feel.OK))
        set_id = await self.db.add_set(
            w["id"], w["tg_id"], item["ex_id"], w["set_i"] + 1, parsed.weight, parsed.reps,
            int(parsed.feel) if parsed.feel else None,
        )
        if parsed.feel:
            return set_id, await self.complete_set(w["id"], set_id, parsed.feel)
        await self.db.update_workout(w["id"], pend_set_id=set_id)
        return set_id, None

    async def complete_set(self, workout_id: int, set_id: int, feel: Feel) -> SetOutcome:
        w = await self.db.get_workout(workout_id)
        s = await self.db.get_set(set_id)
        assert w and s
        item = w["plan"][w["ex_i"]]
        await self.db.set_feel(set_id, int(feel))
        calibrating = not await self.db.last_sets(w["tg_id"], item["ex_id"], exclude_workout=workout_id)
        ladder = self._ladder(await self.db.get_user(w["tg_id"]), w["place"], item["ex_id"])
        hint = set_hint(
            SetResult(s["weight"], s["reps"], feel), item["lo"], item["hi"], item["kind"], item["step"],
            calibrating=calibrating, ladder=ladder,
        )

        set_i = w["set_i"] + 1
        if set_i < item["sets"]:
            await self.db.update_workout(
                workout_id, set_i=set_i, pend_set_id=None,
                cur_weight=hint.next_weight if hint.next_weight is not None else w["cur_weight"],
                cur_reps=hint.next_reps,
            )
            return SetOutcome(hint.text, "next_set")
        return SetOutcome(hint.text, await self._next_exercise(workout_id))

    async def skip_exercise(self, workout_id: int) -> str:
        return await self._next_exercise(workout_id)

    async def _next_exercise(self, workout_id: int) -> str:
        w = await self.db.get_workout(workout_id)
        assert w
        ex_i = w["ex_i"] + 1
        if ex_i >= len(w["plan"]):
            await self.db.update_workout(workout_id, ex_i=ex_i, set_i=0, pend_set_id=None)
            return "finished"
        await self.db.update_workout(workout_id, ex_i=ex_i, set_i=0, pend_set_id=None)
        await self.enter_exercise(workout_id)
        return "next_ex"

    async def swap_exercise(self, workout_id: int) -> bool:
        """Заменить текущее упражнение на похожее (занят тренажёр, нет инвентаря)."""
        w = await self.db.get_workout(workout_id)
        assert w
        if w["set_i"] != 0:
            return False
        item = w["plan"][w["ex_i"]]
        taken = {i["ex_id"] for i in w["plan"]}
        user = await self.db.get_user(w["tg_id"])
        alts = alternatives(item["ex_id"], user, w["place"], taken) if user else []
        if not alts:
            return False
        ex = alts[0]
        w["plan"][w["ex_i"]] = make_item(ex, item["sets"], user.get("goal"), user.get("age")).to_dict()
        await self.db.update_workout(workout_id, plan=w["plan"])
        await self.enter_exercise(workout_id)
        return True

    async def finish(self, user: dict, workout_id: int, now_iso: str, completed: bool = True) -> dict:
        """Завершает тренировку и возвращает краткую сводку."""
        w = await self.db.get_workout(workout_id)
        assert w
        sets = await self.db.workout_sets(workout_id)
        await self.db.update_workout(workout_id, status="done" if sets else "skipped", finished_at=now_iso)
        if sets:
            if w["day_name"] not in ("Кардио", flex_template()["name"]):
                await self.db.update_user(user["tg_id"], rotation_idx=user["rotation_idx"] + 1)
        volume = sum(s["weight"] * s["reps"] for s in sets)
        exercises = {s["ex_id"] for s in sets}
        return {
            "sets": len(sets), "exercises": len(exercises), "volume": volume,
            "streak": await self.db.streak(user["tg_id"]), "completed": completed,
        }


def describe_item(item: dict) -> str:
    ex = get_exercise(item["ex_id"])
    name = ex.name if ex else item["ex_id"]
    if item["kind"] == "cardio":
        return f"{name}: {item['lo']} мин"
    unit = {"weight": "повт.", "reps": "повт.", "time": "сек"}[item["kind"]]
    return f"{name}: {item['sets']} × {item['lo']}–{item['hi']} {unit}"
