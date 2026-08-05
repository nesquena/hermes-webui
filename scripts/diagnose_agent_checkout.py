#!/usr/bin/env python3
"""Report Hermes agent/webui checkout sync state for update troubleshooting.

Non-mutating: does not fetch, stash, reset, or modify the working tree.
Prints branch, commit, remote, ahead/behind/diverged, dirty files, and
processes referencing the checkout.

Can run with system Python (no WebUI deps required). When --via-api is set,
uses hermes-webui's diagnose_checkout() for the same fields the HTTP API
returns.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_git(args: list[str], cwd: Path) -> tuple[str, bool]:
    try:
        proc = subprocess.run(
            ['git', *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=15,
            encoding='utf-8',
            errors='replace',
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc), False
    out = ((proc.stdout or '') + (proc.stderr or '')).strip() if proc.returncode else (proc.stdout or '').strip()
    if proc.returncode == 0:
        return (proc.stdout or '').strip(), True
    return out or f'git exited {proc.returncode}', False


def _default_agent_dir() -> Path:
    env = os.environ.get('HERMES_WEBUI_AGENT_DIR') or os.environ.get('HERMES_AGENT_DIR')
    if env:
        return Path(env).expanduser()
    home = Path(os.environ.get('HERMES_HOME', Path.home() / '.hermes')).expanduser()
    return home / 'hermes-agent'


def _list_processes(path: Path, *, limit: int = 25) -> list[dict]:
    needle = str(path)
    try:
        proc = subprocess.run(
            ['ps', '-ax', '-o', 'pid=,command='],
            capture_output=True,
            text=True,
            timeout=5,
            encoding='utf-8',
            errors='replace',
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    hits: list[dict] = []
    for line in (proc.stdout or '').splitlines():
        text = line.strip()
        if not text or needle not in text:
            continue
        parts = text.split(None, 1)
        try:
            pid = int(parts[0])
        except (ValueError, IndexError):
            continue
        hits.append({'pid': pid, 'command': parts[1] if len(parts) > 1 else ''})
        if len(hits) >= limit:
            break
    return hits


def _standalone_diagnose(path: Path) -> dict:
    if not (path / '.git').exists():
        return {'ok': False, 'path': str(path), 'message': 'Not a git repository'}

    branch, _ = _run_git(['rev-parse', '--abbrev-ref', 'HEAD'], path)
    head, head_ok = _run_git(['rev-parse', 'HEAD'], path)
    remote, _ = _run_git(['remote', 'get-url', 'origin'], path)
    upstream, up_ok = _run_git(['rev-parse', '--abbrev-ref', '@{upstream}'], path)
    if not up_ok:
        # Prefer origin/main then origin/master for agent/webui defaults.
        for candidate in ('origin/main', 'origin/master'):
            _, ok = _run_git(['rev-parse', '--verify', candidate], path)
            if ok:
                upstream = candidate
                up_ok = True
                break
    compare_ref = upstream if up_ok else None
    compare_sha = None
    ahead = behind = None
    relationship = 'unknown'
    if compare_ref and head_ok:
        compare_sha, tip_ok = _run_git(['rev-parse', compare_ref], path)
        if tip_ok and compare_sha == head:
            relationship = 'identical'
            ahead = behind = 0
        elif tip_ok:
            ahead_out, ahead_ok = _run_git(['rev-list', '--count', f'{compare_sha}..{head}'], path)
            behind_out, behind_ok = _run_git(['rev-list', '--count', f'{head}..{compare_sha}'], path)
            ahead = int(ahead_out) if ahead_ok and ahead_out.isdigit() else None
            behind = int(behind_out) if behind_ok and behind_out.isdigit() else None
            _, can_ff = _run_git(['merge-base', '--is-ancestor', 'HEAD', compare_ref], path)
            _, head_has = _run_git(['merge-base', '--is-ancestor', compare_ref, 'HEAD'], path)
            if can_ff and not head_has:
                relationship = 'behind'
            elif head_has and not can_ff:
                relationship = 'ahead'
            else:
                relationship = 'diverged'

    status, status_ok = _run_git(['status', '--porcelain'], path)
    modified: list[str] = []
    untracked: list[str] = []
    if status_ok:
        for line in status.splitlines():
            if len(line) < 4:
                continue
            code, name = line[:2], line[3:]
            if code == '??':
                untracked.append(name)
            else:
                modified.append(name)

    return {
        'ok': True,
        'path': str(path),
        'branch': branch or None,
        'head_sha': head if head_ok else None,
        'remote_url': remote or None,
        'compare_ref': compare_ref,
        'compare_sha': compare_sha,
        'relationship': relationship,
        'ahead': ahead,
        'behind': behind,
        'dirty': bool(status_ok and status),
        'dirty_tracked': bool(modified),
        'modified_files': modified[:100],
        'untracked_files': untracked[:100],
        'modified_count': len(modified),
        'untracked_count': len(untracked),
        'processes': _list_processes(path),
    }


def _print_report(report: dict) -> None:
    if not report.get('ok'):
        print(f"ERROR: {report.get('message') or 'diagnose failed'}")
        if report.get('path'):
            print(f"path: {report['path']}")
        return
    print(f"target:      {report.get('target', '-')}")
    print(f"path:        {report.get('path')}")
    print(f"branch:      {report.get('branch')}")
    print(f"HEAD:        {report.get('head_sha')}")
    print(f"remote:      {report.get('remote_url')}")
    print(f"compare_ref: {report.get('compare_ref')}")
    if report.get('compare_sha'):
        print(f"compare_sha: {report.get('compare_sha')}")
    print(f"relationship:{report.get('relationship')}")
    print(f"ahead:       {report.get('ahead')}")
    print(f"behind:      {report.get('behind')}")
    print(f"dirty:       {report.get('dirty')} (tracked={report.get('dirty_tracked')})")
    print(f"modified:    {report.get('modified_count')} file(s)")
    for name in report.get('modified_files') or []:
        print(f"  M {name}")
    print(f"untracked:   {report.get('untracked_count')} file(s)")
    for name in report.get('untracked_files') or []:
        print(f"  ? {name}")
    procs = report.get('processes') or []
    print(f"processes:   {len(procs)}")
    for proc in procs:
        cmd = (proc.get('command') or '')[:160]
        print(f"  pid {proc.get('pid')}: {cmd}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=('agent', 'webui'), default='agent')
    parser.add_argument('--json', action='store_true')
    parser.add_argument(
        '--via-api',
        action='store_true',
        help='Use api.updates.diagnose_checkout (requires WebUI Python deps)',
    )
    parser.add_argument(
        '--path',
        help='Override checkout path (defaults: agent ~/.hermes/hermes-agent, webui repo root)',
    )
    args = parser.parse_args(argv)

    if args.via_api:
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        from api.updates import diagnose_checkout

        report = diagnose_checkout(args.target)
    else:
        if args.path:
            path = Path(args.path).expanduser()
        elif args.target == 'webui':
            path = REPO_ROOT
        else:
            path = _default_agent_dir()
        report = _standalone_diagnose(path)
        report['target'] = args.target

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_report(report)
    return 0 if report.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
