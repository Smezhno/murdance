# RFC-004: Entity Resolver — Нормализация пользовательского ввода в CRM-идентификаторы

**Детерминированный слой между естественным языком и CRM API**

---

| Поле | Значение |
|------|----------|
| **Проект** | DanceBot Agent / Murdance |
| **Версия RFC** | 1.1 |
| **Дата** | 1 марта 2026 (v1.1: правки по ревью) |
| **Автор** | Александр (при участии Claude) |
| **Статус** | Draft — требует ревью перед началом работ |
| **Зависимости** | CONTRACT.md v1.2, RFC-003 (Conversation Engine v2), RFC_REFERENCE.md v0.4.0 |
| **Не затрагивает** | Budget Guard, Idempotency, Outbound Queue, Security, Observability, VPN-прокси, cancel_flow.py, temporal.py |

---

## 1. Проблема

Бот не может записать клиента на занятие в базовых сценариях. Из тестирования с Татьяной (1 марта 2026):

| Запрос пользователя | Ожидание | Факт | Корневая причина |
|---------------------|----------|------|------------------|
| "к Насте на хилс" | Бот находит занятия Анастасии Николаевой | "Занятий у Насти не нашлось" | `"настя" in "анастасия николаева"` → False |
| "на Гоголя" | Бот фильтрует расписание по филиалу | "На Гоголя нет расписания" | CRM не возвращает branch.name, либо название не совпадает с "Гоголя" |
| "гёрли" | Бот находит Girly Hip-Hop | Не матчится | Нет маппинга разговорных → CRM-названий |
| "хочу купить 2 абонемента" | Бот ведёт в flow покупки | Бот ведёт в flow записи на занятие | Intent не распознан: buy ≠ book |
| "на филиале Тест" | Бот предлагает филиал Тест | "У нас нет филиала Тест" | Тест есть в studio.branches, но не в KB branches |

**Все 5 багов сводятся к одной корневой причине:** отсутствует слой нормализации между человеческим языком и данными CRM/KB. Бот пытается матчить сырой текст напрямую с CRM-идентификаторами.

**Бизнес-эффект:** Бот не выполняет основную функцию — запись на занятие. Запуск с Татьяной заблокирован. Revenue = 0 пока эти баги не исправлены.

---

## 2. Масштаб проблемы и контекст масштабирования

### 2.1 Текущее состояние

- 1 студия (Татьяна), ~22 преподавателя с регулярной ротацией
- 4-5 филиалов, стабильные
- ~10-15 стилей (направлений), редко меняются

### 2.2 Почему ручные aliases не работают

22 преподавателя с ротацией означают, что ручное ведение aliases нежизнеспособно уже для первого клиента. Преподаватели приходят и уходят — админ не будет обновлять конфиг каждый раз.

### 2.3 Почему RAG не решает эту проблему

RAG (Retrieval-Augmented Generation) решает задачу **поиска информации** — семантический поиск по текстовым документам. Наша задача — **резолвинг сущностей в CRM-идентификаторы**: "Настя" → `teacher_id=123` для API-вызова.

RAG вернёт текстовый чанк "Анастасия Николаева ведёт High Heels", но не даст structured `teacher_id` для CRM API. EntityResolver даёт именно это.

RAG нужен для другой задачи (оптимизация KB-контекста в промпте) и будет реализован отдельно.

### 2.4 Почему NLP-библиотеки недостаточны

pymorphy3, natasha, rapidfuzz — решают **другие** задачи:
- pymorphy3: морфология ("Насте" → "Настя"), но НЕ знает что Настя = Анастасия
- natasha: NER (извлечение имён из текста), но связь уменьшительное → полное — неполная
- rapidfuzz: строковое сходство, но "Катя" vs "Екатерина" — низкий score, false negatives на самых частых кейсах

**Правильный инструмент:** словарь уменьшительных форм русских имён (конечный, стабильный датасет ~300 записей) + pymorphy3 только для нормализации падежа.

---

## 3. Решение: EntityResolver

### 3.1 Ключевой архитектурный принцип: LLM извлекает, Resolver нормализует

**EntityResolver НЕ парсит сырой текст пользователя.** Extraction сущностей из текста — задача LLM в engine.py (RFC-003). LLM извлекает raw-значения в `slot_updates`, EntityResolver нормализует их в CRM-идентификаторы.

```
Пользователь: "к Настюше на каблуки на Гоголя"
        │
        ▼
[engine.py] LLM slot extraction:
  slot_updates: {
    teacher_raw: "настюше",        ← LLM извлёк
    style_raw: "каблуки",          ← LLM извлёк
    branch_raw: "гоголя"           ← LLM извлёк
  }
        │
        ▼
[EntityResolver] Нормализация каждого raw-значения ОТДЕЛЬНО:

  resolve_teacher("настюше")
    → exact lookup: "настюше" — not found
    → pymorphy3("настюше") = "настюша"
    → exact lookup: "настюша" — found!
    → names_dict: "настюша" → "анастасия"
    → CRM lookup: "анастасия" → [{name: "Анастасия Николаева", crm_id: 123}]

  resolve_style("каблуки")
    → exact lookup: "каблуки" — found in aliases
    → {name: "High Heels", crm_id: 5}

  resolve_branch("гоголя")
    → exact lookup: "гоголя" — found in aliases
    → {name: "Гоголя", crm_id: 7}
        │
        ▼
CRM API: get_schedule(teacher_id=123, style_id=5, branch_id=7)
```

**Почему resolver не парсит текст:**
- LLM уже умеет извлекать сущности из текста — это его сильная сторона
- N-gram tokenization на русском языке ненадёжна и дорога
- Resolver получает **уже извлечённые** значения, каждое — одна сущность
- Если пользователь не упомянул сущность ("хочу на занятие завтра") — resolver не вызывается для неё, engine продолжает собирать слоты через LLM

