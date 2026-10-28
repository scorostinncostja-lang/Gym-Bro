"""Анкета, замеры и перенастройка расписания (FSM)."""
import html

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot.data import PLACES
from bot.db.database import Database
from bot.services.program_builder import template_for, build_day
from bot.services.reminders import Reminders
from bot.services.timeutil import (
    TZ_PRESETS, WEEKDAYS_RU, fmt_days, local_today, next_training, parse_time, parse_tz,
)
from bot.services.workout import describe_item
from bot.ui import Ob, St, inline, main_menu

router = Router()
router.message.filter(F.chat.type == "private")

MEASURES = [
    ("neck", "шея", "по середине шеи, лента не давит"),
    ("chest", "грудь", "на уровне сосков, на спокойном выдохе"),
    ("biceps", "бицепс", "рука напряжена, самое широкое место"),
    ("forearm", "предплечье", "самое широкое место, рука расслаблена"),
    ("waist", "талия", "на уровне пупка, на выдохе, живот не втягивай"),
    ("thigh", "бедро", "самое широкое место у верха ноги"),
]
MEASURE_LABELS = {k: n for k, n, _ in MEASURES} | {"weight": "вес"}
MEASURE_UNITS = {"weight": "кг"}

GOALS = {"muscle": "💪 Набрать мышцы", "strength": "🏋️ Стать сильнее", "fit": "🤸 Общая форма и здоровье", "lose": "🔥 Похудеть"}
LEVELS = {"new": "Новичок", "some": "Занимаюсь 3–12 месяцев"}
PRESET_DAYS = {2: [0, 3], 3: [0, 2, 4], 4: [0, 1, 3, 4]}
TIMES = ["1600", "1700", "1800", "1900", "2000"]


class Onb(StatesGroup):
    age = State()
    weight = State()
    height = State()
    measure = State()
    goal = State()
    level = State()
    place = State()
    days_n = State()
    days_pick = State()
    time = State()
    tz = State()


def _num(text: str) -> float | None:
    try:
        return float(text.strip().replace(",", "."))
    except ValueError:
        return None


# ---------- /start ----------
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, db: Database):
    await state.clear()
    user = await db.ensure_user(message.from_user.id, message.from_user.first_name)
    if user["onboarded"]:
        await message.answer(
            "Ты уже в системе. Кнопки внизу: тренировка, план, прогресс, добавки, настройки. "
            "Помощь: /help",
            reply_markup=main_menu(),
        )
        return
    await state.set_data({"mode": "onb"})
    await message.answer(
        "Я Gym Bro. Соберу программу под твой инвентарь, буду напоминать о тренировках, "
        "считать прогрессию и отвечать на вопросы по технике.\n\n"
        "Анкета займёт 3 минуты. Лучше заранее взять мягкую ленту (сантиметр): "
        "понадобятся замеры.\n\n<b>Сколько тебе лет?</b>"
    )
    await state.set_state(Onb.age)


@router.message(Onb.age)
async def on_age(message: Message, state: FSMContext):
    n = _num(message.text or "")
    if n is None or not 5 <= n <= 90:
        await message.answer("Напиши возраст числом, например 15.")
        return
    age = int(n)
    if age < 13:
        await message.answer(
            "Бот для 13+. Для тебя сейчас важнее играть, бегать и заниматься физкультурой с "
            "тренером или родителями. Возвращайся, когда исполнится 13."
        )
        await state.clear()
        return
    await state.update_data(age=age)
    if age < 18:
        await message.answer(
            "Тебе нет 18, поэтому у меня особые правила: никаких диет, доз добавок и "
            "работы на максимум. Техника и регулярность важнее веса. Первые 2–3 тренировки "
            "лучше сходить с тренером или взрослым, который покажет базовые движения."
        )
    await message.answer("<b>Вес в кг?</b> (например 62 или 62,5)")
    await state.set_state(Onb.weight)


@router.message(Onb.weight)
async def on_weight(message: Message, state: FSMContext):
    n = _num(message.text or "")
    if n is None or not 25 <= n <= 250:
        await message.answer("Вес в кг, число от 25 до 250.")
        return
    await state.update_data(weight=n)
    data = await state.get_data()
    if data.get("mode") == "onb":
        await message.answer("<b>Рост в см?</b>")
        await state.set_state(Onb.height)
    else:
        await _start_measures(message, state)


@router.message(Onb.height)
async def on_height(message: Message, state: FSMContext):
    n = _num(message.text or "")
    if n is None or not 120 <= n <= 230:
        await message.answer("Рост в см, число от 120 до 230.")
        return
    await state.update_data(height=n)
    await _start_measures(message, state)


