"""Добавки: отметил, что у тебя есть, и бот считает план по весу. Справочник «работает / мусор» рядом."""
import html
import io
import logging
import re

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot.config import Settings
from bot.data import supplements
from bot.db.database import Database
from bot.prompts import LABEL_PROMPT, SUPP_COMMENT_PROMPT, system_prompt
from bot.services.guard import MINOR_AGE
from bot.services.llm import LLM, LLMError, Media
from bot.services.location import country_of, parse_location
from bot.services.supp_label import evaluate, merge_labels, parse_label_json, render, render_price
from bot.services.supp_shop import TIERS, buyable_for, recommend
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
    "• <b>Уже купил:</b> пришли фото банки (лицо, состав, срок годности). Проверю состав и порции, "
    "честно скажу, если что-то не так, и посчитаю дозу под твой вес.\n"
    "• <b>Только планируешь:</b> подскажу, что искать, какие бренды-ориентиры и где заказать у тебя в стране: "
    "бюджетно, подходящее или лучшее.\n"
    "• <b>План приёма:</b> сколько, когда и во сколько, по твоему весу и цели.\n\n"
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


class SuppFlow(StatesGroup):
    photos = State()
    price = State()
    loc = State()


def main_keyboard():
    return inline([
        [("📸 Уже купил: проверить по фото", Sup(act="have"))],
        [("🛒 Планирую купить: что и где", Sup(act="buy"))],
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



# ---------- уже купил: проверка по фото ----------
MAX_LABEL_PHOTOS = 4


def _cancel_kb(extra: list | None = None):
    rows = [extra] if extra else []
    rows.append([("Отмена", Sup(act="cancel"))])
    return inline(rows)


@router.callback_query(Sup.filter(F.act == "cancel"))
async def on_cancel(cb: CallbackQuery, state: FSMContext):
    await cb.answer("Отменено")
    await state.clear()
    await cb.message.edit_reply_markup(reply_markup=None)


@router.callback_query(Sup.filter(F.act == "have"))
async def on_have(cb: CallbackQuery, state: FSMContext, db: Database):
    user = await db.get_user(cb.from_user.id)
    if not user or not user["onboarded"]:
        await cb.answer("Сначала анкета: /start", show_alert=True)
        return
    await cb.answer()
    await state.set_state(SuppFlow.photos)
    await state.set_data({"media": []})
    await cb.message.answer(
        "📸 <b>Пришли 1–4 фото добавки</b> (это банка, а не ты):\n"
        "1. Лицевая сторона с названием и брендом.\n"
        "2. Состав: «Supplement Facts» или «Пищевая ценность», чтобы были видны порция и числа.\n"
        "3. Срок годности, серия, данные импортёра и код маркировки.\n"
        "Свет ровный, без бликов. Фото могут идти по одному или альбомом. Нет возможности сфотографировать? "
        "Просто напиши состав текстом.",
        reply_markup=_cancel_kb(),
    )


@router.message(SuppFlow.photos, F.photo)
async def on_label_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    media = list(data.get("media", []))
    if len(media) >= MAX_LABEL_PHOTOS:
        return
    media.append(message.photo[-1].file_id)
    await state.update_data(media=media)
    await message.answer(
        f"Принял {len(media)} из {MAX_LABEL_PHOTOS}. Пришли ещё (состав, срок годности) или жми «Разобрать».",
        reply_markup=_cancel_kb([("🔍 Разобрать", Sup(act="analyze"))]),
    )


@router.callback_query(Sup.filter(F.act == "analyze"), SuppFlow.photos)
async def on_analyze(cb: CallbackQuery, state: FSMContext, db: Database, llm: LLM, settings: Settings):
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    data = await state.get_data()
    if not data.get("media"):
        await cb.message.answer("Фото пока нет. Пришли снимок банки или напиши состав текстом.")
        return
    await _analyze(cb.message, cb.from_user.id, data["media"], None, state, db, llm, settings, data.get("pid"))


@router.message(SuppFlow.photos, F.text, ~F.text.startswith("/"))
async def on_label_text(message: Message, state: FSMContext, db: Database, llm: LLM, settings: Settings):
    data = await state.get_data()
    await _analyze(message, message.from_user.id, data.get("media", []), message.text, state, db, llm, settings,
                   data.get("pid"))


async def _analyze(
    message: Message, uid: int, file_ids: list[str], text: str | None, state: FSMContext,
    db: Database, llm: LLM, settings: Settings, pid: int | None = None,
) -> None:
    """Разбор этикетки. pid: дослали фото к уже разобранной банке, данные объединяются."""
    user = await db.get_user(uid)
    assert user
    if not llm.available:
        await message.answer("Чтобы читать этикетки, нужен ключ ИИ (GEMINI_API_KEY или GROQ_API_KEY).")
        return
    if not await db.take_quota(uid, "vision", settings.limit_vision_per_day):
        await message.answer("Лимит разборов фото на сегодня исчерпан. Завтра снова или напиши состав текстом.")
        return
    await message.bot.send_chat_action(message.chat.id, "typing")
    media = []
    for fid in file_ids:
        buf = io.BytesIO()
        await message.bot.download(fid, destination=buf)
        media.append(Media(buf.getvalue(), "image/jpeg"))
    prompt = "Разбери этикетку по фото." if media else f"Этикетка текстом:\n{text}"
    if media and text:
        prompt += f"\nПользователь дописал: {text}"
    try:
        raw = await llm.chat(LABEL_PROMPT, [], prompt, media=media or None, max_tokens=700, temperature=0)
    except LLMError as e:
        log.warning("Этикетка без ИИ: %s", e)
        await message.answer("Не получилось прочитать этикетку: ИИ сейчас недоступен. Попробуй через минуту.")
        return
    label = parse_label_json(raw)
    if not label:
        await message.answer("Не смог разобрать ответ. Пришли более чёткое фото состава или напиши его текстом.")
        return
    old = next((p for p in await db.products(uid) if p["id"] == pid), None) if pid else None
    if old:
        label = merge_labels(old["data"], label)
    verdict = evaluate(label, user)
    if old:
        await db.update_product(old["id"], uid, verdict.supp_id, label.get("brand"), label)
        pid = old["id"]
    else:
        pid = await db.add_product(uid, verdict.supp_id, label.get("brand"), label)
    if verdict.supp_id in supplements():
        await db.stack_add(uid, verdict.supp_id)
    await state.clear()
    rows = [[("💰 Указать цену", Sup(act="price", id=str(pid)))]]
    if verdict.missing:
        rows.insert(0, [("📸 Дослать фото", Sup(act="more", id=str(pid)))])
    if verdict.supp_id in supplements():
        rows.append([("🧮 Мой план приёма", Sup(act="plan"))])
    rows.append([("➕ Проверить ещё одну", Sup(act="have"))])
    await message.answer(render(verdict), reply_markup=inline(rows))


@router.callback_query(Sup.filter(F.act == "more"))
async def on_more_photos(cb: CallbackQuery, callback_data: Sup, state: FSMContext):
    await cb.answer()
    await state.set_state(SuppFlow.photos)
    await state.set_data({"media": [], "pid": int(callback_data.id)})
    await cb.message.answer(
        "Пришли фото обратной стороны: таблица состава (Supplement Facts / Пищевая ценность), срок годности, "
        "данные импортёра. Дополню прошлый разбор.",
        reply_markup=_cancel_kb(),
    )


@router.callback_query(Sup.filter(F.act == "price"))
async def on_price(cb: CallbackQuery, callback_data: Sup, state: FSMContext):
    await cb.answer()
    await state.set_state(SuppFlow.price)
    await state.set_data({"pid": int(callback_data.id)})
    await cb.message.answer("Напиши цену упаковки числом (в своей валюте), например <code>12500</code>. "
                            "Посчитаю цену за день и за 100 г белка.", reply_markup=_cancel_kb())


@router.message(SuppFlow.price, F.text, ~F.text.startswith("/"))
async def on_price_value(message: Message, state: FSMContext, db: Database):
    m = re.search(r"\d[\d\s.,]*", message.text.replace("\xa0", " "))
    price = float(re.sub(r"[\s,]", "", m.group(0)).replace(",", ".")) if m else None
    if not price or price <= 0:
        await message.answer("Не понял цену. Напиши число, например 12500.")
        return
    data = await state.get_data()
    user = await db.get_user(message.from_user.id)
    prod = next((p for p in await db.products(message.from_user.id) if p["id"] == data["pid"]), None)
    await state.clear()
    if not user or not prod:
        await message.answer("Не нашёл эту добавку. Пришли фото заново.")
        return
    await db.set_product_price(prod["id"], message.from_user.id, price)
    await message.answer(render_price(evaluate(prod["data"], user), prod["data"], price))


# ---------- планирую купить: что и где ----------
@router.callback_query(Sup.filter(F.act == "buy"))
async def on_buy(cb: CallbackQuery, db: Database):
    user = await db.get_user(cb.from_user.id)
    if not user or not user["onboarded"]:
        await cb.answer("Сначала анкета: /start", show_alert=True)
        return
    await cb.answer()
    rows = [[(supplements()[sid]["name"].split(" (")[0], Sup(act="b1", id=sid))] for sid in buyable_for(user)]
    rows.append([("◀ Назад", Sup(act="main"))])
    await cb.message.edit_text(
        "<b>Что хочешь купить?</b> Показываю только то, что имеет смысл для тебя. Остальное из справочника "
        "(кофеин, предтренировочные, жиросжигатели) не советую.",
        reply_markup=inline(rows),
    )


@router.callback_query(Sup.filter(F.act == "b1"))
async def on_buy_tier(cb: CallbackQuery, callback_data: Sup):
    await cb.answer()
    sid = callback_data.id
    rows = [[(label, Sup(act="b2", id=f"{sid}|{tier}"))] for tier, label in TIERS.items()]
    rows.append([("Все три варианта", Sup(act="b2", id=f"{sid}|all"))])
    rows.append([("◀ Назад", Sup(act="buy"))])
    await cb.message.edit_text(
        f"<b>{html.escape(supplements()[sid]['name'])}</b>\nЧто важнее: цена, баланс или качество?",
        reply_markup=inline(rows),
    )


async def _send_recommendation(message: Message, user: dict, sid: str, tier: str) -> None:
    text = recommend(user, sid, None if tier == "all" else tier)
    for i in range(0, len(text), 3800):
        await message.answer(text[i:i + 3800])


@router.callback_query(Sup.filter(F.act == "b2"))
async def on_buy_result(cb: CallbackQuery, callback_data: Sup, state: FSMContext, db: Database):
    await cb.answer()
    sid, tier = callback_data.id.split("|")
    user = await db.get_user(cb.from_user.id)
    assert user
    if country_of(user) == "DEFAULT" and not user.get("country"):
        await state.set_state(SuppFlow.loc)
        await state.set_data({"sid": sid, "tier": tier})
        await cb.message.answer(
            "Напиши, где ты живёшь: город или страну (например «Алматы» или «Казахстан»), и часовой пояс, "
            "если города нет в списке (<i>+5</i>). Магазины и проверки подлинности зависят от страны.",
            reply_markup=_cancel_kb(),
        )
        return
    await _send_recommendation(cb.message, user, sid, tier)


@router.message(SuppFlow.loc, F.text, ~F.text.startswith("/"))
async def on_buy_location(message: Message, state: FSMContext, db: Database, reminders: Reminders):
    loc = parse_location(message.text)
    if not loc:
        await message.answer("Не нашёл. Напиши город или страну: «Алматы», «Россия», либо пояс «+3».")
        return
    data = await state.get_data()
    fields = {"country": loc.country, "city": loc.city}
    if loc.tz_known:  # назвали только страну: свой часовой пояс человека не трогаем
        fields["tz"] = loc.tz
    await db.update_user(message.from_user.id, **fields)
    user = await db.get_user(message.from_user.id)
    assert user
    reminders.schedule_user(user)
    await state.clear()
    await _send_recommendation(message, user, data["sid"], data["tier"])


@router.callback_query(Sup.filter(F.act == "took"))
async def on_took(cb: CallbackQuery):
    await cb.answer("Отлично 💪")
    await cb.message.edit_reply_markup(reply_markup=None)

