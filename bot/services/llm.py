"""Обёртка над Gemini (цепочка моделей) и Groq (запасной провайдер).

Бесплатные квоты маленькие и у каждой модели свои, модели бывают перегружены. Поэтому у каждой модели
свой «перерыв» после сбоя: квота кончилась — модель выпадает надолго, таймаут — на 5 минут, перегрузка —
на 2. Живые модели идут первыми, пользователь не ждёт по минуте.
"""
import asyncio
import base64
import io
import json
import logging
import re
import time
from dataclasses import dataclass, field

from google import genai
from google.genai import types
from groq import AsyncGroq, NotFoundError

from bot.config import DEFAULT_GROQ_LLM, DEFAULT_GROQ_VISION, Settings
from bot.data import equipment_catalog
from bot.prompts import FEEL_EXTRACT_PROMPT, GEAR_EXTRACT_PROMPT, SET_EXTRACT_PROMPT
from bot.services.progression import Feel

log = logging.getLogger(__name__)

TIMEOUT = 30  # Groq
GEMINI_TIMEOUT_TEXT = 12
GEMINI_TIMEOUT_MEDIA = 25  # фото и видео читаются дольше
GEMINI_THINKING_HEADROOM = 1024
GROQ_THINKING_HEADROOM = 1024
GROQ_IMAGE_SIDE = 1024  # больше — бесплатный Groq отвечает «Request too large»


class LLMError(RuntimeError):
    """Ни один провайдер не смог ответить."""


def cooldown_for(e: BaseException) -> float:
    """Сколько секунд не трогать модель после такой ошибки."""
    t = repr(e)
    if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
        return 300
    if "RESOURCE_EXHAUSTED" in t or "429" in t:
        if re.search(r"per\s*day|PerDay|daily", t, re.I):
            return 6 * 3600
        m = re.search(r"retry\w*['\"]?\s*[:=]?\s*['\"]?(\d+)(?:\.\d+)?s", t, re.I)
        return float(min(3600, max(60, int(m.group(1))))) if m else 1800
    if "503" in t or "UNAVAILABLE" in t or "overloaded" in t:
        return 120
    if "404" in t or "NOT_FOUND" in t or "not found" in t:
        return 24 * 3600
    return 60


def short_error(e: BaseException) -> str:
    t = repr(e)
    if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
        return "не ответила вовремя"
    if "RESOURCE_EXHAUSTED" in t or "429" in t:
        return "лимит бесплатной квоты"
    if "503" in t or "UNAVAILABLE" in t:
        return "перегружена у Google"
    if "404" in t or "NOT_FOUND" in t:
        return "модели нет"
    if "401" in t or "403" in t or "API key" in t:
        return "ключ не подходит"
    return t[:80]


def shrink_image(data: bytes, side: int = GROQ_IMAGE_SIDE) -> bytes:
    """Уменьшает фото до side по длинной стороне. Без Pillow или при ошибке отдаёт как есть."""
    try:
        from PIL import Image

        im = Image.open(io.BytesIO(data))
        if max(im.size) <= side and len(data) < 400_000:
            return data
        im.thumbnail((side, side))
        out = io.BytesIO()
        im.convert("RGB").save(out, "JPEG", quality=82)
        return out.getvalue()
    except Exception:  # noqa: BLE001
        return data


@dataclass(frozen=True)
class Media:
    data: bytes
    mime: str  # image/jpeg, video/mp4 ...

    @property
    def is_video(self) -> bool:
        return self.mime.startswith("video/")


@dataclass
class Health:
    ok: bool | None = None
    ms: int = 0
    err: str = ""
    until: float = 0.0  # monotonic: до этого момента модель не трогаем
    calls: int = 0
    fails: int = 0


@dataclass
class _State:
    health: dict[str, Health] = field(default_factory=dict)
    dead_groq: set[str] = field(default_factory=set)


