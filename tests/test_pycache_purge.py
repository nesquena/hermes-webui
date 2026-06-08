"""Test that __pycache__ purge runs before restart."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestPyCachePurge:
    def test_purge_removes_pycache_dirs(self):
        """_purge_agent_pycache should remove __pycache__ directories."""
        from api.updates import _purge_agent_pycache

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Create nested __pycache__ dirs with .pyc files
            cache1 = root / "sub" / "__pycache__"
            cache1.mkdir(parents=True)
            (cache1 / "mod.cpython-311.pyc").write_text("# stale")

            cache2 = root / "__pycache__"
            cache2.mkdir(parents=True)
            (cache2 / "other.cpython-311.pyc").write_text("# stale")

            # Also a non-pycache dir that should survive
            keep = root / "keep"
            keep.mkdir()
            (keep / "data.txt").write_text("keep me")

            _purge_agent_pycache(root)

            assert not cache1.exists(), "nested __pycache__ should be removed"
            assert not cache2.exists(), "root __pycache__ should be removed"
            assert keep.exists(), "non-pycache dirs should survive"
            assert (keep / "data.txt").read_text() == "keep me"

    def test_purge_none_dir(self):
        """_purge_agent_pycache should handle None without error."""
        from api.updates import _purge_agent_pycache

        _purge_agent_pycache(None)  # should not raise

    def test_purge_missing_dir(self):
        """_purge_agent_pycache should handle nonexistent dirs."""
        from api.updates import _purge_agent_pycache

        _purge_agent_pycache(Path("/nonexistent/path/12345"))  # should not raise
