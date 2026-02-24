# RFC-003: Conversation Engine v2 — LLM-Driven Slot Tracker с Guardrails

**Замена жёсткого FSM на управляемый LLM диалог с детерминированными ограничениями**

---

| Поле | Значение |
|------|----------|
| **Проект** | DanceBot Agent / Murdance |
| **Версия RFC** | 2.0 |
| **Дата** | 23 февраля 2026 |
| **Автор** | Александр (при участии Claude) |
| **Статус** | Draft — требует ревью перед началом работ |
| **Зависимости** | CONTRACT.md v1.2, RFC_REFERENCE.md v0.4.0, **RFC-002 (уже реализован)** |
| **Заменяет** | CONTRACT.md §7 (FSM), RFC_REFERENCE.md §7-8 (FSM + Intent Resolution) |
| **Не затрагивает** | CRM-интеграция, Budget Guard, Idempotency, Outbound Queue, Security, Observability, VPN-прокси |

---

## 1. Проблема

Текущий детерминированный FSM на 13 состояний создаёт ботоподобный UX, который проигрывает живому администратору. Бот не продаёт — бот заполняет форму.

**Симптомы (из реальной переписки 23.02.2026):**

1. Бот вываливает весь список направлений вместо помощи с выбором
2. Показывает расписание без фильтрации по направлению/филиалу/уровню
3. Зацикливается: «Когда тебе удобно?» × 3, вместо того чтобы предложить конкретную дату
4. Не спрашивает про филиал и уровень подготовки — ключевые фильтры для подбора группы
5. Подтверждение записи не содержит адрес и информацию «что с собой брать»
6. Нет механизма помощи с выбором направления через бинарные вопросы
7. Нет upsell-сценариев (акции, абонементы) после записи

**Корневая причина:** FSM привязывает конкретный ответ к конкретному состоянию. Живой администратор адаптируется к контексту — FSM не может.

**Бизнес-эффект:** Конверсия бота ниже, чем у живого администратора. Это блокирует масштабирование — клиенты не будут платить за бота, который хуже человека.

**Решение:** Мини-пивот. Полная замена FSM-оркестратора на LLM-driven engine. Без A/B теста — текущий UX неприемлем.

---

## 2. Текущее состояние системы (post RFC-002)

RFC-002 уже реализован. Важно зафиксировать, от чего отталкиваемся:

### 2.1 Инфраструктура (не меняется)

| Компонент | Состояние | Где |
|-----------|-----------|-----|
| PostgreSQL — единственное хранилище | ✅ Работает | Redis удалён |
| Docker: 4 контейнера (app, worker, postgres, caddy) | ✅ Работает | docker-compose.yml |
| VPN-прокси для Telegram | ✅ Работает | channels/telegram_proxy.py |
| Session store на PostgreSQL | ✅ Работает | storage/session_store.py |
| Idempotency на PostgreSQL | ✅ Работает | core/idempotency.py |
| Outbound queue на PostgreSQL | ✅ Спроектирован | SKIP LOCKED polling |
| Session recovery (CONTRACT §20) | ✅ Работает | conversation.py → recover_stale_sessions() |
| Budget Guard | ✅ Работает | ai/budget_guard.py |
| CRM Impulse adapter (8 функций) | ✅ Работает | integrations/impulse/ |
| Prompt regression (3 suite, 20 тестов) | ✅ Работает | tests/prompt_regression/ |

### 2.2 Текущая модель данных — `SlotValues`

```python
# Текущая модель из app/models.py
class SlotValues(BaseModel):
    group: str | None = None              # Направление
    datetime_resolved: datetime | None = None  # Абсолютная дата/время
    datetime_raw: str | None = None       # Сырой текст даты от пользователя
    client_name: str | None = None
    client_phone: str | None = None
    teacher: str | None = None
    schedule_id: int | str | None = None
    messages: list[dict] = []             # История (last 10)
    
    # Cancel flow
    cancel_bookings: list[dict] = []
    selected_reservation_id: int | str | None = None
```

### 2.3 Текущий код, который затрагивается

