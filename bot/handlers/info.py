"""План, прогресс, справочник упражнений, настройки, приватность."""
import html
from datetime import timedelta

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from urllib.parse import quote_plus

from aiogram.types import CallbackQuery, Message

from bot.cards import send_animation, send_exercise_media, steps_text
from bot.data import PLACES, find_exercises, get_exercise
from bot.db.database import Database
from bot.handlers.onboarding import GOALS, MEASURE_LABELS, MEASURE_UNITS
from bot.data import program_templates
from bot.services.exercise_db import ExerciseDB
from bot.services.program_builder import build_day
from bot.services.reminders import Reminders
from bot.services.timeutil import WEEKDAYS_RU, fmt_days, local_today, next_training, parse_days
from bot.services.workout import describe_item
from bot.sources import ATTRIBUTION
from bot.ui import BTN_PLAN, BTN_SETTINGS, BTN_STATS, Ex, St, inline

router = Router()
router.message.filter(F.chat.type == "private")

HELP = """\
<b>Что я умею</b>
🏋️ /today — тренировка сегодня (в зале или дома)
📅 /plan — твоя программа
📊 /stats — прогресс, серия, рабочие веса
📏 /measure — новые замеры: лента, фото или кружок
📸 /progress — фото «было → стало», /photo_guide — как снимать
💊 /supplements — что работает, а что маркетинг
🔎 /exercise присед — анимация и техника (русский или английский)
📚 /library — каталог упражнений по группам мышц
⚙️ /settings — место, расписание, часовой пояс
🔒 /privacy — какие данные я храню
🗑 /delete_me — удалить все данные

Во время тренировки пиши подходы текстом («40 10 тяжело») или голосом. Любые вопросы по технике и \
восстановлению — просто напиши. Фото еды и видео техники тоже принимаю.

""" + ATTRIBUTION + "."

