"""Обёртка над Gemini (основной) и Groq (запасной)."""
import asyncio
import base64
import json
import logging
import re
import time
from dataclasses import dataclass

from google import genai
from google.genai import types
from groq import AsyncGroq, NotFoundError

from bot.config import DEFAULT_GROQ_LLM, DEFAULT_GROQ_VISION, Settings
from bot.data import equipment_catalog
from bot.prompts import FEEL_EXTRACT_PROMPT, GEAR_EXTRACT_PROMPT, SET_EXTRACT_PROMPT
from bot.services.progression import Feel

log = logging.getLogger(__name__)

TIMEOUT = 30  # Groq и одиночные вызовы
GEMINI_TIMEOUT = 15  # на модель: перегруженная модель отвечает долго, лучше быстро уйти на запасную
COOLDOWN = 120  # после сбоя модель на столько секунд выпадает из очереди
GEMINI_THINKING_HEADROOM = 1024
GROQ_THINKING_HEADROOM = 1024
RETRY_DELAYS = (1.5,)  # повтор после 503/429 на основной модели Gemini


class LLMError(RuntimeError):
    """Ни один провайдер не смог ответить."""


def _transient(e: Exception) -> bool:
    """Временный сбой: перегрузка, лимит, таймаут. Стоит повторить или уйти на запасной вариант."""
    text = repr(e)
    return isinstance(e, asyncio.TimeoutError) or any(
        k in text for k in ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "overloaded", "Timeout")
    )


@dataclass(frozen=True)
class Media:
    data: bytes
    mime: str  # image/jpeg, video/mp4 ...

    @property
    def is_video(self) -> bool:
        return self.mime.startswith("video/")


class LLM:
    def __init__(self, settings: Settings):
        self.s = settings
        self.gemini = genai.Client(api_key=settings.gemini_api_key) if settings.gemini_api_key else None
        self.groq = AsyncGroq(api_key=settings.groq_api_key) if settings.groq_api_key else None
        self._bad_until: dict[str, float] = {}

    @property
    def available(self) -> bool:
        return bool(self.gemini or self.groq)

    async def chat(
        self,
        system: str,
        history: list[tuple[str, str]],
        user_text: str,
        media: "Media | list[Media] | None" = None,
        max_tokens: int = 700,
        temperature: float = 0.6,
    ) -> str:
        """Gemini (с повтором и запасной моделью), затем Groq. media: одно фото или несколько."""
        medias = [] if media is None else media if isinstance(media, list) else [media]
        media = medias  # дальше везде список
        errors: list[str] = []
        if self.gemini:
            models = [self.s.gemini_model]
            if self.s.gemini_fallback_model and self.s.gemini_fallback_model not in models:
                models.append(self.s.gemini_fallback_model)
            now = time.monotonic()
            # Модели, которые только что падали, пропускаем; если упали все, пробуем по порядку.
            live = [m for m in models if self._bad_until.get(m, 0) <= now] or models
            for n, model in enumerate(live):
                delays = RETRY_DELAYS if n == 0 and len(live) > 1 else ()
                for attempt in range(len(delays) + 1):
                    try:
                        return await asyncio.wait_for(
                            self._gemini(model, system, history, user_text, media, max_tokens), GEMINI_TIMEOUT
                        )
                    except Exception as e:  # noqa: BLE001 — любой сбой провайдера = пробуем дальше
                        log.warning("Gemini %s: %r", model, e)
                        errors.append(f"{model}: {e!r}")
                        if _transient(e):
                            self._bad_until[model] = time.monotonic() + COOLDOWN
                            if isinstance(e, asyncio.TimeoutError):
                                break  # таймаут = перегрузка, сразу следующая модель
                            if attempt < len(delays):
                                await asyncio.sleep(delays[attempt])
                                continue
                        break
        if self.groq and not any(m.is_video for m in medias):
            try:
                return await asyncio.wait_for(
                    self._groq(system, history, user_text, media, max_tokens, temperature), TIMEOUT
                )
            except Exception as e:  # noqa: BLE001
                log.warning("Groq failed: %r", e)
                errors.append(f"groq: {e!r}")
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
                {"type": "image_url", "image_url": {"url": f"data:{m.mime};base64,{base64.b64encode(m.data).decode()}"}}
                for m in media
            ]
            models = [self.s.groq_vision_model, DEFAULT_GROQ_VISION]
        else:
            content = user_text
            models = [self.s.groq_llm_model, DEFAULT_GROQ_LLM]
        messages.append({"role": "user", "content": content})
        last: Exception | None = None
        for model in dict.fromkeys(models):  # без повторов, порядок сохраняется
            kwargs: dict = {"max_tokens": max_tokens + GROQ_THINKING_HEADROOM, "temperature": temperature}
            if model.startswith("openai/gpt-oss"):
                kwargs["reasoning_effort"] = "low"  # иначе «размышления» съедают лимит ответа
            try:
                resp = await self.groq.chat.completions.create(model=model, messages=messages, **kwargs)
            except NotFoundError as e:  # модель убрана у провайдера: пробуем актуальную
                log.warning("Groq: модели %s нет, пробую следующую", model)
                last = e
                continue
            text = (resp.choices[0].message.content or "").strip()
            if not text:
                raise LLMError("пустой ответ Groq")
            return text
        raise LLMError(f"Groq: нет доступных моделей ({last!r})")

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
