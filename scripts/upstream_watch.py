#!/usr/bin/env python3
"""Deterministic upstream-change collector for Hermes/WebUI installs.

Read-only by design: this script may fetch remote refs when requested, but it
never pulls, checks out, resets, or restarts anything. It writes JSON/Markdown
reports that cron jobs and the WebUI can consume without re-querying GitHub.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

DEFAULT_WATCHLIST = (
    "gateway",
    "webui",
    "provider",
    "providers",
    "model",
    "models",
    "memory",
    "mcp",
    "cron",
    "telegram",
    "discord",
    "restart",
    "session",
    "compression",
    "kanban",
    "tools",
    "skills",
)


def utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC)


def isoformat_z(dt: _dt.datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.UTC)
    return dt.astimezone(_dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _run_git(path: Path, args: list[str], timeout: int = 10) -> tuple[str, bool]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return "git executable not found", False
    except subprocess.TimeoutExpired as exc:
        detail = (exc.stderr or exc.stdout or "").strip() if isinstance(exc.stderr, str) else ""
        return detail or f"git {' '.join(args)} timed out after {timeout}s", False
    except OSError as exc:
        return f"git failed to start: {exc}", False

    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if result.returncode == 0:
        return out, True
    return err or out or f"git exited with status {result.returncode}", False


def parse_link_header(header: str | None) -> dict[str, str]:
    links: dict[str, str] = {}
    if not header:
        return links
    for part in header.split(","):
        match = re.search(r'<([^>]+)>\s*;\s*rel="([^"]+)"', part.strip())
        if match:
            links[match.group(2)] = match.group(1)
    return links


def fetch_github_commits(
    owner_repo: str,
    since_iso: str,
    token: str | None = None,
    *,
    max_pages: int = 10,
    timeout: int = 15,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return all GitHub commits since `since_iso`, following pagination."""
    base_url = f"https://api.github.com/repos/{owner_repo}/commits"
    url = base_url + "?" + urllib.parse.urlencode({"since": since_iso, "per_page": "100"})
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "hermes-upstream-watch",
    }
    if token:
        headers["Authorization"] = f"token {token}"

    commits: list[dict[str, Any]] = []
    pages = 0
    rate_limit_remaining: str | None = None
    try:
        while url and pages < max_pages:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed GitHub URL/user-configured repo
                pages += 1
                rate_limit_remaining = resp.headers.get("X-RateLimit-Remaining")
                payload = json.loads(resp.read().decode("utf-8"))
                if isinstance(payload, list):
                    commits.extend(payload)
                else:
                    return [], {
                        "status": "error",
                        "error_class": "unexpected_payload",
                        "message": "GitHub commits endpoint did not return a list",
                        "pages": pages,
                    }
                url = parse_link_header(resp.headers.get("Link")).get("next")
        truncated = bool(url)
        return commits, {
            "status": "ok",
            "pages": pages,
            "truncated": truncated,
            "rate_limit_remaining": rate_limit_remaining,
            "error": None,
        }
    except HTTPError as exc:
        return [], {
            "status": "error",
            "error_class": "http_error",
            "http_status": exc.code,
            "message": exc.reason,
            "pages": pages,
        }
    except URLError as exc:
        return [], {
            "status": "error",
            "error_class": "network_error",
            "message": str(exc.reason),
            "pages": pages,
        }
    except (TimeoutError, json.JSONDecodeError, OSError) as exc:
        return [], {
            "status": "error",
            "error_class": exc.__class__.__name__,
            "message": str(exc),
            "pages": pages,
        }


def collect_local_git_state(path: Path) -> dict[str, Any]:
    """Read-only local git state: branch/upstream/ahead/behind/dirty/diverged."""
    path = Path(path).expanduser()
    state: dict[str, Any] = {
        "path": str(path),
        "is_git": False,
        "branch": None,
        "upstream": None,
        "ahead": 0,
        "behind": 0,
        "dirty": False,
        "diverged": False,
        "error": None,
    }
    inside, ok = _run_git(path, ["rev-parse", "--is-inside-work-tree"])
    if not ok or inside.strip().lower() != "true":
        state["error"] = inside or "not a git worktree"
        return state

    state["is_git"] = True
    branch, ok = _run_git(path, ["rev-parse", "--abbrev-ref", "HEAD"])
    if ok:
        state["branch"] = branch
    upstream, ok = _run_git(path, ["rev-parse", "--abbrev-ref", "@{upstream}"])
    if ok and upstream:
        state["upstream"] = upstream
    else:
        state["error"] = upstream or "no upstream tracking branch"

    status, ok = _run_git(path, ["status", "--porcelain"])
    if ok:
        state["dirty"] = bool(status.strip())

    if state["upstream"]:
        counts, ok = _run_git(path, ["rev-list", "--left-right", "--count", f"HEAD...{state['upstream']}"])
        if ok:
            parts = counts.split()
            if len(parts) == 2 and all(p.isdigit() for p in parts):
                state["ahead"] = int(parts[0])
                state["behind"] = int(parts[1])
                state["diverged"] = state["ahead"] > 0 and state["behind"] > 0
        elif not state["error"]:
            state["error"] = counts
    return state


