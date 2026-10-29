"""Тренировка: напоминания, карточки упражнений, ввод подходов, самочувствие (кнопкой или словами)."""
import html
import logging
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import BaseFilter, Command
from aiogram.types import CallbackQuery, Message

from bot.cards import send_exercise_card
from bot.config import Settings
from bot.data import PLACES, get_exercise
from bot.db.database import Database
from bot.prompts import DEBRIEF_PROMPT, system_prompt
from bot.services.debrief import template_debrief, workout_facts
from bot.services.llm import LLM, LLMError
from bot.services.places import user_places
from bot.services.profile import build_profile
from bot.services.progression import FEEL_LABELS, Feel
from bot.services.reminders import Reminders
from bot.services.set_parser import ParsedSet, detect_feel, mentions_pain, parse_set
from bot.services.start_weights import is_hand
from bot.services.timeutil import WEEKDAYS_RU, local_today, next_training, parse_days
from bot.services.workout import SetOutcome, WorkoutService, describe_item
from bot.textfmt import format_llm
from bot.ui import BTN_TODAY, Ex, Fl, MENU_BUTTONS, Rem, Wk, fmt_num, inline, workout_keyboard

log = logging.getLogger(__name__)

router = Router()
router.message.filter(F.chat.type == "private")

PLACE_BY_ACT = {"p_gym": "gym", "p_home_db": "home_db", "p_home_bw": "home_bw",
                "h_db": "home_db", "h_bw": "home_bw"}  # h_*: кнопки из старых сообщений

