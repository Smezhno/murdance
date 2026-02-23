# RFC-002: Миграция архитектуры — отказ от Redis + VPN-прокси для Telegram

**Проект:** DanceBot Agent
**Версия:** 0.1.0
**Дата:** 23 февраля 2026
**Автор:** Александр (при участии Claude)
**Статус:** Draft — требует ревью перед началом работ
**Зависимости:** CONTRACT.md v1.2, RFC_REFERENCE.md v0.4.0

---

## 1. Мотивация

### 1.1 Проблема: Redis — оверкилл для MVP

Текущая архитектура использует Redis для 6 категорий данных:

| Категория | Текущее решение (Redis) | Проблема |
|-----------|------------------------|----------|
| Сессии (FSM state + slots) | `SET session:{chat_id}` TTL 24h | Нет SQL-дебага, нет истории переходов |
| Идемпотентность | `SETNX idempotency:{hash}` TTL 10min | Нет аудита, теряется при рестарте Redis |
| CRM-кеш | `SET impulse:cache:*` TTL 5min | Инвалидация через SCAN — хрупко |
| Очереди (outbound, fallback) | `LPUSH/BRPOP` Redis lists | Нет DLQ без кода, нет retry-счётчика |
| Budget Guard | `INCRBY budget:*` с TTL | 4 разных ключа с разными TTL, дебаг только через redis-cli |
| Inbound dedup | `SETNX seen:{channel}:{msg_id}` TTL 5min | Ок, но тривиально на PG |

**Масштаб MVP:** 1 студия, ~50-100 диалогов/день, ~5-20 бронирований/день.

**Реальная нагрузка на Redis:** <1 RPS. Redis спроектирован для 100k+ RPS.

**Боль при разработке:**
- Дебаг FSM: `redis-cli GET session:12345` → JSON blob, нет истории переходов
- Дебаг идемпотентности: lock исчез по TTL — не понять, был ли он
- Два хранилища = два бэкапа, два мониторинга, два recovery-плана
- Session recovery при рестарте (CONTRACT §20) — парсинг всех Redis ключей через SCAN

### 1.2 Проблема: Telegram заблокирован в России

Сервер в Yandex Cloud (Россия) не может напрямую отправлять запросы к `api.telegram.org`. Необходим VPN/прокси для исходящих HTTP-запросов к Telegram API.

**Доступные ресурсы:** 2 VPN-сервера (за пределами РФ).

**Требования:**
- Failover: если VPN-1 недоступен → автоматически переключиться на VPN-2
- Входящие вебхуки: Telegram → Caddy работает напрямую (Telegram обходит блокировку сам при доставке webhooks, но это надо проверить — см. OQ-9)
- Минимальная задержка: прокси не должен добавлять >200ms к latency

---

## 2. Решение

### 2.1 Полный отказ от Redis

**Docker Compose: 5 контейнеров → 4 контейнера**

```
БЫЛО:  app + worker + redis + postgres + caddy
СТАЛО: app + worker + postgres + caddy
```

Все данные мигрируют в PostgreSQL:

| Категория | Новое решение (PostgreSQL) | Реализация |
|-----------|---------------------------|------------|
| Сессии | Таблица `sessions` | JSONB для slots, enum для state, `updated_at` для timeout |
| Идемпотентность | Таблица `idempotency_locks` | UNIQUE constraint + `created_at` для аудита |
| CRM-кеш | Таблица `crm_cache` | `key`, `value` JSONB, `expires_at` timestamp |
| Очереди | Таблица `outbound_queue` | `status` enum (pending/processing/sent/failed/dlq), `retry_count` |
| Budget Guard | Таблица `budget_counters` | Один ряд на (metric, time_window), `SELECT SUM` |
| Inbound dedup | Таблица `seen_messages` | UNIQUE на `(channel, message_id)`, автоочистка |

### 2.2 VPN-прокси для исходящих запросов к Telegram

**Подход:** SOCKS5/HTTPS прокси с failover на уровне httpx-клиента.

