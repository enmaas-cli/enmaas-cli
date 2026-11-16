"""The receipt and the --json object list a bounded number of failures, each cut short.

A run with many failures would otherwise print a receipt as long as the
index. The index keeps every message whole, and the skill gives commands to
read it without printing the file.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from enmaas_cli.auth import REDACTION, error_text, redact
from enmaas_cli.cli import main
from enmaas_cli.receipts import FAILURE_LIMIT, FAILURE_MESSAGE_CHARS, map_receipt
from enmaas_cli.runner import MapOutcome

KEY = "sk-bounds-test-key-42"
SKILL = Path(__file__).resolve().parents[1] / "skills" / "enmaas" / "SKILL.md"
# The example index path the skill's commands name.
SKILL_INDEX = "/tmp/triage/results.jsonl"
LONG = "E" * 600
REPLY = {
    "choices": [{"finish_reason": "stop", "message": {"content": "done"}}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
}


def _run_map(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    count: int,
    *extra: str,
    detail: str = LONG,
    batch: bool = False,
) -> int:
    """A map over `count` items where every odd item fails with a transport error.

    Failures alternate with successes, so the breaker never trips. A
    transport error's text is not cut by the client, so the index holds it
    whole.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("ENMAAS_KEY", KEY)
    monkeypatch.setenv("ENMAAS_BASE_URL", "https://gw.example.test")
    monkeypatch.delenv("ENMAAS_NO_USAGE_LOG", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["messages"][-1]["content"]
        ids = re.findall(r"=== ITEM (\S+) ===", prompt) if batch else []
        fails = int(ids[0][1:]) % 4 == 2 if batch else bool(int(prompt.removeprefix("do ")) % 2)
        if fails:
            raise httpx.ConnectError(detail)
        if batch:
            text = "\n".join(f"=== ANSWER {item_id} ===\ndone" for item_id in ids)
            reply = {**REPLY, "choices": [{"finish_reason": "stop", "message": {"content": text}}]}
            return httpx.Response(200, json=reply)
        return httpx.Response(200, json=REPLY)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **kw: real_client(transport=httpx.MockTransport(handler)),
    )
    items = tmp_path / "items.jsonl"
    items.write_text(
        "".join(json.dumps({"id": f"i{n:02d}", "x": n}) + "\n" for n in range(count)),
        encoding="utf-8",
    )
    template = tmp_path / "t.txt"
    template.write_text(("shared\n===PER ITEM===\n" if batch else "") + "do {x}", encoding="utf-8")
    argv = ["--config", str(tmp_path / "none.toml"), "map", "-m", "rits/zai-org/glm-5-3"]
    argv += ["--dialect", "openai", "--items", str(items), "--template", str(template)]
    argv += ["--outdir", str(tmp_path / "out"), "--retries", "0", "--concurrency", "1", *extra]
    if batch:
        argv += ["--batch", "2"]
    return main(argv)


def _failed_ids(count: int) -> list[str]:
    return [f"i{n:02d}" for n in range(count) if n % 2]


def _index_errors(tmp_path: Path) -> dict[str, str]:
    lines = (tmp_path / "out" / "results.jsonl").read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines if line.strip()]
    return {row["id"]: row["error"] for row in rows if row.get("status") == "error"}


def _outcome(failures: list[tuple[str, str]]) -> MapOutcome:
    return MapOutcome(
        ok=0,
        failed=len(failures),
        skipped=0,
        attempted=len(failures),
        total=len(failures),
        abort_reason=None,
        input_tokens=0,
        output_tokens=0,
        cached_tokens=0,
        reasoning_tokens=0,
        elapsed=1.0,
        outdir=Path("out"),
        failures=failures,
    )


def _failure_lines(receipt: str) -> list[str]:
    return [line for line in receipt.split("\n") if line.startswith("  failure")]


def test_the_receipt_lists_twenty_failures_and_counts_the_rest() -> None:
    failures = [(f"i{n}", f"boom {n}") for n in range(25)]
    receipt = map_receipt(_outcome(failures), model="m")
    lines = _failure_lines(receipt)
    assert len(lines) == FAILURE_LIMIT == 20
    assert lines[-1] == "  failure   i19  boom 19"
    assert "            5 more in the index" in receipt.split("\n")


def test_the_receipt_with_twenty_failures_has_no_more_line() -> None:
    receipt = map_receipt(_outcome([(f"i{n}", "boom") for n in range(20)]), model="m")
    assert len(_failure_lines(receipt)) == 20
    assert "more in the index" not in receipt


def test_the_receipt_cuts_each_message_after_making_it_printable() -> None:
    receipt = map_receipt(_outcome([("a", "x\n" + "y" * 600)]), model="m")
    (line,) = _failure_lines(receipt)
    message = line.removeprefix("  failure   a  ")
    assert len(message) == FAILURE_MESSAGE_CHARS == 200
    assert message.startswith("x y")


