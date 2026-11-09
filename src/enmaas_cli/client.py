"""Async HTTP against the EnMaaS gateway, with retry classification."""

from __future__ import annotations

import asyncio
import math
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .auth import REDACTION, redact
from .dialect import Request

# Re-exported so existing importers of this name from this module keep working.
from .errors import GatewayError as GatewayError

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
# 90% of the measured ~300s gateway ceiling.
GATEWAY_CEILING_NEAR = 270.0
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_FACTOR = 2.0
JITTER_FRACTION = 0.5
# A gateway or proxy that sends a huge Retry-After would otherwise stall the
# run for as long as it asks.
MAX_RETRY_AFTER_SECONDS = 60.0
ERROR_BODY_CHARS = 500

SleepFn = Callable[[float], Awaitable[None]]


def is_retryable(status: int, elapsed: float = 0.0) -> bool:
    """Service faults are retryable; request faults are not.

    A malformed prompt or revoked key fails identically on the fourth attempt.

    The exception is a slow 504. The gateway was measured timing out at ~300s,
    so a 504 that took that long is the gateway's own ceiling, not a transient
    blip; retrying it three times spends twenty minutes to fail.
    """
    if status == 504 and elapsed >= GATEWAY_CEILING_NEAR:
        return False
    return status in RETRYABLE_STATUS


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        wait = float(raw)
    except ValueError:
        return None
    # NaN fails every comparison, so it would pass a plain min() unchanged.
    if math.isnan(wait):
        return None
    return min(max(wait, 0.0), MAX_RETRY_AFTER_SECONDS)


def _error_detail(body: str, secret: str) -> str:
    """Redact the key from an error body, then cut it to a bounded length.

    The cut never splits the placeholder, so a reader can still see that a
    key was removed.
    """
    redacted = redact(body, secret)
    cut = ERROR_BODY_CHARS
    start = redacted.find(REDACTION, max(0, cut - len(REDACTION) + 1))
    if 0 <= start < cut:
        cut = start + len(REDACTION)
    return redacted[:cut]


def _backoff(attempt: int) -> float:
    delay = BACKOFF_BASE_SECONDS * (BACKOFF_FACTOR ** (attempt - 1))
    return delay + random.uniform(0.0, delay * JITTER_FRACTION)


async def call(
    client: httpx.AsyncClient,
    base_url: str,
    request: Request,
    *,
    timeout: float,
    retries: int,
    sleep: SleepFn = asyncio.sleep,
    elapsed_fn: Callable[[], float] | None = None,
    secret: str = "",
) -> dict[str, Any]:
    """POST a built request, retrying service faults. Returns the parsed JSON.

    When `secret` is set, every copy of it in an error body is replaced
    before the body is cut to length. Redacting after the cut missed a key
    that straddled the cut, and the part before it survived.
    """
    last_status: int | None = None
    last_detail = ""
    started = time.monotonic()

    def _elapsed() -> float:
        return elapsed_fn() if elapsed_fn is not None else time.monotonic() - started

    for attempt in range(1, retries + 2):
        try:
            response = await client.post(
                f"{base_url}{request.path}",
                headers=request.headers,
                json=request.body,
                timeout=timeout,
            )
        except httpx.TransportError as exc:
            last_status, last_detail = None, f"{type(exc).__name__}: {exc}"
            if _elapsed() >= GATEWAY_CEILING_NEAR:
                hint = (
                    " (the gateway's own ~300s ceiling; set --thinking off, "
                    "shorten the prompt, or lower --max-tokens)"
                )
                raise GatewayError(
                    f"{last_detail}{hint}",
                    status=None,
                    attempts=attempt,
                ) from None
        else:
            if response.status_code == 200:
                return dict(response.json())
            last_status = response.status_code
            last_detail = _error_detail(response.text, secret)
            if not is_retryable(response.status_code, _elapsed()):
                hint = ""
                if response.status_code == 504:
                    hint = (
                        " (the gateway's own ~300s ceiling; set --thinking off, "
                        "shorten the prompt, or lower --max-tokens)"
                    )
                raise GatewayError(
                    f"{response.status_code}: {last_detail}{hint}",
                    status=response.status_code,
                    attempts=attempt,
                )
            wait = _retry_after(response)
            if wait is None:
                wait = _backoff(attempt)
            if attempt <= retries:
                await sleep(wait)
                continue

        if attempt <= retries:
            await sleep(_backoff(attempt))

    raise GatewayError(
        f"{last_status if last_status is not None else 'transport error'}: {last_detail}",
        status=last_status,
        attempts=retries + 1,
    )
