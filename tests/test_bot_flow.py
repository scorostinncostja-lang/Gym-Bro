"""Сквозной прогон: настоящий диспетчер и обработчики, Telegram подменён."""
import json
from datetime import datetime

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, MessageEntity, PhotoSize, Update, User, VideoNote

from bot.config import Settings
from bot.db.database import Database
from bot.handlers import chat, info, onboarding, progress, supplements, workout
from bot.main import build_dispatcher
from bot.services.exercise_db import ExerciseDB
from bot.services.llm import LLM
from bot.services.reminders import Reminders
from bot.services.workout import WorkoutService
from bot.ui import Fl, Ob, Rem, Wk

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
    for r in (onboarding.router, workout.router, info.router, supplements.router, progress.router, chat.router):
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

    async def send(self, **fields):
        self.uid += 1
        msg = Message(message_id=self.mid, date=datetime.now(), chat=CHAT, from_user=USER, **fields)
        self.mid += 1
        await self.dp.feed_update(self.bot, Update(update_id=self.uid, message=msg))

    async def photo(self, n: int):
        await self.send(photo=[PhotoSize(file_id=f"ph{n}", file_unique_id=f"u{n}", width=90, height=160)])

    async def press(self, data):
        self.uid += 1
        msg = Message(message_id=self.mid, date=datetime.now(), chat=CHAT, from_user=User(id=999, is_bot=True, first_name="bot"), text="x")
        cb = CallbackQuery(id=str(self.uid), from_user=USER, chat_instance="ci", message=msg, data=data.pack())
        await self.dp.feed_update(self.bot, Update(update_id=self.uid, callback_query=cb))


async def onboard(c: Chatter, age="15", method="tape"):
    await c.say("/start")
    for v in (age, "62", "175"):
        await c.say(v)
    if method == "tape":
        await c.press(Ob(act="mtape"))
        for v in ("35", "90", "28", "25", "75", "50"):
            await c.say(v)
    elif method == "none":
        await c.press(Ob(act="mnone"))
    else:  # photo: дальше тест сам присылает фото или кружок
        await c.press(Ob(act="mphoto"))
        return
    await finish_onboarding(c)


async def finish_onboarding(c: Chatter, places=("gym",), time_text="18:00", city="Алматы", extra="0", goal="muscle",
                            skip_country=False):
    await c.press(Ob(act="goal", arg=goal))
    await c.press(Ob(act="goal2", arg="-"))
    await c.press(Ob(act="level", arg="new"))
    for p in places:
        await c.press(Ob(act="pl", arg=p))
    await c.press(Ob(act="pldone"))
    if "home_db" in places:
        await c.press(Ob(act="gdone"))
    await c.press(Ob(act="n", arg="3"))
    await c.press(Ob(act="dpreset"))
    await c.press(Ob(act="ddone"))
    await c.say(time_text)
    await c.press(Ob(act="x", arg=extra))
    await c.say(city)
    if skip_country:
        await c.press(Ob(act="cskip"))


async def test_onboarding_creates_profile_and_schedule(rig):
    bot, dp, db, session = rig
    c = Chatter(bot, dp)
    await onboard(c)
    u = await db.get_user(1)
    assert u["onboarded"] == 1 and u["age"] == 15 and u["days"] == "0,2,4"
    assert u["train_time"] == "18:00" and u["tz"] == "Asia/Almaty" and u["place"] == "gym"
    assert u["country"] == "KZ" and u["city"] == "Алматы" and u["goal"] == "muscle"
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
    # первый заход, «легко»: стартовый вес был оценкой, поэтому сразу +шаг, а не +1 повтор
    assert w["set_i"] == 1 and w["cur_weight"] == 45
    # ощущение прямо в тексте закрывает подход сразу
    await c.say("45 11 тяжело")
    w = await db.get_workout(w["id"])
    assert w["set_i"] == 2
    await c.say("/finish")
    assert (await db.get_workout(w["id"]))["status"] == "done"
    assert any("Тренировка завершена" in t for t in session.texts())


