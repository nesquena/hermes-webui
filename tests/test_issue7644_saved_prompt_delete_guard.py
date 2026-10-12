"""#7644 saved prompts — deliberate two-step delete + recoverable persistence.

A single click on the popup's 12px ✕ fired ``DELETE /api/prompts`` immediately
(empty ``catch``, no confirmation), and the server rewrote ``saved_prompts.json``
in place with no backup, so a mis-click destroyed a prompt permanently.

These tests pin the fix on both layers:
  * the frontend DELETE must be gated behind an armed/confirm state that
    auto-expires, and a failed delete must keep the row and show a toast;
  * the persistence layer must publish atomically (temp + rename) and keep one
    known-good generation that the loader falls back to when the primary file
    cannot be parsed.
"""
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def _body(source: str, header: str) -> str:
    """Return the brace-matched block that starts at `header`."""
    start = source.index(header)
    depth = 0
    for i in range(start, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
    raise AssertionError(f"unterminated block: {header}")


def test_delete_needs_a_second_click_within_a_window() -> None:
    """First click only arms the row's ✕ (auto-disarms after ~3s); the DELETE
    request must be unreachable from that first click."""
    js = read("static/messages.js")
    handler = _body(js, "del.onclick=async(e)=>")
    delete_at = handler.index("method:'DELETE'")
    guard_at = handler.index("if(!del.classList.contains('is-armed')){")
    arm_at = handler.index("add('is-armed')", guard_at)
    bail_at = handler.index("return;", arm_at)
    assert guard_at < arm_at < bail_at < delete_at, (
        "the first click must add the armed state and return before any request"
    )
    assert "setTimeout" in handler and "clearTimeout" in handler, (
        "the armed state must auto-expire so a stale ✕ can't delete later"
    )


def test_delete_failure_is_surfaced_and_keeps_the_row() -> None:
    """Errors used to vanish into catch(_e){}; now the popup must toast and
    not re-render (row stays) when the DELETE fails."""
    js = read("static/messages.js")
    handler = _body(js, "del.onclick=async(e)=>")
    assert "catch(_e){}" not in handler, "delete errors must not be silently discarded"
    failure_path = handler.split("catch(_e){", 1)[1]
    assert "showToast" in failure_path, "a failed delete must surface a toast"
    assert "return;" in failure_path.split("}catch", 1)[0], (
        "a failed delete must bail out before the popup re-renders"
    )


def test_delete_control_has_a_44px_hit_target() -> None:
    """The 12px icon must keep a >=44x44 target: 8px padding box (28px) plus an
    8px invisible halo on every side."""
    css = read("static/style.css")
    rule = _body(css, ".saved-prompt-delete{")
    assert "padding:8px" in rule, "delete button padding box must be 28px"
    halo = _body(css, ".saved-prompt-delete::after{")
    assert "inset:-8px" in halo, "the ::after halo must add 8px per side (44px total)"
    assert ".saved-prompt-delete.is-armed{" in css, "armed state needs its own style"


def test_save_is_atomic_and_keeps_one_generation(tmp_path, monkeypatch) -> None:
    from api import routes

    p = tmp_path / "webui" / "saved_prompts.json"
    monkeypatch.setattr(routes, "_saved_prompts_path", lambda: p)

    routes._save_saved_prompts([{"id": "a", "text": "one"}])
    assert json.loads(p.read_text(encoding="utf-8")) == [{"id": "a", "text": "one"}]
    assert not Path(str(p) + ".tmp").exists(), "temp file must be renamed away"

    routes._save_saved_prompts([{"id": "b", "text": "two"}])
    assert json.loads(p.read_text(encoding="utf-8")) == [{"id": "b", "text": "two"}]
    bak = json.loads(Path(str(p) + ".bak").read_text(encoding="utf-8"))
    assert bak == [{"id": "a", "text": "one"}], (
        "the previous generation must survive as saved_prompts.json.bak"
    )
    assert routes._load_saved_prompts() == [{"id": "b", "text": "two"}]


def test_loader_recovers_from_a_corrupt_primary(tmp_path, monkeypatch) -> None:
    from api import routes

    p = tmp_path / "webui" / "saved_prompts.json"
    monkeypatch.setattr(routes, "_saved_prompts_path", lambda: p)

    routes._save_saved_prompts([{"id": "a", "text": "one"}])
    routes._save_saved_prompts([{"id": "a", "text": "one"}, {"id": "b", "text": "two"}])
    p.write_text("{torn write", encoding="utf-8")  # crash mid-rewrite
    assert routes._load_saved_prompts() == [{"id": "a", "text": "one"}], (
        "a corrupt primary must fall back to the .bak generation, not []"
    )


def test_loader_returns_empty_when_nothing_exists(tmp_path, monkeypatch) -> None:
    from api import routes

    monkeypatch.setattr(routes, "_saved_prompts_path", lambda: tmp_path / "webui" / "saved_prompts.json")
    assert routes._load_saved_prompts() == []


def test_save_uses_the_shared_atomic_writer(tmp_path, monkeypatch) -> None:
    """The publish must ride api.paths._atomic_write_text (the repo's shared
    temp+fsync+os.replace chokepoint that preserves mode/uid/gid and handles
    symlinks/hard links), not a bespoke truncate of the live file."""
    import inspect

    from api import routes

    source = inspect.getsource(routes._save_saved_prompts)
    assert "from api.paths import _atomic_write_text" in source, (
        "_save_saved_prompts must delegate the atomic publish to api.paths"
    )
    in_place = source.replace("_atomic_write_text(", "")
    assert "write_text(" not in in_place and "open(" not in in_place, (
        "an in-place write_text/open is the #7644 truncation bug"
    )


def test_a_corrupt_primary_never_overwrites_the_good_backup(tmp_path, monkeypatch) -> None:
    """Rotating blindly would let a torn saved_prompts.json clobber the one
    known-good .bak generation, destroying the only recovery source."""
    from api import routes

    p = tmp_path / "webui" / "saved_prompts.json"
    monkeypatch.setattr(routes, "_saved_prompts_path", lambda: p)

    routes._save_saved_prompts([{"id": "a", "text": "one"}])
    routes._save_saved_prompts([{"id": "a", "text": "one"}, {"id": "b", "text": "two"}])
    p.write_text("{torn write", encoding="utf-8")  # crash mid-rewrite

    routes._save_saved_prompts([{"id": "c", "text": "three"}])

    bak = Path(str(p) + ".bak")
    assert json.loads(bak.read_text(encoding="utf-8")) == [{"id": "a", "text": "one"}], (
        "the good backup generation must survive a corrupt primary rotation"
    )
    assert json.loads(p.read_text(encoding="utf-8")) == [{"id": "c", "text": "three"}]
