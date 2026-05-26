# Сказка — Telegram-бот персональных сказок на ночь

Микро-SaaS: бот пишет короткую сказку на ночь с именем ребёнка, выбранным героем и моральным посылом, собирает PDF-книжку с тремя авторскими иллюстрациями. Родитель сам читает её ребёнку перед сном.

Монетизация:
- 1 сказка бесплатно (триал)
- Подписка / пакет — безлимит сказок с тремя иллюстрациями и PDF
- Подарок другу 199 ₽ — одна персональная сказка под имя другого ребёнка (PDF + текст + обложка)

Платежи — Telegram Payments через ЮKassa (своё ИП). Рекуррент через прямой API ЮKassa с сохранённым `payment_method_id`.

## Стек

- Python 3.12, aiogram 3.x, SQLAlchemy 2.0 async, Postgres 16
- Claude Sonnet 4.6 с prompt caching (тексты сказок), Recraft v3 с натренированным custom style (иллюстрации), ReportLab (PDF)
- Docker Compose, бэкапы Postgres каждые 6 часов

## История

В мае 2026 убрали TTS-озвучку и фоновую музыку — продукт стал чистым PDF, без аудио. Соответствующий код (`tts.py`, `tts_azure.py`, `bg_music.py`) можно найти в git-истории, если когда-то понадобится откатить.

## Структура

```
skazka-bot/
├── src/
│   ├── main.py                # точка входа + cron рекуррента
│   ├── config.py              # загрузка .env
│   ├── states.py              # FSM
│   ├── prompts.py             # системные промпты, темы, картинка
│   ├── db/                    # модели и сессия
│   ├── services/              # LLM, TTS, image, billing
│   ├── handlers/              # start, story, library, billing, referral, faq
│   └── keyboards/             # inline-клавиатуры
├── landing/index.html         # одностраничный лендинг
├── marketing/                 # сценарий ролика, креативы, посты для чатов
├── docs/                      # DEPLOY.md, LAUNCH_CHECKLIST.md
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

## Быстрый старт (локально, 5 минут)

```bash
cp .env.example .env
# заполни BOT_TOKEN, YOOKASSA_PROVIDER_TOKEN, GEMINI_API_KEY (минимум)

docker compose up --build
```

После запуска бот сразу принимает /start. Озвучка и картинка — только если задал `ELEVENLABS_API_KEY` и `FAL_KEY`.

## Боевой запуск

См. [docs/DEPLOY.md](docs/DEPLOY.md) — пошаговый гайд для VPS Selectel / timeweb и [docs/LAUNCH_CHECKLIST.md](docs/LAUNCH_CHECKLIST.md) — почасовой чеклист на выходные.

## Команды бота

- `/start` — главное меню
- `/cancel_subscription` — отмена подписки (без потери доступа до конца периода)
- `/refund` — возврат в первые 7 дней
- `/support` — связь с поддержкой
- `/delete_me` — удалить аккаунт и все данные (152-ФЗ)

## Лицензия

MIT. Делай что хочешь, но не вини меня если что-то сломается в проде.
