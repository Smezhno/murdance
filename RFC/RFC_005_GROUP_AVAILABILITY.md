# RFC-005: Group Availability — Sticker-based enrollment control

**Детерминированный слой доступности групп на основе стикеров из Impulse CRM**

---

| Поле | Значение |
|------|----------|
| **Версия** | 0.2.0 |
| **Дата** | 2 марта 2026 |
| **Автор** | Александр + Claude |
| **Статус** | Draft — ожидает проверки API |
| **Зависимости** | RFC-003 (engine.py), RFC-004 (entity resolver), Impulse CRM schedule/addition API |
| **Scope** | Проверка доступности группы для записи + альтернативные предложения |

### Changelog

- **v0.1** — первоначальный draft (предполагал sticker/list или вложенный в schedule)
- **v0.2** — переработано на основе реального API: schedule/addition endpoint, шаблоны vs конкретные даты, ScheduleExpander

---

## 1. Проблема

Бот записывает клиента в любую группу из расписания, не проверяя, открыта ли запись. В CRM администратор управляет доступностью через стикеры — цветные метки, привязанные к конкретным датам конкретных занятий. Без учёта стикеров бот может записать клиента в закрытую группу → конфликт, ручная отмена, потеря доверия.

## 2. Цель

Перед записью проверять доступность конкретной даты занятия. Если запись закрыта — предлагать ближайшую открытую дату или альтернативную группу. Архитектура должна работать с любой CRM, не только с Impulse.

## 3. Не в scope

- Автоматическая покупка абонементов
- Уведомления при смене стикера (нет webhook от CRM)
- UI для управления стикерами (это в CRM)
- Массовая синхронизация всех стикеров (pull on demand)

---

## 4. Модель данных Impulse CRM

### 4.1 Два источника расписания

**Нумерация дней Impulse:** `day: 1=Пн, 2=Вт, 3=Ср, 4=Чт, 5=Пт, 6=Сб, 7=Вс` (не совпадает с Python weekday 0=Пн).

```
schedule/list          → ШАБЛОНЫ (recurring)
                         regular: true, day: 5, minutesBegin: 1170
                         "Каждую пятницу 19:30" (day=5 = Пт)
                         БЕЗ стикеров

schedule/addition      → СТИКЕРЫ по конкретным датам
                         date: 1772668800 (конкретная дата)
                         schedule: {полный объект шаблона}
                         name: "Нельзя присоединиться"
                         color: "cc4125", icon: "ban"
                         entity: "sticker"
```

### 4.2 schedule/addition endpoint

```
POST https://{tenant}.impulsecrm.ru/api/public/schedule/addition
Authorization: Basic {base64(api_key)}
Content-Type: application/json
Body: {"date": 1772668800}  // TODO: проверить точный формат
```

**Response:**

```json
{
  "total": 1,
  "items": [
    {
      "date": 1772668800,
      "dateBegin": 1771459200,
      "dateEnd": null,
      "period": null,
      "showClient": true,
      "schedule": {
        "id": 117,
        "entity": "schedule",
        "group": {"id": 5, "name": "Frame Up Strip"},
        "hall": {"id": 1, "name": "Зал 1"},
        "branch": {"id": 1, "name": "Гоголя"},
        "day": 5,
        "minutesBegin": 1170,
        "minutesEnd": 1230,
        "regular": true
      },
      "branch": {"id": 1, "name": "Гоголя", "entity": "branch"},
      "id": 1984,
      "entity": "sticker",
      "name": "Нельзя присоединиться",
      "color": "cc4125",
      "icon": "ban"
    }
  ]
}
```

**Ключевые факты:**
- Один item = один стикер на одну дату для одного schedule
- `schedule.id` связывает стикер с шаблоном из schedule/list
- `date` — конкретная дата (Unix timestamp)
- На одну дату+schedule может быть несколько items (несколько стикеров)
- Endpoint в пути `/api/public/` — возможно публичный, проверить авторизацию

---

## 5. Архитектура

### 5.1 Два слоя