```
app/worker → httpx → SOCKS5 proxy VPN-1 → api.telegram.org
                   ↓ (если VPN-1 таймаут)
                   → SOCKS5 proxy VPN-2 → api.telegram.org
```

---

## 3. Детальный дизайн: PostgreSQL вместо Redis

### 3.1 Новые таблицы

```sql
-- ============================================================
-- SESSIONS: FSM state + slots + conversation history
-- ============================================================
CREATE TABLE sessions (
    chat_id     TEXT PRIMARY KEY,
    channel     TEXT NOT NULL DEFAULT 'telegram',  -- 'telegram', 'whatsapp'
    state       TEXT NOT NULL DEFAULT 'idle',       -- ConversationState enum value
    slots       JSONB NOT NULL DEFAULT '{}',        -- SlotValues as JSON
    history     JSONB NOT NULL DEFAULT '[]',        -- Last N messages for LLM context
    metadata    JSONB NOT NULL DEFAULT '{}',        -- User profile, preferences
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Индекс для session recovery (CONTRACT §20): найти "зависшие" сессии
CREATE INDEX idx_sessions_state_updated ON sessions (state, updated_at)
    WHERE state NOT IN ('idle');

-- ============================================================
-- IDEMPOTENCY LOCKS: booking dedup
-- ============================================================
CREATE TABLE idempotency_locks (
    fingerprint TEXT PRIMARY KEY,  -- sha256(phone + schedule_id)
    chat_id     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Автоочистка: locks старше 10 минут не блокируют, но остаются для аудита
-- Приложение проверяет: WHERE fingerprint = $1 AND created_at > now() - interval '10 minutes'

-- ============================================================
-- CRM CACHE: schedule, groups, teachers
-- ============================================================
CREATE TABLE crm_cache (
    cache_key   TEXT PRIMARY KEY,
    value       JSONB NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_crm_cache_expires ON crm_cache (expires_at);

-- ============================================================
-- OUTBOUND QUEUE: все исходящие сообщения (CONTRACT §9)
-- ============================================================
CREATE TYPE outbound_status AS ENUM (
    'pending',      -- Ожидает отправки
    'processing',   -- Worker взял в работу
    'sent',         -- Успешно отправлено
    'failed',       -- Ошибка, будет retry
    'dlq'           -- Dead letter — ручная обработка
);

CREATE TABLE outbound_queue (
    id              BIGSERIAL PRIMARY KEY,
    chat_id         TEXT NOT NULL,
    channel         TEXT NOT NULL,
    message_type    TEXT NOT NULL DEFAULT 'text',  -- 'text', 'buttons', 'typing'
    payload         JSONB NOT NULL,                 -- {text, buttons, ...}
    status          outbound_status NOT NULL DEFAULT 'pending',
    priority        INT NOT NULL DEFAULT 0,         -- Higher = sooner
    retry_count     INT NOT NULL DEFAULT 0,
    max_retries     INT NOT NULL DEFAULT 3,
    error_message   TEXT,
    trace_id        UUID,
    scheduled_at    TIMESTAMPTZ NOT NULL DEFAULT now(),  -- Для reminders: будущее время
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Worker poll: SELECT ... WHERE status = 'pending' AND scheduled_at <= now()
-- ORDER BY priority DESC, created_at ASC FOR UPDATE SKIP LOCKED
CREATE INDEX idx_outbound_pending ON outbound_queue (status, scheduled_at, priority DESC)
    WHERE status = 'pending';

-- ============================================================
-- BUDGET COUNTERS: LLM usage tracking
-- ============================================================
CREATE TABLE budget_counters (
    metric      TEXT NOT NULL,      -- 'tokens_per_hour', 'cost_per_day', 'requests_per_minute', 'errors_per_hour'
    window_key  TEXT NOT NULL,      -- '2026-02-23T14:00Z' (hour), '2026-02-23' (day), etc.
    value       NUMERIC NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (metric, window_key)
);

-- ============================================================
-- SEEN MESSAGES: inbound deduplication
-- ============================================================
CREATE TABLE seen_messages (
    channel     TEXT NOT NULL,
    message_id  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (channel, message_id)
);

-- ============================================================
-- ПЕРИОДИЧЕСКАЯ ОЧИСТКА (cron или pg_cron)
-- ============================================================
-- Запускать каждый час:
-- DELETE FROM idempotency_locks WHERE created_at < now() - interval '24 hours';
-- DELETE FROM crm_cache WHERE expires_at < now();
-- DELETE FROM seen_messages WHERE created_at < now() - interval '1 hour';
-- DELETE FROM outbound_queue WHERE status IN ('sent', 'dlq') AND created_at < now() - interval '7 days';
-- DELETE FROM budget_counters WHERE created_at < now() - interval '7 days';
```

