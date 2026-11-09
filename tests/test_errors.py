"""The request failure classes share one base that carries the charged usage."""

from __future__ import annotations

import pytest

from enmaas_cli.errors import BadReplyError, CallError, EmptyResponseError, GatewayError
from enmaas_cli.tokens import Usage


@pytest.mark.parametrize("cls", [GatewayError, EmptyResponseError, BadReplyError])
def test_subclasses_derive_from_call_error(cls: type[CallError]) -> None:
    assert issubclass(cls, CallError)


def test_usage_defaults_to_empty() -> None:
    assert CallError("x").usage == Usage()
    assert EmptyResponseError("x").usage == Usage()
    assert BadReplyError("x").usage == Usage()
    assert GatewayError("x", status=500, attempts=1).usage == Usage()


def test_usage_is_kept_when_given() -> None:
    usage = Usage(input_tokens=3, output_tokens=4)
    assert BadReplyError("x", usage).usage == usage
    assert EmptyResponseError("x", usage).usage == usage


def test_gateway_error_keeps_status_and_attempts() -> None:
    exc = GatewayError("boom", status=503, attempts=4)
    assert exc.status == 503
    assert exc.attempts == 4
    assert str(exc) == "boom"


def test_old_import_paths_give_the_same_classes() -> None:
    from enmaas_cli import client, dialect, errors, tokens

    assert client.GatewayError is errors.GatewayError
    assert dialect.EmptyResponseError is errors.EmptyResponseError
    assert dialect.Usage is tokens.Usage