```
┌─────────────────────────────────────────┐
│  GroupAvailabilityProvider (Protocol)    │  ← CRM-agnostic interface
│  get_availability(schedule_id, date)    │
│  find_next_open(schedule_id, dates)     │
│  find_alternatives(style, branch, date) │
└──────────────┬──────────────────────────┘
               │ implements
┌──────────────▼──────────────────────────┐
│  ImpulseStickerProvider                 │  ← Impulse-specific
│  Fetches stickers via schedule/addition │
│  Maps sticker names → availability      │
└─────────────────────────────────────────┘
```

### 5.2 GroupAvailabilityProvider Protocol

```python
from enum import Enum
from dataclasses import dataclass
from datetime import date

class AvailabilityStatus(Enum):
    OPEN = "open"           # Можно записаться (нет стикера или стикер "МОЖНО")
    CLOSED = "closed"       # Нельзя присоединиться
    PRIORITY = "priority"   # Новая хореография — приоритетная дата
    INFO = "info"           # Информационный стикер (не влияет на запись)
    HOLIDAY = "holiday"     # Выходной / отмена

@dataclass
class GroupAvailability:
    schedule_id: int
    date: date
    status: AvailabilityStatus
    sticker_text: str | None = None   # Оригинальный текст для логирования
    note: str | None = None           # Доп. контекст для LLM

class GroupAvailabilityProvider(Protocol):
    async def get_availability(
        self, schedule_id: int, target_date: date
    ) -> GroupAvailability:
        """Доступность конкретного занятия на конкретную дату."""
        ...

    async def find_next_open(
        self, schedule_id: int, from_date: date, max_weeks: int = 4
    ) -> GroupAvailability | None:
        """Ближайшая открытая дата для этого schedule."""
        ...

    async def find_alternatives(
        self, style_id: int, branch_id: int | None, from_date: date,
        teacher_id: int | None = None,
    ) -> list[GroupAvailability]:
        """Альтернативные открытые группы того же направления."""
        ...
```

### 5.3 ImpulseStickerProvider

#### 5.3.1 Fetch стикеров

```python
async def _fetch_additions(self, target_date: date) -> list[dict]:
    """Fetch sticker additions for a specific date.

    Redis cache: impulse:cache:schedule:additions:{YYYY-MM-DD}, TTL 15min.
    """
    cache_key = f"additions:{target_date.isoformat()}"
    cached = await self._cache.get("schedule", cache_key)
    if cached is not None:
        return cached

    timestamp = int(datetime.combine(target_date, time.min,
                    tzinfo=ZoneInfo("Asia/Vladivostok")).timestamp())

    # TODO: проверить точный формат body (OQ-S1)
    response = await self._client._request(
        "POST", "public/schedule", "addition",
        {"date": timestamp}
    )
    data = response.json()
    items = data.get("items", [])

    await self._cache.set("schedule", items, cache_key)
    return items
```

#### 5.3.2 Классификация стикеров

Татьяна согласилась стандартизировать текст. Конфигурация в `studio.yaml`:

```yaml
availability:
  sticker_mapping:
    open_keywords: ["МОЖНО ПРИСОЕДИНИТЬСЯ"]
    closed_keywords: ["НЕЛЬЗЯ ПРИСОЕДИНИТЬСЯ", "НЕЛЬЗЬЯ ПРИСОЕДИНИТЬСЯ", "ЗАКРЫТО"]
    priority_keywords: ["НОВАЯ ХОРЕОГРАФИЯ"]
    holiday_keywords: ["ВЫХОДНОЙ"]
    info_keywords: ["СТАРТ", "ОТКРЫТЫЙ УРОК"]
    unknown_action: "open"
```

> **Примечание:** "НЕЛЬЗЬЯ ПРИСОЕДИНИТЬСЯ" — реальная опечатка из API. Keyword match по подстроке "НЕЛЬЗЯ" ловит оба варианта.

**Алгоритм (детерминированный, без LLM):**