# ---------- замеры ----------
async def _start_measures(message: Message, state: FSMContext):
    await state.update_data(m_idx=0, m_vals={})
    await message.answer(
        "Теперь замеры лентой. Делай утром, расслабленно, ленту не затягивай. "
        "Любой замер можно пропустить."
    )
    await _ask_measure(message, state)


async def _ask_measure(message: Message, state: FSMContext):
    data = await state.get_data()
    _, name, how = MEASURES[data["m_idx"]]
    await state.set_state(Onb.measure)
    await message.answer(
        f"<b>{name.capitalize()}, см</b>\n{how}.",
        reply_markup=inline([[("Пропустить", Ob(act="mskip"))]]),
    )


async def _save_measure(message: Message, state: FSMContext, value: float | None, db: Database, rem: Reminders):
    data = await state.get_data()
    idx = data["m_idx"]
    vals = dict(data["m_vals"])
    if value is not None:
        vals[MEASURES[idx][0]] = value
    idx += 1
    await state.update_data(m_idx=idx, m_vals=vals)
    if idx < len(MEASURES):
        await _ask_measure(message, state)
        return
    if data["mode"] == "onb":
        await _ask_goal(message, state)
    else:
        await _finish_measure_update(message, state, db)


@router.message(Onb.measure)
async def on_measure(message: Message, state: FSMContext, db: Database, reminders: Reminders):
    n = _num(message.text or "")
    if n is None or not 10 <= n <= 200:
        await message.answer("Число в сантиметрах (10–200) или кнопка «Пропустить».")
        return
    await _save_measure(message, state, n, db, reminders)


@router.callback_query(Ob.filter(F.act == "mskip"), Onb.measure)
async def on_measure_skip(cb: CallbackQuery, state: FSMContext, db: Database, reminders: Reminders):
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    await _save_measure(cb.message, state, None, db, reminders)


@router.message(Command("measure"))
async def cmd_measure(message: Message, state: FSMContext, db: Database):
    user = await db.get_user(message.from_user.id)
    if not user or not user["onboarded"]:
        await message.answer("Сначала анкета: /start")
        return
    await state.set_data({"mode": "upd"})
    await message.answer("Обновим замеры. <b>Вес сегодня в кг?</b>")
    await state.set_state(Onb.weight)


async def _finish_measure_update(message: Message, state: FSMContext, db: Database):
    data = await state.get_data()
    user = await db.get_user(message.chat.id)
    assert user
    today = local_today(user["tz"]).isoformat()
    values = {"weight": data["weight"], **data["m_vals"]}
    prev = await db.measurement_series(user["tg_id"])
    await db.add_measurements(user["tg_id"], values, today)
    await db.update_user(user["tg_id"], weight_kg=data["weight"])
    lines = []
    for kind, val in values.items():
        label = MEASURE_LABELS[kind]
        unit = MEASURE_UNITS.get(kind, "см")
        old = prev.get(kind)
        if old:
            diff = val - old[-1][1]
            sign = "+" if diff > 0 else ""
            lines.append(f"{label}: {val:g} {unit} ({sign}{diff:g} с {old[-1][0]})")
        else:
            lines.append(f"{label}: {val:g} {unit}")
    await state.clear()
    await message.answer("Записал:\n" + "\n".join(lines) + "\n\nИзменения замеров заметны через 4–8 недель. "
                         "Не гоняйся за ними каждый день.", reply_markup=main_menu())


# ---------- цель, уровень, место ----------
async def _ask_goal(message: Message, state: FSMContext):
    data = await state.get_data()
    keys = [k for k in GOALS if k != "lose" or data["age"] >= 18]
    await state.set_state(Onb.goal)
    await message.answer(
        "<b>Главная цель?</b>",
        reply_markup=inline([[(GOALS[k], Ob(act="goal", arg=k))] for k in keys]),
    )


@router.callback_query(Ob.filter(F.act == "goal"), Onb.goal)
async def on_goal(cb: CallbackQuery, callback_data: Ob, state: FSMContext):
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    await state.update_data(goal=callback_data.arg)
    await state.set_state(Onb.level)
    await cb.message.answer(
        "<b>Опыт в зале?</b>",
        reply_markup=inline([[(v, Ob(act="level", arg=k))] for k, v in LEVELS.items()]),
    )


