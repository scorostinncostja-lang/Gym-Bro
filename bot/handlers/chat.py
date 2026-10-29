"""ИИ-тренер: свободный текст, голос, фото еды и видео техники."""
import html
import io
import logging
import time

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.config import Settings
from bot.db.database import Database
from bot.handlers.onboarding import PRESET_DAYS
from bot.handlers.supplements import send_plan
from bot.handlers.workout import process_set_text
from bot.prompts import FOOD_PROMPT, TECHNIQUE_PROMPT, system_prompt
from bot.services import guard
from bot.services.intents import ScheduleChange, parse_schedule_change, parse_supp_intent
from bot.services.llm import LLM, LLMError, Media
from bot.services.profile import build_profile
from bot.services.reminders import Reminders
from bot.services.timeutil import WEEKDAYS_RU, fmt_days, next_training, parse_days
from bot.textfmt import format_llm, reply_long
from bot.services.workout import WorkoutService
from bot.ui import MENU_BUTTONS, Ph, inline

log = logging.getLogger(__name__)

router = Router()
router.message.filter(F.chat.type == "private")

MAX_VIDEO_MB = 15
MAX_VIDEO_SEC = 40
PENDING_TTL = 15 * 60
# Фото ждёт ответа «еда или техника». Храним только file_id, сами фото не сохраняем.
_pending: dict[str, tuple[str, float]] = {}


async def apply_schedule(message: Message, user: dict, change: ScheduleChange, db: Database, reminders: Reminders) -> None:
    """«Напоминай в 19:30», «тренируюсь по пн, ср, пт», «4 раза в неделю»: меняем расписание из чата."""
    fields: dict = {}
    if change.time:
        fields["train_time"] = change.time
    days = change.days or (PRESET_DAYS[change.per_week] if change.per_week else None)
    if days is not None:
        if len(days) not in (2, 3, 4):
            await message.answer(
                f"Могу составить программу на 2, 3 или 4 тренировки в неделю, а ты назвал {len(days)}. "
                "Напиши, например: «тренируюсь по пн, ср, пт»."
            )
            if not fields:
                return
        else:
            fields.update(days=",".join(map(str, days)), days_per_week=len(days), rotation_idx=0)
    await db.update_user(user["tg_id"], **fields)
    fresh = await db.get_user(user["tg_id"])
    assert fresh
    reminders.schedule_user(fresh)
    nxt = next_training(parse_days(fresh["days"]), fresh["train_time"], fresh["tz"])
    when = f" Ближайшая: {WEEKDAYS_RU[nxt.weekday()]} {nxt:%d.%m} в {nxt:%H:%M}." if nxt else ""
    await message.answer(
        f"Готово. Тренировки: {fmt_days(parse_days(fresh['days']))} в {fresh['train_time']}.{when}\n"
        "Не так — просто напиши ещё раз, например «напоминай в 20:00»."
    )


async def try_commands(
    message: Message, user: dict, text: str, db: Database, llm: LLM, settings: Settings, reminders: Reminders,
) -> bool:
    """Настройки и добавки обычным текстом. True = запрос обработан, ИИ-чат не нужен."""
    change = parse_schedule_change(text)
    if change:
        await apply_schedule(message, user, change, db, reminders)
        return True
    supp = parse_supp_intent(text)
    if supp:
        ids, own = supp
        if own:
            for sid in ids:
                await db.stack_add(user["tg_id"], sid)
        owned = await db.stack_list(user["tg_id"]) if own else ids
        await send_plan(message, user, owned, db, llm, settings)
        return True
    return False


async def ask_ai(
    message: Message, text: str, db: Database, llm: LLM, settings: Settings, reminders: Reminders,
) -> None:
    user = await db.get_user(message.from_user.id)
    if not user or not user["onboarded"]:
        await message.answer("Сначала короткая анкета: /start. Потом отвечу на любые вопросы.")
        return
    hit = guard.check(text, user["age"])
    # «У меня есть предтрен»: план сам скажет «не принимай» без доз, жёсткий отказ тут не нужен.
    if hit and not (hit.reason == "minor_stimulant" and parse_supp_intent(text)):
        await message.answer(hit.reply)
        return
    if await try_commands(message, user, text, db, llm, settings, reminders):
        return
    if not llm.available:
        await message.answer("ИИ не подключён: в .env нет ключей. Тренировки и напоминания работают.")
        return
    if not await db.take_quota(message.from_user.id, "llm", settings.limit_llm_per_day):
        await message.answer("Лимит вопросов на сегодня исчерпан. Завтра снова. Тренировки и план работают без лимита.")
        return
    await message.bot.send_chat_action(message.chat.id, "typing")
    system = system_prompt(await build_profile(db, user))
    history = await db.chat_recent(message.from_user.id, 8)
    try:
        answer = await llm.chat(system, history, text)
    except LLMError as e:
        log.warning("LLM недоступен: %s", e)
        await message.answer("ИИ сейчас недоступен, попробуй через минуту.")
        return
    await db.chat_add(message.from_user.id, "user", text)
    await db.chat_add(message.from_user.id, "model", answer)
    await reply_long(message, format_llm(answer))


# ---------- текст ----------
@router.message(F.text, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTONS), StateFilter(None))
async def on_text(message: Message, db: Database, llm: LLM, settings: Settings, reminders: Reminders):
    await ask_ai(message, message.text, db, llm, settings, reminders)


