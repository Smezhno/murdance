"""Impulse CRM integration."""

from app.integrations.impulse.adapter import ImpulseAdapter, get_impulse_adapter
from app.integrations.impulse.models import Client, Group, Reservation, Schedule

__all__ = [
    "ImpulseAdapter",
    "get_impulse_adapter",
    "Client",
    "Group",
    "Reservation",
    "Schedule",
]

