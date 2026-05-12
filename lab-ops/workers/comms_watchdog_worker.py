#!/usr/bin/env python3
import argparse, json, re
from pathlib import Path

def parse_frontmatter(text):
    data={}
    if text.startswith('---'):
        parts=text.split('---',2)
        if len(parts)>=3:
            for line in parts[1].splitlines():
                if ':' in line:
                    k,v=line.split(':',1); data[k.strip()]=v.strip()
    return data

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--inbox-dir', required=True); ap.add_argument('--outbox-dir', required=True); ap.add_argument('--max-pending-minutes', type=int, default=60); ap.add_argument('--output', required=True)
    args=ap.parse_args(); inbox=Path(args.inbox_dir); outbox=Path(args.outbox_dir)
    reqs=[]; alerts=[]
    for p in sorted(inbox.glob('*.md')):
        text=p.read_text(encoding='utf-8'); fm=parse_frontmatter(text); rid=fm.get('id', p.stem)
        needs=fm.get('needs_reply','').lower()=='true'; pending=fm.get('status')=='pending'
        outs=[x.read_text(encoding='utf-8') for x in outbox.glob(f'*{rid}*.md')]
        has_ack=any('ACK' in o or 'ack' in o.lower() for o in outs)
        has_response=any(('response' in o.lower() or 'done' in o.lower()) and 'ACK only' not in o for o in outs)
        item={'request_id':rid,'needs_reply':needs,'status':fm.get('status'),'has_ack':has_ack,'has_response':has_response}
        reqs.append(item)
        if needs and pending and not has_response:
            alerts.append(item)
    payload={'worker':'comms_watchdog','mode':'dry_run','db_write':False,'requests':reqs,'pending_needing_reply':sum(1 for r in reqs if r['needs_reply']),'stale_pending_count':len(alerts),'alerts':alerts}
    Path(args.output).write_text(json.dumps(payload,ensure_ascii=False,indent=2), encoding='utf-8')
    print('COMMS_WATCHDOG', json.dumps({'stale_pending_count': len(alerts)}))
if __name__=='__main__': main()