def test_the_receipt_ends_before_a_crossing_placeholder() -> None:
    message = "x" * 195 + REDACTION + "tail"
    (line,) = _failure_lines(map_receipt(_outcome([("a", message)]), model="m"))
    assert line == "  failure   a  " + "x" * 195


@pytest.mark.parametrize(("count", "truncated"), [(50, True), (40, False)])
def test_map_json_lists_at_most_twenty_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    count: int,
    truncated: bool,
) -> None:
    assert _run_map(tmp_path, monkeypatch, count, "--json") == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["failed"] == count // 2
    assert [f["id"] for f in payload["failures"]] == _failed_ids(count)[:20]
    assert payload["failures_truncated"] is truncated


def test_a_long_message_is_cut_in_the_receipt_and_json_and_whole_in_the_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run_map(tmp_path, monkeypatch, 2, "--json") == 2
    (failure,) = json.loads(capsys.readouterr().out)["failures"]
    assert len(failure["error"]) == 200
    whole = _index_errors(tmp_path)["i01"]
    assert len(whole) > 600
    assert whole.endswith(LONG)
    assert failure["error"] == whole[:200]

    assert _run_map(tmp_path / "again", monkeypatch, 2) == 2
    (line,) = _failure_lines(capsys.readouterr().out)
    assert line == f"  failure   i01  {whole[:200]}"


def test_a_key_at_the_cut_never_shows_in_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    detail = "x" * 160 + KEY + "y" * 100
    assert _run_map(tmp_path, monkeypatch, 2, "--json", detail=detail) == 2
    (failure,) = json.loads(capsys.readouterr().out)["failures"]
    assert failure["error"] == "transport error: ConnectError: " + "x" * 160
    assert len(failure["error"]) <= 200
    assert KEY[:4] not in failure["error"].removesuffix(REDACTION)
    assert KEY not in _index_errors(tmp_path)["i01"]


def _skill_commands() -> list[str]:
    """Every indented code block in the skill that starts with `python3 -I -c`."""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in SKILL.read_text(encoding="utf-8").split("\n"):
        if line.startswith("    ") and (current is not None or line[4:].strip()):
            if current is None:
                current = []
                blocks.append(current)
            current.append(line[4:])
        elif not line.strip() and current is not None:
            current.append("")
        else:
            current = None
    commands = ["\n".join(block).strip("\n") for block in blocks]
    found = [command for command in commands if command.startswith("python3")]
    # Isolated mode: otherwise the working directory is first on the import
    # path, and a json.py in a repository would run.
    assert all(command.startswith("python3 -I -c") for command in found), found
    return found


def _run_skill_command(command: str, index: Path, cwd: Path | None = None) -> list[str]:
    assert command.count(SKILL_INDEX) == 1
    script = command.replace(SKILL_INDEX, str(index))
    # Run by a shell exactly as written, with this interpreter as python3.
    bindir = Path(sys.executable).parent
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": f"{bindir}:/usr/bin:/bin"},
        cwd=cwd,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    return done.stdout.splitlines()


def test_the_skill_commands_count_and_list_the_failures_in_a_real_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run_map(tmp_path, monkeypatch, 50) == 2
    capsys.readouterr()
    index = tmp_path / "out" / "results.jsonl"
    count, listing = _skill_commands()
    assert _run_skill_command(count, index) == ["25"]
    lines = _run_skill_command(listing, index)
    assert [line.split(" ", 1)[0] for line in lines] == _failed_ids(50)[:20]
    errors = _index_errors(tmp_path)
    for line in lines:
        item_id, text = line.split(" ", 1)
        assert text == errors[item_id][:200]


def test_the_skill_commands_use_each_items_latest_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    index = tmp_path / "results.jsonl"
    rows: list[dict[str, Any]] = [
        {"id": "a", "status": "error", "error": "first try"},
        {"type": "request", "charge": 7, "items": 1},
        {"id": "b", "status": "error", "error": "line\nbreak"},
        {"id": "a", "status": "ok", "error": None},
    ]
    index.write_text(
        "".join(json.dumps(row) + "\n" for row in rows) + '{"torn',
        encoding="utf-8",
    )
    count, listing = _skill_commands()
    assert _run_skill_command(count, index) == ["1"]
    assert _run_skill_command(listing, index) == ["b line?break"]


