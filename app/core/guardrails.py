"""Guardrail system for Conversation Engine v2 (RFC-003 §6).

Validates every LLM response before it reaches the client.
Hard checks run first on the original message, auto-fixes apply after.
G6 is handled externally (engine catches tool execution errors).
"""

import re
import unicodedata
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from app.core.prompt_builder import LLMResponse, ToolCall
from app.core.slot_tracker import ConversationPhase
from app.knowledge.base import KnowledgeBase
from app.models import SlotValues

_STUDIO_TZ = ZoneInfo("Asia/Vladivostok")

# Minimum 3 digits before ₽ to avoid matching stray numbers like "в 8 ₽"
_PRICE_RE = re.compile(r"\b(\d{3,5})\s*₽")
_TIME_RE = re.compile(r"\b(\d{1,2}:\d{2})\b")
_WEEKDAYS_RU = {
    # nominative
    "понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье",
    # accusative / prepositional (common in "приходи в пятницу", "в среду")
    "понедельника", "вторника", "среды", "четверга", "пятницы", "субботы", "воскресенья",
    "понедельнике", "вторнике", "среде", "четверге", "пятнице", "субботе", "воскресенье",
    "понедельнику", "вторнику", "среду", "четвергу", "пятницу", "субботу", "воскресенью",
    # abbreviations
    "пн", "вт", "ср", "чт", "пт", "сб", "вс",
}
_COMPARISON_WORDS = {"лучше", "хуже", "сильнее", "слабее"}
_REQUIRED_BOOKING_SLOTS = ("group", "datetime_resolved", "client_name", "client_phone")
_PRICE_TOLERANCE = 50  # ₽


class GuardrailResult(BaseModel):
    passed: bool
    violations: list[str] = []
    corrected_message: str | None = None  # populated by G7/G8 auto-fixes


