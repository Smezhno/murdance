"""Knowledge base loader and validator.

Per CONTRACT §15: Load and validate studio.yaml against schema v1.0.
Fail-fast on invalid schema (app must not start).
"""

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field, field_validator

from app.config import get_settings


class StudioInfo(BaseModel):
    """Studio information section."""

    name: str = Field(..., description="Studio name")
    address: str = Field(..., description="Studio address")
    phone: str = Field(..., description="Studio phone number")
    schedule: str = Field(..., description="General schedule description")
    timezone: str = Field(default="Asia/Vladivostok", description="Timezone")


class Tone(BaseModel):
    """Tone section."""

    style: str = Field(..., description="Communication style")
    pronouns: str = Field(..., description="Pronouns to use")
    emoji: bool = Field(default=True, description="Allow emoji")
    language: str = Field(default="ru", description="Language code")


class Service(BaseModel):
    """Service definition."""

    id: str = Field(..., description="Service ID")
    name: str = Field(..., description="Service name")
    description: str = Field(..., description="Service description")
    price_single: float | None = Field(default=None, description="Single class price")


class Teacher(BaseModel):
    """Teacher definition. No rating field per CONTRACT §15."""

    id: str = Field(..., description="Teacher ID")
    name: str = Field(..., description="Teacher name")
    styles: list[str] = Field(..., description="Dance styles")
    specialization: str = Field(..., description="Specialization/bio")

    @field_validator("styles")
    @classmethod
    def validate_styles(cls, v: list[str]) -> list[str]:
        """Ensure styles is a non-empty list."""
        if not v:
            raise ValueError("styles must be a non-empty list")
        return v


class FAQ(BaseModel):
    """FAQ entry."""

    q: str = Field(..., description="Question")
    a: str = Field(..., description="Answer")


class Holiday(BaseModel):
    """Holiday period."""

    from_date: str = Field(..., alias="from", description="Start date (YYYY-MM-DD)")
    to_date: str = Field(..., alias="to", description="End date (YYYY-MM-DD)")
    name: str = Field(..., description="Holiday name")
    message: str = Field(..., description="Message to show during holiday")

    @field_validator("from_date", "to_date")
    @classmethod
    def validate_date_format(cls, v: str) -> str:
        """Validate date format YYYY-MM-DD."""
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            raise ValueError(f"Date must be in YYYY-MM-DD format, got {v}")
        return v


class ScheduleEntry(BaseModel):
    """Schedule entry for a class."""

    service_id: str = Field(..., description="Service/dance style ID")
    teacher_id: str = Field(..., description="Teacher ID")
    day: str = Field(..., description="Day of week (monday, tuesday, etc.)")
    time: str = Field(..., description="Time in HH:MM format")
    duration_minutes: int = Field(..., description="Class duration in minutes")
    max_students: int = Field(..., description="Maximum students")
    level: str = Field(..., description="Level (начинающие, продолжающие, etc.)")
    room: str = Field(..., description="Room name")

    @field_validator("time")
    @classmethod
    def validate_time_format(cls, v: str) -> str:
        """Validate time format HH:MM."""
        if not re.match(r"^\d{2}:\d{2}$", v):
            raise ValueError(f"Time must be in HH:MM format, got {v}")
        return v


class Subscription(BaseModel):
    """Subscription/abonement definition."""

    id: str = Field(..., description="Subscription ID")
    name: str = Field(..., description="Subscription name")
    classes: int = Field(..., description="Number of classes (-1 for unlimited)")
    price: float = Field(..., description="Price in rubles")
    validity_days: int = Field(..., description="Validity period in days")


class Policy(BaseModel):
    """Policy definition."""

    cancellation: str = Field(..., description="Cancellation policy")
    trial_class: str = Field(..., description="Trial class policy")
    what_to_bring: str = Field(..., description="What to bring to class")
    late_arrival: str = Field(..., description="Late arrival policy")


class Escalation(BaseModel):
    """Escalation configuration."""

    triggers: list[str] = Field(..., description="Trigger phrases for escalation")