INTRO = (
    "<b>Как проходит тренировка</b>\n"
    "1. Смотришь упражнение: анимация и техника.\n"
    "2. Делаешь подход и пишешь «вес повторы», например <code>8 10</code> (или голосом).\n"
    "3. Говоришь, как далось: кнопкой или словами («еле дожал», «мог ещё»).\n"
    "• 😌 Легко: мог сделать ещё 3+ раза\n"
    "• 👌 Нормально: ещё 2\n"
    "• 😤 Тяжело: ещё 1\n"
    "• 🥵 На пределе: больше ни одного\n"
    "4. Я подстраиваю вес. Отдых между подходами 1–2 минуты.\n\n"
    "Стартовые веса — оценка по твоему весу тела, не истина: после первого подхода я их подкручу. "
    "Боль в суставе или спине — сразу стоп."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fmt_set(kind: str, weight: float, reps: int) -> str:
    if kind == "weight":
        return f"{fmt_num(weight)} кг × {reps}"
    if kind == "time":
        return f"{reps} сек"
    return f"{reps} повт."


# ---------- карточки ----------
def overview_text(w: dict) -> str:
    items = "\n".join(f"• {html.escape(describe_item(i))}" for i in w["plan"])
    minutes = sum(i["sets"] for i in w["plan"]) * 3
    return (
        f"<b>{html.escape(w['day_name'])}</b> · {PLACES[w['place']]} · ~{minutes} мин\n{items}"
    )


def set_prompt(w: dict, item: dict, first: bool = False) -> str:
    """Что делать в этом подходе. first: первый заход на упражнение, показываем диапазон."""
    n = w["set_i"] + 1
    kind = item["kind"]
    head = f"<b>Подход {n}/{item['sets']}.</b>"
    if kind == "weight":
        if w["cur_weight"] is None:
            return f"{head} Подбери вес сам: лёгкий, чтобы сделать {item['hi']} повторов с запасом.\nПотом напиши «вес повторы», например <code>20 10</code>."
        hand = " в каждой гантели" if is_hand(item["ex_id"]) else ""
        reps = f"{item['lo']}–{item['hi']}" if first else str(w["cur_reps"])
        return (
            f"{head} Бери <b>{fmt_num(w['cur_weight'])} кг{hand}</b>, цель {reps} повторов.\n"
            f"Сделал — напиши «вес повторы», например <code>{fmt_num(w['cur_weight'])} {w['cur_reps']}</code>."
        )
    if kind == "time":
        return f"{head} Держи {w['cur_reps']} сек.\nНапиши, сколько продержался, например <code>{w['cur_reps']}</code>."
    return f"{head} Цель {w['cur_reps']} повт.\nНапиши, сколько сделал, например <code>{w['cur_reps']}</code>."


def exercise_text(w: dict, item: dict, ex, target, last: list[dict]) -> str:
    unit = {"weight": "повт.", "reps": "повт.", "time": "сек"}[item["kind"]]
    lines = [
        f"<b>{w['ex_i'] + 1}/{len(w['plan'])} · {html.escape(ex.name)}</b>",
        f"{item['sets']} подх. × {item['lo']}–{item['hi']} {unit}",
    ]
    if last:
        lines.append("Прошлый раз: " + ", ".join(fmt_set(item["kind"], s["weight"], s["reps"]) for s in last))
        lines.append(f"<i>{html.escape(target.note)}</i>")
    lines.append("\n<b>Техника</b>\n" + "\n".join(f"• {html.escape(c)}" for c in ex.cues))
    lines.append("\n" + set_prompt(w, item, first=not last))
    return "\n".join(lines)


async def show_exercise(msg: Message, w: dict, workouts: WorkoutService, with_media: bool = True) -> None:
    item = w["plan"][w["ex_i"]]
    ex = get_exercise(item["ex_id"])
    assert ex
    target, last = await workouts.target_for(w["tg_id"], item, w["id"])
    markup = inline([
        [("📖 Подробно", Ex(act="steps", id=ex.id)), ("📹 YouTube", ex.video_url)],
        [("🔄 Заменить", Wk(act="swap", wid=w["id"])), ("⏭ Пропустить", Wk(act="skipex", wid=w["id"]))],
        [("🏁 Закончить тренировку", Wk(act="finish", wid=w["id"]))],
    ])
    await send_exercise_card(msg, ex, workouts.edb, exercise_text(w, item, ex, target, last), markup, with_media=with_media)


async def start_workout(msg: Message, w: dict, db: Database, workouts: WorkoutService) -> None:
    first_time = await db.done_count(w["tg_id"]) == 0
    w = await workouts.start(w["id"], _now())
    if first_time:
        await msg.answer(INTRO)
    await show_exercise(msg, w, workouts)


async def after_outcome(
    msg: Message, wid: int, outcome: SetOutcome, db: Database, workouts: WorkoutService,
    llm: LLM, settings: Settings, logged: str = "",
) -> None:
    """Один ответ на подход: что записал, подсказка и что дальше."""
    w = await db.get_workout(wid)
    assert w
    head = (f"{logged}\n" if logged else "") + f"💬 {outcome.hint_text}"
    if outcome.state == "next_set":
        await msg.answer(head + "\n\n" + set_prompt(w, w["plan"][w["ex_i"]]))
    elif outcome.state == "next_ex":
        await msg.answer(head + "\n\n➡ Следующее упражнение.")
        await show_exercise(msg, w, workouts)
    else:
        await msg.answer(head)
        await finish_workout(msg, w, db, workouts, llm, settings)


async def send_debrief(msg: Message, w: dict, user: dict, db: Database, llm: LLM, settings: Settings) -> None:
    facts, stats = await workout_facts(db, w)
    text = None
    if llm.available and await db.take_quota(user["tg_id"], "llm", settings.limit_llm_per_day):
        try:
            system = system_prompt(await build_profile(db, user), DEBRIEF_PROMPT)
            text = format_llm(await llm.chat(system, [], facts, max_tokens=300))
        except LLMError as e:
            log.warning("Разбор тренировки без ИИ: %s", e)
    await msg.answer("🧠 <b>Разбор тренера</b>\n" + (text or html.escape(template_debrief(stats))))


async def finish_workout(
    msg: Message, w: dict, db: Database, workouts: WorkoutService, llm: LLM, settings: Settings,
    early: bool = False,
) -> None:
    user = await db.get_user(w["tg_id"])
    assert user
    res = await workouts.finish(user, w["id"], _now(), completed=not early)
    if res["sets"] == 0:
        await msg.answer("Подходов не записано, тренировка не засчитана. Вернуться можно в любой момент: «Тренировка».")
        return
    nxt = next_training(parse_days(user["days"]), user["train_time"], user["tz"])
    when = f"{WEEKDAYS_RU[nxt.weekday()]} {nxt:%d.%m} в {nxt:%H:%M}" if nxt else "—"
    vol = f"\nОбъём: {res['volume']:.0f} кг" if res["volume"] else ""
    await msg.answer(
        f"✅ <b>Тренировка завершена.</b>\nПодходов: {res['sets']}, упражнений: {res['exercises']}.{vol}\n"
        f"Серия: {res['streak']} подряд.\nСледующая: {when}."
    )
    await send_debrief(msg, w, user, db, llm, settings)


# ---------- вспомогательные ----------
async def _owned_workout(cb: CallbackQuery, db: Database, wid: int) -> dict | None:
    w = await db.get_workout(wid)
    if not w or w["tg_id"] != cb.from_user.id:
        await cb.answer("Эта тренировка не найдена.", show_alert=True)
        return None
    return w


class ActiveWorkout(BaseFilter):
    """Пропускает сообщение, только если у пользователя идёт тренировка."""

    async def __call__(self, message: Message, db: Database) -> bool | dict:
        w = await db.active_workout(message.from_user.id)
        return {"w": w} if w else False


# ---------- /today ----------
@router.message(Command("today", "workout"))
@router.message(F.text == BTN_TODAY)
async def cmd_today(message: Message, db: Database, workouts: WorkoutService):
    user = await db.get_user(message.from_user.id)
    if not user or not user["onboarded"]:
        await message.answer("Сначала анкета: /start")
        return
    active = await db.active_workout(user["tg_id"])
    if active:
        await message.answer("Продолжаем текущую тренировку.")
        await show_exercise(message, active, workouts, with_media=False)
        return
    w = await workouts.ensure_planned(user, local_today(user["tz"]))
    if w["status"] == "done":
        await message.answer("Сегодня тренировка уже сделана ✅. Отдых — часть программы. Завтра по плану или как в расписании.")
        return
    if w["status"] == "skipped":
        await db.update_workout(w["id"], status="planned")
        w["status"] = "planned"
    await message.answer(
        overview_text(w),
        reply_markup=workout_keyboard(w["id"], user_places(user), w["place"]),
    )


@router.message(Command("finish"))
async def cmd_finish(message: Message, db: Database, workouts: WorkoutService, llm: LLM, settings: Settings):
    w = await db.active_workout(message.from_user.id)
    if not w:
        await message.answer("Сейчас нет активной тренировки.")
        return
    await finish_workout(message, w, db, workouts, llm, settings, early=True)


# ---------- кнопки напоминания ----------
@router.callback_query(Rem.filter())
async def on_reminder(
    cb: CallbackQuery, callback_data: Rem, db: Database, workouts: WorkoutService, reminders: Reminders,
):
    w = await _owned_workout(cb, db, callback_data.wid)
    if not w:
        return
    act = callback_data.act
    user = await db.get_user(cb.from_user.id)
    assert user
    if w["status"] != "planned" and act != "go":
        await cb.answer("Это уже неактуально.", show_alert=False)
        return
    await cb.answer()

    if act == "go":
        if w["status"] == "active":
            await cb.message.edit_reply_markup(reply_markup=None)
            await show_exercise(cb.message, w, workouts, with_media=False)
            return
        if w["status"] != "planned":
            return
        await cb.message.edit_reply_markup(reply_markup=None)
        await start_workout(cb.message, w, db, workouts)
    elif act in PLACE_BY_ACT or act == "home":
        if act == "home":  # старое сообщение: спрашиваем, какой именно дом
            await cb.message.edit_reply_markup(reply_markup=inline([[
                ("С гантелями", Rem(act="p_home_db", wid=w["id"])), ("Без инвентаря", Rem(act="p_home_bw", wid=w["id"])),
            ]]))
            return
        place = PLACE_BY_ACT[act]
        w = await workouts.rebuild_for_place(user, w["id"], place)
        await cb.message.edit_text(
            "Версия под выбранное место:\n\n" + overview_text(w),
            reply_markup=workout_keyboard(w["id"], user_places(user), w["place"]),
        )
    elif act == "snooze":
        reminders.schedule_snooze(user["tg_id"], w["id"])
        await cb.message.edit_reply_markup(reply_markup=None)
        await cb.message.answer("Ок, напомню через 30 минут. Только без «ещё полчасика» ещё раз.")
    elif act == "skip":
        await db.update_workout(w["id"], status="skipped")
        await cb.message.edit_reply_markup(reply_markup=None)
        nxt = next_training(parse_days(user["days"]), user["train_time"], user["tz"])
        when = f"{WEEKDAYS_RU[nxt.weekday()]} {nxt:%d.%m}" if nxt else "по расписанию"
        await cb.message.answer(
            f"Записал пропуск. Серия обнулена, но это не конец. Следующая тренировка: {when}. "
            "Если сил мало, сделай хотя бы 10 минут дома: /today."
        )


# ---------- кнопки внутри тренировки ----------
@router.callback_query(Wk.filter())
async def on_workout_button(
    cb: CallbackQuery, callback_data: Wk, db: Database, workouts: WorkoutService, llm: LLM, settings: Settings,
):
    w = await _owned_workout(cb, db, callback_data.wid)
    if not w:
        return
    if w["status"] != "active":
        await cb.answer("Тренировка уже не активна.", show_alert=True)
        return
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    act = callback_data.act
    if act == "swap":
        if await workouts.swap_exercise(w["id"]):
            w = await db.get_workout(w["id"])
            assert w
            await show_exercise(cb.message, w, workouts)
        else:
            w = await db.get_workout(w["id"])
            assert w
            await cb.message.answer("Замены нет или подход уже начат. Можно пропустить упражнение.")
            await show_exercise(cb.message, w, workouts, with_media=False)
    elif act == "skipex":
        state = await workouts.skip_exercise(w["id"])
        w = await db.get_workout(w["id"])
        assert w
        if state == "finished":
            await finish_workout(cb.message, w, db, workouts, llm, settings)
        else:
            await show_exercise(cb.message, w, workouts)
    elif act == "finish":
        await finish_workout(cb.message, w, db, workouts, llm, settings, early=True)


@router.callback_query(Fl.filter())
async def on_feel(
    cb: CallbackQuery, callback_data: Fl, db: Database, workouts: WorkoutService, llm: LLM, settings: Settings,
):
    w = await _owned_workout(cb, db, callback_data.wid)
    if not w:
        return
    s = await db.get_set(callback_data.sid)
    if not s or s["feel"] is not None or w["status"] != "active":
        await cb.answer("Уже записано.")
        return
    await cb.answer()
    feel = Feel(callback_data.v)
    item = w["plan"][w["ex_i"]]
    logged = f"✔ {fmt_set(item['kind'], s['weight'], s['reps'])} · {FEEL_LABELS[feel]}"
    await cb.message.edit_text(logged)  # то же сообщение: без кнопок и без лишнего сообщения
    outcome = await workouts.complete_set(w["id"], callback_data.sid, feel)
    await after_outcome(cb.message, w["id"], outcome, db, workouts, llm, settings)


# ---------- ввод подхода ----------
def feel_keyboard(wid: int, sid: int):
    buttons = [(label, Fl(wid=wid, sid=sid, v=int(f))) for f, label in FEEL_LABELS.items()]
    return inline([buttons[:2], buttons[2:]])


PAIN_REPLY = (
    "⚠️ Боль в суставе или спине — это стоп, а не «потерпи». Мышечное жжение нормально, "
    "резкая, колющая боль, хруст с болью или онемение нет.\n"
    "Пропусти это упражнение или закончи тренировку. Если боль повторяется, покажись врачу "
    "(спортивному или травматологу)."
)


async def _pain_stop(message: Message, w: dict, db: Database) -> None:
    if w.get("pend_set_id"):
        await db.set_feel(w["pend_set_id"], int(Feel.MAX))
        await db.update_workout(w["id"], pend_set_id=None)
    await message.answer(
        PAIN_REPLY,
        reply_markup=inline([[("⏭ Пропустить упражнение", Wk(act="skipex", wid=w["id"])),
                              ("🏁 Закончить", Wk(act="finish", wid=w["id"]))]]),
    )


async def process_set_text(
    message: Message, w: dict, text: str, db: Database, workouts: WorkoutService, llm: LLM, settings: Settings,
) -> bool:
    """Пытается принять текст как подход или самочувствие. False = это не про тренировку (пусть отвечает чат)."""
    item = w["plan"][w["ex_i"]]
    has_digits = any(c.isdigit() for c in text)
    if mentions_pain(text) and len(text) < 160:
        await _pain_stop(message, w, db)
        return True

    # Висящий подход ждёт самочувствия: принимаем слова вместо кнопки, ИИ понимает свободный текст.
    if w.get("pend_set_id") and not has_digits:
        feel = detect_feel(text)
        if feel is None and len(text) < 200 and llm.available and await db.take_quota(
            message.from_user.id, "llm", settings.limit_llm_per_day
        ):
            feel, pain = await llm.classify_feel(text)
            if pain:
                await _pain_stop(message, w, db)
                return True
        if feel:
            s = await db.get_set(w["pend_set_id"])
            outcome = await workouts.complete_set(w["id"], w["pend_set_id"], feel)
            logged = f"✔ {fmt_set(item['kind'], s['weight'], s['reps'])} · {FEEL_LABELS[feel]}" if s else ""
            await after_outcome(message, w["id"], outcome, db, workouts, llm, settings, logged=logged)
            return True
        # не поняли слова: если это явно не вопрос, просим кнопку
        if "?" not in text and len(text) < 40:
            await message.answer("Не понял, как далось. Нажми кнопку под подходом или напиши: легко / нормально / тяжело / на пределе.")
            return True

    parsed = parse_set(text, item["kind"], default_weight=w["cur_weight"])
    if not parsed and item["kind"] == "weight" and len(text) < 40 and "?" not in text and has_digits is False:
        # Числа словами («двадцать на десять»): просим модель вытащить значения.
        if llm.available and await db.take_quota(message.from_user.id, "llm", settings.limit_llm_per_day):
            weight, reps = await llm.extract_set(text)
            if reps is not None and (weight is not None or w["cur_weight"] is not None):
                parsed = ParsedSet(float(weight if weight is not None else w["cur_weight"]), int(reps), None)
    if not parsed:
        if has_digits and len(text) < 30:
            example = f"<code>{fmt_num(w['cur_weight']) if w['cur_weight'] else 20} 10</code>" if item["kind"] == "weight" else "<code>12</code>"
            await message.answer(f"Не разобрал. Нужен формат {example}: вес и повторы.")
            return True
        return False

    _, outcome = await workouts.log_set(w, parsed)
    logged = f"✔ {fmt_set(item['kind'], parsed.weight, parsed.reps)}"
    if outcome is not None:
        await after_outcome(
            message, w["id"], outcome, db, workouts, llm, settings,
            logged=f"{logged} · {FEEL_LABELS[parsed.feel]}",
        )
        return True
    fresh = await db.get_workout(w["id"])
    assert fresh
    await message.answer(
        f"{logged}. Как далось?\n<i>Нажми кнопку или напиши словами: «легко», «еле дожал».</i>",
        reply_markup=feel_keyboard(w["id"], fresh["pend_set_id"]),
    )
    return True


@router.message(F.text, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS), ActiveWorkout())
async def on_set_text(message: Message, w: dict, db: Database, workouts: WorkoutService, llm: LLM, settings: Settings):
    if not await process_set_text(message, w, message.text, db, workouts, llm, settings):
        raise SkipHandler
