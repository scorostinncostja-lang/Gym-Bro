import json

import pytest

from bot.data import EQUIPMENT_BY_PLACE, PLACES, exercises, program_templates, supplements
from bot.handlers.supplements import card_text
from bot.services import guard
from bot.services.program_builder import alternatives, build_day, template_for
from bot.services.progression import Feel, SetResult, next_target, set_hint
from bot.services.set_parser import parse_set
from bot.services.timeutil import next_training, parse_tz, parse_time


# ---------- прогрессия ----------
def S(w, r, f=Feel.OK):
    return SetResult(w, r, f)


def test_first_time_has_no_weight():
    t = next_target([], 8, 12, "weight", 2.5)
    assert t.weight is None and t.reps == 8


def test_add_weight_when_range_closed_and_not_max():
    t = next_target([S(40, 12), S(40, 12), S(40, 12, Feel.HARD)], 8, 12, "weight", 2.5)
    assert t.weight == 42.5 and t.reps == 8


def test_no_weight_jump_after_failure_to_the_limit():
    t = next_target([S(40, 12), S(40, 12), S(40, 12, Feel.MAX)], 8, 12, "weight", 2.5)
    assert t.weight == 40


def test_add_rep_inside_range():
    t = next_target([S(40, 9), S(40, 9), S(40, 8)], 8, 12, "weight", 2.5)
    assert t.weight == 40 and t.reps == 9


def test_drop_weight_when_far_below_range():
    t = next_target([S(40, 5), S(40, 5), S(40, 4, Feel.MAX)], 8, 12, "weight", 2.5)
    assert t.weight == 37.5


def test_bodyweight_reps_grow_then_hint_to_harder_variant():
    t = next_target([S(0, 10), S(0, 10)], 8, 15, "reps", 0)
    assert t.reps == 11
    t = next_target([S(0, 15, Feel.EASY), S(0, 15, Feel.EASY)], 8, 15, "reps", 0)
    assert "Усложни" in t.note


def test_time_exercise_grows_by_five_seconds():
    t = next_target([S(0, 30), S(0, 30)], 20, 60, "time", 0)
    assert t.reps == 35


def test_hint_easy_adds_rep_and_max_stops_adding():
    h = set_hint(S(40, 10, Feel.EASY), 8, 12, "weight", 2.5)
    assert h.next_reps == 11 and h.next_weight == 40
    h = set_hint(S(40, 12, Feel.EASY), 8, 12, "weight", 2.5)
    assert h.next_weight == 42.5
    h = set_hint(S(40, 10, Feel.MAX), 8, 12, "weight", 2.5)
    assert h.next_weight == 40 and h.next_reps <= 10 and "предел" in h.text.lower()


# ---------- парсер подходов ----------
@pytest.mark.parametrize("text,kind,expected", [
    ("20 на 12", "weight", (20, 12)),
    ("17,5x10", "weight", (17.5, 10)),
    ("20кг 12 раз тяжело", "weight", (20, 12)),
    ("12 раз по 20 кг", "weight", (20, 12)),
    ("12", "reps", (0, 12)),
    ("40 сек", "time", (0, 40)),
])
def test_parse_set_ok(text, kind, expected):
    p = parse_set(text, kind)
    assert p and (p.weight, p.reps) == expected


@pytest.mark.parametrize("text", ["привет", "20", "0 0", "1000 5"])
def test_parse_set_rejects_garbage(text):
    assert parse_set(text, "weight") is None


def test_parse_set_uses_default_weight_and_feel():
    p = parse_set("10 легко", "weight", default_weight=40)
    assert p and p.weight == 40 and p.reps == 10 and p.feel == Feel.EASY
    assert parse_set("не тяжело 40 10", "weight").feel == Feel.EASY


# ---------- программа ----------
@pytest.mark.parametrize("days", [2, 3, 4])
@pytest.mark.parametrize("place", list(PLACES))
def test_every_day_is_buildable_and_respects_equipment(days, place):
    for i in range(len(program_templates(days)) * 2):
        plan = build_day(template_for(days, i), place)
        assert len(plan) >= 4, (days, place, i)
        ids = [p.ex_id for p in plan]
        assert len(ids) == len(set(ids))
        for p in plan:
            assert exercises()[p.ex_id].equip in EQUIPMENT_BY_PLACE[place]
            assert 1 <= p.sets <= 3 and p.lo < p.hi


def test_alternatives_exclude_current_and_stay_in_family():
    alts = alternatives("pushup", "home_bw", {"pushup"})
    assert alts and all(a.id != "pushup" for a in alts)


def test_catalog_integrity():
    for e in exercises().values():
        assert e.cues and e.fedb
        assert e.hasan is None or e.hasan.isdigit()
        assert e.kind in {"weight", "reps", "time"}


