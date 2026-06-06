#!/usr/bin/env python3
"""
auto_forward.py — 多職缺通用 人選自動篩選與轉寄

流程：
  1. 連線到既有 Chrome（CDP debug port 自動偵測）
  2. 載入 jobs/<job_id>.json 設定檔
  3. 讀取 results/<job_id>/candidates_raw.json
  4. 逐筆開啟履歷詳細頁、抽取完整內容（含自傳）
  5. 套用該職缺的評分規則
  6. 對匹配度 >= threshold 的人選：點轉寄 → 填說明 → 發送
  7. 輸出 analysis_<date>.md 與 forward_log.json 到 results/<job_id>/

用法：
  python3 auto_forward.py --job jidian-tainan
  python3 auto_forward.py --job jidian-kaohsiung --dry-run
  python3 auto_forward.py --job gongdi-tainan --threshold 75
  python3 auto_forward.py --job gongdi-taichung --force <resumeId>
  python3 auto_forward.py --list-jobs
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

try:
    import websockets
except ImportError:
    print('⚠️  缺少 websockets 套件，自動安裝中...', flush=True)
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--user', '--quiet', 'websockets'])
    import websockets  # noqa: E402

# 本地 LLM 評分（可選；llm_score.py 失敗或未啟用時自動 fallback）
try:
    import llm_score
    LLM_AVAILABLE = True
except ImportError:
    LLM_AVAILABLE = False

# ----- 路徑 -----
ROOT = Path(__file__).resolve().parent
JOBS_DIR = ROOT / 'jobs'
RESULTS_DIR = ROOT / 'results'


def load_job_profile(job_id: str) -> dict:
    """讀取 jobs/<job_id>.json"""
    path = JOBS_DIR / f'{job_id}.json'
    if not path.exists():
        available = sorted(p.stem for p in JOBS_DIR.glob('*.json'))
        raise SystemExit(f'❌ 找不到職缺檔 {path}\n可用職缺：{available}')
    return json.loads(path.read_text())


def list_jobs() -> int:
    """列出所有可用 jobs"""
    if not JOBS_DIR.exists():
        print('（jobs/ 目錄尚未建立）')
        return 0
    profiles = sorted(JOBS_DIR.glob('*.json'))
    if not profiles:
        print('（無職缺設定檔）')
        return 0
    print(f'{"job_id":<25} {"display_name":<25} {"threshold":<10} {"residence":<10}')
    print('-' * 75)
    for p in profiles:
        d = json.loads(p.read_text())
        s = d.get('scoring', {})
        print(f"{d['job_id']:<25} {d['display_name']:<25} {str(s.get('threshold','-')):<10} {s.get('residence_bonus_keyword','-'):<10}")
    return 0


# ----- CDP 工具 -----
def detect_cdp_port() -> int | None:
    """從 Chrome 行程命令列抓 --remote-debugging-port，挑 104 page 分頁最多者"""
    try:
        out = subprocess.check_output(['ps', 'auxw'], text=True)
    except Exception:
        return None
    candidates = []
    seen = set()
    for line in out.splitlines():
        if 'Google Chrome' not in line or 'Helper' in line:
            continue
        m = re.search(r'--remote-debugging-port=(\d+)', line)
        if not m:
            continue
        port = int(m.group(1))
        if port in seen:
            continue
        seen.add(port)
        try:
            tabs = json.loads(urllib.request.urlopen(f'http://localhost:{port}/json', timeout=2).read())
            count = sum(1 for t in tabs if t.get('type') == 'page' and '104.com.tw' in t.get('url', ''))
            if count > 0:
                candidates.append((count, port))
        except Exception:
            continue
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def list_tabs(port: int):
    return json.loads(urllib.request.urlopen(f'http://localhost:{port}/json', timeout=3).read())


async def cdp_eval(ws_url: str, expression: str):
    async with websockets.connect(ws_url, max_size=20 * 1024 * 1024) as ws:
        await ws.send(json.dumps({
            'id': 1, 'method': 'Runtime.evaluate',
            'params': {'expression': expression, 'returnByValue': True, 'awaitPromise': True}
        }))
        resp = json.loads(await ws.recv())
        return resp.get('result', {}).get('result', {}).get('value')


async def cdp_click_at(ws_url: str, x: float, y: float):
    async with websockets.connect(ws_url, max_size=20 * 1024 * 1024) as ws:
        for i, evt in enumerate(['mousePressed', 'mouseReleased']):
            await ws.send(json.dumps({
                'id': i + 1, 'method': 'Input.dispatchMouseEvent',
                'params': {'type': evt, 'x': x, 'y': y, 'button': 'left', 'clickCount': 1}
            }))
            await ws.recv()


async def navigate(ws_url: str, url: str):
    async with websockets.connect(ws_url, max_size=20 * 1024 * 1024) as ws:
        await ws.send(json.dumps({'id': 1, 'method': 'Page.navigate', 'params': {'url': url}}))
        await ws.recv()


_dialog_listener_ws = None  # 持久 websocket，用於攔截原生 JS dialog


async def start_dialog_listener(ws_url: str):
    """啟動持久 websocket 連線，自動接受原生 JS alert/confirm（如「網路連線發生錯誤」）。"""
    global _dialog_listener_ws
    try:
        ws = await websockets.connect(ws_url, max_size=20 * 1024 * 1024)
        # 啟用 Page domain
        await ws.send(json.dumps({'id': 9001, 'method': 'Page.enable', 'params': {}}))
        await ws.recv()
        _dialog_listener_ws = ws

        async def _listen():
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get('method') == 'Page.javascriptDialogOpening':
                        dialog_msg = msg.get('params', {}).get('message', '')
                        dialog_type = msg.get('params', {}).get('type', 'alert')
                        print(f"\n   ⚡ 偵測到原生 {dialog_type} 對話框：{dialog_msg[:60]}")
                        await ws.send(json.dumps({
                            'id': 9002, 'method': 'Page.handleJavaScriptDialog',
                            'params': {'accept': True}
                        }))
                        await ws.recv()
                        print(f"   ⚡ 已自動點擊「確定」關閉對話框")
            except websockets.ConnectionClosed:
                pass
            except Exception as e:
                print(f"   ⚠️ dialog listener 錯誤：{e}")

        asyncio.create_task(_listen())
        print('✓ 已啟用原生 JS dialog 自動接受')
    except Exception as e:
        print(f'⚠️ 啟動 dialog listener 失敗：{e}')


async def stop_dialog_listener():
    """關閉持久 dialog listener websocket。"""
    global _dialog_listener_ws
    if _dialog_listener_ws:
        try:
            await _dialog_listener_ws.close()
        except Exception:
            pass
        _dialog_listener_ws = None


async def dismiss_network_error(ws_url: str) -> bool:
    """偵測並關閉 104「網路連線發生錯誤」彈窗，回傳是否有關閉。"""
    result = await cdp_eval(ws_url, """
    (function() {
        var pageText = document.body.textContent || '';
        if (!pageText.includes('網路連線') && !pageText.includes('連線發生錯誤'))
            return false;
        // SweetAlert2
        var btn = document.querySelector('.swal2-confirm');
        if (btn) { btn.click(); return 'swal2'; }
        // modal / dialog
        var modals = document.querySelectorAll('.modal, .dialog, [role="dialog"], .swal2-popup');
        for (var m of modals) {
            if (m.textContent.includes('網路連線') || m.textContent.includes('連線發生錯誤')) {
                var buttons = m.querySelectorAll('button, .btn');
                for (var b of buttons) {
                    if (b.textContent.trim() === '確定' || b.textContent.trim() === 'OK') {
                        b.click(); return 'modal';
                    }
                }
            }
        }
        // fallback
        var allBtns = document.querySelectorAll('button');
        for (var b of allBtns) {
            if (b.textContent.trim() === '確定' || b.textContent.trim() === '確認') {
                b.click(); return 'fallback';
            }
        }
        return false;
    })();
    """)
    if result:
        print(f"   ⚡ 已自動關閉網路錯誤彈窗 ({result})")
        await asyncio.sleep(2)
        return True
    return False


# ----- 評分 -----
def parse_months(text: str) -> int:
    y = re.search(r'(\d+)年', text)
    m = re.search(r'(\d+)個月', text)
    return (int(y.group(1)) * 12 if y else 0) + (int(m.group(1)) if m else 0)


def score(candidate: dict, full_text: str, profile: dict) -> dict:
    """套用 profile.scoring 規則，回傳評分。"""
    sc = profile['scoring']
    construction_kw = sc['construction_kw']
    title_kw = sc['title_kw']
    residential_kw = sc.get('residential_kw', [])
    public_kw = sc.get('public_kw', [])
    autobio_positive = sc.get('autobio_positive', [])
    long_tenure_months = sc.get('long_tenure_months', 36)
    require_two_long = sc.get('require_two_long_tenures', False)
    senior_rule = sc['level_rules']['senior']
    junior_rule = sc['level_rules']['junior']
    residence_kw = sc.get('residence_bonus_keyword', '')

    reasons = []
    s = 0
    construction_n = 0
    title_n = 0
    long_tenure_count = 0
    construction_months = 0

    for exp in candidate.get('experiences', []):
        if any(k in exp for k in construction_kw):
            construction_n += 1
        if any(k in exp for k in title_kw):
            title_n += 1
        mo = parse_months(exp)
        if mo >= long_tenure_months:
            long_tenure_count += 1
        if any(k in exp for k in construction_kw + title_kw):
            construction_months += mo

    text = full_text or ''
    residential_n = sum(text.count(k) for k in residential_kw)
    public_n = sum(text.count(k) for k in public_kw)

    # 計分
    if construction_n >= 2:
        s += 22; reasons.append(f'營造/建設/建築師事務所經驗 ×{construction_n} (+22)')
    elif construction_n == 1:
        s += 14; reasons.append('營造/建設/建築師事務所經驗 ×1 (+14)')

    if title_n >= 4:
        s += 22; reasons.append(f'相關職稱 ×{title_n} (+22)')
    elif title_n >= 2:
        s += 14; reasons.append(f'相關職稱 ×{title_n} (+14)')
    elif title_n == 1:
        s += 6; reasons.append('相關職稱 ×1 (+6)')

    if require_two_long:
        if long_tenure_count >= 2:
            s += 16; reasons.append(f'2份任職 ≥ {long_tenure_months // 12} 年 (+16)')
        elif long_tenure_count == 1:
            s += 8; reasons.append(f'1份任職 ≥ {long_tenure_months // 12} 年 (+8)')
    else:
        if long_tenure_count >= 1:
            s += 12; reasons.append(f'單一任職 ≥ {long_tenure_months // 12} 年 (+12)')

    # 等級判定
    construction_years = construction_months // 12
    if construction_years >= senior_rule['min_years'] or construction_n >= senior_rule['min_projects']:
        level = senior_rule['name']; s += 25
        reasons.append(f'{level}資格（建築工程 {construction_years} 年）(+25)')
    elif construction_years >= junior_rule['min_years'] or construction_n >= junior_rule['min_projects']:
        level = junior_rule['name']; s += 16
        reasons.append(f'{level}資格（建築工程 {construction_years} 年）(+16)')
    else:
        level = '不符等級'

    if residential_n > 0:
        bonus = min(8, residential_n * 2)
        s += bonus; reasons.append(f'集合住宅關鍵字 ×{residential_n} (+{bonus})')
    if public_n > 0:
        bonus = min(6, public_n)
        s += bonus; reasons.append(f'公共工程關鍵字 ×{public_n} (+{bonus})')
    if '自傳' in text and autobio_positive:
        hits = sum(1 for k in autobio_positive if k in text)
        if hits >= 3:
            s += 8; reasons.append(f'自傳積極正向（{hits} 項關鍵字）(+8)')
        elif hits >= 1:
            s += 4; reasons.append(f'有自傳，積極關鍵字 ×{hits} (+4)')

    if residence_kw and residence_kw in candidate.get('residence', ''):
        s += 7; reasons.append(f'居住{residence_kw} (+7)')

    return {
        'score': min(s, 100),
        'level': level,
        'reasons': reasons,
        'metrics': {
            'construction_n': construction_n, 'title_n': title_n,
            'long_tenure_count': long_tenure_count,
            'construction_years': construction_years,
            'residential_n': residential_n, 'public_n': public_n,
        },
    }


def _html_escape(s: str) -> str:
    """簡易 HTML 跳脫，避免 XSS 與排版被破壞。"""
    if s is None:
        return ''
    return (str(s)
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;')
            .replace("'", '&#39;'))


def _render_html_report(job_id: str, display_name: str, profile: dict,
                        threshold: int, scored: list, log: dict,
                        resume_cache: dict) -> str:
    """產生 HR 友善的 HTML 報告（含列印樣式）。

    Args:
        scored: 已按分數排序的候選人 list（每項含 _result, _from_cache 等）
        log: 本次 run 的 log，含 results[] 標記 forwarded/skipped_dup
    """
    today = datetime.now().strftime('%Y-%m-%d')
    try:
        job_title = get_job_title_from_brief(profile)
    except ValueError:
        job_title = profile.get('job_id', '未知職缺')
    threshold_priority = 80  # 與 build_summary 一致
    threshold_consider = 70

    # 統計
    total = len(scored)
    forwarded_count = sum(1 for x in log['results'] if x['forwarded'])
    skipped_count = sum(1 for x in log['results'] if x['skipped_dup'])
    threshold_count = sum(1 for c in scored if c['_result']['score'] >= threshold)
    priority_count = sum(1 for c in scored if c['_result']['score'] >= threshold_priority)

    # 建構候選人卡片 HTML
    cards_html = []
    for rank, c in enumerate(scored, 1):
        r = c['_result']
        score = r['score']
        level = r.get('level', '')
        rid = c['resumeId']
        rec = next((x for x in log['results'] if x['resumeId'] == rid), {})

        # 狀態 badge
        if rec.get('forwarded'):
            status_badge = '<span class="badge badge-forwarded">✓ 本次轉寄</span>'
        elif rec.get('skipped_dup'):
            status_badge = '<span class="badge badge-skipped">⏭ 已轉寄過</span>'
        elif score >= threshold:
            status_badge = '<span class="badge badge-threshold">○ 達門檻</span>'
        else:
            status_badge = '<span class="badge badge-below">—</span>'

        # 推薦等級
        if score >= threshold_priority:
            recommend = '🔥 建議優先邀約面試'
            card_class = 'card card-priority'
        elif score >= threshold_consider:
            recommend = '👍 可考慮邀約面試'
            card_class = 'card card-consider'
        elif score >= threshold:
            recommend = '— 達門檻供參考'
            card_class = 'card card-threshold'
        else:
            recommend = '— 供參考'
            card_class = 'card card-below'

        # LLM jobs[] 詳析
        llm = resume_cache.get(rid, {}).get('llm_score', {}) or {}
        jobs_data = llm.get('jobs', []) or []
        highlights = llm.get('highlights', []) or []
        reasoning = llm.get('reasoning', '') or ''

        # 工作經歷表格
        jobs_html = ''
        if jobs_data:
            jobs_rows = []
            for j in jobs_data:
                rel = j.get('relevance', '')
                rel_class = {'高': 'rel-high', '中': 'rel-mid', '低': 'rel-low'}.get(rel, '')
                jobs_rows.append(f"""
                <tr>
                  <td>{_html_escape(j.get('company', ''))}</td>
                  <td>{_html_escape(j.get('title', ''))}</td>
                  <td>{_html_escape(j.get('duration', ''))}</td>
                  <td class="num">{_html_escape(j.get('score', ''))}</td>
                  <td><span class="{rel_class}">{_html_escape(rel)}</span></td>
                  <td>{_html_escape(j.get('summary', ''))}</td>
                </tr>""")
            jobs_html = f"""
            <h4>工作經歷逐段分析（LLM）</h4>
            <table class="jobs-table">
              <thead>
                <tr><th>公司</th><th>職稱</th><th>時長</th><th>分數</th><th>相關度</th><th>說明</th></tr>
              </thead>
              <tbody>{''.join(jobs_rows)}</tbody>
            </table>"""

        # 規則評分依據
        reasons_html = ''
        if r.get('reasons'):
            items = ''.join(f'<li>{_html_escape(rr)}</li>' for rr in r['reasons'])
            reasons_html = f'<h4>規則評分依據</h4><ul class="reasons">{items}</ul>'

        # 亮點
        highlights_html = ''
        if highlights:
            items = ''.join(f'<li>{_html_escape(h)}</li>' for h in highlights)
            highlights_html = f'<h4>LLM 亮點摘要</h4><ul class="highlights">{items}</ul>'

        reasoning_html = ''
        if reasoning:
            reasoning_html = f'<p class="reasoning"><strong>LLM 總評：</strong>{_html_escape(reasoning)}</p>'

        # 候選人基本資料
        rule_s = r.get('rule_score', score)
        llm_s = r.get('llm_score', '—')
        metrics = r.get('metrics', {}) or {}

        cards_html.append(f"""
        <article id="cand-{rid}" class="{card_class}">
          <header class="card-header">
            <div class="card-title">
              <span class="rank">#{rank}</span>
              <h2>{_html_escape(c.get('name', ''))}</h2>
              <span class="level">{_html_escape(level)}</span>
              {status_badge}
            </div>
            <div class="score-block">
              <div class="score">{score}<small>%</small></div>
              <div class="score-breakdown">規則 {rule_s} ｜ LLM {llm_s}</div>
            </div>
          </header>
          <p class="recommend">{recommend}</p>
          <dl class="meta">
            <dt>履歷編號</dt><dd>{_html_escape(rid)}</dd>
            <dt>年齡/性別</dt><dd>{_html_escape(c.get('age',''))} {_html_escape(c.get('gender',''))}</dd>
            <dt>居住地</dt><dd>{_html_escape(c.get('residence',''))}</dd>
            <dt>學歷</dt><dd>{_html_escape(c.get('education',''))}</dd>
            <dt>希望職稱</dt><dd>{_html_escape(c.get('preferJobTitle','').replace('希望職稱 :','').strip())}</dd>
            <dt>建築年資</dt><dd>{metrics.get('construction_years','—')} 年</dd>
            <dt>3 年以上任職</dt><dd>{metrics.get('long_tenure_count','—')} 份</dd>
          </dl>
          {reasoning_html}
          {highlights_html}
          {jobs_html}
          {reasons_html}
        </article>""")

    cards = '\n'.join(cards_html)

    # 排序表格
    rank_rows = []
    for rank, c in enumerate(scored, 1):
        r = c['_result']
        rec = next((x for x in log['results'] if x['resumeId'] == c['resumeId']), {})
        if rec.get('forwarded'):
            status = '✓ 本次轉寄'
        elif rec.get('skipped_dup'):
            status = '⏭ 已轉寄過'
        elif r['score'] >= threshold:
            status = '○ 達門檻'
        else:
            status = '—'
        score_class = 'score-high' if r['score'] >= 80 else ('score-mid' if r['score'] >= 70 else '')
        rank_rows.append(f"""
        <tr>
          <td class="num">{rank}</td>
          <td><a href="#cand-{c['resumeId']}">{_html_escape(c.get('name',''))}</a></td>
          <td class="num {score_class}"><strong>{r['score']}%</strong></td>
          <td>{_html_escape(r.get('level',''))}</td>
          <td>{_html_escape(c.get('age',''))}</td>
          <td>{_html_escape(c.get('residence',''))}</td>
          <td>{status}</td>
        </tr>""")
    rank_table = ''.join(rank_rows)

    # 完整 HTML
    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<title>{_html_escape(job_title)} - 招募分析報告 {today}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, "PingFang TC", "Microsoft JhengHei", "Noto Sans TC", sans-serif;
    margin: 0; padding: 30px; background: #f5f5f5; color: #222;
    line-height: 1.6;
  }}
  .container {{ max-width: 1100px; margin: 0 auto; }}
  header.report-header {{
    background: linear-gradient(135deg, #2c5aa0, #4a7bbf);
    color: white; padding: 30px; border-radius: 12px 12px 0 0;
    margin-bottom: 0;
  }}
  header.report-header h1 {{ margin: 0 0 8px 0; font-size: 28px; }}
  header.report-header .subtitle {{ opacity: 0.9; font-size: 14px; }}
  .stats {{
    background: white; padding: 20px 30px; border-radius: 0 0 12px 12px;
    margin-bottom: 24px; box-shadow: 0 2px 8px rgba(0,0,0,0.05);
    display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 16px;
  }}
  .stat {{ text-align: center; padding: 8px; }}
  .stat .num {{ font-size: 28px; font-weight: bold; color: #2c5aa0; display: block; }}
  .stat .label {{ font-size: 12px; color: #666; }}

  h2.section {{
    margin-top: 36px; padding-bottom: 8px;
    border-bottom: 2px solid #2c5aa0; color: #2c5aa0; font-size: 20px;
  }}

  table.rank {{
    width: 100%; border-collapse: collapse; background: white;
    box-shadow: 0 1px 4px rgba(0,0,0,0.06); border-radius: 6px; overflow: hidden;
  }}
  table.rank th, table.rank td {{
    padding: 10px 12px; text-align: left; border-bottom: 1px solid #eee;
  }}
  table.rank th {{ background: #f0f4fa; color: #2c5aa0; font-weight: 600; }}
  table.rank a {{ color: #2c5aa0; text-decoration: none; }}
  table.rank a:hover {{ text-decoration: underline; }}
  .num {{ text-align: center; }}
  .score-high {{ color: #c0392b; }}
  .score-mid {{ color: #d68910; }}

  .card {{
    background: white; padding: 24px; margin: 16px 0;
    border-radius: 10px; box-shadow: 0 2px 8px rgba(0,0,0,0.06);
    border-left: 6px solid #ddd;
    page-break-inside: avoid;
  }}
  .card-priority {{ border-left-color: #c0392b; }}
  .card-consider {{ border-left-color: #d68910; }}
  .card-threshold {{ border-left-color: #27ae60; }}
  .card-below {{ border-left-color: #95a5a6; opacity: 0.85; }}

  .card-header {{
    display: flex; justify-content: space-between; align-items: flex-start;
    gap: 20px; margin-bottom: 8px;
  }}
  .card-title {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
  .rank {{ background: #2c5aa0; color: white; padding: 4px 10px;
           border-radius: 12px; font-size: 13px; font-weight: bold; }}
  .card-title h2 {{ margin: 0; font-size: 22px; }}
  .level {{ background: #ecf0f1; padding: 4px 10px; border-radius: 12px;
            font-size: 12px; color: #555; }}
  .badge {{ padding: 4px 10px; border-radius: 12px; font-size: 12px; font-weight: bold; }}
  .badge-forwarded {{ background: #27ae60; color: white; }}
  .badge-skipped {{ background: #95a5a6; color: white; }}
  .badge-threshold {{ background: #f39c12; color: white; }}
  .badge-below {{ background: #ecf0f1; color: #666; }}

  .score-block {{ text-align: right; min-width: 100px; }}
  .score {{ font-size: 36px; font-weight: bold; color: #2c5aa0; line-height: 1; }}
  .score small {{ font-size: 18px; font-weight: normal; opacity: 0.7; }}
  .score-breakdown {{ font-size: 11px; color: #888; margin-top: 4px; }}

  .recommend {{
    background: #f8f9fa; padding: 10px 14px; border-radius: 6px;
    margin: 12px 0; font-weight: 500;
  }}
  .card-priority .recommend {{ background: #fdf2f0; color: #c0392b; }}
  .card-consider .recommend {{ background: #fef5e7; color: #d68910; }}

  dl.meta {{
    display: grid; grid-template-columns: auto 1fr auto 1fr; gap: 4px 12px;
    margin: 12px 0; padding: 12px; background: #fafbfc; border-radius: 6px;
    font-size: 13px;
  }}
  dl.meta dt {{ color: #888; font-weight: normal; }}
  dl.meta dd {{ margin: 0; color: #333; }}

  .reasoning {{ background: #f0f7ff; padding: 10px 14px; border-radius: 6px;
                margin: 12px 0; font-size: 13px; color: #1a4d7d; }}
  h4 {{ color: #2c5aa0; margin: 14px 0 8px 0; font-size: 14px; }}
  ul.reasons, ul.highlights {{ margin: 6px 0; padding-left: 22px; font-size: 13px; }}
  ul.reasons li, ul.highlights li {{ margin: 3px 0; }}

  table.jobs-table {{
    width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 6px;
  }}
  table.jobs-table th, table.jobs-table td {{
    padding: 6px 8px; border-bottom: 1px solid #eee; text-align: left;
  }}
  table.jobs-table th {{ background: #f8f9fa; color: #555; }}
  table.jobs-table .num {{ text-align: center; font-weight: bold; }}
  .rel-high {{ color: #c0392b; font-weight: bold; }}
  .rel-mid {{ color: #d68910; }}
  .rel-low {{ color: #888; }}

  footer {{ text-align: center; color: #888; font-size: 11px; margin-top: 40px; padding: 20px; }}

  @media print {{
    body {{ background: white; padding: 0; }}
    .container {{ max-width: 100%; }}
    header.report-header {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
    .card {{ page-break-inside: avoid; border-radius: 0; box-shadow: none;
             border: 1px solid #ddd; border-left-width: 6px; }}
    h2.section {{ page-break-before: auto; }}
    .stat .num {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
    table.rank, table.jobs-table {{ font-size: 11px; }}
  }}
</style>
</head>
<body>
<div class="container">

  <header class="report-header">
    <h1>📋 {_html_escape(job_title)} — 招募分析報告</h1>
    <div class="subtitle">
      生成日期：{today}　｜　職缺代碼：{_html_escape(job_id)}　｜　轉寄門檻：{threshold}%
    </div>
  </header>

  <div class="stats">
    <div class="stat"><span class="num">{total}</span><span class="label">候選人總數</span></div>
    <div class="stat"><span class="num">{threshold_count}</span><span class="label">達門檻 ≥{threshold}%</span></div>
    <div class="stat"><span class="num">{priority_count}</span><span class="label">優先邀約 ≥80%</span></div>
    <div class="stat"><span class="num">{forwarded_count}</span><span class="label">本次轉寄</span></div>
    <div class="stat"><span class="num">{skipped_count}</span><span class="label">已轉寄過</span></div>
  </div>

  <h2 class="section">📊 排名總覽</h2>
  <table class="rank">
    <thead>
      <tr><th>#</th><th>姓名</th><th>匹配度</th><th>等級</th><th>年齡</th><th>居住地</th><th>狀態</th></tr>
    </thead>
    <tbody>{rank_table}</tbody>
  </table>

  <h2 class="section">📝 候選人詳情</h2>
  {cards}

  <footer>
    由 104 自動化招募 pipeline 產生 ｜ {today}
  </footer>

</div>
</body>
</html>"""


