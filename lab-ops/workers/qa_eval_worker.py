def expected_region(title='', source=''):
    text=f'{title} {source}'
    if any(s in text for s in ['湖南','China','DoNews']): return 'China'
    if any(s in text for s in ['Korea','김천','대경일보']): return 'South Korea'
    return None

def evaluate_rows(rows):
    items=[]; fail=0; passed=0
    for row in rows:
        issues=[]; exp=expected_region(row.get('original_title',''), row.get('original_source',''))
        if exp and row.get('region') != exp:
            issues.append(f'region_mismatch_expected_{exp.lower().replace(" ","_")}')
        if issues: fail += 1
        else: passed += 1
        items.append({'id': row.get('id'), 'issues': issues})
    return {'worker':'qa_eval','sample_size':len(rows),'fail_count':fail,'pass_count':passed,'status':'fail' if fail else 'pass','items':items,'db_write':False}
