"""Недельный отчёт: цифры считает код, ИИ добавляет короткий разбор и фокус на следующую неделю."""
import html
import logging
from datetime import date, timedelta

from bot.data import exercises
from bot.db.database import Database
from bot.prompts import WEEK_PROMPT, system_prompt
from bot.services.goals import goals_label, user_goals
from bot.services.timeutil import local_today, parse_days
from bot.textfmt import format_llm

log = logging.getLogger(__name__)


async def weekly_facts(db: Database, user: dict, today: date | None = None) -> dict:
    tg = user["tg_id"]
    today = today or local_today(user["tz"])
    monday = today - timedelta(days=today.weekday())
    prev_monday = monday - timedelta(days=7)
    sets = await db.sets_between(tg, monday.isoformat(), today.isoformat())
    prev = await db.sets_between(tg, prev_monday.isoformat(), (monday - timedelta(days=1)).isoformat())
    catalog = exercises()

    def volume(rows):
        return sum(r["weight"] * r["reps"] for r in rows if (e := catalog.get(r["ex_id"])) and e.kind == "weight")

    def cardio_min(rows):
        return sum(r["reps"] for r in rows if (e := catalog.get(r["ex_id"])) and e.kind == "cardio")

    records = []
    for ex_id in dict.fromkeys(r["ex_id"] for r in sets):
        ex = catalog.get(ex_id)
        if not ex or ex.kind != "weight":
            continue
        best_now = max(r["weight"] for r in sets if r["ex_id"] == ex_id)
        before = await db.best_before(tg, ex_id, monday.isoformat())
        if before and best_now > before:
            records.append(f"{ex.name}: {best_now:g} кг (было {before:g})")
    planned = int(user.get("days_per_week") or 0) + len(parse_days(user.get("cardio_days")))
    return {
        "done": await db.done_between(tg, monday.isoformat(), today.isoformat()),
        "planned": planned,
        "sets": len(sets),
        "volume": volume(sets),
        "volume_prev": volume(prev),
        "cardio": cardio_min(sets),
        "records": records,
        "streak": await db.streak(tg),
        "hard": sum(1 for r in sets if r.get("feel") == 4),
    }


def render_week(f: dict) -> str:
    lines = ["📈 <b>Итоги недели</b>", f"Тренировок: {f['done']} из {f['planned']}", f"Подходов: {f['sets']}"]
    if f["volume"]:
        delta = ""
        if f["volume_prev"]:
            pct = (f["volume"] - f["volume_prev"]) / f["volume_prev"] * 100
            delta = f" ({'+' if pct >= 0 else ''}{pct:.0f}% к прошлой неделе)"
        lines.append(f"Объём: {f['volume']:.0f} кг{delta}")
    if f["cardio"]:
        lines.append(f"Кардио: {f['cardio']} мин")
    lines.append(f"Серия: {f['streak']} подряд")
    if f["records"]:
        lines.append("\n🏆 <b>Рекорды</b>\n" + "\n".join(f"• {html.escape(r)}" for r in f["records"][:6]))
    if f["done"] == 0:
        lines.append("\nНа этой неделе тренировок не было. Не страшно: начни с одной короткой, /today.")
    return "\n".join(lines)


async def weekly_text(db: Database, user: dict, llm=None, settings=None) -> str:
    """Отчёт целиком: цифры и, если ИИ доступен и квота есть, разбор тренера."""
    f = await weekly_facts(db, user)
    text = render_week(f)
    if llm is None or not llm.available or f["sets"] == 0:
        return text
    limit = getattr(settings, "limit_llm_per_day", 40)
    if not await db.take_quota(user["tg_id"], "llm", limit):
        return text
    facts = (
        f"Цели: {goals_label(user_goals(user))}. Тренировок {f['done']} из {f['planned']}, подходов {f['sets']}, "
        f"объём {f['volume']:.0f} кг (прошлая неделя {f['volume_prev']:.0f}), кардио {f['cardio']} мин, "
        f"подходов на пределе {f['hard']}, рекорды: {', '.join(f['records']) or 'нет'}."
    )
    try:
        from bot.services.profile import build_profile

        comment = await llm.chat(system_prompt(await build_profile(db, user), WEEK_PROMPT), [], facts, max_tokens=300)
    except Exception as e:  # noqa: BLE001 — отчёт без комментария лучше, чем никакого
        log.warning("Недельный отчёт без ИИ: %r", e)
        return text
    return text + "\n\n🧠 <b>Тренер</b>\n" + format_llm(comment)