def get_job_title_from_brief(profile: dict) -> str:
    """嚴格從 _source_brief 取 HR 原始的職缺名稱（Word/txt 檔名，去掉日期和副檔名）。
    若 _source_brief 缺失則 raise — 不可 fallback 到 display_name（避免用 LLM 改寫過的名稱）。"""
    source = profile.get('_source_brief', '')
    if not source:
        raise ValueError(
            f'profile.{profile.get("job_id","?")} 沒有 _source_brief 欄位，無法取得職缺名稱。'
            f'請重新跑 parse_job_brief.py 解析 jobs_brief/ 的檔案。'
        )
    name = re.sub(r'\.(docx|doc|txt|md)$', '', source)
    name = re.sub(r'_\d{8}$', '', name)
    if not name:
        raise ValueError(f'_source_brief "{source}" 去掉日期/副檔名後為空，無法取得職缺名稱')
    return name


def build_summary(name: str, result: dict, body: str, profile: dict) -> str:
    """從完整履歷文字組裝摘要（給「說明」欄），長度依 profile.forward.summary_max_chars"""
    max_chars = profile.get('forward', {}).get('summary_max_chars', 980)
    try:
        display_name = get_job_title_from_brief(profile)
    except ValueError:
        display_name = profile.get('job_id', '未知職缺')
    residence_kw = profile.get('scoring', {}).get('residence_bonus_keyword', '')

    bio = ''
    if '自傳' in body:
        idx = body.index('自傳')
        bio = body[idx + 2: idx + 220].strip().replace('\n', ' ')

    cases = []
    for line in body.split('\n'):
        line = line.strip()
        if line.startswith('案名') or line.startswith('案名:'):
            cases.append(line.replace('案名:', '').replace('案名：', '').strip())
        if len(cases) >= 8:
            break

    head = (
        f"【職缺：{display_name}】\n"
        f"【{name}｜匹配度 {result['score']}%｜{result['level']}】\n"
        f"年資：{result['metrics']['construction_years']}年建築工程相關｜"
        f"營造/建設×{result['metrics']['construction_n']}｜"
        f"相關職稱×{result['metrics']['title_n']}｜"
        f"3年以上任職×{result['metrics']['long_tenure_count']}\n\n"
    )
    if cases:
        head += '■代表建案：\n' + '\n'.join(f'{i+1}.{c[:60]}' for i, c in enumerate(cases)) + '\n\n'
    if bio:
        head += '■自傳節錄：\n' + bio[:200] + '...\n\n'
    head += '■評分依據：\n' + '；'.join(result['reasons'][:6])
    # 只有匹配度 ≥80% 才標「建議優先邀約面試」；70-79% 為「可考慮邀約」；<70% 為「供參考」
    score = result.get('score', 0)
    if score >= 80:
        if residence_kw:
            head += f'\n\n建議優先邀約面試，確認集合住宅意願與駐點{residence_kw}條件。'
        else:
            head += '\n\n建議優先邀約面試。'
    elif score >= 70:
        head += '\n\n可考慮邀約面試。'
    else:
        head += '\n\n供參考。'
    return head[:max_chars]


