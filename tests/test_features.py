"""Инвентарь, цели, кардио, разминка, проверка этикеток, магазины."""
import json

import pytest

from bot.data import exercises
from bot.services import cardio, equipment, goals
from bot.services.location import country_of, parse_location
from bot.services.program_builder import build_day, build_extra_day, template_for
from bot.services.progression import CAP_NOTE, Feel, SetResult, next_target, set_hint
from bot.services.set_parser import parse_set
from bot.services.supp_label import evaluate, parse_label_json, render
from bot.services.supp_shop import buyable_for, recommend
from bot.services.timeutil import pick_extra_days
from bot.services.warmup import cooldown, day_type, ramp_line, warmup_text


def home_user(gear_text: str, **kw) -> dict:
    items, other = equipment.parse_gear_text(gear_text)
    u = {"place": "home_db", "level": "new", "goal": "muscle", "age": 15, "weight_kg": 62,
         "equipment": equipment.dump_gear({"items": items, "other": other})}
    u.update(kw)
    return u


# ---------- инвентарь ----------
def test_gear_text_from_user_example():
    items, other = equipment.parse_gear_text(
        "у меня одна гантель 5 кг, турник, резинка с давлением 25 кг, эспандер 25 кг")
    by = {i["id"]: i for i in items}
    assert by["dumbbell"]["count"] == 1 and by["dumbbell"]["weights"] == [5.0]
    assert "pullup_bar" in by and by["band"]["weights"] == [25.0] and by["expander"]["weights"] == [25.0]
    assert other == []


def test_gear_adjustable_and_unknown_items():
    items, other = equipment.parse_gear_text("разборные гантели до 20 кг, гиря 16 кг и скакалка, массажный пистолет")
    by = {i["id"]: i for i in items}
    assert by["dumbbell"]["adjustable"] and by["dumbbell"]["max"] == 20
    assert by["kettlebell"]["weights"] == [16.0] and "rope" in by
    assert other == ["массажный пистолет"]


def test_gear_tags_and_ladder_cap_the_weights():
    u = home_user("одна гантель 5 кг, турник, резинка 25 кг")
    t = equipment.tags(u, "home_db")
    assert {"dumbbell", "pullup_bar", "band", "bodyweight"} <= t and "machine" not in t
    assert equipment.ladder(u, "home_db", "dumbbell") == [5.0]
    assert equipment.snap(12.0, [5.0]) == 5.0
    assert equipment.single_dumbbell(u, "home_db")
    assert equipment.ladder(u, "gym", "dumbbell") is None  # в зале ограничений нет


def test_program_uses_only_what_user_has():
    u = home_user("одна гантель 5 кг, турник, резинка 25 кг")
    allowed = equipment.tags(u, "home_db")
    seen = set()
    for i in range(4):
        for item in build_day(template_for(3, i), u):
            ex = exercises()[item.ex_id]
            seen.add(ex.equip)
            assert ex.equip in allowed or ex.pattern == "cardio" and ex.equip in allowed
    assert {"pullup_bar", "band"} & seen  # турник и резинки реально попали в программу


def test_beginner_does_not_get_pullups_but_experienced_does():
    items = {"home": "турник"}
    new = home_user("турник", level="new")
    some = home_user("турник", level="some")
    ids_new = {i.ex_id for k in range(4) for i in build_day(template_for(3, k), new)}
    ids_some = {i.ex_id for k in range(4) for i in build_day(template_for(3, k), some)}
    assert "pullup" not in ids_new and ("pullup" in ids_some or "chinup" in ids_some)
    assert items


def test_progression_stops_at_heaviest_available_weight():
    ladder = [5.0]
    hint = set_hint(SetResult(5, 12, Feel.EASY), 8, 12, "weight", 1, ladder=ladder)
    assert hint.next_weight == 5 and CAP_NOTE in hint.text and hint.next_reps > 12
    t = next_target([SetResult(5, 12, Feel.OK)] * 3, 8, 12, "weight", 1, ladder=ladder)
    assert t.weight == 5 and t.reps > 12


def test_ladder_steps_between_available_dumbbells():
    t = next_target([SetResult(8, 12, Feel.OK)] * 3, 8, 12, "weight", 1, ladder=[5.0, 8.0, 12.0])
    assert t.weight == 12.0


# ---------- цели ----------
def test_goal_text_parsing():
    assert goals.parse_goals("хочу рельеф и выносливость") == ["definition", "endurance"]
    assert goals.parse_goals("гибкость, шпагат")[0] == "flex"
    assert goals.parse_goals("абракадабра") == []
    assert "lose" not in goals.goal_keys(15) and "lose" in goals.goal_keys(25)


