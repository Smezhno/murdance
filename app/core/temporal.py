"""Temporal Parser: resolve relative dates in CODE, not LLM.

Per CONTRACT §7: Datetime resolution in code, timezone Asia/Vladivostok.
Per RFC CC-2: TemporalParser в коде, LLM не вычисляет даты.
"""

import re
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from app.config import get_settings
from pydantic import BaseModel, Field


class TemporalResult(BaseModel):
    """Temporal parsing result."""

    date: date | None = Field(None, description="Resolved date")
    time: str | None = Field(None, description="Resolved time (HH:MM format)")
    confidence: Literal["high", "medium", "low"] = Field("low", description="Confidence level")
    ambiguous: bool = Field(False, description="Whether result is ambiguous")
    raw_input: str = Field(..., description="Original user input")
    error: str | None = Field(None, description="Error message if parsing failed")


class TemporalParser:
    """Temporal parser for Russian relative dates (CONTRACT §7, RFC CC-2)."""

    def __init__(self, timezone: str | None = None) -> None:
        """Initialize temporal parser.

        Args:
            timezone: Timezone string (defaults to config timezone or Asia/Vladivostok)
        """
        if timezone is None:
            settings = get_settings()
            timezone = settings.timezone
        self.timezone = ZoneInfo(timezone)

    def parse(self, text: str, now: datetime | None = None) -> TemporalResult:
        """Parse relative date/time from Russian text.

        Args:
            text: User input text (Russian)
            now: Optional current datetime for testing (defaults to now in timezone)

        Returns:
            TemporalResult with resolved date/time or error
        """
        if now is None:
            now = datetime.now(self.timezone)
        else:
            # Ensure now is timezone-aware
            if now.tzinfo is None:
                now = now.replace(tzinfo=self.timezone)
            else:
                now = now.astimezone(self.timezone)

        text_lower = text.lower().strip()

        # Try to parse date patterns
        result = self._parse_date(text_lower, now)
        if result.date is not None:
            # Try to extract time
            time_str, time_confidence = self._parse_time(text_lower)
            result.time = time_str
            if time_confidence == "low":
                result.confidence = "medium" if result.confidence == "high" else "low"
            return result

        # Try to parse time-only (today)
        time_str, time_confidence = self._parse_time(text_lower)
        if time_str:
            result.date = now.date()
            result.time = time_str
            result.confidence = time_confidence if time_confidence != "low" else "medium"
            return result

        return TemporalResult(raw_input=text, error="Не удалось распознать дату или время")

    def _parse_date(self, text: str, now: datetime) -> TemporalResult:
        """Parse date from text.

        Args:
            text: Lowercase text
            now: Current datetime

        Returns:
            TemporalResult with date or error
        """
        # "сегодня" → today
        if re.search(r"\bсегодня\b", text):
            return TemporalResult(
                date=now.date(),
                confidence="high",
                raw_input=text,
            )

        # "завтра" → tomorrow
        if re.search(r"\bзавтра\b", text):
            tomorrow = now.date() + timedelta(days=1)
            return TemporalResult(
                date=tomorrow,
                confidence="high",
                raw_input=text,
            )

        # "послезавтра" → day after tomorrow
        if re.search(r"\bпослезавтра\b", text):
            day_after = now.date() + timedelta(days=2)
            return TemporalResult(
                date=day_after,
                confidence="high",
                raw_input=text,
            )

        # "в среду" / "на среду" → next Wednesday (or this Wednesday if not passed yet)
        day_match = re.search(r"\b(?:в|на)\s+(понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье)\b", text)
        if day_match:
            day_name = day_match.group(1)
            day_map = {
                "понедельник": 0,
                "вторник": 1,
                "среду": 2,
                "четверг": 3,
                "пятницу": 4,
                "субботу": 5,
                "воскресенье": 6,
            }
            target_weekday = day_map.get(day_name)
            if target_weekday is not None:
                days_ahead = target_weekday - now.weekday()
                if days_ahead <= 0:  # Target day already passed this week
                    days_ahead += 7  # Next week
                target_date = now.date() + timedelta(days=days_ahead)
                return TemporalResult(
                    date=target_date,
                    confidence="high",
                    raw_input=text,
                )

        # Day-of-week WITHOUT preposition: "понедельник 19:00"
        day_match_no_prep = re.search(r"\b(понедельник|вторник|среду?|четверг|пятницу?|субботу?|воскресенье)\b", text)
        if day_match_no_prep:
            day_name = day_match_no_prep.group(1)
            day_map_no_prep = {
                "понедельник": 0,
                "вторник": 1,
                "среда": 2,
                "среду": 2,
                "четверг": 3,
                "пятница": 4,
                "пятницу": 4,
                "суббота": 5,
                "субботу": 5,
                "воскресенье": 6,
            }
            target_weekday = day_map_no_prep.get(day_name)
            if target_weekday is not None:
                days_ahead = target_weekday - now.weekday()
                if days_ahead <= 0:  # Target day already passed this week
                    days_ahead += 7  # Next week
                target_date = now.date() + timedelta(days=days_ahead)
                return TemporalResult(
                    date=target_date,
                    confidence="high",
                    raw_input=text,
                )

        # "на 5-е" / "5 числа" → 5th of current month (or next month if past)
        day_number_match = re.search(r"\b(?:на\s+)?(\d{1,2})(?:-е|ого|числа)\b", text)
        if day_number_match:
            day_num = int(day_number_match.group(1))
            if 1 <= day_num <= 31:
                # Try current month
                try:
                    target_date = date(now.year, now.month, day_num)
                    if target_date < now.date():
                        # Past date, try next month
                        if now.month == 12:
                            target_date = date(now.year + 1, 1, day_num)
                        else:
                            target_date = date(now.year, now.month + 1, day_num)
                    return TemporalResult(
                        date=target_date,
                        confidence="high",
                        raw_input=text,
                    )
                except ValueError:
                    # Invalid date (e.g., Feb 30)
                    return TemporalResult(
                        raw_input=text,
                        error=f"Неверная дата: {day_num} число",
                    )

        # Try absolute date formats: "15.12.2024" or "15/12/2024" or "2024-12-15"
        date_patterns = [
            r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b",  # DD.MM.YYYY
            r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b",  # DD/MM/YYYY
            r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b",  # YYYY-MM-DD
        ]

        for pattern in date_patterns:
            match = re.search(pattern, text)
            if match:
                try:
                    if pattern.startswith(r"\b(\d{4})"):  # YYYY-MM-DD
                        year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
                    else:  # DD.MM.YYYY or DD/MM/YYYY
                        day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
                    target_date = date(year, month, day)

                    # Check if past date
                    if target_date < now.date():
                        return TemporalResult(
                            raw_input=text,
                            error=f"Прошедшая дата: {target_date.strftime('%d.%m.%Y')}. Предлагаю ближайшее занятие.",
                        )

                    return TemporalResult(
                        date=target_date,
                        confidence="high",
                        raw_input=text,
                    )
                except ValueError:
                    continue

        return TemporalResult(raw_input=text, error="Не удалось распознать дату")

    def _parse_time(self, text: str) -> tuple[str | None, str]:
        """Parse time from text (helper method).

        Args:
            text: Lowercase text

        Returns:
            Tuple of (time_str, confidence) or (None, "low") if not found
        """
        # Time patterns: "19:00", "19.00", "19 часов"
        time_patterns = [
            r"\b(\d{1,2}):(\d{2})\b",  # HH:MM
            r"\b(\d{1,2})\.(\d{2})\b",  # HH.MM
            r"\b(\d{1,2})\s*(?:часов?|ч\.?)\s*(?:(\d{1,2})\s*(?:минут?|мин\.?))?\b",  # "19 часов" or "19 часов 30 минут"
        ]

        for pattern in time_patterns:
            match = re.search(pattern, text)
            if match:
                try:
                    hour = int(match.group(1))
                    minute = int(match.group(2)) if match.group(2) else 0

                    # Validate hour and minute
                    if hour < 0 or hour > 23:
                        continue
                    if minute < 0 or minute > 59:
                        continue

                    time_str = f"{hour:02d}:{minute:02d}"
                    return time_str, "high"
                except (ValueError, IndexError):
                    continue

        # "7 вечера", "3 дня", "10 утра" with AM/PM conversion
        am_pm_match = re.search(r"\b(\d{1,2})\s*(вечера|утра|дня)\b", text)
        if am_pm_match:
            try:
                hour = int(am_pm_match.group(1))
                period = am_pm_match.group(2)

                if period == "утра":
                    # утра: as-is (already in 24h format for morning)
                    if hour < 1 or hour > 12:
                        return None, "low"
                    # Keep as-is (1-11 for morning), 12 утра = midnight
                    if hour == 12:
                        hour = 0  # 12 утра = midnight
                elif period == "вечера":
                    # вечера: +12 if hour < 12, 12 вечера = midnight
                    if hour < 1 or hour > 12:
                        return None, "low"
                    if hour == 12:
                        hour = 0  # 12 вечера = midnight
                    elif hour < 12:
                        hour += 12  # 1-11 вечера → 13:00-23:00
                elif period == "дня":
                    # дня: +12 if hour < 12, 12 дня = noon
                    if hour < 1 or hour > 12:
                        return None, "low"
                    if hour == 12:
                        hour = 12  # 12 дня = noon (12:00)
                    elif hour < 12:
                        hour += 12  # 1-11 дня → 13:00-23:00

                time_str = f"{hour:02d}:00"
                return time_str, "high"
            except (ValueError, IndexError):
                pass

        # Try "вечером", "утром", "днем" (approximate times)
        if re.search(r"\bвечером\b", text):
            return "19:00", "low"
        if re.search(r"\bутром\b", text):
            return "10:00", "low"
        if re.search(r"\bдн[её]м\b", text):
            return "14:00", "low"

        return None, "low"


def get_temporal_parser(timezone: str | None = None) -> TemporalParser:
    """Get temporal parser instance.

    Args:
        timezone: Optional timezone override (defaults to config timezone)

    Returns:
        TemporalParser instance
    """
    return TemporalParser(timezone=timezone)