# ----- 操作流程 -----
async def fetch_resume_text(ws_url: str, detail_url: str) -> str:
    await navigate(ws_url, detail_url)
    # poll until ready
    for _ in range(20):
        await asyncio.sleep(1.5)
        await dismiss_network_error(ws_url)
        body = await cdp_eval(ws_url, "document.readyState === 'complete' ? document.body.innerText : ''")
        if body and len(body) > 3000 and ('自傳' in body or '工作經驗' in body):
            return body
    return await cdp_eval(ws_url, "document.body.innerText") or ''


DEFAULT_FORWARD_EMAILS = ['i00788@fong-yi.com.tw', 'fongchien19@gmail.com', '990409@fong-yi.com.tw']


async def select_contact_by_search(ws_url: str, email: str) -> bool:
    """在「選擇聯絡人」modal 的搜尋框輸入 email → 等過濾結果 → 勾選 checkbox。
    用 placeholder 'mail' / '聯絡人' 來定位搜尋框，不依賴 .modal-dialog class。"""
    email_json = json.dumps(email)

    # Step 1: 找全頁中含 'mail'/'聯絡人'/'姓名' 的 input（即「選擇聯絡人」modal 的搜尋框）
    typed = await cdp_eval(ws_url, f"""
    (function(email) {{
        // 全頁搜尋符合的 input（modal 可能用任何 class）
        const inputs = Array.from(document.querySelectorAll('input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"])'));
        let input = inputs.find(i => i.offsetParent !== null && /mail|聯絡人|姓名/i.test(i.placeholder || ''));
        if (!input) {{
            // 找含「選擇聯絡人」標題的容器
            const titleEl = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,div,span'))
                .find(e => e.offsetParent !== null && /選擇聯絡人/.test((e.textContent||'').trim()) && (e.textContent||'').length < 30);
            if (titleEl) {{
                // 從標題往上找 modal container
                let container = titleEl;
                for (let i = 0; i < 8 && container; i++) {{
                    container = container.parentElement;
                    if (!container) break;
                    const inp = container.querySelector('input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"])');
                    if (inp && inp.offsetParent !== null) {{
                        input = inp;
                        break;
                    }}
                }}
            }}
        }}
        if (!input) return {{ ok: false, reason: 'no search input found' }};

        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
        input.focus();
        setter.call(input, '');
        input.dispatchEvent(new Event('input', {{ bubbles: true }}));
        setter.call(input, email);
        input.dispatchEvent(new Event('input', {{ bubbles: true }}));
        input.dispatchEvent(new Event('change', {{ bubbles: true }}));
        input.dispatchEvent(new KeyboardEvent('keydown', {{ bubbles: true, key: email.slice(-1) }}));
        input.dispatchEvent(new KeyboardEvent('keyup', {{ bubbles: true, key: email.slice(-1) }}));
        // 記住此 input 給後續用
        window.__contactSearchInput = input;
        return {{ ok: true, placeholder: input.placeholder || '' }};
    }})({email_json})
    """)
    if not typed or not typed.get('ok'):
        print(f'    ↳ debug: 搜尋框找不到 (typed={typed})')
        return False

    # Step 2: 等過濾結果出現（最多 4 秒）
    # 偵測策略：找一個葉節點 text 等於 email（過濾後 list 的 email cell）
    matched = False
    for _ in range(8):
        await asyncio.sleep(0.5)
        found = await cdp_eval(ws_url, f"""
        (function(email) {{
            // 找文字節點剛好等於 email 的可見元素（list 中的 email 欄位）
            const all = document.querySelectorAll('span, div, td, p, a');
            for (const el of all) {{
                if (el.offsetParent === null) continue;
                if (el.children.length > 0) continue;  // 只看葉節點
                const t = (el.textContent || '').trim();
                if (t === email) {{
                    return true;
                }}
            }}
            return false;
        }})({email_json})
        """)
        if found:
            matched = True
            break

    if not matched:
        print(f'    ↳ debug: 等待過濾結果逾時，頁面找不到「{email}」的 list 項目')
        return False

    # Step 3: 找含此 email 的「列表 row」（不是「已選取」chip），回傳該列座標
    coords_str = await cdp_eval(ws_url, f"""
    (function(email) {{
        // 找所有文字節點等於 email 的可見葉節點（可能含列表 row + 已選取 chip）
        const all = document.querySelectorAll('span, div, td, p, a');
        const candidates = [];
        for (const el of all) {{
            if (el.offsetParent === null) continue;
            if (el.children.length > 0) continue;
            if ((el.textContent || '').trim() === email) {{
                candidates.push(el);
            }}
        }}
        if (candidates.length === 0) {{
            return JSON.stringify({{ ok: false, reason: 'no email cell' }});
        }}

        // 從每個 candidate 往上找 checkbox 祖先（chip 區沒有 checkbox，會被排除）
        for (const cell of candidates) {{
            let row = cell;
            let cbEl = null;
            for (let i = 0; i < 10; i++) {{
                row = row.parentElement;
                if (!row) break;
                cbEl = row.querySelector('input[type="checkbox"]')
                    || row.querySelector('aot-checkbox')
                    || row.querySelector('[role="checkbox"]')
                    || row.querySelector('.checkbox')
                    || row.querySelector('.el-checkbox');
                if (cbEl) break;
            }}
            if (!cbEl) continue;  // 此 candidate 是 chip，跳過

            // 進一步檢查：row 容器不應該整段含「已選取」標題（避免把整個已選取區當 row）
            const rowText = (row.textContent || '').trim();
            if (rowText.length > 200) continue;  // row 應該短，整段就是 row 容器太大了

            const rowRect = row.getBoundingClientRect();
            const cbRect = cbEl.getBoundingClientRect();
            // 如果 checkbox 太小（被視覺隱藏），點 row 最左端
            if (cbRect.width < 8 || cbRect.height < 8) {{
                return JSON.stringify({{
                    ok: true, x: rowRect.left + 16, y: rowRect.top + rowRect.height/2,
                    note: 'click-row-left', rowText: rowText.slice(0, 80),
                }});
            }}
            return JSON.stringify({{
                ok: true, x: cbRect.left + cbRect.width/2, y: cbRect.top + cbRect.height/2,
                note: 'click-checkbox', rowText: rowText.slice(0, 80),
            }});
        }}
        return JSON.stringify({{ ok: false, reason: 'all candidates are chips (no checkbox ancestor)', count: candidates.length }});
    }})({email_json})
    """)
    if not coords_str:
        print(f'    ↳ debug: 取得座標失敗')
        return False
    try:
        coords = json.loads(coords_str)
    except Exception:
        return False
    if not coords.get('ok'):
        print(f'    ↳ debug: {coords}')
        return False

    # Step 4: 用 CDP Input.dispatchMouseEvent 點擊（穿透 web component shadow DOM）
    await cdp_click_at(ws_url, coords['x'], coords['y'])
    await asyncio.sleep(0.6)

    # Step 5: 驗證已選取（檢查「已選取」區塊是否含此 email chip）
    verified = await cdp_eval(ws_url, f"""
    (function(email) {{
        // 找「已選取」區塊
        const all = document.querySelectorAll('div, span');
        for (const el of all) {{
            if (el.offsetParent === null) continue;
            if (el.children.length > 0) continue;
            if ((el.textContent || '').trim() === email) {{
                // 從這個 cell 往上看是否在「已選取」區塊下
                let p = el.parentElement;
                for (let i = 0; i < 12 && p; i++) {{
                    if (/已選取/.test(p.textContent || '')) {{
                        return true;
                    }}
                    p = p.parentElement;
                }}
            }}
        }}
        return false;
    }})({email_json})
    """)
    if not verified:
        print(f'    ↳ debug: 已點擊但驗證「已選取」chip 不含 {email}（點擊位置: {coords.get("note","?")}, row: {coords.get("rowText","?")}）')
    return bool(verified)


