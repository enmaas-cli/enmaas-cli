import pytest


@pytest.fixture(autouse=True)
def _no_gateway_url_from_the_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a gateway URL set in the developer's shell out of every test.

    A test that needs one sets it itself, so the result never depends on
    who runs the suite.
    """
    monkeypatch.delenv("ENMAAS_BASE_URL", raising=False)