async def test_minor_gets_personal_creatine_plan_without_llm(rig):
    bot, dp, db, session = rig
    c = Chatter(bot, dp)
    await onboard(c)
    await c.say("сколько креатина мне принимать?")
    text = "\n".join(session.texts())
    assert "62 кг" in text and "3 г в день" in text and "Скажи родителям" in text
    assert not any(n == "SendChatAction" for n, _ in session.sent)  # ИИ не вызывался


async def test_minor_still_blocked_from_stimulants_and_diets(rig):
    bot, dp, db, session = rig
    c = Chatter(bot, dp)
    await onboard(c)
    await c.say("хочу предтрен и жиросжигатель")
    await c.say("как сесть на диету и похудеть")
    text = "\n".join(session.texts())
    assert "подросткам нельзя" in text and "Диеты, сушку" in text


async def test_owned_supplements_plan_say_what_not_to_take(rig):
    bot, dp, db, session = rig
    c = Chatter(bot, dp)
    await onboard(c)  # 15 лет, 62 кг, цель набор мышц
    await c.say("у меня есть креатин, протеин и предтрен")
    assert set(await db.stack_list(1)) == {"creatine", "whey", "preworkout"}
    text = "\n".join(session.texts())
    assert "Принимай" in text and "3 г в день" in text  # креатин подростку
    assert "Не принимай" in text and "Предтрен" in text  # опасное
    assert "белок за день: 100–120 г" in text.replace("\xa0", " ") or "Весь белок за день" in text
    await c.say("/supp_time 09:30")
    assert (await db.get_user(1))["supp_time"] == "09:30"


async def test_supplement_buttons_toggle_and_plan(rig):
    bot, dp, db, session = rig
    c = Chatter(bot, dp)
    await onboard(c)
    from bot.ui import Sup
    await c.press(Sup(act="tg", id="creatine"))
    await c.press(Sup(act="tg", id="bcaa"))
    assert set(await db.stack_list(1)) == {"creatine", "bcaa"}
    await c.press(Sup(act="tg", id="bcaa"))
    assert await db.stack_list(1) == ["creatine"]
    await c.press(Sup(act="plan"))
    assert any("План приёма добавок" in t for t in session.texts())


async def test_places_multiselect_and_switch_on_day(rig):
    bot, dp, db, session = rig
    c = Chatter(bot, dp)
    await c.say("/start")
    for v in ("15", "62", "175"):
        await c.say(v)
    await c.press(Ob(act="mnone"))
    await finish_onboarding(c, places=("gym", "home_db", "home_bw"))
    u = await db.get_user(1)
    assert u["place"] == "gym" and u["places"] == "gym,home_db,home_bw"
    await c.say("/today")
    w = await db.next_planned_workout(1)
    assert w["place"] == "gym"
    await c.press(Rem(act="p_home_bw", wid=w["id"]))
    w = await db.get_workout(w["id"])
    assert w["place"] == "home_bw" and w["status"] == "planned"


async def test_time_set_as_text_and_changed_from_chat(rig):
    bot, dp, db, session = rig
    c = Chatter(bot, dp)
    await c.say("/start")
    for v in ("15", "62", "175"):
        await c.say(v)
    await c.press(Ob(act="mnone"))
    await finish_onboarding(c, time_text="19:15")
    assert (await db.get_user(1))["train_time"] == "19:15"
    await c.say("напоминай в 20:30")
    assert (await db.get_user(1))["train_time"] == "20:30"
    await c.say("тренируюсь по вторникам, четвергам и субботам")
    u = await db.get_user(1)
    assert u["days"] == "1,3,5" and u["days_per_week"] == 3
    await c.say("во сколько лучше тренироваться?")  # вопрос не меняет настройки
    assert (await db.get_user(1))["train_time"] == "20:30"


async def test_double_tap_on_start_does_not_duplicate_card(rig):
    import asyncio
    bot, dp, db, session = rig
    c = Chatter(bot, dp)
    await onboard(c)
    await c.say("/today")
    w = await db.next_planned_workout(1)
    await asyncio.gather(c.press(Rem(act="go", wid=w["id"])), c.press(Rem(act="go", wid=w["id"])))
    warm = [t for t in session.texts() if "Разминка" in t]
    assert len(warm) == 1, "двойной тап не должен слать вторую разминку"


