# Настройка премиум-стека: Azure TTS + Claude Sonnet 4.6 + Recraft v3

Этот документ — пошаговая инструкция как получить три ключа, которые нужны для нового стека.

Когда всё получишь — кладёшь в `.env` (см. секцию **«Что класть в .env»** в самом низу).

---

## 1. Azure Speech (TTS — голос)

Что покупаем: Azure Cognitive Services Speech, тариф Neural — это $16 за 1M символов. Одна сказка ~5000 символов = ~7 ₽. Голос Светлана (`ru-RU-SvetlanaNeural`) — тёплая взрослая женщина, идеально для сказок на ночь. Или Дария (`ru-RU-DariyaNeural`), Светлана чаще выбирают для детских проектов.

### Шаги

1. Зайди на https://portal.azure.com — авторизуйся через любую почту (Gmail, Outlook, Apple ID — без разницы).
2. **Первый раз:** Azure попросит создать подписку. Выбери **Free Trial / Бесплатная пробная** — даёт $200 кредитов на месяц, дальше Pay-As-You-Go. Привязка карты обязательна, но списаний не будет, пока не выгребешь $200 (хватит на ~28 000 сказок).
3. В поиске сверху набери **Speech services** → Create.
4. Заполни:
   - **Subscription:** твоя (Free Trial или Pay-As-You-Go)
   - **Resource group:** Create new → `skazka-rg`
   - **Region:** `West Europe` (Frankfurt) или `North Europe` (Dublin) — ближе всего к РФ, минимальная задержка. **НЕ ставь US** — будет лагать на 200ms.
   - **Name:** `skazka-tts`
   - **Pricing tier:** **Standard S0** (это и есть Neural pricing — название «Standard» сбивает с толку, но это правильный тариф)
5. Review + Create → Create. Ждёшь ~30 секунд.
6. Открой созданный ресурс → слева в меню **Keys and Endpoint**.
7. Скопируй:
   - **KEY 1** → это твой `AZURE_SPEECH_KEY`
   - **Location/Region** (например `westeurope`) → это `AZURE_SPEECH_REGION`

### Проверка
Можешь сразу протестировать голос в Azure Speech Studio: https://speech.microsoft.com/portal → Voice Gallery → Russian → Светлана → послушай сэмплы. Если хочешь другой голос — там же выберешь и скажешь мне.

---

## 2. Anthropic API (Claude Sonnet 4.6 для сказок)

**Важно про путаницу:** подписка на Claude.ai (Pro $20/мес или Max $100/мес) — это **отдельный продукт** от Anthropic API. Это два разных биллинга, два разных аккаунта, два разных интерфейса.

- **Claude.ai Pro/Max** = чат-интерфейс для людей. Подписка фиксированная, лимиты сообщений, нет API доступа.
- **Anthropic API (console.anthropic.com)** = программный доступ для разработчиков. Pay-as-you-go (платишь за токены). Это то, что нужно боту.

Если у тебя сейчас Pro на claude.ai — это никак не помогает боту. Нужно открыть **отдельный API аккаунт**. Можно на ту же почту, можно на другую.

### Шаги

1. Зайди на https://console.anthropic.com → Sign Up / Log In.
2. Если ещё нет организации — создай (можно «Skazka Bot» или своё имя).
3. Слева в меню **Plans & Billing** → **Add credits**.
4. Пополни на $20–50 для начала. Это безопасный буфер: при себестоимости ~3 ₽/сказка на тексте $20 хватит на ~600 сказок без кэширования или ~3000 сказок с кэшированием. Без пополнения API не работает (даже на бесплатном trial $5 — он скоро закончится).
5. Включи **Auto-reload** если хочешь чтобы баланс автоматически пополнялся (например +$10 когда падает ниже $5) — удобно для прода.
6. Слева **API Keys** → **Create Key**.
7. Назови ключ `skazka-bot-prod`, скопируй (показывается **один раз**) → это твой `ANTHROPIC_API_KEY` (начинается с `sk-ant-api03-...`).

### Какую модель использовать
В коде по умолчанию стоит `claude-sonnet-4-6` (последняя Sonnet, октябрь 2025). Цены: $3/M input, $15/M output, $0.30/M cached read. Одна сказка с кэшированным промптом = ~2.8 ₽.

Если решишь экономить — `claude-haiku-4-5` дешевле в 3 раза ($1/M input, $5/M output) и тоже отлично пишет, но менее литературно. Менять можно в `.env` (`ANTHROPIC_MODEL=claude-haiku-4-5-20251001`).

---

## 3. FAL (для Recraft v3 — обложки)

Тут хорошие новости: у тебя ключ FAL уже есть (`FAL_KEY` в `.env`), Recraft v3 крутится на том же провайдере, просто другая модель. Нужно только убедиться что баланс пополнен.

### Шаги

