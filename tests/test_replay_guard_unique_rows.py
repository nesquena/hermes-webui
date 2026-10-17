"""The save-time replay guard fingerprints a row only when its identity repeats."""
import copy

import api.models as models


def test_unique_identities_skip_canonicalization(monkeypatch):
    rows = [{'id': f'event-{i}', 'timestamp': i, 'content': 'x'} for i in range(50)]
    canonical = []
    real_dumps = models.json.dumps

    def dumps(*args, **kwargs):
        if kwargs.get('sort_keys'):
            canonical.append(1)
        return real_dumps(*args, **kwargs)

    monkeypatch.setattr(models.json, 'dumps', dumps)
    assert models._deduplicate_exact_stable_messages(rows) == (rows, 0)
    assert canonical == []
    # An exact replay still fingerprints the first row and its copy, then drops the copy.
    assert models._deduplicate_exact_stable_messages(rows + [copy.deepcopy(rows[3])]) == (rows, 1)
    assert len(canonical) == 2