async def submit_contact_picker(ws_url: str) -> bool:
    """點「選擇聯絡人」modal 底部的「送出」按鈕關閉 picker。"""
    result = await cdp_eval(ws_url, """
    (function() {
        // 找含「選擇聯絡人」標題的容器內的「送出」按鈕
        const titleEl = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,div,span'))
            .find(e => e.offsetParent !== null && /選擇聯絡人/.test((e.textContent||'').trim()) && (e.textContent||'').length < 30);
        let container = titleEl;
        for (let i = 0; i < 10 && container; i++) {
            container = container.parentElement;
            if (!container) break;
            // 在 container 內找「送出」按鈕
            const btns = container.querySelectorAll('button, aot-button, .btn');
            for (const b of btns) {
                const t = (b.innerText || b.textContent || b.getAttribute('label') || '').trim();
                if (t === '送出' && b.offsetParent !== null) {
                    const r = b.getBoundingClientRect();
                    return JSON.stringify({ ok: true, x: r.left + r.width/2, y: r.top + r.height/2 });
                }
            }
        }
        // fallback：全頁找「送出」按鈕
        const all = document.querySelectorAll('button, aot-button, .btn');
        for (const b of all) {
            const t = (b.innerText || b.textContent || b.getAttribute('label') || '').trim();
            if (t === '送出' && b.offsetParent !== null) {
                const r = b.getBoundingClientRect();
                return JSON.stringify({ ok: true, x: r.left + r.width/2, y: r.top + r.height/2, fallback: true });
            }
        }
        return JSON.stringify({ ok: false });
    })()
    """)
    if not result:
        return False
    try:
        data = json.loads(result)
    except Exception:
        return False
    if not data.get('ok'):
        return False
    await cdp_click_at(ws_url, data['x'], data['y'])
    await asyncio.sleep(2)
    return True


