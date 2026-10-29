"""Проверка добавки по этикетке: ИИ только читает фото, выводы и дозы считает код.

Бот не может проверить подлинность банки и не оценивает бренды «на глаз». Он смотрит на то, что видно:
знак независимой проверки, фирменные смеси без доз, стимуляторы, долю белка в порции, срок годности,
данные импортёра и сверяет с планом человека.
"""
import json
import re
from dataclasses import dataclass, field

from bot.data import shops_data, supplements
from bot.services.supp_plan import build_plan

_DANGEROUS_KINDS = {"preworkout", "fatburner", "testobooster"}
LEVEL_ICON = {"green": "🟢", "yellow": "🟡", "red": "🔴"}


@dataclass
class Verdict:
    level: str = "green"  # green | yellow | red
    headline: str = ""
    good: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    dose: str = ""
    supply: str = ""
    supp_id: str = "other"
    days_supply: float | None = None
    protein_per_day_g: float | None = None


def parse_label_json(raw: str) -> dict | None:
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _num(x) -> float | None:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and x >= 0 else None


def _brand_trust(brand: str | None, text: str) -> tuple[str, str | None]:
    """('program' | 'big' | 'unknown', описание)."""
    hay = f"{brand or ''} {text}".lower()
    known = shops_data()["brands_known"]
    for b in known["with_programs"]:
        if re.search(b["re"], hay):
            return "program", f"{b['name']}: {b['note']}"
    for b in known["big"]:
        if re.search(b["re"], hay):
            return "big", f"{b['name']}: известный производитель"
    return "unknown", None


