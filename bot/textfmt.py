"""Оформление ответов модели для Telegram."""
import html
import re

from aiogram.types import Message


def format_llm(text: str) -> str:
    """Ответ модели в безопасный HTML для Telegram."""
    t = html.escape(text.strip())
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t, flags=re.S)
    t = re.sub(r"^\s*[\*\-]\s+", "• ", t, flags=re.M)
    return t


async def reply_long(message: Message, text: str) -> None:
    for i in range(0, len(text), 3800):
        await message.answer(text[i:i + 3800])
