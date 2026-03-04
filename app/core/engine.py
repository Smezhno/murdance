"""Conversation Engine v2 — LLM-driven orchestrator (RFC-003 §7).

Replaces BookingFlow.process_message(). booking_flow.py is kept until
integration testing is complete (RFC-003 §7.3).
RFC-005: pre-booking availability check (closed_group_handler).
"""

import json
import logging
import re
from datetime import date, datetime
from functools import lru_cache
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from app.ai.providers.base import LLMResponse as ProviderLLMResponse
from app.ai.router import LLMRouter, get_llm_router
from app.core.availability.closed_group_handler import (
    check_closed_before_booking,
    handle_closed_group,
)
from app.core.booking_confirm import confirm_booking
from app.core.cancel_flow import get_cancel_flow
from app.core.conversation import get_or_create_session, save_session_to_store, update_slots
from app.core.entity_resolver.protocol import EntityResolver
from app.core.guardrails import GuardrailRunner
from app.core.prompt_builder import LLMResponse as CoreLLMResponse, PromptBuilder, ToolCall
from app.core.schedule_flow import generate_schedule_response
from app.core.slot_tracker import ConversationPhase, compute_phase
from app.config import get_settings
from app.integrations.impulse import get_impulse_adapter
from app.integrations.impulse.models import impulse_day_to_weekday
from app.knowledge.base import KnowledgeBase, get_kb
from app.queue.outbound import enqueue_message
from app.models import SlotValues, UnifiedMessage

logger = logging.getLogger(__name__)

_CONFIRM_YES = {
    "да", "yes", "ок", "ok", "+",
    "подтверждаю", "подтверждаем",
    "давай", "конечно", "запиши", "записывай",
    "хочу", "go", "ага", "угу", "давайте",
}
_CONFIRM_NO = {"нет", "no", "-", "отмена", "cancel"}
_MAX_RETRIES = 2

# RFC-007 F13: per-tool summary limits (chars, lines)
_TOOL_SUMMARY_LIMITS: dict[str, tuple[int, int]] = {
    "get_filtered_schedule": (1200, 15),
    "list_my_bookings": (800, 10),
    "default": (500, 6),
}


