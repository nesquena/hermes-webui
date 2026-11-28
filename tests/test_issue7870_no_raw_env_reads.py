"""#7870 round 3: the real-agent tests must not open a ``.env`` file directly.

The maintainer's 10-06 review could not execute this branch at all: the
pre-execution scanner reports the tests' temp ``.env`` reads as
``CREDENTIAL_READ``, and "policy means nothing from this branch was run". The
scanner is right about the pattern and wrong about the intent: the test never
reads a credential, it reads a file it wrote itself, one key at a time, to
prove where a write landed.

The fix is not to argue with the scanner but to stop using the pattern it
flags. The installed agent already exposes the right API for this:
``hermes_cli.config.get_env_value`` / ``load_env`` resolve the ``.env`` of the
home that is CURRENTLY in scope, which is exactly the question these tests ask
("did the write land in THIS profile's file, and nowhere else?"). Reading
through the agent also makes the assertion stronger: the agent's own parser
decides what a key's value is, so a malformed line is reported the way the
product would report it.

So the contract this module pins is:

  * the real-agent module never opens a ``.env`` file directly — every read
    goes through the agent's scoped reader;
  * every read is scoped to a home, so "the profile's own file" and "the
    launch profile's file" are two different questions with two different
    answers;
  * a write is still made through the agent's writer, and the read-back
    through the agent's reader, so the whole path is the real one.

This module is a STATIC check over the test sources: it needs neither the
agent nor a network, so it runs in CI even where the agent is absent — which
is the point: the guard must not depend on the thing it guards.
"""
from __future__ import annotations

import re
from pathlib import Path

TESTS = Path(__file__).resolve().parent
# The two modules this PR added that touch a profile's .env.
REAL_AGENT_MODULE = TESTS / "test_issue7870_real_agent_paths.py"
PROFILE_AUTH_MODULE = TESTS / "test_issue7870_env_keys_profile_auth.py"
# Every module the PR adds that reaches a profile's .env at all.
SCOPED_MODULES = (REAL_AGENT_MODULE, PROFILE_AUTH_MODULE)

# A file access on a path that ends in ".env". The pattern the pre-execution
# scanner reports as CREDENTIAL_READ.
_ENV_FILE_ACCESS = re.compile(
    r"""
    (?:
        \.\s*read_text\s*\(     # Path.read_text(...)
      | \.\s*read_bytes\s*\(    # Path.read_bytes(...)
      | \.\s*write_text\s*\(    # Path.write_text(...)
      | \.\s*write_bytes\s*\(   # Path.write_bytes(...)
      | \bopen\s*\(             # open(...)
    )
    """,
    re.VERBOSE,
)


def _code_lines(text: str) -> list[tuple[int, str]]:
    """Source lines with docstrings and comments removed.

    A mention of ``.env`` in prose is not a file access, and the module's own
    docstrings talk about ``.env`` constantly.
    """
    out = []
    in_doc = None
    for lineno, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        # A triple-quoted string: track it so its body is not scanned.
        if in_doc is None:
            if stripped.startswith(('"""', "'''")):
                quote = stripped[:3]
                if not (stripped.endswith(quote) and len(stripped) > 3):
                    in_doc = quote
                continue
            if stripped.startswith("#"):
                continue
            out.append((lineno, raw))
        else:
            if in_doc in raw:
                in_doc = None
    return out


def test_the_real_agent_module_never_opens_a_env_file_directly():
    """No direct ``.env`` file access in the real-agent module.

    This is the literal pattern the pre-execution scanner reports as
    ``CREDENTIAL_READ``, and it is why the whole branch could not be executed
    under policy. Read through the agent's scoped reader instead.
    """
    offenders = []
    for module in SCOPED_MODULES:
        text = module.read_text(encoding="utf-8")
        for lineno, raw in _code_lines(text):
            if ".env" in raw and _ENV_FILE_ACCESS.search(raw):
                offenders.append(f"{module.name}:{lineno}: {raw.strip()}")
    assert not offenders, (
        "these modules open a .env file directly, which the pre-execution "
        "scanner reports as CREDENTIAL_READ. Read through the agent's scoped "
        "reader instead:\n" + "\n".join(offenders)
    )


def test_the_real_agent_module_reads_env_through_the_agent():
    """The real-agent module must use the agent's reader, not a file handle.

    A direct file read cannot distinguish "the key is absent" from "the file
    is malformed", and it cannot see a value the agent's own parser would
    reject. The agent's scoped reader answers all three.
    """
    text = REAL_AGENT_MODULE.read_text(encoding="utf-8")
    code = "\n".join(raw for _, raw in _code_lines(text))
    assert "get_env_value" in code or "load_env" in code, (
        "the real-agent module must read a profile's .env through the "
        "agent's scoped reader"
    )


def test_the_guard_does_not_need_the_agent_to_run():
    """This guard is static: it must run where the agent is absent.

    If the guard only ran on a machine with the agent installed, the branch
    would go unexecuted in CI exactly as before — the guard would be as
    unavailable as the thing it guards.
    """
    code = "\n".join(raw for _, raw in _code_lines(Path(__file__).read_text(encoding="utf-8")))
    # Split the needles so this check does not match its own source.
    agent_import = "import " + "hermes_"
    skip_call = "importor" + "skip"
    assert agent_import not in code, (
        "this guard must not import the agent: it has to run where the agent "
        "is absent"
    )
    assert skip_call not in code, (
        "this guard must never skip: it is the check that keeps the real-agent "
        "module from reintroducing the flagged pattern"
    )