async def _search_and_check_in_picker(ws_url: str, email: str) -> bool:
    """在已開啟的「選擇聯絡人」picker 中：清空搜尋框 → 輸入 email → 等過濾 → 勾選 row 內的 label。"""
    email_json = json.dumps(email)
    typed = await cdp_eval(ws_url, f"""
        (function(email) {{
            const inputs = document.querySelectorAll('input[placeholder*="輸入聯絡人"]');
            const input = Array.from(inputs).find(i => i.offsetParent !== null);
            if (!input) return false;
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            input.focus();
            setter.call(input, '');
            input.dispatchEvent(new Event('input', {{ bubbles: true }}));
            setter.call(input, email);
            input.dispatchEvent(new Event('input', {{ bubbles: true }}));
            input.dispatchEvent(new Event('change', {{ bubbles: true }}));
            return true;
        }})({email_json})
    """)
    if not typed:
        return False
    await asyncio.sleep(1.2)

    check_result = await cdp_eval(ws_url, f"""
        (function(email) {{
            const rows = document.querySelectorAll('.mail-list .row');
            for (const row of rows) {{
                if (row.offsetParent === null) continue;
                if (!(row.textContent || '').includes(email)) continue;
                const cb = row.querySelector('input[type="checkbox"]');
                if (!cb) continue;
                if (!cb.checked) {{
                    const label = cb.closest('label') || row.querySelector('label');
                    if (label) {{ label.click(); }} else {{ cb.click(); }}
                }}
                return JSON.stringify({{ ok: true, rowText: row.textContent.trim().slice(0, 50) }});
            }}
            return JSON.stringify({{ ok: false, reason: 'no matching row' }});
        }})({email_json})
    """)
    cr = json.loads(check_result or '{}')
    if not cr.get('ok'):
        return False
    return True