**Resolver ДОПОЛНЯЕТ slot extraction, не ЗАМЕНЯЕТ:**
- LLM отвечает за: intent classification, extraction сырых значений из текста, генерацию ответа
- Resolver отвечает за: нормализацию "настюше" → crm_id=123 (детерминированно, без LLM)

### 3.2 Архитектура

```
┌─────────────────────────────────────────────────────────┐
│                    EntityResolver                         │
│                                                           │
│  ┌──────────────────┐  ┌────────────────┐  ┌──────────┐ │
│  │  TeacherResolver  │  │  BranchResolver │  │  Style   │ │
│  │                    │  │                 │  │  Resolver│ │
│  │  - names_dict.json │  │  - aliases from │  │          │ │
│  │    (статический)   │  │    studio.yaml  │  │  - alias │ │
│  │  - pymorphy3       │  │  - unknown_areas│  │    from  │ │
│  │    (fallback only) │  │                 │  │    yaml  │ │
│  │  - CRM teacher list│  │                 │  │          │ │
│  │    (при старте)    │  │                 │  │          │ │
│  └──────────────────┘  └────────────────┘  └──────────┘ │
│                                                           │
│  Вызывается из engine.py с PRE-EXTRACTED значениями:     │
│    resolve_teacher(teacher_raw, tenant_id) → list[...]    │
│    resolve_branch(branch_raw, tenant_id)  → list[...]    │
│    resolve_style(style_raw, tenant_id)    → list[...]    │
│                                                           │
│  НЕ парсит текст. НЕ делает tokenization.                │
│  Получает уже извлечённые LLM значения.                  │
└─────────────────────────────────────────────────────────┘
```

### 3.3 Ключевое решение: два типа сущностей

| Тип | Преподаватели | Филиалы и Стили |
|-----|---------------|-----------------|
| Количество | 22+, ротация | 4-5 / 10-15, стабильные |
| Источник правды | CRM (меняется) | KB конфиг (стабильный) |
| Резолвинг | Автоматический: словарь имён + CRM sync | Конфигурируемый: aliases в studio.yaml |
| Зависимости | pymorphy3 + static names_dict.json | Нет внешних зависимостей |

---

## 4. Детальный дизайн

### 4.1 TeacherResolver — автоматический резолвинг преподавателей

**Поток данных:**

```
1. При старте приложения:
   CRM API: teacher/list → ["Анастасия Николаева", "Екатерина Петрова", ...]
                                       │
   names_dict.json: {"анастасия": ["настя", "настена", "настюша", "ася"], ...}
                                       │
   AutoAliasBuilder: строит lookup таблицу
   {
     "настя": [{"full": "Анастасия Николаева", "crm_id": 123}],
     "настена": [{"full": "Анастасия Николаева", "crm_id": 123}],
     "настюша": [{"full": "Анастасия Николаева", "crm_id": 123}],
     "ася": [{"full": "Анастасия Николаева", "crm_id": 123}],
     "анастасия": [{"full": "Анастасия Николаева", "crm_id": 123}],
     "николаева": [{"full": "Анастасия Николаева", "crm_id": 123}],
     "катя": [{"full": "Екатерина Петрова", "crm_id": 456}],
     ...
   }

2. При запросе resolve_teacher("настюше"):
   pymorphy3("настюше") → "настюша"  (нормализация падежа)
   lookup["настюша"] → [{"full": "Анастасия Николаева", "crm_id": 123}]
   → ResolvedEntity(name="Анастасия Николаева", crm_id=123, confidence=1.0)
```

**Алгоритм resolve_teacher (exact → pymorphy3 fallback, NO fuzzy в MVP):**

```python
def resolve_teacher(self, raw: str, tenant_id: str) -> list[ResolvedEntity]:
    """
    Порядок поиска (от дешёвого к дорогому):
    
    1. Exact match: lowercase(raw) → lookup
       "настя" → found → return
       Стоимость: O(1), ~0.001ms
    
    2. pymorphy3 normal_form: только если exact не нашёл
       "настюше" → pymorphy3 → "настюша" → lookup
       Стоимость: ~0.1ms на одно слово
    
    3. Фамилия с нормализацией рода:
       "николаевой" → pymorphy3 → "николаева" → lookup
       Если CRM: "Николаев" — при build lookup добавляем
       и мужскую, и женскую форму фамилии
       Стоимость: ~0.1ms
    
    4. Если не найдено → пустой список
       Бот: "Не нашла преподавателя. Вот кто ведёт [направление]: ..."
    
    ❌ Fuzzy matching ОТКЛЮЧЁН в MVP.
       Причина: false positives опаснее false negatives.
       Добавим в Phase 4.2+ если реальные данные покажут необходимость.
    """
```

**Нормализация фамилий (решение P0-4):**

При построении lookup из CRM-данных, для каждого преподавателя добавляются обе формы фамилии:

```python
def _build_surname_aliases(self, full_name: str, crm_id: int) -> dict[str, list]:
    """
    CRM: "Анастасия Николаева"
    → lookup["николаева"] = [{full_name, crm_id}]
    → lookup["николаев"] = [{full_name, crm_id}]   # мужская форма
    
    CRM: "Дмитрий Козлов"  
    → lookup["козлов"] = [{full_name, crm_id}]
    → lookup["козлова"] = [{full_name, crm_id}]    # женская форма
    
    Для генерации второй формы: pymorphy3.parse(фамилия) → 
    если tag == femn → inflect({masc})
    если tag == masc → inflect({femn})
    """
```

**Обработка неоднозначностей:**

| Кейс | Пример | Поведение |
|------|--------|-----------|
| 1 совпадение | "Настя" → 1 Анастасия | Используем |
| >1 совпадение | "Настя" → 2 Анастасии | Бот: "У нас хилс ведут Анастасия Николаева и Анастасия Петрова. К кому записать?" |
| 0 совпадений | "Вася" | Бот: "Не нашла преподавателя. Вот кто ведёт [направление]: ..." |

