"""Idea filing stage tests (t_d21da9cd, spec t_14ad8e89 §4–§6).

``api.idea_filing`` is the filing half of idea mode: it consumes the capture
stage's idea payload, derives card title/body (aux-LLM with deterministic
fallback), dedupes against the ideas ledger + boards, creates triage cards
through a board client, spools on board-unreachable, and appends to the
shared ledger under a file lock.

These tests are fully hermetic: the board client is a fake, the ledger and
spool live in a tmp dir (via the module's env overrides, applied with
importlib.reload), and the aux-LLM call is a stub. No hermes-agent install,
no network, no microphone.
"""

from __future__ import annotations

import importlib
import json
import sys
import time
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


# ── Fake board client ──────────────────────────────────────────────────────


class FakeBoardClient:
    """In-process stand-in for the kanban bridge + CLI create paths.

    Records every create call and (optionally) fails like an unreachable
    board. Idempotency-key retries return the FIRST card id, mirroring
    ``hermes_cli.kanban_db.create_task``'s idempotency contract.
    """

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls: list[dict] = []
        self.cards: dict[str, dict] = {}
        self._next = 1

    def create(self, title: str, body: str, hash16: str):
        if self.fail:
            return None, "board-unreachable"
        self.calls.append(
            {"title": title, "body": body, "idempotency_key": f"idea-{hash16}"}
        )
        for cid, card in self.cards.items():
            if card["idempotency_key"] == f"idea-{hash16}":
                # idempotent retry: same card, no duplicate
                return cid, None
        cid = f"t_{self._next:06d}"
        self._next += 1
        self.cards[cid] = dict(self.calls[-1])
        return cid, None


def _aux_reply(cards: list[dict], language: str = "en"):
    """Build a fake OpenAI-shaped aux response carrying strict JSON."""
    payload = {"language": language, "ideas": cards}
    text = json.dumps(payload)
    msg = types.SimpleNamespace(content=text)
    choice = types.SimpleNamespace(message=msg)
    return types.SimpleNamespace(choices=[choice])


# ── Module loading with isolated ledger/spool ──────────────────────────────


@pytest.fixture()
def filing(tmp_path, monkeypatch):
    """Load ``api.idea_filing`` with ledger/spool/lock pointed at tmp_path.

    Hermetic by default: no real aux-LLM loader (``aux_call=None`` means
    deterministic fallback) and no real board-title scan — individual dupe
    tests override ``_all_board_titles`` with canned board state.
    """
    monkeypatch.setenv("HERMES_IDEAS_DIR", str(tmp_path))
    monkeypatch.delenv("HERMES_IDEAS_LEDGER", raising=False)
    sys.path.insert(0, str(REPO_ROOT))
    import api.idea_filing as mod

    module = importlib.reload(mod)
    # keep module-level Path objects pointed at the tmp dir even if the env
    # was read at first import elsewhere
    module.LEDGER_PATH = tmp_path / "ledger.json"
    module.SPOOL_PATH = tmp_path / "pending_cards.jsonl"
    module.LOCK_PATH = tmp_path / ".ledger.lock"
    module.IDEAS_DIR = tmp_path
    module._load_aux_call = lambda: None
    module._all_board_titles = lambda: ([], [])
    yield module
    sys.path.remove(str(REPO_ROOT))


def _payload(transcript: str, **over) -> dict:
    base = {
        "session_id": "20261017_000000_test",
        "source": "webui",
        "transcript": transcript,
        "ts": 1792200000.0,
        "audio_sha16": "0123456789abcdef",
        "attempts": 0,
    }
    base.update(over)
    return base


# ── Validation (spec §6.7) ─────────────────────────────────────────────────


