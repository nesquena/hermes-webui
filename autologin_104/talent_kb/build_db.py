#!/usr/bin/env python3
"""
build_db.py — 把分散在 results/<job>/ 的招募資料彙整成單一 SQLite 知識庫 talent.db

資料來源：
  - results/<job>/resume_cache.json   履歷正文 + LLM 評分（含 jobs[] 逐段分析）
  - results/<job>/candidates_raw.json  基本資料（姓名/年齡/地區/學歷/職稱/經歷）
  - results/<job>/forward_log.json     轉寄歷史
  - jobs/<id>.json                     職缺條件

產出 talent.db 內含：
  candidates   去重後的人才主檔（含 384 維語意向量）
  appearances  一個人出現在哪些職缺、各得幾分、是否轉寄
  jobs         職缺主檔（含職缺需求的語意向量，供跨職缺媒合）
  feedback     HR 決策回饋（面試/錄取/婉拒）

全部用 Python 標準庫，無需 pip install。
embedding 透過 Ollama all-minilm（384 維）。

用法：
  python3 talent_kb/build_db.py            # 增量更新（body 沒變就沿用舊向量）
  python3 talent_kb/build_db.py --reembed  # 強制重算所有向量
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import sqlite3
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # autologin_104/
RESULTS_DIR = ROOT / 'results'
JOBS_DIR = ROOT / 'jobs'
DB_PATH = Path(__file__).resolve().parent / 'talent.db'

OLLAMA_EMBED_URL = 'http://localhost:11434/api/embeddings'
EMBED_MODEL = 'bge-m3'   # 中文語意檢索頂尖，1024 維，context 8192


# ---------- embedding ----------
# bge-m3 context 8192 token，整段履歷可一次 embed（仍保留切段機制當保險）
CHUNK_CHARS = 2000


def _embed_one(text: str) -> list[float] | None:
    try:
        req = urllib.request.Request(
            OLLAMA_EMBED_URL,
            data=json.dumps({'model': EMBED_MODEL, 'prompt': text}).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
        )
        r = json.loads(urllib.request.urlopen(req, timeout=30).read())
        v = r.get('embedding')
        return v if v else None
    except Exception:
        return None


def embed(text: str) -> list[float] | None:
    """切成 ≤140 字小段各自 embed，再平均池化成單一 384 維向量。失敗回 None。"""
    text = (text or '').strip()
    if not text:
        return None
    # 依行切，再把過長的行硬切成 CHUNK_CHARS
    raw_lines = [ln.strip() for ln in text.split('\n') if ln.strip()]
    chunks = []
    for ln in raw_lines:
        for i in range(0, len(ln), CHUNK_CHARS):
            chunks.append(ln[i:i + CHUNK_CHARS])
    chunks = chunks[:30]  # 上限 30 段，避免極端長履歷拖慢
    if not chunks:
        return None
    vecs = []
    for ch in chunks:
        v = _embed_one(ch)
        if v:
            vecs.append(v)
    if not vecs:
        print('  ⚠️ embedding 失敗（所有段落皆無回應）')
        return None
    # mean-pool
    dim = len(vecs[0])
    mean = [sum(v[i] for v in vecs) / len(vecs) for i in range(dim)]
    # L2 正規化（方便之後直接用 dot product 當 cosine）
    norm = sum(x * x for x in mean) ** 0.5 or 1.0
    return [x / norm for x in mean]


def body_hash(text: str) -> str:
    return hashlib.sha256((text or '').encode('utf-8')).hexdigest()[:16]


# ---------- schema ----------
def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS candidates (
        rid           TEXT PRIMARY KEY,
        name          TEXT,
        age           TEXT,
        gender        TEXT,
        residence     TEXT,
        prefer_area   TEXT,
        education     TEXT,
        prefer_title  TEXT,
        work_total    TEXT,
        experiences   TEXT,   -- JSON array of strings
        body          TEXT,
        body_hash     TEXT,
        llm_score     INTEGER,
        level         TEXT,
        reasoning     TEXT,
        highlights    TEXT,   -- JSON array
        jobs_analysis TEXT,   -- JSON array (逐段工作分析)
        embedding     TEXT,   -- JSON array (384 dim)
        detail_href   TEXT,
        first_seen    TEXT,
        last_seen     TEXT
    );
    CREATE TABLE IF NOT EXISTS appearances (
        rid       TEXT,
        job_id    TEXT,
        score     INTEGER,
        forwarded INTEGER DEFAULT 0,
        run_at    TEXT,
        PRIMARY KEY (rid, job_id)
    );
    CREATE TABLE IF NOT EXISTS jobs (
        job_id       TEXT PRIMARY KEY,
        display_name TEXT,
        req_text     TEXT,
        embedding    TEXT
    );
    CREATE TABLE IF NOT EXISTS feedback (
        rid        TEXT,
        job_id     TEXT,
        status     TEXT,    -- pending / interviewing / interviewed / hired / rejected
        note       TEXT,
        assignee         TEXT,    -- 備誰
        interviewer_note TEXT,   -- 面試主管評語
        updated_at TEXT,
        PRIMARY KEY (rid, job_id)
    );
    """)
    for col, ddl in [
        ('assignee', 'TEXT'),
        ('interviewer_note', 'TEXT'),
    ]:
        try:
            conn.execute(f"ALTER TABLE feedback ADD COLUMN {col} {ddl}")
        except sqlite3.OperationalError:
            pass
    conn.commit()


