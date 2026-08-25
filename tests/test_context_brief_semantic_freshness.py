"""Semantic freshness contracts for the Context Brief narrative input."""

import re
from types import SimpleNamespace
from unittest.mock import patch

from api import context_brief


_INDEX_RE = re.compile(r"^--- \[(\d+)\] ", re.MULTILINE)


def _session(messages):
    return SimpleNamespace(messages=messages, parent_session_id=None)


def _rendered_indices(distilled: str) -> list[int]:
    return [int(value) for value in _INDEX_RE.findall(distilled)]


def test_recent_conclusion_is_reserved_before_old_history():
    messages = [
        {
            "role": "user",
            "content": "DEMANDE FONDATRICE: corriger durablement le brief Contexte",
        }
    ]
    for index in range(90):
        messages.append(
            {
                "role": "user",
                "content": f"demande historique {index}: " + ("ancien contexte " * 140),
            }
        )
        if index % 7 == 0:
            messages.append(
                {
                    "role": "assistant",
                    "content": (
                        "# CONCLUSION\n---\n"
                        f"> ancienne conclusion {index}: aucun correctif intégré ni déployé\n"
                        + ("preuve historique " * 230)
                    ),
                }
            )
    messages.extend(
        [
            {
                "role": "user",
                "content": (
                    "[CONTEXT COMPACTION — REFERENCE ONLY]\n"
                    "État historique: aucun correctif intégré ni déployé."
                ),
            },
            {
                "role": "user",
                "content": "DEMANDE RECENTE: finalise et vérifie le correctif",
            },
            {
                "role": "assistant",
                "content": (
                    "# CONCLUSION\n---\n"
                    "> CORRECTIF FINAL: intégré et déployé; aucun travail restant."
                ),
            },
        ]
    )

    distilled = context_brief._distill_context_brief(_session(messages), budget=100_000)
    rendered_indices = _rendered_indices(distilled)

    assert "DEMANDE FONDATRICE" in distilled
    assert "DEMANDE RECENTE" in distilled
    assert "CORRECTIF FINAL" in distilled
    assert "[CONTEXT COMPACTION" not in distilled
    assert len(distilled) <= 100_000
    assert rendered_indices == sorted(rendered_indices)
    assert rendered_indices[-1] == len(messages) - 1


def test_synthetic_history_is_not_narrative_evidence():
    messages = [
        {"role": "user", "content": "vraie demande récente"},
        {
            "role": "assistant",
            "content": (
                "[CONTEXT COMPACTION — REFERENCE ONLY]\n"
                "ancienne synthèse assistant: état encore non déployé"
            ),
        },
        {
            "role": "assistant",
            "content": "synthèse squash: aucun correctif livré",
            "_squash_summary": True,
        },
        {
            "role": "user",
            "content": "[PRIOR CONTEXT — REFERENCE ONLY]\nancien handoff utilisateur",
        },
        {
            "role": "assistant",
            "content": "# CONCLUSION\n---\n> état direct récent vérifié",
        },
    ]

    distilled = context_brief._distill_context_brief(_session(messages))

    assert "vraie demande récente" in distilled
    assert "état direct récent vérifié" in distilled
    assert "ancienne synthèse assistant" not in distilled
    assert "synthèse squash" not in distilled
    assert "ancien handoff utilisateur" not in distilled


def test_prompt_marks_stale_todos_non_actionable():
    captured = {}

    def fake_call_llm(**kwargs):
        captured.update(kwargs)
        return object()

    deterministic = {
        "meta": {
            "title": "chantier fraîcheur",
            "message_count": 42,
            "transcript_revision": "revision-fraiche",
        },
        "todos": {
            "stale": True,
            "counts": {
                "pending": 2,
                "in_progress": 1,
                "completed": 4,
                "cancelled": 0,
            },
            "current": "ANCIENNE TACHE A NE PAS REPRENDRE",
            "items": [
                {"content": "ANCIENNE TACHE A NE PAS REPRENDRE", "status": "in_progress"}
            ],
        },
        "in_flight": {
            "active": False,
            "details": {"pending_user_message": "payload privé"},
            "background_tasks": [],
        },
    }
    session = _session(
        [
            {"role": "user", "content": "demande directe"},
            {
                "role": "assistant",
                "content": "# CONCLUSION\n---\n> livraison directe vérifiée",
            },
        ]
    )

    with patch("agent.auxiliary_client.call_llm", side_effect=fake_call_llm), patch.object(
        context_brief,
        "_extract_llm_content",
        return_value="x" * 300,
    ):
        _text, source = context_brief._generate_llm_brief(
            session,
            "session-fraiche",
            deterministic,
        )

    system_prompt = captured["messages"][0]["content"]
    user_prompt = captured["messages"][1]["content"]
    assert source == "auxiliary-llm"
    assert captured["task"] == "compression"
    assert "model" not in captured
    assert "reasoning_config" not in captured
    assert '"transcript_revision": "revision-fraiche"' in user_prompt
    assert '"stale": true' in user_prompt
    assert '"active": false' in user_prompt
    assert "ANCIENNE TACHE A NE PAS REPRENDRE" not in user_prompt
    assert "payload privé" not in user_prompt
    assert "checklist" in system_prompt.lower()
    assert "obsolète" in system_prompt.lower()
    assert "reste à faire" in system_prompt.lower()


def test_newer_conclusion_has_explicit_temporal_precedence():
    messages = [
        {
            "role": "assistant",
            "content": "# CONCLUSION\n---\n> aucun correctif déployé",
        },
        {
            "role": "tool",
            "content": '{"todos":[{"content":"déployer le correctif","status":"pending"}]}',
        },
        {"role": "user", "content": "finalise le correctif"},
        {
            "role": "assistant",
            "content": (
                "# CONCLUSION\n---\n"
                "> correctif déployé, aucun travail restant"
            ),
        },
    ]

    distilled = context_brief._distill_context_brief(_session(messages))
    rendered_indices = _rendered_indices(distilled)
    state = context_brief._structured_brief_state(
        {
            "meta": {"transcript_revision": "revision-finale"},
            "todos": {
                "stale": True,
                "counts": {"pending": 1},
                "current": "déployer le correctif",
            },
            "in_flight": {"active": False},
        }
    )

    assert "aucun correctif déployé" in distilled
    assert "correctif déployé, aucun travail restant" in distilled
    assert rendered_indices == sorted(rendered_indices)
    assert rendered_indices.index(0) < rendered_indices.index(3)
    assert "index le plus élevé prévaut" in context_brief._BRIEF_SYSTEM
    assert state["todos"]["stale"] is True
    assert state["todos"]["current"] is None


def test_request_evidence_is_capped_to_founder_plus_recent_seven():
    messages = [
        {"role": "user", "content": "DEMANDE FONDATRICE"},
        *[
            {"role": "user", "content": f"demande directe {index}"}
            for index in range(20)
        ],
    ]

    distilled = context_brief._distill_context_brief(_session(messages))
    user_rows = re.findall(r"^--- \[\d+\] user ---$", distilled, re.MULTILINE)

    assert len(user_rows) == 8
    assert "DEMANDE FONDATRICE" in distilled
    assert "demande directe 19" in distilled
    assert "demande directe 12" not in distilled
