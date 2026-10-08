"""Раскрытие референса = разбор обработки по этапам: Capture One / Camera Raw → Photoshop → текстуры → Figma по запросу.
Проверяем, что именно уходит модели (и что у других типов этих правил нет)."""

import asyncio
import io
from types import SimpleNamespace

from PIL import Image

import harness  # noqa: F401  (окружение)

from bot import ai

prompts: list[str] = []


class FakeClient:
    async def chat_completion(self, model, messages, **kw):
        prompts.append(next(p["text"] for p in messages[0]["content"] if p["type"] == "text"))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Что хочешь повторить: цвет или кожу?"))])


def jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), "#C8875A").save(buf, "JPEG")
    return buf.getvalue()


async def run():
    ai._client = lambda: FakeClient()
    image = jpeg()
    await ai.next_question("🎨 Референс", "портрет у окна", "Аня", [], [image], [])
    await ai.compose("🎨 Референс", "портрет у окна", "Аня", [], [image], [("Что повторить?", "цвет и кожу")])
    question, composed = prompts
    for text in (question, composed):
        assert "Capture One" in text and "Camera Raw" in text and "Photoshop" in text and "текстур" in text, text
        assert "экспертная оценка" in text
    assert "## 1. RAW: Capture One / Camera Raw" in composed and "## 4. Текстуры и плёнка" in composed
    assert "## 5. Figma (только если нужна вёрстка" in composed and "Чек-лист повтора" in composed
    assert "Палитра фото, посчитанная по пикселям" in composed and "#C" in composed.split("по пикселям")[1]
    # у других типов — без этих правил и палитры
    await ai.compose("💡 Идея", "приложение для заметок", None, [], [image], [])
    assert "экспертная оценка" not in prompts[-1] and "по пикселям" not in prompts[-1]
    print("reference OK")


asyncio.run(run())
