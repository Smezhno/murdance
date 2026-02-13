"""Pydantic strict models for Impulse CRM entities.

Per CONTRACT §5: Strict validation for schedule, reservation, client, group.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class Schedule(BaseModel):
    """Schedule entry model (CONTRACT §5)."""

    id: int = Field(..., description="Schedule ID")
    group_id: int | None = Field(None, description="Group ID")
    teacher_id: int | None = Field(None, description="Teacher ID")
    hall_id: int | None = Field(None, description="Hall ID")
    date: str = Field(..., description="Date (YYYY-MM-DD)")
    time: str = Field(..., description="Time (HH:MM)")
    duration_minutes: int | None = Field(None, description="Duration in minutes")
    max_students: int | None = Field(None, description="Maximum students")
    current_students: int | None = Field(None, description="Current number of students")
    is_active: bool | None = Field(None, description="Is active")
    created_at: str | None = Field(None, description="Created at timestamp")
    updated_at: str | None = Field(None, description="Updated at timestamp")


class Group(BaseModel):
    """Group model (CONTRACT §5)."""

    id: int = Field(..., description="Group ID")
    name: str = Field(..., description="Group name")
    style_id: int | None = Field(None, description="Style ID")
    teacher_id: int | None = Field(None, description="Teacher ID")
    description: str | None = Field(None, description="Group description")
    is_active: bool | None = Field(None, description="Is active")


class Client(BaseModel):
    """Client model (CONTRACT §5)."""

    id: int = Field(..., description="Client ID")
    name: str = Field(..., description="Client name")
    phone: str = Field(..., description="Phone number")
    email: str | None = Field(None, description="Email")
    informer_id: int | None = Field(None, description="Informer ID (source)")
    created_at: str | None = Field(None, description="Created at timestamp")
    updated_at: str | None = Field(None, description="Updated at timestamp")


class Reservation(BaseModel):
    """Reservation/booking model (CONTRACT §5)."""

    id: int = Field(..., description="Reservation ID")
    client_id: int = Field(..., description="Client ID")
    schedule_id: int = Field(..., description="Schedule ID")
    status_id: int | None = Field(None, description="Status ID")
    created_at: str | None = Field(None, description="Created at timestamp")
    updated_at: str | None = Field(None, description="Updated at timestamp")
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

