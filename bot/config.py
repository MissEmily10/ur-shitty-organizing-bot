import hashlib
import os
import sys


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or default


def require(*names: str) -> None:
    missing = [n for n in names if not env(n)]
    if missing:
        sys.exit(f"Не заданы переменные окружения: {', '.join(missing)}")


TELEGRAM_TOKEN = env("TELEGRAM_TOKEN")
OWNER_ID = int(env("OWNER_ID", "0"))
HF_TOKEN = env("HF_TOKEN")
NOTION_TOKEN = env("NOTION_TOKEN")
NOTION_DATABASE_ID = env("NOTION_DATABASE_ID")

VISION_MODEL = env("VISION_MODEL", "google/gemma-4-26B-A4B-it")
ASR_MODEL = env("ASR_MODEL", "openai/whisper-large-v3")

# Render задаёт RENDER_EXTERNAL_URL сам. Без него бот работает в режиме polling (удобно локально).
WEBHOOK_BASE = env("WEBHOOK_URL") or env("RENDER_EXTERNAL_URL")
PORT = int(env("PORT", "8080"))

# Сколько заметок в день может отправить участник (на владельца лимит не действует): ИИ работает на токене владельца
MEMBER_DAILY_LIMIT = int(env("MEMBER_DAILY_LIMIT", "30"))



def secret(purpose: str) -> str:
    """Стабильный секрет, выведенный из токена бота: отдельная переменная окружения не нужна."""
    return hashlib.sha256(f"{purpose}:{TELEGRAM_TOKEN}".encode()).hexdigest()[:32]


# Имена полей в Notion
P_TITLE = "Название"
P_STATUS = "Статус"
P_PROJECT = "Проект"
P_SOURCE = "Источник"
P_TAGS = "Теги"
P_AUTHOR = "Автор"
P_AUTHOR_ID = "Автор ID"
STATUS_NEW = "Неразобрано"
STATUS_DONE = "Разобрано"
