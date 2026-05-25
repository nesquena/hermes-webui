#!/usr/bin/env python3
"""
archive_orphans.py — 偵測「已找到人選不再需要」的職缺並封存

機制：
  - 每個 jobs/<id>.json 內含 _source_brief 欄位，記錄它是從哪份 brief 解析來的
  - 如果該 brief 已從 jobs_brief/ 消失（HR 拖到「已完成」資料夾或刪除）
    → 該 jobs/<id>.json 視為孤兒，移到 jobs/_archived/<日期>/
  - results/<id>/ 整個目錄也一起移到 results/_archived/<日期>/
  - 之後 run_all.sh 自然就不會再跑該職缺

用法：
  python3 archive_orphans.py              # 預覽（dry-run）哪些會被封存
  python3 archive_orphans.py --apply      # 實際執行封存
  python3 archive_orphans.py --restore <job_id>   # 從封存還原
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
JOBS_DIR = ROOT / 'jobs'
BRIEF_DIR = ROOT / 'jobs_brief'
RESULTS_DIR = ROOT / 'results'
JOBS_ARCHIVE = JOBS_DIR / '_archived'
RESULTS_ARCHIVE = RESULTS_DIR / '_archived'


def find_orphans() -> list[tuple[Path, str]]:
    """回傳 [(json_path, reason), ...] — 沒有對應 brief 的 jobs/<id>.json"""
    orphans = []
    brief_names = (
        {p.name for p in BRIEF_DIR.glob('*.txt')}
        | {p.name for p in BRIEF_DIR.glob('*.md')}
        | {p.name for p in BRIEF_DIR.glob('*.docx')}
        | {p.name for p in BRIEF_DIR.glob('*.doc')}
    )
    for json_path in sorted(JOBS_DIR.glob('*.json')):
        try:
            data = json.loads(json_path.read_text())
        except Exception:
            continue
        source = data.get('_source_brief', '')
        if not source:
            # 沒有 _source_brief 欄位（舊資料，跳過保守處理）
            continue
        if source not in brief_names:
            orphans.append((json_path, f'brief 不存在: {source}'))
    return orphans


def archive_one(json_path: Path, today: str, apply: bool) -> None:
    job_id = json_path.stem
    target_jobs_dir = JOBS_ARCHIVE / today
    target_results_dir = RESULTS_ARCHIVE / today
    results_src = RESULTS_DIR / job_id

    print(f'\n📦 封存 {job_id}')
    print(f'   {json_path} → {target_jobs_dir / json_path.name}')
    if results_src.exists() and results_src.is_dir():
        print(f'   {results_src}/ → {target_results_dir / job_id}/')

    if apply:
        target_jobs_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(json_path), str(target_jobs_dir / json_path.name))
        if results_src.exists():
            target_results_dir.mkdir(parents=True, exist_ok=True)
            dest = target_results_dir / job_id
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(results_src), str(dest))
        print(f'   ✓ 已封存')


def restore_one(job_id: str) -> int:
    """從最新一份封存還原 job_id 回 jobs/ 與 results/"""
    # 找最新的封存目錄含此 job
    candidates = sorted(JOBS_ARCHIVE.glob(f'*/{job_id}.json'), reverse=True)
    if not candidates:
        print(f'❌ 找不到封存的 {job_id}.json（搜尋路徑：{JOBS_ARCHIVE}/*/{job_id}.json）')
        return 1
    src_json = candidates[0]
    dest_json = JOBS_DIR / src_json.name
    print(f'▶ 還原 jobs：{src_json} → {dest_json}')
    if dest_json.exists():
        print(f'⚠️  {dest_json} 已存在，先備份成 .restored.bak')
        shutil.copy2(dest_json, dest_json.with_suffix('.json.restored.bak'))
    shutil.move(str(src_json), str(dest_json))

    # 還原 results
    archive_day = candidates[0].parent.name
    src_results = RESULTS_ARCHIVE / archive_day / job_id
    if src_results.exists():
        dest_results = RESULTS_DIR / job_id
        if dest_results.exists():
            print(f'⚠️  {dest_results} 已存在，跳過 results 還原')
        else:
            print(f'▶ 還原 results：{src_results} → {dest_results}')
            shutil.move(str(src_results), str(dest_results))

    # 提示：對應的 brief 也要 HR 從 Drive「已完成」拖回去
    print(f'\n⚠️  提醒：對應的 brief 檔可能在 Drive「104職缺/已完成/」內，')
    print(f'     若要重新啟用該職缺，請把 brief 拖回 104職缺/ 根目錄')
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='實際執行封存（預設 dry-run）')
    ap.add_argument('--restore', metavar='JOB_ID', help='從封存還原指定職缺')
    args = ap.parse_args()

    if args.restore:
        sys.exit(restore_one(args.restore))

    orphans = find_orphans()
    if not orphans:
        print('✓ 沒有需要封存的孤兒職缺')
        return 0

    print(f'發現 {len(orphans)} 個孤兒職缺：')
    for p, reason in orphans:
        print(f'  • {p.stem} （{reason}）')

    today = datetime.now().strftime('%Y-%m-%d')
    if not args.apply:
        print(f'\n（dry-run 預覽，加 --apply 真的執行）')
        for p, _ in orphans:
            archive_one(p, today, apply=False)
    else:
        for p, _ in orphans:
            archive_one(p, today, apply=True)
        print(f'\n✓ 完成 {len(orphans)} 個職缺封存到 jobs/_archived/{today}/')


if __name__ == '__main__':
    main()
