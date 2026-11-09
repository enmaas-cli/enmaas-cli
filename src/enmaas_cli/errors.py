"""Failures of a single request to the gateway.

Every class derives from CallError, so a caller can catch one type and still
read the usage the reply reported. Tokens that were billed count against the
budget even when the request failed.
"""

from __future__ import annotations

from .tokens import Usage


class CallError(Exception):
    """One request failed. `usage` is what the reply reported, so the charge is kept."""

    def __init__(self, message: str, usage: Usage | None = None) -> None:
        super().__init__(message)
        self.usage = usage if usage is not None else Usage()


class GatewayError(CallError):
    """A request failed after exhausting its retry budget, or failed terminally."""

    def __init__(self, message: str, *, status: int | None, attempts: int) -> None:
        super().__init__(message)
        self.status = status
        self.attempts = attempts


class EmptyResponseError(CallError):
    """HTTP 200 but no usable text. Treated as a failure, never a success.

    Carries the usage, because these tokens were charged. The gateway bills
    a run that reasoned itself to death just as it bills a useful one, so a
    budget that could not see this would be blind to the most expensive
    failure the README documents.
    """


class BadReplyError(CallError):
    """HTTP 200 whose body is not a usable reply.

    The message never quotes the body, because a 200 body can be model
    output.
    """