1. Зайди на https://fal.ai/dashboard → Billing.
2. Пополни на $10–20. Recraft v3 = $0.04 за картинку = ~3.6 ₽. $10 хватит на ~250 сказок.
3. Никаких новых ключей создавать не нужно — твой `FAL_KEY` подойдёт для всех моделей FAL.

В `.env` поменяю переменную:
```
IMAGE_MODEL=recraft-v3
```
(вместо `FAL_MODEL=fal-ai/flux/schnell`)

---

## 4. ЮKassa — без изменений

Старые ключи `YOOKASSA_*` оставляем как есть. Меняется только цена и payload — это всё в коде. Никакие новые токены провайдера у @BotFather запрашивать не нужно, тот же `YOOKASSA_PROVIDER_TOKEN` обслужит и 99 ₽, и 999 ₽, и 1485 ₽.

---

## Что класть в `.env`

Открой файл `.env` в корне проекта и добавь/измени эти строки:

```bash
# ── НОВОЕ: премиум-стек ──────────────────────────────────────

# TTS — переключаем на Azure
TTS_PROVIDER=azure
AZURE_SPEECH_KEY=твой_ключ_из_шага_1
AZURE_SPEECH_REGION=westeurope
AZURE_TTS_VOICE=ru-RU-SvetlanaNeural
AZURE_TTS_STYLE=affectionate
AZURE_TTS_RATE=-8%

# LLM — переключаем на Claude
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-api03-твой_ключ_из_шага_2
ANTHROPIC_MODEL=claude-sonnet-4-6

# Картинки — Recraft v3 вместо Flux Schnell
IMAGE_MODEL=recraft-v3

# ── НОВЫЕ ТАРИФЫ ─────────────────────────────────────────────

# Одна сказка разово: 99 ₽
PRICE_SINGLE_KOPECKS=9900
# Пакет 15 сказок (одна в день): 999 ₽ (−34%)
PRICE_PACK_KOPECKS=99900
PACK_STORIES_COUNT=15
# Подписка на месяц (одна в день): 1485 ₽ (−50%)
PRICE_MONTHLY_KOPECKS=148500

# Free trial: одна бесплатная сказка
FREE_STORY_LIMIT=1
```

**Старые переменные оставляем как fallback (не удаляем):**
- `YANDEX_API_KEY`, `YANDEX_FOLDER_ID` — если Azure упадёт, бот автоматом переключится
- `ELEVENLABS_API_KEY` — last-resort fallback
- `GEMINI_API_KEY` — если хочешь по-быстрому переключить LLM обратно на дешёвый Gemini (`LLM_PROVIDER=gemini`)
- `FAL_KEY` — общий ключ FAL, нужен для Recraft и для fallback на Flux Schnell

**Эти можно удалить или заменить:**
- `PRICE_SUB_KOPECKS=49000` → больше не используется, удаляй
- `PRICE_GIFT_KOPECKS=19900` → можешь оставить если хочешь сохранить функцию подарка (`/gift`), иначе удаляй
- `FAL_MODEL=fal-ai/flux/schnell` → заменяется на `IMAGE_MODEL=recraft-v3`

---

## Чек-лист «всё готово»

- [ ] Azure portal: создан Speech ресурс, скопированы KEY 1 и Region
- [ ] console.anthropic.com: создан API ключ, баланс ≥ $20
- [ ] fal.ai/dashboard: баланс ≥ $10
- [ ] `.env` обновлён со всеми новыми переменными
- [ ] Старые `YANDEX_*`, `ELEVENLABS_*`, `GEMINI_*`, `FAL_KEY` оставлены (как fallback)
- [ ] Бот перезапущен (`docker compose restart bot`)
- [ ] Сделана тестовая сказка `/start` → проверь что голос Azure (не Yandex) и обложка в стиле «детская книжка»

---

## Если что-то пошло не так

| Симптом | Что проверить |
|---|---|
| Бот молчит на «Сделать сказку» | Логи: `docker compose logs bot --tail=200`. Скорее всего ANTHROPIC_API_KEY невалидный или баланс 0. |
| Голос как у Алёны (Яндекс) | Не подхватился `TTS_PROVIDER=azure`. Перезапусти контейнер. Или Azure отвалился — смотри логи `Azure TTS error`. |
| Обложка как раньше (Flux) | Не подхватился `IMAGE_MODEL=recraft-v3`. Или FAL_KEY невалидный → откатывается на Flux. |
| Платёж не проходит | Проверь что в `YOOKASSA_PROVIDER_TOKEN` стоит LIVE токен, не TEST. |
| Долгая генерация (>40 сек) | Azure region далеко (US?) или ANTHROPIC_MODEL = Opus. Проверь регион и модель в `.env`. |

---

Когда все ключи получены — пингуй меня в чате «ключи готовы», я прогоню smoke-test и пройдёмся по первой сказке вместе.
