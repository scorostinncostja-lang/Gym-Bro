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


def split_message(text: str, limit: int = 3800) -> list[str]:
    """Режет по строкам, чтобы не разорвать HTML-тег: Telegram отклоняет сообщение с битой разметкой."""
    parts: list[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > limit:  # одна гигантская строка: режем по пробелу
            cut = line.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:cut])
            line = line[cut:].lstrip()
        if cur and len(cur) + 1 + len(line) > limit:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts or [""]


async def reply_long(message: Message, text: str, reply_markup=None) -> None:
    chunks = split_message(text)
    for n, chunk in enumerate(chunks):
        await message.answer(chunk, reply_markup=reply_markup if n == len(chunks) - 1 else None)
