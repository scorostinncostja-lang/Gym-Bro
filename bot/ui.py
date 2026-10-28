"""Клавиатуры, данные кнопок и мелкие форматтеры."""
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup,
)

BTN_TODAY = "🏋️ Тренировка"
BTN_PLAN = "📅 План"
BTN_STATS = "📊 Прогресс"
BTN_SUPP = "💊 Добавки"
BTN_SETTINGS = "⚙️ Настройки"
MENU_BUTTONS = {BTN_TODAY, BTN_PLAN, BTN_STATS, BTN_SUPP, BTN_SETTINGS}


class Ob(CallbackData, prefix="ob"):
    act: str
    arg: str = ""


class Rem(CallbackData, prefix="rem"):
    act: str
    wid: int


class Wk(CallbackData, prefix="wk"):
    act: str
    wid: int


class Fl(CallbackData, prefix="fl"):
    wid: int
    sid: int
    v: int


class Sup(CallbackData, prefix="sup"):
    act: str
    id: str = ""


class St(CallbackData, prefix="st"):
    act: str
    arg: str = ""


class Ph(CallbackData, prefix="ph"):
    kind: str
    key: str


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_TODAY), KeyboardButton(text=BTN_PLAN)],
            [KeyboardButton(text=BTN_STATS), KeyboardButton(text=BTN_SUPP)],
            [KeyboardButton(text=BTN_SETTINGS)],
        ],
        resize_keyboard=True,
    )


def inline(rows: list[list[tuple[str, CallbackData | str]]]) -> InlineKeyboardMarkup:
    """rows: [[(текст, callback_data | url)]]. Строка, начинающаяся с http, считается ссылкой."""
    out = []
    for row in rows:
        btns = []
        for text, data in row:
            if isinstance(data, str) and data.startswith("http"):
                btns.append(InlineKeyboardButton(text=text, url=data))
            else:
                cb = data.pack() if isinstance(data, CallbackData) else data
                btns.append(InlineKeyboardButton(text=text, callback_data=cb))
        out.append(btns)
    return InlineKeyboardMarkup(inline_keyboard=out)


def fmt_num(x: float | None) -> str:
    if x is None:
        return "—"
    return str(int(x)) if float(x).is_integer() else f"{x:g}"
