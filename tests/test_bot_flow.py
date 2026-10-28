"""Сквозной прогон: настоящий диспетчер и обработчики, Telegram подменён."""
from datetime import datetime

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, MessageEntity, Update, User

from bot.config import Settings
from bot.db.database import Database
from bot.handlers import chat, info, onboarding, supplements, workout
from bot.main import build_dispatcher
from bot.services.exercise_db import ExerciseDB
from bot.services.llm import LLM
from bot.services.reminders import Reminders
from bot.services.workout import WorkoutService
from bot.ui import Fl, Ob, Rem

CHAT = Chat(id=1, type="private")
USER = User(id=1, is_bot=False, first_name="Тест")


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent: list[tuple[str, dict]] = []
        self._mid = 100

    async def close(self): ...

    async def stream_content(self, *a, **k):  # pragma: no cover
        yield b""

    async def make_request(self, bot, method: TelegramMethod, timeout=None):
        data = method.model_dump(exclude_none=True)
        self.sent.append((type(method).__name__, data))
        ret = getattr(method, "__returning__", None)
        self._mid += 1
        msg = Message(message_id=self._mid, date=datetime.now(), chat=CHAT, text=data.get("text"))
        name = str(ret)
        if "list" in name.lower():
            return [msg]
        if "Message" in name:
            return msg
        return True

    def texts(self) -> list[str]:
        return [d.get("text") or d.get("caption") or "" for n, d in self.sent if n in ("SendMessage", "EditMessageText")]


@pytest.fixture
async def rig():
    session = FakeSession()
    bot = Bot("123456:TEST", session=session)
    db = Database(":memory:")
    await db.connect()
    edb = ExerciseDB()
    workouts = WorkoutService(db, edb)
    reminders = Reminders(bot, db, workouts)
    settings = Settings(_env_file=None, bot_token="123456:TEST")
    # Роутеры модульные, а в тестах диспетчеров несколько: отцепляем от прошлого.
    for r in (onboarding.router, workout.router, info.router, supplements.router, chat.router):
        r._parent_router = None
    dp = build_dispatcher(settings=settings, db=db, edb=edb, llm=LLM(settings), workouts=workouts, reminders=reminders)
    yield bot, dp, db, session
    await db.close()


class Chatter:
    def __init__(self, bot, dp):
        self.bot, self.dp, self.uid, self.mid = bot, dp, 1000, 1

    async def say(self, text: str):
        self.uid += 1
        ents = [MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))] if text.startswith("/") else None
        msg = Message(message_id=self.mid, date=datetime.now(), chat=CHAT, from_user=USER, text=text, entities=ents)
        self.mid += 1
        await self.dp.feed_update(self.bot, Update(update_id=self.uid, message=msg))

    async def press(self, data):
        self.uid += 1
        msg = Message(message_id=self.mid, date=datetime.now(), chat=CHAT, from_user=User(id=999, is_bot=True, first_name="bot"), text="x")
        cb = CallbackQuery(id=str(self.uid), from_user=USER, chat_instance="ci", message=msg, data=data.pack())
        await self.dp.feed_update(self.bot, Update(update_id=self.uid, callback_query=cb))


async def onboard(c: Chatter, age="15"):
    await c.say("/start")
    for v in (age, "62", "175", "35", "90", "28", "25", "75", "50"):
        await c.say(v)
    await c.press(Ob(act="goal", arg="muscle"))
    await c.press(Ob(act="level", arg="new"))
    await c.press(Ob(act="place", arg="gym"))
    await c.press(Ob(act="n", arg="3"))
    await c.press(Ob(act="dpreset"))
    await c.press(Ob(act="ddone"))
    await c.press(Ob(act="t", arg="1800"))
    await c.press(Ob(act="tz", arg="4"))


async def test_onboarding_creates_profile_and_schedule(rig):
    bot, dp, db, session = rig
    c = Chatter(bot, dp)
    await onboard(c)
    u = await db.get_user(1)
    assert u["onboarded"] == 1 and u["age"] == 15 and u["days"] == "0,2,4"
    assert u["train_time"] == "18:00" and u["tz"] == "Asia/Almaty" and u["place"] == "gym"
    series = await db.measurement_series(1)
    assert series["weight"][0][1] == 62 and series["biceps"][0][1] == 28 and len(series) == 7
    assert any("Готово" in t for t in session.texts())
    jobs = [j.id for j in (await _jobs(dp))]
    assert "wk:1:cron" in jobs


async def _jobs(dp):
    return dp.workflow_data["reminders"].scheduler.get_jobs()


async def test_under_13_is_refused(rig):
    bot, dp, db, session = rig
    c = Chatter(bot, dp)
    await c.say("/start")
    await c.say("12")
    assert any("13+" in t for t in session.texts())
    assert (await db.get_user(1))["onboarded"] == 0


async def test_workout_session_via_telegram(rig):
    bot, dp, db, session = rig
    c = Chatter(bot, dp)
    await onboard(c)
    await c.say("/today")
    w = await db.next_planned_workout(1)
    assert w
    await c.press(Rem(act="go", wid=w["id"]))
    w = await db.get_workout(w["id"])
    assert w["status"] == "active"
    # первое упражнение: «вес повторы», затем ощущение кнопкой
    await c.say("40 10")
    w = await db.get_workout(w["id"])
    assert w["pend_set_id"]
    await c.press(Fl(wid=w["id"], sid=w["pend_set_id"], v=1))
    w = await db.get_workout(w["id"])
    assert w["set_i"] == 1 and w["cur_reps"] == 11  # «легко» -> на повтор больше
    # ощущение прямо в тексте закрывает подход сразу
    await c.say("40 11 тяжело")
    w = await db.get_workout(w["id"])
    assert w["set_i"] == 2
    await c.say("/finish")
    assert (await db.get_workout(w["id"]))["status"] == "done"
    assert any("Тренировка завершена" in t for t in session.texts())


async def test_minor_guard_blocks_dose_question_without_llm(rig):
    bot, dp, db, session = rig
    c = Chatter(bot, dp)
    await onboard(c)
    await c.say("сколько креатина мне принимать?")
    assert any("Дозировки добавок я не даю" in t for t in session.texts())


async def test_supplements_screen_and_exercise_search(rig):
    bot, dp, db, session = rig
    c = Chatter(bot, dp)
    await onboard(c)
    await c.say("/supplements")
    assert any("Добавки без рекламы" in t for t in session.texts())
    await c.say("/exercise присед")
    assert any(n in ("SendMessage",) for n, _ in session.sent)


async def test_closed_bot_blocks_strangers(rig):
    bot, dp, db, session = rig
    dp.workflow_data["settings"].allowed_user_ids.append(777)
    c = Chatter(bot, dp)
    await c.say("/start")
    assert any("закрытый" in t for t in session.texts())
    assert await db.get_user(1) is None
