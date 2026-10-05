# ur-shitty-organizing-bot

Личный конвейер мыслей: **Telegram → ИИ (Hugging Face) → Notion**.

- Присылаете боту текст, фото страницы блокнота или голосовое.
- ИИ распознаёт и структурирует хаос в Markdown, бот кладёт карточку в таблицу Notion «Входящие идеи» со статусом «Неразобрано».
- В 20:00 по Москве бот присылает «🌙 У тебя есть неразобранное» с кнопкой «Разобрать». Дальше по одной идее: кнопка проекта, «🗑 Удалить» или «⏭ Позже». То же самое запускает команда `/razbor`.

Проекты для кнопок берутся из вариантов поля **«Проект»** в таблице Notion. Добавили новый вариант, и в боте появилась новая кнопка, код трогать не нужно.

## Как это устроено

| Что | Где работает |
|---|---|
| Бот (приём заметок, кнопки) | Render, бесплатный тариф, webhook |
| Вечерний пуш | GitHub Actions по расписанию (`.github/workflows/remind.yml`) |
| Голос → текст | Hugging Face, `openai/whisper-large-v3` |
| Фото/текст → структура | Hugging Face, `Qwen/Qwen2.5-VL-7B-Instruct` |

Модели меняются переменными `VISION_MODEL` и `ASR_MODEL`, например на `meta-llama/Llama-3.2-11B-Vision-Instruct` (сначала нужно принять лицензию Meta на странице модели).

На бесплатном Render сервис засыпает после 15 минут тишины. Первое сообщение после паузы бот обработает с задержкой до минуты, это нормально.

## Настройка (один раз)

### 1. Hugging Face
1. Зарегистрируйтесь на [huggingface.co](https://huggingface.co).
2. Settings → Access Tokens → **Create new token** → тип *Fine-grained* → отметьте **Make calls to Inference Providers**.
3. Скопируйте токен (`hf_...`). Это `HF_TOKEN`.

Бесплатных кредитов на вызовы немного. Если кончатся, ошибка будет видна прямо в ответе бота.

### 2. Notion
1. [notion.so/profile/integrations](https://www.notion.so/profile/integrations) → **New integration** → тип *Internal* → сохранить.
2. Скопируйте *Internal Integration Secret*. Это `NOTION_TOKEN`.
3. Откройте страницу **«Организатор мыслей»** → `•••` → **Connections** → добавьте свою интеграцию. Без этого шага бот не увидит таблицу.

`NOTION_DATABASE_ID` уже прописан: `253fac34a9674889b6e4035acc27828d`.

### 3. Render (сам бот)
1. [dashboard.render.com](https://dashboard.render.com) → **New → Blueprint** → подключите этот репозиторий.
2. Render прочитает `render.yaml` и попросит заполнить `TELEGRAM_TOKEN`, `HF_TOKEN`, `NOTION_TOKEN`. `OWNER_ID` пока можно оставить пустым.
3. После деплоя напишите боту `/start`. Он ответит вашим Telegram ID.
4. Впишите ID в `OWNER_ID` (Render → сервис → Environment) и сохраните, сервис перезапустится. Теперь бот слушается только вас.

### 4. GitHub (вечерний пуш)
Репозиторий → Settings → Secrets and variables → Actions → **New repository secret**, четыре штуки:
`TELEGRAM_TOKEN`, `OWNER_ID`, `NOTION_TOKEN`, `NOTION_DATABASE_ID`.

Проверить сразу: вкладка Actions → «Вечерний пуш» → **Run workflow**.
Время меняется в `remind.yml`. Cron там в UTC: 20:00 МСК = `0 17 * * *`.

## Запуск локально

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # заполните
set -a && source .env && set +a
python -m bot.main     # без WEBHOOK_URL бот работает в режиме polling
```

Если бот уже задеплоен на Render, локально его не запускайте с тем же токеном: Telegram отдаёт обновления только одному из них.
