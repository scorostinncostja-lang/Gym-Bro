"""Тренировка: напоминания, карточки упражнений, ввод подходов, ощущения."""
import html
import logging
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import BaseFilter, Command
from aiogram.types import CallbackQuery, Message

from bot.data import PLACES, get_exercise
from bot.db.database import Database
from bot.services.llm import LLM
from bot.services.progression import FEEL_LABELS, Feel
from bot.services.reminders import Reminders
from bot.services.set_parser import ParsedSet, detect_feel, parse_set
from bot.services.timeutil import WEEKDAYS_RU, local_today, next_training, parse_days
from bot.services.workout import SetOutcome, WorkoutService, describe_item
from bot.cards import send_exercise_media
from bot.config import Settings
from bot.ui import BTN_TODAY, Ex, Fl, MENU_BUTTONS, Rem, Wk, fmt_num, inline

log = logging.getLogger(__name__)

router = Router()
router.message.filter(F.chat.type == "private")


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


async def show_exercise(msg: Message, w: dict, workouts: WorkoutService, with_media: bool = True) -> None:
    item = w["plan"][w["ex_i"]]
    ex = get_exercise(item["ex_id"])
    assert ex
    target, last = await workouts.target_for(w["tg_id"], item, w["id"])
    if with_media:
        await send_exercise_media(msg, ex, workouts.edb)
    unit = {"weight": "повт.", "reps": "повт.", "time": "сек"}[item["kind"]]
    lines = [
        f"<b>{w['ex_i'] + 1}/{len(w['plan'])} · {html.escape(ex.name)}</b>",
        f"{item['sets']} × {item['lo']}–{item['hi']} {unit}",
    ]
    if last:
        lines.append("Прошлый раз: " + ", ".join(fmt_set(item["kind"], s["weight"], s["reps"]) for s in last))
    lines.append(f"<i>{html.escape(target.note)}</i>")
    lines.append("\n<b>Техника</b>\n" + "\n".join(f"• {html.escape(c)}" for c in ex.cues))
    lines.append("\n" + set_prompt(w, item))
    await msg.answer(
        "\n".join(lines),
        reply_markup=inline([
            [("📖 Подробно", Ex(act="steps", id=ex.id)), ("📹 YouTube", ex.video_url)],
            [("🔄 Заменить", Wk(act="swap", wid=w["id"])), ("⏭ Пропустить", Wk(act="skipex", wid=w["id"]))],
            [("🏁 Закончить тренировку", Wk(act="finish", wid=w["id"]))],
        ]),
    )


def set_prompt(w: dict, item: dict) -> str:
    n = w["set_i"] + 1
    kind = item["kind"]
    if kind == "weight":
        goal = f"Цель: {fmt_set(kind, w['cur_weight'], w['cur_reps'])}." if w["cur_weight"] is not None else "Подбери вес сам."
        how = "Напиши «вес повторы», например <code>40 10</code>, или отправь голосовое."
    elif kind == "time":
        goal = f"Цель: {w['cur_reps']} сек."
        how = "Напиши, сколько секунд продержался, например <code>40</code>."
    else:
        goal = f"Цель: {w['cur_reps']} повт."
        how = "Напиши, сколько повторов сделал, например <code>12</code>."
    return f"<b>Подход {n}/{item['sets']}.</b> {goal}\n{how}"


async def start_workout(msg: Message, w: dict, workouts: WorkoutService) -> None:
    w = await workouts.start(w["id"], _now())
    await msg.answer(
        overview_text(w) + "\n\nДиапазон дан для 3 «чистых» подходов. После каждого напиши результат и "
        "как далось: «легко» / «нормально» / «тяжело». Боль в суставах — стоп."
    )
    await show_exercise(msg, w, workouts)


async def after_outcome(msg: Message, wid: int, outcome: SetOutcome, db: Database, workouts: WorkoutService) -> None:
    w = await db.get_workout(wid)
    assert w
    await msg.answer(f"💬 {outcome.hint_text}")
    if outcome.state == "next_set":
        await msg.answer(set_prompt(w, w["plan"][w["ex_i"]]))
    elif outcome.state == "next_ex":
        await show_exercise(msg, w, workouts)
    else:
        await finish_workout(msg, w, db, workouts)


async def finish_workout(msg: Message, w: dict, db: Database, workouts: WorkoutService, early: bool = False) -> None:
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
        f"Серия: {res['streak']} подряд.\nСледующая: {when}.\n\n"
        "Теперь еда с белком и сон 8+ часов: именно там мышцы растут."
    )


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
        reply_markup=inline([[("✅ Начать", Rem(act="go", wid=w["id"])), ("🏠 Дома", Rem(act="home", wid=w["id"]))]]),
    )


@router.message(Command("finish"))
async def cmd_finish(message: Message, db: Database, workouts: WorkoutService):
    w = await db.active_workout(message.from_user.id)
    if not w:
        await message.answer("Сейчас нет активной тренировки.")
        return
    await finish_workout(message, w, db, workouts, early=True)