**Фильтрация по контексту (Phase 4.2+, зависит от OQ-11):** Если известен style И CRM возвращает `teacher.styles[]`, resolver сужает список преподавателей до тех, кто ведёт этот стиль. **В MVP фильтрация по стилю НЕ реализуется** — если CRM не возвращает styles, бот уточняет из полного списка совпадений. Workaround: определять связь teacher↔style через schedule (кто ведёт занятия данного стиля).

### 4.2 names_dict.json — Словарь уменьшительных форм

**Формат:**

```json
{
  "александр": ["саша", "саня", "шура", "шурик", "алекс"],
  "анастасия": ["настя", "настена", "настенька", "настюша", "ася", "стася"],
  "екатерина": ["катя", "катюша", "катерина", "катенька"],
  "мария": ["маша", "машенька", "маруся", "маня"],
  "елена": ["лена", "леночка", "алёна"],
  "ольга": ["оля", "оленька", "олюшка"],
  "татьяна": ["таня", "танюша", "танечка"],
  "наталья": ["наташа", "наталия", "ната"],
  "ирина": ["ира", "ирочка", "иришка"],
  "светлана": ["света", "светочка"],
  "...": "~300 записей, покрывающих 95%+ русских имён"
}
```

**Источник:** Сгенерировать один раз, проверить вручную, положить в репо как статический файл. Это stable dataset — имена не меняются. Файл ~15KB.

**Инвертированный индекс** (строится при загрузке):

```json
{
  "настя": ["анастасия"],
  "настена": ["анастасия"],
  "катя": ["екатерина"],
  "саша": ["александр", "александра"],
  "...": "..."
}
```

**⚠️ Конфликты aliases:** Один alias может указывать на несколько canonical имён (например "саша" → "александр" и "александра"). Это **ожидаемо и корректно** — инвертированный индекс хранит `alias → list[canonical]`, не `alias → canonical`. При загрузке:
- Никаких silent overwrite — append к списку
- Валидация при старте: логировать WARNING для каждого alias с >1 canonical
- В runtime: если "саша" → 2 canonical, и в CRM есть оба → бот уточняет

### 4.3 CRM Teacher Sync

**При старте приложения:**

```python
async def sync_teachers(self, crm_adapter: ImpulseAdapter) -> None:
    """
    1. CRM API: teacher/list → список преподавателей
    2. Для каждого: извлечь имя + фамилию
    3. Имя → нормализовать → найти в names_dict → получить aliases
    4. Построить lookup: alias → [{full_name, crm_id}]
    5. Добавить фамилию как alias (для запросов типа "к Николаевой")
    """
```

**Периодический ресинк:** Каждые 6 часов (или по команде /resync от админа). Это покрывает ротацию преподавателей без перезапуска.

**Формат CRM-ответа** (из RFC_REFERENCE Appendix A, entity `teacher/list`):

```json
{
  "fields": ["id", "name"],
  "limit": 100
}
→ [{"id": 123, "name": "Анастасия Николаева"}, ...]
```

### 4.4 BranchResolver — конфигурируемый резолвинг филиалов

Филиалов мало (4-5), они стабильны, но имеют множество разговорных названий, привязанных к городской географии. Это бизнес-знание — автоматически не вычисляется.

**Конфигурация в studio.yaml:**

```yaml
branches:
  - id: "gogolya"
    name: "Гоголя"
    crm_branch_id: "XX"      # ID в CRM
    address: "Красного Знамени, 59"
    aliases:
      - "гоголя"
      - "красного знамени"
      - "красного знамени 59"
      - "некрасовская"
      - "первая речка"

  - id: "aleutskaya"
    name: "Алеутская"
    crm_branch_id: "YY"
    address: "Алеутская, 17"
    aliases:
      - "алеутская"
      - "центр"            # ⚠️ Дублируется с Семёновской
      - "родина"
      - "клевер"
      - "клевер хаус"
      - "clover"

  - id: "semenovskaya"
    name: "Семёновская"
    crm_branch_id: "ZZ"
    address: "Семёновская, 30а"
    aliases:
      - "семёновская"
      - "семеновская"
      - "центр"            # ⚠️ Дублируется с Алеутской — resolver вернёт оба
      - "изумруд"
      - "лотте"

  - id: "cheremukhovaya"
    name: "Черемуховая"
    crm_branch_id: "WW"
    address: "Черемуховая, 18"
    aliases:
      - "черемуховая"
      - "чуркин"
      - "чайка"

  - id: "test"
    name: "Тест"
    crm_branch_id: "TT"
    address: "Адрес тест"
    aliases:
      - "тест"
```

**Обработка "центр":** resolve_branch("центр") → [Алеутская, Семёновская]. Бот: "В центре у нас два филиала: Алеутская и Семёновская. Какой удобнее?"

**Unknown areas** (районы без филиалов):

```yaml
unknown_areas:
  aliases:
    - "вторая речка"
    - "баляева"
    - "заря"
    - "седанка"
    - "бам"
    - "патрокл"
    - "остров русский"
    - "тихая"
  response_template: "К сожалению, у нас пока нет филиала в этом районе. Ближайшие к вам: {nearest_branches}"
```

**Приоритет при конфликтах:** Branch aliases проверяются ПЕРВЫМИ. Если значение найдено в branch aliases — это branch, unknown_areas не проверяется. Unknown_areas — fallback только если branch resolver вернул пустой результат.

**Валидация при старте:** Если один и тот же alias есть в branches И в unknown_areas — ошибка загрузки. Приложение не стартует. Это предотвращает неоднозначности в runtime.

**Логика nearest_branches:** Статическая таблица "район → ближайшие филиалы". Не geo API — 4 филиала не оправдывают интеграцию. Интерфейс позволяет подменить реализацию позже.

