"""Policy Enforcer: hard rules enforcement.

Per CONTRACT §11: Hard rules table enforced in code, not just prompt.
"""

from typing import Any


class PolicyEnforcer:
    """Policy enforcer for LLM responses (CONTRACT §11)."""

    def __init__(self) -> None:
        """Initialize policy enforcer."""
        self._kb = None

    @property
    def kb(self):
        """Get knowledge base (lazy load)."""
        if self._kb is None:
            from app.knowledge.base import get_kb

            self._kb = get_kb()
        return self._kb

    def check_schedule_requires_tool_call(self, response_text: str, tool_calls: list[dict[str, Any]]) -> bool:
        """Check if booking response requires tool call (CONTRACT §11).

        Rule: Booking actions → require tool_call
        Schedule display does NOT require tool_call (comes from KB).

        Args:
            response_text: LLM response text
            tool_calls: List of tool calls from LLM

        Returns:
            True if rule is satisfied, False if violated
        """
        # Keywords that indicate BOOKING intent (not schedule display)
        booking_keywords = ["записаться", "забронировать", "бронировать", "запись на", "запись к"]

        text_lower = response_text.lower()

        # Check if response mentions booking (not just schedule display)
        mentions_booking = any(keyword in text_lower for keyword in booking_keywords)

        if mentions_booking:
            # Must have tool call for booking actions
            return len(tool_calls) > 0

        return True  # No booking mentioned, rule satisfied

    def check_price_matches_kb(self, response_text: str) -> tuple[bool, str]:
        """Check if price in response matches KB (CONTRACT §11).

        Rule: Price in response → must match KB

        Args:
            response_text: LLM response text

        Returns:
            Tuple of (is_valid, error_message)
        """
        import re

        # Extract prices from response (format: 800₽, 800 руб, 800 рублей)
        price_pattern = r"(\d+)\s*(?:₽|руб|рублей|руб\.)"
        prices_found = re.findall(price_pattern, response_text, re.IGNORECASE)

        if not prices_found:
            return True, ""  # No prices mentioned, rule satisfied

        # Check each price against KB services
        kb_prices: set[float] = set()
        for service in self.kb.services:
            if service.price_single:
                kb_prices.add(service.price_single)

        for price_str in prices_found:
            try:
                price = float(price_str)
                if price not in kb_prices:
                    return False, f"Price {price}₽ not found in KB"
            except ValueError:
                continue

        return True, ""

    def check_tool_failed_fallback(self, tool_calls: list[dict[str, Any]], tool_results: list[Any]) -> tuple[bool, str]:
        """Check if tool failed requires fallback message (CONTRACT §11).

        Rule: Tool failed → "checking with admin"

        Args:
            tool_calls: List of tool calls
            tool_results: List of tool results (None indicates failure)

        Returns:
            Tuple of (is_valid, error_message)
        """
        if not tool_calls:
            return True, ""

        # Check if any tool call failed (result is None or error)
        failed_tools = [
            i for i, result in enumerate(tool_results) if result is None or (isinstance(result, dict) and "error" in result)
        ]

        if failed_tools:
            # This will be checked by the caller - tool failure should trigger fallback
            return True, "tool_failed"  # Signal that fallback is needed

        return True, ""

    def enforce(self, response_text: str, tool_calls: list[dict[str, Any]], tool_results: list[Any] | None = None) -> tuple[bool, str]:
        """Enforce all policy rules (CONTRACT §11).

        Args:
            response_text: LLM response text
            tool_calls: List of tool calls from LLM
            tool_results: Optional list of tool results

        Returns:
            Tuple of (is_valid, error_message)
        """
        # Rule 1: Schedule/booking → require tool_call
        if not self.check_schedule_requires_tool_call(response_text, tool_calls):
            return False, "Schedule/booking response requires tool call"

        # Rule 2: Price in response → must match KB
        price_valid, price_error = self.check_price_matches_kb(response_text)
        if not price_valid:
            return False, price_error

        # Rule 3: Tool failed → fallback (checked by caller)
        if tool_results is not None:
            tool_valid, tool_error = self.check_tool_failed_fallback(tool_calls, tool_results)
            if tool_error == "tool_failed":
                # Signal that fallback message is needed
                return True, "tool_failed"

        return True, ""


# Global policy enforcer instance
policy_enforcer = PolicyEnforcer()