| Файл | Строк | Роль | Судьба |
|------|-------|------|--------|
| `core/booking_flow.py` | ~250 | FSM-роутер: state → handler | 🔴 Заменяется engine.py |
| `core/conversation.py` | ~180 | Session CRUD + FSM transitions | 🟡 Session CRUD остаётся, FSM transitions упрощаются |
| `core/fsm.py` | ~100 | ConversationState enum + can_transition матрица | 🔴 Удаляется |
| `core/slot_collector.py` | ~150 | Сбор слотов через LLM | 🟡 Логика переезжает в промпт |
| `core/intent.py` | ~120 | resolve_intent через LLM | 🟡 Упрощается — intent часть LLM response |
| `core/schedule_flow.py` | ~250 | Расписание из CRM + форматирование | ✅ Без изменений, используется engine |
| `core/cancel_flow.py` | ~170 | Отмена записей | ✅ Без изменений, вызывается из engine |
| `core/booking_confirm.py` | ~150 | Подтверждение + receipt + CRM booking | 🟡 Receipt обогащается (адрес, dress code) |
| `core/response_generator.py` | ~100 | LLM-генерация ответов по шаблонам | 🟡 Расширяется |
| `core/temporal.py` | ~80 | Парсинг дат | ✅ Без изменений |

---

## 3. Архитектура v2: Slot Tracker + LLM + Guardrails

### 3.1 Принцип

```
┌─────────────────────────────────────────────────────┐
│              Conversation Engine v2                   │
│                                                       │
│  ┌────────────┐   ┌─────────────┐   ┌────────────┐  │
│  │ Slot Tracker│◄─►│ LLM Engine  │◄─►│ Guardrails │  │
│  │ (SlotValues │   │ (Prompt +   │   │ (Python)   │  │
│  │  extended)  │   │  Context)   │   │            │  │
│  └──────┬─────┘   └──────┬──────┘   └─────┬──────┘  │
│         │                │                 │          │
│         ▼                ▼                 ▼          │
│  ┌────────────┐   ┌────────────┐   ┌────────────┐   │
│  │ CRM/KB     │   │ schedule   │   │ cancel     │   │
│  │ Data Layer │   │ _flow.py   │   │ _flow.py   │   │
│  └────────────┘   └────────────┘   └────────────┘   │
└─────────────────────────────────────────────────────┘
```

**LLM** ведёт диалог свободно. **Slot Tracker** отслеживает прогресс. **Guardrails** блокируют опасные действия. **Существующие модули** (schedule_flow, cancel_flow, booking_confirm) переиспользуются как есть.

### 3.2 Что меняется, что остаётся

| Компонент | Статус | Пояснение |
|-----------|--------|-----------|
| `core/fsm.py` (ConversationState, can_transition) | 🔴 **Удаляется** | → ConversationPhase в slot_tracker.py |
| `core/booking_flow.py` (FSM-роутер) | 🔴 **Заменяется** | → engine.py |
| `core/slot_collector.py` | 🔴 **Удаляется** | Логика сбора слотов уходит в LLM промпт |
| `core/intent.py` | 🔴 **Удаляется** | Intent — часть LLM structured response |
| `core/conversation.py` | 🟡 **Упрощается** | Session CRUD остаётся, FSM-методы адаптируются |
| `core/booking_confirm.py` | 🟡 **Обогащается** | Receipt: + адрес филиала, + dress code |
| `core/response_generator.py` | 🟡 **Расширяется** | Новые шаблоны промптов |
| `core/schedule_flow.py` | ✅ Без изменений | Вызывается из engine через tool_call |
| `core/cancel_flow.py` | ✅ Без изменений | Вызывается из engine при intent=cancel |
| `core/temporal.py` | ✅ Без изменений | |
| `core/idempotency.py` | ✅ Без изменений | |
| CRM Adapter | ✅ Без изменений | |
| Budget Guard | ✅ Без изменений | |
| Policy Enforcer | ✅ **Усиливается** | Новые guardrail-проверки |
| Telegram channel + VPN proxy | ✅ Без изменений | |
| PostgreSQL storage (RFC-002) | ✅ Без изменений | |
| Prompt regression tests | 🟡 **Расширяются** | Новые тест-кейсы на UX-качество |

---

## 4. Модель данных: расширение SlotValues

### 4.1 Новые поля (обратно-совместимое расширение)

