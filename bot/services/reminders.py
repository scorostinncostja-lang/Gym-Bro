"""Напоминания о тренировках и добавках (APScheduler)."""
import html
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from bot.db.database import Database
from bot.services.supp_plan import build_plan, slot_lines
from bot.services.timeutil import local_today, parse_days, shift_time
from bot.services.workout import WorkoutService
from bot.services.places import user_places
from bot.ui import Sup, inline, workout_keyboard

log = logging.getLogger(__name__)

FOLLOWUP_MIN = 45
SNOOZE_MIN = 30


class Reminders:
    def __init__(self, bot: Bot, db: Database, workouts: WorkoutService):
        self.bot = bot
        self.db = db
        self.workouts = workouts
        self.scheduler = AsyncIOScheduler()

    async def start(self) -> None:
        self.scheduler.start()
        for user in await self.db.onboarded_users():
            self.schedule_user(user)

    def shutdown(self) -> None:
        self.scheduler.shutdown(wait=False)

    # ---------- расписание ----------
    def _remove(self, prefix: str) -> None:
        for job in self.scheduler.get_jobs():
            if job.id.startswith(prefix):
                job.remove()

    def schedule_user(self, user: dict) -> None:
        tg_id = user["tg_id"]
        self._remove(f"wk:{tg_id}:")
        self._remove(f"sp:{tg_id}")
        days = parse_days(user.get("days"))
        cardio_days = parse_days(user.get("cardio_days"))
        tz = user.get("tz")
        if user.get("train_time") and tz:
            h, m = map(int, user["train_time"].split(":"))
            for name, ds in (("cron", days), ("cardio", cardio_days)):
                if not ds:
                    continue
                self.scheduler.add_job(
                    self.send_reminder, CronTrigger(
                        day_of_week=",".join(map(str, ds)), hour=h, minute=m, timezone=ZoneInfo(tz),
                    ),
                    args=[tg_id], id=f"wk:{tg_id}:{name}", misfire_grace_time=600, coalesce=True,
                    replace_existing=True,
                )
        if user.get("supp_remind") and tz:
            self._schedule_supp(user, days, ZoneInfo(tz))

    def _schedule_supp(self, user: dict, days: list[int], tz: ZoneInfo) -> None:
        """Добавки напоминаются в нужный момент: утром, перед тренировкой и после неё."""
        tg_id = user["tg_id"]
        h, m = map(int, user["supp_time"].split(":"))
        self.scheduler.add_job(
            self.send_supp_reminder, CronTrigger(hour=h, minute=m, timezone=tz), args=[tg_id, "am"],
            id=f"sp:{tg_id}:am", misfire_grace_time=900, coalesce=True, replace_existing=True,
        )
        if user.get("train_time") and days:
            for slot, shift in (("pre", -45), ("post", 75)):
                t = shift_time(user["train_time"], shift)
                th, tm = map(int, t.split(":"))
                self.scheduler.add_job(
                    self.send_supp_reminder, CronTrigger(
                        day_of_week=",".join(map(str, days)), hour=th, minute=tm, timezone=tz),
                    args=[tg_id, slot], id=f"sp:{tg_id}:{slot}", misfire_grace_time=900, coalesce=True,
                    replace_existing=True,
                )

    def unschedule_user(self, tg_id: int) -> None:
        self._remove(f"wk:{tg_id}:")
        self._remove(f"sp:{tg_id}")
        self._remove(f"fu:{tg_id}:")
        self._remove(f"sn:{tg_id}:")

    # ---------- задачи ----------
    async def send_reminder(self, tg_id: int, workout_id: int | None = None) -> None:
        user = await self.db.get_user(tg_id)
        if not user or not user["onboarded"]:
            return
        if workout_id is None:
            w = await self.workouts.ensure_planned(user, local_today(user["tz"]))
        else:
            w = await self.db.get_workout(workout_id)
        if not w or w["status"] != "planned":
            return
        streak = await self.db.streak(tg_id)
        streak_line = f"\nСерия: {streak} подряд." if streak else ""
        await self._safe_send(
            tg_id,
            f"💪 Пора тренироваться: <b>{html.escape(w['day_name'])}</b>.{streak_line}\n"
            "Нажми «Начать» или выбери другое место: подберу версию под него.",
            workout_keyboard(w["id"], user_places(user), w["place"], reminder=True),
        )
        self._schedule_followup(tg_id, w["id"])

    def _schedule_followup(self, tg_id: int, workout_id: int) -> None:
        when = datetime.now(ZoneInfo("UTC")) + timedelta(minutes=FOLLOWUP_MIN)
        self.scheduler.add_job(
            self.send_followup, DateTrigger(run_date=when), args=[tg_id, workout_id],
            id=f"fu:{tg_id}:{workout_id}", replace_existing=True, misfire_grace_time=300,
        )

    def schedule_snooze(self, tg_id: int, workout_id: int) -> None:
        when = datetime.now(ZoneInfo("UTC")) + timedelta(minutes=SNOOZE_MIN)
        self.scheduler.add_job(
            self.send_reminder, DateTrigger(run_date=when), args=[tg_id, workout_id],
            id=f"sn:{tg_id}:{workout_id}", replace_existing=True, misfire_grace_time=300,
        )

    async def send_followup(self, tg_id: int, workout_id: int) -> None:
        w = await self.db.get_workout(workout_id)
        if not w or w["status"] != "planned" or w["nagged"]:
            return
        await self.db.update_workout(workout_id, nagged=1)
        user = await self.db.get_user(tg_id)
        if not user:
            return
        await self._safe_send(
            tg_id,
            "Ты не ответил. Тренировка в силе? Даже 15 минут лучше, чем ничего. "
            "Не вышло — нажми «Пропуск», я не обижусь.",
            workout_keyboard(workout_id, user_places(user), w["place"], reminder=True),
        )

    async def send_supp_reminder(self, tg_id: int, slot: str = "am") -> None:
        user = await self.db.get_user(tg_id)
        if not user:
            return
        plan = build_plan(user, await self.db.stack_list(tg_id))
        trains_today = local_today(user["tz"]).weekday() in parse_days(user["days"])
        lines = slot_lines(plan, slot, trains_today)
        if not lines:
            return
        body = "\n".join(f"• {html.escape(x)}" for x in lines)
        title = {"am": "💊 Добавки с едой", "pre": "💊 Перед тренировкой", "post": "💊 После тренировки"}[slot]
        await self._safe_send(
            tg_id, f"{title}:\n{body}\nПлан целиком: /stack",
            inline([[("✅ Принял", Sup(act="took"))]]),
        )

    async def _safe_send(self, tg_id: int, text: str, markup=None) -> None:
        try:
            await self.bot.send_message(tg_id, text, reply_markup=markup)
        except Exception as e:  # noqa: BLE001 — пользователь мог заблокировать бота
            log.warning("не удалось отправить напоминание %s: %r", tg_id, e)
