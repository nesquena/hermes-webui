"""Local knowledge base notes search (fallback when Joplin is disabled)."""

import json
import pathlib
import subprocess
from urllib.parse import parse_qs


def handle_notes_local_search(handler, parsed):
    """Search ~/workspace/knowledge/*.md files using ripgrep."""
    qs = parse_qs(parsed.query)
    query = (qs.get("q") or [""])[0].strip()
    if not query:
        return _json(handler, {"results": []})
    kb_dir = pathlib.Path.home() / "workspace" / "knowledge"
    if not kb_dir.is_dir():
        return _json(handler, {"results": []})
    try:
        # `-e` + `--` keep crafted queries like `-v` from being parsed as flags.
        result = subprocess.run(
            ["rg", "--no-heading", "--line-number", "-i", "-e", query, "--", str(kb_dir)],
            capture_output=True, text=True, timeout=10
        )
        lines = [l for l in result.stdout.split("\n") if l.strip()]
        results = []
        seen = set()
        for line in lines[:50]:
            parts = line.split(":", 2)
            if len(parts) >= 2:
                fpath = parts[0]
                if fpath in seen and len(results) > 20:
                    continue
                seen.add(fpath)
                title = pathlib.Path(fpath).stem
                snippet = parts[2].strip() if len(parts) >= 3 else ""
                if len(snippet) > 200:
                    snippet = snippet[:200] + "..."
                results.append({"id": fpath, "title": title, "snippet": snippet, "source": "local"})
        return _json(handler, {"results": results})
    except Exception:
        return _json(handler, {"results": []})


def handle_notes_local_read(handler, parsed):
    """Read a single knowledge file."""
    qs = parse_qs(parsed.query)
    file_path = (qs.get("path") or [""])[0].strip()
    if not file_path:
        return _bad(handler, "path is required")
    p = pathlib.Path(file_path).expanduser().resolve()
    kb_dir = (pathlib.Path.home() / "workspace" / "knowledge").resolve()
    try:
        p.relative_to(kb_dir)
    except ValueError:
        return _bad(handler, "path must be under knowledge directory")
    if not p.is_file():
        return _bad(handler, "file not found")
    content = p.read_text("utf-8", errors="replace")
    if len(content) > 50000:
        content = content[:50000] + "\n\n[Preview truncated at 50,000 characters]"
    return _json(handler, {"note": {"id": str(p), "title": p.stem, "content": content, "source": "local"}})


def _json(handler, data):
    # Same-origin only — never advertise Access-Control-Allow-Origin: *.
    # Passwordless binds already expose the API to loopback; wildcard CORS would
    # let any visited site read knowledge files via the user's browser.
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.end_headers()
    handler.wfile.write(json.dumps(data).encode())
    return True


def _bad(handler, msg, status=400):
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.end_headers()
    handler.wfile.write(json.dumps({"error": msg}).encode())
    return True