class GuardrailRunner:
    """Runs all guardrail checks on an LLM response (RFC-003 §6.1).

    Execution order:
      1. Hard checks on the original message (collect violations).
      2. Auto-fixes on the (possibly unchanged) message (G7, G8).
    This ensures G9 and other hard checks see the full original message,
    not one that was already truncated by G7.
    """

    def __init__(self, kb: KnowledgeBase) -> None:
        self._kb = kb

    async def check(
        self,
        llm_response: LLMResponse,
        slots: SlotValues,
        phase: ConversationPhase,
        crm_schedule: list | None = None,
        executed_tools: set[str] | None = None,
    ) -> GuardrailResult:
        violations: list[str] = []
        message = llm_response.message
        tool_names = {tc.name for tc in llm_response.tool_calls}
        if executed_tools:
            tool_names |= executed_tools
        tool_map = {tc.name: tc for tc in llm_response.tool_calls}

        # --- Hard checks (original message) ---
        # G12 is the primary schedule guard (no tool_call → block immediately).
        # G1 is secondary — only runs when get_filtered_schedule was called but
        # the time in the message doesn't match CRM data. They must not fire together.
        g12 = self._g12_schedule_mention_without_tool(message, tool_names, phase=phase)
        violations += g12
        if not g12 and "get_filtered_schedule" in tool_names:
            violations += self._g1_schedule_hallucination(message, crm_schedule)

        violations += self._g2_price_hallucination(message)
        violations += self._g3_booking_slots(llm_response, slots)
        violations += self._g4_booking_confirmation(tool_names, slots)
        violations += self._g5_teacher_comparison(message)
        violations += self._g9_receipt_completeness(message, slots, phase)
        violations += self._g10_schedule_id_exists(tool_map, crm_schedule)
        violations += self._g11_datetime_in_future(llm_response)
        violations += self._g13_intent_context(llm_response)

        # --- Auto-fixes (applied after hard checks) ---
        corrected = self._g7_truncate(message)
        corrected = self._g8_strip_emoji(corrected)
        corrected_message = corrected if corrected != message else None

        return GuardrailResult(
            passed=len(violations) == 0,
            violations=violations,
            corrected_message=corrected_message,
        )

    # -------------------------------------------------------------------------
    # G1 — Schedule times in message must exist in CRM data
    # -------------------------------------------------------------------------

    def _g1_schedule_hallucination(self, message: str, crm_schedule: list | None) -> list[str]:
        if not crm_schedule:
            return []
        times_in_message = set(_TIME_RE.findall(message))
        words = set(message.lower().split())
        has_weekday = bool(words & _WEEKDAYS_RU)
        if not times_in_message and not has_weekday:
            return []
        crm_times = {str(e.get("time", "")) for e in crm_schedule if isinstance(e, dict)}
        unknown = times_in_message - crm_times
        if unknown:
            return [f"G1: times {unknown} not found in CRM schedule"]
        return []

    # -------------------------------------------------------------------------
    # G2 — Prices in message must match KB (±50₽ tolerance)
    # -------------------------------------------------------------------------

    def _g2_price_hallucination(self, message: str) -> list[str]:
        prices_in_message = [int(m) for m in _PRICE_RE.findall(message)]
        if not prices_in_message:
            return []
        kb_prices: set[float] = set()
        for sub in self._kb.subscriptions:
            kb_prices.add(sub.price)
        for svc in self._kb.services:
            if svc.price_single:
                kb_prices.add(svc.price_single)
        violations = []
        for price in prices_in_message:
            if not any(abs(price - kp) <= _PRICE_TOLERANCE for kp in kb_prices):
                violations.append(f"G2: price {price}₽ not found in KB (±{_PRICE_TOLERANCE}₽)")
        return violations

    # -------------------------------------------------------------------------
    # G3 — All required slots must be filled before booking intent
    # -------------------------------------------------------------------------

    def _g3_booking_slots(self, llm_response: LLMResponse, slots: SlotValues) -> list[str]:
        # Only enforce slot requirements when actually attempting to CREATE a booking.
        # intent="booking" just means "we're in booking flow" — not "create booking now".
        is_booking = any(
            tc.name == "create_booking" for tc in llm_response.tool_calls
        )
        if not is_booking:
            return []
        missing = [k for k in _REQUIRED_BOOKING_SLOTS if not getattr(slots, k, None)]
        if missing:
            return [f"G3: missing required slots for booking: {missing}"]
        return []

    # -------------------------------------------------------------------------
    # G4 — create_booking only after explicit confirmation
    # -------------------------------------------------------------------------

    def _g4_booking_confirmation(self, tool_names: set[str], slots: SlotValues) -> list[str]:
        if "create_booking" not in tool_names:
            return []
        if not slots.confirmed:
            return ["G4: create_booking called without slots.confirmed=True"]
        return []

    # -------------------------------------------------------------------------
    # G5 — No teacher comparisons
    # -------------------------------------------------------------------------

    def _g5_teacher_comparison(self, message: str) -> list[str]:
        msg_lower = message.lower()
        words = set(msg_lower.split())
        if not (words & _COMPARISON_WORDS):
            return []
        teacher_names_found = []
        for teacher in self._kb.teachers:
            for part in teacher.name.split():
                if _normalize(part) in msg_lower:
                    teacher_names_found.append(teacher.name)
                    break
        if len(teacher_names_found) >= 2:
            return [f"G5: teacher comparison detected ({teacher_names_found})"]
        return []

    # -------------------------------------------------------------------------
    # G9 — POST_BOOKING receipt must contain address + dress code
    # -------------------------------------------------------------------------

    def _address_key(self, full_address: str) -> str:
        """First part of address before parenthesis — used for G9 check."""
        return full_address.split("(")[0].strip()

    def _g9_receipt_completeness(
        self, message: str, slots: SlotValues, phase: ConversationPhase
    ) -> list[str]:
        if phase != ConversationPhase.POST_BOOKING:
            return []
        # G9 applies only to the actual receipt message, not all POST_BOOKING responses.
        # Receipt is identified by the ✅ confirmation marker or explicit phrase.
        is_receipt = "✅" in message or "запись подтверждена" in message.lower()
        if not is_receipt:
            return []
        violations = []
        if slots.branch:
            addr = self._kb.get_branch_address(slots.branch)
            if addr and not _contains_substring(message, self._address_key(addr)):
                violations.append(f"G9: receipt missing branch address for '{slots.branch}'")
        if slots.group:
            dc = self._kb.get_dress_code(slots.group)
            if dc and not _contains_substring(message, dc[:20]):
                violations.append(f"G9: receipt missing dress code for '{slots.group}'")
        return violations

    # -------------------------------------------------------------------------
    # G10 — schedule_id in create_booking must exist in CRM
    # -------------------------------------------------------------------------

    def _g10_schedule_id_exists(
        self, tool_map: dict[str, ToolCall], crm_schedule: list | None
    ) -> list[str]:
        if "create_booking" not in tool_map or not crm_schedule:
            return []
        schedule_id = tool_map["create_booking"].parameters.get("schedule_id")
        if schedule_id is None:
            return []
        crm_ids = {str(e.get("id", "")) for e in crm_schedule if isinstance(e, dict)}
        if str(schedule_id) not in crm_ids:
            return [f"G10: schedule_id {schedule_id!r} not found in CRM schedule"]
        return []

    # -------------------------------------------------------------------------
    # G11 — datetime in slot_updates must be in the future
    # -------------------------------------------------------------------------

    def _g11_datetime_in_future(self, llm_response: LLMResponse) -> list[str]:
        dt_value = llm_response.slot_updates.get("datetime_resolved") or \
                   llm_response.slot_updates.get("datetime")
        if dt_value is None:
            return []
        try:
            if isinstance(dt_value, str):
                dt = datetime.fromisoformat(dt_value)
            elif isinstance(dt_value, datetime):
                dt = dt_value
            else:
                return []
            # Normalise both to UTC for comparison
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_STUDIO_TZ)
            now_utc = datetime.now(timezone.utc)
            if dt.astimezone(timezone.utc) <= now_utc:
                return [f"G11: datetime {dt_value!r} is not in the future"]
        except (ValueError, TypeError):
            return [f"G11: could not parse datetime {dt_value!r}"]
        return []

    # -------------------------------------------------------------------------
    # G13 — buy_subscription / ask_price must not ask about direction, branch, datetime
    # -------------------------------------------------------------------------

    def _g13_intent_context(self, llm_response: LLMResponse) -> list[str]:
        if llm_response.intent not in ("buy_subscription", "ask_price"):
            return []
        msg = llm_response.message.lower()
        booking_question_phrases = [
            "какое направление",
            "какой филиал",
            "в каком филиале",
            "когда хотите",
            "в какой день",
            "на какой день",
            "какое время",
            "во сколько",
            "записать на",
            "записаться на",
            "какое направление вас интересует",
            "какой филиал удобнее",
        ]
        for phrase in booking_question_phrases:
            if phrase in msg:
                return [
                    "G13: buy_subscription/ask_price response must not ask about "
                    "direction, branch, or datetime"
                ]
        return []

    # -------------------------------------------------------------------------
    # G12 — Schedule/time mention requires get_filtered_schedule tool_call
    # -------------------------------------------------------------------------

    def _g12_schedule_mention_without_tool(
        self, message: str, tool_names: set[str], phase: ConversationPhase | None = None
    ) -> list[str]:
        # POST_BOOKING: bot answers from confirmed slot data (datetime_resolved),
        # not from a live schedule query — time mentions are legitimate here.
        if phase == ConversationPhase.POST_BOOKING:
            return []
        if "get_filtered_schedule" in tool_names:
            return []
        has_time = bool(_TIME_RE.search(message))
        words = set(message.lower().split())
        has_weekday = bool(words & _WEEKDAYS_RU)
        if has_time or has_weekday:
            return ["G12: message mentions schedule/time without calling get_filtered_schedule"]
        return []

    # -------------------------------------------------------------------------
    # G7 — Auto-fix: truncate message > 300 chars at sentence boundary
    # -------------------------------------------------------------------------

    def _g7_truncate(self, message: str) -> str:
        if len(message) <= 300:
            return message
        truncated = message[:300]
        last_boundary = max(
            truncated.rfind("."),
            truncated.rfind("!"),
            truncated.rfind("?"),
        )
        if last_boundary > 0:
            return truncated[: last_boundary + 1]
        return truncated

    # -------------------------------------------------------------------------
    # G8 — Auto-fix: strip emoji beyond 2
    # -------------------------------------------------------------------------

    def _g8_strip_emoji(self, message: str) -> str:
        emoji_positions = [i for i, ch in enumerate(message) if _is_emoji(ch)]
        if len(emoji_positions) <= 2:
            return message
        # Remove all emoji beyond the first 2, scanning from the end
        to_remove = set(emoji_positions[2:])
        return "".join(ch for i, ch in enumerate(message) if i not in to_remove)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    return text.lower().strip()


def _contains_substring(haystack: str, needle: str) -> bool:
    """Case-insensitive substring check."""
    return needle.lower() in haystack.lower()


def _is_emoji(char: str) -> bool:
    """Return True if char is an emoji character."""
    cp = ord(char)
    return (
        0x1F600 <= cp <= 0x1F64F  # emoticons
        or 0x1F300 <= cp <= 0x1F5FF  # misc symbols & pictographs
        or 0x1F680 <= cp <= 0x1F6FF  # transport & map
        or 0x1F900 <= cp <= 0x1F9FF  # supplemental symbols
        or 0x2600 <= cp <= 0x26FF    # misc symbols
        or 0x2700 <= cp <= 0x27BF    # dingbats
        or 0xFE00 <= cp <= 0xFE0F    # variation selectors
        or 0x1FA00 <= cp <= 0x1FA6F  # chess symbols etc.
        or 0x1FA70 <= cp <= 0x1FAFF  # food & drink etc.
    )