# ---------- безопасность ----------
def test_guard_blocks_drugs_for_everyone():
    assert guard.check("какой курс стероидов лучше", 25).reason == "drugs"


@pytest.mark.parametrize("text,reason", [
    ("сколько креатина принимать в день?", "minor_supp_dose"),
    ("как пить протеин и когда", "minor_supp_dose"),
    ("хочу жиросжигатель", "minor_stimulant"),
    ("как быстро похудеть, дефицит калорий?", "minor_diet"),
    ("хочу умереть", "red_flag"),
])
def test_guard_minor_rules(text, reason):
    assert guard.check(text, 15).reason == reason


def test_guard_lets_normal_questions_and_adults_through():
    assert guard.check("как делать присед правильно?", 15) is None
    assert guard.check("сколько креатина принимать?", 25) is None
    assert guard.check("а что с добавками?", None) is None


def test_minor_supplement_cards_give_only_teen_doses():
    for s in supplements().values():
        text = card_text(s, adult=False)
        assert "Дозировка" not in text  # взрослая строка подросткам не показывается
        if s["teen"]["ok"]:
            assert s["teen"]["dose"] in text and "Скажи родителям" in text
        else:
            assert "Тебе" in text and s["teen"]["note"] in text
    # взрослая доза креатина (3–5 г) не просачивается в карточку подростка
    assert supplements()["creatine"]["dose"] not in card_text(supplements()["creatine"], adult=False)


def test_teen_whitelist_and_dangerous_never_allowed():
    ok = {s["id"] for s in supplements().values() if s["teen"]["ok"]}
    assert ok == {"creatine", "whey", "omega3", "vitd", "multivit"}
    for sid in ("preworkout", "fatburner", "testobooster", "caffeine"):
        assert not supplements()[sid]["teen"]["ok"]
    for s in supplements().values():
        assert not (s["verdict"] in ("dangerous", "useless") and s["teen"]["ok"])


def test_dose_reply_uses_reference_and_handles_unknown():
    from bot.services.supp_advice import dose_reply
    assert "3 г в день" in dose_reply("сколько креатина")
    assert "кофеин" in dose_reply("доза кофеина").lower()
    assert "Спроси про конкретную" in dose_reply("какие добавки мне пить")


def test_supplement_data_valid():
    for s in supplements().values():
        assert s["verdict"] in {"works", "conditional", "useless", "dangerous"}
        assert s["evidence"] in set("ABCD")
        for k in ("summary", "who", "dose", "risks", "sources"):
            assert s[k]
        assert s["teen"]["note"]
        if s["teen"]["ok"]:
            assert s["teen"]["dose"] and s["teen"]["who"]
    assert supplements()["preworkout"]["verdict"] == "dangerous"


def test_minor_system_prompt_carries_teen_reference():
    from bot.prompts import system_prompt
    minor = system_prompt({"age": 15})
    adult = system_prompt({"age": 25})
    assert "3 г в день" in minor and "не рекомендовать" in minor
    assert "Справочник добавок для подростков" not in adult


# ---------- время ----------
def test_tz_and_time_parsing():
    assert parse_tz("+5") == "Etc/GMT-5"
    assert parse_tz("UTC-3") == "Etc/GMT+3"
    assert parse_tz("Asia/Almaty") == "Asia/Almaty"
    assert parse_tz("+5:30") is None and parse_tz("мусор") is None
    assert parse_time("1830") == "18:30" and parse_time("9") == "09:00" and parse_time("25:00") is None


def test_next_training_skips_past_time_today():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime(2026, 1, 5, 19, 0, tzinfo=ZoneInfo("Europe/Moscow"))  # понедельник 19:00
    nxt = next_training([0, 2, 4], "18:00", "Europe/Moscow", now=now)
    assert nxt.weekday() == 2 and nxt.hour == 18


def test_data_files_are_valid_json():
    from bot.config import DATA_DIR
    for f in ("exercises.json", "programs.json", "supplements.json"):
        json.loads((DATA_DIR / f).read_text(encoding="utf-8"))


async def test_gemini_call_has_thinking_headroom_and_no_temperature():
    from types import SimpleNamespace

    from bot.config import Settings
    from bot.services.llm import GEMINI_THINKING_HEADROOM, LLM

    seen = {}

    async def fake_generate(**kw):
        seen.update(kw)
        return SimpleNamespace(text="ок")

    llm = LLM(Settings(bot_token="x", gemini_api_key="k", groq_api_key=""))
    llm.gemini = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate)))
    assert await llm.chat("sys", [], "привет", max_tokens=100) == "ок"
    cfg = seen["config"]
    assert seen["model"] == "gemini-3.8-flash"
    assert cfg.max_output_tokens == 100 + GEMINI_THINKING_HEADROOM
    assert cfg.temperature is None
    assert cfg.thinking_config.thinking_level.value == "LOW"
