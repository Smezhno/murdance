# RFC: DanceBot Agent v0.4.0

**AI-агент для консультации и записи клиентов студии танцев**

WhatsApp Business API • Telegram Bot API • Instagram Messaging API • **Impulse CRM**

---

| Поле | Значение |
|------|----------|
| **Проект** | DanceBot Agent |
| **Версия RFC** | 0.4.0 (Final — post all reviews) |
| **Дата** | 12 февраля 2026 |
| **Автор** | Александр |
| **Статус** | Production-ready spec |
| **CRM** | Impulse CRM (impulsecrm.ru) — HTTP Basic Auth |
| **Стек** | Python 3.12 + FastAPI + Multi-LLM (Claude / GPT-4o) |
| **Деплой** | Docker Compose → Yandex Cloud VM |
| **MVP-заказчик** | Студия танцев Татьяны (Владивосток) |

### Changelog

- **v0.1** — начальный RFC (архитектура, Paraplan CRM, fallback)
- **v0.2** — FSM, идемпотентность, WhatsApp templates, threat model
- **v0.3** — Paraplan → Impulse CRM, 22 corner-cases, Budget Guard, Prompt Regression Tests
- **v0.4** — UX principles, slot-based скрипты, deployment architecture, degradation levels, inbound dedup, cache contract, KB management, acceptance test scope, outbound SLA

---

## Содержание