def test_the_skill_commands_do_not_import_code_from_the_working_directory(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    marker = tmp_path / "marker"
    (repo / "json.py").write_text(
        f"open({str(marker)!r}, 'w').close()\nfrom importlib import import_module\n",
        encoding="utf-8",
    )
    index = repo / "results.jsonl"
    index.write_text(json.dumps({"id": "a", "status": "error", "error": "x"}) + "\n")
    for command in _skill_commands():
        _run_skill_command(command, index, cwd=repo)
        assert not marker.exists(), command


def test_the_skill_listing_prints_a_hostile_row_bounded_and_inert(tmp_path: Path) -> None:
    index = tmp_path / "results.jsonl"
    long_id = "\x1b]0;TITLE\x07\x1b[2J" + "I" * 1_000_000
    rows = [
        {"id": long_id, "status": "error", "error": "\x1b]52;c;SGVsbG8=\x07" + "E" * 1000},
        {"id": {"k": ["\x1b[31m"] * 5}, "status": "error", "error": "e"},
    ]
    index.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    _, listing = _skill_commands()
    lines = _run_skill_command(listing, index)
    output = "\n".join(lines)
    assert len(output) < 10_000
    assert "\x1b" not in output and "\x07" not in output
    first_id, first_error = lines[0].split(" ", 1)
    assert first_id == "?]0;TITLE??[2J" + "I" * 86
    assert first_error == "?]52;c;SGVsbG8=?" + "E" * 184


def test_the_skill_listing_shows_at_most_twenty_rows(tmp_path: Path) -> None:
    index = tmp_path / "results.jsonl"
    rows = [{"id": f"i{n}", "status": "error", "error": "e"} for n in range(25)]
    index.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    _, listing = _skill_commands()
    assert len(_run_skill_command(listing, index)) == 20


@pytest.mark.parametrize("length", [199, 200, 201])
def test_error_field_strict_lengths(length: int) -> None:
    from enmaas_cli.receipts import failure_line_text

    assert failure_line_text("x" * length) == "x" * min(length, 200)


# The first start puts the placeholder's end exactly at the cap, so it stays.
@pytest.mark.parametrize("start", range(200 - len(REDACTION), 201))
def test_error_field_never_splits_or_extends_placeholder(start: int) -> None:
    from enmaas_cli.receipts import failure_line_text

    message = "x" * start + REDACTION + "tail"
    result = failure_line_text(message)
    assert len(result) <= 200
    assert result == ("x" * start if start + len(REDACTION) > 200 else message[:200])


def _placeholder_starts(text: str) -> list[int]:
    """Every start of the placeholder in `text`, overlapping ones included."""
    starts = []
    start = text.find(REDACTION)
    while start >= 0:
        starts.append(start)
        start = text.find(REDACTION, start + 1)
    return starts


def test_error_text_ends_before_a_placeholder_that_overlapping_text_crosses() -> None:
    # Gateway text after the key can repeat the placeholder's tail. The
    # placeholder it forms overlaps the real one and crosses the cap.
    key = "SECRETKEYVALUE"
    message = "x" * 183 + key + "REDACTED***" + "y" * 50
    assert error_text(message, key) == "x" * 183


@pytest.mark.parametrize(
    "follow",
    ["REDACTED***", "*REDACTED***", "**REDACTED***", REDACTION, "REDACTED***REDACTED***"],
)
@pytest.mark.parametrize("start", range(200 - 3 * len(REDACTION), 201))
def test_error_text_never_cuts_inside_any_placeholder(start: int, follow: str) -> None:
    key = "SECRETKEYVALUE"
    message = "x" * start + key + follow + "y" * 50
    full = redact(message, key)
    result = error_text(message, key)
    assert len(result) <= 200
    assert full.startswith(result)
    assert key[:4] not in result
    for begin in _placeholder_starts(full):
        assert not begin < len(result) < begin + len(REDACTION), (begin, len(result))


def test_index_error_display_is_bounded_but_outcome_keeps_original() -> None:
    from dataclasses import replace

    from enmaas_cli.receipts import map_json

    original = "independent full diagnostic " * 40 + REDACTION
    outcome = replace(_outcome([]), abort_reason="index", index_error=original)
    obj = map_json(outcome, model="m")
    receipt = map_receipt(outcome, model="m")
    error_line = next(line for line in receipt.splitlines() if line.startswith("  error"))
    assert len(obj["error"]) == 200
    assert len(error_line.removeprefix("  error     ")) == 200
    assert outcome.index_error == original
    assert len(outcome.index_error) > 1000


def test_batched_failures_keep_list_limit_and_full_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run_map(tmp_path, monkeypatch, 52, "--json", batch=True) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["batch"] == 2
    assert result["failed"] == 26
    assert result["failures_truncated"] is True
    assert len(result["failures"]) == 20
    errors = _index_errors(tmp_path)
    assert len(errors) == 26
    assert set(errors.values()) == {"transport error: ConnectError: " + LONG}
    assert all(len(item["error"]) == 200 for item in result["failures"])