### 4.5 StyleResolver — конфигурируемый резолвинг направлений

```yaml
style_aliases:
  "High Heels":
    crm_style_id: 5
    aliases: ["хилс", "хиллз", "хилз", "каблуки", "heels", "на каблуках"]
  "Girly Hip-Hop":
    crm_style_id: 8
    aliases: ["гёрли", "герли", "girly", "гёрл"]
  "Contemporary":
    crm_style_id: 3
    aliases: ["контемп", "контемпорари", "contemporary"]
  "Frame Up Strip":
    crm_style_id: 12
    aliases: ["стрип", "strip", "фрейм ап", "frame up"]
  "Dancehall":
    crm_style_id: 9
    aliases: ["дэнсхолл", "дэнс холл", "dancehall"]
```

**Нормализация:** Exact match first (lowercase), затем pymorphy3 для падежей ("каблуках" → "каблуки") только если exact не нашёл.

### 4.6 Единая точка входа

```python
@dataclass
class ResolvedEntity:
    name: str              # Каноническое имя
    crm_id: int | str      # ID в CRM
    entity_type: str        # "teacher" | "branch" | "style"
    confidence: float       # 1.0 для exact match (fuzzy отключён в MVP)
    source: str             # "alias" | "names_dict" | "direct"

@dataclass  
class ResolvedEntities:
    teachers: list[ResolvedEntity]
    branches: list[ResolvedEntity]
    styles: list[ResolvedEntity]
    unknown_area: str | None = None  # Если район опознан как unknown

class EntityResolver(Protocol):
    """Интерфейс. Реализация может быть заменена без изменения engine.py.
    
    Каждый метод получает PRE-EXTRACTED значение из LLM slot_updates,
    не сырой текст пользователя. Tokenization — ответственность LLM.
    """
    
    async def resolve_teacher(self, raw: str, tenant_id: str) -> list[ResolvedEntity]: ...
    
    async def resolve_branch(self, raw: str, tenant_id: str) -> list[ResolvedEntity]: ...
    
    async def resolve_style(self, raw: str, tenant_id: str) -> list[ResolvedEntity]: ...
    
    async def check_unknown_area(self, raw: str, tenant_id: str) -> str | None:
        """Возвращает название unknown area если найдено, иначе None."""
        ...
```

**`tenant_id`** — единственная "заготовка" на мультитенантность. Сейчас = один конфиг, одна студия. При масштабировании — подменяем реализацию, интерфейс тот же.

**Метод `resolve_all` удалён.** Engine.py вызывает resolver'ы по отдельности для каждого raw-значения из LLM slot_updates. Это проще, явнее, и не требует tokenization.

---

## 5. Интеграция с Conversation Engine (RFC-003)

### 5.1 Точка вызова

EntityResolver вызывается в `engine.py` **после** LLM slot extraction и **перед** CRM-запросами:

```
Пользователь: "к Настюше на каблуки"
        │
        ▼
[engine.py] handle_message()
        │
        ├─ [1] LLM → slot extraction:
        │       slot_updates: {
        │         teacher_raw: "настюше",
        │         style_raw: "каблуки"
        │       }
        │
        ├─ [2] Для каждого *_raw слота — вызов EntityResolver:
        │       resolve_teacher("настюше") → [ResolvedEntity("Анастасия Николаева", 123)]
        │       resolve_style("каблуки")  → [ResolvedEntity("High Heels", 5)]
        │       (branch_raw не заполнен → resolve_branch НЕ вызывается)
        │
        ├─ [3] _handle_resolved_entities() → проверка неоднозначностей
        │       Если всё однозначно → обновить слоты:
        │         slots.teacher = "Анастасия Николаева"
        │         slots.teacher_id = 123
        │         slots.group = "High Heels"
        │         slots.style_id = 5
        │
        ├─ [4] compute_phase(slots) → SCHEDULE (или DISCOVERY если branch не заполнен)
        │
        ├─ [5] CRM: get_schedule(teacher_id=123, style_id=5)  ← structured IDs
        │
        ├─ [6] PromptBuilder.build_system_prompt(slots, phase, schedule_data)
        │
        ├─ [7] LLM → response (бот спрашивает про филиал, т.к. branch пустой)
        │
        └─ [8] Guardrails → send
```

**Когда resolver НЕ вызывается:**

Если LLM не извлёк raw-значение (пользователь не упомянул сущность), resolver для этой сущности не вызывается. Например:
- "Хочу на занятие завтра вечером" → LLM: `{datetime_raw: "завтра вечером"}` → teacher/style/branch resolver'ы не вызываются → бот спрашивает направление
- "Привет!" → LLM: `{intent: "greeting"}` → ни один resolver не вызывается

### 5.2 Обработка в engine.py