@router.callback_query(Ob.filter(F.act == "level"), Onb.level)
async def on_level(cb: CallbackQuery, callback_data: Ob, state: FSMContext):
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    await state.update_data(level=callback_data.arg)
    await state.set_state(Onb.place)
    await cb.message.answer(
        "<b>Где будешь тренироваться?</b> Дома можно переключиться на любой тренировке.",
        reply_markup=inline([[(v, Ob(act="place", arg=k))] for k, v in PLACES.items()]),
    )


@router.callback_query(Ob.filter(F.act == "place"), Onb.place)
async def on_place(cb: CallbackQuery, callback_data: Ob, state: FSMContext):
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    await state.update_data(place=callback_data.arg)
    await _ask_days_n(cb.message, state)


# ---------- расписание ----------
async def _ask_days_n(message: Message, state: FSMContext):
    await state.set_state(Onb.days_n)
    await message.answer(
        "<b>Сколько тренировок в неделю?</b>\nДля новичка 3 — оптимум: между ними день отдыха, "
        "мышцы растут именно в отдых.",
        reply_markup=inline([[("2", Ob(act="n", arg="2")), ("3 (рекомендую)", Ob(act="n", arg="3")), ("4", Ob(act="n", arg="4"))]]),
    )


def _days_keyboard(selected: set[int], n: int):
    rows = [[(("✅ " if i in selected else "") + WEEKDAYS_RU[i], Ob(act="d", arg=str(i))) for i in range(4)],
            [(("✅ " if i in selected else "") + WEEKDAYS_RU[i], Ob(act="d", arg=str(i))) for i in range(4, 7)]]
    rows.append([(f"Предложить: {fmt_days(PRESET_DAYS[n])}", Ob(act="dpreset"))])
    if len(selected) == n:
        rows.append([("Готово", Ob(act="ddone"))])
    return inline(rows)


@router.callback_query(Ob.filter(F.act == "n"), Onb.days_n)
async def on_days_n(cb: CallbackQuery, callback_data: Ob, state: FSMContext):
    await cb.answer()
    n = int(callback_data.arg)
    await state.update_data(days_per_week=n, days=[])
    await state.set_state(Onb.days_pick)
    await cb.message.edit_text(
        f"<b>Выбери {n} дн.</b> Лучше с днём отдыха между тренировками.",
        reply_markup=_days_keyboard(set(), n),
    )


@router.callback_query(Ob.filter(F.act.in_({"d", "dpreset"})), Onb.days_pick)
async def on_day_toggle(cb: CallbackQuery, callback_data: Ob, state: FSMContext):
    data = await state.get_data()
    n = data["days_per_week"]
    selected = set(data["days"])
    if callback_data.act == "dpreset":
        selected = set(PRESET_DAYS[n])
    else:
        d = int(callback_data.arg)
        if d in selected:
            selected.remove(d)
        elif len(selected) < n:
            selected.add(d)
        else:
            await cb.answer(f"Уже выбрано {n}. Сними один день.", show_alert=False)
            return
    await state.update_data(days=sorted(selected))
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=_days_keyboard(selected, n))