async def test_pain_message_stops_instead_of_logging(rig):
    bot, dp, db, session = rig
    c = Chatter(bot, dp)
    await onboard(c)
    await c.say("/today")
    w = await db.next_planned_workout(1)
    await c.press(Rem(act="go", wid=w["id"]))
    await c.say("колено болит")
    assert any("Боль в суставе" in t for t in session.texts())
    assert await db.workout_sets(w["id"]) == []


async def test_one_message_per_set_and_text_feel(rig):
    bot, dp, db, session = rig
    c = Chatter(bot, dp)
    await onboard(c)
    await c.say("/today")
    w = await db.next_planned_workout(1)
    await c.press(Rem(act="go", wid=w["id"]))
    await c.press(Wk(act="warm", wid=w["id"]))
    n0 = len([1 for n, _ in session.sent if n == "SendMessage"])
    await c.say("40 10")          # 1 сообщение: «Как далось?»
    await c.say("нормально")      # 1 сообщение: итог + подсказка + следующий подход вместе
    n1 = len([1 for n, _ in session.sent if n == "SendMessage"])
    assert n1 - n0 == 2
    w = await db.get_workout(w["id"])
    assert w["set_i"] == 1


async def test_photo_instead_of_tape_in_onboarding(rig):
    bot, dp, db, session = rig
    c = Chatter(bot, dp)
    await onboard(c, method="photo")
    assert any("Как снимать" in t for t in session.texts())
    for n in range(1, 5):
        await c.photo(n)
    await finish_onboarding(c)
    u = await db.get_user(1)
    assert u["onboarded"] == 1
    days = await db.progress_days(1)
    media = await db.progress_media_on(1, days[0])
    assert [m["label"] for m in media] == ["front", "side", "back", "flex"]
    assert (await db.measurement_series(1)).keys() == {"weight"}  # ленты не было, вес записан


async def test_video_note_replaces_four_photos_and_progress_shows_it(rig):
    bot, dp, db, session = rig
    c = Chatter(bot, dp)
    await onboard(c, method="photo")
    await c.send(video_note=VideoNote(file_id="vn1", file_unique_id="vu1", length=240, duration=15))
    await finish_onboarding(c)
    media = await db.progress_media_on(1, (await db.progress_days(1))[0])
    assert [(m["kind"], m["file_id"]) for m in media] == [("video_note", "vn1")]
    await c.say("/progress")
    assert any(n == "SendVideoNote" for n, _ in session.sent)


async def test_photo_step_can_be_skipped_and_delete_removes_media(rig):
    bot, dp, db, session = rig
    c = Chatter(bot, dp)
    await onboard(c, method="photo")
    await c.photo(1)
    await c.press(Ob(act="pdone"))
    await finish_onboarding(c)
    assert len(await db.progress_media_on(1, (await db.progress_days(1))[0])) == 1
    await db.delete_user(1)
    assert await db.progress_days(1) == []


async def test_onboarding_without_any_measurements(rig):
    bot, dp, db, session = rig
    c = Chatter(bot, dp)
    await onboard(c, method="none")
    assert (await db.get_user(1))["onboarded"] == 1
    assert await db.progress_days(1) == []


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


class StubLLM:
    """ИИ-заглушка: возвращает заранее заданный ответ, наружу не ходит."""
    available = True
    gemini = object()
    groq = None

    def __init__(self, reply: str = "ок"):
        self.reply = reply
        self.calls: list[str] = []

    async def chat(self, system, history, user_text, media=None, max_tokens=700, temperature=0.6):
        self.calls.append(user_text)
        return self.reply

    async def classify_feel(self, text):
        return None, False

    async def extract_gear(self, text):
        return None

    async def extract_set(self, text):
        return None, None


async def start_to_places(c: Chatter, goal_text: str | None = None):
    await c.say("/start")
    for v in ("15", "62", "175"):
        await c.say(v)
    await c.press(Ob(act="mnone"))
    if goal_text:
        await c.press(Ob(act="goal_text"))
        await c.say(goal_text)
    else:
        await c.press(Ob(act="goal", arg="muscle"))
        await c.press(Ob(act="goal2", arg="-"))
    await c.press(Ob(act="level", arg="new"))