```python
async def _resolve_and_update_slots(
    self, slot_updates: dict, slots: SlotValues
) -> str | None:
    """
    Вызывается после LLM slot extraction.
    Для каждого *_raw значения — вызов соответствующего resolver.
    Возвращает уточняющее сообщение если нужно, иначе None.
    """
    # Резолвим только заполненные raw-значения
    teacher_raw = slot_updates.get("teacher_raw")
    style_raw = slot_updates.get("style_raw")
    branch_raw = slot_updates.get("branch_raw")
    
    # --- Преподаватель ---
    if teacher_raw:
        teachers = await self._resolver.resolve_teacher(teacher_raw, self._tenant_id)
        if len(teachers) > 1:
            names = [t.name for t in teachers]
            return f"У нас несколько преподавателей с таким именем: {', '.join(names)}. К кому хотите записаться?"
        elif len(teachers) == 1:
            slots.teacher = teachers[0].name
            slots.teacher_id = teachers[0].crm_id
            slots.teacher_raw = teacher_raw
        else:
            # 0 совпадений — сообщаем, не блокируем
            return f"Не нашла преподавателя «{teacher_raw}». Подсказать, кто ведёт занятия?"
    
    # --- Направление ---
    if style_raw:
        styles = await self._resolver.resolve_style(style_raw, self._tenant_id)
        if len(styles) == 1:
            slots.group = styles[0].name
            slots.style_id = styles[0].crm_id
            slots.style_raw = style_raw
        elif len(styles) == 0:
            return f"Не нашла направление «{style_raw}». Вот что у нас есть: ..."
    
    # --- Филиал ---
    if branch_raw:
        branches = await self._resolver.resolve_branch(branch_raw, self._tenant_id)
        if len(branches) > 1:
            names = [b.name for b in branches]
            return f"В этом районе у нас несколько филиалов: {' и '.join(names)}. Какой удобнее?"
        elif len(branches) == 1:
            slots.branch = branches[0].name
            slots.branch_id = branches[0].crm_id
            slots.branch_raw = branch_raw
        else:
            # Проверяем unknown area
            unknown = await self._resolver.check_unknown_area(branch_raw, self._tenant_id)
            if unknown:
                nearest = self._get_nearest_branches(unknown)
                return f"К сожалению, у нас нет филиала в этом районе. Ближайшие: {', '.join(nearest)}"
            else:
                return f"Не нашла филиал «{branch_raw}». Вот наши филиалы: ..."
    
    return None  # Уточнение не нужно
```

### 5.3 Новые поля в SlotValues (расширение RFC-003 §4.1)

```python
class SlotValues(BaseModel):
    # ... существующие поля из RFC-003 ...
    
    # Новые: RAW значения из LLM slot extraction (до нормализации)
    teacher_raw: str | None = None     # "настюше" — как извлёк LLM
    style_raw: str | None = None       # "каблуки" — как извлёк LLM
    branch_raw: str | None = None      # "гоголя" — как извлёк LLM
    
    # Новые: CRM IDs после нормализации EntityResolver
    teacher_id: int | str | None = None    # CRM ID преподавателя
    branch_id: str | None = None           # CRM ID филиала
    style_id: int | str | None = None      # CRM ID направления
```

Все новые поля имеют defaults — обратная совместимость с существующими сессиями в PostgreSQL.

**Поток данных:**
1. LLM заполняет `*_raw` поля (из текста пользователя)
2. EntityResolver нормализует `*_raw` → `*_id` + обновляет canonical имена (`teacher`, `group`, `branch`)
3. CRM-запросы используют `*_id` (structured), не текстовые названия

### 5.4 Изменение CRM-запросов

**Текущий подход** (сломан):
```python
# schedule_flow.py — фильтрация по текстовому совпадению
schedules = [s for s in crm_data if s.branch_name == slots.branch]  # "Гоголя" ≠ CRM branch name
```

**Новый подход:**
```python
# schedule_flow.py — фильтрация по CRM IDs
schedule_data = await crm.get_schedule(
    teacher_id=slots.teacher_id,    # Structured ID
    style_id=slots.style_id,        # Structured ID
    branch_id=slots.branch_id       # Structured ID (если CRM поддерживает)
)
```

**OQ-10:** Поддерживает ли Impulse CRM API фильтрацию `schedule/list` по `teacher_id`, `style_id`, `branch_id` (через `columns`)? Если нет — фильтрация на стороне бота, но по CRM ID, а не по текстовому названию.

---

## 6. Разделение intents: buy_subscription ≠ book_class

### 6.1 Проблема

Из тестирования: "Хочу купить 2 абонемента по 8 занятий" → бот ведёт в booking flow (выбор направления → выбор даты). Контекст "покупка" потерян.

### 6.2 Решение

Расширить intent classification в LLM response (RFC-003 §7.1):

```python
class LLMResponse(BaseModel):
    message: str
    slot_updates: dict[str, Any] = {}
    tool_calls: list[ToolCall] = []
    intent: str = "continue"
    # Было:  "continue" | "booking" | "cancel" | "escalate" | "info"
    # Стало: "continue" | "booking" | "cancel" | "escalate" | "info" 
    #        | "buy_subscription" | "ask_price" | "ask_trial"
```

**Маршрутизация в engine.py:**

| Intent | Поведение |
|--------|-----------|
| `booking` | Существующий flow: слоты → расписание → запись |
| `buy_subscription` | Информация об абонементах из KB → условия покупки → эскалация к админу (бот не принимает оплату) |
| `ask_price` | Информационный ответ из KB. Уточнить: групповые или индивидуальные. Не переводить в booking flow |
| `ask_trial` | Информация о пробном из KB → предложить записаться на пробное (→ booking flow с пометкой "пробное") |

**Для buy_subscription и ask_price:** бот не должен спрашивать "какое направление?" — это booking-вопрос. Вместо этого: отвечает на вопрос о ценах/абонементах из KB, и только если клиент сам спрашивает про запись — переходит в booking flow.

### 6.3 Guardrail на intent

Новый guardrail G13:

```python
def check_intent_switch(slots: SlotValues, response: LLMResponse) -> GuardrailResult:
    """
    Если текущий intent = buy_subscription или ask_price,
    LLM не должен спрашивать про направление/филиал/дату.
    Эти вопросы уместны только для booking intent.
    """
```

---

## 7. Единый источник филиалов

### 7.1 Проблема

В studio.yaml есть два места с филиалами:
- `studio.branches` — содержит "Тест" и другие
- `branches` (верхнеуровневая секция) — не содержит "Тест"

`prompt_builder` читает из `branches`, поэтому бот не знает про "Тест".

### 7.2 Решение

Одна секция `branches` в studio.yaml (§4.4 этого RFC). `prompt_builder` и `EntityResolver` читают из неё. Секция `studio.branches` удаляется или становится ссылкой.

```python
# knowledge/base.py
@property
def branches(self) -> list[dict]:
    """Единственный источник филиалов."""
    return self._data["branches"]
```

