"""Прогресс-фото: «было → стало» и инструкция по съёмке."""
import html
from datetime import date

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import InputMediaPhoto, Message

from bot.db.database import Database
from bot.photo_guide import GUIDE

router = Router()
router.message.filter(F.chat.type == "private")


def _fmt_day(day: str) -> str:
    return date.fromisoformat(day).strftime("%d.%m.%Y")


async def _send_set(message: Message, items: list[dict], caption: str) -> None:
    """Один набор: альбом фото, кружок или видео. У кружка подписи нет, он идёт отдельным текстом."""
    photos = [i for i in items if i["kind"] == "photo"]
    if photos:
        if len(photos) == 1:
            await message.answer_photo(photos[0]["file_id"], caption=caption)
        else:
            await message.answer_media_group(
                [InputMediaPhoto(media=p["file_id"], caption=caption if n == 0 else None)
                 for n, p in enumerate(photos)]
            )
        return
    clip = items[0]
    if clip["kind"] == "video_note":
        await message.answer(caption)
        await message.answer_video_note(clip["file_id"])
    else:
        await message.answer_video(clip["file_id"], caption=caption)


@router.message(Command("photo_guide"))
async def cmd_photo_guide(message: Message):
    await message.answer(GUIDE)


@router.message(Command("progress"))
async def cmd_progress(message: Message, db: Database):
    user = await db.get_user(message.from_user.id)
    if not user or not user["onboarded"]:
        await message.answer("Сначала анкета: /start")
        return
    days = await db.progress_days(user["tg_id"])
    if not days:
        await message.answer(
            "Фото пока нет. Добавить: /measure → «Фото или кружок». Как снимать: /photo_guide."
        )
        return
    first, last = days[0], days[-1]
    await _send_set(message, await db.progress_media_on(user["tg_id"], first), f"Начало: {_fmt_day(first)}")
    if first == last:
        await message.answer(
            "Это пока единственный набор. Сними второй через 4–8 недель в тех же условиях "
            "(место, свет, время): /measure. Тогда покажу «было → стало»."
        )
        return
    await _send_set(message, await db.progress_media_on(user["tg_id"], last), f"Сейчас: {_fmt_day(last)}")
    weeks = max((date.fromisoformat(last) - date.fromisoformat(first)).days // 7, 1)
    await message.answer(
        f"Между кадрами {weeks} нед. Сравнивай форму плеч, талию и осанку, а не один день. "
        f"Дата последнего набора: {html.escape(_fmt_day(last))}."
    )
