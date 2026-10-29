"""Анкета, замеры и перенастройка расписания (FSM)."""
import asyncio
import html

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot.data import PLACES, equipment_catalog, goals_data
from bot.photo_guide import ASK_METHOD, GUIDE, SHOT_NAMES, SHOTS, next_shot_text
from bot.db.database import Database
from bot.services import goals as goals_svc
from bot.services.equipment import dump_gear, empty_gear, load_gear, merge_gear, parse_gear_text, render_gear
from bot.services.llm import LLM
from bot.services.location import COUNTRY_NAME, Location, parse_location
from bot.services.places import ORDER, places_label, primary_place, user_places
from bot.services.program_builder import template_for, build_day
from bot.services.reminders import Reminders
from bot.services.timeutil import (
    WEEKDAYS_RU, fmt_days, local_today, next_training, parse_days, parse_time, pick_extra_days,
)
from bot.services.workout import WorkoutService, describe_item
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

GOALS = {k: g["name"] for k, g in goals_data().items()}
LEVELS = {"new": "Новичок", "some": "Занимаюсь 3–12 месяцев"}
PRESET_DAYS = {2: [0, 3], 3: [0, 2, 4], 4: [0, 1, 3, 4]}


class Onb(StatesGroup):
    age = State()
    weight = State()
    height = State()
    method = State()
    measure = State()
    photos = State()
    goal = State()
    level = State()
    place = State()
    days_n = State()
    days_pick = State()
    time = State()
    extra = State()
    tz = State()
    country = State()
    goal_text = State()
    gear = State()
    gear_detail = State()


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
        "Анкета займёт 3 минуты. Замеры тела можно сделать лентой, фото или кружком, "
        "а можно пропустить: лента не обязательна.\n\n<b>Сколько тебе лет?</b>"
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
            "Тебе нет 18, поэтому у меня особые правила: никаких диет, стимуляторов и работы на "
            "максимум. Из добавок только безопасные и в малых дозах (/supplements). Техника и "
            "регулярность важнее веса. Первые 2–3 тренировки лучше сходить с тренером или "
            "взрослым, который покажет базовые движения."
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
_media_lock = asyncio.Lock()


def _skip_kb(text: str = "⏭ Пропустить"):
    return inline([[(text, Ob(act="pdone"))]])


async def _start_measures(message: Message, state: FSMContext):
    await state.update_data(m_idx=0, m_vals={}, p_media=[])
    await state.set_state(Onb.method)
    await message.answer(
        ASK_METHOD,
        reply_markup=inline([
            [("📏 Лентой", Ob(act="mtape"))],
            [("📸 Фото или кружок", Ob(act="mphoto"))],
            [("⏭ Пропущу", Ob(act="mnone"))],
        ]),
    )