### 3.2 Ключевые паттерны реализации

#### 3.2.1 Сессии: замена Redis GET/SET

```python
# БЫЛО (Redis):
session_data = await redis.get_json(f"session:{chat_id}")
await redis.set_json(f"session:{chat_id}", session_data, ex=86400)

# СТАЛО (PostgreSQL):
async def get_session(chat_id: str) -> Session | None:
    """Получить сессию. Если updated_at > 24h — вернуть None (expired)."""
    row = await db.fetchrow(
        """SELECT state, slots, history, metadata, updated_at
           FROM sessions WHERE chat_id = $1
           AND updated_at > now() - interval '24 hours'""",
        chat_id,
    )
    if not row:
        return None
    return Session(**row)

async def save_session(chat_id: str, session: Session) -> None:
    """Upsert сессии."""
    await db.execute(
        """INSERT INTO sessions (chat_id, channel, state, slots, history, metadata, updated_at)
           VALUES ($1, $2, $3, $4, $5, $6, now())
           ON CONFLICT (chat_id) DO UPDATE SET
             state = EXCLUDED.state,
             slots = EXCLUDED.slots,
             history = EXCLUDED.history,
             metadata = EXCLUDED.metadata,
             updated_at = now()""",
        chat_id, session.channel, session.state,
        json.dumps(session.slots), json.dumps(session.history),
        json.dumps(session.metadata),
    )
```

#### 3.2.2 Идемпотентность: замена Redis SETNX

```python
# БЫЛО (Redis):
is_new = await redis.setnx(f"idempotency:{fingerprint}", "1", ex=600)

# СТАЛО (PostgreSQL):
async def acquire_booking_lock(fingerprint: str, chat_id: str) -> bool:
    """Атомарный lock. True = новая запись, False = дубликат."""
    try:
        await db.execute(
            """INSERT INTO idempotency_locks (fingerprint, chat_id)
               VALUES ($1, $2)""",
            fingerprint, chat_id,
        )
        return True  # Новая запись
    except asyncpg.UniqueViolationError:
        # Проверяем: lock ещё активен (< 10 минут)?
        row = await db.fetchrow(
            """SELECT created_at FROM idempotency_locks
               WHERE fingerprint = $1
               AND created_at > now() - interval '10 minutes'""",
            fingerprint,
        )
        return row is None  # None = lock истёк, можно повторить
```

#### 3.2.3 Outbound Queue: замена Redis lists

```python
# БЫЛО (Redis):
await redis.lpush("outbound:queue", json.dumps(message))
# Worker:
raw = await redis.rpop("outbound:queue", timeout=5)

# СТАЛО (PostgreSQL):
async def enqueue_message(chat_id: str, channel: str, text: str,
                          trace_id: UUID, scheduled_at: datetime | None = None) -> int:
    """Поставить сообщение в очередь. Возвращает ID."""
    row = await db.fetchrow(
        """INSERT INTO outbound_queue (chat_id, channel, payload, trace_id, scheduled_at)
           VALUES ($1, $2, $3, $4, COALESCE($5, now()))
           RETURNING id""",
        chat_id, channel, json.dumps({"text": text}), trace_id, scheduled_at,
    )
    return row["id"]

# Worker (polling с SKIP LOCKED — безопасно для конкуренции):
async def poll_messages(batch_size: int = 10) -> list[OutboundMessage]:
    """Взять batch сообщений для отправки."""
    rows = await db.fetch(
        """UPDATE outbound_queue
           SET status = 'processing', updated_at = now()
           WHERE id IN (
               SELECT id FROM outbound_queue
               WHERE status = 'pending' AND scheduled_at <= now()
               ORDER BY priority DESC, created_at ASC
               FOR UPDATE SKIP LOCKED
               LIMIT $1
           )
           RETURNING *""",
        batch_size,
    )
    return [OutboundMessage(**row) for row in rows]
```