async def test_home_gear_written_in_own_words_shapes_the_program(rig):
    bot, dp, db, session = rig
    c = Chatter(bot, dp)
    await start_to_places(c)
    await c.press(Ob(act="pl", arg="home_db"))
    await c.press(Ob(act="pldone"))
    await c.say("одна гантель 5 кг, турник, резинка 25 кг, эспандер 25 кг")
    assert any("Записал" in t and "Турник" in t for t in session.texts())
    await c.press(Ob(act="gdone"))
    await c.press(Ob(act="n", arg="3"))
    await c.press(Ob(act="dpreset"))
    await c.press(Ob(act="ddone"))
    await c.say("18:00")
    await c.press(Ob(act="x", arg="0"))
    await c.say("Москва")
    u = await db.get_user(1)
    from bot.services.equipment import load_gear
    gear = {i["id"]: i for i in load_gear(u)["items"]}
    assert gear["dumbbell"]["weights"] == [5.0] and gear["band"]["weights"] == [25.0] and "pullup_bar" in gear
    await c.say("/plan")
    plan_text = "\n".join(session.texts())
    assert "Жим ногами" not in plan_text and "тренажёре" not in plan_text.split("Ближайшая тренировка")[-1]


async def test_goal_written_by_user_sets_goals(rig):
    bot, dp, db, session = rig
    c = Chatter(bot, dp)
    await start_to_places(c, goal_text="хочу рельеф и выносливость")
    await finish_places_and_schedule(c)
    u = await db.get_user(1)
    assert u["goal"] == "definition" and u["goal2"] == "endurance"


async def finish_places_and_schedule(c: Chatter, extra="0", city="Алматы"):
    await c.press(Ob(act="pl", arg="gym"))
    await c.press(Ob(act="pldone"))
    await c.press(Ob(act="n", arg="3"))
    await c.press(Ob(act="dpreset"))
    await c.press(Ob(act="ddone"))
    await c.say("18:00")
    await c.press(Ob(act="x", arg=extra))
    await c.say(city)


async def test_extra_cardio_days_get_workout_and_reminder(rig):
    from datetime import date, timedelta

    bot, dp, db, session = rig
    c = Chatter(bot, dp)
    await start_to_places(c, goal_text="выносливость")
    await finish_places_and_schedule(c, extra="2")
    u = await db.get_user(1)
    extra_days = [int(x) for x in u["cardio_days"].split(",")]
    assert len(extra_days) == 2 and not set(extra_days) & {0, 2, 4}
    jobs = [j.id for j in (await _jobs(dp))]
    assert "wk:1:cardio" in jobs
    d = date.today()
    while d.weekday() != extra_days[0]:
        d += timedelta(days=1)
    w = await dp.workflow_data["workouts"].ensure_planned(u, d)
    assert w["day_name"] == "Кардио" and w["plan"][0]["kind"] == "cardio"


async def test_warmup_comes_first_and_stretch_after_workout(rig):
    bot, dp, db, session = rig
    c = Chatter(bot, dp)
    await onboard(c)
    await c.say("/today")
    w = await db.next_planned_workout(1)
    await c.press(Rem(act="go", wid=w["id"]))
    assert any("Разминка" in t for t in session.texts())
    await c.press(Wk(act="warm", wid=w["id"]))
    await c.say("40 10 нормально")
    await c.say("/finish")
    assert any("Растяжка" in t for t in session.texts())
    assert any("Разбор тренера" in t for t in session.texts())


async def test_settings_gear_flow_rebuilds_today(rig):
    bot, dp, db, session = rig
    c = Chatter(bot, dp)
    await onboard(c)
    from bot.ui import St
    await c.press(St(act="place"))
    await c.press(Ob(act="pl", arg="home_db"))
    await c.press(Ob(act="pl", arg="gym"))   # зал снимаем
    await c.press(Ob(act="pldone"))
    await c.say("две гантели по 8 кг, скакалка")
    await c.press(Ob(act="gdone"))
    u = await db.get_user(1)
    assert u["place"] == "home_db" and "скакалк" in " ".join(session.texts()).lower()
    assert "8" in u["equipment"]