async def forward_to_recipients(ws_url: str, summary: str, emails: list, detail_url: str) -> int:
    """單一候選人轉寄給多個收件人：1 次轉寄 modal + 1 次 picker，內部依序搜尋勾選 N 個 email。

    流程：
    1. navigate 到候選人詳情頁
    2. 點 sidebar「轉寄」→ 開啟「轉寄」modal
    3. 移除預設收件者
    4. 點「收件者」selector → 開啟「選擇聯絡人」picker
    5. 對每個 email：在 picker 內搜尋 + 勾選（chip 會累積到「已選取」區）
    6. 點「送出」關閉 picker
    7. 回轉寄 modal 填說明
    8. 點「發送」一次寄給所有勾選的人

    回傳：實際勾選成功的人數（0 表完全失敗）
    """
    # 1. navigate（含 1 次重試：若首次 navigate 後 40s 仍等不到按鈕，再 navigate 一次）
    btn_ready = False
    for nav_attempt in range(2):
        await navigate(ws_url, detail_url)
        for i in range(40):  # 從 20s 提升到 40s（部分履歷頁載入較慢）
            await asyncio.sleep(1)
            if await cdp_eval(ws_url, """
                (function() {
                    const btns = document.querySelectorAll('button');
                    for (const b of btns) {
                        if (b.offsetParent === null) continue;
                        if ((b.textContent || '').trim() === '轉寄' && b.querySelector('.vip-icon-forward')) return true;
                    }
                    return false;
                })()
            """):
                btn_ready = True
                print(f'    ✓ sidebar 轉寄 按鈕已渲染 ({i+1}s, nav #{nav_attempt+1})')
                break
        if btn_ready:
            break
        if nav_attempt == 0:
            print(f'    ⚠️ 40s 等不到按鈕，重新 navigate 再試一次...')
    if not btn_ready:
        print(f'    ✗ 等不到 sidebar 轉寄 按鈕')
        return 0
    await asyncio.sleep(3)

    # 2. 點 sidebar 轉寄 → 開「轉寄」modal
    btn_coords = await cdp_eval(ws_url, """
        (function() {
            const btns = document.querySelectorAll('button');
            for (const b of btns) {
                if (b.offsetParent === null) continue;
                if ((b.textContent || '').trim() === '轉寄' && b.querySelector('.vip-icon-forward')) {
                    const r = b.getBoundingClientRect();
                    return JSON.stringify({ x: r.left + r.width/2, y: r.top + r.height/2 });
                }
            }
            return null;
        })()
    """)
    if not btn_coords:
        return 0
    bc = json.loads(btn_coords)
    async with websockets.connect(ws_url, max_size=20 * 1024 * 1024) as ws:
        for i_id, params in enumerate([
            {'type': 'mouseMoved', 'x': bc['x'], 'y': bc['y']},
            {'type': 'mousePressed', 'x': bc['x'], 'y': bc['y'], 'button': 'left', 'clickCount': 1, 'buttons': 1},
            {'type': 'mouseReleased', 'x': bc['x'], 'y': bc['y'], 'button': 'left', 'clickCount': 1, 'buttons': 0},
        ]):
            await ws.send(json.dumps({'id': i_id + 1, 'method': 'Input.dispatchMouseEvent', 'params': params}))
            await ws.recv()
            await asyncio.sleep(0.1)
    await asyncio.sleep(2)

    forward_modal = await cdp_eval(ws_url, """
        (function() {
            const titles = document.querySelectorAll('.modal-title');
            for (const t of titles) {
                if (t.offsetParent === null) continue;
                const main = (t.firstChild && t.firstChild.textContent || '').trim();
                if (main === '轉寄') return true;
            }
            return false;
        })()
    """)
    if not forward_modal:
        print(f'    ✗ 「轉寄」modal 未出現')
        return 0
    print(f'    📋 轉寄 modal 已開啟')

    # 3. 移除預設收件者 tags
    await cdp_eval(ws_url, """
        (function() {
            const selectors = document.querySelectorAll('.aot-tag-selector');
            for (const s of selectors) {
                if (s.offsetParent === null) continue;
                const deletes = s.querySelectorAll('.vip-icon-delete');
                for (const d of deletes) d.click();
            }
        })()
    """)
    await asyncio.sleep(0.8)

    # 4. 點收件者 selector 開 picker
    sel_clicked = await cdp_eval(ws_url, """
        (function() {
            const selectors = document.querySelectorAll('.aot-tag-selector__input');
            for (const s of selectors) {
                if (s.offsetParent === null) continue;
                const r = s.getBoundingClientRect();
                if (r.width < 5 || r.height < 5) continue;
                return JSON.stringify({ x: r.left + r.width/2, y: r.top + r.height/2 });
            }
            return null;
        })()
    """)
    if not sel_clicked:
        print(f'    ✗ 找不到 收件者 selector')
        return 0
    sc = json.loads(sel_clicked)
    async with websockets.connect(ws_url, max_size=20 * 1024 * 1024) as ws:
        for i_id, params in enumerate([
            {'type': 'mouseMoved', 'x': sc['x'], 'y': sc['y']},
            {'type': 'mousePressed', 'x': sc['x'], 'y': sc['y'], 'button': 'left', 'clickCount': 1, 'buttons': 1},
            {'type': 'mouseReleased', 'x': sc['x'], 'y': sc['y'], 'button': 'left', 'clickCount': 1, 'buttons': 0},
        ]):
            await ws.send(json.dumps({'id': i_id + 1, 'method': 'Input.dispatchMouseEvent', 'params': params}))
            await ws.recv()
            await asyncio.sleep(0.1)
    await asyncio.sleep(2)

    picker_opened = await cdp_eval(ws_url, """
        (function() {
            const titles = document.querySelectorAll('.modal-title');
            for (const t of titles) {
                if (t.offsetParent === null) continue;
                if ((t.textContent || '').trim() === '選擇聯絡人') return true;
            }
            return false;
        })()
    """)
    if not picker_opened:
        print(f'    ✗ 「選擇聯絡人」picker 未開啟')
        return 0
    print(f'    📋 選擇聯絡人 picker 已開啟')

    # 5. 對每個 email 在 picker 內搜尋 + 勾選（chip 累積到「已選取」）
    checked_count = 0
    for email in emails:
        ok = await _search_and_check_in_picker(ws_url, email)
        if ok:
            checked_count += 1
            print(f'    ☑ 已勾選 {email}')
        else:
            print(f'    ⚠️ 找不到聯絡人 {email}（跳過）')
        await asyncio.sleep(0.5)

    if checked_count == 0:
        print(f'    ✗ 沒有任何聯絡人成功勾選，中止')
        return 0

    # 6. 點 picker「送出」
    submit_coords = await cdp_eval(ws_url, """
        (function() {
            const btns = document.querySelectorAll('button.btn-primary, button');
            for (const b of btns) {
                if (b.offsetParent === null) continue;
                if ((b.textContent || '').trim() === '送出') {
                    const r = b.getBoundingClientRect();
                    return JSON.stringify({ x: r.left + r.width/2, y: r.top + r.height/2 });
                }
            }
            return null;
        })()
    """)
    if not submit_coords:
        print(f'    ✗ 找不到 picker 送出 按鈕')
        return 0
    sb = json.loads(submit_coords)
    async with websockets.connect(ws_url, max_size=20 * 1024 * 1024) as ws:
        for i_id, params in enumerate([
            {'type': 'mouseMoved', 'x': sb['x'], 'y': sb['y']},
            {'type': 'mousePressed', 'x': sb['x'], 'y': sb['y'], 'button': 'left', 'clickCount': 1, 'buttons': 1},
            {'type': 'mouseReleased', 'x': sb['x'], 'y': sb['y'], 'button': 'left', 'clickCount': 1, 'buttons': 0},
        ]):
            await ws.send(json.dumps({'id': i_id + 1, 'method': 'Input.dispatchMouseEvent', 'params': params}))
            await ws.recv()
            await asyncio.sleep(0.1)
    await asyncio.sleep(2)
    print(f'    ✓ 已送出 picker（{checked_count}/{len(emails)} 人勾選）')

    # 7. 填說明 textarea
    fill_expr = """
    (function(text){
        const tas = Array.from(document.querySelectorAll('.modal-body textarea, textarea'));
        const ta = tas.find(t => t.offsetParent !== null);
        if (!ta) return 'no textarea';
        ta.focus();
        const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
        setter.call(ta, text);
        ta.dispatchEvent(new Event('input', {bubbles: true}));
        ta.dispatchEvent(new Event('change', {bubbles: true}));
        return ta.value.length;
    })(""" + json.dumps(summary) + ")"
    fill_result = None
    for _ in range(10):
        fill_result = await cdp_eval(ws_url, fill_expr)
        if isinstance(fill_result, int) and fill_result > 0:
            break
        await asyncio.sleep(0.5)
    if not isinstance(fill_result, int) or fill_result == 0:
        print(f'    ✗ 找不到說明 textarea')
        return 0
    print(f'    ✓ 已填說明（{fill_result} 字）')
    await asyncio.sleep(1)

    # 8. 點「發送」
    send_coords = await cdp_eval(ws_url, """
        (function() {
            const btns = document.querySelectorAll('aot-button');
            for (const b of btns) {
                if (b.offsetParent === null) continue;
                if (b.getAttribute('label') === '發送') {
                    const r = b.getBoundingClientRect();
                    return JSON.stringify({ x: r.left + r.width/2, y: r.top + r.height/2 });
                }
            }
            return null;
        })()
    """)
    if not send_coords:
        print(f'    ✗ 找不到 發送 aot-button')
        return 0
    sd = json.loads(send_coords)
    async with websockets.connect(ws_url, max_size=20 * 1024 * 1024) as ws:
        for i_id, params in enumerate([
            {'type': 'mouseMoved', 'x': sd['x'], 'y': sd['y']},
            {'type': 'mousePressed', 'x': sd['x'], 'y': sd['y'], 'button': 'left', 'clickCount': 1, 'buttons': 1},
            {'type': 'mouseReleased', 'x': sd['x'], 'y': sd['y'], 'button': 'left', 'clickCount': 1, 'buttons': 0},
        ]):
            await ws.send(json.dumps({'id': i_id + 1, 'method': 'Input.dispatchMouseEvent', 'params': params}))
            await ws.recv()
            await asyncio.sleep(0.1)
    await asyncio.sleep(3)
    await dismiss_network_error(ws_url)
    print(f'    ✓ 已點 發送 → {checked_count} 個收件人')
    return checked_count


async def forward_resume(ws_url: str, summary: str, emails: list | None = None,
                        detail_url: str | None = None) -> bool:
    """轉寄一份履歷給多個收件人（單次 modal，picker 內勾選多人後送出一次）。
    任何收件人有勾選成功就回傳 True。
    """
    if emails is None:
        emails = DEFAULT_FORWARD_EMAILS
    if not detail_url:
        print(f'  ✗ forward_resume 需要 detail_url')
        return False

    print(f'  📨 轉寄給 {len(emails)} 個收件人：{", ".join(emails)}')
    try:
        ok_count = await forward_to_recipients(ws_url, summary, emails, detail_url)
    except Exception as e:
        print(f'  ✗ 轉寄錯誤: {e}')
        return False
    print(f'  {"✓" if ok_count > 0 else "✗"} 成功 {ok_count}/{len(emails)}')
    return ok_count > 0


def load_history(log_path: Path) -> dict:
    """讀取 forward_log.json，回傳 {forwarded_ids: set, runs: list}"""
    if not log_path.exists():
        return {'forwarded_ids': set(), 'runs': []}
    try:
        raw = json.loads(log_path.read_text())
    except Exception:
        return {'forwarded_ids': set(), 'runs': []}
    if 'runs' not in raw and 'results' in raw:
        runs = [raw]
    else:
        runs = raw.get('runs', [])
    forwarded_ids = set(raw.get('forwarded_ids', []))
    for r in runs:
        for x in r.get('results', []):
            if x.get('forwarded'):
                forwarded_ids.add(x.get('resumeId'))
    return {'forwarded_ids': forwarded_ids, 'runs': runs}


def save_history(log_path: Path, forwarded_ids: set, runs: list):
    payload = {
        'forwarded_ids': sorted(forwarded_ids),
        'runs': runs,
    }
    log_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))


# ----- 履歷正文快取 -----
def load_resume_cache(cache_path: Path, ttl_days: int) -> dict:
    """讀 resume_cache.json，自動清掉過期項目。
    結構：{resumeId: {body: str, cached_at: ISO, len: int}}"""
    if not cache_path.exists():
        return {}
    try:
        raw = json.loads(cache_path.read_text())
    except Exception:
        return {}
    if ttl_days <= 0:
        return raw
    from datetime import timedelta
    cutoff = datetime.now() - timedelta(days=ttl_days)
    fresh = {}
    for rid, entry in raw.items():
        try:
            cached_at = datetime.fromisoformat(entry.get('cached_at', ''))
            if cached_at >= cutoff:
                fresh[rid] = entry
        except Exception:
            continue
    return fresh


def save_resume_cache(cache_path: Path, cache: dict):
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2))


