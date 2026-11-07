"""Guards the temp-root isolation added to ``tests/conftest.py``.

The Hermes runtime exports TMPDIR/TMP/TEMP pointing inside ``~/.hermes``. If a
test process inherits those unchanged, ``tempfile.gettempdir()`` lands in
``~/.hermes/cache/scratch`` and the Agent folds any temp home under the native
home back onto the real ``~/.hermes/profiles/`` — a test-created profile then
appears in the live profile list. ``conftest.py`` now pins all three variables
to a root verifiably outside the production home.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def test_temp_root_is_outside_the_production_hermes_home():
    production = (Path.home() / ".hermes").resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()

    assert temp_root != production, "tempfile.gettempdir() resolved to the production Hermes home"
    assert production not in temp_root.parents, (
        f"tempfile.gettempdir() ({temp_root}) is inside the production Hermes home "
        f"({production}); conftest must repoint TMPDIR/TMP/TEMP"
    )


def test_all_three_temp_env_vars_agree():
    assert os.environ.get("TMPDIR") == os.environ.get("TEMP") == os.environ.get("TMP"), (
        "TMPDIR, TEMP and TMP must all point at the same isolated temp root; "
        "tempfile.gettempdir() skips a missing candidate and falls through to the next"
    )
