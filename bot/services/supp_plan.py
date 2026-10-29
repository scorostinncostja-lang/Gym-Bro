"""Персональный план приёма добавок: считает код по весу, возрасту и цели, ИИ только комментирует.

Для каждой добавки из набора пользователя: принимать (сколько, когда) или «не нужно» / «не принимай».
Подросткам доступны только позиции с teen.ok и малые дозы из справочника.
"""
import html
from dataclasses import dataclass

from bot.data import supplements
from bot.services.supp_advice import teen_ok

ADULT_AGE = 18
from bot.services.goals import goal_name, protein_per_kg

STATUS_TITLE = {
    "take": "✅ <b>Принимай</b>",
    "maybe": "🟡 <b>По условию</b>",
    "skip": "❌ <b>Не нужно</b>",
    "avoid": "☠️ <b>Не принимай</b>",
}


@dataclass
class PlanLine:
    sid: str
    name: str
    status: str  # take | maybe | skip | avoid
    dose: str = ""
    when: str = ""
    note: str = ""


@dataclass
class Plan:
    lines: list[PlanLine]
    weight: float
    age: int
    goal: str
    protein: tuple[int, int]  # диапазон г белка в день
    train_time: str | None
    adult: bool


def _shift(hhmm: str | None, minutes: int) -> str | None:
    if not hhmm:
        return None
    h, m = map(int, hhmm.split(":"))
    total = (h * 60 + m + minutes) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def _first_sentence(text: str) -> str:
    return text.split(". ")[0].rstrip(".") + "."


def _protein_range(weight: float, goal: str, adult: bool) -> tuple[int, int]:
    gpk = protein_per_kg(goal) if adult else min(protein_per_kg(goal), 1.6)  # подросткам без верхних значений
    return round(weight * (gpk - 0.2) / 5) * 5, round(weight * (gpk + 0.2) / 5) * 5


def _line(s: dict, adult: bool, weight: float, goal: str) -> PlanLine:
    sid, name, verdict = s["id"], s["name"].split(" (")[0], s["verdict"]
    t = s["teen"]

    if verdict == "dangerous":
        return PlanLine(sid, name, "avoid", note=("Нет. " if adult else "Подросткам категорически нет. ") + _first_sentence(s["summary"]))
    if verdict == "useless":
        note = _first_sentence(s["summary"])
        status = "avoid" if (not adult and "категорически" in t["note"]) else "skip"
        return PlanLine(sid, name, status, note=("Не нужно. " if status == "skip" else "Нет. ") + note)
    if not adult and not teen_ok(s):
        return PlanLine(sid, name, "skip", note=t["note"])

    if sid == "creatine":
        if adult:
            grams = min(5.0, max(3.0, round(weight * 0.05 * 2) / 2))
            dose = f"{grams:g} г в день"
        else:
            dose = "3 г в день (мерная ложка из банки)"
        weak = goal in ("endurance", "flex")
        return PlanLine(
            sid, name, "maybe" if weak else "take", dose + ", без «загрузки»",
            "каждый день, и в дни отдыха тоже; с любой едой, можно после тренировки",
            ("Для твоей цели даёт мало: креатин помогает силе и объёму, а не выносливости. " if weak else "")
            + "Эффект копится 3–4 недели. Пей воду в течение дня. Нельзя при болезнях почек.",
        )
    if sid == "whey":
        low, high = _protein_range(weight, goal, adult)
        servings = 1 if (not adult or high <= 110) else 2
        dose = f"{servings} порц. по 25–30 г порошка (≈20–25 г белка каждая)"
        return PlanLine(
            sid, name, "take", dose,
            "в день тренировки через 1–2 часа после неё; в день отдыха только если белка из еды не хватает",
            f"Весь белок за день: {low}–{high} г. Мясо, рыба, яйца, творог и молоко закрывают большую часть, "
            f"порошок добирает недостающее. Если с едой выходит {low}+ г, порошок не нужен.",
        )
    if sid == "omega3":
        dose = t["dose"].split(". ")[0] if not adult else s["dose"].split(". ")[0]
        return PlanLine(sid, name, "maybe", dose.rstrip("."), "с самым плотным приёмом пищи",
                        "Нужна, только если жирную рыбу ешь реже 2 раз в неделю.")
    if sid == "vitd":
        dose = t["dose"].split(". ")[0] if not adult else "1000–2000 МЕ в день (точную дозу лучше по анализу)"
        return PlanLine(sid, name, "maybe", dose.rstrip("."), "утром или днём вместе с едой, где есть жиры",
                        "Нужен осенью и зимой при мало солнца. Лучше сдать анализ на 25-OH витамин D.")
    if sid == "multivit":
        return PlanLine(sid, name, "maybe", (t["dose"] if not adult else s["dose"]).rstrip("."), "с завтраком",
                        "Нужны, только если питание однообразное. Мышцы они не растят.")
    if sid == "caffeine":  # сюда доходят только взрослые: подросткам teen.ok = False
        mg = min(200, round(weight * 3 / 10) * 10)
        return PlanLine(sid, name, "maybe", f"{mg} мг (≈3 мг на кг; максимум 400 мг в сутки)",
                        "за 30–60 минут до тренировки, только в дни тренировок, не после 16:00",
                        "Начни с половины дозы. Если страдает сон, откажись.")
    # прочее условное для взрослых: доза из справочника
    return PlanLine(sid, name, "maybe", s["dose"].rstrip("."), "", _first_sentence(s["who"]))


