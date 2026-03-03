# DanceBot: Анализ экономики токенов

## Факты из логов

| Метрика | Значение |
|---------|----------|
| Успешные LLM вызовы | ~2000 prompt + ~500 completion = ~2500 tokens |
| Провальный вызов ("Давай") | 107835 input tokens |
| System prompt | ~262 000 символов ≈ 65 000 tokens |
| Нормальный system prompt (по CONTRACT) | < 4 000 tokens |
| Разница | **16x перерасход** |

## Экономика YandexGPT Pro RC

YandexGPT Pro (актуальные цены Yandex Cloud):
- Input: 0.24₽ / 1000 tokens  
- Output: 0.72₽ / 1000 tokens

### Сценарий A: Текущий (сломанный, 107k tokens на вызов)

```
1 LLM вызов:   107k × 0.24₽/1k = 25.68₽ input + 0.5k × 0.72₽/1k = 0.36₽ output = 26₽
1 диалог (5-7 LLM вызовов): ~130-180₽
100 клиентов/день: ~15 000₽/день
Месяц: ~450 000₽
```

**450k₽/мес на LLM при целевом MRR 75k₽. Убытки с первого дня.**

### Сценарий B: Нормальный (2.5k tokens на вызов, как в успешных логах)

```
1 LLM вызов:   2k × 0.24₽/1k = 0.48₽ input + 0.5k × 0.72₽/1k = 0.36₽ output = 0.84₽
1 диалог (5-7 вызовов): ~4-6₽
100 клиентов/день: ~500₽/день
Месяц: ~15 000₽
```

**15k₽/мес — приемлемо для bootstrap при MRR 75k₽ (20% от выручки).**

### Сценарий C: Оптимальный (CONTRACT — LLM только для classification/extraction)

CONTRACT §5 говорит: LLM only for intent classification, slot extraction, message rewriting.

Если большинство ответов формируются шаблонами, а LLM нужен только для NLU:

```
1 LLM вызов (classification): ~800 input + 200 output = 0.34₽
1 диалог: 2-3 LLM вызова + шаблонные ответы = 0.7-1₽
100 клиентов/день: ~100₽/день
Месяц: ~3 000₽
```

**3k₽/мес — отличная маржа.**

## Что вошло в 262k символов system prompt

По RFC и коду, system prompt собирается из:
1. _role_and_tone() — ~500 chars
2. _sales_rules() — ~2000 chars (10 правил)
3. _format_slots_context() — ~500 chars
4. _format_conversation_history() — **??? неограниченная история с schedule dump**
5. _format_kb_context() — **??? вся база знаний целиком?**
6. _format_tools() — ~1000 chars
7. _constraints() — ~500 chars

Пункты 4 и 5 — источник проблемы.

## Что должно быть в system prompt (по CONTRACT)

CONTRACT чётко говорит:
- LLM не для фактов → KB не нужна целиком в system prompt
- Расписание из CRM → не дампить 211 записей в промпт
- Intent classification + slot extraction = маленький промпт

Правильная архитектура:
```
System prompt = роль + правила + текущие слоты + инструменты
                ~500  + ~2000 + ~300       + ~1000
                = ~3800 chars ≈ 1000 tokens
```

KB факты инъектируются ТОЛЬКО когда нужны:
- Вопрос про цену → инъекция 1 строки с ценой
- Вопрос про расписание → tool call в CRM, НЕ дамп в промпт
- FAQ вопрос → инъекция 1 Q&A пары

## Root causes

1. **conversation_history в system prompt** — schedule dump (211 entries)
   попадает в историю как tool result, потом история вставляется в system prompt.
   При 5+ сообщениях — экспоненциальный рост.

2. **KB целиком в system prompt** — если _format_kb_context() вставляет
   весь studio.yaml (все стили, все FAQ, все преподаватели) — это 
   ненужные 10-20k chars на каждый вызов.

3. **Schedule dump как text** — 211 schedule entries превращаются в 
   огромный текст, который сохраняется в messages history и 
   пересылается в каждый следующий LLM вызов.

4. **Нет sliding window** — RFC CC-22 описывает sliding window 20 msg +
   summarization. Не реализовано. History растёт бесконечно.

## Рекомендации

### Immediate (сегодня)

1. **Убрать conversation_history из system prompt** — она уже в messages
2. **Не дампить schedule в messages** — tool results должны быть краткими:
   "Найдено 3 занятия: Ср 4/03 18:00 Гоголя, Пт 6/03 19:00 Алеутская..."
   НЕ полный JSON 211 записей
3. **KB context = только релевантная часть** — RAG-подход:
   если пользователь спрашивает про цену → инъекция 1 FAQ пары
   если идёт запись → только адрес выбранного филиала

### Short-term (эта неделя)

4. **Sliding window** — максимум 6 последних messages в LLM context
5. **Tool results cap** — tool results в history хранить как summary,
   не как raw data. "get_schedule returned 3 slots for High Heels at Гоголя"
6. **Budget Guard alarm** — если estimated_tokens > 5000, не отправлять,
   а логировать и отвечать шаблоном

### Medium-term (следующий спринт)

7. **Гибридная архитектура** — LLM для NLU (intent + slots), 
   шаблоны для ответов. Это CONTRACT §5.
8. **RAG для KB** — вместо "вся KB в промпте" → search по KB → 
   инъекция только релевантных фактов
9. **Мониторинг** — dashboard: tokens/call, cost/dialog, cost/day
