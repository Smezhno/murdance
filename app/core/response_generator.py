"""Natural response generation via LLM.

The CODE decides WHAT to communicate; this module decides HOW to say it.
Schedule/price data in context is passed verbatim — LLM only controls phrasing.
"""

import logging
from typing import Any
from uuid import UUID

from app.ai.router import get_llm_router
from app.knowledge.base import get_kb

logger = logging.getLogger(__name__)

# intent → what the bot needs to accomplish right now
_TASK: dict[str, str] = {
    "greet": "поприветствуй пользователя и предложи помочь с записью",
    "ask_direction": "спроси, какое направление или стиль танца интересует пользователя",
    "show_schedule": "покажи расписание и предложи выбрать удобное время или преподавателя",
    "ask_datetime": "спроси, когда пользователю удобно прийти (день недели или дату)",
    "ask_contact": "попроси имя и номер телефона для оформления записи",
    "confirm_booking": "покажи итог записи и попроси подтвердить (да/нет)",
    "booking_done": "сообщи что запись оформлена, пожелай удачи, предложи пробное если уместно",
    "booking_cancelled": "сообщи что запись отменена, мягко предложи альтернативу или пробное",
    "info_answer": "ответь на вопрос пользователя, используя только предоставленные данные",
    "unclear": "вежливо скажи что не поняла, попроси переформулировать",
    "error_crm": "извинись за техническую неполадку, скажи что администратор поможет",
    "suggest_trial": "предложи бесплатное пробное занятие как первый шаг",
}

_FALLBACK: dict[str, str] = {
    "greet": "Привет! Чем могу помочь? 😊",
    "ask_direction": "Какое направление тебя интересует?",
    "show_schedule": "Вот расписание — выбирай удобное время!",
    "ask_datetime": "Когда тебе удобно прийти? Укажи день недели или дату.",
    "ask_contact": "Назови, пожалуйста, имя и номер телефона для записи.",
    "confirm_booking": "Всё верно? Подтверждаешь запись? (да/нет)",
    "booking_done": "✅ Запись оформлена! Ждём тебя!",
    "booking_cancelled": "Запись отменена. Если передумаешь — пишите!",
    "info_answer": "Уточни у администратора — он поможет.",
    "unclear": "Не совсем поняла 😊 Можешь переформулировать?",
    "error_crm": "Что-то пошло не так, администратор свяжется с тобой.",
    "suggest_trial": "Приходи на бесплатное пробное — лучший способ попробовать! 🔥",
}

_SYSTEM_PROMPT = """\
Ты — помощник танцевальной студии She Dance во Владивостоке.

Тон общения: дружелюбный, тёплый, на «ты». Как подруга, которая тоже танцует.
Максимум 2 эмодзи на сообщение. Без канцеляризмов, без "Как AI...".
Ответ до 300 символов.

{tone_examples}

Твоя задача сейчас: {task}

{context_data}

Правила:
- НЕ выдумывай расписание, цены, наличие мест
- Если данные предоставлены — используй только их
- Расписание, цены и имена преподавателей впиши ДОСЛОВНО как указано выше
- Будь краткой и конкретной\
"""


def _build_context_data(context: dict[str, Any]) -> str:
    """Format context dict into a short data block for the system prompt."""
    if not context:
        return ""
    lines = ["Данные:"]
    for key, val in context.items():
        lines.append(f"  {key}: {val}")
    return "\n".join(lines)


async def generate_response(
    intent: str,
    context: dict[str, Any],
    trace_id: UUID,
    conversation_history: list[dict] | None = None,
) -> str:
    """Generate a natural, human-like response using LLM.

    The caller decides WHAT to say (intent + context); this function decides HOW.
    Falls back to a hardcoded phrase if the LLM call fails.
    """
    task = _TASK.get(intent, f"помоги пользователю ({intent})")
    fallback = _FALLBACK.get(intent, "Чем могу помочь?")

    try:
        kb = get_kb()
        tone_examples = ""
        examples = getattr(kb.tone, "examples", None)
        if examples:
            examples_str = "\n".join(f'  - "{e}"' for e in examples[:3])
            tone_examples = f"Примеры тона:\n{examples_str}"

        system = _SYSTEM_PROMPT.format(
            tone_examples=tone_examples,
            task=task,
            context_data=_build_context_data(context),
        )

        messages: list[dict[str, str]] = [{"role": "system", "content": system}]
        if conversation_history:
            messages.extend(conversation_history[-6:])
        messages.append({"role": "user", "content": f"[intent={intent}]"})

        router = get_llm_router()
        response = await router.call(
            messages=messages,
            tools=None,
            temperature=0.3,
            trace_id=trace_id,
        )
        return response.text.strip() or fallback

    except Exception:
        logger.warning("generate_response LLM call failed for intent=%s, using fallback", intent)
        return fallback
