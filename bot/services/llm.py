"""Обёртка над Gemini (основной) и Groq (запасной)."""
import asyncio
import base64
import json
import logging
import re
from dataclasses import dataclass

from google import genai
from google.genai import types
from groq import AsyncGroq

from bot.config import Settings
from bot.prompts import SET_EXTRACT_PROMPT

log = logging.getLogger(__name__)

TIMEOUT = 45


class LLMError(RuntimeError):
    """Ни один провайдер не смог ответить."""


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

    @property
    def available(self) -> bool:
        return bool(self.gemini or self.groq)

    async def chat(
        self,
        system: str,
        history: list[tuple[str, str]],
        user_text: str,
        media: Media | None = None,
        max_tokens: int = 700,
        temperature: float = 0.6,
    ) -> str:
        errors: list[str] = []
        if self.gemini:
            try:
                return await asyncio.wait_for(
                    self._gemini(system, history, user_text, media, max_tokens, temperature), TIMEOUT
                )
            except Exception as e:  # noqa: BLE001 — любой сбой провайдера = пробуем запасной
                log.warning("Gemini failed: %r", e)
                errors.append(f"gemini: {e!r}")
        if self.groq and not (media and media.is_video):
            try:
                return await asyncio.wait_for(
                    self._groq(system, history, user_text, media, max_tokens, temperature), TIMEOUT
                )
            except Exception as e:  # noqa: BLE001
                log.warning("Groq failed: %r", e)
                errors.append(f"groq: {e!r}")
        raise LLMError("; ".join(errors) or "нет настроенных провайдеров")

    async def _gemini(self, system, history, user_text, media, max_tokens, temperature) -> str:
        assert self.gemini
        contents = [
            types.Content(
                role="user" if role == "user" else "model",
                parts=[types.Part.from_text(text=text)],
            )
            for role, text in history
        ]
        parts = [types.Part.from_text(text=user_text)]
        if media:
            parts.insert(0, types.Part.from_bytes(data=media.data, mime_type=media.mime))
        contents.append(types.Content(role="user", parts=parts))
        resp = await self.gemini.aio.models.generate_content(
            model=self.s.gemini_model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system, max_output_tokens=max_tokens, temperature=temperature
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
            b64 = base64.b64encode(media.data).decode()
            content = [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": f"data:{media.mime};base64,{b64}"}},
            ]
            model = self.s.groq_vision_model
        else:
            content = user_text
            model = self.s.groq_llm_model
        messages.append({"role": "user", "content": content})
        resp = await self.groq.chat.completions.create(
            model=model, messages=messages, max_tokens=max_tokens, temperature=temperature
        )
        text = (resp.choices[0].message.content or "").strip()
        if not text:
            raise LLMError("пустой ответ Groq")
        return text

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