class KnowledgeBase(BaseModel):
    """Knowledge base schema v1.0 (CONTRACT §15)."""

    schema_version: str = Field(..., description="Schema version (must be 1.0)")
    studio: StudioInfo = Field(..., description="Studio information")
    tone: Tone = Field(..., description="Tone settings")
    services: list[Service] = Field(..., description="Services list")
    teachers: list[Teacher] = Field(..., description="Teachers list")
    schedule: list[ScheduleEntry] = Field(default_factory=list, description="Class schedule")
    subscriptions: list[Subscription] = Field(default_factory=list, description="Subscriptions")
    policies: Policy | None = Field(default=None, description="Studio policies")
    faq: list[FAQ] = Field(default_factory=list, description="FAQ entries")
    holidays: list[Holiday] = Field(default_factory=list, description="Holiday periods")
    escalation: Escalation = Field(..., description="Escalation configuration")

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, v: str) -> str:
        """Ensure schema version is 1.0."""
        if v != "1.0":
            raise ValueError(f"Schema version must be 1.0, got {v}")
        return v

    @field_validator("services")
    @classmethod
    def validate_services(cls, v: list[Service]) -> list[Service]:
        """Ensure at least one service exists."""
        if not v:
            raise ValueError("At least one service must be defined")
        return v

    @field_validator("teachers")
    @classmethod
    def validate_teachers(cls, v: list[Teacher]) -> list[Teacher]:
        """Ensure at least one teacher exists."""
        if not v:
            raise ValueError("At least one teacher must be defined")
        return v

    def search_faq(self, query: str) -> list[FAQ]:
        """Search FAQ entries by question text (case-insensitive).

        Returns matching FAQ entries sorted by relevance.
        """
        query_lower = query.lower()
        matches: list[tuple[FAQ, int]] = []

        for faq_entry in self.faq:
            question_lower = faq_entry.q.lower()
            if query_lower in question_lower:
                # Simple relevance: position of match (earlier = more relevant)
                position = question_lower.find(query_lower)
                matches.append((faq_entry, position))

        # Sort by position (earlier matches first)
        matches.sort(key=lambda x: x[1])
        return [faq for faq, _ in matches]

    def get_service_by_id(self, service_id: str) -> Service | None:
        """Get service by ID."""
        for service in self.services:
            if service.id == service_id:
                return service
        return None

    def get_teacher_by_id(self, teacher_id: str) -> Teacher | None:
        """Get teacher by ID."""
        for teacher in self.teachers:
            if teacher.id == teacher_id:
                return teacher
        return None

    def find_classes_by_style(self, style: str) -> list[ScheduleEntry]:
        """Find classes by dance style/service ID."""
        return [entry for entry in self.schedule if entry.service_id == style]

    def find_classes_by_day(self, day: str) -> list[ScheduleEntry]:
        """Find classes by day of week."""
        day_lower = day.lower()
        return [entry for entry in self.schedule if entry.day.lower() == day_lower]

    def find_classes_by_teacher(self, teacher_id: str) -> list[ScheduleEntry]:
        """Find classes by teacher ID."""
        return [entry for entry in self.schedule if entry.teacher_id == teacher_id]

    def get_next_class(self, style: str, current_datetime: datetime | None = None) -> ScheduleEntry | None:
        """Get next upcoming class for a given style.

        Args:
            style: Service/dance style ID
            current_datetime: Current datetime (defaults to now in studio timezone)

        Returns:
            Next scheduled class or None if not found
        """
        if current_datetime is None:
            timezone = ZoneInfo(self.studio.timezone)
            current_datetime = datetime.now(timezone)

        current_day = current_datetime.strftime("%A").lower()
        current_time = current_datetime.strftime("%H:%M")

        # Day order for week
        day_order = {
            "monday": 0,
            "tuesday": 1,
            "wednesday": 2,
            "thursday": 3,
            "friday": 4,
            "saturday": 5,
            "sunday": 6,
        }

        style_classes = self.find_classes_by_style(style)
        if not style_classes:
            return None

        # Sort classes by day and time
        upcoming_classes: list[tuple[int, ScheduleEntry]] = []

        for entry in style_classes:
            entry_day_num = day_order.get(entry.day.lower(), -1)
            if entry_day_num == -1:
                continue

            current_day_num = day_order.get(current_day, -1)
            if current_day_num == -1:
                continue

            # Check if class is today and time is later, or if it's a future day
            if entry_day_num > current_day_num or (
                entry_day_num == current_day_num and entry.time > current_time
            ):
                upcoming_classes.append((entry_day_num * 10000 + int(entry.time.replace(":", "")), entry))

        if not upcoming_classes:
            # If no classes found this week, return first class of next week
            if style_classes:
                return min(style_classes, key=lambda e: day_order.get(e.day.lower(), 999))

        # Return earliest upcoming class
        upcoming_classes.sort(key=lambda x: x[0])
        return upcoming_classes[0][1] if upcoming_classes else None

    def format_schedule_text(self) -> str:
        """Format schedule as human-readable text for LLM."""
        if not self.schedule:
            return "Расписание пока не заполнено."

        lines = ["Расписание занятий:\n"]
        day_names_ru = {
            "monday": "Понедельник",
            "tuesday": "Вторник",
            "wednesday": "Среда",
            "thursday": "Четверг",
            "friday": "Пятница",
            "saturday": "Суббота",
            "sunday": "Воскресенье",
        }

        # Group by day
        by_day: dict[str, list[ScheduleEntry]] = {}
        for entry in self.schedule:
            day_ru = day_names_ru.get(entry.day.lower(), entry.day.capitalize())
            if day_ru not in by_day:
                by_day[day_ru] = []
            by_day[day_ru].append(entry)

        # Sort days
        day_order_ru = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
        sorted_days = sorted(by_day.keys(), key=lambda d: day_order_ru.index(d) if d in day_order_ru else 999)

        for day in sorted_days:
            entries = sorted(by_day[day], key=lambda e: e.time)
            lines.append(f"\n{day}:")
            for entry in entries:
                service = self.get_service_by_id(entry.service_id)
                teacher = self.get_teacher_by_id(entry.teacher_id)
                service_name = service.name if service else entry.service_id
                teacher_name = teacher.name if teacher else entry.teacher_id
                lines.append(
                    f"  {entry.time} - {service_name} ({entry.level}) - {teacher_name} - {entry.room}"
                )

        return "\n".join(lines)

    def format_for_llm(self) -> str:
        """Format full KB context for system prompt."""
        lines = [
            f"Студия: {self.studio.name}",
            f"Адрес: {self.studio.address}",
            f"Телефон: {self.studio.phone}",
            f"\nНаправления:",
        ]

        for service in self.services:
            price_info = f"{service.price_single}₽" if service.price_single else "цена уточняется"
            lines.append(f"- {service.name} ({service.id}): {service.description}. Разовое занятие: {price_info}")

        lines.append("\nПреподаватели:")
        for teacher in self.teachers:
            styles_str = ", ".join(teacher.styles)
            lines.append(f"- {teacher.name} ({teacher.id}): {styles_str}. {teacher.specialization}")

        if self.subscriptions:
            lines.append("\nАбонементы:")
            for sub in self.subscriptions:
                classes_str = "безлимит" if sub.classes == -1 else f"{sub.classes} занятий"
                lines.append(f"- {sub.name}: {sub.price}₽ ({classes_str}, действует {sub.validity_days} дней)")

        lines.append("\n" + self.format_schedule_text())

        if self.policies:
            lines.append("\nПравила студии:")
            lines.append(f"- Отмена: {self.policies.cancellation}")
            lines.append(f"- Пробное занятие: {self.policies.trial_class}")
            lines.append(f"- Что взять с собой: {self.policies.what_to_bring}")
            lines.append(f"- Опоздание: {self.policies.late_arrival}")

        if self.faq:
            lines.append("\nЧастые вопросы:")
            for faq in self.faq[:5]:  # Limit to first 5 for brevity
                lines.append(f"Q: {faq.q}\nA: {faq.a}")

        return "\n".join(lines)


# Global KB instance
_kb: KnowledgeBase | None = None


def load_knowledge_base(path: str | None = None) -> KnowledgeBase:
    """Load and validate knowledge base from YAML file.

    Per CONTRACT §15: Invalid schema → app refuses to start.
    """
    global _kb

    if _kb is not None:
        return _kb

    settings = get_settings()
    kb_path = Path(path or settings.kb_file_path)

    if not kb_path.exists():
        raise FileNotFoundError(f"Knowledge base file not found: {kb_path}")

    with open(kb_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not data:
        raise ValueError(f"Knowledge base file is empty: {kb_path}")

    try:
        _kb = KnowledgeBase(**data)
    except Exception as e:
        raise ValueError(f"Invalid knowledge base schema in {kb_path}: {e}") from e

    return _kb


def get_kb() -> KnowledgeBase:
    """Get global knowledge base instance."""
    if _kb is None:
        raise RuntimeError("Knowledge base not loaded. Call load_knowledge_base() first.")
    return _kb


def reload_knowledge_base(path: str | None = None) -> KnowledgeBase:
    """Reload knowledge base from YAML file.

    Resets the global KB instance and loads fresh data.
    """
    global _kb
    _kb = None
    return load_knowledge_base(path)