Prompt builder формирует "Наши филиалы" из этого же источника:

```python
# prompt_builder.py
def _format_kb_context(self, slots, phase):
    branches = self._kb.branches
    branch_text = "\n".join(f"- {b['name']}: {b['address']}" for b in branches)
    return f"Наши филиалы:\n{branch_text}"
```

---

## 8. Файловая структура

### 8.1 Новые файлы

```
app/
  core/
    entity_resolver/
      __init__.py              # Экспорт: EntityResolver, ResolvedEntity, ResolvedEntities
      protocol.py              # EntityResolver Protocol (интерфейс)
      models.py                # ResolvedEntity, ResolvedEntities dataclasses
      teacher_resolver.py      # TeacherResolver: names_dict + CRM sync + pymorphy3
      branch_resolver.py       # BranchResolver: aliases из studio.yaml
      style_resolver.py        # StyleResolver: aliases из studio.yaml  
      alias_resolver.py        # AliasEntityResolver: реализация Protocol, объединяет три resolver'а
      names_dict.json          # Словарь уменьшительных форм (~300 имён)

tests/
  unit/
    test_teacher_resolver.py   # Тесты: "Настя" → Анастасия, падежи, неоднозначности, фамилии м/ж
    test_branch_resolver.py    # Тесты: "центр" → [Алеутская, Семёновская], unknown areas, приоритеты
    test_style_resolver.py     # Тесты: "гёрли" → Girly Hip-Hop, "каблуки" → High Heels
    test_entity_resolver.py    # Интеграционные: engine вызывает resolver'ы по raw-значениям
```

### 8.2 Изменяемые файлы

| Файл | Изменение |
|------|-----------|
| `app/models.py` | Добавить `teacher_raw`, `style_raw`, `branch_raw`, `teacher_id`, `branch_id`, `style_id` в SlotValues |
| `app/core/engine.py` | Вызов EntityResolver перед prompt builder + обработка неоднозначностей |
| `knowledge/studio.yaml` | Единый `branches` с aliases. `style_aliases`. `unknown_areas`. Удалить дублирование `studio.branches` |
| `knowledge/base.py` | Единый accessor `branches`. Новые accessors для aliases |
| `app/core/prompt_builder.py` | Использовать единый `branches` из KB |
| `app/core/schedule_flow.py` | Использовать CRM IDs вместо текстовых названий для фильтрации |

### 8.3 Зависимости

| Зависимость | Назначение | Размер |
|-------------|------------|--------|
| pymorphy3 | Нормализация падежей: "Насте" → "Настя", "каблуках" → "каблуки" | ~15MB |

Одна новая зависимость. names_dict.json — статический файл в репо, не зависимость.

---

## 9. Совместимость с CONTRACT.md

| Секция CONTRACT | Требование | Как выполняется |
|-----------------|------------|-----------------|
| §2 "Do NOT implement Multi-tenant" | Не строить мультитенантную инфраструктуру | `tenant_id` — параметр в интерфейсе, реализация single-tenant. Никакой инфраструктуры |
| §4 Data ownership | Schedule, bookings из CRM. Prices из KB | EntityResolver не меняет ownership. Резолвинг → CRM IDs → CRM запрос |
| §5 CRM: Impulse | Entities: teacher, schedule, style | Используем teacher/list для sync. Остальные API без изменений |
| §6 "Never guess" | Бот не выдумывает | EntityResolver детерминированный. Если не нашёл → "не нашла преподавателя" |
| §11 LLM rules | LLM не для фактов | EntityResolver — код, не LLM. Детерминированный |
| §15 Knowledge Base | studio.yaml | Расширяем aliases конфиг. Валидация при старте |
| §24 Tech constraints | Python, FastAPI, pydantic v2 | pymorphy3 — единственная новая зависимость |

---

## 10. Тестирование

### 10.1 Unit-тесты

```python
# test_teacher_resolver.py

def test_diminutive_to_full():
    """Настя → Анастасия Николаева"""
    r = teacher_resolver.resolve("настя")
    assert len(r) == 1
    assert r[0].name == "Анастасия Николаева"

def test_case_form():
    """Насте, Настю, Настей → все резолвятся"""
    for form in ["насте", "настю", "настей", "настюше"]:
        r = teacher_resolver.resolve(form)
        assert len(r) >= 1

def test_ambiguous():
    """Две Анастасии → список из двух"""
    # Setup: CRM mock с двумя Анастасиями
    r = teacher_resolver.resolve("настя")
    assert len(r) == 2

def test_unknown():
    """Неизвестное имя → пустой список"""
    r = teacher_resolver.resolve("вася")
    assert len(r) == 0

def test_surname():
    """По фамилии → находит"""
    r = teacher_resolver.resolve("николаева")
    assert len(r) == 1

def test_surname_gender():
    """Женская форма фамилии → находит мужскую в CRM и наоборот"""
    # CRM: "Анастасия Николаева"
    r = teacher_resolver.resolve("николаев")  # мужская форма
    assert len(r) == 1
    assert r[0].name == "Анастасия Николаева"

# test_style_filter — отложен до Phase 4.2+ (зависит от OQ-11)


# test_branch_resolver.py

def test_exact_name():
    """Гоголя → Гоголя"""
    r = branch_resolver.resolve("гоголя")
    assert len(r) == 1
    assert r[0].name == "Гоголя"

def test_alias():
    """Первая речка → Гоголя"""
    r = branch_resolver.resolve("первая речка")
    assert r[0].name == "Гоголя"

def test_ambiguous_center():
    """Центр → [Алеутская, Семёновская]"""
    r = branch_resolver.resolve("центр")
    assert len(r) == 2

def test_unknown_area():
    """Седанка → unknown area"""
    r = branch_resolver.resolve("седанка")
    assert len(r) == 0
    # unknown_area flag set separately

def test_case_insensitive():
    """ГОГОЛЯ, Гоголя, гоголя → одинаковый результат"""
    for form in ["ГОГОЛЯ", "Гоголя", "гоголя"]:
        r = branch_resolver.resolve(form)
        assert r[0].name == "Гоголя"


# test_style_resolver.py

def test_alias_match():
    """каблуки → High Heels"""
    r = style_resolver.resolve("каблуки")
    assert r[0].name == "High Heels"

def test_slang():
    """гёрли → Girly Hip-Hop"""
    r = style_resolver.resolve("гёрли")
    assert r[0].name == "Girly Hip-Hop"

def test_case_form():
    """каблуках → каблуки → High Heels"""
    r = style_resolver.resolve("каблуках")
    assert r[0].name == "High Heels"
```