class TestValidation:
    def test_empty_transcript_rejected(self, filing):
        board = FakeBoardClient()
        result = filing.file_ideas(_payload(""), create=board.create)
        assert result.ok is False
        assert result.kind == "nothing_heard"
        assert "🤷" in result.text
        assert result.card_ids == []
        assert board.calls == []  # no card, no crash

    def test_whitespace_transcript_rejected(self, filing):
        board = FakeBoardClient()
        result = filing.file_ideas(_payload("    \n\t  "), create=board.create)
        assert result.kind == "nothing_heard"
        assert board.calls == []

    def test_too_short_transcript_rejected(self, filing):
        board = FakeBoardClient()
        result = filing.file_ideas(_payload("hi"), create=board.create)
        assert result.kind == "nothing_heard"
        assert board.calls == []

    def test_missing_transcript_key_rejected(self, filing):
        board = FakeBoardClient()
        result = filing.file_ideas(_payload(""), create=board.create)
        assert result.kind == "nothing_heard"


# ── Happy path (spec §6.2) ─────────────────────────────────────────────────


class TestFileSingleIdea:
    def test_aux_derived_idea_files_one_card(self, filing):
        board = FakeBoardClient()
        aux = lambda **kw: _aux_reply(
            [
                {
                    "title": "Transit layer for earth-monitor",
                    "problem": "No transit data in earth-monitor.",
                    "first_action": "Add NL 9292 data source.",
                    "effort": "M",
                    "acceptance": "NL transit routes render on the map.",
                }
            ]
        )

        result = filing.file_ideas(
            _payload("we need a transit layer in earth-monitor with 9292 data"),
            aux_call=aux,
            create=board.create,
        )

        assert result.ok is True
        assert result.kind == "filed"
        assert len(result.card_ids) == 1
        cid = result.card_ids[0]
        assert result.text.startswith(f"✅ {cid}:")
        assert "Transit layer" in result.text

        call = board.calls[0]
        assert call["title"] == "IDEA: Transit layer for earth-monitor"
        assert call["title"].startswith("IDEA: ")
        assert len(call["title"]) <= len("IDEA: ") + 72
        assert call["idempotency_key"].startswith("idea-")
        assert len(call["idempotency_key"]) == len("idea-") + 16

    def test_card_body_is_five_section_template_with_verbatim_source(self, filing):
        board = FakeBoardClient()
        transcript = "we need a transit layer in earth-monitor with 9292 data"
        aux = lambda **kw: _aux_reply(
            [
                {
                    "title": "Transit layer for earth-monitor",
                    "problem": "No transit data.",
                    "first_action": "Add 9292 source.",
                    "effort": "M",
                    "acceptance": "Routes render.",
                }
            ]
        )

        filing.file_ideas(_payload(transcript), aux_call=aux, create=board.create)

        body = board.calls[0]["body"]
        for section in ("PROBLEM", "FIRST ACTION", "EFFORT", "SOURCE", "ACCEPTANCE"):
            assert section in body, f"missing section {section}"
        assert transcript in body  # verbatim quote, Dutch stays Dutch
        assert "session: 20261017_000000_test" in body
        assert "date: " in body

    def test_names_kept_verbatim(self, filing):
        board = FakeBoardClient()
        aux = lambda **kw: _aux_reply(
            [
                {
                    "title": "Podcast sources in Toolbelt and omarchy-toolbelt",
                    "problem": "p",
                    "first_action": "f",
                    "effort": "S",
                    "acceptance": "a",
                }
            ]
        )
        filing.file_ideas(
            _payload("podcasts in de Toolbelt en omarchy-toolbelt graag"),
            aux_call=aux,
            create=board.create,
        )
        assert "Toolbelt" in board.calls[0]["title"]
        assert "omarchy-toolbelt" in board.calls[0]["title"]

    def test_derive_fallback_on_aux_failure(self, filing):
        board = FakeBoardClient()

        def broken_aux(**kw):
            raise RuntimeError("aux unavailable")

        result = filing.file_ideas(
            _payload("transit layer voor earth-monitor met 9292 data als nieuwe laag"),
            aux_call=broken_aux,
            create=board.create,
        )
        assert result.ok is True
        assert result.kind == "filed"
        assert result.derives_used == ["fallback"]
        title = board.calls[0]["title"]
        assert title.startswith("IDEA: ")
        # fallback cuts the transcript itself at a word boundary ≤ 60 chars
        assert len(title) <= len("IDEA: ") + 61
        body = board.calls[0]["body"]
        assert "TBD-specify" in body
        assert "S (unreviewed)" in body

    def test_derive_fallback_on_non_json_aux_reply(self, filing):
        board = FakeBoardClient()
        msg = types.SimpleNamespace(content="Sure! Here's your idea card …")
        aux = lambda **kw: types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=msg)]
        )

        result = filing.file_ideas(
            _payload("some spoken idea about dashboards and metrics"),
            aux_call=aux,
            create=board.create,
        )
        assert result.kind == "filed"
        assert result.derives_used == ["fallback"]

    def test_aux_times_out_to_fallback_without_blocking(self, filing):
        board = FakeBoardClient()

        def slow_aux(**kw):
            raise TimeoutError("aux call exceeded 5 s budget")

        started = time.monotonic()
        result = filing.file_ideas(
            _payload("idea about caching weather tiles locally"),
            aux_call=slow_aux,
            create=board.create,
        )
        # a hanging/timing-out aux can never block filing: both attempts
        # raise, both are swallowed, the fallback card is filed
        assert result.kind == "filed"
        assert result.derives_used == ["fallback"]
        assert time.monotonic() - started < 5

    def test_ledger_appended_after_create(self, filing):
        board = FakeBoardClient()
        aux = lambda **kw: _aux_reply(
            [
                {
                    "title": "Ledger append check",
                    "problem": "p",
                    "first_action": "f",
                    "effort": "S",
                    "acceptance": "a",
                }
            ]
        )
        filing.file_ideas(
            _payload("ledger append check idea"), aux_call=aux, create=board.create
        )

        ledger = json.loads(filing.LEDGER_PATH.read_text())
        entries = ledger["entries"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["card_id"]
        assert entry["finalized"] is True
        assert entry["hash"] and len(entry["hash"]) == 16
        assert entry["quote"].startswith("ledger append")


# ── Multi-idea utterances (spec §6.4) ──────────────────────────────────────


class TestMultiIdea:
    def test_two_ideas_two_cards(self, filing):
        board = FakeBoardClient()
        aux = lambda **kw: _aux_reply(
            [
                {
                    "title": "Transit layer for earth-monitor",
                    "problem": "p1",
                    "first_action": "f1",
                    "effort": "M",
                    "acceptance": "a1",
                },
                {
                    "title": "Podcast sources in Toolbelt",
                    "problem": "p2",
                    "first_action": "f2",
                    "effort": "S",
                    "acceptance": "a2",
                },
            ]
        )

        result = filing.file_ideas(
            _payload(
                "ik wil een transit layer in earth-monitor en ook podcasts in de Toolbelt"
            ),
            aux_call=aux,
            create=board.create,
        )

        assert result.kind == "filed"
        assert len(result.card_ids) == 2
        assert len(set(result.card_ids)) == 2
        assert result.text.count("✅") == 2
        titles = [c["title"] for c in board.calls]
        assert "IDEA: Transit layer for earth-monitor" in titles
        assert "IDEA: Podcast sources in Toolbelt" in titles

    def test_more_than_three_ideas_capped(self, filing):
        board = FakeBoardClient()
        titles = [
            "Transit layer for earth-monitor",
            "Podcast sources in Toolbelt",
            "Weather tile cache on pluto",
            "Cron audit dashboard",
            "Regex cheat-sheet widget",
        ]
        ideas = [
            {
                "title": t,
                "problem": "p",
                "first_action": "f",
                "effort": "S",
                "acceptance": "a",
            }
            for t in titles
        ]
        aux = lambda **kw: _aux_reply(ideas)

        result = filing.file_ideas(
            _payload("vijf verschillende wensen in een zin"),
            aux_call=aux,
            create=board.create,
        )

        assert len(result.card_ids) == 3
        assert len(board.calls) == 3
        assert result.more_in_source == 2
        assert "(+2 more in source)" in result.text
        filed = [c["title"] for c in board.calls]
        assert "IDEA: Cron audit dashboard" not in filed  # capped out

    def test_fallback_never_splits(self, filing):
        board = FakeBoardClient()
        result = filing.file_ideas(
            _payload(
                "eerste idee over dashboards en tweede idee over cronjobs door elkaar"
            ),
            aux_call=None,  # forces fallback (no aux resolvable in tests)
            create=board.create,
        )
        assert len(result.card_ids) == 1


# ── Idempotency / dedupe (spec §6.5) ───────────────────────────────────────


class TestIdempotencyAndDedupe:
    def _file_once(
        self, filing, board, transcript="transit layer for earth-monitor 9292"
    ):
        aux = lambda **kw: _aux_reply(
            [
                {
                    "title": "Transit layer for earth-monitor",
                    "problem": "p",
                    "first_action": "f",
                    "effort": "M",
                    "acceptance": "a",
                }
            ]
        )
        return filing.file_ideas(
            _payload(transcript), aux_call=aux, create=board.create
        )

    def test_retry_same_idea_returns_same_card(self, filing):
        board = FakeBoardClient()
        first = self._file_once(filing, board)
        second = self._file_once(filing, board)

        assert first.kind == "filed"
        assert second.kind == "dupe"
        assert second.card_ids == []
        assert second.duplicate_of == first.card_ids[0]
        assert "🔁" in second.text
        assert first.card_ids[0] in second.text
        assert len(board.cards) == 1  # exactly one card on the board

    def test_dedupe_via_idempotency_key_when_ledger_empty(self, filing):
        """Network retry (ledger not yet flushed) still converges via key."""
        board = FakeBoardClient()
        aux = lambda **kw: _aux_reply(
            [
                {
                    "title": "Transit layer for earth-monitor",
                    "problem": "p",
                    "first_action": "f",
                    "effort": "M",
                    "acceptance": "a",
                }
            ]
        )
        p = _payload("transit layer for earth-monitor 9292")
        filing.file_ideas(p, aux_call=aux, create=board.create)
        # simulate ledger loss: board-level idempotency must still hold
        filing.LEDGER_PATH.unlink()
        result = filing.file_ideas(p, aux_call=aux, create=board.create)
        assert result.kind == "filed"
        assert len(board.cards) == 1  # same card returned, no duplicate

    def test_dedupe_against_existing_board_title(self, filing):
        filing._all_board_titles = lambda: (
            [("t_abc123", "IDEA: Transit layer for earth-monitor")],
            [],
        )
        board = FakeBoardClient()
        aux = lambda **kw: _aux_reply(
            [
                {
                    "title": "Transit layer for earth-monitor",
                    "problem": "p",
                    "first_action": "f",
                    "effort": "M",
                    "acceptance": "a",
                }
            ]
        )
        result = filing.file_ideas(
            _payload("transit layer for earth-monitor 9292"),
            aux_call=aux,
            create=board.create,
        )
        assert result.kind == "dupe"
        assert result.duplicate_of == "t_abc123"
        assert board.calls == []

    def test_dedupe_fuzzy_overlap_against_other_board(self, filing):
        filing._all_board_titles = lambda: (
            [],
            [("t_xyz789", "IDEA: earth-monitor transit map layer with 9292 routes")],
        )
        board = FakeBoardClient()
        aux = lambda **kw: _aux_reply(
            [
                {
                    "title": "Transit layer for earth-monitor 9292",
                    "problem": "p",
                    "first_action": "f",
                    "effort": "M",
                    "acceptance": "a",
                }
            ]
        )
        result = filing.file_ideas(
            _payload("transit layer for earth-monitor met 9292"),
            aux_call=aux,
            create=board.create,
        )
        assert result.kind == "dupe"
        assert result.duplicate_of == "t_xyz789"

    def test_dedupe_quote_match_against_ledger(self, filing):
        """Intake compares quote[:80] EQUALITY — a re-spoken identical opener
        is a dupe even when the derived title differs."""
        filing.ledger_append(
            {
                "ts": 1,
                "hash": "0" * 16,
                "quote": "identical spoken idea about widgets",
                "card_id": "t_q1",
                "title": "IDEA: Widgets idea",
            }
        )
        filing._all_board_titles = lambda: ([], [])
        board = FakeBoardClient()
        result = filing.file_ideas(
            _payload("identical spoken idea about widgets"),  # ≤80 chars: equal prefix
            aux_call=None,
            create=board.create,
        )
        assert result.kind == "dupe"
        assert result.duplicate_of == "t_q1"
        assert board.calls == []

    def test_hash_matches_intake_lane_algorithm(self, filing):
        """The dedupe hash must equal intake_run.title_hash exactly."""
        title = "IDEA: Podcast and SoundCloud sources in Toolbelt"
        expected = "a" * 0  # placeholder, computed below from the algorithm
        import hashlib

        toks = sorted(
            __import__("re").findall(
                r"[a-z0-9]{2,}", title.lower().replace("idea:", "", 1)
            )
        )
        expected = hashlib.sha256(" ".join(toks).encode()).hexdigest()[:16]
        assert filing.title_hash(title) == expected

    def test_token_overlap_requires_two_distinctive_tokens(self, filing):
        assert filing.token_overlap("IDEA: Chat fix", "IDEA: Chat improvement") == 0.0
        assert (
            filing.token_overlap(
                "IDEA: transit layer earth-monitor", "IDEA: earth-monitor transit layer"
            )
            == 1.0
        )


# ── Board unreachable → spool + recover (spec §6.6) ────────────────────────


class TestSpoolAndRecover:
    def test_unreachable_board_spools_payload(self, filing):
        board = FakeBoardClient(fail=True)
        aux = lambda **kw: _aux_reply(
            [
                {
                    "title": "Spool test idea",
                    "problem": "p",
                    "first_action": "f",
                    "effort": "S",
                    "acceptance": "a",
                }
            ]
        )

        result = filing.file_ideas(
            _payload("spool test idea about retrying later"),
            aux_call=aux,
            create=board.create,
        )

        assert result.ok is False
        assert result.kind == "spooled"
        assert "💾" in result.text
        assert "unreachable" in result.text.lower()
        assert result.spool_path == str(filing.SPOOL_PATH)

        records = [
            json.loads(l)
            for l in filing.SPOOL_PATH.read_text().splitlines()
            if l.strip()
        ]
        assert len(records) == 1
        rec = records[0]
        # intake lane's pending_cards.jsonl format (hash/title/body/…)
        for key in ("hash", "title", "body", "session_id", "date", "quote"):
            assert key in rec, f"spool record missing {key}"
        assert rec["title"].startswith("IDEA: ")

    def test_spool_record_is_batch_intake_compatible(self, filing):
        """The batch lane reads {hash,title,body,session_id,date,quote} —
        a record missing any of those would be silently dropped by it."""
        board = FakeBoardClient(fail=True)
        filing.file_ideas(
            _payload("another idea that cannot reach the board"),
            aux_call=None,
            create=board.create,
        )
        rec = json.loads(filing.SPOOL_PATH.read_text().splitlines()[0])
        assert set(rec) >= {"hash", "title", "body", "session_id", "date", "quote"}
        assert len(rec["hash"]) == 16

    def test_ledger_marks_pending_card(self, filing):
        board = FakeBoardClient(fail=True)
        filing.file_ideas(
            _payload("pending ledger entry idea"), aux_call=None, create=board.create
        )
        entries = json.loads(filing.LEDGER_PATH.read_text())["entries"]
        assert any(e.get("pending_card") for e in entries)

    def test_retry_spooled_files_once_board_back(self, filing):
        board = FakeBoardClient(fail=True)
        filing.file_ideas(
            _payload("retry me when the board returns"),
            aux_call=None,
            create=board.create,
        )
        assert filing.SPOOL_PATH.exists()

        ok_board = FakeBoardClient()
        sleeps: list[float] = []
        filed = filing.retry_spooled(
            sleep=sleeps.append, create=ok_board.create, max_rounds=1
        )

        assert filed == 1
        assert sleeps == [filing.RETRY_DELAYS[0]]
        assert len(ok_board.calls) == 1
        # spool drained and ledger finalized
        assert not filing.SPOOL_PATH.exists()
        entries = json.loads(filing.LEDGER_PATH.read_text())["entries"]
        assert any(e.get("card_id") and e.get("finalized") for e in entries)

    def test_retry_spooled_keeps_records_when_still_unreachable(self, filing):
        board = FakeBoardClient(fail=True)
        filing.file_ideas(
            _payload("keep me spooled"), aux_call=None, create=board.create
        )
        filed = filing.retry_spooled(
            sleep=lambda s: None, create=board.create, max_rounds=2
        )
        assert filed == 0
        records = [json.loads(l) for l in filing.SPOOL_PATH.read_text().splitlines()]
        assert len(records) == 1
        assert records[0]["attempts"] == 2  # batch lane takes it from here

    def test_retry_spooled_full_schedule(self, filing):
        board = FakeBoardClient(fail=True)
        filing.file_ideas(
            _payload("full schedule retry"), aux_call=None, create=board.create
        )
        sleeps: list[float] = []
        filing.retry_spooled(sleep=sleeps.append, create=board.create)
        assert sleeps == list(filing.RETRY_DELAYS)  # 5 s / 30 s / 2 m


# ── Secrets (spec §6.8) ────────────────────────────────────────────────────


class TestSecrets:
    def test_secret_transcript_files_card_but_never_echos(self, filing):
        board = FakeBoardClient()
        secret = "my api key is sk-abcdefghijklmnop1234 please rotate"
        aux = lambda **kw: _aux_reply(
            [
                {
                    "title": "Rotate exposed api key",
                    "problem": "Key spoken in chat.",
                    "first_action": "Rotate the key.",
                    "effort": "S",
                    "acceptance": "Key rotated.",
                }
            ]
        )

        result = filing.file_ideas(_payload(secret), aux_call=aux, create=board.create)

        assert result.kind == "filed"
        assert result.card_ids  # still filed — it's the user's own board
        assert "sk-abcdefghijklmnop1234" not in result.text
        body = board.calls[0]["body"]
        assert "rotate" in body.lower()
        # the raw secret stays in the card body (verbatim SOURCE is the
        # board's convention) but the confirmation never echoes it
        assert "sk-abcdefghijklmnop1234" in body

    def test_non_secret_transcript_has_no_rotate_note(self, filing):
        board = FakeBoardClient()
        filing.file_ideas(
            _payload("gewoon een idee over kaarttegels cachen"),
            aux_call=None,
            create=board.create,
        )
        assert "rotate" not in board.calls[0]["body"].lower()

    @pytest.mark.parametrize(
        "sample",
        [
            "token=abcdef1234567890abcdef",
            "AWS key AKIAIOSFODNN7EXAMPLE on laptop",
            "glpat-abcdefghijklmnopqrst",
        ],
    )
    def test_secret_patterns_detected(self, filing, sample):
        assert filing._has_secret(sample) is True

    def test_normal_text_not_flagged(self, filing):
        assert filing._has_secret("transit layer met 9292 data erin") is False


# ── Title / hashing helpers ────────────────────────────────────────────────


class TestHelpers:
    def test_word_bound_cut_respects_limit_and_boundary(self, filing):
        text = "een hele lange Nederlandse zin die ver over de grens gaat"
        cut = filing._word_bound_cut(text, 20)
        assert len(cut) <= 20
        assert not cut.endswith((" ", ","))

    def test_norm_title_sorts_tokens_case_insensitively(self, filing):
        assert filing.norm_title("IDEA: B A") == filing.norm_title("a b")

    def test_full_title_joins_cards(self, filing):
        cards = [
            filing.IdeaCard("One", "", "", "", ""),
            filing.IdeaCard("Two", "", "", "", ""),
        ]
        assert filing._full_title(cards) == "IDEA: One + Two"

    def test_idem_key_shape(self, filing):
        key = filing.idem_key_for("IDEA: Something")
        assert key.startswith("idea-") and len(key) == len("idea-") + 16

    def test_build_card_body_defaults_missing_fields(self, filing):
        card = filing.IdeaCard("Title only", "", "", "", "")
        body = filing.build_card_body(
            card, session_id="s1", quote="quote text", card_date="2026-10-17"
        )
        assert "TBD-specify" in body
        assert "S (unreviewed)" in body
        assert "2026-10-17" in body

    def test_to_payload_shape(self, filing):
        r = filing.FilingResult(
            ok=True, kind="filed", text="✅ t_1: x", card_ids=["t_1"]
        )
        p = r.to_payload()
        assert p == {
            "ok": True,
            "kind": "filed",
            "text": "✅ t_1: x",
            "card_ids": ["t_1"],
            "duplicate_of": None,
            "spool_path": None,
            "more_in_source": 0,
        }


# ── Create-path plumbing (bridge → CLI → spool) ────────────────────────────


class TestCreatePaths:
    def test_create_card_prefers_bridge(self, filing, monkeypatch):
        class FakeBridge:
            def __init__(self):
                self.called = False

            def __call__(self, title, body, hash16):
                self.called = True
                return "t_bridge"

        bridge = FakeBridge()
        monkeypatch.setattr(filing, "_create_via_bridge", bridge)
        monkeypatch.setattr(
            filing,
            "_create_via_cli",
            lambda *a: pytest.fail("CLI must not run when bridge works"),
        )
        cid, err = filing.create_card("IDEA: X", "body", "h" * 16)
        assert (cid, err) == ("t_bridge", None)
        assert bridge.called

    def test_create_card_falls_back_to_cli(self, filing, monkeypatch):
        monkeypatch.setattr(filing, "_create_via_bridge", lambda *a: None)
        monkeypatch.setattr(filing, "_create_via_cli", lambda *a: "t_cli")
        cid, err = filing.create_card("IDEA: X", "body", "h" * 16)
        assert (cid, err) == ("t_cli", None)

    def test_create_card_reports_unreachable_when_both_fail(self, filing, monkeypatch):
        monkeypatch.setattr(filing, "_create_via_bridge", lambda *a: None)
        monkeypatch.setattr(filing, "_create_via_cli", lambda *a: None)
        cid, err = filing.create_card("IDEA: X", "body", "h" * 16)
        assert cid is None
        assert err == "board-unreachable"

    def test_cli_parses_first_id_despite_preamble_noise(self, filing, monkeypatch):
        """The CLI prints provider preamble before the JSON — parse the
        FIRST "id": "t_…" or a noisy stream yields the wrong card."""
        noise = 'provider: ready\n{"thought": "maybe id t_wrong"}\n'
        out = noise + '{"id": "t_right", "title": "IDEA: X"}'
        proc = types.SimpleNamespace(returncode=0, stdout=out, stderr="")
        monkeypatch.setattr(filing.subprocess, "run", lambda *a, **kw: proc)
        monkeypatch.setattr(filing, "_hermes_bin", lambda: "hermes")
        assert filing._create_via_cli("IDEA: X", "body", "h" * 16) == "t_right"

    def test_cli_failure_returns_none(self, filing, monkeypatch):
        proc = types.SimpleNamespace(returncode=1, stdout="boom", stderr="err")
        monkeypatch.setattr(filing.subprocess, "run", lambda *a, **kw: proc)
        monkeypatch.setattr(filing, "_hermes_bin", lambda: "hermes")
        assert filing._create_via_cli("IDEA: X", "body", "h" * 16) is None


# ── Ledger locking ─────────────────────────────────────────────────────────


class TestLedgerLock:
    def test_concurrent_appends_all_land(self, filing):
        import threading

        def worker(n):
            for i in range(10):
                filing.ledger_append(
                    {"ts": n * 100 + i, "hash": f"{n:03d}{i:03d}{'0' * 10}"}
                )

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        entries = json.loads(filing.LEDGER_PATH.read_text())["entries"]
        assert len(entries) == 40
        hashes = [e["hash"] for e in entries]
        assert len(set(hashes)) == 40  # no lost updates

    def test_lock_released_after_use(self, filing):
        filing.ledger_append({"ts": 1, "hash": "a" * 16})
        assert not filing.LOCK_PATH.exists()

    def test_ledger_corrupt_reads_as_empty(self, filing):
        filing.LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        filing.LEDGER_PATH.write_text("{not json")
        assert filing._read_ledger() == []