```python
class SlotValues(BaseModel):
    """Расширенная модель слотов. Новые поля имеют значения по умолчанию —
    существующие сессии в PostgreSQL десериализуются без ошибок."""
    
    # === Существующие поля (без изменений) ===
    group: str | None = None              # Направление (style_name)
    datetime_resolved: datetime | None = None
    datetime_raw: str | None = None
    client_name: str | None = None
    client_phone: str | None = None
    teacher: str | None = None
    schedule_id: int | str | None = None
    messages: list[dict] = []
    cancel_bookings: list[dict] = []
    selected_reservation_id: int | str | None = None
    
    # === Новые поля (Phase v2) ===
    branch: str | None = None             # Филиал: "Семёновская", "Гоголя", "Чуркин", "Алеутская"
    experience: str | None = None         # "новичок" | "продолжающий" | None
    schedule_shown: bool = False          # Расписание уже показано клиенту
    summary_shown: bool = False           # Резюме показано, ждём подтверждения
    confirmed: bool = False               # Явное "да"
    booking_created: bool = False         # Запись создана в CRM
    receipt_sent: bool = False            # Receipt отправлен
```

### 4.2 ConversationPhase — замена ConversationState

```python
class ConversationPhase(str, Enum):
    """Фаза вычисляется из слотов. Не хранится — вычисляется на лету."""
    
    GREETING = "greeting"           # Нет слотов
    DISCOVERY = "discovery"         # Собираем branch/style/experience
    SCHEDULE = "schedule"           # Фильтры есть → показываем расписание
    COLLECTING_CONTACT = "contact"  # Дата выбрана → ФИО + телефон
    CONFIRMATION = "confirmation"   # Все данные → резюме + ждём "да"
    BOOKING = "booking"             # Подтверждено → CRM lock
    POST_BOOKING = "post_booking"   # Записан → receipt
    
    # === Делегируемые (логика в существующих модулях) ===
    CANCEL_FLOW = "cancel_flow"     # → cancel_flow.py (без изменений)
    ADMIN_HANDOFF = "admin_handoff" # → escalation.py (без изменений)


def compute_phase(slots: SlotValues, is_cancel: bool = False, is_admin: bool = False) -> ConversationPhase:
    """Детерминированное вычисление фазы из слотов."""
    if is_cancel:
        return ConversationPhase.CANCEL_FLOW
    if is_admin:
        return ConversationPhase.ADMIN_HANDOFF
    if slots.receipt_sent:
        return ConversationPhase.POST_BOOKING
    if slots.booking_created:
        return ConversationPhase.POST_BOOKING
    if slots.confirmed:
        return ConversationPhase.BOOKING
    if slots.summary_shown:
        return ConversationPhase.CONFIRMATION
    if slots.client_name and slots.client_phone and slots.datetime_resolved:
        return ConversationPhase.CONFIRMATION  # Готовы показать резюме
    if slots.datetime_resolved or slots.schedule_shown:
        return ConversationPhase.COLLECTING_CONTACT
    if slots.branch and slots.group:
        return ConversationPhase.SCHEDULE
    if slots.branch or slots.group or slots.experience:
        return ConversationPhase.DISCOVERY
    return ConversationPhase.GREETING
```

### 4.3 Совместимость с PostgreSQL sessions

Таблица `sessions` хранит `slots` как JSONB. Новые поля имеют дефолты → старые сессии десериализуются корректно. Поле `fsm_state` в таблице больше не используется для роутинга (фаза вычисляется из слотов), но сохраняется для логирования и session recovery. При записи — `fsm_state = compute_phase(slots).value`.

---

## 5. Системный промпт — Sales Intelligence

### 5.1 Структура

```
[SYSTEM PROMPT]
├── Роль и тон
├── Правила продажи (из скрипта Татьяны)  
├── Текущее состояние слотов (JSON)
├── Доступные данные (KB excerpt + CRM schedule cache)
├── Доступные инструменты (tools)
├── Ограничения (что нельзя)
└── Формат ответа (structured JSON)
```

### 5.2 Роль и тон

```
Ты — администратор танцевальной студии "She Dance" во Владивостоке.
Ты общаешься в мессенджере с потенциальным клиентом.

Тон:
- Тёплый, заботливый, как подруга которая работает в студии
- Без натужных смайликов (максимум 1-2 на сообщение, не в каждом)
- Без канцеляризма: "оформление записи" → "запишу тебя"
- Адаптируй обращение: если клиент на "вы" — отвечай на "вы"
- Короткие сообщения: до 300 символов (Telegram)
- Никогда не говори "Как AI", "Как бот", "Как языковая модель"
```

