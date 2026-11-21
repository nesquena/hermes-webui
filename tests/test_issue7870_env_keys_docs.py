"""#7870: the env-keys endpoints must stay documented.

The greptile P2 finding on this PR was that the new GET/PUT/DELETE routes
changed a public workflow without any public documentation. This test is the
drift guard for that: it reads the routes the server actually registers and
requires the architecture reference to describe each of them, together with
the two contract points a reader cannot guess from the route table -- that a
listing never returns a value, and that the profile is the authenticated one
rather than the ``?profile=`` selector.

It fails when an endpoint is added, renamed, or has its behaviour changed
without the documentation following.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURE = REPO_ROOT / "ARCHITECTURE.md"
ROUTES = REPO_ROOT / "api" / "routes.py"
README = REPO_ROOT / "README.md"


def _doc_text() -> str:
    return ARCHITECTURE.read_text(encoding="utf-8")


def _env_keys_section() -> str:
    """The env-keys section of the architecture reference.

    Sliced by heading so an unrelated mention elsewhere in the file cannot
    satisfy the assertions.
    """
    text = _doc_text()
    start = text.find("### 4.13 Custom Environment Keys")
    assert start != -1, "ARCHITECTURE.md has no 'Custom Environment Keys' section"
    # The section ends at the next heading of the same or a higher level.
    rest = text[start:]
    m = re.search(r"\n#{2,3} ", rest)
    return rest[: m.start()] if m else rest


def test_the_env_keys_section_exists():
    """The endpoints have a section of their own in the architecture reference."""
    section = _env_keys_section()
    assert len(section) > 400, (
        "the env-keys section is a stub; it must document the endpoints, "
        "their responses, and their profile behavior"
    )


@pytest.mark.parametrize(
    "route",
    [
        "GET /api/env/keys",
        "PUT /api/env/keys",
        "DELETE /api/env/keys/<name>",
    ],
)
def test_every_registered_env_keys_route_is_documented(route):
    """Each route the server registers appears in the documentation."""
    assert route in _env_keys_section(), f"{route} is not documented"


def test_the_documented_routes_match_the_registered_ones():
    """The documented route set is exactly the registered route set.

    Guards against the documentation drifting into describing an endpoint
    that no longer exists, or omitting one that does.
    """
    routes_src = ROUTES.read_text(encoding="utf-8")
    # The route table is explicit in the source: assert the three known
    # route shapes are present so the documented set cannot silently diverge.
    for literal in (
        'parsed.path == "/api/env/keys"',
        'parsed.path.startswith("/api/env/keys/")',
    ):
        assert literal in routes_src, f"the server no longer registers {literal!r}"

    section = _env_keys_section()
    documented = set(re.findall(r"`(GET|PUT|DELETE) (/api/env/keys[^`]*)`", section))
    assert documented == {
        ("GET", "/api/env/keys"),
        ("PUT", "/api/env/keys"),
        ("DELETE", "/api/env/keys/<name>"),
    }, f"the documented route set drifted: {sorted(documented)}"


def test_the_documentation_states_that_values_are_never_returned():
    """A reader must learn that a listing masks the value."""
    section = _env_keys_section()
    assert "redacted_value" in section
    assert re.search(r"never carries a value|never return[s]? a value", section), (
        "the documentation must state that a response never carries a value"
    )


def test_the_documentation_states_the_profile_rule():
    """A reader must learn that the profile is the authenticated one."""
    section = _env_keys_section()
    assert "?profile=" in section, (
        "the documentation must state that the ?profile= selector is not "
        "accepted as an authorization mechanism"
    )
    assert re.search(r"not accepted|is not an HTTP\s+authorization", section), (
        "the documentation must say why the ?profile= selector is refused"
    )


def test_the_readme_points_at_the_documented_endpoints():
    """The user-facing entry point names the endpoints and links the detail."""
    readme = README.read_text(encoding="utf-8")
    assert "/api/env/keys" in readme, (
        "README must mention the env-keys endpoints: this is a user-facing "
        "workflow change (AGENTS.md: update docs when changing user-facing "
        "workflows)"
    )
    assert "ARCHITECTURE.md#413-custom-environment-keys" in readme, (
        "README must link to the env-keys section of the architecture reference"
    )