class LLM:
    def __init__(self, settings: Settings):
        self.s = settings
        self.gemini = genai.Client(api_key=settings.gemini_api_key) if settings.gemini_api_key else None
        self.groq = AsyncGroq(api_key=settings.groq_api_key) if settings.groq_api_key else None
        self._st = _State()

    @property
    def available(self) -> bool:
        return bool(self.gemini or self.groq)

    @property
    def gemini_models(self) -> list[str]:
        chain = [self.s.gemini_model, self.s.gemini_fallback_model, *self.s.gemini_fallback_models]
        return list(dict.fromkeys(m for m in chain if m))

    def health(self, name: str) -> Health:
        return self._st.health.setdefault(name, Health())

    def _ok(self, name: str, started: float) -> None:
        h = self.health(name)
        h.ok, h.ms, h.err, h.until, h.calls = True, int((time.monotonic() - started) * 1000), "", 0.0, h.calls + 1

    def _fail(self, name: str, e: BaseException, started: float) -> None:
        h = self.health(name)
        h.ok, h.ms, h.err = False, int((time.monotonic() - started) * 1000), short_error(e)
        h.until = time.monotonic() + cooldown_for(e)
        h.calls += 1
        h.fails += 1

    def _live(self, models: list[str]) -> list[str]:
        now = time.monotonic()
        return [m for m in models if self.health(m).until <= now]

    async def chat(
        self,
        system: str,
        history: list[tuple[str, str]],
        user_text: str,
        media: "Media | list[Media] | None" = None,
        max_tokens: int = 700,
        temperature: float = 0.6,
    ) -> str:
        """Живые модели Gemini по порядку, затем Groq, в крайнем случае «отдыхающая» модель Gemini."""
        medias = [] if media is None else media if isinstance(media, list) else [media]
        errors: list[str] = []
        timeout = GEMINI_TIMEOUT_MEDIA if medias else GEMINI_TIMEOUT_TEXT
        tried: set[str] = set()

        async def try_gemini(model: str) -> str | None:
            tried.add(model)
            started = time.monotonic()
            try:
                text = await asyncio.wait_for(
                    self._gemini(model, system, history, user_text, medias, max_tokens), timeout
                )
            except Exception as e:  # noqa: BLE001 — любой сбой модели = пробуем следующую
                log.warning("Gemini %s: %s", model, short_error(e))
                self._fail(model, e, started)
                errors.append(f"{model}: {short_error(e)}")
                return None
            self._ok(model, started)
            return text

        if self.gemini:
            for model in self._live(self.gemini_models):
                if (text := await try_gemini(model)) is not None:
                    return text
        if self.groq and not any(m.is_video for m in medias):
            started = time.monotonic()
            try:
                text = await asyncio.wait_for(
                    self._groq(system, history, user_text, medias, max_tokens, temperature), TIMEOUT
                )
                self._ok("groq", started)
                return text
            except Exception as e:  # noqa: BLE001
                log.warning("Groq: %s", short_error(e))
                self._fail("groq", e, started)
                errors.append(f"groq: {short_error(e)}")
        if self.gemini:  # всё упало: последний шанс у модели, которая раньше всех «отдохнёт»
            rest = sorted((m for m in self.gemini_models if m not in tried), key=lambda m: self.health(m).until)
            for model in rest[:1]:
                if (text := await try_gemini(model)) is not None:
                    return text
        raise LLMError("; ".join(errors) or "нет настроенных провайдеров")

    async def _gemini(self, model, system, history, user_text, media, max_tokens) -> str:
        assert self.gemini
        contents = [
            types.Content(
                role="user" if role == "user" else "model",
                parts=[types.Part.from_text(text=text)],
            )
            for role, text in history
        ]
        parts = [types.Part.from_text(text=user_text)]
        for m in reversed(media or []):
            parts.insert(0, types.Part.from_bytes(data=m.data, mime_type=m.mime))
        contents.append(types.Content(role="user", parts=parts))
        # У Gemini 3.x токены «размышления» входят в max_output_tokens: без запаса ответ обрезается
        # или приходит пустым. Температуру не передаём: для 3.x sampling-параметры устарели.
        resp = await self.gemini.aio.models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=max_tokens + GEMINI_THINKING_HEADROOM,
                thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW),
            ),
        )
        text = (resp.text or "").strip()
        if not text:
            raise LLMError("пустой ответ Gemini")
        return text

    async def _groq(self, system, history, user_text, media, max_tokens, temperature) -> str:
        assert self.groq
        messages: list[dict] = [{"role": "system", "content": system}]
        messages += [
            {"role": "user" if role == "user" else "assistant", "content": text} for role, text in history
        ]
        if media:
            content = [{"type": "text", "text": user_text}] + [
                {"type": "image_url", "image_url": {
                    "url": f"data:image/jpeg;base64,{base64.b64encode(shrink_image(m.data)).decode()}"}}
                for m in media
            ]
            models = [self.s.groq_vision_model, DEFAULT_GROQ_VISION]
        else:
            content = user_text
            models = [self.s.groq_llm_model, DEFAULT_GROQ_LLM]
        messages.append({"role": "user", "content": content})
        last: Exception | None = None
        for model in dict.fromkeys(models):  # без повторов, порядок сохраняется
            if model in self._st.dead_groq:
                continue
            kwargs: dict = {"max_tokens": max_tokens + GROQ_THINKING_HEADROOM, "temperature": temperature}
            if model.startswith("openai/gpt-oss"):
                kwargs["reasoning_effort"] = "low"  # иначе «размышления» съедают лимит ответа
            try:
                resp = await self.groq.chat.completions.create(model=model, messages=messages, **kwargs)
            except NotFoundError as e:  # модель убрана у провайдера: запоминаем и больше не дёргаем
                log.warning("Groq: модели %s нет, перехожу на актуальную", model)
                self._st.dead_groq.add(model)
                last = e
                continue
            text = (resp.choices[0].message.content or "").strip()
            if not text:
                raise LLMError("пустой ответ Groq")
            return text
        raise LLMError(f"Groq: нет доступных моделей ({last!r})")

    async def ping_all(self) -> list[tuple[str, bool, int, str]]:
        """Проверка всех моделей одним коротким запросом (команда /ai). Обновляет «перерывы»."""
        async def gem(model: str):
            started = time.monotonic()
            try:
                await asyncio.wait_for(self._gemini(model, "Отвечай одним словом.", [], "Скажи: ок", [], 20),
                                       GEMINI_TIMEOUT_TEXT)
                self._ok(model, started)
            except Exception as e:  # noqa: BLE001
                self._fail(model, e, started)
            h = self.health(model)
            return model, bool(h.ok), h.ms, h.err

        async def grq():
            started = time.monotonic()
            try:
                await asyncio.wait_for(self._groq("Отвечай одним словом.", [], "Скажи: ок", [], 20, 0), TIMEOUT)
                self._ok("groq", started)
            except Exception as e:  # noqa: BLE001
                self._fail("groq", e, started)
            h = self.health("groq")
            return f"Groq ({self.s.groq_llm_model if self.s.groq_llm_model not in self._st.dead_groq else DEFAULT_GROQ_LLM})", bool(h.ok), h.ms, h.err

        jobs = [gem(m) for m in self.gemini_models] if self.gemini else []
        if self.groq:
            jobs.append(grq())
        return list(await asyncio.gather(*jobs))

    async def classify_feel(self, text: str) -> tuple[Feel | None, bool]:
        """Свободный текст о подходе («еле дожал, колено ныло») в (самочувствие, есть ли боль)."""
        try:
            raw = await self.chat(FEEL_EXTRACT_PROMPT, [], text, max_tokens=60, temperature=0)
        except LLMError:
            return None, False
        m = re.search(r"\{.*?\}", raw, re.S)
        if not m:
            return None, False
        try:
            data = json.loads(m.group(0))
        except ValueError:
            return None, False
        feel = {"easy": Feel.EASY, "ok": Feel.OK, "hard": Feel.HARD, "max": Feel.MAX}.get(str(data.get("feel")))
        return feel, bool(data.get("pain"))

    async def extract_gear(self, text: str) -> tuple[list[dict], list[str]] | None:
        """Нестандартное описание инвентаря -> (предметы, непонятое). None, если модель не справилась."""
        try:
            raw = await self.chat(GEAR_EXTRACT_PROMPT, [], text, max_tokens=400, temperature=0)
        except LLMError:
            return None
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except ValueError:
            return None
        valid = {c["id"] for c in equipment_catalog()}
        items = []
        for i in data.get("items") or []:
            if not isinstance(i, dict) or i.get("id") not in valid:
                continue
            item = {"id": i["id"]}
            if isinstance(i.get("count"), int) and 0 < i["count"] <= 20:
                item["count"] = i["count"]
            weights = [float(w) for w in (i.get("weights") or []) if isinstance(w, (int, float)) and 0 < w <= 300]
            if weights:
                item["weights"] = sorted(set(weights))
            if i.get("adjustable") and isinstance(i.get("max"), (int, float)) and 0 < i["max"] <= 300:
                item.update(adjustable=True, min=float(i.get("min") or 1), max=float(i["max"]),
                            step=2.5 if i["max"] > 20 else 1.0)
                item.pop("weights", None)
            items.append(item)
        other = [str(o)[:60] for o in (data.get("other") or []) if o][:5]
        return items, other

    async def extract_set(self, text: str) -> tuple[float | None, int | None]:
        """Запасной разбор подхода, когда регулярки не справились (слова вместо цифр)."""
        try:
            raw = await self.chat(SET_EXTRACT_PROMPT, [], text, max_tokens=60, temperature=0)
        except LLMError:
            return None, None
        m = re.search(r"\{.*?\}", raw, re.S)
        if not m:
            return None, None
        try:
            data = json.loads(m.group(0))
            w, r = data.get("weight"), data.get("reps")
            return (float(w) if w is not None else None, int(r) if r is not None else None)
        except (ValueError, TypeError):
            return None, None

    async def transcribe(self, audio: bytes, filename: str = "voice.ogg") -> str:
        """Речь в текст через Groq Whisper."""
        if not self.groq:
            raise LLMError("GROQ_API_KEY не задан")
        resp = await asyncio.wait_for(
            self.groq.audio.transcriptions.create(
                file=(filename, audio),
                model=self.s.groq_stt_model,
                language="ru",
                prompt="Тренировка. Вес и повторения: двадцать килограммов на двенадцать раз.",
            ),
            TIMEOUT,
        )
        return (resp.text or "").strip()
