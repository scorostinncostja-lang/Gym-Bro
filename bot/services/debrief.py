"""Итог тренировки: факты считает код, ИИ только пересказывает их по-человечески."""
from bot.data import get_exercise
from bot.db.database import Database
from bot.services.progression import Feel

_FEEL_WORD = {Feel.EASY: "легко", Feel.OK: "нормально", Feel.HARD: "тяжело", Feel.MAX: "на пределе"}


def _fmt_w(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


def _fmt_sets(sets: list[dict], kind: str = "weight") -> str:
    out = []
    for s in sets:
        feel = _FEEL_WORD.get(Feel(s["feel"]), "") if s.get("feel") else ""
        if kind == "cardio":
            base = f"{s['reps']} мин" + (f", {_fmt_w(s['weight'])} км" if s["weight"] else "")
        elif kind == "time":
            base = f"{s['reps']} сек"
        else:
            base = f"{_fmt_w(s['weight'])}×{s['reps']}" if s["weight"] else f"{s['reps']}"
        out.append(f"{base} ({feel})" if feel else base)
    return ", ".join(out)


async def workout_facts(db: Database, w: dict) -> tuple[str, dict]:
    """Текст фактов для ИИ и простая статистика для шаблонного итога."""
    sets = await db.workout_sets(w["id"])
    by_ex: dict[str, list[dict]] = {}
    for s in sets:
        by_ex.setdefault(s["ex_id"], []).append(s)
    lines = []
    stats = {"easy": 0, "max": 0, "total": len(sets), "progress": 0, "exercises": len(by_ex)}
    for ex_id, ss in by_ex.items():
        ex = get_exercise(ex_id)
        prev = await db.last_sets(w["tg_id"], ex_id, exclude_workout=w["id"])
        kind = ex.kind if ex else "weight"
        lines.append(f"- {ex.name if ex else ex_id}: сейчас {_fmt_sets(ss, kind)}; раньше {_fmt_sets(prev, kind) or 'впервые'}")
        stats["easy"] += sum(1 for s in ss if s.get("feel") == Feel.EASY)
        stats["max"] += sum(1 for s in ss if s.get("feel") == Feel.MAX)
        if kind == "weight" and prev and max(s["weight"] for s in ss) > max(s["weight"] for s in prev):
            stats["progress"] += 1
    return "\n".join(lines), stats


def template_debrief(stats: dict) -> str:
    """Итог без ИИ: когда ключей нет или лимит исчерпан."""
    parts = []
    if stats["progress"]:
        parts.append(f"Вес вырос в упражнениях: {stats['progress']}. Это и есть прогресс.")
    if stats["max"] >= 2:
        parts.append("Несколько подходов было на пределе. В следующий раз не гонись за весом: техника и 1–2 повтора в запасе.")
    elif stats["total"] and stats["easy"] >= stats["total"] / 2:
        parts.append("Большинство подходов было лёгким. В следующий раз вес вырастет сам.")
    else:
        parts.append("Нагрузка в рабочей зоне. Держи тот же план.")
    parts.append("Сегодня: белок в каждом приёме пищи и сон 8+ часов, мышцы растут в отдых.")
    return " ".join(parts)