# ---------- кнопки напоминания ----------
@router.callback_query(Rem.filter())
async def on_reminder(cb: CallbackQuery, callback_data: Rem, db: Database, workouts: WorkoutService, reminders: Reminders):
    w = await _owned_workout(cb, db, callback_data.wid)
    if not w:
        return
    act = callback_data.act
    user = await db.get_user(cb.from_user.id)
    assert user
    if w["status"] not in ("planned",) and act != "go":
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
        await start_workout(cb.message, w, workouts)
    elif act == "home":
        await cb.message.edit_reply_markup(reply_markup=inline([[
            ("С гантелями", Rem(act="h_db", wid=w["id"])), ("Без инвентаря", Rem(act="h_bw", wid=w["id"])),
        ]]))
    elif act in ("h_db", "h_bw"):
        place = "home_db" if act == "h_db" else "home_bw"
        w = await workouts.rebuild_for_place(user, w["id"], place)
        await cb.message.edit_text(
            "Домашняя версия на сегодня:\n\n" + overview_text(w),
            reply_markup=inline([[("✅ Начать", Rem(act="go", wid=w["id"]))]]),
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
            "Если сил мало, сделай хотя бы 10 минут дома: /today → «Дома»."
        )


# ---------- кнопки внутри тренировки ----------
@router.callback_query(Wk.filter())
async def on_workout_button(cb: CallbackQuery, callback_data: Wk, db: Database, workouts: WorkoutService):
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
            await cb.message.answer("Заменил на похожее.")
            await show_exercise(cb.message, w, workouts)
        else:
            await cb.message.answer("Замены нет или подход уже начат. Можно пропустить упражнение.")
            w = await db.get_workout(w["id"])
            assert w
            await show_exercise(cb.message, w, workouts, with_media=False)
    elif act == "skipex":
        state = await workouts.skip_exercise(w["id"])
        w = await db.get_workout(w["id"])
        assert w
        if state == "finished":
            await finish_workout(cb.message, w, db, workouts)
        else:
            await show_exercise(cb.message, w, workouts)
    elif act == "finish":
        await finish_workout(cb.message, w, db, workouts, early=True)


@router.callback_query(Fl.filter())
async def on_feel(cb: CallbackQuery, callback_data: Fl, db: Database, workouts: WorkoutService):
    w = await _owned_workout(cb, db, callback_data.wid)
    if not w:
        return
    s = await db.get_set(callback_data.sid)
    if not s or s["feel"] is not None or w["status"] != "active":
        await cb.answer("Уже записано.")
        return
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    outcome = await workouts.complete_set(w["id"], callback_data.sid, Feel(callback_data.v))
    await after_outcome(cb.message, w["id"], outcome, db, workouts)


# ---------- ввод подхода ----------
def feel_keyboard(wid: int, sid: int):
    return inline([[(label, Fl(wid=wid, sid=sid, v=int(f))) for f, label in FEEL_LABELS.items()][:2],
                   [(label, Fl(wid=wid, sid=sid, v=int(f))) for f, label in FEEL_LABELS.items()][2:]])


async def process_set_text(
    message: Message, w: dict, text: str, db: Database, workouts: WorkoutService, llm: LLM, settings: Settings,
) -> bool:
    """Пытается принять текст как подход. False = это не подход (пусть отвечает чат)."""
    item = w["plan"][w["ex_i"]]
    # Ощущение отдельным словом («тяжело») закрывает висящий подход.
    if w.get("pend_set_id") and not any(c.isdigit() for c in text):
        feel = detect_feel(text)
        if feel:
            outcome = await workouts.complete_set(w["id"], w["pend_set_id"], feel)
            await after_outcome(message, w["id"], outcome, db, workouts)
            return True
    parsed = parse_set(text, item["kind"], default_weight=w["cur_weight"])
    if not parsed and item["kind"] == "weight" and len(text) < 40 and "?" not in text:
        # Числа словами («двадцать на десять»): просим модель вытащить значения.
        if llm.available and await db.take_quota(message.from_user.id, "llm", settings.limit_llm_per_day):
            weight, reps = await llm.extract_set(text)
            if reps is not None and (weight is not None or w["cur_weight"] is not None):
                parsed = ParsedSet(float(weight if weight is not None else w["cur_weight"]), int(reps), None)
    if not parsed:
        if any(c.isdigit() for c in text) and len(text) < 30:
            example = "<code>40 10</code>" if item["kind"] == "weight" else "<code>12</code>"
            await message.answer(f"Не разобрал. Нужен формат {example}.")
            return True
        return False

    _, outcome = await workouts.log_set(w, parsed)
    if outcome is not None:
        await message.answer(f"✔ {fmt_set(item['kind'], parsed.weight, parsed.reps)} · {FEEL_LABELS[parsed.feel]}")
        await after_outcome(message, w["id"], outcome, db, workouts)
        return True
    fresh = await db.get_workout(w["id"])
    assert fresh
    await message.answer(
        f"✔ {fmt_set(item['kind'], parsed.weight, parsed.reps)}. Как далось?",
        reply_markup=feel_keyboard(w["id"], fresh["pend_set_id"]),
    )
    return True


@router.message(F.text, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS), ActiveWorkout())
async def on_set_text(message: Message, w: dict, db: Database, workouts: WorkoutService, llm: LLM, settings: Settings):
    if not await process_set_text(message, w, message.text, db, workouts, llm, settings):
        raise SkipHandler
