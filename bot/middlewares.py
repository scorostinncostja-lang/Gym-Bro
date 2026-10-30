"""Защита от гонок: двойное нажатие кнопки и пачка быстрых сообщений.

aiogram обрабатывает апдейты параллельно. Без очереди два быстрых нажатия на «Начать»
отправляют две карточки упражнения, а два быстрых подхода пишутся в одно и то же состояние.
"""
import asyncio
import time

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery

# Кнопки, повтор которых бессмыслен. Переключатели (дни недели, места, добавки) сюда не входят.
_ONCE_PREFIXES = ("rem:", "wk:", "fl:", "ob:pdone", "ob:mnone", "ob:mtape", "ob:mphoto")


class UserSerialMiddleware(BaseMiddleware):
    """Апдейты одного пользователя идут строго по очереди."""

    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None or data.get("_voice"):  # расшифровка голосового идёт внутри уже занятой очереди
            return await handler(event, data)
        if len(self._locks) > 2000:  # не копим замки бесконечно
            self._locks = {k: v for k, v in self._locks.items() if v.locked()}
        lock = self._locks.setdefault(user.id, asyncio.Lock())
        async with lock:
            return await handler(event, data)


class DoubleTapMiddleware(BaseMiddleware):
    """Повторное нажатие той же кнопки на том же сообщении в течение пары секунд игнорируется."""

    def __init__(self, ttl: float = 3.0) -> None:
        self.ttl = ttl
        self._done: dict[tuple, float] = {}

    async def __call__(self, handler, event: CallbackQuery, data):
        if not (event.data and event.data.startswith(_ONCE_PREFIXES)):
            return await handler(event, data)
        key = (event.from_user.id, event.message.message_id if event.message else 0, event.data)
        now = time.monotonic()
        if len(self._done) > 500:
            self._done = {k: t for k, t in self._done.items() if now - t < self.ttl}
        if now - self._done.get(key, -1e9) < self.ttl:
            await event.answer()
            return None
        try:
            return await handler(event, data)
        finally:
            self._done[key] = time.monotonic()


class StateEscapeMiddleware(BaseMiddleware):
    """Команда или кнопка меню посреди настройки отменяет её.

    Иначе человек, который начал менять время и передумал, застревает: его «/today» или «40 10»
    принимаются за ответ на вопрос настройки. Во время первой анкеты состояние не трогаем:
    после /help можно спокойно продолжить отвечать.
    """

    def __init__(self, menu_buttons: set[str]) -> None:
        self.menu = menu_buttons

    async def __call__(self, handler, event, data):
        text = getattr(event, "text", None) or ""
        state = data.get("state")
        if state is not None and (text.startswith("/") and not text.startswith("/start") or text in self.menu):
            if await state.get_state() is not None:
                db = data.get("db")
                user = await db.get_user(event.from_user.id) if db and event.from_user else None
                if user and user.get("onboarded"):
                    await state.clear()
        return await handler(event, data)
