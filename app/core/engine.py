"""Conversation Engine v2 — LLM-driven orchestrator (RFC-003 §7).

Replaces BookingFlow.process_message(). booking_flow.py is kept until
integration testing is complete (RFC-003 §7.3).

Do NOT modify: cancel_flow.py, schedule_flow.py, booking_confirm.py,
               idempotency.py, conversation.py.
"""

import json
import logging
from datetime import datetime
from functools import lru_cache
from typing import Any
from uuid import UUID

from app.ai.providers.base import LLMResponse as ProviderLLMResponse
from app.ai.router import LLMRouter, get_llm_router
from app.core.booking_confirm import confirm_booking
from app.core.cancel_flow import get_cancel_flow
from app.core.conversation import get_or_create_session, save_session_to_store, update_slots
from app.core.guardrails import GuardrailRunner
from app.core.prompt_builder import LLMResponse as CoreLLMResponse, PromptBuilder, ToolCall
from app.core.schedule_flow import fetch_schedule, generate_schedule_response
from app.core.slot_tracker import ConversationPhase, compute_phase
from app.integrations.impulse import get_impulse_adapter
from app.knowledge.base import KnowledgeBase, get_kb
from app.models import SlotValues, UnifiedMessage

logger = logging.getLogger(__name__)

_CONFIRM_YES = {"да", "yes", "ок", "ok", "+", "подтверждаю", "подтверждаем"}
_CONFIRM_NO = {"нет", "no", "-", "отмена", "cancel"}
_MAX_RETRIES = 2
_HISTORY_KEEP = 50  # stored in session; LLM sees last 10 via prompt_builder


