#!/usr/bin/env python3
import argparse, json

def main():
    p=argparse.ArgumentParser(); p.add_argument('--dry-run', action='store_true'); p.add_argument('--limit', type=int, default=5); p.add_argument('--output', required=True)
    args=p.parse_args()
    payload={"worker":"impact_brief","mode":"dry_run" if args.dry_run else "run","db_write":False,"candidates":[]}
    open(args.output,'w',encoding='utf-8').write(json.dumps(payload,ensure_ascii=False,indent=2))
    print('IMPACT_BRIEF', json.dumps({"ok": True, "limit": args.limit}))
if __name__=='__main__': main()
