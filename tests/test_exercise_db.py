from bot.services.exercise_db import ExerciseDB, _compact


REC = {
    "id": "0001", "name": "sled 45в° leg press", "category": "upper legs", "body_part": "upper legs",
    "equipment": "sled machine", "muscle_group": "quadriceps", "secondary_muscles": ["glutes"],
    "instruction_steps": {"ru": ["Сядьте.", "Жмите."], "en": ["Sit.", "Press."]},
    "gif_url": "videos/0001-x.gif", "image": "images/0001-x.jpg",
}


def make():
    db = ExerciseDB()
    db.items = {"0001": ExerciseDB._to_obj(_compact(REC))}
    return db


def test_compact_and_urls():
    db = make()
    e = db.get("0001")
    assert e.name == "sled 45° leg press" and e.steps_ru == ("Сядьте.", "Жмите.")
    assert db.gif_url("0001").startswith("https://raw.githubusercontent.com/hasaneyldrm/exercises-dataset/")
    assert db.gif_url("0001").endswith("videos/0001-x.gif")
    assert db.gif_url("nope") is None and db.gif_url(None) is None


def test_search_and_parts():
    db = make()
    assert db.search("leg press")[0].id == "0001"
    assert db.search("совсем другое") == []
    assert db.parts() == ["upper legs"] and db.by_part("upper legs")[0].id == "0001"


def test_instructions_string_fallback():
    rec = dict(REC, instruction_steps={}, instructions={"ru": "Сядьте. Жмите!"})
    assert _compact(rec)["steps_ru"] == ["Сядьте.", "Жмите!"]
