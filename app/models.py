"""Pydantic v2 models for data boundaries.

All data models use strict validation.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator


class MessageType(str, Enum):
    """Message type enumeration."""

    TEXT = "text"
    VOICE = "voice"
    STICKER = "sticker"
    IMAGE = "image"


class Channel(str, Enum):
    """Channel enumeration."""

    TELEGRAM = "telegram"
    WHATSAPP = "whatsapp"


class UnifiedMessage(BaseModel):
    """Unified message model for inbound messages (CONTRACT §8).

    Must have: channel, chat_id, message_id, timestamp, text, message_type, sender_phone.
    Note: Not using strict mode to allow chat_id coercion from int (Telegram sends int).
    """

    channel: Literal["telegram", "whatsapp"] = Field(..., description="Message channel")
    chat_id: str = Field(..., description="Chat ID (unique per channel)")
    message_id: str = Field(..., description="Message ID (unique per channel)")
    timestamp: datetime = Field(..., description="Message timestamp")
    text: str = Field(default="", description="Message text content")
    message_type: Literal["text", "voice", "sticker", "image"] = Field(
        default="text",
        description="Message type",
    )
    sender_phone: str | None = Field(
        default=None,
        description="Sender phone number (auto-filled from WhatsApp)",
    )
    sender_name: str | None = Field(
        default=None,
        description="Sender name (if available from channel)",
    )
    raw_payload: dict = Field(
        default_factory=dict,
        description="Raw webhook payload, never log in full",
    )
    trace_id: UUID = Field(default_factory=uuid4, description="Trace ID for observability")

    @field_validator("text")
    @classmethod
    def validate_text(cls, v: str) -> str:
        """Ensure text is not None."""
        return v or ""


class ConversationState(str, Enum):
    """FSM conversation states (CONTRACT §7)."""

    IDLE = "IDLE"
    COLLECTING_INTENT = "COLLECTING_INTENT"
    BROWSING_SCHEDULE = "BROWSING_SCHEDULE"
    COLLECTING_GROUP = "COLLECTING_GROUP"
    COLLECTING_DATETIME = "COLLECTING_DATETIME"
    COLLECTING_CONTACT = "COLLECTING_CONTACT"
    CONFIRM_BOOKING = "CONFIRM_BOOKING"
    BOOKING_IN_PROGRESS = "BOOKING_IN_PROGRESS"
    BOOKING_DONE = "BOOKING_DONE"
    CANCEL_FLOW = "CANCEL_FLOW"
    SERIAL_BOOKING = "SERIAL_BOOKING"
    HANDOFF_TO_ADMIN = "HANDOFF_TO_ADMIN"
    ADMIN_RESPONDING = "ADMIN_RESPONDING"


class SlotValues(BaseModel):
    """Slot values for booking flow (CONTRACT §7)."""

    group: str | None = None
    datetime_raw: str | None = None
    datetime_resolved: datetime | None = None
    client_name: str | None = None
    client_phone: str | None = None
    schedule_id: str | None = None
    messages: list[dict[str, str]] = Field(
        default_factory=list,
        description="Conversation history (last 10 messages for LLM context)",
    )


class Session(BaseModel):
    """Conversation session model (CONTRACT §4, §7).

    Stored in Redis with TTL 24h.
    Contains FSM state and slot values.
    Note: Not using strict mode to allow chat_id coercion from int (Telegram sends int).
    """

    trace_id: UUID = Field(..., description="Trace ID for observability")
    channel: Literal["telegram", "whatsapp"] = Field(..., description="Channel")
    chat_id: str = Field(..., description="Chat ID")
    state: ConversationState = Field(
        default=ConversationState.IDLE,
        description="Current FSM state",
    )
    slots: SlotValues = Field(
        default_factory=SlotValues,
        description="Slot values for booking flow",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Session creation time",
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Last update time",
    )
    expires_at: datetime = Field(..., description="Session expiration time (TTL 24h)")

    def update(self) -> None:
        """Update the updated_at timestamp."""
        self.updated_at = datetime.now(timezone.utc)


class BookingRequest(BaseModel):
    """Booking request model (CONTRACT §7).

    Required booking slots: group, datetime, client_name, client_phone, confirmation.
    """

    model_config = {"strict": True}

    group: str = Field(..., description="Group/class identifier")
    datetime: datetime = Field(..., description="Booking datetime (timezone: Asia/Vladivostok)")
    client_name: str = Field(..., description="Client name")
    client_phone: str = Field(..., description="Client phone number")
    confirmation: bool = Field(..., description="Explicit confirmation (must be True)")

    @field_validator("confirmation")
    @classmethod
    def validate_confirmation(cls, v: bool) -> bool:
        """Ensure confirmation is explicitly True."""
        if not v:
            raise ValueError("Confirmation must be explicitly True")
        return v

