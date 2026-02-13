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

    # Build system prompt with KB context
    system_prompt = f"""Ты — помощник студии танцев. Помогаешь клиентам записаться на занятия.

Контекст студии:
{kb.format_for_llm()}

Текущее состояние диалога:
- Состояние: {session_state}
- Заполненные слоты: {session_slots}

Правила:
1. Определи intent: booking, schedule_query, price_query, info, greeting, cancel, admin
2. Извлеки слоты из сообщения: group, datetime, client_name, client_phone
3. Не придумывай расписание или цены — используй только данные из KB
4. Отвечай кратко (≤300 символов), дружелюбно, без корпоративного жаргона

Отвечай в формате JSON:
{{
    "intent": "booking|schedule_query|price_query|info|greeting|cancel|admin",
    "slots": {{
        "group": "...",
        "datetime": "...",
        "client_name": "...",
        "client_phone": "..."
    }},
    "response": "текст ответа пользователю"
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