PRIVACY = """\
<b>Что хранится</b>
• Возраст, рост, вес, замеры, цель, расписание, история подходов.
• Фото и кружки для «было → стало»: только ссылка на файл у Telegram, сами файлы я не копирую.
• Последние ~20 реплик диалога для контекста.

<b>Куда уходят данные</b>
• Текст вопросов, фото еды и видео техники отправляются в Google Gemini или Groq для ответа. \
Голосовые — в Groq для расшифровки. Telegram-ник и ID туда не передаются.
• Фото еды и видео техники не сохраняются, анализ идёт «на лету».
• Фото и кружки тела никуда не отправляются и ИИ не анализируются: они только для твоего сравнения.

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
@router.message(Command("stats"))
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


# ---------- /exercise, /library ----------
PARTS_RU = {
    "chest": "Грудь", "back": "Спина", "shoulders": "Плечи", "upper arms": "Бицепс и трицепс",
    "lower arms": "Предплечья", "upper legs": "Бёдра и ягодицы", "lower legs": "Голени",
    "waist": "Пресс и корпус", "neck": "Шея", "cardio": "Кардио",
}


def _ex_buttons(curated, found) -> list[list[tuple[str, Ex]]]:
    rows = [[(f"⭐ {e.name}", Ex(act="cur", id=e.id))] for e in curated[:6]]
    rows += [[(e.name, Ex(act="db", id=e.id))] for e in found[:12]]
    return rows


async def send_curated(msg: Message, ex, edb: ExerciseDB) -> None:
    await send_exercise_media(msg, ex, edb)
    has_steps = bool(edb.get(ex.hasan) and edb.get(ex.hasan).steps_ru)  # type: ignore[union-attr]
    row = ([("📖 Подробно", Ex(act="steps", id=ex.id))] if has_steps else []) + [("📹 YouTube", ex.video_url)]
    await msg.answer(
        f"<b>{html.escape(ex.name)}</b>\n" + "\n".join(f"• {html.escape(c)}" for c in ex.cues),
        reply_markup=inline([row]),
    )


async def send_db_item(msg: Message, item, edb: ExerciseDB) -> None:
    gif = edb.gif_url(item.id)
    if gif:
        await send_animation(msg, gif)
    q = quote_plus(f"{item.name} exercise form")
    await msg.answer(
        steps_text(item.name, item),
        reply_markup=inline([[("📹 YouTube", f"https://www.youtube.com/results?search_query={q}")]]),
    )


@router.message(Command("exercise"))
async def cmd_exercise(message: Message, command: CommandObject, edb: ExerciseDB):
    query = (command.args or "").strip()
    if len(query) < 3:
        await message.answer(
            "Напиши часть названия: <code>/exercise присед</code> или по-английски "
            "<code>/exercise lat pulldown</code>. Каталог по мышцам: /library"
        )
        return
    curated = find_exercises(query)
    found = edb.search(query) if edb.ready else []
    curated_hw = {e.hasan for e in curated}
    found = [e for e in found if e.id not in curated_hw]
    if not curated and not found:
        await message.answer("Не нашёл. Попробуй другое слово: присед, жим, тяга, планка, squat, curl.")
        return
    if len(curated) == 1 and not found:
        await send_curated(message, curated[0], edb)
        return
    await message.answer(
        "Нашёл, выбери (⭐ — из моей программы):", reply_markup=inline(_ex_buttons(curated, found))
    )


@router.message(Command("library"))
async def cmd_library(message: Message, edb: ExerciseDB):
    if not edb.ready:
        await message.answer("База упражнений сейчас недоступна. Попробуй позже. Основные упражнения: /exercise присед")
        return
    rows = [[(PARTS_RU.get(p, p), Ex(act="part", id=p))] for p in edb.parts() if p != "cardio"]
    await message.answer(
        f"📚 <b>Каталог: {len(edb.items)} упражнений</b> с анимацией и инструкцией на русском. "
        "Выбери группу мышц. Названия в каталоге английские, шаги на русском.",
        reply_markup=inline(rows),
    )


@router.callback_query(Ex.filter(F.act == "part"))
async def on_part(cb: CallbackQuery, callback_data: Ex, edb: ExerciseDB):
    await cb.answer()
    items = edb.by_part(callback_data.id, 20)
    rows = [[(e.name, Ex(act="db", id=e.id))] for e in items]
    rows.append([("◀ Группы мышц", Ex(act="parts", id=""))])
    await cb.message.edit_text(
        f"<b>{PARTS_RU.get(callback_data.id, callback_data.id)}</b>: самые простые варианты. "
        "Больше — через поиск: <code>/exercise curl</code>.",
        reply_markup=inline(rows),
    )


@router.callback_query(Ex.filter(F.act == "parts"))
async def on_parts(cb: CallbackQuery, edb: ExerciseDB):
    await cb.answer()
    rows = [[(PARTS_RU.get(p, p), Ex(act="part", id=p))] for p in edb.parts() if p != "cardio"]
    await cb.message.edit_text("📚 <b>Каталог упражнений.</b> Выбери группу мышц.", reply_markup=inline(rows))


@router.callback_query(Ex.filter(F.act == "db"))
async def on_db_item(cb: CallbackQuery, callback_data: Ex, edb: ExerciseDB):
    item = edb.get(callback_data.id)
    if not item:
        await cb.answer("Не нашёл.", show_alert=True)
        return
    await cb.answer()
    await send_db_item(cb.message, item, edb)


@router.callback_query(Ex.filter(F.act == "cur"))
async def on_curated(cb: CallbackQuery, callback_data: Ex, edb: ExerciseDB):
    ex = get_exercise(callback_data.id)
    if not ex:
        await cb.answer("Не нашёл.", show_alert=True)
        return
    await cb.answer()
    await send_curated(cb.message, ex, edb)


@router.callback_query(Ex.filter(F.act == "steps"))
async def on_steps(cb: CallbackQuery, callback_data: Ex, edb: ExerciseDB):
    ex = get_exercise(callback_data.id)
    if not ex:
        await cb.answer("Не нашёл.", show_alert=True)
        return
    await cb.answer()
    await cb.message.answer(steps_text(ex.name, edb.get(ex.hasan)))


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
        "Удалить всё: профиль, замеры, фото, историю тренировок? Это необратимо.",
        reply_markup=inline([[("Да, удалить", St(act="delyes")), ("Отмена", St(act="delno"))]]),
    )


@router.message(Command("delete_me"))
async def cmd_delete(message: Message):
    await message.answer(
        "Удалить всё: профиль, замеры, фото, историю тренировок? Это необратимо.",
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
