"""Последний роутер: то, что никто не обработал. Без него устаревшая кнопка крутит загрузку бесконечно."""
from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, Message

router = Router()


@router.callback_query()
async def stale_button(cb: CallbackQuery):
    await cb.answer("Эта кнопка устарела. Открой раздел заново через меню.", show_alert=False)


@router.message(F.chat.type == "private", F.sticker | F.document | F.animation | F.location | F.contact | F.audio)
async def unsupported(message: Message):
    await message.answer(
        "Такое я не понимаю. Пиши текстом или голосом, присылай фото (еда, техника, банка добавки) "
        "или видео техники. Все команды: /help"
    )


@router.message(F.chat.type == "private", ~StateFilter(None))
async def stuck_in_state(message: Message):
    await message.answer("Жду ответ на вопрос выше. Передумал — нажми /start или любую команду меню.")