# ---------- голос ----------
@router.message(F.voice)
async def on_voice(message: Message, bot: Bot, state: FSMContext, db: Database, llm: LLM,
                   settings: Settings, workouts: WorkoutService, reminders: Reminders):
    if await state.get_state():
        await message.answer("Сейчас идёт анкета: ответь, пожалуйста, текстом.")
        return
    if not llm.groq:
        await message.answer("Голос не подключён: нет GROQ_API_KEY.")
        return
    if (message.voice.file_size or 0) > 5 * 1024 * 1024 or (message.voice.duration or 0) > 60:
        await message.answer("Голосовое длиннее минуты не беру. Скажи покороче.")
        return
    if not await db.take_quota(message.from_user.id, "stt", settings.limit_stt_per_day):
        await message.answer("Лимит голосовых на сегодня исчерпан. Пиши текстом.")
        return
    buf = io.BytesIO()
    await bot.download(message.voice, destination=buf)
    try:
        text = await llm.transcribe(buf.getvalue())
    except Exception as e:  # noqa: BLE001
        log.warning("STT: %r", e)
        await message.answer("Не смог расшифровать голос. Попробуй ещё раз или напиши.")
        return
    if not text:
        await message.answer("Ничего не расслышал. Повтори, пожалуйста.")
        return
    await message.answer(f"🎙 {html.escape(text)}")
    w = await db.active_workout(message.from_user.id)
    if w and await process_set_text(message, w, text, db, workouts, llm, settings):
        return
    await ask_ai(message, text, db, llm, settings, reminders)


# ---------- фото ----------
def _gc() -> None:
    now = time.time()
    for k in [k for k, (_, ts) in _pending.items() if now - ts > PENDING_TTL]:
        _pending.pop(k, None)


@router.message(F.photo)
async def on_photo(message: Message, db: Database, llm: LLM, settings: Settings):
    user = await db.get_user(message.from_user.id)
    if not user or not user["onboarded"]:
        await message.answer("Сначала анкета: /start")
        return
    if not llm.available:
        await message.answer("ИИ не подключён, фото разобрать не могу.")
        return
    _gc()
    key = f"{message.chat.id}:{message.message_id}"
    _pending[key] = (message.photo[-1].file_id, time.time())
    caption = (message.caption or "").lower()
    kind = "food" if any(w in caption for w in ("еда", "поел", "обед", "ужин", "завтрак")) else (
        "tech" if any(w in caption for w in ("техник", "форма", "упражн")) else None)
    if kind:
        await analyze_photo(message, message.from_user.id, kind, str(message.message_id), db, llm, settings)
        return
    await message.reply(
        "Что на фото?",
        reply_markup=inline([[("🍽 Еда", Ph(kind="food", key=str(message.message_id))),
                              ("🏋️ Техника упражнения", Ph(kind="tech", key=str(message.message_id)))]]),
    )


async def analyze_photo(message: Message, user_id: int, kind: str, mid: str, db: Database, llm: LLM, settings: Settings) -> None:
    entry = _pending.pop(f"{message.chat.id}:{mid}", None)
    if not entry:
        await message.answer("Фото устарело. Пришли заново.")
        return
    if not await db.take_quota(user_id, "vision", settings.limit_vision_per_day):
        await message.answer("Лимит разборов фото и видео на сегодня исчерпан.")
        return
    buf = io.BytesIO()
    await message.bot.download(entry[0], destination=buf)
    user = await db.get_user(user_id)
    assert user
    system = system_prompt(await build_profile(db, user), FOOD_PROMPT if kind == "food" else TECHNIQUE_PROMPT)
    await message.bot.send_chat_action(message.chat.id, "typing")
    try:
        answer = await llm.chat(
            system, [], "Разбери это фото." if kind == "food" else "Разбери технику по этому фото.",
            media=Media(buf.getvalue(), "image/jpeg"), max_tokens=600,
        )
    except LLMError as e:
        log.warning("Vision недоступен: %s", e)
        await message.answer("Не получилось разобрать фото. Попробуй позже.")
        return
    await reply_long(message, format_llm(answer))


@router.callback_query(Ph.filter())
async def on_photo_kind(cb: CallbackQuery, callback_data: Ph, db: Database, llm: LLM, settings: Settings):
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    await analyze_photo(cb.message, cb.from_user.id, callback_data.kind, callback_data.key, db, llm, settings)


# ---------- видео техники ----------
@router.message(F.video)
async def on_video(message: Message, bot: Bot, db: Database, llm: LLM, settings: Settings):
    user = await db.get_user(message.from_user.id)
    if not user or not user["onboarded"]:
        await message.answer("Сначала анкета: /start")
        return
    if not llm.gemini:
        await message.answer("Видео разбирает только Gemini, а ключ GEMINI_API_KEY не задан.")
        return
    v = message.video
    if (v.file_size or 0) > MAX_VIDEO_MB * 1024 * 1024 or (v.duration or 0) > MAX_VIDEO_SEC:
        await message.answer(f"Видео до {MAX_VIDEO_SEC} секунд и до {MAX_VIDEO_MB} МБ: один подход, снято сбоку.")
        return
    if not await db.take_quota(message.from_user.id, "vision", settings.limit_vision_per_day):
        await message.answer("Лимит разборов фото и видео на сегодня исчерпан.")
        return
    buf = io.BytesIO()
    await bot.download(v, destination=buf)
    system = system_prompt(await build_profile(db, user), TECHNIQUE_PROMPT)
    await bot.send_chat_action(message.chat.id, "typing")
    try:
        answer = await llm.chat(system, [], "Разбери технику на видео.", media=Media(buf.getvalue(), v.mime_type or "video/mp4"), max_tokens=600)
    except LLMError as e:
        log.warning("Video недоступно: %s", e)
        await message.answer("Не получилось разобрать видео. Попробуй позже.")
        return
    await reply_long(message, format_llm(answer))
