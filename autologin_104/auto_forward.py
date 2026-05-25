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
    display_name = get_job_title_from_brief(profile)
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
    if residence_kw:
        head += f'\n\n建議優先邀約面試，確認集合住宅意願與駐點{residence_kw}條件。'
    else:
        head += '\n\n建議優先邀約面試。'
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


DEFAULT_FORWARD_EMAILS = ['fongchien19@gmail.com']


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


async def forward_one_recipient(ws_url: str, summary: str, email: str, detail_url: str) -> bool:
    """單一收件人轉寄：navigate → 點轉寄 → 在 picker 選此 email → 送出 → 填說明 → 發送。

    每次呼叫都會重新 navigate，確保 Vue 完整初始化 click handler。
    """
    # 1. 重新 navigate（即使是同一 URL，Page.navigate 會強制重新載入）
    await navigate(ws_url, detail_url)

    # 等 轉寄 按鈕渲染
    btn_ready = False
    for i in range(20):
        await asyncio.sleep(1)
        found_by = await cdp_eval(ws_url, """
            (function() {
                // 優先：找含 vip-icon-forward 圖示的按鈕（不受文字/letterSpacing影響）
                const iconEl = document.querySelector('.vip-icon-forward');
                if (iconEl) {
                    const btn = iconEl.closest('button, [role="button"]') || iconEl.parentElement;
                    if (btn) {
                        const r = btn.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) return 'icon';
                    }
                }
                // 備援：文字比對
                const btns = document.querySelectorAll('button, aot-button, [role="button"]');
                for (const b of btns) {
                    const r = b.getBoundingClientRect();
                    if (r.width === 0 && r.height === 0) continue;
                    const t = (b.innerText || b.textContent || b.getAttribute('label') || '').replace(/\s+/g, '');
                    if (t === '轉寄') return 'text';
                }
                return null;
            })()
        """)
        if found_by:
            btn_ready = True
            print(f'    ✓ 轉寄 按鈕已渲染 ({i+1}s, by={found_by})')
            break
    if not btn_ready:
        debug_btns = await cdp_eval(ws_url, """
            (function() {
                const btns = Array.from(document.querySelectorAll('button, aot-button, [role="button"]'));
                const vis = btns.filter(b => { const r = b.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
                    .map(b => (b.innerText || b.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 20));
                return JSON.stringify(vis.slice(0, 15));
            })()
        """)
        print(f'    ✗ 等不到 轉寄 按鈕，頁面可見按鈕：{debug_btns}')
        return False
    await asyncio.sleep(3)  # 多等讓 Vue 綁定事件

    # 清掉可能的遺留 modal
    await cdp_eval(ws_url, """
        (function() {
            const closes = document.querySelectorAll('.close, .modal-close, [aria-label="Close"], [aria-label="關閉"]');
            for (const c of closes) {
                const r = c.getBoundingClientRect();
                if (r.width > 0 || r.height > 0) c.click();
            }
        })()
    """)
    async with websockets.connect(ws_url, max_size=20 * 1024 * 1024) as ws:
        for params in [
            {'type': 'keyDown', 'key': 'Escape', 'code': 'Escape', 'windowsVirtualKeyCode': 27, 'nativeVirtualKeyCode': 27},
            {'type': 'keyUp', 'key': 'Escape', 'code': 'Escape', 'windowsVirtualKeyCode': 27, 'nativeVirtualKeyCode': 27},
        ]:
            await ws.send(json.dumps({'id': 1, 'method': 'Input.dispatchKeyEvent', 'params': params}))
            await ws.recv()
    await asyncio.sleep(0.5)

    # 2. 點擊 轉寄 按鈕（試多種方式直到 picker 開啟）
    btn_coords = await cdp_eval(ws_url, """
        (function() {
            // 優先：vip-icon-forward 圖示
            const iconEl = document.querySelector('.vip-icon-forward');
            if (iconEl) {
                const btn = iconEl.closest('button, [role="button"]') || iconEl.parentElement;
                if (btn) {
                    const r = btn.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) {
                        btn.focus && btn.focus();
                        return JSON.stringify({ x: r.left + r.width/2, y: r.top + r.height/2 });
                    }
                }
            }
            // 備援：文字比對
            const btns = document.querySelectorAll('button, aot-button, [role="button"]');
            for (const b of btns) {
                const r = b.getBoundingClientRect();
                if (r.width === 0 && r.height === 0) continue;
                const t = (b.innerText || b.textContent || b.getAttribute('label') || '').replace(/\s+/g, '');
                if (t === '轉寄') {
                    b.focus && b.focus();
                    return JSON.stringify({ x: r.left + r.width/2, y: r.top + r.height/2 });
                }
            }
            return null;
        })()
    """)
    if not btn_coords:
        print(f'    ✗ 找不到 轉寄 按鈕座標')
        return False
    bc = json.loads(btn_coords)

    # 試 3 種點擊方式，每次後檢查 picker 是否開啟
    picker_opened = False
    for method in ['cdp_mouse', 'js_click', 'enter_key']:
        if method == 'cdp_mouse':
            async with websockets.connect(ws_url, max_size=20 * 1024 * 1024) as ws:
                for i_id, params in enumerate([
                    {'type': 'mouseMoved', 'x': bc['x'], 'y': bc['y']},
                    {'type': 'mousePressed', 'x': bc['x'], 'y': bc['y'], 'button': 'left', 'clickCount': 1, 'buttons': 1},
                    {'type': 'mouseReleased', 'x': bc['x'], 'y': bc['y'], 'button': 'left', 'clickCount': 1, 'buttons': 0},
                ]):
                    await ws.send(json.dumps({'id': i_id + 1, 'method': 'Input.dispatchMouseEvent', 'params': params}))
                    await ws.recv()
                    await asyncio.sleep(0.08)
        elif method == 'js_click':
            await cdp_eval(ws_url, """
                (function() {
                    // 優先：vip-icon-forward 圖示
                    const iconEl = document.querySelector('.vip-icon-forward');
                    if (iconEl) {
                        const btn = iconEl.closest('button, [role="button"]') || iconEl.parentElement;
                        if (btn) { btn.click(); return true; }
                    }
                    // 備援：文字比對
                    const btns = document.querySelectorAll('button');
                    for (const b of btns) {
                        const r = b.getBoundingClientRect();
                        if (r.width === 0 && r.height === 0) continue;
                        if ((b.textContent || '').replace(/\s+/g, '') === '轉寄') {
                            b.click();
                            const i = b.querySelector('i');
                            const s = b.querySelector('span');
                            if (i) i.click();
                            if (s) s.click();
                            return true;
                        }
                    }
                    return false;
                })()
            """)
        elif method == 'enter_key':
            async with websockets.connect(ws_url, max_size=20 * 1024 * 1024) as ws:
                for params in [
                    {'type': 'keyDown', 'key': 'Enter', 'code': 'Enter', 'windowsVirtualKeyCode': 13, 'nativeVirtualKeyCode': 13},
                    {'type': 'keyUp', 'key': 'Enter', 'code': 'Enter', 'windowsVirtualKeyCode': 13, 'nativeVirtualKeyCode': 13},
                ]:
                    await ws.send(json.dumps({'id': 1, 'method': 'Input.dispatchKeyEvent', 'params': params}))
                    await ws.recv()
        await asyncio.sleep(2)
        # 檢查 picker 是否開啟
        opened = await cdp_eval(ws_url, """
            (function() {
                // 找含「選擇聯絡人」標題或 placeholder「聯絡人姓名」的 input
                const all = document.querySelectorAll('h1,h2,h3,h4,h5,div,span');
                for (const e of all) {
                    if (e.offsetParent === null) continue;
                    if ((e.textContent || '').trim() === '選擇聯絡人') return true;
                }
                const inputs = document.querySelectorAll('input');
                for (const i of inputs) {
                    if (i.offsetParent === null) continue;
                    if (/聯絡人|姓名/.test(i.placeholder || '')) return true;
                }
                return false;
            })()
        """)
        if opened:
            print(f'    📋 picker 已開啟（方法: {method}）')
            picker_opened = True
            break

    if not picker_opened:
        print(f'    ✗ 3 種點擊方式都無法開啟 picker')
        return False

    # 3. 在 picker 中：先取消所有已選取的（清除預設），然後搜尋並勾選此 email
    await cdp_eval(ws_url, """
        (function() {
            // 點掉「已選取」區的所有 × 移除按鈕
            const removes = document.querySelectorAll('.tag-close, .chip-close, [aria-label*="remove"], [aria-label*="移除"]');
            for (const r of removes) {
                if (r.offsetParent !== null) r.click();
            }
            // 通用：找 chip 內的 × 文字
            const tags = document.querySelectorAll('.tag, .chip, [class*="selected-item"]');
            for (const t of tags) {
                if (t.offsetParent === null) continue;
                const closeIcon = Array.from(t.querySelectorAll('*')).find(e => (e.textContent || '').trim() === '×' || (e.textContent || '').trim() === '✕');
                if (closeIcon) closeIcon.click();
            }
        })()
    """)
    await asyncio.sleep(0.5)

    # 搜尋並勾選
    ok = await select_contact_by_search(ws_url, email)
    if not ok:
        print(f'    ✗ 無法勾選聯絡人 {email}')
        return False
    print(f'    📧 已勾選 {email}')

    # 4. 點 picker 的「送出」
    if not await submit_contact_picker(ws_url):
        print(f'    ⚠️ 點不到 送出 按鈕')
    await asyncio.sleep(2)

    # 5. 填說明 textarea
    fill_expr = """
    (function(text){
        const tas = Array.from(document.querySelectorAll('textarea')).filter(t => t.offsetParent !== null);
        const ta = tas[0];
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
        print(f'    ✗ 找不到說明 textarea ({fill_result})')
        return False
    print(f'    ✓ 已填說明（{fill_result} 字）')
    await asyncio.sleep(1)

    # 6. 點「發送」
    coords_str = await cdp_eval(ws_url, """
        (function(){
            const candidates = Array.from(document.querySelectorAll('aot-button, button, .btn, [role="button"]'));
            const btn = candidates.find(b => {
                if (b.offsetParent === null) return false;
                const t = (b.innerText || b.textContent || b.getAttribute('label') || '').trim();
                return t === '發送' || t === '送出' || t === '確認送出';
            });
            if (!btn) return null;
            const r = btn.getBoundingClientRect();
            return JSON.stringify({x: r.left + r.width/2, y: r.top + r.height/2});
        })()
    """)
    if not coords_str:
        print(f'    ✗ 找不到 發送 按鈕')
        return False
    cs = json.loads(coords_str)
    await cdp_click_at(ws_url, cs['x'], cs['y'])
    await asyncio.sleep(3)
    await dismiss_network_error(ws_url)
    print(f'    ✓ 已點 發送 → {email}')
    return True


async def forward_resume(ws_url: str, summary: str, emails: list | None = None,
                        detail_url: str | None = None) -> bool:
    """對 emails 中的每個收件人，各跑一次完整的轉寄流程。
    任何一個成功就回傳 True。
    """
    if emails is None:
        emails = DEFAULT_FORWARD_EMAILS
    if not detail_url:
        print(f'  ✗ forward_resume 需要 detail_url 才能 navigate')
        return False

    success_count = 0
    for idx, email in enumerate(emails, 1):
        print(f'  📨 轉寄 ({idx}/{len(emails)}) → {email}')
        try:
            if await forward_one_recipient(ws_url, summary, email, detail_url):
                success_count += 1
            else:
                print(f'    ✗ {email} 轉寄失敗')
        except Exception as e:
            print(f'    ✗ {email} 轉寄錯誤: {e}')
        await asyncio.sleep(2)
    print(f'  ✓ 成功 {success_count}/{len(emails)}')
    return success_count > 0



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

    print(f'✓ 職缺：{display_name} ({job_id})')

    if not candidates_json.exists():
        print(f'❌ 找不到 {candidates_json}')
        print(f'   請先執行：python3 run_search.py --job {job_id}')
        return 1

    data = json.loads(candidates_json.read_text())
    candidates = data.get('candidates', [])
    print(f'✓ 共 {len(candidates)} 位人選')

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
        if use_llm and LLM_AVAILABLE:
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

        # 權重融合
        if llm_result:
            cfg = llm_score.get_llm_config(profile)
            rw = cfg.get('rule_weight', 0.6)
            lw = cfg.get('llm_weight', 0.4)
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