#### 3.2.4 Budget Guard: замена Redis INCRBY

```python
# БЫЛО (Redis):
new_count = await redis.incr(f"budget:tokens:hour:{hour_key}", tokens)

# СТАЛО (PostgreSQL):
async def increment_budget(metric: str, window_key: str, amount: float) -> float:
    """Атомарный инкремент. Возвращает новое значение."""
    row = await db.fetchrow(
        """INSERT INTO budget_counters (metric, window_key, value)
           VALUES ($1, $2, $3)
           ON CONFLICT (metric, window_key) DO UPDATE
             SET value = budget_counters.value + EXCLUDED.value
           RETURNING value""",
        metric, window_key, amount,
    )
    return float(row["value"])

async def check_budget(metric: str, window_key: str, limit: float) -> tuple[bool, float]:
    """Проверить лимит. Возвращает (within_limit, current_value)."""
    row = await db.fetchrow(
        """SELECT COALESCE(value, 0) as value FROM budget_counters
           WHERE metric = $1 AND window_key = $2""",
        metric, window_key,
    )
    current = float(row["value"]) if row else 0.0
    return current < limit, current
```

#### 3.2.5 CRM Cache: замена Redis SET с TTL

```python
# БЫЛО (Redis):
await redis.set_json(f"impulse:cache:schedule:{date}", data, ex=300)
cached = await redis.get_json(f"impulse:cache:schedule:{date}")

# СТАЛО (PostgreSQL):
async def cache_set(key: str, value: dict, ttl_seconds: int) -> None:
    """Записать в кеш с TTL."""
    await db.execute(
        """INSERT INTO crm_cache (cache_key, value, expires_at)
           VALUES ($1, $2, now() + make_interval(secs => $3))
           ON CONFLICT (cache_key) DO UPDATE SET
             value = EXCLUDED.value,
             expires_at = EXCLUDED.expires_at""",
        key, json.dumps(value), ttl_seconds,
    )

async def cache_get(key: str) -> dict | None:
    """Получить из кеша. None если истёк или не найден."""
    row = await db.fetchrow(
        """SELECT value FROM crm_cache
           WHERE cache_key = $1 AND expires_at > now()""",
        key,
    )
    return json.loads(row["value"]) if row else None
```

#### 3.2.6 Session Recovery (CONTRACT §20): замена Redis SCAN

```python
# БЫЛО (Redis): SCAN session:* → parse JSON → check state
# Хрупко: SCAN может пропустить ключи, нет фильтрации по state

# СТАЛО (PostgreSQL): один SQL-запрос
async def recover_stale_sessions() -> dict[str, int]:
    """Запуск при старте app. Возвращает счётчики."""
    stats = {"booking_rescued": 0, "expired_reset": 0, "admin_timeout": 0}

    # 1. BOOKING_IN_PROGRESS > 1 min → fallback + notify
    stuck = await db.fetch(
        """UPDATE sessions SET state = 'idle', updated_at = now()
           WHERE state = 'booking_in_progress'
             AND updated_at < now() - interval '1 minute'
           RETURNING chat_id, slots"""
    )
    for row in stuck:
        await enqueue_message(row["chat_id"], "telegram",
            "Извините, произошёл сбой при записи. Администратор свяжется с вами.",
            trace_id=uuid4())
        # + enqueue в fallback для админа
    stats["booking_rescued"] = len(stuck)

    # 2. Any state > 24h → IDLE
    expired = await db.execute(
        """UPDATE sessions SET state = 'idle', slots = '{}', updated_at = now()
           WHERE state != 'idle'
             AND updated_at < now() - interval '24 hours'"""
    )
    stats["expired_reset"] = int(expired.split()[-1])  # "UPDATE N"

    # 3. ADMIN_RESPONDING > 4h → notify + IDLE
    admin = await db.fetch(
        """UPDATE sessions SET state = 'idle', updated_at = now()
           WHERE state = 'admin_responding'
             AND updated_at < now() - interval '4 hours'
           RETURNING chat_id"""
    )
    for row in admin:
        await enqueue_message(row["chat_id"], "telegram",
            "Администратор ответит позже. Напишите снова, если нужна помощь.",
            trace_id=uuid4())
    stats["admin_timeout"] = len(admin)

    return stats
```

