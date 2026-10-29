"""Добавки: отметил, что у тебя есть, и бот считает план по весу. Справочник «работает / мусор» рядом."""
import html
import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from bot.config import Settings
from bot.data import supplements
from bot.db.database import Database
from bot.prompts import SUPP_COMMENT_PROMPT, system_prompt
from bot.services.guard import MINOR_AGE
from bot.services.llm import LLM, LLMError
from bot.services.profile import build_profile
from bot.services.reminders import Reminders
from bot.services.supp_advice import TEEN_FOOTER
from bot.services.supp_plan import build_plan, plan_facts, render_plan
from bot.services.timeutil import parse_time
from bot.textfmt import format_llm
from bot.ui import BTN_SUPP, Sup, inline

log = logging.getLogger(__name__)

router = Router()
router.message.filter(F.chat.type == "private")

VERDICTS = {
    "works": "✅ Работает",
    "conditional": "🟡 Условно",
    "useless": "❌ Мусор",
    "dangerous": "☠️ Опасно",
}
VERDICT_ICON = {k: v.split()[0] for k, v in VERDICTS.items()}

MAIN_TEXT = (
    "<b>Добавки без рекламы</b>\n"
    "Отметь, что у тебя уже есть или что хочешь купить. Я посчитаю по твоему весу: сколько принимать, "
    "когда, а что не нужно вообще.\n"
    "Можно и текстом: «у меня есть креатин и протеин».\n\n"
    "Порядок важности: еда → сон → прогрессия нагрузки → и только потом добавки."
)
LEGEND = (
    "<b>Справочник</b>\n"
    "✅ работает · 🟡 только в определённых случаях · ❌ деньги на ветер · ☠️ вредно\n"
    "Оценка доказательств: A (сильные) → D (слабые или нет)."
)
MINOR_HEAD = (
    "\n\n<b>Тебе до 18</b>: даю малые дозы для безопасных добавок (креатин, протеин, омега-3, витамин D, "
    "мультивитамины). Кофеин, предтренировочные, жиросжигатели, тестобустеры и диеты — нет. "
    "Скажи родителям."
)


def _is_adult(user: dict | None) -> bool:
    return bool(user and (user.get("age") or 0) >= MINOR_AGE)


def main_keyboard():
    return inline([
        [("✅ Отметить, что у меня есть", Sup(act="own"))],
        [("🧮 Мой план приёма", Sup(act="plan"))],
        [("📚 Справочник: что работает", Sup(act="list"))],
    ])


def own_keyboard(owned: set[str]):
    items = list(supplements().values())
    rows, row = [], []
    for s in items:
        mark = "✅ " if s["id"] in owned else ""
        row.append((f"{mark}{s['name'].split(' (')[0]}", Sup(act="tg", id=s["id"])))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([("🧮 Рассчитать план", Sup(act="plan"))])
    return inline(rows)


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
    rows.append([("◀ Назад", Sup(act="main"))])
    return inline(rows)


def card_text(s: dict, adult: bool) -> str:
    t = s["teen"]
    who = s["who"] if adult or not t["ok"] else t["who"]
    lines = [
        f"<b>{html.escape(s['name'])}</b>",
        f"{VERDICTS[s['verdict']]} · доказательства: {s['evidence']}",
        "",
        html.escape(s["summary"]),
        f"\n<b>Кому</b>: {html.escape(who)}",
    ]
    if adult:
        lines.append(f"<b>Дозировка</b>: {html.escape(s['dose'])}")
        lines.append(f"<b>Риски</b>: {html.escape(s['risks'])}")
    elif t["ok"]:
        lines.append(f"<b>Тебе (до 18)</b>: {html.escape(t['dose'])}")
        lines.append(f"<b>Важно</b>: {html.escape(t['note'])}")
        lines.append(f"<b>Риски</b>: {html.escape(s['risks'])}")
        lines.append(f"<i>{TEEN_FOOTER}</i>")
    else:
        lines.append(f"<b>Тебе</b>: {html.escape(t['note'])}")
    lines.append(f"\n<i>Источники: {html.escape(s['sources'])}</i>")
    return "\n".join(lines)


async def send_plan(
    message: Message, user: dict, owned: list[str], db: Database, llm: LLM, settings: Settings,
) -> None:
    """План по весу одним сообщением; ИИ добавляет короткий комментарий под этого человека."""
    plan = build_plan(user, owned)
    text = render_plan(plan)
    if plan.lines and llm.available and await db.take_quota(user["tg_id"], "llm", settings.limit_llm_per_day):
        try:
            system = system_prompt(await build_profile(db, user), SUPP_COMMENT_PROMPT)
            comment = await llm.chat(system, [], plan_facts(plan), max_tokens=350)
            text += "\n\n🧠 <b>Комментарий тренера</b>\n" + format_llm(comment)
        except LLMError as e:
            log.warning("Комментарий к плану добавок без ИИ: %s", e)
    kb = inline([
        [("✏️ Изменить набор", Sup(act="own")), ("🔔 Напоминания", Sup(act="remind"))],
    ]) if plan.lines else main_keyboard()
    for i in range(0, len(text), 3800):
        last = i + 3800 >= len(text)
        await message.answer(text[i:i + 3800], reply_markup=kb if last else None)


@router.message(Command("supplements", "supp"))
@router.message(F.text == BTN_SUPP)
async def cmd_supplements(message: Message, db: Database):
    user = await db.get_user(message.from_user.id)
    await message.answer(MAIN_TEXT + ("" if _is_adult(user) else MINOR_HEAD), reply_markup=main_keyboard())


