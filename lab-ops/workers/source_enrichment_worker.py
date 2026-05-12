#!/usr/bin/env python3
import argparse, json, re, sqlite3
from html.parser import HTMLParser
from pathlib import Path

class ArticleTextParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.skip=0; self.parts=[]
    def handle_starttag(self, tag, attrs):
        if tag in {'script','style','nav','footer','header'}: self.skip += 1
    def handle_endtag(self, tag):
        if tag in {'script','style','nav','footer','header'} and self.skip: self.skip -= 1
    def handle_data(self, data):
        if not self.skip:
            text=data.strip()
            if text: self.parts.append(text)

def extract_article_text_from_html(html: str) -> str:
    p=ArticleTextParser(); p.feed(html or '')
    return re.sub(r'\s+', ' ', ' '.join(p.parts)).strip()

def decode_response_body(raw: bytes, declared_charset: str|None=None) -> str:
    encs=[]
    if declared_charset: encs.append(declared_charset)
    encs += ['utf-8','shift_jis','cp932','euc_jp','latin-1']
    best=''
    for enc in encs:
        try:
            text=raw.decode(enc)
            if '�' not in text: return text
            if not best: best=text
        except Exception: pass
    return best or raw.decode('utf-8','replace')

def enrichment_status_for_text(text: str) -> str:
    n=len(text or '')
    if n >= 600: return 'full_text_candidate'
    if n >= 120: return 'short_text'
    if n > 0: return 'rss_summary'
    return 'unknown_short'

def confidence_for_enriched_text(text: str) -> str:
    n=len(text or '')
    if n >= 1200: return 'high'
    if n >= 160: return 'medium'
    return 'low'

def select_candidates(limit: int, db_path: Path|str='research.db') -> list[dict]:
    db=Path(db_path)
    if not db.exists(): return []
    conn=sqlite3.connect(db); conn.row_factory=sqlite3.Row
    try:
        rows=conn.execute('SELECT * FROM articles WHERE COALESCE(llm_status,\'pending\') != \'completed\' ORDER BY COALESCE(selection_score,0) DESC, COALESCE(published_date,fetched_at,\'\') DESC LIMIT ?', (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally: conn.close()

def write_full_text_candidates(packets: list[dict], db_path: Path|str='research.db', backup_dir: Path|str='.') -> dict:
    db=Path(db_path); backup_dir=Path(backup_dir); backup_dir.mkdir(parents=True, exist_ok=True)
    backup=backup_dir/'source_enrichment_backup.json'
    backup.write_text(json.dumps(packets,ensure_ascii=False), encoding='utf-8')
    updated=skipped=0
    conn=sqlite3.connect(db)
    try:
        for p in packets:
            if p.get('source_status')!='full_text_candidate': skipped+=1; continue
            conn.execute('UPDATE articles SET extracted_text=?, content_extraction_status=?, extraction_method=?, citation_canonical_url=? WHERE id=?', (p.get('extracted_text',''), 'full_content', 'full_content', p.get('canonical_url'), p.get('article_id')))
            updated+=1
        conn.commit()
    finally: conn.close()
    return {"db_write": True, "updated_count": updated, "skipped_count": skipped, "backup_path": str(backup)}

def packet_for_row(row: dict) -> dict:
    text=row.get('extracted_text') or ''
    status=enrichment_status_for_text(text)
    return {"article_id": row.get('id'), "source_status": status, "confidence": confidence_for_enriched_text(text), "recommended_next_action": "fetch_full_text" if status!='full_text_candidate' else "manual_review", "db_write": False}

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--dry-run', action='store_true'); ap.add_argument('--limit', type=int, default=10); ap.add_argument('--output', required=True); ap.add_argument('--queue-output')
    args=ap.parse_args(); rows=select_candidates(args.limit); packets=[packet_for_row(r) for r in rows]
    payload={"worker":"source_enrichment","mode":"dry_run" if args.dry_run else "run","db_write":False,"candidate_count":len(packets)}
    Path(args.output).write_text(json.dumps(payload,ensure_ascii=False,indent=2), encoding='utf-8')
    if args.queue_output:
        Path(args.queue_output).write_text('\n'.join(json.dumps(p,ensure_ascii=False) for p in packets)+('\n' if packets else ''), encoding='utf-8')
    print('SOURCE_ENRICHMENT', json.dumps({"ok": True, "candidate_count": len(packets)}))
if __name__=='__main__': main()