### 3.3 Коммуникация app ↔ worker

**Было:** Redis lists (LPUSH/BRPOP)

**Стало:** PostgreSQL + LISTEN/NOTIFY для real-time уведомлений

```python
# App: после INSERT в outbound_queue
await db.execute("NOTIFY outbound_new")

# Worker: слушает канал
async def worker_loop():
    conn = await asyncpg.connect(...)
    await conn.add_listener("outbound_new", on_notify)

    while True:
        messages = await poll_messages(batch_size=10)
        if messages:
            for msg in messages:
                await send_message(msg)
        else:
            # Если нет сообщений, ждём NOTIFY или таймаут 5 сек
            await asyncio.sleep(5)
```

**Преимущество:** LISTEN/NOTIFY — zero-polling при отсутствии сообщений, мгновенная реакция при появлении.

---

## 4. Детальный дизайн: VPN-прокси для Telegram

### 4.1 Архитектура

```
┌─────────────────────────────┐
│  Yandex Cloud VM            │
│                             │
│  app/worker                 │
│    ├─ httpx client          │
│    │   ├─ proxy: VPN-1 ────────→ api.telegram.org
│    │   └─ proxy: VPN-2 ────────→ api.telegram.org (fallback)
│    │                        │
│    ├─ CRM requests ─────────────→ impulsecrm.ru (напрямую)
│    └─ LLM requests ─────────────→ llm.api.cloud.yandex.net (напрямую)
│                             │
│  caddy ←────────────────────────── Telegram webhooks (входящие)
└─────────────────────────────┘
```

**Только Telegram API требует прокси.** CRM и LLM-провайдеры доступны напрямую.

### 4.2 Конфигурация

```python
# config.py — новые env vars
class Settings(BaseSettings):
    # ... existing ...

    # Telegram proxy (VPN servers)
    telegram_proxy_primary: str = ""    # "socks5://user:pass@vpn1.example.com:1080"
    telegram_proxy_fallback: str = ""   # "socks5://user:pass@vpn2.example.com:1080"
    telegram_proxy_timeout: int = 5     # секунд на connect через прокси
    telegram_proxy_health_interval: int = 60  # секунд между health-check прокси
```

### 4.3 Реализация: httpx с failover

```python
# app/channels/telegram_proxy.py
"""Telegram HTTP client с VPN failover."""

import httpx
import structlog

logger = structlog.get_logger()

class TelegramHttpClient:
    """httpx client с автоматическим failover между двумя VPN-прокси."""

    def __init__(self, primary_proxy: str, fallback_proxy: str, timeout: int = 5):
        self._proxies = [primary_proxy, fallback_proxy]
        self._active_index = 0  # 0 = primary, 1 = fallback
        self._timeout = timeout
        self._clients: list[httpx.AsyncClient] = []

    async def start(self) -> None:
        """Создать два httpx клиента с разными прокси."""
        for proxy_url in self._proxies:
            transport = httpx.AsyncHTTPTransport(
                proxy=proxy_url,
                retries=1,
            )
            client = httpx.AsyncClient(
                transport=transport,
                timeout=httpx.Timeout(self._timeout, connect=self._timeout),
            )
            self._clients.append(client)

    async def stop(self) -> None:
        for client in self._clients:
            await client.aclose()

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Запрос через активный прокси. При ошибке — failover на второй."""
        for attempt in range(2):  # Максимум 2 попытки (primary + fallback)
            idx = (self._active_index + attempt) % 2
            client = self._clients[idx]
            proxy_name = "primary" if idx == 0 else "fallback"

            try:
                response = await client.request(method, url, **kwargs)
                # Если успешно через fallback — запомнить его как активный
                if attempt > 0:
                    self._active_index = idx
                    logger.warning("telegram_proxy.failover",
                                   new_active=proxy_name)
                return response

            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError) as e:
                logger.error("telegram_proxy.error",
                             proxy=proxy_name, error=str(e))
                if attempt == 1:
                    raise  # Оба прокси недоступны

        raise RuntimeError("Both Telegram proxies failed")  # Unreachable

    async def health_check(self) -> dict[str, bool]:
        """Проверить доступность обоих прокси."""
        results = {}
        for idx, client in enumerate(self._clients):
            name = "primary" if idx == 0 else "fallback"
            try:
                # Лёгкий запрос к Telegram API
                r = await client.get("https://api.telegram.org/", timeout=3)
                results[name] = r.status_code < 500
            except Exception:
                results[name] = False
        return results
```