async def test_supplement_label_flow_by_text_and_price(rig):
    bot, dp, db, session = rig
    c = Chatter(bot, dp)
    await onboard(c)
    stub = StubLLM(json.dumps({
        "readable": True, "kind": "creatine", "brand": "Optimum Nutrition", "product": "Micronized Creatine",
        "serving_g": 5, "servings": 120, "net_g": 600, "per_serving": {"creatine_g": 5}, "certs": ["Informed Sport"],
        "expiry_visible": True, "importer_visible": True, "proprietary_blend": False, "stimulants": [], "claims": []}))
    dp.workflow_data["llm"] = stub
    from bot.ui import Sup
    await c.press(Sup(act="have"))
    await c.say("Optimum Nutrition Micronized Creatine, порция 5 г, креатин 5 г, 120 порций")
    text = "\n".join(session.texts())
    assert "Нормально" in text and "Informed Sport" in text and "3 г" in text
    products = await db.products(1)
    assert len(products) == 1 and products[0]["supp_id"] == "creatine"
    assert "creatine" in await db.stack_list(1)
    await c.press(Sup(act="price", id=str(products[0]["id"])))
    await c.say("12 000")
    assert any("в день" in t for t in session.texts())
    assert (await db.products(1))[0]["price"] == 12000


async def test_label_photos_are_collected_before_analysis(rig):
    bot, dp, db, session = rig
    c = Chatter(bot, dp)
    await onboard(c)
    from bot.ui import Sup
    await c.press(Sup(act="have"))
    await c.photo(1)
    await c.photo(2)
    assert any("Принял 2 из 4" in t for t in session.texts())
    await c.press(Sup(act="cancel"))
    await c.photo(3)  # после отмены фото уходит в обычный разбор, а не в добавки
    assert not any("Принял 3 из 4" in t for t in session.texts())


async def test_buy_flow_asks_country_then_gives_marketplaces(rig):
    bot, dp, db, session = rig
    c = Chatter(bot, dp)
    await c.say("/start")
    for v in ("15", "62", "175"):
        await c.say(v)
    await c.press(Ob(act="mnone"))
    await c.press(Ob(act="goal", arg="muscle"))
    await c.press(Ob(act="goal2", arg="-"))
    await c.press(Ob(act="level", arg="new"))
    await finish_places_and_schedule(c, city="+5")
    await c.press(Ob(act="cskip"))
    assert (await db.get_user(1))["country"] is None
    from bot.ui import Sup
    await c.press(Sup(act="b1", id="creatine"))
    await c.press(Sup(act="b2", id="creatine|fit"))
    assert any("где ты живёшь" in t for t in session.texts())
    await c.say("Казахстан")
    text = "\n".join(session.texts())
    assert "Kaspi.kz" in text and "Подходящее" in text
    assert (await db.get_user(1))["country"] == "KZ"


async def test_supplement_reminders_split_by_time_slot(rig):
    bot, dp, db, session = rig
    c = Chatter(bot, dp)
    await onboard(c)
    for sid in ("creatine", "whey", "vitd"):
        await db.stack_add(1, sid)
    reminders = dp.workflow_data["reminders"]
    await db.update_user(1, days="0,1,2,3,4,5,6", supp_remind=1)  # тренировка каждый день
    await reminders.send_supp_reminder(1, "am")
    await reminders.send_supp_reminder(1, "post")
    texts = session.texts()
    am = next(t for t in texts if "Добавки с едой" in t)
    post = next(t for t in texts if "После тренировки" in t)
    assert "Витамин D" in am and "Креатин" not in am
    assert "Сывороточный протеин" in post and "Креатин" in post
    await db.update_user(1, days="")
    await reminders.send_supp_reminder(1, "am")
    assert any("Креатин" in t for t in session.texts() if "Добавки с едой" in t)
    jobs = [j.id for j in (await _jobs(dp))]
    reminders.schedule_user(await db.get_user(1))
    jobs = {j.id for j in reminders.scheduler.get_jobs()}
    assert "sp:1:am" in jobs