```python
def classify_sticker(name: str, config: StickerMapping) -> AvailabilityStatus:
    name_upper = name.strip().upper()

    # Порядок: CLOSED первым (safety-first)
    for kw in config.closed_keywords:
        if kw.upper() in name_upper:
            return AvailabilityStatus.CLOSED
    for kw in config.holiday_keywords:
        if kw.upper() in name_upper:
            return AvailabilityStatus.HOLIDAY
    for kw in config.open_keywords:
        if kw.upper() in name_upper:
            return AvailabilityStatus.OPEN
    for kw in config.priority_keywords:
        if kw.upper() in name_upper:
            return AvailabilityStatus.PRIORITY
    for kw in config.info_keywords:
        if kw.upper() in name_upper:
            return AvailabilityStatus.INFO

    logger.warning("unknown_sticker", name=name)
    return AvailabilityStatus.OPEN  # default: open
```

#### 5.3.3 Множественные стикеры на одну дату

Приоритет: CLOSED/HOLIDAY > PRIORITY > OPEN > INFO.
CLOSED побеждает всё (safety-first).

#### 5.3.4 Кеширование

```
Redis key:   impulse:cache:schedule:additions:{YYYY-MM-DD}
TTL:         15 минут
Value:       JSON list items из schedule/addition response
```

---

## 6. Развёртывание шаблонов в конкретные даты

### 6.1 Проблема

schedule/list возвращает шаблоны: `{regular: true, day: 5, minutesBegin: 1170}`. Чтобы проверить стикер на пятницу 7 марта — нужно развернуть шаблон.

### 6.2 ScheduleExpander

```python
@dataclass
class ExpandedSlot:
    schedule_id: int
    date: date
    time_begin: time
    time_end: time
    group_name: str
    teacher_name: str | None
    branch_name: str | None
    style_id: int | None
    availability: AvailabilityStatus = AvailabilityStatus.OPEN

def expand_schedule(
    templates: list[Schedule],
    from_date: date,
    to_date: date,
) -> list[ExpandedSlot]:
    """Развернуть шаблоны schedule в конкретные даты. Impulse day 1=Пн → impulse_day_to_weekday()."""
    slots = []
    for tmpl in templates:
        if tmpl.regular and tmpl.day is not None:
            python_weekday = impulse_day_to_weekday(tmpl.day)  # 1-7 → 0-6
            current = from_date
            while current <= to_date:
                if current.weekday() == python_weekday:
                    slots.append(_make_slot(tmpl, current))
                current += timedelta(days=1)
        elif tmpl.date_begin:
            slot_date = date.fromtimestamp(tmpl.date_begin)
            if from_date <= slot_date <= to_date:
                slots.append(_make_slot(tmpl, slot_date))
    slots.sort(key=lambda s: (s.date, s.time_begin))
    return slots
```

### 6.3 Полный flow

```
1. schedule/list          → шаблоны (кеш 15 мин)
2. expand_schedule(...)   → конкретные даты на нужный период
3. schedule/addition      → стикеры для каждой уникальной даты (кеш 15 мин)
4. classify + resolve     → AvailabilityStatus на каждый слот
5. Показать клиенту / проверить перед записью
```

---

## 7. Интеграция в engine.py

### 7.1 Перед подтверждением записи

```python
if slots.confirmed and not slots.booking_created:
    avail = await self._availability.get_availability(
        slots.schedule_id, slots.target_date
    )
    if avail.status in (AvailabilityStatus.CLOSED, AvailabilityStatus.HOLIDAY):
        return await self._handle_closed_group(session, slots, avail)
```

### 7.2 В формировании расписания для LLM

```
  Пн 3 мар 19:30 | Стрип-пластика | Настя | Гоголя | ✅
  Ср 5 мар 20:00 | Хилс | Юлия | Семёновская | ❌ ЗАКРЫТО
  Пт 7 мар 19:30 | Фрейм ап стрип | Танцура Ю. | ⭐ НОВАЯ ХОРЕОГРАФИЯ
```

### 7.3 _handle_closed_group

```
1. find_next_open → ближайшая OPEN дата того же schedule
2a. Нашли → "На [дата] закрыта. Ближайшая — [дата]. Записать?"
2b. Не нашли → find_alternatives(style, branch)
   3a. Тот же филиал → предложить
   3b. Тот же педагог другой филиал → предложить
   3c. Другой филиал → предложить
   3d. Нет альтернатив → "Передать администратору?"
      → escalate_to_admin(reason="Лист ожидания: ...")
```

### 7.4 Guardrail G14

Блокирует `create_booking` для schedule с CLOSED/HOLIDAY статусом. Hard block — даже если LLM попытается записать.

