"""План, прогресс, справочник упражнений, настройки, приватность."""
import html
from datetime import timedelta

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InputMediaPhoto, Message

from bot.data import PLACES, find_exercises, get_exercise
from bot.db.database import Database
from bot.handlers.onboarding import GOALS, MEASURE_LABELS, MEASURE_UNITS
from bot.services.program_builder import build_day
from bot.data import program_templates
from bot.services.reminders import Reminders
from bot.services.timeutil import WEEKDAYS_RU, fmt_days, local_today, next_training, parse_days
from bot.services.workout import describe_item
from bot.ui import BTN_PLAN, BTN_SETTINGS, BTN_STATS, St, inline, main_menu

router = Router()
router.message.filter(F.chat.type == "private")

HELP = """\
<b>Что я умею</b>
🏋️ /today — тренировка сегодня (в зале или дома)
📅 /plan — твоя программа
📊 /stats — прогресс, серия, рабочие веса
📏 /measure — новые замеры
💊 /supplements — что работает, а что маркетинг
🔎 /exercise присед — техника и картинки упражнения
⚙️ /settings — место, расписание, часовой пояс
🔒 /privacy — какие данные я храню
🗑 /delete_me — удалить все данные

Во время тренировки пиши подходы текстом («40 10 тяжело») или голосом. Любые вопросы по технике и \
восстановлению — просто напиши. Фото еды и видео техники тоже принимаю."""

PRIVACY = """\
<b>Что хранится</b>
• Возраст, рост, вес, замеры, цель, расписание, история подходов.
• Последние ~20 реплик диалога для контекста.

<b>Куда уходят данные</b>
• Текст вопросов, фото еды и видео техники отправляются в Google Gemini или Groq для ответа. \
Голосовые — в Groq для расшифровки. Telegram-ник и ID туда не передаются.
• Фото и видео я не сохраняю, анализ идёт «на лету».
• Фото тела не анализирую.

Удалить всё: /delete_me."""


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP)


@router.message(Command("privacy"))
async def cmd_privacy(message: Message):
    await message.answer(PRIVACY)


# ---------- /plan ----------
@router.message(Command("plan"))
@router.message(F.text == BTN_PLAN)
async def cmd_plan(message: Message, db: Database):
    user = await db.get_user(message.from_user.id)
    if not user or not user["onboarded"]:
        await message.answer("Сначала анкета: /start")
        return
    days = parse_days(user["days"])
    blocks = []
    for tpl in program_templates(user["days_per_week"]):
        items = "\n".join(f"• {html.escape(describe_item(i.to_dict()))}" for i in build_day(tpl, user["place"]))
        blocks.append(f"<b>{html.escape(tpl['name'])}</b>\n{items}")
    nxt = next_training(days, user["train_time"], user["tz"])
    when = f"{WEEKDAYS_RU[nxt.weekday()]} {nxt:%d.%m} в {nxt:%H:%M}" if nxt else "—"
    await message.answer(
        f"{PLACES[user['place']]} · {fmt_days(days)} в {user['train_time']}\n"
        f"Ближайшая тренировка: {when}\n\n" + "\n\n".join(blocks)
        + "\n\nДни чередуются по кругу. Веса подбираются автоматически по твоим результатам."
    )


# ---------- /stats ----------
@router.message(Command("stats", "progress"))
@router.message(F.text == BTN_STATS)
async def cmd_stats(message: Message, db: Database):
    user = await db.get_user(message.from_user.id)
    if not user or not user["onboarded"]:
        await message.answer("Сначала анкета: /start")
        return
    tg = user["tg_id"]
    today = local_today(user["tz"])
    monday = today - timedelta(days=today.weekday())
    total = await db.done_count(tg)
    week = await db.done_count(tg, monday.isoformat())
    streak = await db.streak(tg)
    lines = [
        "📊 <b>Прогресс</b>",
        f"Тренировок всего: {total}",
        f"На этой неделе: {week} из {user['days_per_week']}",
        f"Серия подряд: {streak}",
    ]
    series = await db.measurement_series(tg)
    deltas = []
    for kind, pts in series.items():
        if len(pts) >= 2:
            diff = pts[-1][1] - pts[0][1]
            unit = MEASURE_UNITS.get(kind, "см")
            sign = "+" if diff > 0 else ""
            deltas.append(f"{MEASURE_LABELS.get(kind, kind)} {sign}{diff:g} {unit}")
    if deltas:
        first = min(pts[0][0] for pts in series.values() if len(pts) >= 2)
        lines.append(f"\n<b>Замеры с {first}</b>\n" + ", ".join(deltas))
    elif series:
        lines.append("\nЗамеры: пока одна точка. Повтори через 3–4 недели: /measure")
    best = await db.best_lifts(tg)
    if best:
        rows = []
        for b in best[:6]:
            ex = get_exercise(b["ex_id"])
            if ex:
                rows.append(f"• {html.escape(ex.name)}: {b['best_w']:g} кг ({b['sessions']} трен.)")
        lines.append("\n<b>Рабочие веса</b>\n" + "\n".join(rows))
    if total == 0:
        lines.append("\nПока пусто. Первая тренировка всё запустит: /today")
    await message.answer("\n".join(lines))