### 4.4 Интеграция в TelegramChannel

```python
# Изменения в app/channels/telegram.py

class TelegramChannel:
    def __init__(self, bot_token: str, proxy_client: TelegramHttpClient):
        self._token = bot_token
        self._client = proxy_client  # Вместо обычного httpx.AsyncClient
        self._base_url = f"https://api.telegram.org/bot{bot_token}"

    async def send_message(self, chat_id: str, text: str) -> bool:
        """Отправка через VPN-прокси с failover."""
        response = await self._client.request(
            "POST",
            f"{self._base_url}/sendMessage",
            json={"chat_id": chat_id, "text": text},
        )
        return response.status_code == 200
```

### 4.5 VPN-серверы: конфигурация

**VPN-1: XRay (VLESS)** — Primary proxy

XRay уже имеет встроенную поддержку SOCKS5 inbound. Не нужен отдельный софт.

Добавить в `/usr/local/etc/xray/config.json` дополнительный inbound:

```json
{
  "inbounds": [
    // ... существующий VLESS inbound ...
    {
      "tag": "socks-in",
      "port": 1080,
      "listen": "0.0.0.0",
      "protocol": "socks",
      "settings": {
        "auth": "password",
        "accounts": [
          {
            "user": "dancebot",
            "pass": "<STRONG_RANDOM_PASSWORD>"
          }
        ],
        "udp": false
      }
    }
  ],
  "outbounds": [
    {
      "tag": "direct",
      "protocol": "freedom"
    }
  ],
  "routing": {
    "rules": [
      {
        "type": "field",
        "inboundTag": ["socks-in"],
        "domain": ["api.telegram.org"],
        "outboundTag": "direct"
      },
      {
        "type": "field",
        "inboundTag": ["socks-in"],
        "outboundTag": "block"
      }
    ]
  }
}
```

**Ключевое:** routing разрешает через SOCKS только `api.telegram.org`. Всё остальное — block. Это защита от злоупотребления, если кто-то узнает пароль.

**VPN-2: AmneziaWG** — Fallback proxy

AmneziaWG — это VPN-туннель (L3), а не прокси. Варианты:

**Вариант A (рекомендуется): microsocks поверх AmneziaWG**

```bash
# На VPN-2 сервере:
# 1. Установить microsocks (минимальный SOCKS5 прокси, ~100 строк C)
apt install -y build-essential git
git clone https://github.com/rofl0r/microsocks.git
cd microsocks && make && cp microsocks /usr/local/bin/

# 2. Запустить как systemd сервис
cat > /etc/systemd/system/microsocks.service << 'EOF'
[Unit]
Description=MicroSOCKS proxy for DanceBot
After=network.target

[Service]
ExecStart=/usr/local/bin/microsocks -i 0.0.0.0 -p 1080 -u dancebot -P <STRONG_RANDOM_PASSWORD>
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl enable --now microsocks
```

**Вариант B: SSH tunnel (без установки софта)**

