#!/usr/bin/env python3
"""
server.py — 招募人才管理知識庫 本機網頁介面

啟動：
  python3 talent_kb/server.py          # 預設 http://localhost:8090
  python3 talent_kb/server.py --port 8095

功能：
  1. 人才搜尋庫    — 關鍵字 + 篩選（地區/分數/職類/職缺/轉寄狀態）
  2. 語意檢索 RAG  — 自然語言查詢（all-minilm 向量 cosine）
  3. 跨職缺智能媒合 — 每位人才對 6 職缺的實際分數 + 語意契合度
  4. 決策回饋學習  — 標記面試/錄取/婉拒，沉澱用人決策

全部 Python 標準庫，無需 pip install。資料讀自 talent_kb/talent.db。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

DB_PATH = Path(__file__).resolve().parent / 'talent.db'
OLLAMA_EMBED_URL = 'http://localhost:11434/api/embeddings'
EMBED_MODEL = 'bge-m3'   # 與 build_db 一致
CHUNK_CHARS = 2000


# ---------- embedding（查詢用，與 build_db 一致）----------
def _embed_one(text: str):
    try:
        req = urllib.request.Request(
            OLLAMA_EMBED_URL,
            data=json.dumps({'model': EMBED_MODEL, 'prompt': text}).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
        )
        r = json.loads(urllib.request.urlopen(req, timeout=30).read())
        return r.get('embedding')
    except Exception:
        return None


def embed_query(text: str):
    text = (text or '').strip()
    if not text:
        return None
    chunks = [text[i:i + CHUNK_CHARS] for i in range(0, len(text), CHUNK_CHARS)][:10]
    vecs = [v for v in (_embed_one(c) for c in chunks) if v]
    if not vecs:
        return None
    dim = len(vecs[0])
    mean = [sum(v[i] for v in vecs) / len(vecs) for i in range(dim)]
    norm = sum(x * x for x in mean) ** 0.5 or 1.0
    return [x / norm for x in mean]


def cosine(a, b):
    # a, b 已 L2 正規化 → dot product 即 cosine
    return sum(x * y for x, y in zip(a, b))


# ---------- DB 存取 ----------
def migrate_db():
    if not DB_PATH.exists():
        return
    conn = sqlite3.connect(DB_PATH)
    for col, ddl in [
        ('assignee', 'TEXT'),
        ('interviewer_note', 'TEXT'),
    ]:
        try:
            conn.execute(f"ALTER TABLE feedback ADD COLUMN {col} {ddl}")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()


def get_all_assignees():
    conn = get_conn()
    try:
        rows = conn.execute('SELECT DISTINCT assignee FROM feedback WHERE assignee IS NOT NULL AND assignee != "" ORDER BY assignee').fetchall()
        return [r['assignee'] for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def load_jobs():
    conn = get_conn()
    rows = conn.execute('SELECT job_id, display_name, embedding FROM jobs').fetchall()
    conn.close()
    jobs = {}
    for r in rows:
        jobs[r['job_id']] = {
            'display_name': r['display_name'],
            'embedding': json.loads(r['embedding']) if r['embedding'] else None,
        }
    return jobs


def row_to_candidate(r):
    return {
        'rid': r['rid'],
        'name': r['name'] or '(未知)',
        'age': r['age'],
        'gender': r['gender'],
        'residence': r['residence'],
        'prefer_area': r['prefer_area'],
        'education': r['education'],
        'prefer_title': r['prefer_title'],
        'work_total': r['work_total'],
        'experiences': json.loads(r['experiences'] or '[]'),
        'llm_score': r['llm_score'],
        'level': r['level'],
        'reasoning': r['reasoning'],
        'highlights': json.loads(r['highlights'] or '[]'),
        'jobs_analysis': json.loads(r['jobs_analysis'] or '[]'),
        'detail_href': r['detail_href'],
        'embedding': json.loads(r['embedding']) if r['embedding'] else None,
    }


def search(q='', mode='keyword', area='', min_score=0, job='', forwarded='', limit=60):
    conn = get_conn()
    rows = [row_to_candidate(r) for r in conn.execute(
        "SELECT * FROM candidates WHERE name IS NOT NULL AND name != ''").fetchall()]
    # appearances: rid -> {job_id: {score, forwarded}}
    app = {}
    for a in conn.execute('SELECT * FROM appearances').fetchall():
        app.setdefault(a['rid'], {})[a['job_id']] = {'score': a['score'], 'forwarded': a['forwarded']}
    conn.close()

    qvec = embed_query(q) if (mode == 'semantic' and q) else None

    out = []
    for c in rows:
        c['appearances'] = app.get(c['rid'], {})
        c['best_score'] = max([c['llm_score'] or 0] + [v['score'] for v in c['appearances'].values()], default=0)
        c['ever_forwarded'] = any(v['forwarded'] for v in c['appearances'].values())

        # 篩選
        if area and area not in (c['residence'] or '') and area not in (c['prefer_area'] or ''):
            continue
        if min_score and c['best_score'] < min_score:
            continue
        if job and job not in c['appearances']:
            continue
        if forwarded == 'yes' and not c['ever_forwarded']:
            continue
        if forwarded == 'no' and c['ever_forwarded']:
            continue

        # 查詢比對
        if q:
            if mode == 'semantic':
                if not (qvec and c['embedding']):
                    continue
                c['sim'] = cosine(qvec, c['embedding'])
            else:
                hay = ' '.join([
                    c['name'] or '', c['prefer_title'] or '', c['education'] or '',
                    c['residence'] or '', c['prefer_area'] or '', c['reasoning'] or '',
                    ' '.join(c['highlights']), ' '.join(str(e) for e in c['experiences']),
                ])
                if q not in hay:
                    continue
                c['sim'] = None
        else:
            c['sim'] = None
        out.append(c)

    # 排序：語意模式按相似度，否則按最佳分數
    if mode == 'semantic' and q:
        out.sort(key=lambda x: x.get('sim') or 0, reverse=True)
    else:
        out.sort(key=lambda x: x['best_score'], reverse=True)
    return out[:limit]


def candidate_detail(rid):
    conn = get_conn()
    r = conn.execute('SELECT * FROM candidates WHERE rid=?', (rid,)).fetchone()
    if not r:
        conn.close()
        return None
    c = row_to_candidate(r)
    c['appearances'] = {}
    for a in conn.execute('SELECT * FROM appearances WHERE rid=?', (rid,)).fetchall():
        c['appearances'][a['job_id']] = {'score': a['score'], 'forwarded': a['forwarded'], 'run_at': a['run_at']}
    c['feedback'] = {}
    for fb in conn.execute('SELECT * FROM feedback WHERE rid=?', (rid,)).fetchall():
        fb_dict = dict(fb)
        c['feedback'][fb['job_id']] = {
            'status': fb_dict.get('status', 'pending'),
            'note': fb_dict.get('note', ''),
            'assignee': fb_dict.get('assignee', ''),
            'interviewer_note': fb_dict.get('interviewer_note', ''),
            'updated_at': fb_dict.get('updated_at', '')
        }
    conn.close()

    # 跨職缺媒合
    jobs = load_jobs()
    matches = []
    for jid, jinfo in jobs.items():
        real = c['appearances'].get(jid, {}).get('score')
        affinity = None
        if c['embedding'] and jinfo['embedding']:
            affinity = round(cosine(c['embedding'], jinfo['embedding']), 3)
        matches.append({
            'job_id': jid,
            'display_name': jinfo['display_name'],
            'real_score': real,           # 實際被評分過的分數（None = 沒搜過）
            'affinity': affinity,         # 語意契合度 0-1
            'forwarded': c['appearances'].get(jid, {}).get('forwarded', 0),
        })
    # 排序：有實際分數優先、再按 affinity
    matches.sort(key=lambda m: (m['real_score'] or 0, m['affinity'] or 0), reverse=True)
    c['matches'] = matches
    return c


def save_feedback(rid, job_id, status, note, assignee, interviewer_note=''):
    from datetime import datetime
    conn = get_conn()
    conn.execute("""INSERT OR REPLACE INTO feedback
                    (rid,job_id,status,note,assignee,interviewer_note,updated_at)
                    VALUES (?,?,?,?,?,?,?)""",
                 (rid, job_id, status, note, assignee, interviewer_note,
                  datetime.now().isoformat()))
    conn.commit()
    conn.close()


def stats():
    conn = get_conn()
    n = conn.execute('SELECT COUNT(*) FROM candidates').fetchone()[0]
    scored = conn.execute('SELECT COUNT(*) FROM candidates WHERE llm_score IS NOT NULL').fetchone()[0]
    fwd = conn.execute('SELECT COUNT(DISTINCT rid) FROM appearances WHERE forwarded=1').fetchone()[0]
    jobs = [dict(r) for r in conn.execute('SELECT job_id, display_name FROM jobs').fetchall()]
    fb = conn.execute('SELECT COUNT(*) FROM feedback').fetchone()[0]
    conn.close()
    return {'total': n, 'scored': scored, 'forwarded': fwd, 'jobs': jobs, 'feedback': fb}


# ---------- HTML ----------
def esc(s):
    s = str(s or '')
    return (s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            .replace('"', '&quot;'))


def page_search():
    st = stats()
    job_opts = ''.join(f'<option value="{esc(j["job_id"])}">{esc(j["display_name"])}</option>' for j in st['jobs'])
    return f"""<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>招募人才知識庫</title>