### 5.3 Правила продажи (из скрипта Татьяны)

```
ПРАВИЛА ВЕДЕНИЯ ДИАЛОГА:

1. ОДИН ВОПРОС ЗА РАЗ
   ❌ "Какой филиал и направление вас интересует?"
   ✅ "В каком филиале удобнее заниматься?"

2. ПОРЯДОК СБОРА (если клиент не дал информацию сам):
   Филиал → Направление → Опыт → [показать расписание] → Дата → ФИО/Телефон → Подтверждение
   Если клиент дал несколько данных сразу — принимай все, не переспрашивай.

3. ПОМОЩЬ С ВЫБОРОМ НАПРАВЛЕНИЯ
   Если клиент не знает что выбрать:
   - Спроси: "Хотите что-то женственное на каблуках или энергичное в кроссовках?"
   - Каблуки → High Heels, Frame Up Strip
   - Кроссовки → Girly Hip-Hop, Dancehall
   - Дай краткое описание 1-2 подходящих (из KB)
   НЕ ВЫВАЛИВАЙ ВЕСЬ СПИСОК.

4. РАСПИСАНИЕ — ФИЛЬТРУЙ
   - Сначала собери направление + филиал (+ опыт для подбора уровня)
   - Потом вызови get_filtered_schedule
   - Группируй по преподавателю
   - Сразу предложи ближайшую дату пробного

5. ПРЕДЛАГАЙ КОНКРЕТНУЮ ДАТУ
   ❌ "Когда тебе удобно прийти?" (клиент не знает ваше расписание)
   ✅ "Ближайшее пробное у Кати — пятница, 28 февраля, 19:00. Запишем?"
   Если "нет" — предложи следующий вариант.

6. ПОСЛЕ ЗАПИСИ — ОБЯЗАТЕЛЬНО:
   - Адрес филиала (из KB: branches)
   - Что взять с собой (из KB: dress_code по направлению)
   
7. ЕСЛИ НЕТ МЕСТ:
   - "В этой группе нет мест."
   - Предложи: лист ожидания ИЛИ другую группу
   - "Подберём другую группу?"

8. ЕСЛИ КЛИЕНТ СОМНЕВАЕТСЯ ("подумаю", "дорого"):
   - Не давить
   - "Что смущает? Может, подберём другое?"
   - Предложи просто прийти на пробное

9. НЕ ПОВТОРЯЙСЯ
   Если расписание уже показано — не показывай снова.
   Предложи конкретную дату.

10. ЗАПРОС ЭСКАЛАЦИИ
    "Позовите человека", жалоба, возврат → эскалация к администратору.
```

### 5.4 Контекст слотов (инжектируется динамически)

```json
{
  "phase": "discovery",
  "collected": {
    "branch": null,
    "group": "High Heels",
    "experience": "новичок",
    "datetime_resolved": null,
    "teacher": null,
    "client_name": null,
    "client_phone": null
  },
  "missing": ["branch", "datetime_resolved", "client_name", "client_phone"],
  "schedule_shown": false,
  "history_summary": "Клиент интересуется High Heels, новичок."
}
```

### 5.5 Инструменты (Tools)

LLM вызывает tools, engine исполняет их через существующие модули:

```python
TOOLS = [
    {
        "name": "get_filtered_schedule",
        "description": "Получить расписание с фильтрами. Вызывай ПЕРЕД показом расписания.",
        "parameters": {
            "style": "Направление (опционально)",
            "branch": "Филиал (опционально)", 
            "teacher": "Преподаватель (опционально)"
        },
        "implementation": "schedule_flow.generate_schedule_response()"
    },
    {
        "name": "search_kb",
        "description": "Поиск в базе знаний: цены, FAQ, описания направлений, адреса, dress code",
        "parameters": {"query": "Текст запроса"},
        "implementation": "kb.search()"
    },
    {
        "name": "create_booking",
        "description": "Создать запись в CRM. ТОЛЬКО после явного подтверждения клиентом.",
        "parameters": {
            "schedule_id": "ID занятия из расписания",
            "client_name": "ФИО",
            "client_phone": "Телефон"
        },
        "implementation": "booking_confirm.confirm_booking()"
    },
    {
        "name": "start_cancel_flow",
        "description": "Начать процесс отмены записи",
        "parameters": {},
        "implementation": "cancel_flow.start()"
    },
    {
        "name": "escalate_to_admin",
        "description": "Передать диалог администратору",
        "parameters": {"reason": "Причина"},
        "implementation": "escalation relay"
    }
]
```

