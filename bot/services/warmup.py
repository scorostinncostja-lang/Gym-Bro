"""Разминка до тренировки и растяжка после. Состав зависит от того, какие мышцы работают сегодня."""
import html

from bot.data import Exercise, exercises, get_exercise, warmup_data

_LOWER = {"squat", "hinge", "glute", "glute_hard", "lunge", "leg_curl", "leg_ext", "calves"}
_UPPER = {"push_h", "push_h_easy", "push_v", "pull_v", "pull_v_easy", "pull_h", "biceps", "biceps_alt",
          "triceps", "lateral", "rear_delt", "chest_fly"}
_BIG = {"squat", "hinge", "push_h", "push_v", "pull_v", "pull_h", "lunge"}


def day_type(plan: list[dict]) -> str:
    """lower / upper / full / cardio / flex по плану дня."""
    patterns = {e.pattern for i in plan if (e := get_exercise(i["ex_id"]))}
    if patterns and all(p.startswith("stretch_") for p in patterns):
        return "flex"
    if patterns == {"cardio"}:
        return "cardio"
    lower, upper = bool(patterns & _LOWER), bool(patterns & _UPPER)
    return "full" if lower and upper else "lower" if lower else "upper"


def warmup_text(plan: list[dict]) -> tuple[str, list[str]]:
    """Текст разминки и id GIF для кнопок."""
    data = warmup_data()
    kind = day_type(plan)
    blocks = [data["flex"] if kind == "flex" else data["cardio"] if kind == "cardio" else data["general"]]
    if kind in ("lower", "full"):
        blocks.append(data["lower"])
    if kind in ("upper", "full"):
        blocks.append(data["upper"])
    steps = [s for b in blocks for s in b]
    lines = [f"{n}. {html.escape(s['t'])}" for n, s in enumerate(steps, 1)]
    gifs = [s["gif"] for s in steps if s.get("gif")]
    minutes = {"flex": 4, "cardio": 6}.get(kind, 7)
    head = f"🔥 <b>Разминка, ~{minutes} мин</b>\nМышцы холодные: без неё выше риск травмы и слабее подходы.\n"
    tail = ""
    if kind in ("lower", "upper", "full"):
        tail = "\n\n" + html.escape(data["ramp"])
    return head + "\n".join(lines) + tail, gifs


def ramp_line(weight: float | None, fmt) -> str | None:
    """Разминочные подходы с весом для первого тяжёлого упражнения."""
    if not weight or weight < 10:
        return None
    return f"Разминочные подходы: 8 × {fmt(weight * 0.5)} кг, потом 4 × {fmt(weight * 0.7)} кг."


def cooldown(plan: list[dict], extended: bool = False) -> list[Exercise]:
    """Растяжки на мышцы, которые работали. Не больше 6 (8 при цели «гибкость»)."""
    data = warmup_data()["cooldown"]
    out: list[Exercise] = []
    for item in plan:
        ex = get_exercise(item["ex_id"])
        if not ex:
            continue
        for sid in data.get(ex.pattern, []):
            st = exercises().get(sid)
            if st and st not in out:
                out.append(st)
    return out[: 8 if extended else 6]
