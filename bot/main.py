import asyncio
import logging

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, CallbackQuery, ErrorEvent, Message

from pydantic import ValidationError

from bot.config import get_settings
from bot.db.database import Database
from bot.middlewares import DoubleTapMiddleware, UserSerialMiddleware
from bot.handlers import chat, info, onboarding, progress, supplements, workout
from bot.services.exercise_db import ExerciseDB
from bot.services.llm import LLM
from bot.services.reminders import Reminders
from bot.services.workout import WorkoutService

log = logging.getLogger("gymbro")

COMMANDS = [
    BotCommand(command="today", description="Тренировка сегодня"),
    BotCommand(command="plan", description="Моя программа"),
    BotCommand(command="stats", description="Прогресс и серия"),
    BotCommand(command="measure", description="Новые замеры (лента, фото, кружок)"),
    BotCommand(command="progress", description="Фото «было → стало»"),
    BotCommand(command="photo_guide", description="Как снимать фото и кружок"),
    BotCommand(command="supplements", description="Добавки без рекламы"),
    BotCommand(command="exercise", description="Анимация и техника упражнения"),
    BotCommand(command="library", description="Каталог упражнений"),
    BotCommand(command="settings", description="Настройки"),
    BotCommand(command="help", description="Помощь"),
    BotCommand(command="privacy", description="Какие данные я храню"),
    BotCommand(command="delete_me", description="Удалить мои данные"),
]


class AccessMiddleware(BaseMiddleware):
    """Если задан ALLOWED_USER_IDS, пускаем только этих пользователей."""

    def __init__(self, allowed: list[int]):
        self.allowed = allowed

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if self.allowed and (user is None or user.id not in self.allowed):
            if isinstance(event, Message):
                await event.answer("Бот пока закрытый.")
            elif isinstance(event, CallbackQuery):
                await event.answer("Бот пока закрытый.", show_alert=True)
            return None
        return await handler(event, data)


async def on_error(event: ErrorEvent) -> bool:
    log.exception("Необработанная ошибка", exc_info=event.exception)
    update = event.update
    target = update.message or (update.callback_query.message if update.callback_query else None)
    if target:
        try:
            await target.answer("Что-то пошло не так. Я записал ошибку, попробуй ещё раз.")
        except Exception:  # noqa: BLE001
            pass
    return True


def build_dispatcher(*, settings, db, edb, llm, workouts, reminders) -> Dispatcher:
    dp = Dispatcher(
        storage=MemoryStorage(), db=db, edb=edb, llm=llm, settings=settings,
        workouts=workouts, reminders=reminders,
    )
    access = AccessMiddleware(settings.allowed_user_ids)
    serial = UserSerialMiddleware()
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)
    dp.message.outer_middleware(serial)
    dp.callback_query.outer_middleware(serial)
    dp.callback_query.outer_middleware(DoubleTapMiddleware())
    dp.errors.register(on_error)
    # Порядок важен: ИИ-чат с «ловит всё» идёт последним.
    for r in (onboarding.router, workout.router, info.router, supplements.router, progress.router, chat.router):
        dp.include_router(r)
    return dp


# Шумные библиотеки: на каждый апдейт и запрос пишут INFO, журнал на сервере раздувается.
_NOISY = ("httpx", "httpcore", "aiogram.event", "aiogram.dispatcher", "apscheduler", "google_genai",
          "google.genai", "urllib3", "aiohttp", "groq")


def setup_logging(level: str) -> None:
    lvl = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(level=lvl, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for name in _NOISY:
        logging.getLogger(name).setLevel(max(lvl, logging.WARNING))


async def run() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    db = Database(settings.db_path)
    await db.connect()
    edb = ExerciseDB(cache_dir=settings.db_path.parent / "cache")
    await edb.load()
    workouts = WorkoutService(db, edb)
    reminders = Reminders(bot, db, workouts)
    llm = LLM(settings)

    dp = build_dispatcher(
        settings=settings, db=db, edb=edb, llm=llm, workouts=workouts, reminders=reminders
    )

    await bot.set_my_commands(COMMANDS)
    await reminders.start()
    log.info("Бот запущен. ИИ: gemini=%s groq=%s", bool(llm.gemini), bool(llm.groq))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        reminders.shutdown()
        await db.close()
        await bot.session.close()


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        pass
    except ValidationError as e:
        raise SystemExit(f"Не хватает настроек в .env (скопируй .env.example): {e.errors()[0]['loc']}") from None
    except TelegramUnauthorizedError:
        raise SystemExit("Telegram отклонил BOT_TOKEN. Проверь токен от @BotFather в .env.") from None


if __name__ == "__main__":
    main()
