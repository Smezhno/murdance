# DanceBot

Агент для студии танцев с поддержкой записи на занятия через мессенджеры.

## Описание

- Узнать расписание занятий
- Записаться на занятия
- Получить информацию о ценах и абонементах
- Отменить или перенести запись

Агент использует LLM (базовая - YandexGPT Pro 5.1) для понимания естественного языка и интеграцию с CRM системой Impulse для управления записями. Заложена возможность расширения LLM

## Архитектура

- **FastAPI** — основной веб-сервер для обработки webhook'ов
- **Worker** — фоновый процесс для отправки сообщений и напоминаний
- **PostgreSQL** — единственное хранилище: сессии, очереди, кэш, аудит (RFC-002)
- **Caddy** — HTTPS прокси

## Требования

- Python 3.11+
- Docker и Docker Compose
- PostgreSQL 14+

## Быстрый старт

### 1. Клонирование репозитория

```bash
git clone https://github.com/Smezhno/murdance.git
cd murdance
```

### 2. Настройка переменных окружения

```bash
cp .env.example .env
```

**Обязательные переменные:**
- `TELEGRAM_BOT_TOKEN` — токен Telegram бота
- `TELEGRAM_SECRET_TOKEN` — секретный токен для верификации webhook
- `ADMIN_TELEGRAM_CHAT_ID` — ID чата администратора
- `POSTGRES_PASSWORD` — пароль PostgreSQL
- `YANDEXGPT_API_KEY` — API ключ YandexGPT
- `YANDEXGPT_FOLDER_ID` — ID папки Yandex Cloud
- `CRM_TENANT` — имя тенанта Impulse CRM
- `CRM_API_KEY` — API ключ Impulse CRM

### 3. Запуск через Docker Compose

```bash
docker compose up -d
```

Это запустит все сервисы:
- `app` — FastAPI приложение (порт 8000)
- `worker` — фоновый процесс
- `postgres` — PostgreSQL база данных
- `caddy` — HTTPS прокси (порт 443)

### 4. Проверка работоспособности

```bash
curl http://localhost:8000/health
```

Должен вернуть:
```json
{
  "status": "healthy",
  "postgres": "healthy",
  "crm": "healthy",
  "pool": {}
}
```

## Разработка

### Установка зависимостей

```bash
pip install -e .
```

### Запуск локально (без Docker)

1. Запустите PostgreSQL локально или через Docker:
```bash
docker compose up postgres -d
```

2. Создайте `.env` файл с настройками

3. Запустите приложение:
```bash
uvicorn app.main:app --reload
```

### Структура проекта

```
murdance/
├── app/                    # Основное приложение
│   ├── ai/                 # LLM роутер, бюджет, политики
│   ├── channels/           # Адаптеры для Telegram/WhatsApp
│   ├── core/              # FSM, booking flow, intent resolution
│   ├── integrations/      # Интеграции (Impulse CRM)
│   ├── knowledge/         # База знаний студии
│   ├── storage/           # PostgreSQL клиент, миграции, session store
│   ├── config.py          # Конфигурация
│   ├── models.py          # Pydantic модели
│   └── main.py            # FastAPI приложение
├── worker/                # Фоновый процесс (Phase 3)
├── knowledge/              # YAML файлы базы знаний
├── tests/                  # Тесты
│   └── prompt_regression/ # Регрессионные тесты промптов
├── docker-compose.yml     # Docker Compose конфигурация
├── Dockerfile             # Docker образ для app
├── Dockerfile.worker      # Docker образ для worker
├── CONTRACT.md            # Техническое задание
├── CURSOR_PLAYBOOK.md     # План разработки
└── pyproject.toml         # Зависимости проекта
```

## API Endpoints

### `POST /webhook/telegram`
Webhook для получения сообщений от Telegram.

### `GET /health`
Проверка состояния сервисов. Возвращает статус PostgreSQL, CRM и пул соединений.

### `POST /debug` (только в TEST_MODE)
Отладочный endpoint для тестирования booking flow.

## Тестирование

### Запуск регрессионных тестов промптов

```bash
python -m tests.prompt_regression.runner
```

Тесты проверяют:
- Booking flow (happy path, 6 шагов)
- Schedule queries (различные сценарии)
- Edge cases (коррекция, смена темы, прошлые даты)

**Требования:** ≥ 90% тестов должны проходить (CONTRACT §21).

## Конфигурация

Все настройки задаются через переменные окружения в файле `.env`. См. `.env.example` для полного списка переменных.

### Основные настройки

- **Telegram**: токен бота, секретный токен, ID админа
- **CRM**: тенант и API ключ Impulse CRM
- **LLM**: API ключ и folder ID YandexGPT
- **База данных**: параметры подключения к PostgreSQL
- **Budget Guard**: лимиты токенов, стоимости, запросов

## Особенности

- **Idempotency**: защита от дублирования записей через PostgreSQL INSERT ON CONFLICT
- **Fallback queue**: автоматическая очередь при ошибках CRM
- **Circuit breaker**: защита от каскадных сбоев внешних сервисов
- **Budget Guard**: автоматическое отключение LLM при превышении лимитов
- **Policy Enforcer**: жесткие правила для LLM (требование tool calls для booking)
- **Temporal Parser**: парсинг относительных дат в коде (не через LLM)

## Состояние проекта

**Phase 2.4 завершена:**
- ✅ Telegram канал
- ✅ FSM с slot filling
- ✅ LLM роутер с YandexGPT
- ✅ Интеграция с Impulse CRM
- ✅ Booking flow с idempotency
- ✅ Prompt regression тесты

**В разработке (Phase 3):**
- Cancel flow
- Human handoff
- Session recovery
- Degradation levels

## Лицензия

Проект разрабатывается для студии танцев Tatyana's Studio.

## Контакты

- Репозиторий: https://github.com/Smezhno/murdance
- Документация: см. `CONTRACT.md` и `RFC_REFERENCE.md`
