"""Impulse CRM integration errors."""


class CRMUnavailableError(Exception):
    """CRM network/API error — should propagate to circuit breaker."""

    pass