<style>
:root{{
  --cream:#F6F1E9;--parchment:#FDFAF5;--sand:#EDE5D8;
  --sage:#7A9B7E;--sage-d:#5C7A60;--sage-pale:#EAF0EB;
  --brown:#4A3F35;--brown-m:#7A6E65;--brown-l:#B0A49A;
  --stone:#D5CBBE;--charcoal:#1E1A17;
}}
*{{box-sizing:border-box}}
body{{font-family:"PingFang TC",-apple-system,"Helvetica Neue",sans-serif;margin:0;background:var(--cream);color:var(--brown);letter-spacing:.01em}}
header{{background:var(--charcoal);color:#F0EAE0;padding:16px 32px;display:flex;align-items:center;gap:20px;flex-wrap:wrap;border-bottom:2px solid var(--sage)}}
header h1{{font-size:1.05em;margin:0;font-weight:500;letter-spacing:.08em;text-transform:uppercase}}
header .stat{{font-size:.8em;color:var(--brown-l);letter-spacing:.04em}}
.wrap{{max-width:1100px;margin:0 auto;padding:24px 20px}}
.searchbar{{background:var(--parchment);border-radius:2px;padding:18px 20px;border:1px solid var(--stone);margin-bottom:20px}}
.searchbar .row{{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:10px}}
.searchbar input[type=text],.searchbar select{{padding:8px 12px;border:1px solid var(--stone);border-radius:2px;font-size:13.5px;background:var(--cream);color:var(--brown);font-family:inherit}}
.searchbar input[type=text]:focus,.searchbar select:focus{{outline:none;border-color:var(--sage);box-shadow:0 0 0 2px var(--sage-pale)}}
#q{{flex:1;min-width:240px}}
.modetag{{display:inline-flex;border:1px solid var(--stone);border-radius:2px;overflow:hidden}}
.modetag label{{cursor:pointer;font-size:12.5px;letter-spacing:.04em}}
.modetag input{{display:none}}
.modetag input:checked+span{{background:var(--sage);color:#fff}}
.modetag span{{padding:7px 14px;display:block;color:var(--brown-m)}}
button.go{{background:var(--sage);color:#fff;border:0;padding:9px 22px;border-radius:2px;cursor:pointer;font-size:13.5px;letter-spacing:.06em;font-family:inherit;transition:background .15s}}
button.go:hover{{background:var(--sage-d)}}
.card{{background:var(--parchment);border-radius:2px;padding:14px 18px;margin-bottom:8px;border:1px solid var(--stone);cursor:pointer;border-left:3px solid var(--stone);transition:border-color .15s,box-shadow .15s}}
.card:hover{{border-color:var(--sage);box-shadow:0 3px 14px rgba(74,63,53,.08)}}
.card.s80{{border-left-color:var(--sage-d)}}.card.s70{{border-left-color:var(--sage)}}.card.s60{{border-left-color:var(--brown-l)}}
.card .top{{display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:8px}}
.card .nm{{font-size:1.05em;font-weight:600;color:var(--charcoal)}}
.card .sc{{font-size:1.25em;font-weight:700;color:var(--sage-d)}}
.card .meta{{color:var(--brown-m);font-size:.85em;margin:3px 0;line-height:1.5}}
.card .tags span{{display:inline-block;background:var(--sand);color:var(--brown-m);border-radius:2px;padding:2px 8px;margin:2px 4px 2px 0;font-size:.78em}}
.badge{{font-size:.72em;padding:2px 8px;border-radius:2px;margin-left:6px}}
.badge.fwd{{background:var(--sage-pale);color:var(--sage-d);border:1px solid var(--sage);white-space:nowrap}}
.sim{{color:var(--brown-m);font-size:.82em;font-style:italic}}
.hint{{color:var(--brown-l);font-size:.82em;margin:6px 0;line-height:1.6}}
#results .empty{{text-align:center;color:var(--brown-l);padding:48px;letter-spacing:.04em}}
</style></head><body>
<header>
  <h1>招募人才知識庫</h1>
  <span class="stat">人才 {st['total']} ｜ 已評分 {st['scored']} ｜ 曾轉寄 {st['forwarded']} ｜ 職缺 {len(st['jobs'])} ｜ 回饋 {st['feedback']}</span>
</header>
<div class="wrap">
  <div class="searchbar">
    <div class="row">
      <input type="text" id="q" placeholder="輸入關鍵字，或切換語意搜尋問「會Revit、待過建設公司、台南機電主任」">
      <button class="go" onclick="doSearch()">搜尋</button>
    </div>
    <div class="row">
      <span class="modetag">
        <label><input type="radio" name="mode" value="keyword" checked><span>關鍵字</span></label>
        <label><input type="radio" name="mode" value="semantic"><span>語意 RAG</span></label>
      </span>
      <select id="job"><option value="">全部職缺</option>{job_opts}</select>
      <input type="text" id="area" placeholder="地區(台南/高雄)" style="width:130px">
      <select id="min_score">
        <option value="0">不限分數</option><option value="80">≥80 優先</option>
        <option value="70">≥70</option><option value="60">≥60</option>
      </select>
      <select id="forwarded">
        <option value="">轉寄不拘</option><option value="yes">已轉寄</option><option value="no">未轉寄</option>
      </select>
    </div>
    <div class="hint">💡 語意搜尋用自然語言描述理想人選；關鍵字搜尋比對姓名/職稱/經歷/評語。點卡片看完整分析與跨職缺媒合。</div>
  </div>
  <div id="results"><div class="empty">輸入條件後按搜尋</div></div>
</div>
<script>
async function doSearch(){{
  const q=document.getElementById('q').value;
  const mode=document.querySelector('input[name=mode]:checked').value;
  const job=document.getElementById('job').value;
  const area=document.getElementById('area').value;
  const min_score=document.getElementById('min_score').value;
  const forwarded=document.getElementById('forwarded').value;
  const box=document.getElementById('results');
  box.innerHTML='<div class="empty">搜尋中…'+(mode==='semantic'?'（語意向量計算）':'')+'</div>';
  const p=new URLSearchParams({{q,mode,job,area,min_score,forwarded}});
  const r=await fetch('/api/search?'+p); const data=await r.json();
  if(!data.length){{box.innerHTML='<div class="empty">沒有符合的人才</div>';return;}}
  box.innerHTML=data.map(c=>{{
    const sc=c.best_score||0;
    const cls=sc>=80?'s80':sc>=70?'s70':sc>=60?'s60':'';
    const sim=c.sim!=null?`<span class="sim">語意契合 ${{(c.sim*100).toFixed(0)}}%</span>`:'';
    const fwd=c.ever_forwarded?'<span class="badge fwd">曾轉寄</span>':'';
    const tags=(c.experiences||[]).slice(0,3).map(e=>`<span>${{esc(String(e).slice(0,40))}}</span>`).join('');
    const jobs=Object.keys(c.appearances||{{}}).join('、');
    return `<div class="card ${{cls}}" onclick="location.href='/candidate?rid=${{c.rid}}'">
      <div class="top"><span class="nm">${{esc(c.name)}} ${{fwd}} ${{sim}}</span>
        <span class="sc">${{sc}}<small style="font-size:.5em;color:#999">分</small></span></div>
      <div class="meta">${{esc(c.age||'')}} ｜ ${{esc(c.residence||'')}} ｜ ${{esc(c.education||'')}}</div>
      <div class="meta">希望職稱：${{esc((c.prefer_title||'').slice(0,60))}}</div>
      <div class="meta" style="color:#999">出現職缺：${{esc(jobs||'—')}}</div>
      <div class="tags">${{tags}}</div></div>`;
  }}).join('');
}}
function esc(s){{return String(s||'').replace(/[&<>"]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]));}}
document.getElementById('q').addEventListener('keydown',e=>{{if(e.key==='Enter')doSearch();}});
doSearch();
</script>
</body></html>"""


def page_candidate(rid):
    c = candidate_detail(rid)
    if not c:
        return '<h1>找不到此人才</h1><a href="/">返回</a>'

    exp_html = ''.join(f'<li>{esc(e)}</li>' for e in c['experiences'])
    hl_html = ''.join(f'<li>{esc(h)}</li>' for h in c['highlights'])

    ja_html = ''
    for j in c['jobs_analysis']:
        rel = j.get('relevance', '')
        relcls = {'高': '#d32f2f', '中': '#f57c00', '低': '#999'}.get(rel, '#999')
        ja_html += f"""<tr>
          <td>{esc(j.get('company',''))}</td><td>{esc(j.get('title',''))}</td>
          <td>{esc(j.get('duration',''))}</td>
          <td style="text-align:center">{esc(j.get('score',''))}</td>
          <td style="text-align:center;color:{relcls};font-weight:bold">{esc(rel)}</td>
          <td>{esc(j.get('summary',''))}</td></tr>"""

    match_html = ''
    for m in c['matches']:
        real = m['real_score']
        aff = m['affinity']
        real_disp = f'<b style="color:var(--sage-d)">{real}分</b>' if real is not None else '<span style="color:var(--brown-l)">未搜尋</span>'
        aff_disp = f'{int(aff*100)}%' if aff is not None else '—'
        fwd = '<span class="badge fwd">已轉寄</span>' if m['forwarded'] else ''
        fb = c['feedback'].get(m['job_id'], {})
        fb_status = fb.get('status', 'pending')
        fb_note = fb.get('note', '')
        fb_assignee = fb.get('assignee', '')
        fb_iw_note = fb.get('interviewer_note', '')

        match_html += f"""<tr>
          <td>{esc(m['display_name'])}</td>
          <td style="text-align:center">{real_disp}</td>
          <td style="text-align:center">{aff_disp}</td>
          <td style="text-align:center;white-space:nowrap">{fwd}</td>
          <td style="text-align:center">
            <select class="fb-status" onchange="triggerSave('{esc(rid)}','{esc(m['job_id'])}',this)">
              <option value="pending" {'selected' if fb_status=='pending' else ''}>—</option>
              <option value="interviewing" {'selected' if fb_status=='interviewing' else ''}>約面試</option>
              <option value="interviewed" {'selected' if fb_status=='interviewed' else ''}>已面試</option>
              <option value="hired" {'selected' if fb_status=='hired' else ''}>✅錄取</option>
              <option value="rejected" {'selected' if fb_status=='rejected' else ''}>❌婉拒</option>
            </select>
          </td>
          <td>
            <input type="text" class="fb-note" value="{esc(fb_note)}" style="width:100%;padding:4px;border:1px solid #ccc;border-radius:4px" placeholder="輸入備註..." onchange="triggerSave('{esc(rid)}','{esc(m['job_id'])}',this)">
          </td>
          <td>
            <input type="text" class="fb-assignee" list="assignee-list" value="{esc(fb_assignee)}" style="width:100px;padding:4px;border:1px solid #ccc;border-radius:4px" placeholder="經辦人" onchange="triggerSave('{esc(rid)}','{esc(m['job_id'])}',this)">
          </td>
          <td>
            <textarea class="fb-interviewer-note" rows="2" style="width:100%;min-width:160px;padding:4px;border:1px solid #ccc;border-radius:4px;resize:vertical;font-size:.88em" placeholder="面試主管評語..." onchange="triggerSave('{esc(rid)}','{esc(m['job_id'])}',this)">{esc(fb_iw_note)}</textarea>
          </td>
        </tr>"""

    assignees = get_all_assignees()
    datalist_options = ''.join(f'<option value="{esc(a)}">' for a in assignees)
    datalist_html = f'<datalist id="assignee-list">{datalist_options}</datalist>'

    detail_link = f'<a href="{esc(c["detail_href"])}" target="_blank">→ 開啟 104 履歷原頁</a>' if c['detail_href'] else ''

    return f"""<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(c['name'])} — 人才詳情</title>
<style>
:root{{
  --cream:#F6F1E9;--parchment:#FDFAF5;--sand:#EDE5D8;
  --sage:#7A9B7E;--sage-d:#5C7A60;--sage-pale:#EAF0EB;
  --brown:#4A3F35;--brown-m:#7A6E65;--brown-l:#B0A49A;
  --stone:#D5CBBE;--charcoal:#1E1A17;
}}
*{{box-sizing:border-box}}
body{{font-family:"PingFang TC",-apple-system,"Helvetica Neue",sans-serif;margin:0;background:var(--cream);color:var(--brown);letter-spacing:.01em}}
header{{background:var(--charcoal);color:#F0EAE0;padding:14px 32px;border-bottom:2px solid var(--sage)}}
header a{{color:#C8D8C9;text-decoration:none;font-size:.9em;letter-spacing:.04em}}
header a:hover{{color:#fff}}
.wrap{{width:80%;margin:0 auto;padding:24px 0}}
.box{{background:var(--parchment);border-radius:2px;padding:20px 24px;margin-bottom:14px;border:1px solid var(--stone)}}
h2{{color:var(--charcoal);border-bottom:1px solid var(--stone);padding-bottom:8px;font-size:1em;font-weight:600;letter-spacing:.07em;text-transform:uppercase;margin-top:0}}
.big{{font-size:1.55em;font-weight:700;color:var(--charcoal)}}
.score{{font-size:2.2em;color:var(--sage-d);font-weight:700}}
table{{width:100%;border-collapse:collapse;font-size:.88em}}
th,td{{border:1px solid var(--stone);padding:8px 10px;text-align:left}}
th{{background:var(--sand);color:var(--brown);font-weight:600;letter-spacing:.04em;font-size:.82em}}
.meta{{color:var(--brown-m);margin:4px 0;line-height:1.6;font-size:.9em}}
ul{{margin:6px 0;padding-left:18px;line-height:1.7}}
.badge.fwd{{background:var(--sage-pale);color:var(--sage-d);font-size:.72em;padding:2px 8px;border-radius:2px;border:1px solid var(--sage);white-space:nowrap}}
select{{padding:5px 8px;border-radius:2px;border:1px solid var(--stone);background:var(--cream);color:var(--brown);font-family:inherit;font-size:.88em}}
select:focus{{outline:none;border-color:var(--sage)}}
input[type=text],textarea{{font-family:inherit;color:var(--brown);background:var(--cream);border:1px solid var(--stone);border-radius:2px}}
input[type=text]:focus,textarea:focus{{outline:none;border-color:var(--sage);box-shadow:0 0 0 2px var(--sage-pale)}}
.print{{float:right;background:var(--sage);color:#fff;border:0;padding:6px 16px;border-radius:2px;cursor:pointer;font-family:inherit;font-size:.85em;letter-spacing:.04em}}
.print:hover{{background:var(--sage-d)}}
@media print{{header,.print,.noprint{{display:none}}body{{background:#fff}}}}
</style></head><body>
<header><a href="/">← 返回搜尋</a></header>
<div class="wrap">
  <div class="box">
    <button class="print noprint" onclick="window.print()">🖨 列印</button>
    <div class="big">{esc(c['name'])} <span style="font-size:.55em;color:#666">{esc(c['age'])} {esc(c['gender'])}</span></div>
    <div class="meta">居住地：{esc(c['residence'])} ｜ 希望工作地：{esc(c['prefer_area'])}</div>
    <div class="meta">學歷：{esc(c['education'])}</div>
    <div class="meta">希望職稱：{esc(c['prefer_title'])}</div>
    <div class="meta">總年資：{esc(c['work_total'])}</div>
    <div class="meta">{detail_link}</div>
  </div>

  <div class="box">
    <h2>🤖 AI 綜合評分</h2>
    <p><span class="score">{c['llm_score'] if c['llm_score'] is not None else '—'}</span>
       <span style="color:#666">/ 100 ｜ {esc(c['level'])}</span></p>
    <p class="meta"><b>評語：</b>{esc(c['reasoning'])}</p>
    {f'<b>亮點：</b><ul>{hl_html}</ul>' if hl_html else ''}
  </div>

  {f'''<div class="box"><h2>🧩 工作經歷逐段分析</h2>
  <table><tr><th>公司</th><th>職稱</th><th>時長</th><th>分數</th><th>相關</th><th>說明</th></tr>
  {ja_html}</table></div>''' if ja_html else ''}

  <div class="box">
    <h2>🎯 跨職缺智能媒合</h2>
    <p class="meta" style="color:#888">實際分數＝曾被搜尋評分；語意契合＝履歷向量 vs 職缺需求向量。可在此編輯狀態、備註說明與備誰（自動儲存）。</p>
    <table><tr><th>職缺</th><th>實際分數</th><th>語意契合</th><th>轉寄</th><th>流程進度狀態</th><th>備註說明</th><th>備誰</th><th>面試主管評語</th></tr>
    {match_html}</table>
  </div>

  {f'''<div class="box"><h2>📄 工作經歷</h2><ul>{exp_html}</ul></div>''' if exp_html else ''}
</div>
<script>
async function triggerSave(rid, jobId, element) {{
  const row = element.closest('tr');
  const status = row.querySelector('.fb-status').value;
  const note = row.querySelector('.fb-note').value;
  const assignee = row.querySelector('.fb-assignee').value;
  const interviewer_note = (row.querySelector('.fb-interviewer-note') || {{}}).value || '';

  const originalBorderColor = element.style.borderColor || element.style.border || '#ccc';
  element.style.borderColor = '#eab308';

  try {{
    const res = await fetch('/api/feedback', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ rid, job_id: jobId, status, note, assignee, interviewer_note }})
    }});
    if (res.ok) {{
      element.style.borderColor = '#22c55e';
      setTimeout(() => {{
        element.style.borderColor = originalBorderColor;
      }}, 1000);
    }} else {{
      element.style.borderColor = '#ef4444';
    }}
  }} catch (err) {{
    element.style.borderColor = '#ef4444';
  }}
}}
</script>
{datalist_html}
</body></html>"""


# ---------- HTTP handler ----------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # 安靜

    def _send(self, body, ctype='text/html; charset=utf-8', code=200):
        data = body.encode('utf-8') if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path == '/':
            self._send(page_search())
        elif u.path == '/candidate':
            rid = qs.get('rid', [''])[0]
            self._send(page_candidate(rid))
        elif u.path == '/api/search':
            res = search(
                q=qs.get('q', [''])[0].strip(),
                mode=qs.get('mode', ['keyword'])[0],
                area=qs.get('area', [''])[0].strip(),
                min_score=int(qs.get('min_score', ['0'])[0] or 0),
                job=qs.get('job', [''])[0],
                forwarded=qs.get('forwarded', [''])[0],
            )
            # 去掉 embedding（太大不傳）
            for c in res:
                c.pop('embedding', None)
            self._send(json.dumps(res, ensure_ascii=False), 'application/json; charset=utf-8')
        else:
            self._send('404', code=404)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == '/api/feedback':
            ln = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(ln) or '{}')
            save_feedback(body.get('rid', ''), body.get('job_id', ''),
                          body.get('status', 'pending'), body.get('note', ''),
                          body.get('assignee', ''),
                          body.get('interviewer_note', ''))
            self._send(json.dumps({'ok': True}), 'application/json')
        else:
            self._send('404', code=404)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8090)
    args = ap.parse_args()
    if not DB_PATH.exists():
        print(f'✗ 找不到 {DB_PATH}，請先執行：python3 talent_kb/build_db.py')
        return
    migrate_db()
    srv = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'🗂️  招募人才知識庫已啟動')
    print(f'    瀏覽器開啟 → http://localhost:{args.port}')
    print(f'    Ctrl+C 停止')
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止')


if __name__ == '__main__':
    main()