# ----- 主流程 -----
async def run(profile: dict, threshold: int, dry_run: bool, port: int | None,
              force_ids: set, no_cache: bool, cache_ttl_days: int, refresh_ids: set,
              use_llm: bool = True):
    job_id = profile['job_id']
    display_name = profile['display_name']
    job_dir = RESULTS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    candidates_json = job_dir / 'candidates_raw.json'
    log_json = job_dir / 'forward_log.json'
    cache_json = job_dir / 'resume_cache.json'
    analysis_md = job_dir / f"analysis_{datetime.now().strftime('%Y%m%d')}.md"
    analysis_html = job_dir / f"analysis_{datetime.now().strftime('%Y%m%d')}.html"

    print(f'✓ 職缺：{display_name} ({job_id})')

    if not candidates_json.exists():
        print(f'❌ 找不到 {candidates_json}')
        print(f'   請先執行：python3 run_search.py --job {job_id}')
        return 1

    data = json.loads(candidates_json.read_text())
    candidates = data.get('candidates', [])
    print(f'✓ 共 {len(candidates)} 位人選')

    # 若搜尋結果為 0 人，產出特殊訊息報告後直接結束
    if len(candidates) == 0:
        history = load_history(log_json)
        already_forwarded = history['forwarded_ids']
        msg_md = '\n'.join([
            f'# {display_name} 人選自動化分析報告',
            f'',
            f'**執行時間**：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
            f'**職缺代碼**：{job_id}',
            f'',
            f'## ⚠️ 搜尋結果：0 位人選',
            f'',
            f'> **搜尋條件太過嚴格，未有人才符合**',
            f'',
            f'### 建議調整方向',
            f'- 放寬年齡範圍（age_min / age_max）',
            f'- 放寬總年資要求（work_exp_years）',
            f'- 增加可接受的科系（majors）',
            f'- 擴大希望工作地或居住地（work_locations / home_locations）',
            f'- 將「最近活動日」從 7 天內改為 14 天內或 1 個月內',
            f'- 減少必要證照數量或改為非必要條件',
            f'',
            f'請與 HR 主管討論後，修改 Google Drive 上的職缺需求文件，下次排程會自動套用新條件。',
        ])
        analysis_md.write_text(msg_md)
        # 也產出簡易 HTML 版
        html_body = f"""<!DOCTYPE html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<title>{display_name} - 分析報告</title>
<style>
body{{font-family:-apple-system,sans-serif;max-width:760px;margin:40px auto;padding:24px;color:#333}}
h1{{color:#1e3a8a;border-bottom:3px solid #1e3a8a;padding-bottom:8px}}
.warn{{background:#fff4e5;border-left:4px solid #ff9800;padding:16px;margin:20px 0;border-radius:4px}}
.warn .title{{font-size:1.3em;color:#e65100;font-weight:bold;margin-bottom:8px}}
ul li{{margin:6px 0}}
.meta{{color:#666;font-size:0.9em}}
</style></head><body>
<h1>{_html_escape(display_name)} 人選自動化分析報告</h1>
<p class="meta">執行時間：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}｜職缺代碼：{job_id}</p>
<div class="warn">
  <div class="title">⚠️ 搜尋結果：0 位人選</div>
  <div>搜尋條件太過嚴格，未有人才符合</div>
</div>
<h3>建議調整方向</h3>
<ul>
  <li>放寬年齡範圍（age_min / age_max）</li>
  <li>放寬總年資要求（work_exp_years）</li>
  <li>增加可接受的科系（majors）</li>
  <li>擴大希望工作地或居住地（work_locations / home_locations）</li>
  <li>將「最近活動日」從 7 天內改為 14 天內或 1 個月內</li>
  <li>減少必要證照數量或改為非必要條件</li>
</ul>
<p>請與 HR 主管討論後，修改 Google Drive 上的職缺需求文件，下次排程會自動套用新條件。</p>
</body></html>"""
        try:
            analysis_html.write_text(html_body)
        except (PermissionError, OSError) as e:
            print(f'⚠️ 無法寫入 HTML 報告：{e}')
        # 寫入空 forward_log 並結束
        history.setdefault('runs', []).append({
            'timestamp': datetime.now().isoformat(),
            'job_id': job_id, 'threshold': threshold, 'results': [],
            'note': '搜尋條件太過嚴格，未有人才符合',
        })
        save_history(log_json, already_forwarded, history['runs'])
        print(f'\n⚠️ 搜尋條件太過嚴格，未有人才符合')
        print(f'  → {analysis_md}')
        print(f'  → {analysis_html}')
        return 0

    history = load_history(log_json)
    already_forwarded = history['forwarded_ids']
    resume_cache = {} if no_cache else load_resume_cache(cache_json, cache_ttl_days)
    print(f'✓ 歷史已轉寄：{len(already_forwarded)} 人｜履歷快取：{len(resume_cache)} 筆 (TTL {cache_ttl_days} 天)'
          + (f'（強制重發：{sorted(force_ids)}）' if force_ids else '')
          + (f'（強制重抓：{sorted(refresh_ids)}）' if refresh_ids else '')
          + ('（--no-cache 不使用快取）' if no_cache else ''))

    # 決定哪些人需要連線到 Chrome：未快取 / 強制重抓的人
    rids_needing_fetch = [c.get('resumeId') for c in candidates
                          if c.get('resumeId') not in resume_cache or c.get('resumeId') in refresh_ids]
    needs_chrome_for_fetch = len(rids_needing_fetch) > 0
    # 轉寄也需要 Chrome（達門檻且未轉寄過 + 非 dry-run）
    needs_chrome_for_forward = (not dry_run) and any(
        c.get('resumeId') not in already_forwarded
        for c in candidates
    )
    needs_chrome = needs_chrome_for_fetch or needs_chrome_for_forward

    ws_url = None
    if needs_chrome:
        if port is None:
            port = detect_cdp_port()
        if port is None:
            # 軟性失敗：dry-run 可以只分析有快取的
            if dry_run and len(resume_cache) > 0:
                print(f'⚠️  找不到 Chrome（{len(rids_needing_fetch)} 人需重抓將跳過），dry-run 模式仍分析 {len(resume_cache)} 位有快取者')
            else:
                print('❌ 找不到 Chrome CDP debug port。請先用 run_search.py 啟動 Chrome 並登入 104。')
                return 1
        else:
            print(f'✓ Chrome CDP port: {port}')
            tabs = list_tabs(port)
            target_tab = next((t for t in tabs if 'vip.104.com.tw' in t.get('url', '') and t.get('type') == 'page'), None)
            if not target_tab:
                if dry_run and len(resume_cache) > 0:
                    print(f'⚠️  沒有 104 分頁，dry-run 仍分析 {len(resume_cache)} 位有快取者')
                else:
                    print('❌ 沒有開啟中的 104 分頁')
                    return 1
            else:
                ws_url = target_tab['webSocketDebuggerUrl']
                print(f'✓ Target tab: {target_tab["title"][:40]}｜需重抓 {len(rids_needing_fetch)} 人')
                # 啟動原生 JS dialog 自動接受（攔截「網路連線發生錯誤」彈窗）
                await start_dialog_listener(ws_url)
    else:
        print('✓ 全部命中快取，無需開啟 Chrome 重抓履歷')

    log = {
        'run_at': datetime.now().isoformat(), 'job_id': job_id,
        'threshold': threshold, 'dry_run': dry_run, 'results': [],
    }
    md_lines = [
        f'# {display_name} 人選自動化分析報告',
        f'> 執行時間：{datetime.now().strftime("%Y-%m-%d %H:%M")}｜job_id={job_id}｜threshold={threshold}%｜dry_run={dry_run}',
        f'\n## 排名摘要\n',
        '| 排名 | 姓名 | 匹配度 | 等級 | 來源 | 狀態 |',
        '|------|------|--------|------|------|------|',
    ]

    scored = []
    cache_hits = 0
    cache_misses = 0
    for i, c in enumerate(candidates, 1):
        name = c.get('name', '?')
        rid = c.get('resumeId', '')
        print(f'\n[{i}/{len(candidates)}] {name}')

        cache_entry = resume_cache.get(rid)
        force_refresh = rid in refresh_ids
        if cache_entry and not force_refresh and not no_cache:
            body = cache_entry.get('body', '')
            print(f'  ⚡ 命中快取（{len(body)} 字，{cache_entry.get("cached_at","")[:10]}），跳過 navigation')
            from_cache = True
            cache_hits += 1
        else:
            if ws_url is None:
                print(f'  ✗ 需重抓但無 Chrome session，略過')
                continue
            body = await fetch_resume_text(ws_url, c['detailHref'])
            resume_cache[rid] = {
                'body': body,
                'cached_at': datetime.now().isoformat(),
                'len': len(body),
                'name': name,
            }
            # 立刻寫回，避免中途中斷遺失
            save_resume_cache(cache_json, resume_cache)
            from_cache = False
            cache_misses += 1

        result = score(c, body, profile)
        rule_score = result['score']  # 純規則分（0-100）

        # LLM 評分（0-100，從自傳/工作描述語意取得；與規則分按權重融合）
        llm_result = None
        # 極端規則分跳過 LLM 評分以省時間：< 30（明顯不符）或 > 90（明顯符合）
        skip_llm_extreme = (rule_score < 20 or rule_score > 95)
        if use_llm and LLM_AVAILABLE and not skip_llm_extreme:
            cached_llm = (cache_entry or {}).get('llm_score') if cache_entry else None
            # 舊快取 max_score 是 20，新版是 100；若分數 ≤20 視為舊版需重算
            if cached_llm and cached_llm.get('score', 0) > 20 and not force_refresh:
                llm_result = cached_llm
                print(f'  ⚡ LLM 評分命中快取：{llm_result["score"]}/100')
            else:
                if cached_llm and cached_llm.get('score', 0) <= 20:
                    print(f'  🤖 偵測到舊版 LLM 快取（0-20 制），重新評分...')
                cfg = llm_score.get_llm_config(profile)
                print(f'  🤖 LLM 評分中（{cfg["scoring_model"]}）...')
                t0 = datetime.now()
                llm_result = llm_score.score_autobiography(body, profile)
                if llm_result:
                    elapsed = (datetime.now() - t0).total_seconds()
                    print(f'  🤖 LLM 評分：{llm_result["score"]}/100（{elapsed:.0f}s）｜{llm_result.get("reasoning","")[:60]}')
                    if rid in resume_cache:
                        resume_cache[rid]['llm_score'] = llm_result
                        save_resume_cache(cache_json, resume_cache)
                else:
                    print(f'  🤖 LLM 評分失敗，僅用規則分')
        elif skip_llm_extreme:
            print(f'  ⏭ 規則分 {rule_score} 屬極端值，跳過 LLM 評分（省 ~40s）')

        # 權重融合
        if llm_result:
            cfg = llm_score.get_llm_config(profile)
            rw = cfg.get('rule_weight', 0.4)
            lw = cfg.get('llm_weight', 0.6)
            llm_pct = llm_result['score']
            combined = round(rule_score * rw + llm_pct * lw)
            result['score'] = min(100, combined)
            result['rule_score'] = rule_score
            result['llm_score'] = llm_pct
            result['reasons'].append(
                f"加權合成：規則 {rule_score} × {rw} + LLM {llm_pct} × {lw} = {combined}"
            )
            result['reasons'].append(f"LLM 評語：{llm_result.get('reasoning','')[:120]}")
            result['llm'] = llm_result

        c['_body'] = body
        c['_result'] = result
        c['_from_cache'] = from_cache
        scored.append(c)
        print(f'  匹配度 {result["score"]}% / {result["level"]}')

        forwarded = False
        skipped_dup = False
        if result['score'] >= threshold:
            if rid in already_forwarded and rid not in force_ids:
                skipped_dup = True
                print(f'  ⏭  已轉寄過，略過（用 --force {rid} 可重發）')
            elif dry_run:
                print(f'  [dry-run] 達門檻但略過實際轉寄')
            else:
                if ws_url is None:
                    print(f'  ✗ 達門檻但無 Chrome session 可轉寄')
                else:
                    print(f'  ▶ 達 {threshold}% 門檻，執行轉寄...')
                    # 優先用 LLM 寫客製化說明，失敗再用範本
                    summary = None
                    if use_llm and LLM_AVAILABLE:
                        cfg = llm_score.get_llm_config(profile)
                        print(f'  🤖 LLM 撰寫說明文字（{cfg["summary_model"]}）...')
                        t0 = datetime.now()
                        summary = llm_score.generate_summary(name, body, result, llm_result, profile,
                                                             max_chars=profile.get('forward', {}).get('summary_max_chars', 980))
                        if summary:
                            print(f'  🤖 LLM 說明完成（{(datetime.now()-t0).total_seconds():.0f}s, {len(summary)} 字）')
                        else:
                            print(f'  🤖 LLM 撰寫失敗，使用範本')
                    if not summary:
                        summary = build_summary(name, result, body, profile)
                    try:
                        fwd_emails = profile.get('forward', {}).get('emails', DEFAULT_FORWARD_EMAILS)
                        # 新版 forward_resume：對每個 email 各做一次完整流程（navigate + click 轉寄）
                        forwarded = await forward_resume(ws_url, summary, emails=fwd_emails,
                                                          detail_url=c['detailHref'])
                        print(f'  {"✓ 轉寄成功" if forwarded else "✗ 轉寄失敗"}')
                        if forwarded:
                            already_forwarded.add(rid)
                    except Exception as e:
                        print(f'  ✗ 轉寄錯誤: {e}')

        log['results'].append({
            'name': name, 'resumeId': rid,
            'score': result['score'], 'level': result['level'],
            'reasons': result['reasons'], 'from_cache': from_cache,
            'forwarded': forwarded, 'skipped_dup': skipped_dup,
        })

    # 排序產出 markdown
    scored.sort(key=lambda x: x['_result']['score'], reverse=True)
    for rk, c in enumerate(scored, 1):
        r = c['_result']
        rec = next((x for x in log['results'] if x['resumeId'] == c['resumeId']), {})
        if rec.get('forwarded'):
            status = '✅ 本次轉寄'
        elif rec.get('skipped_dup'):
            status = '⏭ 已轉寄過'
        elif r['score'] >= threshold:
            status = '○ 達門檻(dry-run)' if dry_run else '✗ 失敗'
        else:
            status = '—'
        source = '⚡快取' if c.get('_from_cache') else '🌐重抓'
        md_lines.append(f"| {rk} | {c['name']} | **{r['score']}%** | {r['level']} | {source} | {status} |")

    md_lines.append('\n---\n')
    for rk, c in enumerate(scored, 1):
        r = c['_result']
        md_lines.append(f"## {rk}. {c['name']}｜{r['level']}｜{r['score']}%")
        md_lines.append(f"- 履歷代碼：{c['resumeId']}｜{c.get('age','')}｜{c.get('residence','')}")
        md_lines.append(f"- 學歷：{c.get('education','')}")
        md_lines.append(f"- 希望職稱：{c.get('preferJobTitle','').replace('希望職稱 :','').strip()}")
        md_lines.append(f"- 評分依據：")
        for rr in r['reasons']:
            md_lines.append(f"  - {rr}")
        md_lines.append('')

    analysis_md.write_text('\n'.join(md_lines))

    # 另外產生 HTML 版本，方便 HR 觀看與列印
    try:
        analysis_html.write_text(_render_html_report(
            job_id=job_id,
            display_name=display_name,
            profile=profile,
            threshold=threshold,
            scored=scored,
            log=log,
            resume_cache=resume_cache,
        ))
    except (ValueError, PermissionError, OSError) as e:
        print(f'⚠️ HTML 報告產生失敗（不影響轉寄記錄）：{e}')
    history['runs'].append(log)
    save_history(log_json, already_forwarded, history['runs'])
    save_resume_cache(cache_json, resume_cache)

    forwarded_count = sum(1 for x in log['results'] if x['forwarded'])
    skipped_count = sum(1 for x in log['results'] if x['skipped_dup'])
    print(f'\n=========================================')
    print(f'✓ 完成：{len(candidates)} 人分析｜⚡快取命中 {cache_hits}｜🌐重抓 {cache_misses}｜'
          f'{forwarded_count} 人本次轉寄｜{skipped_count} 人略過(已轉寄)')
    print(f'  → {analysis_md}')
    print(f'  → {log_json}')
    print(f'  → {cache_json}')

    await stop_dialog_listener()
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--job', type=str, default=None,
                   help='職缺代碼（jobs/<id>.json）。例：jidian-tainan / jidian-kaohsiung / gongdi-tainan / gongdi-taichung')
    p.add_argument('--list-jobs', action='store_true', help='列出所有可用 jobs')
    p.add_argument('--threshold', type=int, default=None,
                   help='轉寄門檻百分比（預設讀 profile.scoring.threshold）')
    p.add_argument('--dry-run', action='store_true', help='只分析不實際轉寄')
    p.add_argument('--port', type=int, default=None, help='Chrome CDP debug port (預設自動偵測)')
    p.add_argument('--force', action='append', default=[],
                   help='強制重發指定 resumeId（可重複指定）')
    p.add_argument('--reset-history', action='store_true',
                   help='清空該 job 的轉寄去重記錄（保留 runs 歷史）')
    p.add_argument('--no-cache', action='store_true',
                   help='不使用履歷快取，每筆都重新爬取')
    p.add_argument('--cache-ttl-days', type=int, default=3,
                   help='履歷快取有效天數（預設 3 天）')
    p.add_argument('--refresh', action='append', default=[],
                   help='強制重抓指定 resumeId 的履歷（可重複指定）')
    p.add_argument('--clear-cache', action='store_true',
                   help='清空該 job 的履歷快取')
    p.add_argument('--no-llm', action='store_true',
                   help='關閉本地 LLM 評分與說明生成（純規則模式）')
    p.add_argument('--llm-check', action='store_true',
                   help='檢查 Ollama 連線與可用模型後退出')
    args = p.parse_args()

    if args.llm_check:
        if not LLM_AVAILABLE:
            print('❌ llm_score.py 模組不存在')
            sys.exit(1)
        h = llm_score.health_check()
        if h['ok']:
            print('✓ Ollama 連線正常')
            print(f'✓ 可用模型 ({len(h["models"])}):')
            for m in h['models']:
                print(f'  - {m}')
        else:
            print(f'❌ Ollama 連線失敗: {h.get("error","")}')
            print('   請確認：ollama serve')
        sys.exit(0 if h['ok'] else 1)

    if args.list_jobs:
        sys.exit(list_jobs())
    if not args.job:
        p.error('需要 --job <id>，或用 --list-jobs 查看可用職缺')

    profile = load_job_profile(args.job)
    threshold = args.threshold if args.threshold is not None else profile['scoring']['threshold']

    log_json = RESULTS_DIR / args.job / 'forward_log.json'
    cache_json = RESULTS_DIR / args.job / 'resume_cache.json'
    if args.reset_history and log_json.exists():
        h = load_history(log_json)
        save_history(log_json, set(), h['runs'])
        print(f'✓ 已清空 {args.job} 去重記錄（runs 歷史保留 {len(h["runs"])} 筆）')
    if args.clear_cache and cache_json.exists():
        cache_json.unlink()
        print(f'✓ 已清空 {args.job} 履歷快取')

    sys.exit(asyncio.run(run(
        profile, threshold, args.dry_run, args.port,
        set(args.force), args.no_cache, args.cache_ttl_days, set(args.refresh),
        use_llm=not args.no_llm,
    )))


if __name__ == '__main__':
    main()