# ---------- /exercise ----------
@router.message(Command("exercise"))
async def cmd_exercise(message: Message, command: CommandObject):
    query = (command.args or "").strip()
    if len(query) < 3:
        await message.answer("Напиши часть названия, например: <code>/exercise присед</code> или <code>/exercise жим</code>.")
        return
    found = find_exercises(query)
    if not found:
        await message.answer("Такого упражнения нет в моей базе. Попробуй другое слово: присед, жим, тяга, планка.")
        return
    if len(found) > 1:
        await message.answer("Нашёл несколько, уточни:\n" + "\n".join(f"• {html.escape(e.name)}" for e in found[:15]))
        return
    ex = found[0]
    try:
        await message.answer_media_group([InputMediaPhoto(media=u) for u in ex.images])
    except Exception:  # noqa: BLE001
        pass
    await message.answer(
        f"<b>{html.escape(ex.name)}</b>\n" + "\n".join(f"• {html.escape(c)}" for c in ex.cues),
        reply_markup=inline([[("📹 Видео-разбор", ex.video_url)]]),
    )


# ---------- /settings ----------
@router.message(Command("settings"))
@router.message(F.text == BTN_SETTINGS)
async def cmd_settings(message: Message, db: Database):
    user = await db.get_user(message.from_user.id)
    if not user or not user["onboarded"]:
        await message.answer("Сначала анкета: /start")
        return
    await message.answer(
        "⚙️ <b>Настройки</b>\n"
        f"Цель: {GOALS.get(user['goal'], '—')}\n"
        f"Место: {PLACES[user['place']]}\n"
        f"Расписание: {fmt_days(parse_days(user['days']))} в {user['train_time']}\n"
        f"Часовой пояс: {user['tz']}",
        reply_markup=inline([
            [("📍 Место", St(act="place")), ("📆 Расписание", St(act="sched"))],
            [("🌍 Часовой пояс", St(act="tz")), ("📏 Замеры", St(act="meas"))],
            [("🗑 Удалить мои данные", St(act="del"))],
        ]),
    )


@router.callback_query(St.filter(F.act == "place"))
async def settings_place(cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=inline(
        [[(v, St(act="setplace", arg=k))] for k, v in PLACES.items()]
    ))


@router.callback_query(St.filter(F.act == "setplace"))
async def settings_setplace(cb: CallbackQuery, callback_data: St, db: Database):
    await cb.answer()
    await db.update_user(cb.from_user.id, place=callback_data.arg)
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(f"Место: {PLACES[callback_data.arg]}. Новые тренировки уже под него. Смотри: /plan")


@router.callback_query(St.filter(F.act == "del"))
async def settings_del(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer(
        "Удалить всё: профиль, замеры, историю тренировок? Это необратимо.",
        reply_markup=inline([[("Да, удалить", St(act="delyes")), ("Отмена", St(act="delno"))]]),
    )


@router.message(Command("delete_me"))
async def cmd_delete(message: Message):
    await message.answer(
        "Удалить всё: профиль, замеры, историю тренировок? Это необратимо.",
        reply_markup=inline([[("Да, удалить", St(act="delyes")), ("Отмена", St(act="delno"))]]),
    )


@router.callback_query(St.filter(F.act == "delno"))
async def delete_no(cb: CallbackQuery):
    await cb.answer("Отменено")
    await cb.message.edit_reply_markup(reply_markup=None)


@router.callback_query(St.filter(F.act == "delyes"))
async def delete_yes(cb: CallbackQuery, db: Database, reminders: Reminders, state: FSMContext):
    await cb.answer()
    reminders.unschedule_user(cb.from_user.id)
    await db.delete_user(cb.from_user.id)
    await state.clear()
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer("Всё удалено. Начать заново: /start")
