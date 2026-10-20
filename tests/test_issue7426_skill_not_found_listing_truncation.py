"""#7426: the skill-not-found reply must not present a capped list as the whole set.

`_skill_not_found_payload` caps `available_skills` so a large skills tree cannot
bloat an error payload. The bug was that the cap was silent: with 74 skills
installed, a caller asking for one that sorts past the 20th got "not found" plus
a 20-name list that did not contain it and did not say anything was omitted, so
the only reasonable reading was that the skill is not installed.

These tests pin the honesty of the payload, not the size of the cap.
"""

import pathlib

import api.routes as routes


def _stub_listing(monkeypatch, count):
    names = [f"skill-{i:03d}" for i in range(count)]
    monkeypatch.setattr(
        routes,
        "_skills_list_from_dir",
        lambda *args, **kwargs: {
            "success": True,
            "skills": [{"name": n} for n in names],
            "count": len(names),
        },
    )
    return names


def test_truncated_listing_reports_the_total_and_says_it_is_truncated(monkeypatch):
    names = _stub_listing(monkeypatch, 74)
    payload = routes._skill_not_found_payload("skill-073", pathlib.Path("/nonexistent"))

    assert payload["success"] is False
    # The cap still applies: the payload does not grow without bound.
    assert len(payload["available_skills"]) < len(names)
    # But a caller can now tell a partial list from a complete one.
    assert payload["available_skills_truncated"] is True
    assert payload["total_skills"] == 74
    assert "74" in payload["hint"]
    assert str(len(payload["available_skills"])) in payload["hint"]


def test_untruncated_listing_is_not_flagged_as_truncated(monkeypatch):
    names = _stub_listing(monkeypatch, 3)
    payload = routes._skill_not_found_payload("nope", pathlib.Path("/nonexistent"))

    assert payload["available_skills"] == names
    assert payload["available_skills_truncated"] is False
    assert payload["total_skills"] == 3
    assert payload["hint"] == "Use skills_list to see all available skills"


def test_empty_skills_tree_reports_zero_and_no_truncation(monkeypatch):
    _stub_listing(monkeypatch, 0)
    payload = routes._skill_not_found_payload("nope", pathlib.Path("/nonexistent"))

    assert payload["available_skills"] == []
    assert payload["available_skills_truncated"] is False
    assert payload["total_skills"] == 0


def test_exactly_at_the_cap_is_not_flagged_as_truncated(monkeypatch):
    names = _stub_listing(monkeypatch, routes._SKILL_NOT_FOUND_LIST_LIMIT)
    payload = routes._skill_not_found_payload("nope", pathlib.Path("/nonexistent"))

    assert payload["available_skills"] == names
    assert payload["available_skills_truncated"] is False
    assert payload["total_skills"] == len(names)