def test_goal_changes_rep_ranges_and_adds_cardio_finisher():
    base = {"place": "gym", "level": "new", "age": 25}
    strength = build_day(template_for(3, 0), {**base, "goal": "strength"})
    endurance = build_day(template_for(3, 0), {**base, "goal": "endurance"})
    muscle = build_day(template_for(3, 0), {**base, "goal": "muscle"})
    weight_sets = lambda plan: [(p.lo, p.hi) for p in plan if p.kind == "weight"]  # noqa: E731
    assert weight_sets(strength)[0] == (5, 8) and weight_sets(endurance)[0] == (15, 20) and weight_sets(muscle)[0] == (8, 12)
    assert endurance[-1].kind == "cardio" and endurance[-1].lo == 15
    assert not any(p.kind == "cardio" for p in muscle)


def test_teen_strength_never_goes_below_six_reps():
    plan = build_day(template_for(3, 0), {"place": "gym", "level": "new", "age": 15, "goal": "strength"})
    assert all(p.lo >= 6 for p in plan if p.kind == "weight")


# ---------- кардио ----------
def test_cardio_modes_by_place_and_equipment():
    gym = [e.id for e in cardio.available({"place": "gym", "level": "new"}, "gym")]
    assert gym[:2] == ["treadmill", "bike_stationary"]
    home = home_user("велосипед, скакалка, беговая дорожка")
    ids = [e.id for e in cardio.available(home, "home_db")]
    assert {"bike_outdoor", "jump_rope", "treadmill", "run_outdoor"} <= set(ids)
    bare = [e.id for e in cardio.available({"place": "home_bw", "level": "new"}, "home_bw")]
    assert set(bare) == {"run_outdoor", "walk_fast"}  # бег и ходьба всегда доступны


def test_run_walk_protocol_and_heart_zone():
    assert "бег 1 мин" in cardio.run_walk_text(0) and "непрерывно" in cardio.run_walk_text(99)
    assert cardio.hr_zone(15) == (123, 144)


def test_cardio_log_parses_minutes_and_distance():
    p = parse_set("25 мин 4 км, нормально", "cardio")
    assert p and p.reps == 25 and p.weight == 4 and p.feel == Feel.OK
    assert parse_set("40", "cardio").reps == 40 and parse_set("40", "cardio").weight == 0
    assert parse_set("без цифр", "cardio") is None


def test_cardio_progression_adds_minutes_then_backs_off():
    up = next_target([SetResult(0, 20, Feel.EASY)], 15, 40, "cardio", 1)
    assert up.reps == 23
    down = next_target([SetResult(0, 30, Feel.MAX)], 15, 40, "cardio", 1)
    assert down.reps == 28


def test_extra_day_is_flex_or_cardio_by_goal():
    name, items = build_extra_day({"place": "gym", "level": "new", "goal": "flex", "age": 20})
    assert name == "Гибкость и мобильность" and all(exercises()[i.ex_id].pattern.startswith("stretch_") for i in items)
    name, items = build_extra_day({"place": "gym", "level": "new", "goal": "endurance", "age": 20})
    assert name == "Кардио" and items[0].kind == "cardio"


def test_extra_days_are_spread_away_from_strength_days():
    two = pick_extra_days([0, 2, 4], 2)
    assert len(two) == 2 and not set(two) & {0, 2, 4} and two == sorted(two)
    days = pick_extra_days([0, 2, 4], 3)
    assert len(days) == 3 and not set(days) & {0, 2, 4}
    assert pick_extra_days([0, 1, 2, 3, 4, 5, 6], 2) == []


# ---------- разминка и растяжка ----------
def test_warmup_depends_on_day_type():
    lower = [{"ex_id": "leg_press"}, {"ex_id": "leg_curl"}]
    upper = [{"ex_id": "machine_chest_press"}, {"ex_id": "lat_pulldown"}]
    assert day_type(lower) == "lower" and day_type(upper) == "upper"
    assert day_type(lower + upper) == "full" and day_type([{"ex_id": "treadmill"}]) == "cardio"
    t_low, gifs = warmup_text(lower)
    assert "Приседания без веса" in t_low and "Разминочные подходы" in t_low and gifs
    t_up, _ = warmup_text(upper)
    assert "Лопаточные" in t_up and "Приседания без веса" not in t_up
    assert "Разминочные подходы" not in warmup_text([{"ex_id": "treadmill"}])[0]


def test_cooldown_matches_worked_muscles_and_is_limited():
    plan = [{"ex_id": "leg_press"}, {"ex_id": "machine_chest_press"}, {"ex_id": "lat_pulldown"},
            {"ex_id": "leg_curl"}, {"ex_id": "triceps_pushdown"}, {"ex_id": "cable_crunch"}]
    names = [s.id for s in cooldown(plan)]
    assert "st_quad" in names and "st_chest" in names and len(names) <= 6
    assert len(cooldown(plan, extended=True)) <= 8