```bash
# На Yandex Cloud VM — постоянный SSH-туннель с SOCKS5 proxy:
# (autossh для автоматического реконнекта)
apt install -y autossh
autossh -M 0 -f -N -D 1081 -o "ServerAliveInterval=30" user@vpn2-ip
# httpx подключается к socks5://127.0.0.1:1081
```

Рекомендуется вариант A — проще, надёжнее, не зависит от SSH.

**Безопасность обоих серверов:**

```bash
# Firewall — разрешить SOCKS порт ТОЛЬКО с IP Yandex Cloud VM:
# На VPN-1 (XRay):
ufw allow from <YANDEX_VM_IP> to any port 1080
ufw deny 1080

# На VPN-2 (microsocks):
ufw allow from <YANDEX_VM_IP> to any port 1080
ufw deny 1080
```

- Аутентификация: SOCKS5 user/password (разные на каждом сервере)
- Routing: только `api.telegram.org` (XRay — на уровне конфига, microsocks — на уровне firewall outbound)
- Мониторинг: `journalctl -u xray` / `journalctl -u microsocks`

---

## 5. План миграции

### 5.1 Порядок (один модуль за раз)

| Шаг | Модуль | Что делаем | Риск |
|-----|--------|------------|------|
| 0 | **SQL миграции** | Создать все новые таблицы | Низкий |
| 1 | **storage/postgres.py** | Расширить PostgresStorage: add connection pool, helpers | Низкий |
| 2 | **VPN proxy** | Новый модуль `channels/telegram_proxy.py` | Средний — тестировать с реальным TG API |
| 3 | **Сессии** | Мигрировать `core/conversation.py` с Redis → PG | Высокий — затрагивает весь flow |
| 4 | **Идемпотентность** | Мигрировать `core/idempotency.py` | Низкий — изолированный модуль |
| 5 | **CRM кеш** | Мигрировать `integrations/impulse/cache.py` | Низкий |
| 6 | **Budget Guard** | Мигрировать `ai/budget_guard.py` | Низкий |
| 7 | **Inbound dedup** | Мигрировать `channels/dedup.py` | Низкий |
| 8 | **Outbound queue** | Новый модуль (Phase 3 — ранее не реализован) | Средний |
| 9 | **Worker** | Реализовать worker с PG polling + LISTEN/NOTIFY | Средний |
| 10 | **Удаление Redis** | Убрать контейнер, код, зависимости | Низкий |
| 11 | **Session recovery** | Реализовать CONTRACT §20 на PG | Низкий — SQL тривиальный |

### 5.2 Критерий готовности каждого шага

- Юнит-тесты проходят
- Prompt regression ≥ 90%
- `docker compose up` работает
- `/health` эндпоинт показывает всё зелёным

---

## 6. Изменения в CONTRACT.md и Cursor Rules

### 6.1 CONTRACT.md — секции для обновления

**§3 Architecture:**
```
БЫЛО: redis → Sessions, locks, cache, queues, budget counters
СТАЛО: postgres → All data (sessions, cache, queues, budget, logs, audit)

БЫЛО: 5 containers: app, worker, redis, postgres, caddy
СТАЛО: 4 containers: app, worker, postgres, caddy
```

**§4 Data ownership:**
```
БЫЛО: Conversation state + slots → Redis TTL 24h
СТАЛО: Conversation state + slots → PostgreSQL sessions table

БЫЛО: User profiles → Redis TTL 90d
СТАЛО: User profiles → PostgreSQL sessions.metadata

БЫЛО: CRM data → cached in Redis
СТАЛО: CRM data → cached in PostgreSQL crm_cache table
```

**§10 Idempotency:**
```
БЫЛО: Redis SETNX, TTL 10min
СТАЛО: PostgreSQL INSERT with UNIQUE constraint, app checks created_at < 10min
```

**Новая секция — §XX Telegram Proxy:**
```
All outbound HTTP requests to api.telegram.org MUST go through SOCKS5 proxy.
Two proxies configured: primary + fallback.
Failover: automatic, transparent to calling code.
Inbound webhooks: direct to Caddy (no proxy needed — verify with OQ-9).
```

### 6.2 Cursor Rules — обновления