# ---------- 載入來源資料 ----------
def load_all_sources() -> tuple[dict, dict, dict]:
    """回傳 (candidates_basic, resume_scores, forward_status)。"""
    basic = {}        # rid -> {name, age, ... , experiences[], detail_href, jobs_seen[]}
    scores = {}       # rid -> {body, llm_score dict, cached_at}
    appearances = {}  # (rid, job_id) -> {score, forwarded, run_at}

    # candidates_raw：基本資料 + 出現在哪個 job
    for f in sorted(glob.glob(str(RESULTS_DIR / '*' / 'candidates_raw.json'))):
        job_id = Path(f).parent.name
        if job_id.startswith('_'):
            continue
        try:
            d = json.loads(Path(f).read_text())
        except Exception:
            continue
        for c in d.get('candidates', []):
            rid = str(c.get('resumeId', '')).strip()
            if not rid:
                continue
            rec = basic.setdefault(rid, {'jobs_seen': set()})
            rec['jobs_seen'].add(job_id)
            # 用最豐富的一份基本資料（後蓋前，但只在欄位非空時）
            for k in ('name', 'age', 'gender', 'residence', 'preferArea',
                      'education', 'preferJobTitle', 'workExpTotal', 'detailHref'):
                val = (c.get(k) or '').strip() if isinstance(c.get(k), str) else c.get(k)
                if val:
                    rec[k] = val
            if c.get('experiences'):
                rec['experiences'] = c['experiences']

    # resume_cache：履歷正文 + LLM 評分
    for f in sorted(glob.glob(str(RESULTS_DIR / '*' / 'resume_cache.json'))):
        job_id = Path(f).parent.name
        if job_id.startswith('_'):
            continue
        try:
            d = json.loads(Path(f).read_text())
        except Exception:
            continue
        for rid, v in d.items():
            rid = str(rid).strip()
            cur = scores.get(rid)
            # 取有 llm_score 且 body 較長的版本
            if cur is None or (len(v.get('body', '')) > len(cur.get('body', ''))):
                scores[rid] = v
            if v.get('name') and rid in basic and not basic[rid].get('name'):
                basic[rid]['name'] = v['name']

    # forward_log：各職缺得分 + 轉寄狀態
    for f in sorted(glob.glob(str(RESULTS_DIR / '*' / 'forward_log.json'))):
        job_id = Path(f).parent.name
        if job_id.startswith('_'):
            continue
        try:
            d = json.loads(Path(f).read_text())
        except Exception:
            continue
        fwd_ids = set(str(x) for x in d.get('forwarded_ids', []))
        # 從 runs 取每人最近一次分數
        for run in d.get('runs', []):
            run_at = run.get('run_at', '')
            for r in run.get('results', []):
                rid = str(r.get('resumeId', '')).strip()
                if not rid:
                    continue
                key = (rid, job_id)
                appearances[key] = {
                    'score': r.get('score', 0),
                    'forwarded': 1 if (r.get('forwarded') or rid in fwd_ids) else 0,
                    'run_at': run_at,
                }
        # 沒在 runs 但在 forwarded_ids 的，也補一筆
        for rid in fwd_ids:
            key = (rid, job_id)
            if key not in appearances:
                appearances[key] = {'score': 0, 'forwarded': 1, 'run_at': ''}

    return basic, scores, appearances


def build_embed_text(basic: dict, score: dict) -> str:
    """組合用於語意檢索的文字表示。"""
    parts = []
    if basic.get('name'):
        parts.append(f"姓名:{basic['name']}")
    if basic.get('preferJobTitle'):
        parts.append(f"希望職稱:{basic['preferJobTitle']}")
    if basic.get('education'):
        parts.append(f"學歷:{basic['education']}")
    if basic.get('residence'):
        parts.append(f"居住地:{basic['residence']}")
    if basic.get('preferArea'):
        parts.append(f"希望工作地:{basic['preferArea']}")
    for exp in (basic.get('experiences') or [])[:8]:
        parts.append(str(exp))
    if score:
        ls = score.get('llm_score') or {}
        if ls.get('reasoning'):
            parts.append(ls['reasoning'])
        for h in (ls.get('highlights') or []):
            parts.append(h)
    # 自傳節錄
    body = (score or {}).get('body', '')
    if '自傳' in body:
        i = body.index('自傳')
        parts.append(body[i:i + 300])
    return '\n'.join(parts)