### 10.2 Prompt regression — новые тесты

```yaml
# tests/prompt_regression/test_entity_resolution.yaml

- name: "Уменьшительное имя преподавателя"
  messages:
    - user: "Хочу к Насте на хилс"
  expected:
    not_contains: ["не нашлось", "не найден"]
    contains_one_of: ["Анастасия", "расписание", "записать"]

- name: "Разговорное название филиала"
  messages:
    - user: "На Первой речке есть занятия?"
  expected:
    contains_one_of: ["Гоголя", "Красного Знамени", "расписание"]

- name: "Центр → два филиала"
  messages:
    - user: "Хочу в центре"
  expected:
    contains: ["Алеутская"]
    contains: ["Семёновская"]
    contains_one_of: ["какой", "удобнее", "выбрать"]

- name: "Разговорное название стиля"
  messages:
    - user: "Запишите на гёрли"
  expected:
    not_contains: ["не найден", "нет такого"]
    contains_one_of: ["Girly", "филиал", "расписание"]

- name: "Район без филиала"
  messages:
    - user: "Есть что-нибудь на Седанке?"
  expected:
    contains_one_of: ["нет филиала", "ближайш"]
```

### 10.3 Тестирование CRM sync

```python
# test_teacher_sync.py

async def test_sync_builds_lookup():
    """После sync lookup содержит aliases для каждого преподавателя"""
    mock_crm = MockCRM(teachers=[
        {"id": 1, "name": "Анастасия Николаева"},
        {"id": 2, "name": "Екатерина Петрова"},
    ])
    resolver = TeacherResolver(names_dict=NAMES_DICT)
    await resolver.sync(mock_crm)
    
    assert resolver.resolve("настя")[0].crm_id == 1
    assert resolver.resolve("катя")[0].crm_id == 2
    assert resolver.resolve("николаева")[0].crm_id == 1

async def test_resync_updates():
    """Ресинк подхватывает нового преподавателя"""
    mock_crm = MockCRM(teachers=[{"id": 1, "name": "Анастасия Николаева"}])
    resolver = TeacherResolver(names_dict=NAMES_DICT)
    await resolver.sync(mock_crm)
    
    # Добавили преподавателя
    mock_crm.teachers.append({"id": 3, "name": "Ольга Сидорова"})
    await resolver.sync(mock_crm)
    
    assert resolver.resolve("оля")[0].crm_id == 3
```

---

## 11. Открытые вопросы

| # | Вопрос | Приоритет | Влияние |
|---|--------|-----------|---------|
| OQ-10 | Поддерживает ли Impulse schedule/list фильтрацию по teacher_id, style_id, branch_id через `columns`? | P0 — выяснить до начала реализации | Определяет: фильтрация на стороне CRM или на стороне бота |
| OQ-11 | Формат CRM ответа teacher/list: какие поля возвращаются? Есть ли styles[] у преподавателя? | P0 | Определяет возможность фильтрации преподов по стилю |
| OQ-12 | Как CRM хранит связь преподаватель ↔ филиал? | P1 | Нужно для: "Настя на Гоголя" → проверить, ведёт ли Настя на Гоголя |
| OQ-8 (из RFC_REF) | Impulse HTTP Basic Auth format: `api_key:` или `:api_key`? | P0 | Блокер CRM sync |

---

## 12. Риски и митигация

| Риск | P | Митигация |
|------|---|-----------|
| names_dict не покрывает редкое имя | 🟡 | Fallback: показать список преподов по стилю. Админ может добавить кастомный alias через конфиг |
| CRM teacher/list не возвращает styles | 🟡 | style_filter отключён в MVP. Workaround: определять связь teacher↔style через schedule/list — кто ведёт занятия данного стиля. Добавим в Phase 4.2+ после подтверждения OQ-11 |
| pymorphy3 неправильно нормализует | 🟢 | Exact match aliases проверяется ДО pymorphy3. pymorphy3 — fallback для падежей |
| CRM sync упал при старте | 🔴 | **Degraded mode** (не молчать!): ERROR в логи → alert админу в TG → бот НЕ отвечает на booking запросы ("Технические неполадки, администратор свяжется с вами") → info-запросы из KB работают → retry sync каждые 5 минут. По сути Degradation Level L1 из CONTRACT §13 |
| Две Анастасии — бот уточняет каждый раз | 🟢 | Если указан стиль — фильтрация сужает до одной. Уточнение только при реальной неоднозначности |

---

## 13. Метрики успеха

| Метрика | Сейчас | Цель | Как измеряем |
|---------|--------|------|--------------|
| "Настя" → найден препод | ❌ Fail | ✅ Pass | Prompt regression |
| "на Гоголя" → расписание найдено | ❌ Fail | ✅ Pass | Prompt regression |
| "гёрли" → направление найдено | ❌ Fail | ✅ Pass | Prompt regression |
| "центр" → уточняющий вопрос | ❌ Не реализовано | ✅ Pass | Prompt regression |
| "купить абонемент" → не booking flow | ❌ Fail | ✅ Pass | Prompt regression |
| Entity resolution latency p95 | — | < 10ms | Structured logging |
| CRM teacher sync | — | < 5s на старте | Structured logging |