@router.callback_query(Ob.filter(F.act == "mtape"), Onb.method)
async def on_method_tape(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(
        "Замеры лентой. Делай утром, расслабленно, ленту не затягивай. Любой замер можно пропустить."
    )
    await _ask_measure(cb.message, state)


@router.callback_query(Ob.filter(F.act == "mphoto"), Onb.method)
async def on_method_photo(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    await state.set_state(Onb.photos)
    await cb.message.answer(GUIDE)
    await cb.message.answer(
        "Пришли <b>4 фото</b> по порядку или один <b>кружок</b>. Не получилось сейчас — пропусти, "
        "можно потом через /measure.\n\n" + next_shot_text(0),
        reply_markup=_skip_kb(),
    )


@router.callback_query(Ob.filter(F.act == "mnone"), Onb.method)
async def on_method_none(cb: CallbackQuery, state: FSMContext, db: Database):
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    await _after_measures(cb.message, state, db)


async def _after_measures(message: Message, state: FSMContext, db: Database):
    data = await state.get_data()
    if data["mode"] == "onb":
        await _ask_goal(message, state)
    else:
        await _finish_measure_update(message, state, db)


@router.message(Onb.photos, F.photo | F.video_note | F.video)
async def on_progress_media(message: Message, state: FSMContext, db: Database):
    # Альбом приходит пачкой апдейтов: без замка счётчик кадров сбивается.
    async with _media_lock:
        media = list((await state.get_data()).get("p_media", []))
        if message.photo:
            if len(media) >= len(SHOTS) or (media and media[0][0] != "photo"):
                return
            label = SHOTS[len(media)][0]
            media.append(["photo", label, message.photo[-1].file_id])
            await state.update_data(p_media=media)
            if len(media) < len(SHOTS):
                await message.answer(
                    f"✔ Принял: {SHOT_NAMES[label]}.\n{next_shot_text(len(media))}",
                    reply_markup=_skip_kb("Хватит, продолжить"),
                )
                return
            await message.answer("✔ Все 4 кадра приняты.")
        else:
            clip = message.video_note or message.video
            kind = "video_note" if message.video_note else "video"
            await state.update_data(p_media=[[kind, "clip", clip.file_id]])
            await message.answer("✔ Видео принял.")
        await _after_measures(message, state, db)


@router.callback_query(Ob.filter(F.act == "pdone"), Onb.photos)
async def on_photos_done(cb: CallbackQuery, state: FSMContext, db: Database):
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    async with _media_lock:
        await _after_measures(cb.message, state, db)


@router.message(Onb.photos, F.text, ~F.text.startswith("/"))
async def on_photos_text(message: Message):
    await message.answer(
        "Жду фото или кружок. Нет возможности сейчас — нажми «Пропустить».", reply_markup=_skip_kb()
    )


async def _ask_measure(message: Message, state: FSMContext):
    data = await state.get_data()
    _, name, how = MEASURES[data["m_idx"]]
    await state.set_state(Onb.measure)
    await message.answer(
        f"<b>{name.capitalize()}, см</b>\n{how}.",
        reply_markup=inline([[("Пропустить", Ob(act="mskip"))]]),
    )


async def _save_measure(message: Message, state: FSMContext, value: float | None, db: Database):
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
    await _after_measures(message, state, db)


@router.message(Onb.measure)
async def on_measure(message: Message, state: FSMContext, db: Database):
    n = _num(message.text or "")
    if n is None or not 10 <= n <= 200:
        await message.answer("Число в сантиметрах (10–200) или кнопка «Пропустить».")
        return
    await _save_measure(message, state, n, db)


@router.callback_query(Ob.filter(F.act == "mskip"), Onb.measure)
async def on_measure_skip(cb: CallbackQuery, state: FSMContext, db: Database):
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    await _save_measure(cb.message, state, None, db)


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
    media = data.get("p_media") or []
    if media:
        await db.add_progress_media(user["tg_id"], today, [tuple(m) for m in media])
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
    photos = "\nФото сохранил: /progress." if media else ""
    await message.answer("Записал:\n" + "\n".join(lines) + photos + "\n\nИзменения заметны через 4–8 недель. "
                         "Не гоняйся за ними каждый день.", reply_markup=main_menu())


# ---------- цель, уровень, место ----------
def _goals_kb(keys: list[str], chosen: list[str]):
    rows = []
    for k in keys:
        mark = f"{chosen.index(k) + 1}. " if k in chosen else ""
        rows.append([(f"{'✅ ' if k in chosen else ''}{mark}{GOALS[k]}", Ob(act="gl", arg=k))])
    rows.append([("✍️ Написать самому", Ob(act="goal_text"))])
    if chosen:
        rows.append([("Готово ➡", Ob(act="gldone"))])
    return inline(rows)


async def _ask_goal(message: Message, state: FSMContext):
    data = await state.get_data()
    keys = goals_svc.goal_keys(data.get("age"))
    chosen = [g for g in data.get("goals", []) if g in keys]
    await state.update_data(goals=chosen)
    await state.set_state(Onb.goal)
    await message.answer(
        "<b>Какие у тебя цели?</b> Отметь сколько угодно, порядок нажатия = приоритет. Или напиши своими "
        "словами: «сила, объём, выносливость, рельеф». От целей зависят повторы, кардио и растяжка.",
        reply_markup=_goals_kb(keys, chosen),
    )


async def _after_goals(message: Message, state: FSMContext, db: Database, workouts: WorkoutService | None = None):
    """Цели выбраны: в анкете идём к уровню, в настройках сохраняем."""
    data = await state.get_data()
    chosen = data["goals"]
    summary = (
        f"Цели: {goals_svc.goals_label(chosen)}.\n{goals_svc.goal_about(chosen[0])}"
        + (f"\n\n{note}" if (note := goals_svc.conflict_note(chosen, data.get("age"))) else "")
    )
    fields = {"goals": ",".join(chosen), "goal": chosen[0], "goal2": chosen[1] if len(chosen) > 1 else None}
    if data.get("mode") == "goal":
        await db.update_user(message.chat.id, **fields)
        user = await db.get_user(message.chat.id)
        if user and workouts:
            await workouts.refresh_planned(user)
        await state.clear()
        await message.answer(summary + "\n\nНовые тренировки уже под них: /plan", reply_markup=main_menu())
        return
    await state.update_data(goals=chosen, goal=fields["goal"], goal2=fields["goal2"])  # в анкете храним список
    await message.answer(summary)
    await state.set_state(Onb.level)
    await message.answer(
        "<b>Опыт в зале?</b>",
        reply_markup=inline([[(v, Ob(act="level", arg=k))] for k, v in LEVELS.items()]),
    )


@router.callback_query(Ob.filter(F.act == "gl"), Onb.goal)
async def on_goal_toggle(cb: CallbackQuery, callback_data: Ob, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    chosen = list(data.get("goals", []))
    if callback_data.arg in chosen:
        chosen.remove(callback_data.arg)
    else:
        chosen.append(callback_data.arg)
    await state.update_data(goals=chosen)
    await cb.message.edit_reply_markup(reply_markup=_goals_kb(goals_svc.goal_keys(data.get("age")), chosen))


@router.callback_query(Ob.filter(F.act == "gldone"), Onb.goal)
async def on_goals_done(cb: CallbackQuery, state: FSMContext, db: Database, workouts: WorkoutService):
    data = await state.get_data()
    if not data.get("goals"):
        await cb.answer("Выбери хотя бы одну цель.", show_alert=True)
        return
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    await _after_goals(cb.message, state, db, workouts)


@router.callback_query(Ob.filter(F.act == "goal_text"), Onb.goal)
async def on_goal_text_btn(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    await state.set_state(Onb.goal_text)
    await cb.message.answer(
        "Напиши, чего хочешь, можно всё сразу. Например: «сила, объём мышц, выносливость, рельеф», "
        "«выглядеть большим и бегать долго», «гибкость, сесть на шпагат»."
    )


@router.message(Onb.goal_text, F.text, ~F.text.startswith("/"))
async def on_goal_text(message: Message, state: FSMContext, db: Database, workouts: WorkoutService):
    data = await state.get_data()
    found = goals_svc.parse_goals(message.text, data.get("age"))
    if not found:
        await message.answer(
            "Не понял. Назови цели словами: объём, рельеф, сила, выносливость, гибкость, форма и здоровье."
        )
        return
    await state.update_data(goals=found)
    await _after_goals(message, state, db, workouts)


@router.callback_query(Ob.filter(F.act == "level"), Onb.level)
async def on_level(cb: CallbackQuery, callback_data: Ob, state: FSMContext):
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    await state.update_data(level=callback_data.arg)
    await _ask_places(cb.message, state, set())


def _places_kb(selected: set[str]):
    rows = [[(("✅ " if k in selected else "") + v, Ob(act="pl", arg=k))] for k, v in PLACES.items()]
    if selected:
        rows.append([("Готово ➡", Ob(act="pldone"))])
    return inline(rows)


async def _ask_places(message: Message, state: FSMContext, selected: set[str]):
    await state.set_state(Onb.place)
    await state.update_data(places=sorted(selected))
    await message.answer(
        "<b>Где ты можешь тренироваться?</b> Отметь всё, что подходит, можно несколько. "
        "Программа строится под основное место (зал, если он есть), а в день тренировки "
        "одной кнопкой переключаешься на другое: «некогда в зал» — делаешь дома.",
        reply_markup=_places_kb(selected),
    )


@router.callback_query(Ob.filter(F.act == "pl"), Onb.place)
async def on_place_toggle(cb: CallbackQuery, callback_data: Ob, state: FSMContext):
    await cb.answer()
    selected = set((await state.get_data()).get("places", []))
    selected ^= {callback_data.arg}
    await state.update_data(places=sorted(selected))
    await cb.message.edit_reply_markup(reply_markup=_places_kb(selected))


@router.callback_query(Ob.filter(F.act == "pldone"), Onb.place)
async def on_places_done(cb: CallbackQuery, state: FSMContext, db: Database):
    data = await state.get_data()
    places = [p for p in ORDER if p in set(data.get("places", []))]
    if not places:
        await cb.answer("Выбери хотя бы одно место.", show_alert=True)
        return
    await cb.answer()
    main = primary_place(places)
    await cb.message.edit_text(f"Места: {', '.join(PLACES[p] for p in places)}. Основное: {PLACES[main]}.")
    if data.get("mode") == "places":
        await db.update_user(cb.from_user.id, place=main, places=",".join(places))
        user = await db.get_user(cb.from_user.id)
        if "home_db" in places and user:
            await state.set_data({"mode": "gear", "gear": load_gear(user)})
            await _ask_gear(cb.message, state, standalone=True)
            return
        await state.clear()
        await cb.message.answer("Сохранил. Новые тренировки уже под это. Смотри: /plan", reply_markup=main_menu())
        return
    await state.update_data(place=main, places=places)
    if "home_db" in places:
        await state.update_data(gear=empty_gear())
        await _ask_gear(cb.message, state)
        return
    await _ask_days_n(cb.message, state)


@router.callback_query(St.filter(F.act == "place"))
async def settings_places(cb: CallbackQuery, state: FSMContext, db: Database):
    await cb.answer()
    user = await db.get_user(cb.from_user.id)
    await state.set_data({"mode": "places"})
    await _ask_places(cb.message, state, set(user_places(user)) if user else set())


# ---------- инвентарь дома ----------
_WEIGHTED = {"dumbbell", "kettlebell", "band", "expander"}


def _gear_kb(gear: dict):
    have = {i["id"] for i in gear["items"]}
    rows, row = [], []
    for c in equipment_catalog():
        row.append((("✅ " if c["id"] in have else "") + c["name"], Ob(act="gt", arg=c["id"])))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([("🗑 Очистить", Ob(act="gclear")), ("Готово ➡", Ob(act="gdone"))])
    return inline(rows)


async def _ask_gear(message: Message, state: FSMContext, standalone: bool = False):
    data = await state.get_data()
    gear = data.get("gear") or empty_gear()
    await state.set_state(Onb.gear)
    await message.answer(
        "<b>Что у тебя есть дома?</b> Отметь кнопками или <b>напиши списком</b> со всеми деталями: "
        "веса гантелей и гирь, сопротивление резинок и эспандера, что ещё есть. Например:\n"
        "<i>«одна гантель 5 кг, турник, резинка 25 кг, эспандер 25 кг, скакалка, беговая дорожка»</i>\n\n"
        "Программа будет только из того, что у тебя есть, и с твоими весами.\n\n"
        f"<b>Сейчас записано</b>\n{render_gear(gear)}",
        reply_markup=_gear_kb(gear),
    )


async def _gear_update(message: Message, state: FSMContext, items: list[dict], other: list[str]):
    data = await state.get_data()
    gear = merge_gear(data.get("gear") or empty_gear(), items, other)
    await state.update_data(gear=gear)
    await message.answer(
        f"Записал.\n{render_gear(gear)}\n\nЕщё что-то? Допиши текстом или жми «Готово».",
        reply_markup=_gear_kb(gear),
    )


@router.callback_query(Ob.filter(F.act == "gt"), Onb.gear)
async def on_gear_toggle(cb: CallbackQuery, callback_data: Ob, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    gear = data.get("gear") or empty_gear()
    gid = callback_data.arg
    if any(i["id"] == gid for i in gear["items"]):
        gear["items"] = [i for i in gear["items"] if i["id"] != gid]
        await state.update_data(gear=gear)
        await cb.message.edit_reply_markup(reply_markup=_gear_kb(gear))
        return
    gear["items"].append({"id": gid})
    await state.update_data(gear=gear, gear_pending=gid)
    await cb.message.edit_reply_markup(reply_markup=_gear_kb(gear))
    if gid in _WEIGHTED:
        await state.set_state(Onb.gear_detail)
        hint = {
            "dumbbell": "Сколько гантелей и какого веса? «2 по 5 кг», «одна 8 кг», «разборные до 20 кг»",
            "kettlebell": "Какой вес гири? «16 кг» или «8 и 12 кг»",
            "band": "Какое сопротивление? «25 кг», «10, 20 и 30 кг» или «лёгкая и средняя»",
            "expander": "Какое сопротивление? «25 кг»",
        }[gid]
        await cb.message.answer(hint + ". Если не знаешь, нажми «Не знаю».",
                                reply_markup=inline([[("Не знаю", Ob(act="gskip"))]]))


@router.callback_query(Ob.filter(F.act == "gskip"), Onb.gear_detail)
async def on_gear_skip(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    await state.set_state(Onb.gear)
    gear = (await state.get_data()).get("gear") or empty_gear()
    await cb.message.answer("Ок, без точных весов.", reply_markup=_gear_kb(gear))


@router.message(Onb.gear_detail, F.text, ~F.text.startswith("/"))
async def on_gear_detail(message: Message, state: FSMContext):
    data = await state.get_data()
    gid = data.get("gear_pending", "dumbbell")
    name = next((c["name"] for c in equipment_catalog() if c["id"] == gid), gid)
    items, other = parse_gear_text(f"{name} {message.text}")
    await state.set_state(Onb.gear)
    mine = [i for i in items if i["id"] == gid]
    await _gear_update(message, state, mine, [])


@router.message(Onb.gear, F.text, ~F.text.startswith("/"))
async def on_gear_text(message: Message, state: FSMContext, db: Database, llm: LLM, settings):
    items, other = parse_gear_text(message.text)
    if (not items or other) and len(message.text) > 3 and llm.available and await db.take_quota(
        message.from_user.id, "llm", settings.limit_llm_per_day
    ):
        smart = await llm.extract_gear(message.text)
        if smart:
            items, other = smart[0] or items, smart[1]
    if not items and not other:
        await message.answer("Не понял. Напиши, например: «гантели 2 по 8 кг, турник, резинка 25 кг».")
        return
    await _gear_update(message, state, items, other)


@router.callback_query(Ob.filter(F.act == "gclear"), Onb.gear)
async def on_gear_clear(cb: CallbackQuery, state: FSMContext):
    await cb.answer("Очистил")
    await state.update_data(gear=empty_gear())
    await cb.message.edit_reply_markup(reply_markup=_gear_kb(empty_gear()))


@router.callback_query(Ob.filter(F.act == "gdone"), Onb.gear)
async def on_gear_done(cb: CallbackQuery, state: FSMContext, db: Database, workouts: WorkoutService):
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    data = await state.get_data()
    gear = data.get("gear") or empty_gear()
    if data.get("mode") == "gear":
        await db.update_user(cb.from_user.id, equipment=dump_gear(gear))
        user = await db.get_user(cb.from_user.id)
        if user:
            await workouts.refresh_planned(user)
        await state.clear()
        await cb.message.answer(
            f"Инвентарь сохранён:\n{render_gear(gear)}\n\nПрограмма пересобрана под него: /plan",
            reply_markup=main_menu(),
        )
        return
    await cb.message.answer(f"Инвентарь:\n{render_gear(gear)}")
    await _ask_days_n(cb.message, state)


@router.callback_query(St.filter(F.act == "gear"))
async def settings_gear(cb: CallbackQuery, state: FSMContext, db: Database):
    await cb.answer()
    user = await db.get_user(cb.from_user.id)
    await state.set_data({"mode": "gear", "gear": load_gear(user)})
    await _ask_gear(cb.message, state, standalone=True)


@router.callback_query(St.filter(F.act == "goal"))
async def settings_goal(cb: CallbackQuery, state: FSMContext, db: Database):
    await cb.answer()
    user = await db.get_user(cb.from_user.id)
    await state.set_data({"mode": "goal", "age": (user or {}).get("age")})
    await _ask_goal(cb.message, state)


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
        "<b>Во сколько напоминать?</b> Напиши время, например <code>18:30</code>. "
        "Потом его можно поменять прямо в чате: «напоминай в 19:00»." + warn,
    )


async def _after_time(message: Message, state: FSMContext, time_str: str, db: Database, rem: Reminders):
    await state.update_data(train_time=time_str)
    await _ask_extra(message, state, db)


@router.message(Onb.time)
async def on_time_text(message: Message, state: FSMContext, db: Database, reminders: Reminders):
    t = parse_time(message.text or "")
    if not t:
        await message.answer("Не понял время. Напиши так: <code>18:30</code> или просто <code>18</code>.")
        return
    await _after_time(message, state, t, db, reminders)


# ---------- дополнительные дни: кардио или гибкость ----------
async def _ask_extra(message: Message, state: FSMContext, db: Database):
    data = await state.get_data()
    uid = message.chat.id
    if "goals" not in data:  # настройки: цели берём из профиля
        user = await db.get_user(uid)
        await state.update_data(goals=goals_svc.user_goals(user))
        data = await state.get_data()
    rec = goals_svc.extra_days(data.get("goals"))
    kind = "гибкости и растяжки" if goals_svc.extra_kind(data.get("goals")) == "flex" else "кардио"
    await state.set_state(Onb.extra)
    await message.answer(
        f"<b>Дополнительные дни для {kind}?</b> Бег, велосипед и велодорожки, дорожка, скакалка или ходьба "
        "— зависит от того, что у тебя есть. Отдельные дни, не вместо силовых.\n"
        f"По твоей цели рекомендую: {rec}.",
        reply_markup=inline([[((f"{n}" + (" ✅ рекомендую" if n == rec else "")), Ob(act="x", arg=str(n))) for n in range(4)]]),
    )


@router.callback_query(Ob.filter(F.act == "x"), Onb.extra)
async def on_extra(cb: CallbackQuery, callback_data: Ob, state: FSMContext, db: Database, reminders: Reminders):
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    data = await state.get_data()
    if data.get("mode") == "extra":
        user = await db.get_user(cb.from_user.id)
        strength = parse_days(user["days"]) if user else []
    else:
        strength = sorted(data.get("days", []))
    n = int(callback_data.arg)
    extra = pick_extra_days(strength, n)
    await state.update_data(cardio_days=extra)
    if extra:
        await cb.message.answer("Дополнительные дни: " + fmt_days(extra) + ". Поменять можно в настройках.")
    if data.get("mode") in ("sched", "extra"):
        await _save_schedule(cb.message, state, db, reminders)
        return
    await _ask_tz(cb.message, state)


async def _save_schedule(message: Message, state: FSMContext, db: Database, rem: Reminders):
    data = await state.get_data()
    uid = message.chat.id
    fields: dict = {"cardio_days": ",".join(map(str, data.get("cardio_days", [])))}
    if data["mode"] == "sched":
        fields.update(days_per_week=data["days_per_week"], days=",".join(map(str, data["days"])),
                      train_time=data["train_time"], rotation_idx=0)
    await db.update_user(uid, **fields)
    rem.schedule_user((await db.get_user(uid)))  # type: ignore[arg-type]
    await state.clear()
    await message.answer("Расписание обновлено. Напомню вовремя.", reply_markup=main_menu())


@router.callback_query(St.filter(F.act == "extra"))
async def settings_extra(cb: CallbackQuery, state: FSMContext, db: Database):
    await cb.answer()
    await state.set_data({"mode": "extra"})
    await _ask_extra(cb.message, state, db)


# ---------- город и часовой пояс ----------
async def _ask_tz(message: Message, state: FSMContext):
    await state.set_state(Onb.tz)
    await message.answer(
        "<b>В каком ты городе?</b> Напиши город, например <i>Алматы</i> или <i>Москва</i>. "
        "Если города нет в моём списке, напиши часовой пояс: <i>+5</i> или <i>UTC+3</i>. "
        "Нужно для напоминаний и чтобы подобрать магазины добавок в твоей стране."
    )


async def _after_loc(message: Message, state: FSMContext, loc: Location, db: Database, rem: Reminders):
    data = await state.get_data()
    await state.update_data(tz=loc.tz, country=loc.country, city=loc.city)
    if data["mode"] == "tz":
        await db.update_user(message.chat.id, tz=loc.tz, country=loc.country, city=loc.city)
        rem.schedule_user((await db.get_user(message.chat.id)))  # type: ignore[arg-type]
        await state.clear()
        where = ", ".join(x for x in (loc.city, COUNTRY_NAME.get(loc.country or "")) if x)
        await message.answer(f"Записал: {where or loc.tz}. Пояс: {loc.tz}.", reply_markup=main_menu())
        return
    if not loc.country:
        await state.set_state(Onb.country)
        await message.answer(
            "Из какой ты страны? Нужно для подбора магазинов добавок. Напиши, например, "
            "<i>Казахстан</i>, или «пропустить».",
            reply_markup=inline([[("Пропустить", Ob(act="cskip"))]]),
        )
        return
    await _finalize(message, state, loc.tz, db, rem)


@router.message(Onb.tz)
async def on_tz_text(message: Message, state: FSMContext, db: Database, reminders: Reminders):
    loc = parse_location(message.text or "")
    if not loc:
        await message.answer("Не нашёл такой город. Напиши часовой пояс: <i>+3</i>, <i>+5</i> или <i>Europe/Berlin</i>.")
        return
    await _after_loc(message, state, loc, db, reminders)


@router.message(Onb.country, F.text, ~F.text.startswith("/"))
async def on_country(message: Message, state: FSMContext, db: Database, reminders: Reminders):
    loc = parse_location(message.text or "")
    data = await state.get_data()
    country = loc.country if loc and loc.country else None
    await state.update_data(country=country)
    await _finalize(message, state, data["tz"], db, reminders)


@router.callback_query(Ob.filter(F.act == "cskip"), Onb.country)
async def on_country_skip(cb: CallbackQuery, state: FSMContext, db: Database, reminders: Reminders):
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    data = await state.get_data()
    await _finalize(cb.message, state, data["tz"], db, reminders)


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
        goal2=data.get("goal2"), goals=",".join(data.get("goals") or [data["goal"]]), level=data["level"], place=data["place"],
        places=",".join(data.get("places") or [data["place"]]),
        equipment=dump_gear(data.get("gear") or empty_gear()),
        cardio_days=",".join(map(str, data.get("cardio_days", []))),
        city=data.get("city"), country=data.get("country"),
        days_per_week=data["days_per_week"],
        days=",".join(map(str, days)), train_time=data["train_time"], tz=tz, rotation_idx=0, onboarded=1,
    )
    today = local_today(tz).isoformat()
    await db.add_measurements(tg_id, {"weight": data["weight"], **data["m_vals"]}, today)
    if data.get("p_media"):
        await db.add_progress_media(tg_id, today, [tuple(m) for m in data["p_media"]])
    user = await db.get_user(tg_id)
    assert user
    rem.schedule_user(user)
    await state.clear()

    tpl = template_for(user["days_per_week"], 0)
    plan = build_day(tpl, user)
    nxt = next_training(days, user["train_time"], tz)
    when = f"{WEEKDAYS_RU[nxt.weekday()]} {nxt:%d.%m} в {nxt:%H:%M}" if nxt else "—"
    items = "\n".join(f"• {html.escape(describe_item(i.to_dict()))}" for i in plan)
    extra = parse_days(user.get("cardio_days"))
    kind = "гибкость" if goals_svc.extra_kind(goals_svc.user_goals(user)) == "flex" else "кардио"
    extra_line = f"\nДополнительные дни ({kind}): {fmt_days(extra)}." if extra else ""
    await message.answer(
        f"Готово. Цели: {goals_svc.goals_label(goals_svc.user_goals(user))}. Программа: {user['days_per_week']} трен./нед "
        f"({fmt_days(days)} в {user['train_time']}), {places_label(user_places(user))}.{extra_line}\n\n"
        f"<b>Первая тренировка — {html.escape(tpl['name'])}</b>\n{items}\n\n"
        f"Напомню: {when}. Хочешь начать раньше — кнопка «Тренировка».\n"
        "Перед каждой тренировкой я дам разминку, после неё растяжку. Вес на первое упражнение подскажу сам.",
        reply_markup=main_menu(),
    )


@router.callback_query(St.filter(F.act == "meas"))
async def settings_meas(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_data({"mode": "upd"})
    await cb.message.answer("Обновим замеры. <b>Вес сегодня в кг?</b>")
    await state.set_state(Onb.weight)
