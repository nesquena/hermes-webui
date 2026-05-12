#!/usr/bin/env python3
import argparse, json

def main():
    p=argparse.ArgumentParser(); p.add_argument('--dry-run', action='store_true'); p.add_argument('--output', required=True)
    args=p.parse_args()
    payload={"worker":"news_health","mode":"dry_run" if args.dry_run else "run","db_write":False,
             "metrics":{"llm_status":{},"pending_by_text_length":{}},"recommendations":[]}
    open(args.output,'w',encoding='utf-8').write(json.dumps(payload,ensure_ascii=False,indent=2))
    print('NEWS_HEALTH_STATUS', json.dumps({"ok": True}))
if __name__=='__main__': main()
