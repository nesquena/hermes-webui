"""The isolation guard must preserve imports used by shared DB fixtures."""
import json
import subprocess
import sys

import pytest

from tests import conftest


def test_agent_schema_import_survives_path_restore(tmp_path, monkeypatch):
    if conftest.HERMES_AGENT is None:
        pytest.skip("No Agent checkout discovered")

    # Runtime initialization adds the discovered checkout after conftest takes
    # its snapshot. The first test's teardown used to remove that valid path,
    # so gateway fixtures silently seeded a reduced schema on the next test.
    monkeypatch.syspath_prepend(str(conftest.HERMES_AGENT))
    guard = conftest._restore_hermes_cli_module.__wrapped__()
    next(guard)
    with pytest.raises(StopIteration):
        next(guard)

    # A fresh interpreter avoids a cached hermes_state masking a broken path.
    # Exercise the real schema owner, not a mocked title writer.
    result = subprocess.run(
        [sys.executable, "-c", "\n".join([
            "import sys, json",
            "sys.path[:] = json.loads(sys.argv[1])",
            "from hermes_state import SessionDB",
            "from pathlib import Path",
            "db = SessionDB(db_path=Path(sys.argv[2]))",
            "db.ensure_session(session_id='path-restore', source='webui')",
            "db.set_session_title('path-restore', 'Canonical title')",
            "assert db.get_session_title('path-restore') == 'Canonical title'",
            "db.close()",
        ]), json.dumps(sys.path), str(tmp_path / "state.db")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