### 5.6 Формат ответа LLM (structured output)

```python
class LLMResponse(BaseModel):
    """LLM возвращает JSON с этой структурой."""
    
    message: str                           # Текст клиенту
    slot_updates: dict[str, Any] = {}      # {"branch": "Семёновская", "experience": "новичок"}
    tool_calls: list[ToolCall] = []        # Запросы инструментов
    intent: str = "continue"               # "continue"|"booking"|"cancel"|"escalate"|"info"

class ToolCall(BaseModel):
    name: str
    parameters: dict[str, Any] = {}
```

---

## 6. Guardrails — Детерминированные проверки

### 6.1 Таблица проверок

| # | Проверка | Тип | Когда | Действие |
|---|---------|-----|-------|----------|
| G1 | Расписание в ответе = данные CRM | 🔴 Hard | Ответ содержит время/дни | Блок → retry с данными CRM |
| G2 | Цена в ответе = данные KB | 🔴 Hard | Ответ содержит число+₽ | Блок → retry с данными KB |
| G3 | Все обязательные слоты перед booking | 🔴 Hard | intent=booking | Блок → запросить недостающие |
| G4 | Booking только после явного "да" | 🔴 Hard | tool_call=create_booking | Блок → спросить подтверждение |
| G5 | Нет сравнения преподавателей | 🔴 Hard | Всегда | Блок → retry |
| G6 | Tool failed → "уточню у администратора" | 🔴 Hard | Tool error | Подмена ответа |
| G7 | Длина ≤ 300 символов (TG) | 🟡 Auto-fix | Всегда | Обрезка |
| G8 | ≤ 2 эмодзи | 🟡 Auto-fix | Всегда | Удаление лишних |
| G9 | Receipt: адрес + dress code | 🔴 Hard | phase=POST_BOOKING | Дополнение из KB |
| G10 | schedule_id существует в CRM | 🔴 Hard | tool_call=create_booking | Блок → актуальное расписание |
| G11 | Дата в будущем | 🔴 Hard | slot_update datetime | Блок → предложить следующую |
| G12 | schedule/price в ответе → был tool_call | 🔴 Hard | Ответ содержит расписание | Блок → force tool_call |

### 6.2 Retry-логика

```python
MAX_GUARDRAIL_RETRIES = 2

async def generate_with_guardrails(session, user_message, context):
    for attempt in range(MAX_GUARDRAIL_RETRIES + 1):
        llm_response = await llm_engine.generate(session, user_message, context)
        result = await run_guardrails(llm_response, session.slots, context)
        
        if result.passed:
            return llm_response
        
        if attempt < MAX_GUARDRAIL_RETRIES:
            # Retry с описанием нарушений
            context.add_system_note(f"Нарушения: {result.violations}. Исправь.")
        else:
            return safe_fallback_response(session.slots)
```

---

## 7. Conversation Engine — главный цикл

### 7.1 Поток обработки

```
Inbound Message
     │
     ▼
[1] Dedup + Filter (без изменений)
     │
     ▼
[2] Load Session (session_store.get_session — PostgreSQL)
     │
     ▼
[3] Phase = compute_phase(slots)
     │
     ├── CANCEL_FLOW → delegate to cancel_flow.py (без изменений)
     ├── ADMIN_HANDOFF → delegate to escalation.py (без изменений)
     │
     ▼
[4] Build LLM Context:
    - System prompt (role + rules)
    - Current slots JSON
    - Conversation history (slots.messages, last 10)
    - KB excerpt (branch addresses, style descriptions, prices, active promos)
    - CRM schedule cache (если phase >= SCHEDULE)
     │
     ▼
[5] LLM Generate → LLMResponse (structured JSON)
     │
     ▼
[6] Execute tool_calls:
    - get_filtered_schedule → schedule_flow.generate_schedule_response()
    - search_kb → kb.search()
    - create_booking → booking_confirm.confirm_booking()
    - start_cancel_flow → cancel_flow.start()
    - escalate → escalation relay
     │
     ▼
[7] Guardrails check
    ├── PASS → [8]
    └── FAIL → retry [5] (max 2) → safe fallback
     │
     ▼
[8] Apply slot_updates to session.slots
     │
     ▼
[9] Save session (session_store.save_session — PostgreSQL)
     │
     ▼
[10] Send response → Outbound Queue (PostgreSQL)
```