---

## 8. Конфигурация в studio.yaml

```yaml
availability:
  sticker_mapping:
    open_keywords: ["МОЖНО ПРИСОЕДИНИТЬСЯ"]
    closed_keywords: ["НЕЛЬЗЯ ПРИСОЕДИНИТЬСЯ", "НЕЛЬЗЬЯ ПРИСОЕДИНИТЬСЯ", "ЗАКРЫТО"]
    priority_keywords: ["НОВАЯ ХОРЕОГРАФИЯ"]
    holiday_keywords: ["ВЫХОДНОЙ"]
    info_keywords: ["СТАРТ", "ОТКРЫТЫЙ УРОК"]
    unknown_action: "open"
  max_lookahead_weeks: 4
  alternative_priority:
    - same_style_same_branch
    - same_style_same_teacher_other_branch
    - same_style_other_branch
```

---

## 9. Файлы

| Файл | ~Строк | Описание |
|------|--------|----------|
| `app/core/availability/__init__.py` | 5 | Exports |
| `app/core/availability/protocol.py` | 50 | Protocol, AvailabilityStatus, GroupAvailability |
| `app/core/availability/classifier.py` | 60 | classify_sticker, resolve_multiple |
| `app/core/availability/impulse_provider.py` | 120 | ImpulseStickerProvider |
| `app/core/availability/schedule_expander.py` | 80 | expand_schedule, ExpandedSlot |
| `app/core/engine.py` | +40 | _handle_closed_group |
| `app/core/prompt_builder.py` | +20 | schedule статусы |
| `app/core/guardrails.py` | +15 | G14 |
| `app/integrations/impulse/adapter.py` | +30 | get_additions() |
| `knowledge/studio.yaml` | +15 | sticker_mapping |
| `tests/unit/test_classifier.py` | 60 | classify tests |
| `tests/unit/test_schedule_expander.py` | 50 | expand tests |
| `tests/unit/test_availability.py` | 80 | provider tests |

**~450 строк нового кода, 5 новых файлов.**

---

## 10. Блокирующие задачи

| # | Задача | Как проверить |
|---|--------|---------------|
| B1 | Формат body schedule/addition | curl с `{"date": unix_ts}`, потом `{"dateFrom": ..., "dateTo": ...}` |
| B2 | Путь: `/api/public/schedule/addition` или `/api/schedule/addition`? | curl обоих вариантов |
| B3 | Стандарт текста стикеров с Татьяной | Показать список, получить "ок" |
| B4 | schedule/list возвращает group.style_id, branch.id? | Проверить response |

---

## 11. Порядок реализации

```
Phase 1: Protocol + Classifier + tests           [~2ч]
Phase 2: ScheduleExpander + tests                 [~2ч]
Phase 3: ImpulseStickerProvider + adapter          [~3ч]
Phase 4: Engine integration + alternatives         [~3ч]
Phase 5: Integration tests + regression            [~2ч]
```

---

## 12. Открытые вопросы

| # | Вопрос | Статус |
|---|--------|--------|
| OQ-S1 | Точный формат body schedule/addition | 🔴 Блокер |
| OQ-S2 | public vs non-public path | 🔴 Блокер |
| OQ-S3 | Поддержка диапазона дат (dateFrom/dateTo)? | 🟡 |
| OQ-S4 | Rate limit на schedule/addition | 🟡 |
| OQ-S5 | "ОТКРЫТЫЙ УРОК" — open или info? | 🟢 |
| OQ-S6 | >1 стикер на один schedule+date? | 🟢 (handled) |

---

## 13. Риски

| Риск | P | Митигация |
|------|---|-----------|
| schedule/addition не фильтрует по дате → возвращает ВСЕ | 🟡 | Фильтровать в коде + aggressive cache |
| Татьяна забывает ставить стикеры | 🟡 | Default OPEN — лучше записать лишнего |
| Стикеры только на 1-2 недели | 🟡 | max_lookahead_weeks + "уточню у администратора" |
| Нестандартный текст стикера | 🟢 | Keyword substring match + UNKNOWN → log + open |
| API формат изменится | 🟢 | Pydantic strict + smoke test |