def evaluate(label: dict, user: dict, price: float | None = None) -> Verdict:
    """Разбор этикетки под конкретного человека. label — JSON от модели."""
    sup = supplements()
    adult = int(user.get("age") or 0) >= 18
    kind = label.get("kind") if label.get("kind") in sup or label.get("kind") in _DANGEROUS_KINDS else "other"
    v = Verdict(supp_id=kind)
    per = label.get("per_serving") or {}
    serving = _num(label.get("serving_g"))
    servings = _num(label.get("servings"))
    net = _num(label.get("net_g"))
    brand = label.get("brand")
    certs = [c for c in (label.get("certs") or []) if isinstance(c, str)]
    stimulants = [x for x in (label.get("stimulants") or []) if isinstance(x, str)]

    def red(msg: str):
        v.level = "red"
        v.flags.append(msg)

    def yellow(msg: str):
        if v.level == "green":
            v.level = "yellow"
        v.flags.append(msg)

    if not label.get("readable", True):
        v.level = "yellow"
        v.headline = "Не смог прочитать этикетку"
        v.flags.append("Фото нечёткое. Пришли снимок состава при хорошем свете: чтобы были видны порция и числа.")
        return v

    # --- безопасность важнее всего
    if kind in _DANGEROUS_KINDS:
        red("Такие добавки не принимай: стимуляторы и смеси без дозировок вредят сердцу, сну и гормонам"
            + (" (подросткам особенно)." if not adult else "."))
    if not adult and kind in sup and not sup[kind]["teen"]["ok"] and kind not in _DANGEROUS_KINDS:
        red(f"Тебе до 18 это не подходит: {sup[kind]['teen']['note']}")
    if stimulants and not adult:
        red("В составе стимуляторы (" + ", ".join(stimulants[:3]) + "). Подросткам нельзя.")
    elif stimulants:
        yellow("В составе стимуляторы (" + ", ".join(stimulants[:3]) + "): проверь общую дозу кофеина за день.")
    if label.get("proprietary_blend"):
        (red if stimulants else yellow)("Фирменная смесь: дозы компонентов не указаны, оценить нельзя.")

    # --- реклама и маркировка
    claims = [c for c in (label.get("claims") or []) if isinstance(c, str)]
    if claims:
        yellow("Громкие обещания на упаковке («" + claims[0][:60] + "»): честные добавки так не пишут.")
    if label.get("expiry_visible") is False:
        yellow("Срок годности не виден. Проверь его на банке до покупки или приёма.")
    if label.get("importer_visible") is False:
        yellow("Не вижу данных импортёра или производителя. На легальной банке они есть.")

    # --- бренд и сертификаты
    trust, note = _brand_trust(brand, str(label.get("product") or ""))
    if certs:
        v.good.append("Есть знак независимой проверки: " + ", ".join(certs[:3])
                      + ". Проверь серию (lot) на сайте сертификатора, значок можно подделать.")
    if trust == "program":
        v.good.append(note or "")
        if not certs:
            yellow("На этой банке знака независимой проверки не вижу: у бренда он есть не на всех продуктах.")
    elif trust == "big":
        v.good.append(note or "")
        if not certs:
            yellow("Независимую проверку партии подтвердить не могу.")
    elif not certs:
        yellow(
            "Этого бренда нет в моём списке известных, знака независимой проверки не вижу. Это не значит, что "
            "продукт плохой, но проверить качество я не могу. Бери только с полным составом и от проверенного продавца."
        )

    # --- расчёт под человека
    plan_line = next((ln for ln in build_plan(user, [kind]).lines if ln.sid == kind), None) if kind in sup else None
    dose_text = ""
    daily_servings: float | None = None

    if kind == "creatine":
        cr = _num(per.get("creatine_g"))
        target = float(re.search(r"[\d.,]+", plan_line.dose).group(0).replace(",", ".")) if plan_line and plan_line.dose else 3.0
        if cr and cr > 0:
            daily_servings = target / cr
            dose_text = f"Твоя доза {target:g} г = {daily_servings:.2g} порции ({cr:g} г креатина в порции)."
            if serving and cr / serving < 0.8:
                yellow("Креатина в порции меньше 80% от её веса: в банке много добавок. Бери чистый моногидрат.")
        else:
            yellow("Не вижу, сколько креатина в порции: посчитать твою дозу нельзя.")
    elif kind == "whey":
        pr = _num(per.get("protein_g"))
        if pr and serving:
            share = pr / serving
            v.good.append(f"Белка в порции {pr:g} г из {serving:g} г ({share:.0%}).")
            if share < 0.55:
                yellow("Белка меньше 55% веса порции: это скорее гейнер или смесь, а не протеин.")
            elif share < 0.7:
                yellow("Белка 55–70% веса порции: много углеводов и добавок, для протеина мало.")
            daily_servings = 1.0 if (not adult or (plan_line and plan_line.dose.startswith("1 ")) ) else 2.0
            dose_text = f"По твоему плану {daily_servings:g} порц. в день (≈{daily_servings * pr:g} г белка)."
            v.protein_per_day_g = daily_servings * pr
        sugar = _num(per.get("sugar_g"))
        if sugar and serving and sugar / serving > 0.1:
            yellow(f"Сахара {sugar:g} г в порции: для протеина многовато.")
    elif kind == "omega3":
        epa = _num(per.get("epa_dha_mg"))
        if epa and epa > 0:
            target = 500.0 if not adult else 1000.0
            caps = max(1, round(target / epa))
            daily_servings = float(caps)
            dose_text = f"Твоя норма ≈ {target:g} мг EPA+DHA = {caps} порц. в день (в порции {epa:g} мг)."
        else:
            yellow("Не вижу EPA+DHA в порции: «1000 мг рыбьего жира» значит только 300 мг полезных кислот.")
    elif kind == "vitd":
        iu = _num(per.get("vitd_iu"))
        if iu and iu > 0:
            if not adult and iu > 4000:
                red(f"В порции {iu:g} МЕ: для подростка это слишком много. Не принимай без врача.")
            else:
                target = 1000.0
                daily_servings = max(0.5, round(target / iu * 2) / 2)
                dose_text = f"Твоя доза ≈ {target:g} МЕ = {daily_servings:g} порц. в день (в порции {iu:g} МЕ)."
        else:
            yellow("Не вижу дозу витамина D в МЕ или мкг.")

    v.dose = dose_text
    if daily_servings and servings:
        v.days_supply = servings / daily_servings
        v.supply = f"Хватит примерно на {v.days_supply:.0f} дн."
    elif daily_servings and net and serving:
        v.days_supply = net / serving / daily_servings
        v.supply = f"Хватит примерно на {v.days_supply:.0f} дн."

    names = {"green": "Нормально", "yellow": "Есть вопросы", "red": "Не рекомендую"}
    v.headline = f"{names[v.level]}: {label.get('product') or label.get('brand') or sup.get(kind, {}).get('name', 'добавка')}"
    return v


def render(v: Verdict, price: float | None = None) -> str:
    import html
    lines = [f"{LEVEL_ICON[v.level]} <b>{html.escape(v.headline)}</b>"]
    if v.good:
        lines += ["", "<b>Хорошо</b>"] + [f"• {html.escape(g)}" for g in v.good if g]
    if v.flags:
        lines += ["", "<b>Вопросы</b>"] + [f"• {html.escape(f)}" for f in v.flags]
    if v.dose:
        lines += ["", "<b>Тебе</b>", html.escape(v.dose) + (" " + html.escape(v.supply) if v.supply else "")]
    if price and v.days_supply:
        lines.append(f"Цена: {price:,.0f} за упаковку, ≈ {price / v.days_supply:,.0f} в день.".replace(",", " "))
        if v.protein_per_day_g:
            per100 = price / (v.days_supply * v.protein_per_day_g) * 100
            lines.append(f"За 100 г чистого белка: ≈ {per100:,.0f}. Сравни с другими банками по этому числу.".replace(",", " "))
    lines.append("\n<i>Я читаю только то, что видно на фото. Подлинность банки и качество партии проверить не могу: "
                 "сверяй серию на сайте сертификатора и покупай у проверенного продавца.</i>")
    return "\n".join(lines)


def supply_days(servings: float | None, daily: float | None) -> float | None:
    return servings / daily if servings and daily else None

