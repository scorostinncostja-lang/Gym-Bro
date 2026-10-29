"""Логика тренировки: план дня, подходы, прогрессия. Без Telegram."""
from dataclasses import dataclass
from datetime import date

from bot.data import get_exercise
from bot.db.database import Database
from bot.services.exercise_db import ExerciseDB
from bot.services.program_builder import alternatives, build_day, template_for
from bot.services.progression import Feel, SetResult, Target, next_target, set_hint
from bot.services.start_weights import start_weight
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
        tpl = template_for(user["days_per_week"], user["rotation_idx"])
        plan = [i.to_dict() for i in build_day(tpl, user["place"])]
        wid = await self.db.create_workout(tg_id, day.isoformat(), tpl["name"], user["place"], plan)
        w = await self.db.get_workout(wid)
        assert w
        return w

    async def rebuild_for_place(self, user: dict, workout_id: int, place: str) -> dict:
        """«Сегодня не могу в зал»: тот же день, но под другой инвентарь."""
        w = await self.db.get_workout(workout_id)
        assert w and w["status"] == "planned"
        tpl = template_for(user["days_per_week"], user["rotation_idx"])
        plan = [i.to_dict() for i in build_day(tpl, place)]
        await self.db.update_workout(workout_id, plan=plan, place=place)
        w = await self.db.get_workout(workout_id)
        assert w
        return w

    async def target_for(self, tg_id: int, item: dict, workout_id: int) -> tuple[Target, list[dict]]:
        last = await self.db.last_sets(tg_id, item["ex_id"], exclude_workout=workout_id)
        results = [SetResult(s["weight"], s["reps"], Feel(s["feel"])) for s in last]
        start = None
        if not last and item["kind"] == "weight":
            user = await self.db.get_user(tg_id)
            if user:
                start = start_weight(item["ex_id"], user["weight_kg"], user["age"], user["level"], item["step"])
        return next_target(results, item["lo"], item["hi"], item["kind"], item["step"], start), last

    async def start(self, workout_id: int, now_iso: str) -> dict:
        w = await self.db.get_workout(workout_id)
        assert w
        await self.db.update_workout(workout_id, status="active", started_at=now_iso, ex_i=0, set_i=0)
        return await self.enter_exercise(workout_id)

    async def enter_exercise(self, workout_id: int) -> dict:
        """Выставляет рабочую цель для текущего упражнения."""
        w = await self.db.get_workout(workout_id)
        assert w
        item = w["plan"][w["ex_i"]]
        target, _ = await self.target_for(w["tg_id"], item, workout_id)
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
        hint = set_hint(
            SetResult(s["weight"], s["reps"], feel), item["lo"], item["hi"], item["kind"], item["step"],
            calibrating=calibrating,
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
        alts = alternatives(item["ex_id"], w["place"], taken)
        if not alts:
            return False
        ex = alts[0]
        lo, hi = ex.range
        w["plan"][w["ex_i"]] = {
            "ex_id": ex.id, "sets": item["sets"] if ex.kind != "time" else min(item["sets"], 3),
            "lo": lo, "hi": hi, "kind": ex.kind, "step": ex.step,
        }
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
    unit = {"weight": "повт.", "reps": "повт.", "time": "сек"}[item["kind"]]
    return f"{name}: {item['sets']} × {item['lo']}–{item['hi']} {unit}"
