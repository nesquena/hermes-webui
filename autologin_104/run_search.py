#!/usr/bin/env python3
"""
run_search.py — 多職缺搜尋包裝器

把 jobs/<job_id>.json 的 search 區塊轉成 autologin_104.py 需要的 criteria.md，
跑搜尋後把 candidates_raw.json 搬到 results/<job_id>/。

用法：
  python3 run_search.py --job jidian-tainan
  python3 run_search.py --job gongdi-taichung
  python3 run_search.py --list-jobs
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
JOBS_DIR = ROOT / 'jobs'
RESULTS_DIR = ROOT / 'results'
DEFAULT_CRITERIA_MD = ROOT / 'criteria_jidian_deputy_tainan.md'   # autologin_104.py hard-coded
LEGACY_CANDIDATES = ROOT / 'candidates_raw.json'
AUTOLOGIN = ROOT / 'autologin_104.py'

# autologin_104.py 需要 browser-use venv（含 dotenv、playwright 等）
# 依序嘗試找一個能 import dotenv 的 python
PYTHON_CANDIDATES = [
    '/Users/fongyimac/hermes-webui/browser-use/.venv/bin/python3',
    '/Users/fongyimac/.hermes/hermes-agent/venv/bin/python3',
    os.environ.get('AUTOLOGIN_PYTHON', ''),
    'python3',
]


def find_python() -> str:
    """找一個能 import dotenv 的 python（autologin_104.py 必要相依）"""
    for cand in PYTHON_CANDIDATES:
        if not cand:
            continue
        try:
            r = subprocess.run([cand, '-c', 'import dotenv, browser_use'],
                               capture_output=True, timeout=10)
            if r.returncode == 0:
                return cand
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    sys.exit('❌ 找不到能跑 autologin_104.py 的 python（需要 browser-use venv）。\n'
             '   請設定環境變數 AUTOLOGIN_PYTHON=<python 路徑>')


def render_criteria_md(profile: dict) -> str:
    """把 profile.search 渲染成 autologin_104.py 期望的 markdown 結構。"""
    s = profile['search']
    py_block = {
        'keyword': s.get('keyword', ''),
        'job_categories': s.get('job_categories', []),
        'work_locations': s.get('work_locations', []),
        'home_locations': s.get('home_locations', []),
        'last_action_days': s.get('last_action_days', '7天內'),
        'work_exp_years': s.get('work_exp_years', 3),
        'work_exp_range': s.get('work_exp_range', '以上'),
        'majors': s.get('majors', []),
        'age_min': s.get('age_min'),
        'age_max': s.get('age_max'),
        'tools': s.get('tools', []),
        'certificates': s.get('certificates', []),
    }
    py_repr = 'SEARCH_CRITERIA = ' + json.dumps(py_block, ensure_ascii=False, indent=4).replace('null', 'None')
    return (
        f"# {profile.get('display_name', profile['job_id'])} - 104 查詢人才條件\n\n"
        f"> 自動由 jobs/{profile['job_id']}.json 產生，請勿手動編輯。\n\n"
        f"## Python 端對應參數\n\n"
        f"```python\n{py_repr}\n```\n"
    )


def list_jobs() -> int:
    if not JOBS_DIR.exists():
        print('（jobs/ 目錄尚未建立）')
        return 0
    for p in sorted(JOBS_DIR.glob('*.json')):
        d = json.loads(p.read_text())
        s = d['search']
        print(f"{d['job_id']:<22} {d['display_name']:<22} city={s['work_locations']} age={s['age_min']}-{s['age_max']} exp≥{s['work_exp_years']}年")
    return 0


def kill_orphan_chromes():
    """清掉殘留的 browser-use 自動化 Chrome 行程（避免 temp profile 累積）。"""
    try:
        import subprocess as sp
        out = sp.check_output(['ps', '-axo', 'pid=,command='], text=True)
        pids = []
        for line in out.splitlines():
            if 'browser-use-user-data-dir' in line and 'Helper' not in line:
                pid = line.strip().split()[0]
                if pid.isdigit():
                    pids.append(pid)
        for p in pids:
            try:
                os.kill(int(p), 9)
            except Exception:
                pass
        if pids:
            print(f'  ↳ 清掉 {len(pids)} 個殘留 Chrome 行程')
            time.sleep(2)
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--job', type=str, default=None)
    ap.add_argument('--list-jobs', action='store_true')
    ap.add_argument('--keep-criteria-md', action='store_true',
                    help='保留產生的 criteria_jidian_deputy_tainan.md（預設執行後不還原）')
    ap.add_argument('--max-retries', type=int, default=2,
                    help='搜尋失敗自動重試次數（預設 2）')
    ap.add_argument('--clean-chrome', action='store_true',
                    help='開始前清掉殘留的自動化 Chrome 行程')
    args = ap.parse_args()

    if args.list_jobs:
        sys.exit(list_jobs())
    if not args.job:
        ap.error('需要 --job <id>')

    profile_path = JOBS_DIR / f'{args.job}.json'
    if not profile_path.exists():
        sys.exit(f'❌ 找不到 {profile_path}')
    profile = json.loads(profile_path.read_text())

    job_dir = RESULTS_DIR / args.job
    job_dir.mkdir(parents=True, exist_ok=True)

    # 1) 備份既有 criteria.md，覆寫為當前 job 的條件
    backup = None
    if DEFAULT_CRITERIA_MD.exists() and not args.keep_criteria_md:
        backup = DEFAULT_CRITERIA_MD.with_suffix('.md.bak')
        shutil.copy2(DEFAULT_CRITERIA_MD, backup)
    DEFAULT_CRITERIA_MD.write_text(render_criteria_md(profile))
    print(f'✓ 已將 {profile["display_name"]} 條件寫入 {DEFAULT_CRITERIA_MD.name}')

    if args.clean_chrome:
        print('▶ 清理殘留的自動化 Chrome 行程...')
        kill_orphan_chromes()

    # 2) 跑 autologin_104.py，含自動 retry（冷啟 Profile 4 容易連不上 104）
    py = find_python()
    rc, file_seen, completed = 1, False, False

    def run_one_attempt(attempt_num: int) -> tuple[int, bool, bool]:
        """跑一次 autologin。回傳 (exit_code, file_seen, completed)"""
        print(f'▶ 執行 #{attempt_num}：{py} {AUTOLOGIN.name}')
        if LEGACY_CANDIDATES.exists():
            LEGACY_CANDIDATES.unlink()
        proc = subprocess.Popen(
            [py, str(AUTOLOGIN)], cwd=str(ROOT),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=1, text=True,
        )
        import fcntl
        fd = proc.stdout.fileno()
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        rc_local = 0
        completed_local = False
        file_seen_local = False
        started = time.time()
        last_output = time.time()
        grace_started = 0.0
        idle_limit = 360  # 6 分鐘無 stdout 輸出才視為卡住
        hard_limit = 600  # 總時長 10 分鐘上限
        ws_reconnect_at = 0.0  # CDP WebSocket 重連時間（重連後若 60s 無進展就 kill）

        def stop(why: str, exit_code: int = 0):
            print(f'  ↳ {why}')
            proc.terminate()
            return exit_code

        try:
            while proc.poll() is None:
                try:
                    chunk = proc.stdout.read()
                except Exception:
                    chunk = None
                if chunk:
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                    last_output = time.time()
                    if 'All phases complete' in chunk:
                        completed_local = True
                        grace_started = time.time()
                        print('  ↳ 偵測到「All phases complete」')
                    # 偵測 CDP WebSocket 重連事件（重連後若無進展，提前 kill）
                    if 'WebSocket reconnection attempt' in chunk:
                        ws_reconnect_at = time.time()

                # file_seen 只認「檔案有內容（count > 0 或 candidates 非空）」，避免空檔誤判
                if not file_seen_local and LEGACY_CANDIDATES.exists():
                    try:
                        import json as _json
                        d = _json.loads(LEGACY_CANDIDATES.read_text())
                        cnt = d.get('count', len(d.get('candidates', [])))
                        if cnt > 0:
                            file_seen_local = True
                            grace_started = grace_started or time.time()
                            print(f'  ↳ 偵測到 candidates_raw.json 已落盤（{cnt} 人）')
                    except Exception:
                        pass  # 檔案還在寫入或格式不完整，繼續等

                now = time.time()
                if file_seen_local and (now - grace_started) > 5:
                    rc_local = stop('autologin 完成，主動 terminate')
                    break
                if completed_local and not file_seen_local and (now - grace_started) > 60:
                    rc_local = stop('「complete」後 60s 仍無有效結果，強制 kill', 124)
                    break
                # CDP WebSocket 重連後若 60s 仍無進展，認定恢復失敗
                if ws_reconnect_at > 0 and (now - ws_reconnect_at) > 60 and (now - last_output) > 60:
                    rc_local = stop('CDP WebSocket 重連後 60s 無進展，視為失敗', 124)
                    break
                if (now - last_output) > idle_limit:
                    rc_local = stop(f'閒置 {idle_limit}s 沒輸出，視為卡住', 124)
                    break
                if (now - started) > hard_limit:
                    rc_local = stop(f'總時長超過 {hard_limit}s，強制 kill', 124)
                    break

                time.sleep(0.5)
        except KeyboardInterrupt:
            proc.terminate()
            raise
        finally:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        return rc_local, file_seen_local, completed_local

    def is_legit_result() -> bool:
        """檢查 candidates_raw.json 是否合法（非 chrome-error、有 searchResult URL）"""
        if not LEGACY_CANDIDATES.exists():
            return False
        try:
            d = json.loads(LEGACY_CANDIDATES.read_text())
        except Exception:
            return False
        url = d.get('url', '')
        if 'chrome-error' in url or 'about:blank' in url:
            return False
        if 'searchResult' not in url:
            return False
        return True

    for attempt in range(1, args.max_retries + 2):  # 至少跑 1 次，加 max_retries 次重試
        rc, file_seen, completed = run_one_attempt(attempt)
        if file_seen and is_legit_result():
            break  # 真成功
        if attempt <= args.max_retries:
            print(f'  ⚠️  本次未拿到合法結果，{3} 秒後重試（{attempt}/{args.max_retries}）...')
            kill_orphan_chromes()  # 清殘留再試
            time.sleep(3)
        else:
            print(f'  ✗ 重試 {args.max_retries} 次後仍失敗')

    print(f'  exit={rc}, completed={completed}, file_seen={file_seen}')

    # 3) 搬 candidates_raw.json → results/<job>/（先驗證內容是否有效）
    if LEGACY_CANDIDATES.exists():
        try:
            data = json.loads(LEGACY_CANDIDATES.read_text())
            url = data.get('url', '')
            count = data.get('count', 0)
            if 'chrome-error' in url or 'about:blank' in url:
                print(f'⚠️  autologin 產出無效（Chrome 載入失敗 url={url!r}），不覆寫既有結果')
                LEGACY_CANDIDATES.unlink()
                file_seen = False  # 視為失敗
            elif count == 0 and 'searchResult' not in url:
                print(f'⚠️  autologin 產出空 candidates 且非搜尋結果頁（url={url[:60]!r}），不覆寫')
                LEGACY_CANDIDATES.unlink()
                file_seen = False
            else:
                dest = job_dir / 'candidates_raw.json'
                shutil.move(str(LEGACY_CANDIDATES), str(dest))
                print(f'✓ 搬移 candidates_raw.json → {dest}（{count} 人）')
        except Exception as e:
            print(f'⚠️  candidates_raw.json 解析失敗: {e}')
            file_seen = False
    else:
        print('⚠️  autologin_104 沒產出 candidates_raw.json')

    # 4) 還原 criteria.md
    if backup is not None:
        shutil.move(str(backup), str(DEFAULT_CRITERIA_MD))
        print(f'✓ 還原 {DEFAULT_CRITERIA_MD.name}')

    # 若搜尋有產出檔案（file_seen），即使 autologin 退出碼非 0 也視為成功
    final_rc = 0 if file_seen else rc
    sys.exit(final_rc)


if __name__ == '__main__':
    main()