def maybe_fetch(path: Path, remote: str = "origin", timeout: int = 20) -> dict[str, Any]:
    """Run `git fetch --quiet` with timeout and return structured status."""
    out, ok = _run_git(Path(path).expanduser(), ["fetch", remote, "--quiet"], timeout=timeout)
    return {"ok": ok, "remote": remote, "error": None if ok else out}


def detect_topics(message: str, watchlist: tuple[str, ...] | list[str] = DEFAULT_WATCHLIST) -> list[str]:
    msg = message.lower()
    hits: list[str] = []
    for word in watchlist:
        needle = word.lower()
        if re.search(rf"(?<![a-z0-9_]){re.escape(needle)}(?![a-z0-9_])", msg):
            canonical = "provider" if needle == "providers" else "model" if needle == "models" else needle
            if canonical not in hits:
                hits.append(canonical)
    return hits


def normalize_commit(raw: dict[str, Any], watchlist: tuple[str, ...] | list[str]) -> dict[str, Any]:
    commit = raw.get("commit") or {}
    author_obj = raw.get("author") or {}
    commit_author = commit.get("author") or {}
    message = str(commit.get("message") or "").splitlines()[0]
    return {
        "sha": str(raw.get("sha") or "")[:12],
        "author": author_obj.get("login") or commit_author.get("name") or "unknown",
        "date": commit_author.get("date"),
        "message": message,
        "url": raw.get("html_url"),
        "topics": detect_topics(message, watchlist),
    }


def repo_display_name(owner_repo: str) -> str:
    return owner_repo.rsplit("/", 1)[-1]


def build_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Hermes Upstream Watch",
        "",
        f"Generated: {report.get('generated_at')}",
        f"Status: {report.get('status')}",
        f"Window: {report.get('window_hours')}h",
        "",
    ]
    for repo in report.get("repos", []):
        local = repo.get("local") or {}
        upstream = repo.get("upstream") or {}
        lines.extend(
            [
                f"## {repo.get('github')}",
                "",
                f"Local: branch `{local.get('branch')}` tracking `{local.get('upstream')}`; "
                f"behind={local.get('behind')}, ahead={local.get('ahead')}, "
                f"dirty={local.get('dirty')}, diverged={local.get('diverged')}",
                f"Upstream commits: {upstream.get('commit_count', 0)} ({upstream.get('status')})",
                "",
            ]
        )
        if upstream.get("error"):
            lines.append(f"Error: {upstream.get('error')}\n")
        commits = upstream.get("commits") or []
        if commits:
            for commit in commits[:30]:
                topics = ", ".join(commit.get("topics") or []) or "none"
                sha = commit.get("sha") or "unknown"
                url = commit.get("url")
                label = f"[{sha}]({url})" if url else sha
                lines.append(f"- {label} {commit.get('message')} — {commit.get('author')} ({topics})")
        else:
            lines.append("- No commits in window.")
        lines.append("")
    hits = report.get("watchlist_hits") or []
    lines.extend(["## Watchlist hits", ""])
    if hits:
        for hit in hits[:50]:
            topics = ", ".join(hit.get("topics") or [])
            lines.append(f"- **{hit.get('repo')}** `{hit.get('sha')}` [{topics}]: {hit.get('message')}")
    else:
        lines.append("- None.")
    lines.append("")
    return "\n".join(lines)


def _copy_latest(path: Path, latest: Path) -> None:
    latest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, latest)