def job_req_text(prof: dict) -> str:
    s = prof.get('search', {})
    parts = [
        prof.get('display_name', ''),
        '關鍵字:' + s.get('keyword', ''),
        '職類:' + '、'.join(s.get('job_categories', [])),
        '工作地:' + '、'.join(s.get('work_locations', [])),
        '科系:' + '、'.join(s.get('majors', [])),
        '工具:' + '、'.join(s.get('tools', [])),
        '證照:' + '、'.join(s.get('certificates', [])),
        f"年齡:{s.get('age_min','')}-{s.get('age_max','')}",
        f"年資:{s.get('work_exp_years','')}年以上",
    ]
    return '\n'.join(p for p in parts if p)


# ---------- 主流程 ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--reembed', action='store_true', help='強制重算所有向量')
    args = ap.parse_args()

    print(f'📂 來源: {RESULTS_DIR}')
    print(f'💾 輸出: {DB_PATH}')
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    # 既有向量（增量用）
    old_vecs = {}
    for rid, bh, emb in conn.execute('SELECT rid, body_hash, embedding FROM candidates'):
        if emb:
            old_vecs[rid] = (bh, emb)

    basic, scores, appearances = load_all_sources()
    all_rids = set(basic) | set(scores)
    print(f'\n發現 {len(all_rids)} 位不重複人才，開始彙整...')

    now = datetime.now().isoformat()
    embedded = reused = 0
    for i, rid in enumerate(sorted(all_rids), 1):
        b = basic.get(rid, {})
        sc = scores.get(rid, {})
        ls = sc.get('llm_score') or {}
        body = sc.get('body', '')
        bh = body_hash(body)

        # embedding：body 沒變就沿用
        emb_json = None
        if not args.reembed and rid in old_vecs and old_vecs[rid][0] == bh:
            emb_json = old_vecs[rid][1]
            reused += 1
        else:
            etext = build_embed_text(b, sc)
            vec = embed(etext)
            if vec:
                emb_json = json.dumps(vec)
                embedded += 1

        conn.execute("""
            INSERT OR REPLACE INTO candidates
            (rid,name,age,gender,residence,prefer_area,education,prefer_title,
             work_total,experiences,body,body_hash,llm_score,level,reasoning,
             highlights,jobs_analysis,embedding,detail_href,first_seen,last_seen)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                    COALESCE((SELECT first_seen FROM candidates WHERE rid=?), ?), ?)
        """, (
            rid, b.get('name') or sc.get('name', ''), str(b.get('age', '')), b.get('gender', ''),
            b.get('residence', ''), b.get('preferArea', ''), b.get('education', ''),
            b.get('preferJobTitle', ''), str(b.get('workExpTotal', '')),
            json.dumps(b.get('experiences', []), ensure_ascii=False),
            body, bh,
            ls.get('score'), ls.get('level_assessment', ''), ls.get('reasoning', ''),
            json.dumps(ls.get('highlights', []), ensure_ascii=False),
            json.dumps(ls.get('jobs', []), ensure_ascii=False),
            emb_json, b.get('detailHref', ''),
            rid, now, now,
        ))
        if i % 20 == 0:
            print(f'  ...{i}/{len(all_rids)}')

    # appearances
    conn.execute('DELETE FROM appearances')
    for (rid, job_id), v in appearances.items():
        conn.execute(
            'INSERT OR REPLACE INTO appearances (rid,job_id,score,forwarded,run_at) VALUES (?,?,?,?,?)',
            (rid, job_id, v['score'], v['forwarded'], v['run_at']))

    # jobs + 職缺需求向量
    job_n = 0
    for jf in sorted(JOBS_DIR.glob('*.json')):
        try:
            prof = json.loads(jf.read_text())
        except Exception:
            continue
        jid = prof.get('job_id', jf.stem)
        rt = job_req_text(prof)
        old = conn.execute('SELECT req_text, embedding FROM jobs WHERE job_id=?', (jid,)).fetchone()
        if old and old[0] == rt and old[1] and not args.reembed:
            emb_json = old[1]
        else:
            v = embed(rt)
            emb_json = json.dumps(v) if v else None
        conn.execute('INSERT OR REPLACE INTO jobs (job_id,display_name,req_text,embedding) VALUES (?,?,?,?)',
                     (jid, prof.get('display_name', jid), rt, emb_json))
        job_n += 1

    conn.commit()

    # 統計
    n_cand = conn.execute('SELECT COUNT(*) FROM candidates').fetchone()[0]
    n_scored = conn.execute('SELECT COUNT(*) FROM candidates WHERE llm_score IS NOT NULL').fetchone()[0]
    n_emb = conn.execute("SELECT COUNT(*) FROM candidates WHERE embedding IS NOT NULL").fetchone()[0]
    n_app = conn.execute('SELECT COUNT(*) FROM appearances').fetchone()[0]
    n_fwd = conn.execute('SELECT COUNT(*) FROM appearances WHERE forwarded=1').fetchone()[0]
    conn.close()

    print(f'\n✓ 完成')
    print(f'  人才: {n_cand}（有 LLM 評分 {n_scored}，有向量 {n_emb}）')
    print(f'  向量: 新算 {embedded}、沿用 {reused}')
    print(f'  職缺: {job_n}')
    print(f'  出現記錄: {n_app}（已轉寄 {n_fwd}）')
    print(f'  → {DB_PATH}')


if __name__ == '__main__':
    main()