def build_plan(user: dict, owned: list[str]) -> Plan:
    sup = supplements()
    weight = float(user.get("weight_kg") or 60)
    age = int(user.get("age") or 0)
    adult = age >= ADULT_AGE
    goal = user.get("goal") or "fit"
    lines = [_line(sup[sid], adult, weight, goal) for sid in owned if sid in sup]
    order = {"take": 0, "maybe": 1, "skip": 2, "avoid": 3}
    lines.sort(key=lambda ln: order[ln.status])
    return Plan(lines, weight, age, goal, _protein_range(weight, goal, adult), user.get("train_time"), adult)


def _timeline(plan: Plan) -> list[str]:
    take = [ln for ln in plan.lines if ln.status in ("take", "maybe")]
    ids = {ln.sid for ln in take}
    out = []
    morning = [ln.name for ln in take if ln.sid in ("multivit", "vitd", "omega3")]
    if morning:
        out.append(f"Утром с едой: {', '.join(morning)}.")
    if "caffeine" in ids and plan.train_time:
        out.append(f"За 30–60 мин до тренировки (около {_shift(plan.train_time, -45)}): кофеин.")
    after = [ln.name for ln in take if ln.sid in ("whey",)]
    if after and plan.train_time:
        out.append(f"После тренировки (около {_shift(plan.train_time, 75)}): {', '.join(after)}.")
    if "creatine" in ids:
        out.append("Креатин: каждый день в одно и то же время, удобно вместе с едой.")
    return out


def render_plan(plan: Plan) -> str:
    head = (
        f"🧮 <b>План приёма добавок</b>\n"
        f"Рассчитано на {plan.weight:g} кг, {plan.age} лет, цель: {goal_name(plan.goal)}."
    )
    if not plan.lines:
        return head + "\n\nНабор пуст. Отметь, что у тебя есть: /supplements"
    blocks = [head]
    for status in ("take", "maybe", "skip", "avoid"):
        items = [ln for ln in plan.lines if ln.status == status]
        if not items:
            continue
        rows = [STATUS_TITLE[status]]
        for ln in items:
            parts = [f"• <b>{html.escape(ln.name)}</b>"]
            if ln.dose:
                parts.append(f" — {html.escape(ln.dose)}")
            text = "".join(parts)
            if ln.when:
                text += f"\n  Когда: {html.escape(ln.when)}."
            if ln.note:
                text += f"\n  <i>{html.escape(ln.note)}</i>"
            rows.append(text)
        blocks.append("\n".join(rows))
    timeline = _timeline(plan)
    if timeline:
        blocks.append("🕒 <b>Расписание дня</b>\n" + "\n".join(html.escape(x) for x in timeline))
    foot = "Я не врач, это справка по твоему весу, а не назначение."
    if not plan.adult:
        foot = "Скажи родителям. " + foot + " Основа роста: еда, сон, тренировки."
    blocks.append(f"<i>{foot}</i>")
    return "\n\n".join(blocks)


def plan_facts(plan: Plan) -> str:
    """Для ИИ: готовый план в виде текста, числа менять нельзя."""
    rows = [f"Вес {plan.weight:g} кг, возраст {plan.age}, цель {goal_name(plan.goal)}."]
    for ln in plan.lines:
        rows.append(f"- {ln.name} [{ln.status}] {ln.dose} {ln.when} {ln.note}".strip())
    return "\n".join(rows)


def slot_lines(plan: Plan, slot: str, trains_today: bool) -> list[str]:
    """Что принять в этот момент: утро с едой, перед тренировкой, после неё."""
    out = []
    for ln in plan.lines:
        if ln.status not in ("take", "maybe"):
            continue
        short = f"{ln.name}: {ln.dose}"
        if slot == "am" and (ln.sid in ("multivit", "vitd", "omega3") or (ln.sid == "creatine" and not trains_today)):
            out.append(short)
        elif slot == "pre" and ln.sid == "caffeine":
            out.append(short)
        elif slot == "post" and ln.sid in ("whey", "creatine"):
            out.append(short)
    return out