@router.callback_query(Sup.filter(F.act == "main"))
async def on_main(cb: CallbackQuery, db: Database):
    await cb.answer()
    user = await db.get_user(cb.from_user.id)
    await cb.message.edit_text(MAIN_TEXT + ("" if _is_adult(user) else MINOR_HEAD), reply_markup=main_keyboard())


@router.callback_query(Sup.filter(F.act == "list"))
async def on_list(cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_text(LEGEND, reply_markup=list_keyboard())


@router.callback_query(Sup.filter(F.act == "show"))
async def on_show(cb: CallbackQuery, callback_data: Sup, db: Database):
    s = supplements().get(callback_data.id)
    if not s:
        await cb.answer("Не нашёл.")
        return
    await cb.answer()
    user = await db.get_user(cb.from_user.id)
    owned = s["id"] in await db.stack_list(cb.from_user.id)
    rows = [[("➖ Убрать из моего набора" if owned else "➕ У меня есть",
              Sup(act="tgc", id=s["id"]))], [("◀ Справочник", Sup(act="list"))]]
    await cb.message.edit_text(card_text(s, _is_adult(user)), reply_markup=inline(rows))


async def _toggle(db: Database, tg_id: int, sid: str) -> set[str]:
    owned = set(await db.stack_list(tg_id))
    if sid in owned:
        await db.stack_remove(tg_id, sid)
        owned.discard(sid)
    else:
        await db.stack_add(tg_id, sid)
        owned.add(sid)
    return owned


@router.callback_query(Sup.filter(F.act == "own"))
async def on_own(cb: CallbackQuery, db: Database):
    await cb.answer()
    owned = set(await db.stack_list(cb.from_user.id))
    await cb.message.edit_text(
        "<b>Что у тебя есть?</b> Нажимай на добавки, которые уже есть или которые собираешься купить. "
        "Потом «Рассчитать план». Лишнее я так и скажу: не нужно.",
        reply_markup=own_keyboard(owned),
    )


@router.callback_query(Sup.filter(F.act == "tg"))
async def on_toggle(cb: CallbackQuery, callback_data: Sup, db: Database):
    if callback_data.id not in supplements():
        await cb.answer("Не нашёл.")
        return
    await cb.answer()
    owned = await _toggle(db, cb.from_user.id, callback_data.id)
    await cb.message.edit_reply_markup(reply_markup=own_keyboard(owned))


@router.callback_query(Sup.filter(F.act == "tgc"))
async def on_toggle_card(cb: CallbackQuery, callback_data: Sup, db: Database):
    if callback_data.id not in supplements():
        await cb.answer("Не нашёл.")
        return
    owned = await _toggle(db, cb.from_user.id, callback_data.id)
    await cb.answer("Добавил в мой набор. План: /stack" if callback_data.id in owned else "Убрал.")
    rows = [[("➖ Убрать из моего набора" if callback_data.id in owned else "➕ У меня есть",
              Sup(act="tgc", id=callback_data.id))], [("◀ Справочник", Sup(act="list"))]]
    await cb.message.edit_reply_markup(reply_markup=inline(rows))


async def _plan_for(message: Message, tg_id: int, db: Database, llm: LLM, settings: Settings) -> None:
    user = await db.get_user(tg_id)
    if not user or not user["onboarded"]:
        await message.answer("Сначала анкета: /start")
        return
    owned = await db.stack_list(tg_id)
    if not owned:
        await message.answer(
            "Набор пуст. Отметь, что у тебя есть, или напиши «у меня есть креатин и протеин».",
            reply_markup=main_keyboard(),
        )
        return
    await send_plan(message, user, owned, db, llm, settings)


@router.callback_query(Sup.filter(F.act == "plan"))
async def on_plan(cb: CallbackQuery, db: Database, llm: LLM, settings: Settings):
    await cb.answer()
    await _plan_for(cb.message, cb.from_user.id, db, llm, settings)


@router.message(Command("stack"))
async def cmd_stack(message: Message, db: Database, llm: LLM, settings: Settings):
    await _plan_for(message, message.from_user.id, db, llm, settings)


@router.callback_query(Sup.filter(F.act == "remind"))
async def on_remind_toggle(cb: CallbackQuery, db: Database, reminders: Reminders):
    user = await db.get_user(cb.from_user.id)
    if not user or not user["onboarded"]:
        await cb.answer("Сначала анкета: /start", show_alert=True)
        return
    new = 0 if user["supp_remind"] else 1
    await db.update_user(cb.from_user.id, supp_remind=new)
    reminders.schedule_user((await db.get_user(cb.from_user.id)))  # type: ignore[arg-type]
    await cb.answer("Включил ежедневное напоминание" if new else "Выключил")
    if new:
        await cb.message.answer(
            f"Напомню в {user['supp_time']} каждый день, что принять. Время: <code>/supp_time 09:00</code>"
        )


@router.message(Command("supp_time"))
async def cmd_supp_time(message: Message, command: CommandObject, db: Database, reminders: Reminders):
    user = await db.get_user(message.from_user.id)
    if not user or not user["onboarded"]:
        await message.answer("Сначала анкета: /start")
        return
    t = parse_time(command.args or "")
    if not t:
        await message.answer("Формат: <code>/supp_time 09:00</code>")
        return
    await db.update_user(message.from_user.id, supp_time=t)
    reminders.schedule_user((await db.get_user(message.from_user.id)))  # type: ignore[arg-type]
    await message.answer(f"Время напоминания: {t}.")