```
БЫЛО: Redis (sessions, locks, cache, queues, budgets)
СТАЛО: PostgreSQL (all data — sessions, cache, queues, budgets, logs)

БЫЛО: Communication: app pushes to Redis list → worker consumes
СТАЛО: Communication: app inserts to outbound_queue → NOTIFY → worker polls

БЫЛО: Idempotency: sha256(phone + schedule_id), Redis SETNX
СТАЛО: Idempotency: sha256(phone + schedule_id), PostgreSQL UNIQUE constraint

УДАЛИТЬ: Redis lists are the queue
ДОБАВИТЬ: PostgreSQL + LISTEN/NOTIFY is the queue. FOR UPDATE SKIP LOCKED for concurrency.
ДОБАВИТЬ: Telegram API requests require SOCKS5 proxy (see config).
```

---

## 7. Что НЕ меняется

- FSM логика (13 состояний, переходы) — без изменений
- Policy Enforcer — без изменений
- LLM router — без изменений
- CRM Impulse adapter (кроме cache layer) — без изменений
- Knowledge Base — без изменений
- Conversation rules — без изменений
- Webhook signature verification — без изменений
- Prompt regression tests — без изменений

---

## 8. Риски и митигация

| Риск | P | Митигация |
|------|---|-----------|
| PG latency выше Redis для сессий | 🟢 | <1ms vs <0.1ms — незаметно при 1 RPS. Индексы на PK. |
| PG connection exhaustion | 🟡 | asyncpg pool: min=2, max=10. Мониторить в /health. |
| LISTEN/NOTIFY пропуск | 🟢 | Worker всё равно поллит каждые 5 сек — NOTIFY лишь ускоряет. |
| Оба VPN-прокси недоступны | 🟡 | Alert админу в Postgres (TG недоступен). Входящие продолжают работать. Retry с backoff. |
| VPN-прокси добавляет latency | 🟢 | Цель <200ms. Серверы в Европе — задержка до РФ ~100ms. |
| РКН блокирует один протокол (VLESS или AWG) | 🟡 | Два разных протокола — вероятность одновременной блокировки низкая. Failover автоматический. |
| РКН блокирует входящие webhook от Telegram | 🟢 | Маловероятно (входящий трафик). Если случится — перенести Caddy на VPN-1 как reverse proxy. |
| Миграция ломает существующие сессии | 🟢 | Продакшена нет — нечего мигрировать. Чистый старт. |

---

## 9. Открытые вопросы

| ID | Вопрос | Влияние | Действие |
|----|--------|---------|----------|
| OQ-9 | Telegram доставляет webhooks напрямую на российский IP? Или нужен прокси и для входящих? | Архитектура Caddy | Протестировать: выставить webhook, отправить сообщение боту, проверить логи Caddy |
| OQ-10 | ~~Какой протокол прокси на VPN-серверах~~ | **Решён:** VPN-1: XRay SOCKS5 inbound (встроенный). VPN-2: microsocks поверх AmneziaWG. | — |
| OQ-11 | httpx[socks] нужен? | **Да** — SOCKS5 прокси, нужна зависимость `socksio` (`pip install httpx[socks]`) | Добавить в requirements.txt |
| OQ-12 | pg_cron или application-level cleanup для очистки старых данных? | Ops complexity | Начать с app-level (APScheduler), перейти на pg_cron если нужно |

---

## 10. Оценка трудозатрат

| Шаг | Оценка | Зависимости |
|-----|--------|-------------|
| SQL миграции + PG storage | 2-3 часа | — |
| VPN proxy модуль | 2-3 часа | Доступ к VPN-серверам |
| Миграция сессий | 4-6 часов | Storage готов |
| Миграция idempotency + dedup + cache + budget | 3-4 часа | Storage готов |
| Outbound queue + Worker | 4-6 часов | Storage + proxy готовы |
| Session recovery | 1-2 часа | Сессии мигрированы |
| Тестирование E2E + cleanup Redis | 2-3 часа | Всё готово |
| **Итого** | **~18-27 часов** | |

---

*RFC-002 v0.1.0 — Draft. Требует ревью перед началом работ.*