1. [Executive Summary](#1-executive-summary)
2. [Non-goals](#2-non-goals)
3. [UX Principles & Conversation Guidelines](#3-ux-principles--conversation-guidelines)
4. [Сценарии MVP — slot-based scripts](#4-сценарии-mvp--slot-based-scripts)
5. [Архитектура системы](#5-архитектура-системы)
6. [Deployment Architecture](#6-deployment-architecture)
7. [Conversation Manager — FSM](#7-conversation-manager--fsm)
8. [Intent Resolution & Multi-turn](#8-intent-resolution--multi-turn)
9. [Интеграция с Impulse CRM](#9-интеграция-с-impulse-crm)
10. [Corner Cases (CC-1..CC-22)](#10-corner-cases)
11. [Knowledge Base — модуль и управление](#11-knowledge-base--модуль-и-управление)
12. [Human Handoff Protocol](#12-human-handoff-protocol)
13. [AI / LLM-слой & Policy Enforcement](#13-ai--llm-слой--policy-enforcement)
14. [Budget Guard](#14-budget-guard)
15. [Prompt Regression Tests](#15-prompt-regression-tests)
16. [Интеграция мессенджеров](#16-интеграция-мессенджеров)
17. [Inbound Deduplication](#17-inbound-deduplication)
18. [Outbound Delivery & Queues](#18-outbound-delivery--queues)
19. [WhatsApp 24h Window & Templates](#19-whatsapp-24h-window--templates)
20. [Data Storage & Caching](#20-data-storage--caching)
21. [Degradation Levels (Fallback)](#21-degradation-levels)
22. [Observability & Metrics](#22-observability--metrics)
23. [Logging & Privacy](#23-logging--privacy)
24. [Security & Threat Model](#24-security--threat-model)
25. [Test Strategy](#25-test-strategy)
26. [Roadmap](#26-roadmap)
27. [Risks](#27-risks)
28. [Open Questions](#28-open-questions)
29. [Acceptance Criteria](#29-acceptance-criteria)
30. [Appendix A: Impulse CRM API](#appendix-a-impulse-crm-api)

---

## 1. Executive Summary

DanceBot Agent — AI-агент для студий танцев: консультирует, записывает на занятия через Impulse CRM, отправляет напоминания, эскалирует администратору. Работает через WhatsApp, Telegram и Instagram.

**Принципы:**
- **Defensive by default:** каждый вход валидируется, каждый выход проверяется
- **Script-driven, but natural:** обязательные шаги в коде, живая речь от LLM
- **LLM никогда не выдумывает:** факты только из KB и CRM, код блокирует галлюцинации
- **Бюджет под контролем:** hard limits на токены, auto-shutdown при аномалиях
- **Graceful degradation:** 4 уровня деградации, данные не теряются ни в одном

---

## 2. Non-goals

- ❌ Приём оплаты / платёжные системы
- ❌ Личный кабинет клиента (есть в Impulse CRM)
- ❌ Админ-панель (управление через YAML-конфиги и код)
- ❌ Голосовые сообщения (вежливый отказ + просьба написать текстом)
- ❌ Автоматическое продление абонементов
- ❌ Multi-tenant инфраструктура (архитектура готова, но не в MVP)
- ❌ Микросервисная архитектура (монолит, см. секцию 6)

---

## 3. UX Principles & Conversation Guidelines

### 3.1 Стиль общения

| Правило | Пример ✅ | Антипример ❌ |
|---------|----------|--------------|
| Живо, без канцелярита | "Привет! На какое направление хочешь записаться?" | "Здравствуйте. Для осуществления записи укажите направление." |
| Без повторяющихся шаблонов | Каждый ответ уникален | "Спасибо за ваш вопрос! Я с радостью помогу!" × 10 |
| Без "как ИИ" маркеров | "Покажу расписание" | "Как языковая модель, я могу найти расписание" |
| Одно сообщение = один вопрос | "На какой день?" | "На какой день, во сколько и как вас зовут?" |
| Адаптация под канал | TG: с кнопками. WA: короче. IG: ещё короче. | Одинаковый текст везде |
| Эмодзи — умеренно | 1-2 эмодзи на сообщение | 🎉💃🔥✨🎊 |

### 3.2 Обязательные правила диалога

1. **Перед подтверждением записи** бот ОБЯЗАН показать резюме:
   > "Записываю: Мария, +7-999-123-45-67, Contemporary, среда 19:00. Всё верно?"

2. **Если данных нет в KB или CRM** → "Уточню у администратора и напишу!" (НИКОГДА не предполагать)

3. **Если LLM не уверен в intent** → переспрос, а не угадывание

4. **Длина ответа по каналу:**
   - Telegram: до 300 символов (+ inline-кнопки)
   - WhatsApp: до 200 символов
   - Instagram: до 150 символов

---

## 4. Сценарии MVP — slot-based scripts

> "Script" — это **не шаблоны текста**, а: обязательные слоты (данные), допустимые пути, политика подтверждения/эскалации.

### S1: Консультация по услугам/расписанию

| Параметр | Значение |
|----------|----------|
| **Обязательные слоты** | — (информационный запрос) |
| **Источник данных** | Knowledge Base (YAML) + Impulse schedule cache |
| **Критерий "успешно"** | Клиент получил ответ на вопрос |
| **Handoff** | Если вопрос не в KB и не в CRM |
| **Ограничение** | ❗ Ответ ТОЛЬКО из KB/CRM. Если нет данных → "уточню у администратора" |

### S2: Запись на занятие

| Параметр | Значение |
|----------|----------|
| **Обязательные слоты** | `group` (направление), `datetime` (дата+время), `client_name`, `client_phone` |
| **Опциональные слоты** | `third_party_name` (запись за другого), `comment` |
| **Auto-fill** | `client_phone` из WhatsApp (если канал WA). `datetime.timezone` = Asia/Vladivostok |
| **Критерий "успешно"** | Reservation создана в Impulse CRM ИЛИ отправлена в fallback |
| **Handoff** | Нет мест + нет альтернатив. Клиент недоволен. |
| **Обязательный шаг** | Резюме перед подтверждением (правило 3.2.1) |

### S3: Напоминания

| Параметр | Значение |
|----------|----------|
| **Тип** | Proactive (cron) |
| **Тайминг** | 24ч и 2ч до занятия |
| **Канал** | Тот же, откуда записался клиент |
| **WhatsApp** | Через pre-approved template (см. секцию 19) |
| **Критерий "успешно"** | Сообщение доставлено |
| **Fallback** | Если WA template не прошёл → TG/IG если есть. Иначе → лог. |

### S4: Эскалация

| Параметр | Значение |
|----------|----------|
| **Триггеры** | Явный запрос ("позовите человека"), KB miss, жалоба, возврат |
| **Handoff** | Relay mode → админ TG-чат (секция 12) |
| **Критерий "успешно"** | Админ ответил клиенту через relay |
| **Timeout** | 2ч → повторный алерт, 4ч → "администратор ответит позже" → IDLE |

### S5: Cancel / Reschedule

| Параметр | Значение |
|----------|----------|
| **MVP scope** | Отмена и перенос только последней/активной записи |
| **Идентификация записи** | Клиент НЕ знает reservation_id → бот показывает список будущих записей по телефону |
| **Обязательные слоты** | `reservation` (выбрана из списка) |
| **Перенос** | Cancel old → create new (с idempotency lock) |
| **Поздняя отмена** | < 2ч до занятия → предупреждение о списании |
| **Handoff** | Если запись не найдена + клиент настаивает |

### S6: Серийная запись

| Параметр | Значение |
|----------|----------|
| **Обязательные слоты** | `group`, `weekdays[]`, `period` (месяц/кол-во недель) |
| **Критерий "успешно"** | N записей из M возможных создано |
| **Лимит** | Макс 20 записей за раз (MVP) |
| **Handoff** | Если > 20 или нестандартный запрос |

---

## 5. Архитектура системы

### 5.1 Компоненты

| Слой | Компонент | Ответственность |
|------|-----------|-----------------|
| Channels | Channel Gateway | Webhook → UnifiedMessage. Signature verify. Inbound dedup. |
| Channels | Message Filter | Голосовые, стикеры, картинки → вежливый отказ ДО LLM. |
| Channels | Outbound Queue | Rate-limit per channel. Retry. Dead-letter queue. |
| Core | Conversation Manager | FSM orchestrator. Session. Timeout watchdog. |
| Core | Intent Resolver | Определение intent + slot extraction. Приоритизация. |
| Core | Temporal Parser | "завтра", "на среду", "на 5-е" → абсолютная дата. |
| Core | Contact Validator | Телефон: нормализация + валидация. Запись за другого. |
| Core | Idempotency Guard | booking_fingerprint → Redis lock. |
| Core | Session Recovery | Cleanup stale sessions on startup. |
| AI | LLM Router | Multi-provider. Tool calling. Budget Guard. |
| AI | Policy Enforcer | Hard rules в коде: блок галлюцинаций, JSON validation. |
| AI | Context Manager | Sliding window + summarization (анти-CC-22). |
| AI | Prompt Engine | System prompt builder + KB injection. |
| Integration | Impulse Adapter | HTTP Basic auth. Retry. Cache. Error handler. |
| Integration | Scheduler | APScheduler: reminders, sync, health-check. |
| Resilience | Fallback Handler | TG-чат + Redis queue. SLA. |
| Resilience | Budget Guard | Token/cost hard limits. Auto-shutdown. |
| Observability | Trace Logger | structlog + trace_id. Test mode. |

### 5.2 Структура проекта

```
dancebot/
├── app/
│   ├── main.py                    # FastAPI, webhook routes, lifespan
│   ├── config.py                  # pydantic-settings (.env)
│   ├── models.py                  # UnifiedMessage, Session, BookingRequest
│   ├── channels/
│   │   ├── base.py                # ChannelProtocol
│   │   ├── telegram.py            # aiogram 3.x
│   │   ├── whatsapp.py            # Cloud API (httpx)
│   │   ├── instagram.py           # Messenger Platform (httpx)
│   │   ├── filters.py             # Voice/sticker/photo → reply
│   │   ├── outbound_queue.py      # Async queue + rate limiter
│   │   └── dedup.py               # Inbound message deduplication
│   ├── core/
│   │   ├── conversation.py        # FSM orchestrator
│   │   ├── fsm.py                 # States, transitions, metadata
│   │   ├── intent.py              # Intent resolver + priorities
│   │   ├── temporal.py            # Relative date parsing
│   │   ├── contact_validator.py   # Phone normalization
│   │   ├── idempotency.py         # Booking fingerprint
│   │   ├── escalation.py          # Handoff relay
│   │   ├── scheduler.py           # APScheduler
│   │   └── session_recovery.py    # Startup cleanup
│   ├── ai/
│   │   ├── router.py              # LLMRouter
│   │   ├── budget_guard.py        # Token limits
│   │   ├── policy.py              # Hard guardrails
│   │   ├── context_manager.py     # Sliding window + summary
│   │   ├── json_parser.py         # 3-step JSON extraction
│   │   ├── providers/
│   │   │   ├── base.py
│   │   │   ├── anthropic.py
│   │   │   └── openai.py
│   │   ├── tools.py               # Tool definitions
│   │   └── prompts/
│   │       ├── system.py          # Prompt builder
│   │       └── templates/
│   ├── integrations/
│   │   └── impulse/
│   │       ├── client.py          # httpx + Basic auth
│   │       ├── models.py          # Pydantic strict
│   │       ├── cache.py           # Schedule cache
│   │       ├── fallback.py        # TG + queue
│   │       └── error_handler.py   # CRM error codes
│   └── storage/
│       ├── redis.py
│       └── models.py
├── knowledge/
│   ├── base.py                    # KB loader + validator + search
│   ├── studio_tatyana.yaml
│   └── _template.yaml
├── tests/
│   ├── prompt_regression/
│   │   ├── test_booking_flow.yaml
│   │   ├── test_schedule_query.yaml
│   │   ├── test_edge_cases.yaml
│   │   └── runner.py
│   ├── e2e/                       # Full scenario tests
│   └── unit/
├── docker-compose.yml
├── Dockerfile
├── .env.example
└── pyproject.toml
```

---

## 6. Deployment Architecture

### 6.1 Принцип: монолит на одном VM

> **Контекст:** ограниченный бюджет, мало технических знаний у оператора (Александр — PM, не DevOps). Всё поднимает Cursor на базе Yandex Cloud.

**Архитектура: Docker Compose монолит** — НЕ микросервисы.

```
┌─────────────────────────────────────────────┐
│ Yandex Cloud VM (2 vCPU, 4GB RAM, 40GB SSD) │
│                                              │
│  docker-compose up                           │
│  ┌─────────────────────────────────────────┐ │
│  │ app (FastAPI + uvicorn)                 │ │
│  │   • webhook endpoints                   │ │
│  │   • conversation manager                │ │
│  │   • LLM router                          │ │
│  │   • impulse adapter                     │ │
│  │   • APScheduler (in-process)            │ │
│  │   • outbound queue (asyncio)            │ │
│  └─────────────┬───────────────────────────┘ │
│                │                              │
│  ┌─────────────▼───────────────────────────┐ │
│  │ redis (7-alpine)                        │ │
│  │   • sessions, cache, locks, queues      │ │
│  │   • fallback queue                      │ │
│  │   • budget counters                     │ │
│  └─────────────────────────────────────────┘ │
│                                              │
│  ┌─────────────────────────────────────────┐ │
│  │ caddy (reverse proxy + auto SSL)        │ │
│  │   • HTTPS termination                   │ │
│  │   • Let's Encrypt auto-renew            │ │
│  └─────────────────────────────────────────┘ │
└──────────────────────────────────────────────┘
```

### 6.2 Почему монолит

| Фактор | Решение |
|--------|---------|
| Бюджет | ~2000₽/мес (1 VM) вместо ~10000₽ (3+ сервиса) |
| Сложность | docker-compose up — одна команда |
| Оператор | Не DevOps. Cursor поднимает, логи через `docker logs` |
| Масштаб MVP | 1 студия, ~50-100 диалогов/день. Монолит справится. |
| Переход | Если нужно масштабировать → выносим worker в отдельный контейнер |

### 6.3 Сервисы и процессы

| Сервис | Роль | Контейнер |
|--------|------|-----------|
| **API service** | FastAPI: webhooks, conversation, LLM, tools | `app` |
| **Scheduler** | APScheduler (in-process): reminders, health-check, cache sync | `app` (тот же) |
| **Outbound worker** | asyncio background task: очередь отправки | `app` (тот же) |
| **Redis** | Sessions, cache, queues, locks, budgets | `redis` |
| **Caddy** | HTTPS + reverse proxy | `caddy` |

**Нет отдельного worker/DB/message queue** — всё внутри одного Python-процесса + Redis. Для MVP это оптимально.

### 6.4 docker-compose.yml

```yaml
version: "3.8"
services:
  app:
    build: .
    restart: unless-stopped
    env_file: .env
    depends_on: [redis]
    volumes:
      - ./knowledge:/app/knowledge  # hot-reload KB
      - ./logs:/app/logs
    expose: ["8000"]

  redis:
    image: redis:7-alpine
    restart: unless-stopped
    command: redis-server --requirepass ${REDIS_PASSWORD}
    volumes: ["redis_data:/data"]

  caddy:
    image: caddy:2-alpine
    restart: unless-stopped
    ports: ["443:443", "80:80"]
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile
      - caddy_data:/data

volumes:
  redis_data:
  caddy_data:
```

### 6.5 Эксплуатация (для оператора)

| Действие | Команда |
|----------|---------|
| Запуск | `docker compose up -d` |
| Логи | `docker compose logs -f app` |
| Перезапуск | `docker compose restart app` |
| Обновление кода | `git pull && docker compose up -d --build app` |
| Обновление KB | Редактировать `knowledge/studio_tatyana.yaml`, перезапуск app |
| Бэкап Redis | `docker compose exec redis redis-cli BGSAVE` |

---

## 7. Conversation Manager — FSM

### 7.1 Состояния

```python
class ConversationState(str, Enum):
    # === Transient (кратковременные) ===
    IDLE = "idle"                              # Начальное. TTL: до сообщения
    COLLECTING_INTENT = "collecting_intent"    # Определяем что хочет. TTL: 24h
    BROWSING_SCHEDULE = "browsing_schedule"    # Смотрит расписание. TTL: 24h
    COLLECTING_GROUP = "collecting_group"      # Выбирает группу. TTL: 24h
    COLLECTING_DATETIME = "collecting_datetime" # Выбирает время. TTL: 24h
    COLLECTING_CONTACT = "collecting_contact"  # Имя + телефон. TTL: 24h
    CONFIRM_BOOKING = "confirm_booking"        # Подтверждение. TTL: 3h
    BOOKING_IN_PROGRESS = "booking_in_progress" # Запрос к CRM. TTL: 30s
    SERIAL_BOOKING = "serial_booking"          # Серийная запись. TTL: 24h
    CANCEL_FLOW = "cancel_flow"                # Отмена/перенос. TTL: 24h
    
    # === Terminal (завершающие — auto-transition) ===
    BOOKING_DONE = "booking_done"              # Запись создана. Auto → IDLE через 5s
    # Смысл: позволяет отправить "Записал! Напомню за день" до перехода в IDLE
    
    # === Persistent (долгоживущие) ===
    HANDOFF_TO_ADMIN = "handoff_to_admin"      # Ждём админа. TTL: 4h
    ADMIN_RESPONDING = "admin_responding"       # Relay mode. TTL: 4h
```

### 7.2 Правила FSM при конкурирующих событиях

| Ситуация | Поведение |
|----------|-----------|
| **Новое сообщение в active state** | Обрабатываем в контексте текущего state |
| **Повторный вопрос** (клиент спросил то же) | Отвечаем заново (может забыл) |
| **Смена темы** ("а сколько стоит?" в середине записи) | Отвечаем из KB, мягко возвращаем: "А по записи — продолжим?" |
| **Параллельные запросы** ("хочу и расписание, и записаться") | Приоритет: запись > консультация. "Давай сначала запишу, а расписание покажу после!" |
| **Сообщение в BOOKING_IN_PROGRESS** | Буферизуем, не обрабатываем (lock) |
| **Сообщение в ADMIN_RESPONDING** | Relay в админ-чат (бот молчит) |

### 7.3 Таймауты

| State | Timeout | Действие |
|-------|---------|----------|
| Любой (кроме ADMIN_*) | 24h | → IDLE |
| CONFIRM_BOOKING | 1h | Переспрос: "Ещё актуально?" |
| CONFIRM_BOOKING | 3h | → IDLE с уведомлением |
| BOOKING_IN_PROGRESS | 30s | Timeout → fallback |
| BOOKING_DONE | 5s | → IDLE (auto) |
| HANDOFF_TO_ADMIN | 2h | Повторный алерт |
| ADMIN_RESPONDING | 4h | "Админ не ответил" → IDLE |

### 7.4 Session Recovery (crash/restart)

При старте app:
1. Scan all `session:*` keys in Redis
2. `BOOKING_IN_PROGRESS` + age > 1min → send to fallback + notify client
3. Any state + age > 24h → reset to IDLE
4. `ADMIN_RESPONDING` + age > 4h → notify client "admin will reply later" → IDLE

---

## 8. Intent Resolution & Multi-turn

### 8.1 Intent taxonomy

| Intent | Приоритет | Trigger examples |
|--------|-----------|-----------------|
| `booking` | 🔴 1 (highest) | "запишите", "хочу на занятие", "есть места?" |
| `cancel` | 🔴 1 | "отменить", "не приду", "перенести" |
| `schedule` | 🟡 2 | "расписание", "когда занятие", "во сколько" |
| `price` | 🟡 2 | "сколько стоит", "цена", "абонемент" |
| `info` | 🟢 3 | "какие направления", "кто преподаватель" |
| `lateness` | 🟢 3 | "опаздываю", "задерживаюсь" |
| `greeting` | ⚪ 4 | "привет", "здравствуйте" |
| `admin` | 🔴 1 | "позовите человека", "жалоба" |

### 8.2 Правила multi-turn

1. **Intent определяется LLM** через system prompt с taxonomy + примерами
2. **Смена intent:** LLM сообщает в structured output: `{intent: "new", slot_update: {...}}`
3. **Приоритет:** если текущий intent=booking и новый=info → отвечаем на info, возвращаемся к booking
4. **Фокус:** бот мягко возвращает в flow: "Хороший вопрос! Contemporary — 800₽ за разовое. А по записи — на среду в 19:00 подойдёт?"
5. **Correction:** "нет, на 19" → обновить slot, не менять intent

---

## 9. Интеграция с Impulse CRM

### 9.1 API Overview

| Параметр | Значение |
|----------|----------|
| **Auth** | HTTP Basic (API-ключ, бессрочный) |
| **Format** | REST JSON, POST для list/update/delete, GET для load |
| **Entities** | 22, каждая с 5 actions (list, load, update, update+id, delete) |
| **Base URL** | `https://{tenant}.impulsecrm.ru/api/{entity}/{action}` |

### 9.2 Ключевые сущности

| Сущность | Использование |
|----------|--------------|
| `schedule` | ✅ list — расписание занятий |
| `reservation` | ✅ list/update/delete — записи клиентов |
| `client` | ✅ list/update — клиентская база |
| `group` | ✅ list — группы/направления |
| `teacher` | ✅ list — преподаватели |
| `hall` | ✅ list — залы |
| `style` | ✅ list — направления |
| `informer` | ✅ list — источники ("WhatsApp Bot") |
| `status` | ⚠️ list — статусы для воронки |

### 9.3 Booking Flow

```
1. Поиск клиента:   POST /api/client/list  {columns: {phone: "+7..."}}
2. Если не найден:   POST /api/client/update {name, phone, informerId}
3. Создание записи:  POST /api/reservation/update {clientId, scheduleId, ...}
4. При ошибке:       → fallback TG-чат + Redis queue
```

### 9.4 CRM Error Handling

| Ошибка CRM | Ответ клиенту |
|-------------|---------------|
| Нет мест | "Нет мест на это время. {alternatives}" |
| Уже записан | "Вы уже записаны! Хотите на другое?" |
| Занятие не найдено | "Расписание изменилось. Показать актуальное?" |
| Занятие в прошлом | "Это время прошло. Ближайшее: {next}" |
| Группа заполнена | "Группа полная. Лист ожидания или другое время?" |
| HTTP 5xx | "Технический сбой. Записал заявку — администратор подтвердит." |

### 9.5 Идемпотентность

| Параметр | Значение |
|----------|----------|
| **Что считается дублем** | Совпадение phone + scheduleId (= тот же клиент + то же занятие) |
| **Fingerprint** | `sha256(phone + schedule_id)` |
| **Redis key** | `idempotency:{fingerprint}` |
| **TTL** | 10 минут |
| **При дубле** | "Вы уже записаны на это занятие ✅" |
| **Гарантия** | Lock ставится ДО вызова CRM. Даже при retry — одна запись. |

---

## 10. Corner Cases

Все 22 кейса из v0.3 сохранены. Ключевые:

| CC# | Кейс | Решение |
|-----|------|---------|
| 2 | Темпоральные ловушки | `TemporalParser` в коде, LLM не вычисляет даты |
| 3 | Рваный диалог | Correction handler: обновить slot, не сбрасывать FSM |
| 5 | Голосовые/стикеры | `MessageFilter` ДО LLM |
| 6 | Запись за другого | Отдельные поля client_name / contact_name |
| 7 | Серийная запись | Batch creation с idempotency на каждую |
| 9 | "Я опаздываю" | Не меняем запись, предупреждаем преподавателя |
| 10 | "Кто лучше?" | Policy Enforcer: только факты из KB |
| 11 | Каникулы | holidays в KB → "студия отдыхает до {date}" |
| 13 | Параллельная переписка | Processing lock + pending buffer |
| 16 | Зомби-состояния | Session recovery on startup |
| 17 | "Вы тут?" | Typing indicator + 30s timeout |
| 21 | Невалидный JSON от LLM | 3-step parser: standard → regex → retry → fallback |
| 22 | Пухнущий контекст | Sliding window 20 msg + summarization |

Полное описание каждого CC — см. v0.3 секция 6.

---

## 11. Knowledge Base — модуль и управление

### 11.1 KB как контракт

```yaml
# schema_version обязательна. При несовпадении → ошибка при старте
schema_version: "1.0"

# Обязательные секции:
studio: { name, address, phone, schedule, timezone }
tone: { style, pronouns, emoji, language }
services: [{ id, name, description, price_single, price_subscription_8 }]
teachers: [{ id, name, styles, specialization }]  # БЕЗ поля rating
faq: [{ q, a }]
holidays: [{ from, to, name, message }]
escalation: { triggers[], admin_telegram_id }
```

### 11.2 Кто и как обновляет

| Действие | Кто | Как |
|----------|-----|-----|
| Цены, услуги, FAQ | Александр / Татьяна | Редактировать YAML → `docker compose restart app` |
| Расписание | CRM (source of truth) | Автоматически через cache sync |
| Каникулы | Александр | YAML → restart |
| Преподаватели (био) | Александр | YAML → restart |

### 11.3 Актуальность и конфликт KB vs CRM

| Данные | Source of truth | Fallback |
|--------|----------------|----------|
| **Расписание** | CRM (Impulse) | KB → "расписание могло измениться, уточню" |
| **Цены** | KB (YAML) | Если KB miss → "уточню у администратора" |
| **Преподаватели** | CRM (list) + KB (bio) | CRM = кто есть, KB = описания |
| **FAQ** | KB | Если не найдено → эскалация |

**Правило:** если KB и CRM противоречат (например, KB говорит "800₽", а CRM пусто) → приоритет KB для цен, приоритет CRM для расписания.

### 11.4 Валидация при старте

```python
class KBValidator:
    def validate_on_startup(self, path: str):
        data = yaml.safe_load(open(path))
        assert data.get("schema_version") == "1.0", "Schema version mismatch"
        assert "studio" in data, "Missing studio section"
        assert "services" in data and len(data["services"]) > 0, "No services"
        assert "teachers" in data, "No teachers"
        # Если валидация провалилась → app не стартует
```

---

## 12. Human Handoff Protocol

### Relay Mode (Telegram)

```
1. Бот → клиенту: "Передаю администратору. Ответит в этом чате ⏳"
2. Бот → админ TG-чат: "🔔 Эскалация: [контекст]. /reply {chat_id} ваш ответ"
3. Админ: "/reply 12345 Привет, да, можно перенести"
4. Бот → клиенту: "Администратор Татьяна: Привет, да, можно перенести"
5. Клиент отвечает → бот relay в админ-чат (FSM = ADMIN_RESPONDING)
6. Админ: "/close 12345" → FSM → IDLE
```

---

## 13. AI / LLM-слой & Policy Enforcement

### 13.1 Soft rules (промпт) vs Hard rules (код)

| Правило | Тип | Реализация |
|---------|-----|------------|
| "Отвечай дружелюбно" | 🟡 Soft | System prompt |
| "Одно сообщение = один вопрос" | 🟡 Soft | System prompt |
| "Не выдумывай расписание" | 🔴 **Hard** | Policy Enforcer: если intent=schedule → require tool_call |
| "Не называй цену не из KB" | 🔴 **Hard** | Policy Enforcer: price regex → check against KB |
| "Резюме перед записью" | 🔴 **Hard** | FSM: CONFIRM_BOOKING обязателен |
| "Нет данных → 'уточню'" | 🔴 **Hard** | Policy Enforcer: если tool failed → forced fallback text |
| "Не сравнивай преподавателей" | 🔴 **Hard** | Policy Enforcer: detect comparison patterns |
| "Антигаллюцинация фактов" | 🔴 **Hard** | Тестируемое: prompt regression + policy checks |

### 13.2 JSON Validation

```python
async def parse_tool_call(raw) -> ToolCall | None:
    # Step 1: standard parse
    # Step 2: extract from markdown code block
    # Step 3: retry LLM with "respond ONLY in valid JSON"
    # Step 4: return None → ConversationManager handles as "не понял"
    # БРОСАТЬ ИСКЛЮЧЕНИЕ ЗАПРЕЩЕНО — бот не должен упасть
```

---

## 14. Budget Guard

```python
# Hard limits — при превышении ЛЮБОГО → auto-shutdown LLM
MAX_TOKENS_PER_HOUR = 100_000     # ~$1 Claude Sonnet
MAX_TOKENS_PER_DAY = 500_000      # ~$5
MAX_COST_PER_DAY_USD = 10.0       # absolute cap
MAX_REQUESTS_PER_MINUTE = 30      # anti-loop
MAX_ERRORS_PER_HOUR = 50          # anomaly detection

# При shutdown:
# 1. Alert в TG-чат
# 2. Бот переключается в static mode (ответы из KB без LLM)
# 3. Записи через fallback
```

---

## 15. Prompt Regression Tests

### Scope

```yaml
# tests/prompt_regression/test_booking_flow.yaml
# Каждый тест: sequence of (user → expected)
# Expected checks: contains[], not_contains[], tool_calls[]

# Стабильность: temperature=0, seed=42
# Критерий pass: contains match (не exact match — LLM вариативен)
# Flaky protection: каждый тест запускается 3 раза, pass если 2/3
```

**Запуск:**
- В CI/CD перед deploy
- После любого изменения system prompt
- После изменения KB
- `python -m tests.prompt_regression.runner` → exit code 0/1

---

## 16. Интеграция мессенджеров

### 16.1 Unified Interface

```python
class ChannelProtocol(Protocol):
    async def parse_webhook(self, request: Request) -> UnifiedMessage: ...
    async def send_message(self, chat_id: str, text: str) -> bool: ...
    async def send_buttons(self, chat_id: str, text: str, buttons: list) -> bool: ...
    async def send_typing(self, chat_id: str) -> None: ...
    def verify_signature(self, request: Request) -> bool: ...
```

### 16.2 Каналы

| Канал | Макс. длина ответа | Кнопки | Typing |
|-------|--------------------|--------|--------|
| Telegram | 300 символов | Inline buttons | ✅ |
| WhatsApp | 200 символов | Quick replies (3 max) | ✅ |
| Instagram | 150 символов | Ice breakers | ✅ |

---

## 17. Inbound Deduplication

```python
class InboundDedup:
    """
    Проблема: мессенджеры могут отправить webhook дважды.
    Решение: message_id в Redis с коротким TTL.
    """
    
    async def is_duplicate(self, message: UnifiedMessage) -> bool:
        key = f"seen:{message.channel}:{message.message_id}"
        # SETNX: true если ключ создан, false если уже есть
        is_new = await redis.set(key, "1", ex=300, nx=True)  # 5 min TTL
        return not is_new  # True = дубликат
    
    # Out-of-order: обрабатываем все сообщения, 
    # но при записи — idempotency guard защищает от дубликатов
```

---

## 18. Outbound Delivery & Queues

### 18.1 Outbound Queue

```python
class OutboundQueue:
    """Все исходящие сообщения проходят через очередь."""
    
    # Rate limits per channel
    LIMITS = {
        "telegram": 30,     # msg/sec
        "whatsapp": 80,     # msg/sec (tier dependent)
        "instagram": 200,   # msg/24h per user
    }
    
    async def enqueue(self, channel, chat_id, text, priority=0):
        await redis.zadd(f"outbound:{channel}", {json.dumps(msg): priority})
    
    async def _worker(self):
        """Background task: dequeue + rate limit + send + retry"""
        while True:
            msg = await redis.zpopmin(f"outbound:{channel}")
            if await self._check_rate_limit(msg):
                success = await self._send(msg)
                if not success:
                    await self._retry_or_dlq(msg)
```

### 18.2 Retry Policy

| Попытка | Задержка | Действие при неудаче |
|---------|----------|---------------------|
| 1 | 0s | retry |
| 2 | 5s | retry |
| 3 | 30s | retry |
| 4+ | — | → Dead Letter Queue (DLQ) + alert |

### 18.3 Dead Letter Queue

```
# Redis key: dlq:{channel}
# Содержит: {chat_id, text, error, attempts, timestamp}
# Мониторинг: если DLQ > 10 → alert
# Обработка: ручная или retry по расписанию (1 раз в час)
```

### 18.4 SLA напоминаний

| Тип | SLA |
|-----|-----|
| Напоминание 24ч | Доставлено ±30 мин от расчётного времени |
| Напоминание 2ч | Доставлено ±5 мин от расчётного времени |
| Ответ на сообщение | < 5с (p95) от получения webhook |

---

## 19. WhatsApp 24h Window & Templates

### 19.1 Типы сообщений

| Тип | Когда нужен template | Пример |
|-----|---------------------|--------|
| Ответ на сообщение | ❌ (внутри 24h window) | Любой ответ бота |
| Напоминание 24ч | ✅ **Всегда** | "Завтра в 19:00 у тебя Contemporary!" |
| Напоминание 2ч | ✅ **Всегда** | "Через 2 часа занятие!" |
| Booking confirmation | ✅ (если прошло >24ч от последнего msg) | "Запись подтверждена" |

### 19.2 Templates (согласовать с Meta до Phase 4)

| Template name | Текст | Ответственный |
|---------------|-------|---------------|
| `lesson_reminder_24h` | "Привет {{1}}! Завтра в {{2}} у тебя {{3}}. До встречи! 💃" | Александр |
| `lesson_reminder_2h` | "{{1}}, через 2 часа — {{2}}. Ждём!" | Александр |
| `booking_confirm` | "{{1}}, вы записаны: {{2}}, {{3}}. Вопросы? Напишите!" | Александр |

### 19.3 Fallback если окно закрыто и template не одобрен

1. Отправить через другой канал (TG/IG) если есть
2. Если нет → fallback queue для ручной отправки + alert

**Напоминания должны работать предсказуемо:** если template не одобрен → Phase 4 блокируется до одобрения.

---

## 20. Data Storage & Caching

### 20.1 Разделение хранилищ

| Тип данных | Где хранится | Source of truth |
|-----------|-------------|----------------|
| Состояние диалога (FSM, slots) | Redis (TTL) | Redis |
| Долгосрочные данные (user profile) | Redis (TTL 90d) | Redis* |
| Расписание, записи, клиенты | Impulse CRM | **CRM** |
| Логи, метрики, trace | structlog → файлы + stdout | Файлы |
| KB (услуги, цены, FAQ) | YAML файл | **YAML** |
| Fallback queue | Redis (no TTL) | Redis |

*В будущем user profile может переехать в PostgreSQL.

### 20.2 TTL Policy

| Сущность | TTL | Почему |
|----------|-----|--------|
| Session (FSM + messages) | 24h | Диалог не длится дольше |
| User profile | 90d | "Мария, вы в прошлый раз..." |
| Conversation summary | 30d | Сжатая история |
| Schedule cache | 15 min | CRM = source of truth |
| Groups cache | 1h | Редко меняется |
| Teachers cache | 1h | Редко меняется |
| Idempotency lock | 10 min | Защита от дублей |
| Budget counters | 1h / 24h | Скользящее окно |
| Processing lock | 30s | Anti-race |
| Inbound dedup | 5 min | Webhook retry window |
| DLQ messages | ∞ | Пока не обработаны |

### 20.3 Кэширование — контракт

| Что кэшируется | TTL | Инвалидация | Правило для клиента |
|----------------|-----|-------------|---------------------|
| Расписание (schedule) | 15 min | После создания booking (force refresh) | Данные актуальны |
| Группы (groups) | 1h | Ежечасно | Данные актуальны |
| Цены | ∞ (из KB) | При restart | Данные актуальны |
| Преподаватели | 1h | Ежечасно | Данные актуальны |

**Правило:** бот НИКОГДА не отвечает "кэшированными" данными с оговоркой "могу ошибаться". Если кэш стухший (schedule > 15min) → force refresh. Если refresh failed → "Не могу проверить расписание, уточню у администратора."

---

## 21. Degradation Levels

| Уровень | Причина | Поведение бота | Сообщение клиенту |
|---------|---------|----------------|-------------------|
| **L0: Normal** | Всё ок | Полный функционал | — |
| **L1: CRM down** | Impulse 5xx / timeout | Консультация из KB ✅. Запись → fallback TG-чат. | "Записываю вашу заявку, администратор подтвердит в ближайшее время!" |
| **L2: LLM down** | Budget limit / API outage | Static mode: ответы из KB по keyword match. Запись → fallback. | "Сейчас я работаю в ограниченном режиме. Могу записать заявку — администратор свяжется." |
| **L3: Channel down** | WA API / TG outage | Отправка через другой канал если есть. Иначе → DLQ. | (на другом канале): "Пишу сюда, другой канал временно недоступен" |
| **L4: Queue backlog** | DLQ > 50 или fallback queue > 20 | Alert каждые 30 мин. | — (клиент не видит) |

**Для каждого уровня: данные НЕ теряются.** Всё попадает в fallback queue или DLQ.

---

## 22. Observability & Metrics

### 22.1 Продуктовые KPI (обязательные в MVP)

| Метрика | Тип | Цель |
|---------|-----|------|
| % успешных записей (E2E) | Product | ≥ 95% |
| % handoff / эскалаций | Product | < 15% |
| Avg сообщений до записи | Product | < 8 |
| % "не понял" (fallback intent) | Product | < 10% |
| Conversion: консультация → запись | Product | > 20% |
| LLM cost per booking | Financial | < $0.05 |
| p95 время ответа | Technical | < 5с |
| Fallback rate (CRM errors) | Technical | < 5% |
| Budget Guard triggers / week | Technical | 0 |
| DLQ size | Technical | < 10 |

### 22.2 Аналитика — обязательная часть MVP

Метрики пишутся в structured log → агрегируются скриптом (Phase 5) или Grafana (Phase 6).

### 22.3 Test Mode

```python
# env TEST_MODE=true
# 1. CRM вызовы → mock
# 2. Логи в stdout с полным trace
# 3. TG: /debug → текущий FSM state, session, slots
# 4. TG: /trace {id} → полный лог обработки
# 5. TG: /reset → сброс сессии
```

---

## 23. Logging & Privacy

### 23.1 Что логируется

| Данные | Логируется | Формат |
|--------|-----------|--------|
| trace_id | ✅ | UUID |
| channel, chat_id | ✅ | Полный |
| FSM state transitions | ✅ | state_from → state_to |
| LLM: provider, model, tokens, latency, cost | ✅ | Числа |
| CRM: endpoint, status, latency | ✅ | Числа |
| User message text | ✅ | Полный (для debug) |
| Bot response text | ✅ | Полный |

### 23.2 Что ЗАПРЕЩЕНО логировать

| Данные | Причина |
|--------|---------|
| API keys / tokens | Security |
| CRM auth credentials | Security |
| Cookies | Security |
| raw_payload целиком | Может содержать токены |

### 23.3 PII masking

| Поле | В логах | В Redis |
|------|---------|---------|
| Телефон | `+7999****567` | Полный |
| Имя | Полное (допустимо) | Полное |
| Email | `m***@mail.ru` | Полный |

### 23.4 Retention

| Тип | Retention | Хранение |
|-----|-----------|----------|
| App logs | 30 дней | Файлы на VM |
| Structured metrics | 90 дней | Redis / файлы |
| Conversation history | 24ч (session) + 30д (summary) | Redis |
| Fallback queue | До обработки | Redis |

---

## 24. Security & Threat Model

| Угроза | P | Митигация |
|--------|---|-----------|
| Утечка CRM API key | 🟡 | .env file (600 perms). Yandex Lockbox в проде. Rotate при подозрении. |
| Утечка Messenger tokens | 🟡 | Аналогично. |
| Spoofed webhooks | 🟡 | Signature verification per channel (обязательно). |
| Replay attack | 🟡 | Timestamp window 5 min. Inbound dedup (message_id). |
| Prompt injection | 🔴 | User input → `user` role only. Policy Enforcer. Sanitize. |
| DDoS через мессенджеры | 🟢 | Rate limit per chat_id. |

**Обязательные меры:**
- Webhook signature verification (TG: secret_token, WA/IG: X-Hub-Signature-256)
- Replay protection (timestamp + message_id dedup)
- Secrets в .env (dev) / Yandex Lockbox (prod)
- Redis AUTH + network isolation
- HTTPS (Caddy + Let's Encrypt)

---

## 25. Test Strategy

### 25.1 Scope

| Тип теста | Что проверяет | Когда запускается |
|-----------|---------------|-------------------|
| **Prompt regression** | Диалоги: booking, schedule, edge cases | Перед deploy. После изменения prompt/KB. |
| **E2E scenarios** | S1-S6 полностью с mock CRM | CI/CD |
| **Idempotency** | Дубликаты при retry | CI/CD |
| **Dedup** | Повторные webhook | CI/CD |
| **Cancel/reschedule** | Отмена, перенос, не найдена запись | CI/CD |
| **Degradation** | L1-L4 behaviour | CI/CD |
| **Budget Guard** | Limits trigger correctly | Unit tests |
| **WhatsApp templates** | Template send + fallback | Manual (Phase 4) |

### 25.2 Стабильность prompt tests

```python
# Prompt tests WILL be flaky (LLM is non-deterministic)
# Mitigation:
#   1. temperature=0, seed=42
#   2. Check "contains" not "equals" 
#   3. Run each test 3x, pass if 2/3
#   4. Tolerance: 90% of suite must pass (not 100%)
```

---

## 26. Roadmap

| Фаза | Сроки | Deliverables |
|------|-------|--------------|
| **Phase 1** | 2 нед | Скелет. Telegram. LLM Router. KB loader + validator. FSM. Temporal Parser. Budget Guard. Test mode. |
| **Phase 2** | 2 нед | Impulse Adapter. Booking flow E2E. Idempotency. CRM errors. Message filters. Inbound dedup. Prompt regression v1. |
| **Phase 3** | 2 нед | Serial booking. Cancel/change. Human Handoff relay. Fallback + alerts. Context manager. Session recovery. |
| **Phase 4** | 2 нед | WhatsApp + Instagram. Templates (approve beforehand!). Reminders. Outbound queue + DLQ. |
| **Phase 5** | 1 нед | E2E tests. Load test. Monitoring script. Deploy to Yandex Cloud. Documentation. |
| **Phase 6** | TBD | Multi-tenant. Второй заказчик. PostgreSQL for analytics. Grafana. |

---

## 27. Risks

| Риск | P | Митигация |
|------|---|-----------|
| LLM галлюцинирует факты | 🔴 | Policy Enforcer (hard). Prompt tests. |
| Ночная петля → бюджет | 🔴 | Budget Guard: hard limits + auto-shutdown |
| Prompt drift → booking breaks | 🔴 | Regression tests в CI/CD |
| Impulse API изменится | 🟡 | Pydantic strict. Smoke tests. |
| WhatsApp templates не одобрены | 🟡 | Подавать заранее. Fallback на TG/IG. |
| Дубликаты записей | 🟡 | Idempotency guard + inbound dedup |
| Зомби-сессии | 🟡 | Session recovery on startup |
| VM упал | 🟡 | docker restart policy. Redis persistence. |

---

## 28. Open Questions

| # | Вопрос | Дедлайн |
|---|--------|---------|
| OQ-1 | Impulse: какие `informerId` и `statusId`? | Phase 2 |
| OQ-2 | Impulse: rate limits на API? | Phase 1 |
| OQ-3 | WhatsApp templates: подать на согласование | Phase 3 |
| OQ-4 | Каникулы 2026-2027: даты | Phase 1 |
| OQ-5 | Правила поздней отмены студии | Phase 3 |
| OQ-6 | Макс записей в серийной: 20? | Phase 3 |
| OQ-7 | Согласие на обработку ПД (152-ФЗ) | Phase 4 |

---

## 29. Acceptance Criteria

| Критерий | Порог | Тест |
|----------|-------|------|
| Booking E2E works | Pass | E2E test suite |
| 0 потерянных записей при CRM сбое | 100% | Degradation L1 test |
| Prompt regression pass | ≥ 90% | Runner в CI |
| Budget Guard triggers correctly | Pass | Unit test |
| All 22 CC covered | Pass | E2E + manual |
| LLM cost per booking | < $0.05 | Metric tracking |
| Response time p95 | < 5s | Load test |
| Inbound dedup works | Pass | Unit test |
| Outbound retry + DLQ works | Pass | Integration test |

---

## Appendix A: Impulse CRM API

### Auth
```
HTTP Basic Auth
Header: Authorization: Basic {base64(api_key)}
```

### 5 Actions per entity
| Action | HTTP | Description |
|--------|------|-------------|
| list | POST | List with filters, pagination, sort |
| load | GET | Single record by ID |
| update (no id) | POST | Create |
| update (with id) | POST | Modify |
| delete | POST | Delete |

### URL Format
```
POST https://{tenant}.impulsecrm.ru/api/{entity}/{action}
```

### Request Body (list)
```json
{
    "fields": ["id", "name", "phone"],
    "limit": 10,
    "page": 1,
    "sort": {"created": "desc"},
    "columns": {"phone": "+79991234567"}
}
```

### 22 Entities
Key for agent: `schedule`, `reservation`, `client`, `group`, `teacher`, `hall`, `style`, `informer`, `status`.

---

*RFC v0.4.0 — final production-ready spec. All reviews integrated. Ready for Cursor.*
