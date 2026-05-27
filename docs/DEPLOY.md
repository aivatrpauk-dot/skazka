# Деплой на VPS (Selectel / timeweb / Reg.ru cloud)

Этот гайд проводит по полному циклу за 60–90 минут. Дальше — бот работает сам.

## 0. Что нужно собрать перед деплоем

| Что | Где взять | Сколько ждать |
|---|---|---|
| Бот-токен | @BotFather → /newbot | 30 секунд |
| ЮKassa shop_id + secret_key + provider_token | Личный кабинет ЮKassa → Интеграция | до 1 рабочего дня (модерация) |
| Gemini API key | https://aistudio.google.com/apikey | 1 минута, бесплатный тариф |
| ElevenLabs API key | https://elevenlabs.io → Profile → API Keys | 1 минута, нужен платный план Creator $22/мес для нормальных объёмов |
| FAL API key | https://fal.ai/dashboard/keys | 1 минута, $5 free credits |
| Домен (по желанию) | reg.ru / nic.ru | 5 минут |

## 1. VPS

### Selectel Cloud

1. https://my.selectel.ru → Облачная платформа → Создать сервер
2. Конфиг: 2 vCPU, 2 ГБ RAM, 40 ГБ NVMe, Ubuntu 24.04 LTS — около 350 ₽/мес
3. Регион — Москва или Санкт-Петербург (Telegram отвечает быстрее из РФ-зоны)
4. SSH-ключ — добавь свой публичный ключ. Если нет — `ssh-keygen -t ed25519`
5. Создай → получаешь IP

### timeweb cloud (альтернатива)

То же самое в https://timeweb.cloud, тариф Cloud-S от 270 ₽/мес.

## 2. Первоначальная настройка сервера

```bash
ssh root@<IP>

# обновление и базовые тулзы
apt update && apt upgrade -y
apt install -y curl git ufw fail2ban

# фаервол
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw --force enable

# Docker + Compose plugin
curl -fsSL https://get.docker.com | sh
docker --version
docker compose version

# Пользователь под бота (не сидим под рутом)
useradd -m -s /bin/bash -G docker skazka
mkdir -p /home/skazka/.ssh
cp ~/.ssh/authorized_keys /home/skazka/.ssh/
chown -R skazka:skazka /home/skazka/.ssh
chmod 700 /home/skazka/.ssh && chmod 600 /home/skazka/.ssh/authorized_keys
```

## 3. Деплой кода

```bash
su - skazka
git clone <твой-репо> skazka-bot
cd skazka-bot
cp .env.example .env
nano .env   # заполни все ключи
docker compose up -d --build
docker compose logs -f bot
```

Должно появиться `Bot starting…`. Идёшь в Telegram, открываешь бота, жмёшь /start.

## 4. ЮKassa

1. Зарегистрируйся на https://yookassa.ru как ИП. Понадобятся: ИНН, ОГРНИП, расчётный счёт.
2. Модерация — до 1 рабочего дня.
3. После модерации: Личный кабинет → **Интеграция → API-ключи** — получишь `shop_id` и `secret_key`. Положи их в `.env`.
4. Подключи **Telegram Payments**: @BotFather → /mybots → выбери бота → Payments → ЮKassa → подключить. Получаешь `provider_token` (для тестовой и для боевой среды). Положи боевой в `.env` как `YOOKASSA_PROVIDER_TOKEN`.
5. В кабинете ЮKassa: **Настройки → Способы оплаты** — включи СБП, карты Мир/Visa/MC, ЮMoney. СБП обязательно — конверсия в РФ выше.
6. **Настройки → 54-ФЗ** — выбери способ выбивания чеков (если ИП на УСН, обычно «ЮKassa сама пробивает онлайн-чеки»). Платный модуль ~3 000 ₽/мес — окупается с первой сотни платящих.

## 5. Метаданные бота

**Автоматически при каждом старте бота** (заданы в `src/bot_setup.py`):
- display name = "Сказка"
- short description (about)
- description (длинное приветствие до /start)
- список команд в меню "/"

Хочешь поменять — правишь `src/bot_setup.py` и делаешь `docker compose restart bot`. Идти в BotFather не нужно.

**Только руками в @BotFather** (API это не умеет):
- `/setuserpic` — аватарка бота (PNG 512×512, можешь сгенерить через FAL/Midjourney или попросить дизайнера)

## 6. Мониторинг (10 минут)

### UptimeRobot
1. https://uptimerobot.com — регистрация, бесплатный тариф
2. Add monitor → HTTP(s) → если бот без вебхука, мониторь сам сервер (поставь lightweight healthcheck на 80 порту)
3. Уведомления — на email или Telegram

### Sentry
1. https://sentry.io — Free tier до 5k событий/мес
2. Создай проект Python, скопируй DSN в `.env` → `SENTRY_DSN=...`
3. Перезапусти бот — `docker compose restart bot`

### Бэкапы
Уже включены в `docker-compose.yml` — раз в 6 часов делается `pg_dump` в `./backups`. Раз в неделю руками заливай дамп на S3 timeweb (100 ₽/мес) или на собственный Yandex Object Storage.

## 7. Лендинг (опционально, но желательно)

Файл `landing/index.html` — однофайловый, можно развернуть как угодно:

**Вариант А: GitHub Pages**
```
git init landing && cd landing
cp ../landing/index.html .
git add . && git commit -m "init"
git push <твой-репо>
# Settings → Pages → main / root
```

**Вариант Б: Vercel (быстрее, бесплатно)**
```
npm i -g vercel
cd landing && vercel --prod
```

**Вариант В: Selectel S3 + cloudflare** — самый дешёвый под нагрузкой. Не критично на старте.

В лендинге замени `{BOT_USERNAME}` (find-and-replace) на реальный handle бота, например `https://t.me/dream_skazka_bot`.

## 8. Безопасность

- `.env` — никогда не коммитим. Уже в .gitignore.
- Доступ к ЮKassa API-ключам — только с твоего VPS, в боевом кабинете включи IP-фильтр.
- В Telegram-сессии бота не храним ничего лишнего (используется MemoryStorage). После рестарта — FSM сбрасывается. Если нужно сохранять состояние — поменяй на RedisStorage.
- Раз в неделю меняй SSH-пароль root и проверяй `last -a | head` на чужие логины.

## 9. Сколько это стоит ежемесячно

| Статья | ₽/мес |
|---|---|
| VPS 2/2/40 | 350 |
| S3 для бэкапов | 100 |
| ElevenLabs Creator $22 (если есть платящие) | ~2 200 |
| Gemini (≤500 сказок) | ~100 |
| FAL (≤500 картинок) | ~150 |
| Домен | ~30 (амортизация года) |
| **Итого фикс** | **~2 950 ₽** |

Плюс ~4,27% от выручки на эквайринг ЮKassa и 6% УСН с доходов.

## 10. Что делать если что-то сломалось

```bash
# логи бота
docker compose logs -f bot --tail 200

# перезапуск
docker compose restart bot

# полный рестарт (БД остаётся)
docker compose down && docker compose up -d

# восстановление из бэкапа
zcat backups/skazka_YYYYMMDD_HHMM.sql.gz | docker compose exec -T db psql -U skazka skazka
```

Если бот не отвечает дольше 5 минут — UptimeRobot пришлёт алерт. Если сыпятся ошибки — увидишь в Sentry.

Деплой готов. Иди в [LAUNCH_CHECKLIST.md](LAUNCH_CHECKLIST.md) за пошаговым планом выходных.