def test_ramp_line_only_for_heavy_enough_weight():
    f = lambda x: f"{x:g}"  # noqa: E731
    assert ramp_line(40, f) == "Разминочные подходы: 8 × 20 кг, потом 4 × 28 кг."
    assert ramp_line(6, f) is None


# ---------- город ----------
def test_location_variants():
    assert parse_location("Алматы").country == "KZ"
    assert parse_location("+5 Казахстан").country == "KZ"
    assert parse_location("Москва").tz == "Europe/Moscow"
    assert country_of({"tz": "Asia/Almaty"}) == "KZ" and country_of({}) == "DEFAULT"


# ---------- этикетка ----------
LABEL = {
    "readable": True, "kind": "creatine", "brand": "Optimum Nutrition", "product": "Micronized Creatine",
    "serving_g": 5, "servings": 120, "net_g": 600, "per_serving": {"creatine_g": 5},
    "proprietary_blend": False, "stimulants": [], "certs": ["Informed Sport"],
    "expiry_visible": True, "importer_visible": True, "claims": [],
}
TEEN = {"age": 15, "weight_kg": 62, "goal": "muscle", "train_time": "18:00"}


def test_good_creatine_label_for_teen_gets_green_and_dose_math():
    v = evaluate(LABEL, TEEN)
    assert v.level == "green" and "Informed Sport" in " ".join(v.good)
    assert "3 г" in v.dose and "0.6" in v.dose and v.days_supply == pytest.approx(200)
    assert "200" in render(v, 12000)


def test_preworkout_is_red_for_everyone_and_unknown_brand_is_flagged_fairly():
    pre = {**LABEL, "kind": "preworkout", "brand": "NoName", "certs": [], "stimulants": ["кофеин 300 мг"],
           "proprietary_blend": True}
    assert evaluate(pre, TEEN).level == "red" and evaluate(pre, {**TEEN, "age": 25}).level == "red"
    unknown = {**LABEL, "brand": "Какой-то бренд", "certs": []}
    v = evaluate(unknown, TEEN)
    assert v.level == "yellow"
    text = " ".join(v.flags)
    assert "не значит, что продукт плохой" in text  # без голословных обвинений


def test_whey_label_checks_protein_share_and_price():
    whey = {"readable": True, "kind": "whey", "brand": "Brand X", "serving_g": 40, "servings": 25, "net_g": 1000,
            "per_serving": {"protein_g": 18, "sugar_g": 6}, "certs": [], "expiry_visible": True,
            "importer_visible": True}
    v = evaluate(whey, TEEN)
    assert v.level == "yellow" and any("Белка меньше" in f or "55–70%" in f for f in v.flags)
    assert any("Сахара" in f for f in v.flags)
    text = render(v, 10000)
    assert "За 100 г чистого белка" in text


def test_teen_gets_red_for_unsuitable_supplement_and_vitd_overdose():
    caffeine = {**LABEL, "kind": "caffeine", "per_serving": {"caffeine_mg": 200}}
    assert evaluate(caffeine, TEEN).level == "red"
    vit = {**LABEL, "kind": "vitd", "per_serving": {"vitd_iu": 5000}}
    assert evaluate(vit, TEEN).level == "red"
    assert evaluate({"readable": False}, TEEN).level == "yellow"


def test_label_json_parser_ignores_noise():
    assert parse_label_json('вот ответ: {"kind": "whey"} спасибо')["kind"] == "whey"
    assert parse_label_json("нет json") is None


# ---------- где купить ----------
def test_recommend_uses_country_and_budget_tiers():
    kz = recommend({**TEEN, "country": "KZ"}, "creatine")
    assert "Kaspi.kz" in kz and "Creapure" in kz and "Бюджетно" in kz and "Лучшее по качеству" in kz
    ru = recommend({**TEEN, "country": "RU"}, "whey", "best")
    assert "Честный знак" in ru and "Optimum Nutrition" in ru and "Бюджетно" not in ru
    other = recommend({**TEEN, "country": None, "tz": "Etc/GMT-1"}, "creatine")
    assert "Другие страны" in other


def test_buyable_list_is_safe_for_teens():
    assert set(buyable_for(TEEN)) == {"creatine", "whey", "omega3", "vitd", "multivit"}
    assert "caffeine" not in buyable_for({**TEEN, "age": 25})


def test_shops_data_is_consistent():
    from bot.data import shops_data
    d = shops_data()
    for c in d["countries"].values():
        assert {m["tier"] for m in c["markets"]} <= {"budget", "fit", "best"} and c["checks"]
    for sid, b in d["brands"].items():
        assert b["what"] and b["best"] and b["fit"] and b["budget"], sid
    json.dumps(d)
