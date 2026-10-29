"""Что и где купить: бренды-ориентиры и маркетплейсы по стране. Данные в data/shops.json, составлены вручную."""
import html

from bot.data import shops_data, supplements
from bot.services.location import COUNTRY_NAME, country_of
from bot.services.supp_plan import build_plan

TIERS = {"budget": "💸 Бюджетно", "fit": "⚖️ Подходящее", "best": "⭐ Лучшее по качеству"}
BUYABLE = ["creatine", "whey", "omega3", "vitd", "multivit"]


def buyable_for(user: dict) -> list[str]:
    """Что подростку разрешено искать: только безопасное. Кофеин и прочее не предлагаем."""
    sup = supplements()
    adult = int(user.get("age") or 0) >= 18
    return [s for s in BUYABLE if s in sup and (adult or sup[s]["teen"]["ok"])]


def recommend(user: dict, supp_id: str, tier: str | None = None) -> str:
    data = shops_data()
    country = country_of(user)
    region = data["countries"].get(country) or data["countries"]["DEFAULT"]
    brands = data["brands"][supp_id]
    sup = supplements()[supp_id]
    line = next((ln for ln in build_plan(user, [supp_id]).lines), None)
    tiers = [tier] if tier else ["budget", "fit", "best"]

    out = [f"🛒 <b>{html.escape(sup['name'])}</b> · {html.escape(region['name'])}"]
    if line and line.dose:
        out.append(f"Тебе нужно: {html.escape(line.dose)}.")
    out.append(f"\n<b>Что искать</b>\n{html.escape(brands['what'])}")
    for t in tiers:
        items = brands.get(t, [])
        markets = [m for m in region["markets"] if m["tier"] == t]
        out.append(f"\n<b>{TIERS[t]}</b>")
        out += [f"• {html.escape(b)}" for b in items]
        if markets:
            out.append("Где заказать:")
            out += [f"→ {html.escape(m['name'])}. {html.escape(m['why'])}" for m in markets]
    out.append("\n<b>Как не нарваться на подделку</b>")
    out += [f"• {html.escape(c)}" for c in region["checks"]]
    out.append(f"\n{html.escape(data['price_hint'])}")
    certs = ", ".join(f"{k}: {v}" for k, v in data["certs"].items())
    out.append(f"\n<i>Проверка сертификатов: {html.escape(certs)}. Справочник составлен вручную в {data['checked']}: "
              "наличие и цены в твоём городе смотри сам. Я не получаю комиссию с магазинов.</i>")
    return "\n".join(out)


def where_label(user: dict) -> str:
    c = country_of(user)
    return COUNTRY_NAME.get(c, "твоя страна")
