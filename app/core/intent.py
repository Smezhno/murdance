"""Intent resolution via LLM.

Per CONTRACT §11: Intent classification and slot extraction ONLY (no function calling).
Tool execution belongs in booking_flow, not intent resolver.
"""

from typing import Any
from uuid import UUID

from app.ai.json_parser import extract_json
from app.ai.router import get_llm_router
from app.knowledge.base import get_kb
from app.models import UnifiedMessage


async def resolve_intent(
    message: UnifiedMessage,
    session_state: str,
    session_slots: dict[str, Any],
    trace_id: UUID,
    conversation_history: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Resolve intent and extract slots via LLM (CONTRACT §11).

    Intent resolution should use LLM for intent classification and slot extraction ONLY.
    No function calling - tool execution belongs in booking_flow.

    Args:
        message: User message
        session_state: Current FSM state
        session_slots: Current slot values
        trace_id: Trace ID for logging
        conversation_history: List of previous messages (last 10 included as context)

    Returns:
        Dict with intent, slots, response_text
    """
    kb = get_kb()
    llm_router = get_llm_router()

    # Build list of known directions from KB for the prompt
    known_directions = ", ".join(s.name for s in kb.services) if kb.services else "направления не загружены"

    # Build system prompt with KB context
    system_prompt = f"""Ты — помощник студии танцев. Помогаешь клиентам записаться на занятия.

Контекст студии:
{kb.format_for_llm()}

Текущее состояние диалога:
- Состояние: {session_state}
- Заполненные слоты: {session_slots}

═══ ПРАВИЛА ОПРЕДЕЛЕНИЯ INTENT (строго по приоритету) ═══

ПРАВИЛО 1 — BOOKING (наивысший приоритет):
Если сообщение содержит любое из слов: "записаться", "запишите", "запиши", "запись",
"хочу на занятие", "хочу записаться", "забронировать", "бронь", "хочу попасть",
"хочу прийти", "хочу посетить" — intent ВСЕГДА = "booking", без исключений.
Даже если направление неизвестно или написано с ошибкой.

ПРАВИЛО 2 — CANCEL:
Слова "отменить", "отмена записи", "не приду", "отпишите" → intent = "cancel"

ПРАВИЛО 3 — SCHEDULE_QUERY (наивысший приоритет после booking):
Любые вопросы про время, дни, расписание занятий — ВСЕГДА intent = "schedule_query".
Триггеры: "расписание", "когда", "во сколько", "какие занятия", "какое время",
"какой день", "есть ли", "в какое время", "а когда", "а во сколько".
ВАЖНО: даже если ты не знаешь расписание — всё равно возвращай intent = "schedule_query".
НЕ отвечай на вопросы о расписании самостоятельно — система запросит CRM автоматически.
В поле "response" напиши: "Сейчас уточню расписание..."

ПРАВИЛО 4 — остальные: price_query, info, greeting, admin

═══ ПРАВИЛА ОТВЕТА (CONTRACT §6) ═══

ЗАПРЕЩЕНО придумывать факты. Нельзя:
- Называть преподавателей для направлений, которых нет в KB
- Утверждать что занятие есть/нет без данных из KB или CRM
- Спорить с пользователем если он поправляет тебя
- Отвечать на вопросы о расписании без данных из CRM (всегда используй schedule_query)
- Подставлять дату/время автоматически: если пользователь не назвал день или время — datetime в slots ВСЕГДА null
- Угадывать "ближайшую" или "следующую" дату — это запрещено, нужно явно спросить у пользователя

Если пользователь называет направление которого нет в KB:
→ "Такого направления не нашла. У нас есть: {known_directions}. Что выбираете?"

Если не знаешь ответ на вопрос о расписании:
→ НЕ пиши "Уточню у администратора". Верни intent = "schedule_query" — система сама запросит CRM.

Если не знаешь ответ на другой вопрос:
→ "Уточню у администратора и дам ответ." НЕ УГАДЫВАЙ.

Если пользователь поправляет тебя:
→ Согласись и скорректируй ответ. Никогда не настаивай на своём.

═══ ФОРМАТ ОТВЕТА ═══

Отвечай ТОЛЬКО JSON, без markdown:
{{
    "intent": "booking|schedule_query|price_query|info|greeting|cancel|admin",
    "slots": {{
        "group": "название направления или null",
        "teacher": "имя преподавателя или null (если пользователь выбрал конкретного преподавателя)",
        "datetime": "дата/время ТОЛЬКО если пользователь явно назвал день или время — иначе null. НИКОГДА не угадывай и не подставляй дату автоматически.",
        "client_name": "имя или null",
        "client_phone": "телефон или null"
    }},
    "response": "текст ответа пользователю (≤300 символов, дружелюбно)"
}}"""

    messages = [
        {"role": "system", "content": system_prompt},
    ]

    # Add conversation history (last 10 messages)
    if conversation_history:
        for hist_msg in conversation_history[-10:]:
            messages.append(hist_msg)

    # Add current message
    messages.append({"role": "user", "content": message.text})

    # Call LLM (NO tools - intent resolution only)
    response = await llm_router.call(
        messages=messages,
        tools=None,  # No function calling in intent resolver
        temperature=0.0,
        trace_id=trace_id,
    )

    # Parse response
    result: dict[str, Any] = {
        "intent": "info",  # Default
        "slots": {},
        "response_text": response.text,  # Will be overridden if JSON parsed successfully
    }

    # Try to extract structured data from response
    json_data = extract_json(response.text)
    if json_data:
        result["intent"] = json_data.get("intent", "info")
        result["slots"] = json_data.get("slots", {})
        # Use "response" field from JSON if available, otherwise fallback to raw text
        result["response_text"] = json_data.get("response", response.text)

    return result