### 7.2 Делегирование в существующие модули

| Условие | Делегат | Изменения в делегате |
|---------|---------|---------------------|
| intent="cancel" ИЛИ phase=CANCEL_FLOW | `cancel_flow.py` | Нет. Работает как есть. |
| intent="escalate" ИЛИ phase=ADMIN_HANDOFF | `escalation.py` | Нет. |
| tool_call=create_booking | `booking_confirm.confirm_booking()` | Receipt обогащается: +адрес, +dress code из KB |
| tool_call=get_filtered_schedule | `schedule_flow.generate_schedule_response()` | Нет. |
| Подтверждение (phase=CONFIRMATION, текст="да") | `booking_confirm.confirm_booking()` | Без изменений |

### 7.3 Файловая структура

```
app/core/
├── engine.py              # NEW — ConversationEngine (главный оркестратор, ~200 строк)
├── slot_tracker.py        # NEW — compute_phase(), SlotValues extensions (~80 строк)
├── guardrails.py          # NEW — GuardrailRunner, 12 проверок (~150 строк)
├── prompt_builder.py      # NEW — Сборка системного промпта (~100 строк)
├── conversation.py        # SIMPLIFIED — Session CRUD (удалить FSM routing)
├── schedule_flow.py       # UNCHANGED
├── cancel_flow.py         # UNCHANGED  
├── booking_confirm.py     # ENRICHED — receipt + address + dress code
├── response_generator.py  # EXTENDED — новые промпт-шаблоны
├── temporal.py            # UNCHANGED
├── idempotency.py         # UNCHANGED
├── escalation.py          # UNCHANGED
│
├── fsm.py                 # DELETED
├── booking_flow.py        # DELETED (заменён engine.py)
├── slot_collector.py      # DELETED (логика в промпте)
├── intent.py              # DELETED (intent в LLM response)
```

---

## 8. Knowledge Base — дополнения

### 8.1 Новые секции в studio.yaml

```yaml
# Добавить в knowledge/studio.yaml

branches:
  - id: "semenovskaya"
    name: "Семёновская"
    address: "Семёновская 30а (стеклянное здание, крайняя дверь справа)"
    styles: ["high_heels", "frame_up_strip", "girly_hiphop", "vogue"]
    
  - id: "gogolya"
    name: "Гоголя"
    address: "Красного Знамени 59, 8 этаж (после лифта направо)"
    styles: ["high_heels", "frame_up_strip", "dancehall"]

  - id: "churkin"
    name: "Чуркин"
    address: "Черемуховая 40"
    styles: ["frame_up_strip", "girly_hiphop"]
    
  - id: "aleutskaya"
    name: "Алеутская"
    address: "Алеутская 28"
    styles: ["high_heels", "vogue", "dancehall"]

style_recommendations:
  # Используется промптом для помощи с выбором направления
  feminine_heels: ["high_heels", "frame_up_strip"]
  feminine_sneakers: ["girly_hiphop"]
  energetic: ["dancehall", "dancehall_female"]

dress_code:
  # Используется guardrail G9 для обогащения receipt
  high_heels: "Любая удобная форма, сменная обувь — носочки либо каблуки со светлой подошвой"
  frame_up_strip: "Любая удобная форма, сменная обувь — носочки либо каблуки со светлой подошвой"
  girly_hiphop: "Любая удобная форма, сменная обувь — кроссовки"
  dancehall: "Любая удобная форма, сменная обувь — кроссовки"
  vogue: "Любая удобная форма, сменная обувь — кроссовки либо каблуки"

# promotions: пока пустой — будет заполняться из CRM когда разберёмся с API
promotions: []
```

---

## 9. Совместимость с CONTRACT.md

