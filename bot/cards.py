"""Показ упражнений: GIF из внешней базы, русские шаги, запасные картинки."""
import html
import logging

from aiogram.types import InputMediaPhoto, Message

from bot import sources
from bot.data import Exercise
from bot.services.exercise_db import DbExercise, ExerciseDB

log = logging.getLogger(__name__)

# Telegram хранит загруженные GIF у себя: повторно отдаём по file_id, а не по URL.
_FILE_IDS: dict[str, str] = {}


async def send_animation(msg: Message, url: str) -> bool:
    try:
        sent = await msg.answer_animation(_FILE_IDS.get(url) or url)
    except Exception as e:  # noqa: BLE001 — не критично, есть запасной вариант
        log.info("GIF не отправился %s: %r", url, e)
        return False
    if url not in _FILE_IDS and sent.animation:
        _FILE_IDS[url] = sent.animation.file_id
    return True


async def send_exercise_media(msg: Message, ex: Exercise, edb: ExerciseDB) -> None:
    gif = edb.gif_url(ex.hasan)
    if gif and await send_animation(msg, gif):
        return
    try:
        await msg.answer_media_group([InputMediaPhoto(media=u) for u in ex.images])
    except Exception as e:  # noqa: BLE001
        log.info("картинки не отправились %s: %r", ex.id, e)


CAPTION_LIMIT = 1024  # лимит Telegram на подпись к медиа


async def send_exercise_card(msg: Message, ex: Exercise, edb: ExerciseDB, text: str, markup, with_media: bool = True) -> None:
    """Карточка упражнения одним сообщением: GIF с подписью и кнопками. Нет GIF — картинки и текст."""
    if with_media:
        gif = edb.gif_url(ex.hasan)
        if gif:
            if len(text) <= CAPTION_LIMIT:
                try:
                    sent = await msg.answer_animation(_FILE_IDS.get(gif) or gif, caption=text, reply_markup=markup)
                    if gif not in _FILE_IDS and sent.animation:
                        _FILE_IDS[gif] = sent.animation.file_id
                    return
                except Exception as e:  # noqa: BLE001 — идём к запасным картинкам
                    log.info("GIF с подписью не отправился %s: %r", gif, e)
            elif await send_animation(msg, gif):
                await msg.answer(text, reply_markup=markup)
                return
        try:
            await msg.answer_media_group([InputMediaPhoto(media=u) for u in ex.images])
        except Exception as e:  # noqa: BLE001
            log.info("картинки не отправились %s: %r", ex.id, e)
    await msg.answer(text, reply_markup=markup)


def steps_text(title: str, item: DbExercise | None, empty: str = "Подробной инструкции в базе нет.") -> str:
    lines = [f"📖 <b>{html.escape(title)}</b>"]
    if item and item.steps_ru:
        lines += [f"{i}. {html.escape(s)}" for i, s in enumerate(item.steps_ru, 1)]
        extra = [m for m in item.secondary if m]
        if extra:
            lines.append("\nЕщё работают: " + html.escape(", ".join(dict.fromkeys(extra))))
    else:
        lines.append(empty)
    lines.append(f"\n<i>{html.escape(sources.ATTRIBUTION)}</i>")
    return "\n".join(lines)[:3900]
