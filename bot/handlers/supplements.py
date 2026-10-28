"""Добавки: честный справочник «работает / условно / мусор / опасно»."""
import html

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from bot.data import supplements
from bot.db.database import Database
from bot.services.guard import MINOR_AGE
from bot.services.reminders import Reminders
from bot.services.timeutil import parse_time
from bot.ui import BTN_SUPP, Sup, inline

router = Router()
router.message.filter(F.chat.type == "private")

VERDICTS = {
    "works": "✅ Работает",
    "conditional": "🟡 Условно",
    "useless": "❌ Мусор",
    "dangerous": "☠️ Опасно",
}
VERDICT_ICON = {k: v.split()[0] for k, v in VERDICTS.items()}

LEGEND = (
    "<b>Добавки без рекламы</b>\n"
    "✅ работает · 🟡 только в определённых случаях · ❌ деньги на ветер · ☠️ вредно\n"
    "Оценка доказательств: A (сильные) → D (слабые или нет).\n"
    "Порядок важности: еда → сон → прогрессия нагрузки → и только потом добавки.\n"
    "Я не врач, это справка, а не назначение."
)
MINOR_HEAD = (
    "\nТебе нет 18, поэтому дозы и схемы приёма я не показываю. Сначала еда и сон; "
    "если захочется добавку — обсуди с родителями и врачом."
)


def _is_adult(user: dict | None) -> bool:
    return bool(user and (user.get("age") or 0) >= MINOR_AGE)


def list_keyboard():
    items = list(supplements().values())
    order = {"works": 0, "conditional": 1, "useless": 2, "dangerous": 3}
    items.sort(key=lambda s: order[s["verdict"]])
    rows, row = [], []
    for s in items:
        row.append((f"{VERDICT_ICON[s['verdict']]} {s['name'].split(' (')[0]}", Sup(act="show", id=s["id"])))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return inline(rows)


def card_text(s: dict, adult: bool) -> str:
    lines = [
        f"<b>{html.escape(s['name'])}</b>",
        f"{VERDICTS[s['verdict']]} · доказательства: {s['evidence']}",
        "",
        html.escape(s["summary"]),
        f"\n<b>Кому</b>: {html.escape(s['who'])}",
    ]
    if adult:
        lines.append(f"<b>Дозировка</b>: {html.escape(s['dose'])}")
        lines.append(f"<b>Риски</b>: {html.escape(s['risks'])}")
    else:
        lines.append(f"<b>Тебе</b>: {html.escape(s['minor_note'])}")
        lines.append("<i>Дозировки для тех, кому нет 18, не даю.</i>")
    lines.append(f"\n<i>Источники: {html.escape(s['sources'])}</i>")
    return "\n".join(lines)


@router.message(Command("supplements", "supp"))
@router.message(F.text == BTN_SUPP)
async def cmd_supplements(message: Message, db: Database):
    user = await db.get_user(message.from_user.id)
    text = LEGEND + ("" if _is_adult(user) else MINOR_HEAD)
    await message.answer(text, reply_markup=list_keyboard())


@router.callback_query(Sup.filter(F.act == "list"))
async def on_list(cb: CallbackQuery, db: Database):
    await cb.answer()
    user = await db.get_user(cb.from_user.id)
    await cb.message.edit_text(LEGEND + ("" if _is_adult(user) else MINOR_HEAD), reply_markup=list_keyboard())


@router.callback_query(Sup.filter(F.act == "show"))
async def on_show(cb: CallbackQuery, callback_data: Sup, db: Database):
    s = supplements().get(callback_data.id)
    if not s:
        await cb.answer("Не нашёл.")
        return
    await cb.answer()
    user = await db.get_user(cb.from_user.id)
    adult = _is_adult(user)
    rows = []
    if adult and s["verdict"] in ("works", "conditional"):
        in_stack = s["id"] in await db.stack_list(cb.from_user.id)
        rows.append([("➖ Убрать из набора" if in_stack else "➕ В мой набор",
                      Sup(act="rm" if in_stack else "add", id=s["id"]))])
    rows.append([("◀ Все добавки", Sup(act="list"))])
    await cb.message.edit_text(card_text(s, adult), reply_markup=inline(rows))


@router.callback_query(Sup.filter(F.act.in_({"add", "rm"})))
async def on_stack_change(cb: CallbackQuery, callback_data: Sup, db: Database, reminders: Reminders):
    user = await db.get_user(cb.from_user.id)
    if not _is_adult(user):
        await cb.answer("Набор доступен с 18 лет.", show_alert=True)
        return
    if callback_data.act == "add":
        await db.stack_add(cb.from_user.id, callback_data.id)
        await cb.answer("Добавлено. Мой набор: /stack")
    else:
        await db.stack_remove(cb.from_user.id, callback_data.id)
        await cb.answer("Убрано.")
    reminders.schedule_user(user)  # type: ignore[arg-type]


@router.message(Command("stack"))
async def cmd_stack(message: Message, db: Database):
    user = await db.get_user(message.from_user.id)
    if not _is_adult(user):
        await message.answer("Личный набор и напоминания про добавки доступны с 18 лет. "
                             "Сейчас: еда, сон, тренировки. Справка: /supplements")
        return
    stack = await db.stack_list(message.from_user.id)
    sup = supplements()
    if not stack:
        await message.answer("Набор пуст. Открой /supplements и добавь нужное (кнопка «В мой набор»).")
        return
    on = bool(user["supp_remind"])  # type: ignore[index]
    lines = "\n".join(f"• {html.escape(sup[s]['name'])}: {html.escape(sup[s]['dose'])}" for s in stack if s in sup)
    await message.answer(
        f"<b>Мой набор</b>\n{lines}\n\nНапоминание: {'вкл' if on else 'выкл'} в {user['supp_time']}. "  # type: ignore[index]
        "Время: <code>/supp_time 09:00</code>",
        reply_markup=inline([[("🔕 Выключить" if on else "🔔 Включить напоминание", Sup(act="remind"))]]),
    )


@router.callback_query(Sup.filter(F.act == "remind"))
async def on_remind_toggle(cb: CallbackQuery, db: Database, reminders: Reminders):
    user = await db.get_user(cb.from_user.id)
    if not _is_adult(user):
        await cb.answer("Только с 18 лет.", show_alert=True)
        return
    new = 0 if user["supp_remind"] else 1  # type: ignore[index]
    await db.update_user(cb.from_user.id, supp_remind=new)
    reminders.schedule_user((await db.get_user(cb.from_user.id)))  # type: ignore[arg-type]
    await cb.answer("Включено" if new else "Выключено")
    await cb.message.edit_reply_markup(reply_markup=None)


@router.message(Command("supp_time"))
async def cmd_supp_time(message: Message, command: CommandObject, db: Database, reminders: Reminders):
    user = await db.get_user(message.from_user.id)
    if not _is_adult(user):
        await message.answer("Напоминания про добавки доступны с 18 лет.")
        return
    t = parse_time(command.args or "")
    if not t:
        await message.answer("Формат: <code>/supp_time 09:00</code>")
        return
    await db.update_user(message.from_user.id, supp_time=t)
    reminders.schedule_user((await db.get_user(message.from_user.id)))  # type: ignore[arg-type]
    await message.answer(f"Время напоминания: {t}.")