class ConversationEngine:
    """Main conversation orchestrator (RFC-003 §7).

    Instantiate once (singleton via get_conversation_engine()) and reuse.
    All heavy I/O is async; no state is stored on the instance.
    """

    def __init__(
        self,
        llm_router: LLMRouter,
        prompt_builder: PromptBuilder,
        guardrails: GuardrailRunner,
        impulse_adapter: Any,
        kb: KnowledgeBase,
    ) -> None:
        self._llm = llm_router
        self._pb = prompt_builder
        self._gr = guardrails
        self._impulse = impulse_adapter
        self._kb = kb

    # -------------------------------------------------------------------------
    # Public entry point
    # -------------------------------------------------------------------------

    async def handle_message(self, message: UnifiedMessage, trace_id: UUID) -> str:
        """Main entry point — replaces BookingFlow.process_message()."""
        if not message.text or not message.text.strip():
            return "Не понял. Напиши, что тебя интересует."

        session = await get_or_create_session(
            str(trace_id), message.channel, message.chat_id
        )
        slots: SlotValues = session.slots

        # --- Commands ---
        if message.text.strip() == "/start":
            await update_slots(session, **_empty_slots())
            return self._handle_start(session)

        if message.text.startswith("/debug"):
            return self._handle_debug(session)

        # --- Compute phase ---
        is_cancel = getattr(session, "state", None) and \
            str(getattr(session.state, "value", "")) == "cancel_flow"
        phase = compute_phase(slots, is_cancel=bool(is_cancel))

        # --- Special-phase fast paths ---
        if phase == ConversationPhase.CANCEL_FLOW:
            cancel_flow = get_cancel_flow()
            if slots.selected_reservation_id:
                response = await cancel_flow.confirm(session, message, trace_id)
            else:
                response = await cancel_flow.select(session, message, trace_id)
            await self._append_history(session, message.text, response)
            await save_session_to_store(session)
            return self._enforce_length(response, message.channel)

        if phase == ConversationPhase.ADMIN_HANDOFF:
            response = self._safe_fallback(phase)
            await self._append_history(session, message.text, response)
            await save_session_to_store(session)
            return response

        if phase == ConversationPhase.BOOKING and slots.confirmed and not slots.booking_created:
            response, created = await confirm_booking(session, trace_id, self._impulse, self._kb)
            if created:
                await update_slots(session, booking_created=True)
            else:
                await update_slots(session, confirmed=False)
            await self._append_history(session, message.text, response)
            await save_session_to_store(session)
            return self._enforce_length(response, message.channel)

        # --- Confirmation fast path (user says да/нет) ---
        if phase == ConversationPhase.CONFIRMATION:
            text_lower = message.text.strip().lower()
            if text_lower in _CONFIRM_YES:
                await update_slots(session, confirmed=True)
                response, created = await confirm_booking(session, trace_id, self._impulse, self._kb)
                if created:
                    await update_slots(session, booking_created=True)
                else:
                    await update_slots(session, confirmed=False)
                await self._append_history(session, message.text, response)
                await save_session_to_store(session)
                return self._enforce_length(response, message.channel)
            if text_lower in _CONFIRM_NO:
                await update_slots(session, **_empty_slots())
                response = "Хорошо, отменяю. Напиши, если захочешь записаться снова."
                await self._append_history(session, message.text, response)
                await save_session_to_store(session)
                return response

        # --- Pre-fetch CRM schedule if needed ---
        crm_schedule: list = []
        if phase in (
            ConversationPhase.SCHEDULE,
            ConversationPhase.COLLECTING_CONTACT,
            ConversationPhase.CONFIRMATION,
        ):
            try:
                result = await fetch_schedule(self._impulse, slots.model_dump(), trace_id)
                if isinstance(result, list):
                    crm_schedule = result
            except Exception as exc:
                logger.warning("schedule pre-fetch failed: %s", exc)

        # --- LLM loop with guardrail retries ---
        response = await self._llm_loop(
            message=message,
            session=session,
            slots=slots,
            phase=phase,
            crm_schedule=crm_schedule,
            trace_id=trace_id,
        )

        await self._append_history(session, message.text, response)
        await save_session_to_store(session)
        return self._enforce_length(response, message.channel)

    # -------------------------------------------------------------------------
    # LLM loop
    # -------------------------------------------------------------------------

    async def _llm_loop(
        self,
        message: UnifiedMessage,
        session: Any,
        slots: SlotValues,
        phase: ConversationPhase,
        crm_schedule: list,
        trace_id: UUID,
    ) -> str:
        violation_hint = ""
        for attempt in range(_MAX_RETRIES + 1):
            system_prompt = self._pb.build_system_prompt(slots, phase, crm_schedule)
            if violation_hint:
                system_prompt += (
                    f"\n\nПРЕДЫДУЩИЙ ОТВЕТ ОТКЛОНЁН. Нарушения: {violation_hint}. Исправь."
                )

            messages = self._build_messages(system_prompt, slots, message.text)

            try:
                raw: ProviderLLMResponse = await self._llm.call(messages, trace_id=trace_id)
            except Exception as exc:
                logger.error("LLM call failed (attempt %d): %s", attempt, exc)
                return self._safe_fallback(phase)

            parsed = self._parse_llm_response(raw.text)

            # If LLM requested create_booking, convert to intent="booking" so guardrails
            # (G3, G4) validate slots and confirmed=True BEFORE any CRM write happens.
            # The actual booking is executed on the next handle_message() cycle via the
            # BOOKING fast path in handle_message().
            if any(tc.name == "create_booking" for tc in parsed.tool_calls):
                parsed = CoreLLMResponse(
                    message=parsed.message,
                    slot_updates=parsed.slot_updates,
                    tool_calls=[tc for tc in parsed.tool_calls if tc.name != "create_booking"],
                    intent="booking",
                )

            # Execute tool_calls and re-call LLM with results as context
            tool_results = await self._execute_tool_calls(parsed.tool_calls, session, trace_id)
            if tool_results:
                # Apply slot_updates from the first LLM response before overwriting parsed
                if parsed.slot_updates:
                    await self._apply_slot_updates(session, parsed.slot_updates)
                    slots = session.slots

                for tool_name, result_text in tool_results.items():
                    messages.append({"role": "user", "content": f"[{tool_name}]: {result_text}"})
                try:
                    raw2: ProviderLLMResponse = await self._llm.call(messages, trace_id=trace_id)
                    parsed = self._parse_llm_response(raw2.text)
                except Exception as exc:
                    logger.error("LLM re-call after tools failed (attempt %d): %s", attempt, exc)
                    return self._safe_fallback(phase)

            # Apply slot_updates from the final parsed response
            if parsed.slot_updates:
                await self._apply_slot_updates(session, parsed.slot_updates)
                slots = session.slots  # refresh local reference

            result = await self._gr.check(parsed, slots, phase, crm_schedule or None)
            if result.passed:
                final_message = result.corrected_message or parsed.message
                if parsed.intent == "booking" and slots.confirmed and not slots.booking_created:
                    booking_response, created = await confirm_booking(
                        session, trace_id, self._impulse, self._kb
                    )
                    if created:
                        await update_slots(session, booking_created=True)
                    else:
                        await update_slots(session, confirmed=False)
                    return booking_response
                return final_message

            violation_hint = "; ".join(result.violations)
            logger.warning("guardrail violation (attempt %d): %s", attempt, violation_hint)

        logger.error("guardrails failed after %d retries — returning safe fallback", _MAX_RETRIES)
        return self._safe_fallback(phase)

    # -------------------------------------------------------------------------
    # Tool execution
    # -------------------------------------------------------------------------

    async def _execute_tool_calls(
        self,
        tool_calls: list[ToolCall],
        session: Any,
        trace_id: UUID,
    ) -> dict[str, str]:
        results: dict[str, str] = {}
        for tc in tool_calls:
            try:
                if tc.name == "get_filtered_schedule":
                    text = await generate_schedule_response(
                        self._impulse,
                        session.slots.model_dump(),
                        trace_id,
                        message_text=tc.parameters.get("message_text", ""),
                    )
                    results[tc.name] = text

                elif tc.name == "start_cancel_flow":
                    text = await get_cancel_flow().start(session, trace_id)
                    results[tc.name] = text

                elif tc.name == "search_kb":
                    query = tc.parameters.get("query", "")
                    if query:
                        faqs = self._kb.search_faq(query)
                        text = "\n".join(f"Q: {f.q}\nA: {f.a}" for f in faqs) if faqs \
                            else "Ответ на этот вопрос не найден в базе знаний."
                    else:
                        text = "Данные уже в системном промпте."
                    results[tc.name] = text

                elif tc.name == "escalate_to_admin":
                    results[tc.name] = self._safe_fallback(ConversationPhase.ADMIN_HANDOFF)

                else:
                    logger.warning("unknown tool_call: %s", tc.name)

            except Exception as exc:
                logger.error("tool_call %s failed: %s", tc.name, exc)
                results[tc.name] = f"Ошибка при выполнении {tc.name}."

        return results

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _build_messages(
        self, system_prompt: str, slots: SlotValues, user_text: str
    ) -> list[dict[str, str]]:
        messages = [{"role": "system", "content": system_prompt}]
        for msg in slots.messages[-10:]:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_text})
        return messages

    def _parse_llm_response(self, raw_text: str) -> CoreLLMResponse:
        """Parse structured JSON from LLM output into CoreLLMResponse.

        Falls back to a plain-text message if JSON parsing fails.
        """
        text = raw_text.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        try:
            data = json.loads(text)
            tool_calls_raw = data.pop("tool_calls", [])
            tool_calls = [
                ToolCall(**tc) if isinstance(tc, dict) else tc
                for tc in tool_calls_raw
            ]
            data["tool_calls"] = tool_calls
            return CoreLLMResponse(**data)
        except Exception:
            return CoreLLMResponse(message=raw_text, intent="continue")

    async def _apply_slot_updates(self, session: Any, slot_updates: dict) -> None:
        filtered = {}
        for k, v in slot_updates.items():
            if v is None:
                continue
            # LLM returns datetime fields as ISO strings — parse back to datetime
            if k in ("datetime_resolved",) and isinstance(v, str):
                try:
                    v = datetime.fromisoformat(v)
                except (ValueError, TypeError):
                    pass
            filtered[k] = v
        if filtered:
            await update_slots(session, **filtered)

    async def _append_history(self, session: Any, user_text: str, bot_text: str) -> None:
        messages = list(session.slots.messages)
        messages.append({"role": "user", "content": user_text})
        messages.append({"role": "assistant", "content": bot_text})
        # Keep last _HISTORY_KEEP messages to bound storage size
        messages = messages[-_HISTORY_KEEP:]
        await update_slots(session, messages=messages)

    def _handle_start(self, session: Any) -> str:
        return (
            f"Привет! Я помогу записаться на занятие в {self._kb.studio.name}. "
            "Напиши, какое направление тебя интересует, или задай любой вопрос."
        )

    def _handle_debug(self, session: Any) -> str:
        return (
            f"Debug info:\n"
            f"Phase: {compute_phase(session.slots).value}\n"
            f"Slots: {session.slots.model_dump()}\n"
        )

    def _safe_fallback(self, phase: ConversationPhase) -> str:
        if phase == ConversationPhase.GREETING:
            return (
                f"{self._kb.studio.name} — запишись на пробное занятие! "
                "Напиши, что тебя интересует."
            )
        if phase in (
            ConversationPhase.DISCOVERY,
            ConversationPhase.SCHEDULE,
            ConversationPhase.COLLECTING_CONTACT,
        ):
            return "Уточню расписание у администратора — он ответит в ближайшее время."
        if phase in (
            ConversationPhase.CONFIRMATION,
            ConversationPhase.BOOKING,
            ConversationPhase.POST_BOOKING,
        ):
            return (
                "Возникла техническая проблема. "
                "Пожалуйста, свяжитесь с администратором напрямую."
            )
        if phase == ConversationPhase.ADMIN_HANDOFF:
            return "Передаю тебя администратору — он ответит в ближайшее время."
        # CANCEL_FLOW or unknown
        return "Не удалось обработать запрос. Свяжитесь с администратором."

    @staticmethod
    def _enforce_length(text: str, channel: str) -> str:
        max_length = {"telegram": 300, "whatsapp": 200}.get(str(channel), 300)
        if len(text) <= max_length:
            return text
        truncated = text[: max_length - 3]
        last_boundary = max(
            truncated.rfind("."), truncated.rfind("!"), truncated.rfind("?")
        )
        if last_boundary > max_length // 2:
            return truncated[: last_boundary + 1]
        return truncated + "..."


def _empty_slots() -> dict:
    return {
        "group": None, "teacher": None, "datetime_raw": None,
        "datetime_resolved": None, "client_name": None, "client_phone": None,
        "schedule_id": None, "messages": [], "branch": None, "experience": None,
        "schedule_shown": False, "summary_shown": False, "confirmed": False,
        "booking_created": False, "receipt_sent": False,
    }


@lru_cache()
def get_conversation_engine() -> ConversationEngine:
    kb = get_kb()
    llm_router = get_llm_router()
    prompt_builder = PromptBuilder(kb)
    guardrails = GuardrailRunner(kb)
    impulse = get_impulse_adapter()
    return ConversationEngine(llm_router, prompt_builder, guardrails, impulse, kb)
