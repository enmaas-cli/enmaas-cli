import json
from pathlib import Path

import pytest

from enmaas_cli.runner import ValidationError, load_items, render, resume_skips


def _items(tmp_path: Path, rows: list[dict[str, object]]) -> Path:
    path = tmp_path / "items.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def test_load_items_reads_jsonl(tmp_path: Path) -> None:
    path = _items(tmp_path, [{"id": "a", "x": 1}, {"id": "b", "x": 2}])
    assert [i["id"] for i in load_items(path)] == ["a", "b"]


def test_missing_id_is_rejected(tmp_path: Path) -> None:
    path = _items(tmp_path, [{"x": 1}])
    with pytest.raises(ValidationError, match="line 1"):
        load_items(path)


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    path = _items(tmp_path, [{"id": "a"}, {"id": "a"}])
    with pytest.raises(ValidationError, match="duplicate"):
        load_items(path)


def test_malformed_line_is_rejected_with_line_number(tmp_path: Path) -> None:
    path = tmp_path / "items.jsonl"
    path.write_text('{"id": "a"}\nnot json\n', encoding="utf-8")
    with pytest.raises(ValidationError, match="line 2"):
        load_items(path)


def test_blank_lines_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "items.jsonl"
    path.write_text('{"id": "a"}\n\n{"id": "b"}\n', encoding="utf-8")
    assert len(load_items(path)) == 2


def test_render_substitutes_fields() -> None:
    assert render("Check {name}", {"id": "a", "name": "thing"}) == "Check thing"


def test_render_rejects_missing_field_naming_the_item() -> None:
    with pytest.raises(ValidationError) as exc:
        render("Check {name}", {"id": "item-7"})
    assert "item-7" in str(exc.value)
    assert "name" in str(exc.value)


def test_resume_skips_only_items_recorded_ok_with_a_file(tmp_path: Path) -> None:
    outdir = tmp_path / "out"
    outdir.mkdir()
    (outdir / "a.md").write_text("done", encoding="utf-8")
    (outdir / "c.md").write_text("done", encoding="utf-8")
    index = outdir / "results.jsonl"
    index.write_text(
        '{"id": "a", "status": "ok"}\n'
        '{"id": "b", "status": "ok"}\n'
        '{"id": "c", "status": "error"}\n',
        encoding="utf-8",
    )
    # a: ok + file present -> skip. b: ok but no file -> redo. c: error -> redo.
    assert resume_skips(outdir) == {"a"}


def test_resume_with_no_index_skips_nothing(tmp_path: Path) -> None:
    assert resume_skips(tmp_path) == set()


def test_path_traversal_id_is_rejected(tmp_path: Path) -> None:
    path = _items(tmp_path, [{"id": "../../escaped"}])
    with pytest.raises(ValidationError, match="not a safe filename"):
        load_items(path)


def test_subdir_id_is_rejected(tmp_path: Path) -> None:
    path = _items(tmp_path, [{"id": "sub/dir/item"}])
    with pytest.raises(ValidationError, match="not a safe filename"):
        load_items(path)


def test_dotdot_id_is_rejected(tmp_path: Path) -> None:
    path = _items(tmp_path, [{"id": ".."}])
    with pytest.raises(ValidationError, match="not a safe filename"):
        load_items(path)


def test_empty_id_is_rejected(tmp_path: Path) -> None:
    path = _items(tmp_path, [{"id": ""}])
    with pytest.raises(ValidationError, match="not a safe filename"):
        load_items(path)


def test_absolute_path_id_is_rejected(tmp_path: Path) -> None:
    path = _items(tmp_path, [{"id": "/etc/passwd"}])
    with pytest.raises(ValidationError, match="not a safe filename"):
        load_items(path)


def test_ordinary_id_is_accepted(tmp_path: Path) -> None:
    path = _items(tmp_path, [{"id": "item-7"}])
    items = load_items(path)
    assert len(items) == 1
    assert items[0]["id"] == "item-7"


def test_id_with_dots_is_accepted(tmp_path: Path) -> None:
    path = _items(tmp_path, [{"id": "file.v2"}])
    items = load_items(path)
    assert len(items) == 1
    assert items[0]["id"] == "file.v2"
