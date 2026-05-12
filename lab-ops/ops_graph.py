#!/usr/bin/env python3
import argparse, json
from pathlib import Path

def build_graph(root=None):
    nodes=[
        {'id':'cron:legal-research-pipeline','type':'cron'}, {'id':'worker:source_enrichment','type':'worker'},
        {'id':'worker:news_health','type':'worker'}, {'id':'worker:summarization','type':'worker'},
        {'id':'db:research.articles','type':'database'}, {'id':'queue:ready_for_summary','type':'queue'},
        {'id':'queue:enrichment_needed','type':'queue'}, {'id':'artifact:ops_graph.json','type':'artifact'},
        {'id':'handoff:jarvis_taeyoon','type':'handoff'}, {'id':'bottleneck:source_enrichment','type':'bottleneck'},
    ]
    edges=[
        {'source':'cron:legal-research-pipeline','target':'worker:news_health','type':'triggers'},
        {'source':'worker:source_enrichment','target':'queue:ready_for_summary','type':'creates'},
        {'source':'queue:enrichment_needed','target':'bottleneck:source_enrichment','type':'blocked_by'},
        {'source':'worker:news_health','target':'db:research.articles','type':'verifies'},
    ]
    return {'graph_type':'labops_operational_kg','summary':{'top_bottleneck_id':'bottleneck:source_enrichment'},'nodes':nodes,'edges':edges}

def render_md(graph):
    return '# LabOps Operational Graph\n\n## ข่าวเข้าจากไหน\ncron:legal-research-pipeline triggers workers.\n\n## ติดตรงไหน\nbottleneck:source_enrichment\n'

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--json-output', required=True); ap.add_argument('--md-output', required=True)
    args=ap.parse_args(); g=build_graph()
    Path(args.json_output).write_text(json.dumps(g,ensure_ascii=False,indent=2), encoding='utf-8')
    Path(args.md_output).write_text(render_md(g), encoding='utf-8')
    print('OPS_GRAPH', json.dumps({'ok': True}))
if __name__=='__main__': main()