| CONTRACT § | Правило | Реализация в v2 |
|------------|---------|-----------------|
| §4 | Bot MUST NOT invent schedule/prices | Guardrails G1, G2, G12 |
| §6 | One question per message | Системный промпт, правило 1 |
| §6 | Mandatory summary + confirmation | Guardrail G3, G4 + phase CONFIRMATION |
| §6 | Receipt: date/time/group/address | Guardrail G9 + KB dress_code |
| §6 | Response length ≤ 300 chars | Guardrail G7 |
| §7 | Slot filling with slot-skipping | Slot Tracker — нативно |
| §7 | Session 24h TTL | session_store (PostgreSQL) — без изменений |
| §7 | CONFIRM_BOOKING timeout 3h | compute_phase + session expiry — без изменений |
| §7 | Topic change mid-booking | LLM обрабатывает нативно |
| §7 | Message during booking → buffer | Lock во время CRM-запроса — без изменений |
| §9 | Outbound через queue | PostgreSQL outbound_queue — без изменений |
| §10 | Idempotency | idempotency.py (PostgreSQL) — без изменений |
| §11 | LLM forbidden actions | Guardrails G1-G12 |
| §12 | Budget Guard | Без изменений |
| §13 | Degradation | Без изменений |
| §14 | Human Handoff | cancel_flow.py + escalation — без изменений |
| §20 | Session recovery | conversation.py recover_stale_sessions() — адаптируется под phases |

---

## 10. Prompt Regression Tests — обновления

### 10.1 Новые тесты

```yaml
# tests/prompt_regression/test_sales_quality.yaml

- name: "Помощь с выбором направления"
  messages:
    - user: "Хочу на танцы, я новичок, не знаю что выбрать"
  expected:
    contains_one_of: ["каблук", "кроссовк", "женственн", "энергичн"]
    not_contains: ["High Heels, Girly Hip-Hop, Frame Up Strip, Dancehall"]

- name: "Предложение конкретной даты вместо 'когда удобно'"
  setup:
    slots: {group: "High Heels", branch: "Семёновская", experience: "новичок"}
  messages:
    - user: "Когда можно прийти?"
  expected:
    tool_calls: ["get_filtered_schedule"]
    contains_one_of: ["ближайш", "предлож", "можно подойти", "запишем"]
    not_contains: ["Когда тебе удобно"]

- name: "Без зацикливания"  
  setup:
    slots: {group: "High Heels", schedule_shown: true}
  messages:
    - user: "Когда можно прийти?"
  expected:
    not_contains: ["Расписание High Heels"]  # Не повторять
    contains_one_of: ["предлож", "запис"]

- name: "Receipt содержит адрес и dress code"
  setup:
    slots: {booking_created: true, group: "High Heels", branch: "Семёновская"}
  expected:
    contains: ["Семёновская 30а"]
    contains_one_of: ["каблуки", "носочки", "подошв"]

- name: "Один вопрос за раз"
  messages:
    - user: "Привет! Хочу записаться"
  expected:
    max_question_marks: 1

- name: "Нет мест — предложение альтернативы"
  setup:
    crm_mock: {no_spots: true}
  expected:
    contains: ["нет мест"]
    contains_one_of: ["лист ожидания", "другую группу", "другое время"]

- name: "Филиал спрашивается"
  messages:
    - user: "Хочу записаться на High Heels"
  expected:
    contains_one_of: ["филиал", "Гоголя", "Семёновская", "удобнее заниматься"]
```

### 10.2 Существующие 20 тестов

Прогоняются без изменений. Threshold ≥ 90% сохраняется.

---

## 11. Открытые вопросы

| # | Вопрос | Статус | Влияние |
|---|--------|--------|---------|
| OQ-V2-1 | A/B тест нужен? | ✅ **Решён: нет.** Полная замена. | — |
| OQ-V2-2 | Как определить ближайшую дату пробного из CRM? | ⏳ Запрос в поддержку Impulse. Пока тег/стикер — API не ясен. | Без этого бот не может предложить конкретную дату — **P0 blocker**. Workaround: показать расписание группы, LLM предлагает ближайший день. |
| OQ-V2-3 | Акции — откуда брать? | ⏳ Будут в CRM, механизм не определён. | Пока `promotions: []` в KB. Upsell-логика готова, активируется когда данные появятся. |
| OQ-V2-4 | Webhooks от Telegram приходят на российский IP? (OQ-9 из RFC-002) | ⏳ Нужно протестировать | Если нет — прокси для входящих |

**Workaround для OQ-V2-2 (ближайшая дата пробного):**

