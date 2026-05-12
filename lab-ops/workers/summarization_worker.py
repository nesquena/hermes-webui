#!/usr/bin/env python3

def expected_region_from_source(title='', source=''):
    text=f'{title} {source}'
    if any(s in text for s in ['Korea','한국','김천','대경일보']): return 'South Korea'
    if any(s in text for s in ['China','中国','湖南','DoNews']): return 'China'
    if 'Japan' in text or '日本' in text: return 'Japan'
    return None

def process_articles(articles, write=False, summarize_fn=None, update_fn=None, mark_failed_fn=None):
    results=[]; completed=failed=0
    summarize_fn = summarize_fn or (lambda title, content: None)
    update_fn = update_fn or (lambda article_id, summary: None)
    mark_failed_fn = mark_failed_fn or (lambda article_id: None)
    for a in articles:
        summary=summarize_fn(a.get('original_title',''), a.get('extracted_text',''))
        if not summary:
            failed += 1; mark_failed_fn(a.get('id')); results.append({'article_id':a.get('id'),'db_write':False,'status':'failed'}); continue
        expected=expected_region_from_source(a.get('original_title',''), a.get('original_source',''))
        if expected: summary=dict(summary); summary['region']=expected
        completed += 1
        if write: update_fn(a.get('id'), summary)
        results.append({'article_id':a.get('id'),'db_write':bool(write),'status':'completed'})
    return {'attempted': len(articles), 'completed': completed, 'failed': failed, 'results': results}
