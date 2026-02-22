"""Pydantic strict models for Impulse CRM entities.

Per CONTRACT §5: Strict validation for schedule, reservation, client, group.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class Schedule(BaseModel):
    """Schedule entry model (CONTRACT §5).

    Matches Impulse CRM /schedule/list response structure.
    Regular classes use day+minutesBegin; one-time use dateBegin timestamp.
    """

    model_config = {"extra": "ignore", "populate_by_name": True}

    id: int = Field(..., description="Schedule ID")
    day: int | None = Field(None, description="Day of week (0=Mon, 6=Sun)")
    minutes_begin: int | None = Field(None, alias="minutesBegin", description="Start time in minutes from midnight")
    minutes_end: int | None = Field(None, alias="minutesEnd", description="End time in minutes from midnight")
    date_begin: int | None = Field(None, alias="dateBegin", description="Unix timestamp for one-time classes")
    regular: bool | None = Field(None, description="True if recurring weekly class")
    group: dict[str, Any] | None = Field(None, description="Group object with style, teacher, etc.")
    hall: dict[str, Any] | None = Field(None, description="Hall object")
    branch: dict[str, Any] | None = Field(None, description="Branch object")

    @property
    def style_name(self) -> str:
        """Direction name from group.style.name."""
        try:
            return self.group["style"]["name"]
        except (TypeError, KeyError):
            return "Направление не указано"

    @property
    def time_str(self) -> str:
        """Start time as HH:MM from minutesBegin."""
        if self.minutes_begin is not None:
            h, m = divmod(self.minutes_begin, 60)
            return f"{h:02d}:{m:02d}"
        # Fallback to group.age
        try:
            return self.group.get("age", "?")
        except (TypeError, AttributeError):
            return "?"

    @property
    def place_count(self) -> int | None:
        """Max students from group.placeCount."""
        try:
            return self.group.get("placeCount")
        except (TypeError, AttributeError):
            return None

    @property
    def branch_name(self) -> str:
        """Branch name."""
        try:
            return self.branch.get("name", "")
        except (TypeError, AttributeError):
            return ""

    @property
    def group_id(self) -> int | None:
        """Group ID for grouping schedules."""
        try:
            return self.group.get("id")
        except (TypeError, AttributeError):
            return None

    @property
    def teacher_name(self) -> str | None:
        """Teacher name from group.teacher1.name."""
        try:
            return self.group.get("teacher1", {}).get("name")
        except (TypeError, AttributeError):
            return None


class Group(BaseModel):
    """Group model (CONTRACT §5)."""

    id: int = Field(..., description="Group ID")
    name: str = Field(..., description="Group name")
    style_id: int | None = Field(None, description="Style ID")
    teacher_id: int | None = Field(None, description="Teacher ID")
    description: str | None = Field(None, description="Group description")
    is_active: bool | None = Field(None, description="Is active")


class Client(BaseModel):
    """Client model (CONTRACT §5). Matches Impulse CRM /client structure."""

    model_config = {"extra": "ignore"}

    id: int = Field(..., description="Client ID")
    name: str = Field(..., description="Client first name")
    phone: list[str] | str | None = Field(None, description="Phone numbers (array in CRM)")

    @property
    def phone_str(self) -> str:
        """Return first phone as string."""
        if isinstance(self.phone, list):
            return self.phone[0] if self.phone else ""
        return self.phone or ""


class Reservation(BaseModel):
    """Reservation/booking model (CONTRACT §5). Matches Impulse CRM /reservation structure."""

    model_config = {"extra": "ignore"}

    id: int = Field(..., description="Reservation ID")
    client: dict[str, Any] | None = Field(None, description="Client object")
    schedule: dict[str, Any] | None = Field(None, description="Schedule object")
    date: str | None = Field(None, description="Reservation date")
    notes: str | None = Field(None, description="Notes")


class Teacher(BaseModel):
    """Teacher model (CONTRACT §5)."""

    id: int = Field(..., description="Teacher ID")
    name: str = Field(..., description="Teacher name")
    phone: str | None = Field(None, description="Phone number")
    email: str | None = Field(None, description="Email")
    is_active: bool | None = Field(None, description="Is active")


class Hall(BaseModel):
    """Hall/room model (CONTRACT §5)."""

    id: int = Field(..., description="Hall ID")
    name: str = Field(..., description="Hall name")
    capacity: int | None = Field(None, description="Capacity")
    is_active: bool | None = Field(None, description="Is active")


class Style(BaseModel):
    """Style/dance direction model (CONTRACT §5)."""

    id: int = Field(..., description="Style ID")
    name: str = Field(..., description="Style name")
    description: str | None = Field(None, description="Description")
    is_active: bool | None = Field(None, description="Is active")


class ImpulseListResponse(BaseModel):
    """Impulse CRM list response wrapper."""

    data: list[dict[str, Any]] = Field(..., description="List of records")
    total: int | None = Field(None, description="Total count")
    page: int | None = Field(None, description="Current page")
    limit: int | None = Field(None, description="Page limit")


class ImpulseErrorResponse(BaseModel):
    """Impulse CRM error response."""

    error: str = Field(..., description="Error message")
    code: str | None = Field(None, description="Error code")
    details: dict[str, Any] | None = Field(None, description="Error details")