@router.callback_query(Ob.filter(F.act == "ddone"), Onb.days_pick)
async def on_days_done(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if len(data["days"]) != data["days_per_week"]:
        await cb.answer("Выбери нужное число дней.", show_alert=True)
        return
    await cb.answer()
    days = sorted(data["days"])
    await cb.message.edit_text(f"Дни: {fmt_days(days)}")
    warn = ""
    if data["days_per_week"] >= 3 and any(b - a == 1 for a, b in zip(days, days[1:])):
        warn = "\nДва дня подряд — тяжелее восстановиться, но можно."
    await state.set_state(Onb.time)
    await cb.message.answer(
        "<b>Во сколько напоминать?</b> Кнопкой или напиши время, например 17:30." + warn,
        reply_markup=inline([[(f"{t[:2]}:{t[2:]}", Ob(act="t", arg=t)) for t in TIMES]]),
    )


async def _after_time(message: Message, state: FSMContext, time_str: str, db: Database, rem: Reminders):
    data = await state.get_data()
    await state.update_data(train_time=time_str)
    if data["mode"] == "sched":
        user = await db.get_user(message.chat.id)
        assert user
        await db.update_user(user["tg_id"], days_per_week=data["days_per_week"],
                             days=",".join(map(str, data["days"])), train_time=time_str, rotation_idx=0)
        rem.schedule_user((await db.get_user(user["tg_id"])))  # type: ignore[arg-type]
        await state.clear()
        await message.answer("Расписание обновлено. Напомню вовремя.", reply_markup=main_menu())
        return
    await _ask_tz(message, state)


@router.callback_query(Ob.filter(F.act == "t"), Onb.time)
async def on_time_btn(cb: CallbackQuery, callback_data: Ob, state: FSMContext, db: Database, reminders: Reminders):
    await cb.answer()
    t = callback_data.arg
    await cb.message.edit_reply_markup(reply_markup=None)
    await _after_time(cb.message, state, f"{t[:2]}:{t[2:]}", db, reminders)


@router.message(Onb.time)
async def on_time_text(message: Message, state: FSMContext, db: Database, reminders: Reminders):
    t = parse_time(message.text or "")
    if not t:
        await message.answer("Время вида 18:30.")
        return
    await _after_time(message, state, t, db, reminders)


# ---------- часовой пояс ----------
async def _ask_tz(message: Message, state: FSMContext):
    await state.set_state(Onb.tz)
    rows = [[(name, Ob(act="tz", arg=str(i)))] for i, (name, _) in enumerate(TZ_PRESETS)]
    await message.answer(
        "<b>Часовой пояс?</b> Нужен, чтобы напоминания приходили вовремя. Выбери из списка или "
        "напиши смещение, например +4.",
        reply_markup=inline(rows),
    )


async def _after_tz(message: Message, state: FSMContext, tz: str, db: Database, rem: Reminders):
    data = await state.get_data()
    if data["mode"] == "tz":
        user = await db.get_user(message.chat.id)
        assert user
        await db.update_user(user["tg_id"], tz=tz)
        rem.schedule_user((await db.get_user(user["tg_id"])))  # type: ignore[arg-type]
        await state.clear()
        await message.answer(f"Часовой пояс: {tz}.", reply_markup=main_menu())
        return
    await _finalize(message, state, tz, db, rem)


@router.callback_query(Ob.filter(F.act == "tz"), Onb.tz)
async def on_tz_btn(cb: CallbackQuery, callback_data: Ob, state: FSMContext, db: Database, reminders: Reminders):
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    await _after_tz(cb.message, state, TZ_PRESETS[int(callback_data.arg)][1], db, reminders)


@router.message(Onb.tz)
async def on_tz_text(message: Message, state: FSMContext, db: Database, reminders: Reminders):
    tz = parse_tz(message.text or "")
    if not tz:
        await message.answer("Не понял. Напиши смещение (+3, -5) или название вида Europe/Moscow.")
        return
    await _after_tz(message, state, tz, db, reminders)


@router.callback_query(St.filter(F.act == "tz"))
async def settings_tz(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_data({"mode": "tz"})
    await _ask_tz(cb.message, state)


@router.callback_query(St.filter(F.act == "sched"))
async def settings_sched(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_data({"mode": "sched"})
    await _ask_days_n(cb.message, state)


# ---------- финал анкеты ----------
async def _finalize(message: Message, state: FSMContext, tz: str, db: Database, rem: Reminders):
    data = await state.get_data()
    tg_id = message.chat.id
    days = data["days"]
    await db.update_user(
        tg_id, age=data["age"], weight_kg=data["weight"], height_cm=data["height"], goal=data["goal"],
        level=data["level"], place=data["place"], days_per_week=data["days_per_week"],
        days=",".join(map(str, days)), train_time=data["train_time"], tz=tz, rotation_idx=0, onboarded=1,
    )
    await db.add_measurements(tg_id, {"weight": data["weight"], **data["m_vals"]}, local_today(tz).isoformat())
    user = await db.get_user(tg_id)
    assert user
    rem.schedule_user(user)
    await state.clear()

    tpl = template_for(user["days_per_week"], 0)
    plan = build_day(tpl, user["place"])
    nxt = next_training(days, user["train_time"], tz)
    when = f"{WEEKDAYS_RU[nxt.weekday()]} {nxt:%d.%m} в {nxt:%H:%M}" if nxt else "—"
    items = "\n".join(f"• {html.escape(describe_item(i.to_dict()))}" for i in plan)
    await message.answer(
        f"Готово. Программа: {user['days_per_week']} трен./нед ({fmt_days(days)} в {user['train_time']}), "
        f"{PLACES[user['place']]}.\n\n<b>Первая тренировка — {html.escape(tpl['name'])}</b>\n{items}\n\n"
        f"Напомню: {when}. Хочешь начать раньше — кнопка «Тренировка».\n"
        "На первой тренировке вес подбирается легко. Я подскажу, как.",
        reply_markup=main_menu(),
    )


@router.callback_query(St.filter(F.act == "meas"))
async def settings_meas(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_data({"mode": "upd"})
    await cb.message.answer("Обновим замеры. <b>Вес сегодня в кг?</b>")
    await state.set_state(Onb.weight)
