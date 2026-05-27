# Деплой: Admin dashboard + Партнёрка с audit ledger

Что добавлено за это обновление:

- **Мини-дашборд `/stats`** — юзеры, выручка, конверсии, источники, pending-комиссии
- **Партнёрская система** — каждый клик `?start=pati` навсегда привязывает юзера к партнёру; при каждой его оплате (включая рекурренты) автоматически создаётся запись комиссии
- **Immutable ledger** — каждая комиссия = отдельная строка с уникальным `payment_id`, не редактируется кроме одной операции «выплата»
- **Self-service для партнёра** — `/partner_login`, `/my_stats`, `/my_payments`, `/my_link`
- **UTM-трекинг** — `?start=mama_baby` сохраняется в `user.utm_source`, видно в `/stats`
- **CSV-экспорт** — `/export_csv commissions` отдаёт файл, подходящий для аудита и бухгалтерии

---

## Что менялось в коде

| Файл | Что |
|---|---|
| `src/db/models.py` | + Partner, PartnerCommission; + поля `partner_id` и `utm_source` на User |
| `src/db/__init__.py` | экспорт новых моделей |
| `src/db/session.py` | INLINE_MIGRATIONS для прода |
| `src/services/partners.py` | **НОВЫЙ** — find/create/register_commission/attribute/mark_paid |
| `src/services/billing.py` | `process_successful_payment` теперь возвращает и Payment; `create_recurring_payment` тоже |
| `src/services/__init__.py` | экспорт новых функций |
| `src/handlers/start.py` | `_resolve_start_payload` — распознаёт partner-коды / ref_XX / просто UTM |
| `src/handlers/billing.py` | `on_paid` вызывает `attribute_payment_to_partner` |
| `src/handlers/admin.py` | **НОВЫЙ** — `/stats /partners /partner_add /partner_stats /partner_payout /partner_link /export_csv` |
| `src/handlers/partner.py` | **НОВЫЙ** — `/partner_login /my_stats /my_payments /my_link` |
| `src/handlers/__init__.py` | добавлены роутеры admin + partner |
| `src/main.py` | renewal_worker вызывает `attribute_payment_to_partner` |

---

## Деплой

### 1. Загрузить новый код на сервер

С локальной машины (если SSH-бан прошёл):

```bash
rsync -avz --delete --exclude='__pycache__' --exclude='.DS_Store' \
  ~/Downloads/skazka-bot/src/ \
  skazka@77.110.119.32:/home/skazka/skazka-bot/src/
```

Или через git push/pull если завёл репо.

### 2. Пересобрать и поднять

На сервере под `skazka`:

```bash
cd ~/skazka-bot
docker compose up -d --build bot
docker compose logs -f bot --tail 50
```

В логах ждём:
```
Inline migration ... ALTER TABLE users ADD COLUMN IF NOT EXISTS partner_id INTEGER
...
Bot starting…
```

Если какая-то миграция вернёт ошибку «column already exists» — это нормально (миграции идемпотентные, мы их перепроверяем).

### 3. Проверить дашборд

В Telegram открой бот, отправь:

```
/stats
```

Если выдало сводку — всё работает. Если выдало «команда не найдена» — твой `ADMIN_IDS` в .env не содержит твой telegram_id. Проверь:

```bash
docker compose exec bot cat /app/.env | grep ADMIN_IDS
```

Должна быть строка `ADMIN_IDS=12345678` (твой telegram_id). Если пусто/неверно — поправь .env и перезапусти `docker compose up -d`.

---

## Создать партнёра Pati

В TG, под админом:

```
/partner_add pati Pati_Instagram 30 50
```

Бот ответит:
- ссылку для размещения: `https://t.me/dream_skazka_bot?start=pati`
- секретный токен партнёра (примерно 32 символа)

**Этот токен НИКОМУ не отправляй, кроме самого партнёра.** В письме на её рекламную почту:

> Привет! Ваш партнёрский доступ к боту «Сказка»:
>
> 1. Откройте бота: https://t.me/dream_skazka_bot
> 2. Авторизуйтесь: `/partner_login <ВАШТОКЕН>`
> 3. После этого вам доступны: `/my_stats`, `/my_payments`, `/my_link`
>
> Каждая оплата от вашей аудитории автоматически зачисляется на ваш счёт, вы видите все цифры в реальном времени и сами проверяете точность по `payment_id` каждой операции.
>
> Ваша ссылка для размещения:
> `https://t.me/dream_skazka_bot?start=pati`

---

## Как партнёр проверяет, что её не обманывают

Это самое важное.

1. **`/my_payments`** — она видит каждую оплату отдельной строкой:
   ```
   ⏳ 18.05 10:34 · pid=42 · 490₽ × 30% = 147₽
   ⏳ 18.05 12:11 · pid=43 · 490₽ × 30% = 147₽
   ✅ 17.05 22:01 · pid=41 · 490₽ × 30% = 147₽   ← уже выплачено
   ```
   `pid` = `Payment.id` в нашей БД. Уникальный референс конкретной транзакции.