---

## 14. План внедрения

### Phase 4.1: Фундамент (2-3 дня)

```
Промпт 4.1.1 — Модели данных + Protocol
- ResolvedEntity, ResolvedEntities dataclasses
- EntityResolver Protocol
- Расширение SlotValues (teacher_id, branch_id, style_id)
- Unit-тесты на модели

Промпт 4.1.2 — names_dict.json
- Сгенерировать словарь ~300 русских имён с уменьшительными формами
- Формат: {"анастасия": ["настя", "настена", ...], ...}
- Инвертированный индекс: {"настя": "анастасия", ...}
- Валидация: нет дубликатов в aliases, все lowercase

Промпт 4.1.3 — TeacherResolver
- Загрузка names_dict.json
- CRM sync (teacher/list)
- resolve() с pymorphy3 нормализацией
- Unit-тесты (§10.1)
```

### Phase 4.2: Resolvers + KB (2-3 дня)

```
Промпт 4.2.1 — BranchResolver + StyleResolver
- Загрузка aliases из studio.yaml
- resolve() с lowercase match
- Unknown areas detection
- Unit-тесты

Промпт 4.2.2 — Обновление studio.yaml
- Единая секция branches с aliases и crm_branch_id
- style_aliases секция
- unknown_areas секция
- Удалить дублирование studio.branches
- Обновить KB валидацию

Промпт 4.2.3 — AliasEntityResolver (объединение)
- Реализация Protocol (три отдельных resolve_* метода + check_unknown_area)
- Валидация при старте: конфликты branch aliases vs unknown_areas → ошибка
- Валидация: names_dict alias conflicts → WARNING в лог
- Integration тесты
```

### Phase 4.3: Интеграция с Engine (2-3 дня)

```
Промпт 4.3.1 — Интеграция в engine.py
- Добавить teacher_raw, style_raw, branch_raw в LLM slot_updates schema
- _resolve_and_update_slots(): вызов resolver'ов по raw-значениям из LLM
- Обработка неоднозначностей (>1 результат → уточняющий вопрос)
- Использование CRM IDs в schedule_flow.py
- Degraded mode при sync fail: booking блокируется, info работает
- Integration тесты

Промпт 4.3.2 — Новые intents
- buy_subscription, ask_price, ask_trial в LLMResponse
- Маршрутизация в engine.py
- Guardrail G13 (intent switch)

Промпт 4.3.3 — Prompt regression тесты
- 5 новых тестов из §10.2
- Прогон всех существующих тестов
- Threshold ≥ 90%
```

**Итого: ~6-9 рабочих дней**

### Зависимость от RFC-003

EntityResolver спроектирован для интеграции с Conversation Engine v2 (RFC-003). Если RFC-003 ещё не реализован, Phases 4.1-4.2 (resolver'ы, тесты, KB) можно реализовать независимо. Phase 4.3 (интеграция с engine.py) требует RFC-003.

---

## 15. Будущее масштабирование (НЕ реализуем сейчас)

Документируем для контекста. Ни один из этих пунктов не входит в текущий RFC.

| Этап | Когда | Что меняется |
|------|-------|-------------|
| 2-3 студии | ~3 мес | `tenant_id` начинает использоваться реально. Каждая студия — свой studio.yaml. TeacherResolver синкает с CRM каждой студии |
| 10+ студий | ~6 мес | Onboarding-скрипт: подключить CRM → автогенерация aliases → владелец проверяет. Замена AliasEntityResolver на SmartEntityResolver с auto-aliases |
| 50+ студий | ~12 мес | Admin UI для управления aliases. Возможно: LLM-based resolver как fallback для незнакомых имён. Geo API для unknown_areas |

Интерфейс EntityResolver (Protocol) не меняется на всех этапах. Меняется только реализация.

---

## 16. Changelog

### v1.1 (правки по ревью)

| Пункт ревью | Решение | Секция RFC |
|-------------|---------|------------|
| **P0-1: resolve_all без tokenization** | Resolver НЕ парсит текст. LLM извлекает raw-значения в slot_updates, resolver нормализует каждое отдельно. `resolve_all` удалён. | §3.1, §4.6, §5.1, §5.2 |
| **P0-2: pymorphy3 на каждое слово** | Exact match ПЕРВЫМ (O(1)). pymorphy3 вызывается только если exact не нашёл. | §4.1 алгоритм |
| **P0-3: teacher.styles[] не подтверждён** | `style_filter` отложен до Phase 4.2+, зависит от OQ-11. MVP работает без фильтрации по стилю. | §4.1, §10.1 |
| **P0-4: фамилии м/ж** | При build lookup добавляются обе формы фамилии (м + ж). pymorphy3 inflect. | §4.1 `_build_surname_aliases` |
| **P1-1: конфликт aliases "саша"** | Инвертированный индекс: `alias → list[canonical]`, не перезапись. WARNING при загрузке. | §4.2 |
| **P1-2: fuzzy matching** | Отключён в MVP. Exact + pymorphy3 покрывают 95%+ кейсов. Fuzzy — Phase 4.2+ если данные покажут. | §4.1 алгоритм |
| **P1-3: branch vs unknown_area** | Приоритет: branch aliases → unknown_areas. Валидация при старте: конфликт = ошибка загрузки. | §4.4 |
| **P1-4: sync fail** | Degraded mode: ERROR → alert → booking блокируется → info из KB работает → retry каждые 5 мин. | §12 риски |
| **Стратегическое: resolver дополняет** | Явно зафиксировано: resolver ДОПОЛНЯЕТ LLM slot extraction, не ЗАМЕНЯЕТ. Engine не зависит от resolver для базовых сценариев. | §3.1 |

---

*RFC-004 v1.1 — Draft. Основано на результатах тестирования от 1 марта 2026. Правки по ревью интегрированы. Требует ревью перед началом работ.*
