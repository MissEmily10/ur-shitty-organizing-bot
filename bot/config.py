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
# Картинки (иконки проектов). Позже сюда можно вписать свою LoRA-модель в её стиле
IMAGE_MODEL = env("IMAGE_MODEL", "black-forest-labs/FLUX.1-schnell")
AUTO_ICONS = env("AUTO_ICONS", "1") == "1"
BANNERS = env("BANNERS", "1") == "1"  # меню с картинками по умолчанию (каждый может выключить в /settings)  # «0» — не рисовать иконку сама при создании проекта

# Render задаёт RENDER_EXTERNAL_URL сам. Без него бот работает в режиме polling (удобно локально).
WEBHOOK_BASE = env("WEBHOOK_URL") or env("RENDER_EXTERNAL_URL")
PORT = int(env("PORT", "8080"))

# Командный режим. ИИ работает на токене владелицы, поэтому у участников дневные лимиты (на владелицу не действуют).
MEMBER_DAILY_LIMIT = int(env("MEMBER_DAILY_LIMIT", "30"))  # заметок в день
MEMBER_AI_LIMIT = int(env("MEMBER_AI_LIMIT", "20"))  # запросов к ИИ в день: 🤖, ✨ раскрытие, /ask
INVITE_DAYS = int(env("INVITE_DAYS", "7"))  # сколько дней действует код приглашения
# Командные проекты пока создаёт только владелица; «1» — разрешить всем участникам
TEAM_PROJECTS_BY_MEMBERS = env("TEAM_PROJECTS_BY_MEMBERS", "") == "1"



def secret(purpose: str) -> str:
    """Стабильный секрет, выведенный из токена бота: отдельная переменная окружения не нужна."""
    return hashlib.sha256(f"{purpose}:{TELEGRAM_TOKEN}".encode()).hexdigest()[:32]


# Имена полей в Notion
P_TITLE = "Название"
P_STATUS = "Статус"
P_PROJECT = "Проект"
P_SOURCE = "Источник"
P_TAGS = "Теги"
P_TYPE = "Тип"
P_AUTHOR = "Автор"
P_AUTHOR_ID = "Автор ID"
P_WHEN = "Когда"
P_DONE = "Выполнено"
P_AI_TYPE = "Тип (ИИ)"
P_MERGED = "Объединено"  # заметку объединили с другими в одну: в поиске похожих и разборе больше не участвует
STATUS_NEW = "Неразобрано"
STATUS_DONE = "Разобрано"