Пока API Impulse не отдаёт тег "пробное", бот:
1. Вызывает `get_filtered_schedule(style, branch)` — получает расписание группы
2. Из CRM-расписания берёт ближайший день недели, вычисляет конкретную дату через `temporal.py`
3. LLM формулирует: "Ближайшее занятие у Кати — среда, 26 февраля, 19:00. Запишем вас?"

Это не идеально (не все занятия открыты для пробных), но лучше чем "когда тебе удобно?" × 3.

---

## 12. Риски и митигация

| Риск | P | Митигация |
|------|---|-----------|
| LLM выдумает расписание | 🔴 | Guardrail G1 + G12 |
| LLM забудет собрать слот | 🟡 | compute_phase() + Guardrail G3 перед booking |
| LLM сломает тон | 🟡 | Prompt regression тесты на sales quality |
| Cost/диалог вырастет | 🟡 | Budget Guard. KPI: < $0.05/booking |
| Латентность | 🟡 | Pre-fetch CRM по фазе. Cache расписания. |
| Регрессия рабочих сценариев | 🔴 | Все 20 существующих тестов + 7 новых |
| cancel_flow/escalation сломаются | 🟢 | Не трогаем — делегируем как есть |
| Старые сессии в PG несовместимы | 🟢 | Новые поля с дефолтами. Pydantic десериализует. |

---

## 13. Метрики успеха

| Метрика | Сейчас | Цель v2 | Как измеряем |
|---------|--------|---------|--------------|
| Сообщений до записи | ~12 | ≤ 8 | Postgres booking_attempts |
| Повторные расписания в диалоге | 3 (из лога) | 0 | Prompt regression |
| Receipt с адресом + dress code | 0% | 100% | Guardrail G9 |
| Вопрос про филиал задан | 0% | 100% | Prompt regression |
| LLM cost per booking | ~$0.03 | < $0.05 | Budget Guard |
| Prompt regression pass rate | 90% | ≥ 90% | CI/CD (27 тестов) |

---

## 14. План внедрения

### Phase 3.1: Фундамент (3-4 дня)

```
Промпт 3.1.1 — SlotValues extension + compute_phase
- Расширить SlotValues в app/models.py (новые поля с дефолтами)
- Создать app/core/slot_tracker.py (ConversationPhase, compute_phase)
- Unit-тесты: compute_phase для каждой комбинации слотов

Промпт 3.1.2 — Prompt Builder
- Создать app/core/prompt_builder.py
- Сборка: role + rules + slots JSON + KB excerpt + tools
- Инжекция conversation history из slots.messages

Промпт 3.1.3 — Guardrails
- Создать app/core/guardrails.py (12 проверок из §6)
- Unit-тесты на каждую проверку

Промпт 3.1.4 — KB дополнения
- Добавить branches, dress_code, style_recommendations в studio.yaml
- Расширить валидацию KB при старте
```

### Phase 3.2: Engine + Интеграция (3-4 дня)

```
Промпт 3.2.1 — Conversation Engine
- Создать app/core/engine.py
- Интеграция: slot_tracker + prompt_builder + guardrails + llm_router
- Делегирование в cancel_flow.py и escalation.py

Промпт 3.2.2 — Подключение к main.py
- Заменить BookingFlow на ConversationEngine в webhook handler
- Адаптировать conversation.py (удалить FSM-routing, оставить Session CRUD)

Промпт 3.2.3 — Обогащение receipt
- booking_confirm.py: добавить адрес филиала и dress code из KB
- Guardrail G9: проверка наличия этих полей

Промпт 3.2.4 — Удаление старого кода
- Удалить: fsm.py, booking_flow.py, slot_collector.py, intent.py
- Убрать импорты и зависимости
```

### Phase 3.3: Тесты + Polish (2-3 дня)

```
Промпт 3.3.1 — Prompt Regression
- 7 новых тестов из §10.1
- Прогон всех 27 тестов

Промпт 3.3.2 — Тестирование с Татьяной
- Deploy
- 20+ сценариев из скриптов Татьяны
- Корректировка промпта по обратной связи
```

**Итого: ~8-11 рабочих дней**

---

*RFC-003 v2.0 — Draft. Основано на реальном состоянии кодовой базы post RFC-002. Требует ревью перед началом работ.*
