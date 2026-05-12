#!/usr/bin/env python3
import argparse, datetime
from pathlib import Path

def main():
    p=argparse.ArgumentParser(); p.add_argument('--dry-run', action='store_true'); p.add_argument('--inbox-dir', required=True); p.add_argument('--symptom', required=True); p.add_argument('--evidence', required=True)
    args=p.parse_args(); out=Path(args.inbox_dir); out.mkdir(parents=True, exist_ok=True)
    path=out/(datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')+'-taeyoon-improvement.md')
    path.write_text(f"---\nid: taeyoon-improvement\nfrom: yuto\nto: jarvis-dev-qa\ntype: request\nstatus: pending\napproved_by: kei\n---\nSymptom: {args.symptom}\nEvidence: {args.evidence}\n", encoding='utf-8')
    print('TAEYOON_TASK', path)
if __name__=='__main__': main()