def _summarize_tool_result(tool_name: str, result_text: str) -> str:
    """Compact tool result for LLM context. Limit by tool type (RFC-007 F13)."""
    max_chars, max_lines = _TOOL_SUMMARY_LIMITS.get(
        tool_name, _TOOL_SUMMARY_LIMITS["default"]
    )
    if len(result_text) <= max_chars:
        return result_text
    lines = result_text.strip().split("\n")
    kept = lines[:max_lines]
    if len(lines) > max_lines:
        kept.append(f"(и ещё {len(lines) - max_lines} строк)")
    return "\n".join(kept)[:max_chars]


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
        resolver: EntityResolver | None = None,
        availability_provider: Any = None,
    ) -> None:
        self._llm = llm_router
        self._pb = prompt_builder
        self._gr = guardrails
        self._impulse = impulse_adapter
        self._kb = kb
        self._resolver = resolver
        self._availability = availability_provider
        from app.config import get_settings
        self._tenant_id = get_settings().crm_tenant

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

        # --- Gibberish detection (RFC-007 F9) ---
        if self._is_gibberish(message.text or ""):
            count = (getattr(slots, "gibberish_count", 0) or 0) + 1
            await update_slots(session, gibberish_count=count)
            if count >= 3:
                await self._notify_admin_gibberish(session)
                return "Передаю тебя администратору — он ответит в ближайшее время."
            return "Не совсем понял. Напиши, что интересует — направление, расписание, запись?"
        if getattr(slots, "gibberish_count", 0):
            await update_slots(session, gibberish_count=0)

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
            missing = self._get_missing_booking_slots(slots)
            if missing:
                pass  # Fall through to LLM to collect missing slots
            else:
                closed_msg = await self._maybe_handle_closed_before_booking(session, trace_id)
                if closed_msg:
                    await self._append_history(session, message.text, closed_msg)
                    await save_session_to_store(session)
                    return self._enforce_length(closed_msg, message.channel)
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
            first_word = (text_lower.split(",")[0].strip().split()[0] or "") if text_lower else ""
            if first_word in _CONFIRM_YES or text_lower in _CONFIRM_YES:
                if getattr(slots, "escalation_pending_reason", None):
                    await update_slots(session, escalation_pending_reason=None)
                    response = self._safe_fallback(ConversationPhase.ADMIN_HANDOFF)
                    await self._append_history(session, message.text, response)
                    return response

                missing = self._get_missing_booking_slots(slots)
                if missing:
                    await update_slots(session, confirmed=True)
                    pass  # Fall through to LLM loop which will ask for missing slots
                else:
                    await update_slots(session, confirmed=True)
                    closed_msg = await self._maybe_handle_closed_before_booking(session, trace_id)
                    if closed_msg:
                        await self._append_history(session, message.text, closed_msg)
                        await save_session_to_store(session)
                        return self._enforce_length(closed_msg, message.channel)
                    response, created = await confirm_booking(session, trace_id, self._impulse, self._kb)
                    if created:
                        await update_slots(session, booking_created=True)
                    else:
                        await update_slots(session, confirmed=False)
                    await self._append_history(session, message.text, response)
                    await save_session_to_store(session)
                    return self._enforce_length(response, message.channel)
            first_word_no = (text_lower.split(",")[0].strip().split()[0] or "") if text_lower else ""
            if first_word_no in _CONFIRM_NO or text_lower in _CONFIRM_NO:
                await update_slots(session, **_empty_slots())
                response = "Хорошо, отменяю. Напиши, если захочешь записаться снова."
                await self._append_history(session, message.text, response)
                await save_session_to_store(session)
                return response

        # --- LLM loop (schedule only via tool get_filtered_schedule, not in system prompt) ---
        response = await self._llm_loop(
            message=message,
            session=session,
            slots=slots,
            phase=phase,
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
        trace_id: UUID,
    ) -> str:
        violation_hint = ""
        executed_tools: set[str] = set()
        for attempt in range(_MAX_RETRIES + 1):
            system_prompt = self._pb.build_system_prompt(
                slots, phase,
                schedule_data=None,
                user_text=message.text or "",
            )
            logger.debug("SYSTEM_PROMPT_SIZE: %s chars", len(system_prompt))
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

            logger.info("LLM_RAW_RESPONSE: %.500s", (raw.text or "")[:500])
            parsed = self._parse_llm_response(raw.text)
            logger.debug("DEBUG_LLM: %.500s", (raw.text or "")[:500])

            # If LLM requested create_booking, convert to intent="booking" so guardrails
            # (G3, G4) validate slots and confirmed=True BEFORE any CRM write happens.
            # The actual booking is executed via confirm_booking() which uses session.slots.schedule_id
            # (set by get_filtered_schedule). We never take schedule_id from LLM params.
            # Persist client_name/client_phone from create_booking params into slot_updates
            # so they are applied to session.slots (LLM often sends them only in tool params).
            if any(tc.name == "create_booking" for tc in parsed.tool_calls):
                cb = next((tc for tc in parsed.tool_calls if tc.name == "create_booking"), None)
                merged_updates = dict(parsed.slot_updates) if parsed.slot_updates else {}
                if cb and cb.parameters:
                    sid_param = cb.parameters.get("schedule_id")
                    if sid_param is not None:
                        try:
                            int(sid_param)
                        except (ValueError, TypeError):
                            logger.warning(
                                "LLM sent non-numeric schedule_id=%s, booking will use slots.schedule_id=%s",
                                sid_param, getattr(session.slots, "schedule_id", None),
                            )
                    for key in ("client_name", "client_phone"):
                        val = cb.parameters.get(key)
                        if val and isinstance(val, str) and val.strip():
                            merged_updates[key] = val.strip()
                parsed = CoreLLMResponse(
                    message=parsed.message,
                    slot_updates=merged_updates,
                    tool_calls=[tc for tc in parsed.tool_calls if tc.name != "create_booking"],
                    intent="booking",
                )

            # Execute tool_calls and re-call LLM with results as context
            tool_results, availability_cache = await self._execute_tool_calls(
                parsed.tool_calls, session, trace_id, user_text=message.text
            )
            if tool_results:
                executed_tools.update(tool_results.keys())
                recent = set(getattr(session.slots, "recent_tools", None) or [])
                recent.update(tool_results.keys())
                await update_slots(session, recent_tools=list(recent))
                # Apply slot_updates from the first LLM response before overwriting parsed
                if parsed.slot_updates:
                    logger.info("LLM_SLOT_UPDATES: %s", parsed.slot_updates)
                else:
                    logger.info(
                        "LLM_NO_SLOT_UPDATES: message=%.200s",
                        (parsed.message or "")[:200],
                    )
                if parsed.slot_updates:
                    await self._apply_slot_updates(session, parsed.slot_updates)
                    slots = session.slots
                    clarification = await self._resolve_and_update_slots(
                        session, parsed.slot_updates, slots
                    )
                    if clarification:
                        return clarification

                # Tool result IS the user response — no second LLM call (RFC-007 F3)
                _DIRECT_RETURN_TOOLS = ("start_cancel_flow", "escalate_to_admin")
                for _name in _DIRECT_RETURN_TOOLS:
                    if _name in tool_results:
                        return tool_results[_name]

                for tool_name, result_text in tool_results.items():
                    capped = _summarize_tool_result(tool_name, result_text)
                    messages.append({"role": "user", "content": f"[{tool_name}]: {capped}"})
                try:
                    raw2: ProviderLLMResponse = await self._llm.call(messages, trace_id=trace_id)
                    parsed = self._parse_llm_response(raw2.text)
                except Exception as exc:
                    logger.error("LLM re-call after tools failed (attempt %d): %s", attempt, exc)
                    return self._safe_fallback(phase)

            # Apply slot_updates from the final parsed response
            if parsed.slot_updates:
                logger.info("LLM_SLOT_UPDATES: %s", parsed.slot_updates)
            else:
                logger.info(
                    "LLM_NO_SLOT_UPDATES: message=%.200s",
                    (parsed.message or "")[:200],
                )
            if parsed.slot_updates:
                await self._apply_slot_updates(session, parsed.slot_updates)
                slots = session.slots  # refresh local reference
                clarification = await self._resolve_and_update_slots(
                    session, parsed.slot_updates, slots
                )
                if clarification:
                    return clarification
                slots = session.slots

            result = await self._gr.check(
                parsed,
                slots,
                phase,
                None,  # crm_schedule removed per RFC-006 (schedule only via tool calls)
                executed_tools=executed_tools,
                session_tools=set(getattr(slots, "recent_tools", None) or []),
                availability_cache=availability_cache if self._availability else None,
            )
            if result.passed:
                final_message = result.corrected_message or parsed.message
                if parsed.intent == "buy_subscription":
                    return await self._handle_subscription_inquiry(session, parsed, final_message)
                if parsed.intent == "ask_price":
                    return await self._handle_price_inquiry(session, parsed, final_message)
                if parsed.intent == "ask_trial":
                    return await self._handle_trial_inquiry(session, parsed, final_message)
                if parsed.intent == "booking" and slots.confirmed and not slots.booking_created:
                    missing = self._get_missing_booking_slots(slots)
                    if missing:
                        _MISSING_LABELS = {
                            "branch": "филиал",
                            "group": "направление",
                            "datetime_resolved": "дата и время",
                            "client_name": "имя",
                            "client_phone": "номер телефона",
                        }
                        missing_labels = [_MISSING_LABELS.get(s, s) for s in missing]
                        return f"Для записи нужно уточнить: {', '.join(missing_labels)}."
                    closed_msg = await self._maybe_handle_closed_before_booking(session, trace_id)
                    if closed_msg:
                        return closed_msg
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
            # G14 block → return _handle_closed_group message instead of retry
            if any("G14:" in v for v in result.violations):
                avail = None
                sid = slots.schedule_id
                dt_resolved = slots.datetime_resolved
                if sid and dt_resolved and availability_cache:
                    try:
                        cache_key = f"{int(sid)}:{dt_resolved.date().isoformat()}"
                        avail = availability_cache.get(cache_key)
                    except (TypeError, ValueError, AttributeError):
                        pass
                if avail is not None:
                    return await handle_closed_group(
                        session, slots, avail,
                        self._availability, self._impulse
                    )

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
        user_text: str = "",
    ) -> tuple[dict[str, str], dict[str, Any]]:
        results: dict[str, str] = {}
        availability_cache: dict[str, Any] = {}
        for tc in tool_calls:
            try:
                if tc.name == "get_filtered_schedule":
                    # Merge LLM tool parameters into slots for this call
                    # so schedule filters by style/branch/teacher even if slots aren't set yet
                    call_slots = session.slots.model_dump()
                    if tc.parameters.get("style") and not call_slots.get("group"):
                        call_slots["group"] = tc.parameters["style"]
                    if tc.parameters.get("branch") and not call_slots.get("branch"):
                        call_slots["branch"] = tc.parameters["branch"]
                    if tc.parameters.get("teacher") and not call_slots.get("teacher"):
                        call_slots["teacher"] = tc.parameters["teacher"]

                    raw = await generate_schedule_response(
                        self._impulse,
                        call_slots,
                        trace_id,
                        message_text=user_text or tc.parameters.get("message_text", ""),
                        availability_provider=self._availability,
                    )
                    if isinstance(raw, str):
                        text, cache, first_slot = raw, {}, None
                    elif len(raw) == 2:
                        text, cache, first_slot = raw[0], raw[1], None
                    else:
                        text, cache, first_slot = raw[0], raw[1], raw[2] if len(raw) > 2 else None
                    results[tc.name] = text
                    if cache:
                        availability_cache.update(cache)

                    if first_slot and first_slot.get("schedule_id") is not None:
                        try:
                            sid = first_slot["schedule_id"]
                            sdate = first_slot.get("date")
                            stime = first_slot.get("time")
                            await update_slots(session, schedule_id=str(sid), schedule_shown=True)
                            if sdate and stime:
                                dt_naive = datetime.combine(sdate, stime)
                                dt_resolved = dt_naive.replace(tzinfo=ZoneInfo("Asia/Vladivostok"))
                                await update_slots(session, datetime_resolved=dt_resolved)
                        except Exception as e:
                            logger.debug("SCHEDULE_SLOT_EXTRACT_FAILED: %s", e)
                        logger.debug(
                            "SLOTS_AFTER_SCHEDULE: sid=%s dt=%s",
                            session.slots.schedule_id,
                            session.slots.datetime_resolved,
                        )

                elif tc.name == "start_cancel_flow":
                    text = await get_cancel_flow().start(session, trace_id)
                    results[tc.name] = text

                elif tc.name == "list_my_bookings":
                    phone = tc.parameters.get("phone") or session.slots.client_phone
                    if not phone or not str(phone).strip():
                        results[tc.name] = "Для просмотра записей нужен ваш номер телефона."
                    else:
                        try:
                            client = await self._impulse.find_client(str(phone).strip())
                            if not client:
                                results[tc.name] = "Клиент не найден."
                            else:
                                bookings = await self._impulse.list_bookings(
                                    client_id=client.id, date_from=date.today()
                                )
                                if not bookings:
                                    results[tc.name] = "Активных записей нет."
                                else:
                                    lines = []
                                    for i, b in enumerate(bookings[:5]):
                                        try:
                                            bd = b.date_as_date
                                            dt_str = bd.strftime("%d.%m.%Y") if bd else "?"
                                            lines.append(
                                                f"{i + 1}. {b.group_name}, {dt_str} в {b.time_str}"
                                            )
                                        except Exception:
                                            lines.append(f"{i + 1}. (запись)")
                                    results[tc.name] = "\n".join(lines)
                        except Exception as exc:
                            logger.warning("list_my_bookings failed: %s", exc)
                            results[tc.name] = "Не удалось загрузить записи. Обратитесь к администратору."

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
                    reason = tc.parameters.get("reason") or "Запрос эскалации"
                    try:
                        settings = get_settings()
                        admin_chat_id = getattr(settings, "admin_telegram_chat_id", None)
                        if admin_chat_id:
                            last_msgs = getattr(session.slots, "messages", [])[-6:]
                            context_lines = [
                                f"⚠️ Эскалация от {session.chat_id}",
                                f"Причина: {reason}",
                            ]
                            for m in last_msgs:
                                role = "👤" if m.get("role") == "user" else "🤖"
                                context_lines.append(f"{role} {(m.get('content') or '')[:80]}")
                            admin_text = "\n".join(context_lines)
                            await enqueue_message(
                                chat_id=str(admin_chat_id),
                                channel=session.channel,
                                text=admin_text,
                                trace_id=session.trace_id,
                                priority=1,
                            )
                        else:
                            logger.warning("admin_telegram_chat_id not set — skipping admin notify")
                    except Exception as e:
                        logger.warning("Failed to notify admin: %s", e)
                    results[tc.name] = "Передаю тебя администратору — он ответит в ближайшее время."

                else:
                    logger.warning("unknown tool_call: %s", tc.name)

            except Exception as exc:
                logger.error("tool_call %s failed: %s", tc.name, exc)
                results[tc.name] = f"Ошибка при выполнении {tc.name}."

        return results, availability_cache

    # -------------------------------------------------------------------------
    # RFC-007 F9: Gibberish detection
    # -------------------------------------------------------------------------

    _GIBBERISH_WHITELIST = frozenset(
        {"да", "нет", "+", "-", "ок", "ага", "угу", "кек", "хз" "нее", "збс", "оки", "пон", "йеп", "ноу", "лол"}
    )

    def _is_gibberish(self, text: str) -> bool:
        """Detect gibberish input. Conservative — don't block short valid messages."""
        t = text.strip()
        if t.lower() in self._GIBBERISH_WHITELIST or t.isdigit():
            return False
        if len(t) <= 1:
            return False
        words = re.findall(r"\b\w{2,}\b", t, re.UNICODE)
        if not words and not re.search(r"\d", t):
            return True
        if len(t) >= 6 and len(set(t.replace(" ", ""))) <= 3:
            return True
        return False

    async def _notify_admin_gibberish(self, session: Any) -> None:
        """Notify admin that user sent 3+ gibberish messages in a row."""
        try:
            settings = get_settings()
            admin_chat_id = getattr(settings, "admin_telegram_chat_id", None)
            if admin_chat_id:
                admin_text = f"⚠️ Gibberish/спам от {session.chat_id}. 3+ подряд."
                await enqueue_message(
                    chat_id=str(admin_chat_id),
                    channel=session.channel,
                    text=admin_text,
                    trace_id=session.trace_id,
                    priority=1,
                )
            else:
                logger.warning("admin_telegram_chat_id not set — skipping gibberish notify")
        except Exception as e:
            logger.warning("Failed to notify admin (gibberish): %s", e)

    # -------------------------------------------------------------------------
    # RFC-005: Availability check before booking
    # -------------------------------------------------------------------------

    async def _resolve_schedule_id_and_date(
        self, slots: SlotValues
    ) -> tuple[int | None, date | None]:
        """Resolve schedule_id and target_date from slots (mirrors confirm_booking Phase 2)."""
        sid = slots.schedule_id
        if sid is not None:
            try:
                return int(sid), slots.datetime_resolved.date() if slots.datetime_resolved else None
            except (TypeError, ValueError):
                pass
        if not slots.datetime_resolved:
            return None, None
        target_dt = slots.datetime_resolved
        target_weekday = target_dt.weekday()
        target_minutes = target_dt.hour * 60 + target_dt.minute
        group_lower = (slots.group or "").lower()
        teacher_lower = (getattr(slots, "teacher", None) or "").lower()
        schedules = await self._impulse.get_schedule()
        for sch in schedules:
            if sch.day is None or impulse_day_to_weekday(sch.day) != target_weekday:
                continue
            if sch.minutes_begin is None or abs(sch.minutes_begin - target_minutes) > 30:
                continue
            if group_lower and group_lower not in (sch.style_name or "").lower():
                continue
            if teacher_lower and (not sch.teacher_name or teacher_lower not in sch.teacher_name.lower()):
                continue
            return sch.id, target_dt.date()
        return None, None

    async def _maybe_handle_closed_before_booking(
        self, session: Any, trace_id: UUID
    ) -> str | None:
        """If group is CLOSED/HOLIDAY, run handle_closed_group and return message. Else None."""
        avail = await check_closed_before_booking(
            self._availability, session.slots, self._impulse
        )
        if avail is None:
            return None
        return await handle_closed_group(
            session, session.slots, avail, self._availability, self._impulse
        )

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _get_missing_booking_slots(self, slots: SlotValues) -> list[str]:
        """Return list of missing required booking slots."""
        required = {
            "branch": slots.branch,
            "group": slots.group,
            "datetime_resolved": slots.datetime_resolved,
            "client_name": slots.client_name,
            "client_phone": slots.client_phone,
        }
        return [k for k, v in required.items() if not v]

    def _build_messages(
        self, system_prompt: str, slots: SlotValues, user_text: str
    ) -> list[dict[str, str]]:
        messages = [{"role": "system", "content": system_prompt}]
        history = slots.messages[-10:]
        total_chars = len(system_prompt) + len(user_text)
        MAX_CHARS = 80000  # ~20k tokens, safe for 32k limit
        included: list[dict] = []
        for msg in reversed(history):
            content = msg.get("content", "")
            if total_chars + len(content) > MAX_CHARS:
                logger.debug("TOKEN_BUDGET_SKIP: dropping msg len=%s, total=%s", len(content), total_chars)
                continue
            included.append(msg)
            total_chars += len(content)
        for msg in reversed(included):
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_text})
        logger.debug("BUILD_MESSAGES: %s msgs, ~%s chars", len(messages), total_chars)
        return messages

    def _extract_message_from_partial(self, text: str) -> str | None:
        """Extract message field from truncated JSON (e.g. LLM cut off mid-response)."""
        match = re.search(r'"message"\s*:\s*"((?:[^"\\]|\\.){5,})', text)
        if match:
            msg = match.group(1).rstrip("\\")
            return msg.strip()
        return None

    def _parse_llm_response(self, raw_text: str) -> CoreLLMResponse:
        """Parse structured JSON from LLM output into CoreLLMResponse.

        Falls back to extracted message, cleaned text, or generic message if JSON fails.
        """
        text = raw_text.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            parts = text.split("```")
            text = parts[1] if len(parts) > 1 else text
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
            msg = self._extract_message_from_partial(text)
            if msg:
                return CoreLLMResponse(message=msg[:300], intent="continue")
            clean = re.sub(r'[{}\[\]"\\]', '', text)
            clean = re.sub(r'\b(message|slot_updates|tool_calls|intent)\b\s*:', '', clean)
            clean = clean.strip()[:300]
            if len(clean) > 10:
                return CoreLLMResponse(message=clean, intent="continue")
            return CoreLLMResponse(
                message="Уточните, пожалуйста, ваш вопрос.",
                intent="continue",
            )

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
            # schedule_id must be str in SlotValues
            if k == "schedule_id" and v is not None:
                v = str(v)
            filtered[k] = v
        if filtered:
            await update_slots(session, **filtered)

    async def _resolve_and_update_slots(
        self, session: Any, slot_updates: dict, slots: SlotValues
    ) -> str | None:
        """Resolve teacher_raw, style_raw, branch_raw to CRM IDs (RFC-004 §5.2).

        Returns clarification message if ambiguous, not found, or unknown area; else None.
        If resolver is not ready (teacher sync failed), skip resolution so LLM flow continues
        without normalization instead of returning "не нашла" for every name.
        """
        logger.debug(
            "RESOLVE_ENTRY: resolver=%s ready=%s updates=%s",
            self._resolver is not None,
            getattr(self._resolver, "is_ready", "N/A"),
            slot_updates,
        )
        if not self._resolver:
            logger.debug("RESOLVE_EXIT: no resolver")
            return None
        if not getattr(self._resolver, "is_ready", True):
            logger.debug("RESOLVE_EXIT: not ready")
            return None
        teacher_raw = slot_updates.get("teacher_raw") if isinstance(slot_updates.get("teacher_raw"), str) else None
        style_raw = slot_updates.get("style_raw") if isinstance(slot_updates.get("style_raw"), str) else None
        branch_raw = slot_updates.get("branch_raw") if isinstance(slot_updates.get("branch_raw"), str) else None

        # Skip re-resolution of already-confirmed entities (prevents loop when LLM re-sends teacher_raw on branch input)
        if teacher_raw and session.slots.teacher_id is not None:
            logger.info(
                "SKIP_TEACHER_RESOLVE: already set teacher_id=%s teacher=%s, ignoring teacher_raw=%s",
                session.slots.teacher_id,
                session.slots.teacher,
                teacher_raw,
            )
            teacher_raw = None
        if style_raw and session.slots.style_id is not None:
            logger.info(
                "SKIP_STYLE_RESOLVE: already set style_id=%s group=%s, ignoring style_raw=%s",
                session.slots.style_id,
                session.slots.group,
                style_raw,
            )
            style_raw = None
        if branch_raw and session.slots.branch_id is not None:
            logger.info(
                "SKIP_BRANCH_RESOLVE: already set branch_id=%s branch=%s, ignoring branch_raw=%s",
                session.slots.branch_id,
                session.slots.branch,
                branch_raw,
            )
            branch_raw = None

        logger.debug(
            "RESOLVE_AFTER_GUARDS: teacher_raw=%s style_raw=%s branch_raw=%s",
            teacher_raw, style_raw, branch_raw,
        )
        logger.debug(
            "RESOLVE_CURRENT_SLOTS: teacher_id=%s style_id=%s branch_id=%s",
            session.slots.teacher_id, session.slots.style_id, session.slots.branch_id,
        )

        # Resolve style first; save immediately so it persists even if teacher needs clarification (Fix D)
        resolved_style_id: int | str | None = None
        if style_raw and style_raw.strip():
            styles = await self._resolver.resolve_style(style_raw.strip(), self._tenant_id)
            if len(styles) == 1:
                resolved_style_id = styles[0].crm_id
                await update_slots(
                    session,
                    group=styles[0].name,
                    style_id=styles[0].crm_id,
                    style_raw=style_raw.strip(),
                )

        if teacher_raw and teacher_raw.strip():
            teachers = await self._resolver.resolve_teacher(teacher_raw.strip(), self._tenant_id)
            logger.debug(
                "RESOLVE_TEACHER: raw=%s found=%s names=%s",
                teacher_raw, len(teachers), [t.name for t in teachers],
            )
            # Filter by style when user said e.g. "к Насте на heels" (Fix C)
            if len(teachers) > 1 and resolved_style_id is not None:
                try:
                    target_sid = int(resolved_style_id)
                    groups = await self._impulse.get_groups()
                    teacher_style_map: dict[int | str, set[int]] = {}
                    for g in groups:
                        if g.teacher_id is not None and g.style_id is not None:
                            teacher_style_map.setdefault(g.teacher_id, set()).add(g.style_id)
                    filtered = [
                        t for t in teachers
                        if target_sid in teacher_style_map.get(t.crm_id, set())
                    ]
                    if filtered:
                        teachers = filtered
                except Exception:
                    pass

            if len(teachers) > 1:
                names = [t.name for t in teachers]
                return f"У нас несколько преподавателей: {', '.join(names)}. К кому записать?"
            if len(teachers) == 1:
                await update_slots(
                    session,
                    teacher=teachers[0].name,
                    teacher_id=teachers[0].crm_id,
                    teacher_raw=teacher_raw.strip(),
                )
            else:
                return f"Не нашла преподавателя «{teacher_raw.strip()}». Подсказать, кто ведёт занятия?"

        if style_raw and style_raw.strip() and resolved_style_id is None:
            styles = await self._resolver.resolve_style(style_raw.strip(), self._tenant_id)
            if len(styles) > 1:
                names = [s.name for s in styles]
                return f"У нас несколько направлений: {', '.join(names)}. Какое интересно?"
            if len(styles) == 1:
                await update_slots(
                    session,
                    group=styles[0].name,
                    style_id=styles[0].crm_id,
                    style_raw=style_raw.strip(),
                )
            else:
                return f"Не нашла направление «{style_raw.strip()}». Подсказать, какие есть?"

        if branch_raw and branch_raw.strip():
            logger.info(
                "SLOTS_BEFORE_BRANCH: teacher=%s teacher_id=%s style=%s style_id=%s",
                session.slots.teacher,
                session.slots.teacher_id,
                session.slots.group,
                session.slots.style_id,
            )
            branches = await self._resolver.resolve_branch(branch_raw.strip(), self._tenant_id)
            if len(branches) > 1:
                names = [b.name for b in branches]
                return f"У нас несколько филиалов: {', '.join(names)}. В какой удобнее?"
            if len(branches) == 1:
                await update_slots(
                    session,
                    branch=branches[0].name,
                    branch_id=branches[0].crm_id,
                    branch_raw=branch_raw.strip(),
                )
                logger.info(
                    "SLOTS_AFTER_BRANCH: teacher=%s teacher_id=%s style=%s style_id=%s",
                    session.slots.teacher,
                    session.slots.teacher_id,
                    session.slots.group,
                    session.slots.style_id,
                )
            else:
                unknown = await self._resolver.check_unknown_area(
                    branch_raw.strip(), self._tenant_id
                )
                if unknown:
                    ua = self._kb.get_unknown_areas()
                    nearest = (ua.get("nearest_branches") or {}).get(unknown, [])
                    template = (ua.get("response_template") or "Ближайшие филиалы: {nearest_branches}")
                    nearest_str = ", ".join(nearest) if nearest else "уточните у администратора"
                    return template.format(nearest_branches=nearest_str)
                return f"Не нашла филиал «{branch_raw.strip()}». Подсказать, какие есть?"

        logger.debug("RESOLVE_EXIT: no clarification needed")
        return None

    async def _handle_subscription_inquiry(
        self, session: Any, parsed: CoreLLMResponse, final_message: str
    ) -> str:
        """Answer about subscriptions from KB. Do not ask direction/branch/date (RFC-004 §6)."""
        return final_message

    async def _handle_price_inquiry(
        self, session: Any, parsed: CoreLLMResponse, final_message: str
    ) -> str:
        """Answer about prices from KB. Do not redirect to booking flow (RFC-004 §6)."""
        return final_message

    async def _handle_trial_inquiry(
        self, session: Any, parsed: CoreLLMResponse, final_message: str
    ) -> str:
        """Answer about trial from KB. If user wants to book trial → redirect to booking (RFC-004 §6)."""
        return final_message

    async def _append_history(self, session: Any, user_text: str, bot_text: str) -> None:
        messages = list(session.slots.messages)
        messages.append({"role": "user", "content": user_text})
        capped_bot = bot_text[:1500] if len(bot_text) > 1500 else bot_text
        messages.append({"role": "assistant", "content": capped_bot})
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
        max_length = {"telegram": 400, "whatsapp": 200}.get(str(channel), 400)
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
        # RFC-004 entity resolver IDs — MUST reset on /start
        "teacher_id": None,
        "style_id": None,
        "branch_id": None,
        # Raw values from LLM
        "teacher_raw": None,
        "style_raw": None,
        "branch_raw": None,
        "recent_tools": [],
    }


_entity_resolver: EntityResolver | None = None


def set_entity_resolver(resolver: EntityResolver | None) -> None:
    """Set global EntityResolver (called from main.py at startup)."""
    global _entity_resolver
    _entity_resolver = resolver


def get_entity_resolver() -> EntityResolver | None:
    """Return current global EntityResolver (for resync job)."""
    return _entity_resolver


def set_availability_provider(provider: Any) -> None:
    """Set availability provider on the global engine (RFC-005, called from main.py)."""
    engine = get_conversation_engine()
    engine._availability = provider


def get_availability_provider() -> Any:
    """Return current availability provider (RFC-005, for tests/resync)."""
    engine = get_conversation_engine()
    return engine._availability


@lru_cache()
def get_conversation_engine() -> ConversationEngine:
    kb = get_kb()
    llm_router = get_llm_router()
    prompt_builder = PromptBuilder(kb)
    guardrails = GuardrailRunner(kb)
    impulse = get_impulse_adapter()
    return ConversationEngine(
        llm_router, prompt_builder, guardrails, impulse, kb, resolver=_entity_resolver
    )
