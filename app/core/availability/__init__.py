"""Group availability — protocol and implementations (RFC-005)."""

from app.core.availability.protocol import (
    AvailabilityStatus,
    GroupAvailability,
    GroupAvailabilityProvider,
)

__all__ = [
    "AvailabilityStatus",
    "GroupAvailability",
    "GroupAvailabilityProvider",
]