2. **Снимаем нагрузку с её доверия** — формула шеринга открытая (`30%` сохранён в `share_pct_snapshot` на момент создания каждой строки) и не меняется задним числом.

3. **Бот сам ей пишет** каждый раз, когда юзер из её аудитории оплатил:
   > 💰 Новая комиссия: 147 ₽
   > (30% от 490 ₽)
   >
   > К выплате накопилось: 1 470 ₽
   > Подробная история: /my_payments

4. **Прозрачность аудита** — она может в любой момент написать тебе:
   > Денис, пришли мне выгрузку моих операций
   
   Ты делаешь `/export_csv commissions`, фильтруешь по `partner_id`, отправляешь. Все строки сходятся с тем, что она видит в `/my_payments`. Никакой возможности «занизить» — каждая строка появляется автоматически в момент оплаты.

5. **Выплаты подтверждаются обеими сторонами**:
   ```
   /partner_payout pati СБП TX-001-from-Tinkoff
   ```
   После этого бот уведомляет её:
   > 💵 Вам выплачено 1 470 ₽ (10 операций)
   > Способ: СБП
   > Референс: TX-001-from-Tinkoff
   >
   > Все детали в /my_payments — там строки уже помечены ✅

   `paid_out_reference` = твой реальный id транзакции из выписки. Партнёр может cross-check.

---

## Чек-лист сценария «партнёр Pati начала работать»

1. ✅ Ты: `/partner_add pati Pati_Instagram 30 50` — получаешь токен
2. ✅ Ты: пишешь Pati на email с токеном и инструкцией
3. ✅ Pati: открывает бот, делает `/partner_login <токен>` — связывает свой Telegram
4. ✅ Pati: получает `/my_link`, делает Reel
5. ✅ Юзер X кликает её ссылку, попадает в бот → `user.partner_id = pati.id`
6. ✅ Юзер X оплатил подписку 490 ₽
7. ✅ Авто: `Payment(amount=49000)` создан → `PartnerCommission(commission=14700, share_pct=30)` создан → Pati получает DM: «💰 +147 ₽»
8. ✅ Pati делает `/my_payments` — видит свою строку с `pid=...`
9. ✅ Через месяц у того же юзера прошёл рекуррент 490 ₽
10. ✅ Авто: ещё один Payment + ещё один PartnerCommission (147 ₽ снова) — Pati опять получает DM
11. ✅ Когда у Pati набралось, например, 5 000 ₽ pending — ты переводишь СБП, потом `/partner_payout pati СБП <реф_из_тинькофф>` — все её строки помечены ✅, Pati уведомлена

---

## Команды-шпаргалка

### Для тебя (админа)

| Команда | Что делает |
|---|---|
| `/stats` | Мини-дашборд: юзеры, выручка, конверсии, источники, pending-комиссии |
| `/partners` | Список всех партнёров |
| `/partner_add CODE NAME [SHARE%] [DISCOUNT%]` | Создать партнёра, получить токен и ссылку |
| `/partner_stats CODE` | Детальная сводка по конкретному партнёру |
| `/partner_link CODE` | Получить deep-link партнёра |
| `/partner_payout CODE МЕТОД [REF]` | Пометить все pending как выплаченные |
| `/export_csv users\|payments\|commissions` | CSV-файл для аудита |

### Для партнёра

| Команда | Что делает |
|---|---|
| `/partner_login <token>` | Первичная авторизация |
| `/my_stats` | Сводка своих метрик |
| `/my_payments` | Ledger всех операций |
| `/my_link` | Своя deep-link для размещения |

---

## Что использовать для UTM (без партнёрки)

Для платных размещений в каналах (не партнёрка, а просто посев):

- Канал «Мама и Малыш»: ссылка `t.me/dream_skazka_bot?start=mama_baby`
- Тестовый канал: `t.me/dream_skazka_bot?start=mama_test`
- Любая другая метка: `t.me/dream_skazka_bot?start=<что_угодно>` — попадёт в `user.utm_source`, видно в `/stats` → 📈 Источники

---

## Что НЕ покрыли (возможные улучшения)

- Веб-дашборд (HTML страница с графиками). Если захочешь — сделаем артефакт или мини FastAPI с метриками
- Автовыплаты через ЮKassa Payouts API — пока выплачиваешь руками, помечаешь `/partner_payout`
- Кампании партнёров — лимит по сроку (например, шеринг только первые 6 мес каждого юзера) — сейчас pure lifetime
- Многоуровневая партнёрка (партнёр приводит партнёра) — пока нет, не нужно для MVP
- Подписи commission_signature — для совсем параноидальной криптографической верификации