def build_report(
    *,
    repo_specs: list[tuple[str, Path]],
    window_hours: int,
    out_dir: Path,
    fetch_remote: bool = True,
    token: str | None = None,
    watchlist: tuple[str, ...] | list[str] = DEFAULT_WATCHLIST,
) -> dict[str, Any]:
    now = utc_now()
    since = now - _dt.timedelta(hours=window_hours)
    since_iso = isoformat_z(since)
    report: dict[str, Any] = {
        "generated_at": isoformat_z(now),
        "status": "ok",
        "window_hours": window_hours,
        "since": since_iso,
        "repos": [],
        "highlights": [],
        "watchlist_hits": [],
    }

    partial = False
    for owner_repo, local_path in repo_specs:
        fetch_state = maybe_fetch(local_path) if fetch_remote else {"ok": None, "skipped": True}
        local_state = collect_local_git_state(local_path)
        commits_raw, meta = fetch_github_commits(owner_repo, since_iso, token=token)
        commits = [normalize_commit(c, watchlist) for c in commits_raw]
        # Local dirty/no-upstream state is useful signal, not collector failure.
        # Mark the whole run partial only when upstream/network/fetch broke or the
        # configured local path is not a git checkout at all.
        if meta.get("status") != "ok" or fetch_state.get("ok") is False or not local_state.get("is_git"):
            partial = True
        repo = {
            "name": repo_display_name(owner_repo),
            "github": owner_repo,
            "local_path": str(Path(local_path).expanduser()),
            "fetch": fetch_state,
            "local": local_state,
            "upstream": {
                "status": meta.get("status"),
                "commit_count": len(commits),
                "commits": commits,
                "rate_limit_remaining": meta.get("rate_limit_remaining"),
                "pages": meta.get("pages"),
                "truncated": meta.get("truncated", False),
                "error": meta if meta.get("status") != "ok" else None,
            },
        }
        report["repos"].append(repo)
        for commit in commits:
            if commit["topics"]:
                report["watchlist_hits"].append(
                    {
                        "repo": owner_repo,
                        "sha": commit["sha"],
                        "message": commit["message"],
                        "topics": commit["topics"],
                        "url": commit.get("url"),
                    }
                )

    if partial:
        report["status"] = "partial"

    out_dir = Path(out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    json_path = out_dir / f"{stamp}.json"
    md_path = out_dir / f"{stamp}.md"
    json_text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    json_path.write_text(json_text, encoding="utf-8")
    md_path.write_text(build_markdown(report), encoding="utf-8")

    latest_dir = out_dir.parent if out_dir.name == "upstream-watch" else out_dir
    latest_json = latest_dir / "upstream-watch-latest.json"
    latest_md = latest_dir / "upstream-watch-latest.md"
    _copy_latest(json_path, latest_json)
    _copy_latest(md_path, latest_md)
    report["report_paths"] = {
        "json": str(json_path),
        "markdown": str(md_path),
        "latest_json": str(latest_json),
        "latest_markdown": str(latest_md),
    }
    # Rewrite JSON after adding paths so latest contains its own location metadata.
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _copy_latest(json_path, latest_json)
    return report


def parse_repo_spec(spec: str) -> tuple[str, Path]:
    if "=" not in spec:
        raise argparse.ArgumentTypeError("repo spec must be OWNER/REPO=/local/path")
    owner_repo, path = spec.split("=", 1)
    if "/" not in owner_repo:
        raise argparse.ArgumentTypeError("repo spec owner must look like OWNER/REPO")
    return owner_repo, Path(path).expanduser()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect upstream Hermes repo changes and local git state")
    parser.add_argument("--repo", action="append", type=parse_repo_spec, required=True, help="OWNER/REPO=/local/path")
    parser.add_argument("--window-hours", type=int, default=24)
    parser.add_argument("--out-dir", type=Path, default=Path.home() / ".hermes" / "workspace" / "reports" / "upstream-watch")
    parser.add_argument("--no-fetch", action="store_true", help="Do not run git fetch before local state collection")
    parser.add_argument("--token-env", default="GITHUB_TOKEN", help="Environment variable for optional GitHub token")
    args = parser.parse_args(argv)

    token = os.environ.get(args.token_env) or None
    try:
        report = build_report(
            repo_specs=args.repo,
            window_hours=args.window_hours,
            out_dir=args.out_dir,
            fetch_remote=not args.no_fetch,
            token=token,
        )
    except Exception as exc:  # pragma: no cover - last-resort CLI guard
        print(f"upstream-watch failed before writing report: {exc}", file=sys.stderr)
        return 1

    paths = report.get("report_paths") or {}
    print(f"status={report['status']} latest={paths.get('latest_json')}")
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
