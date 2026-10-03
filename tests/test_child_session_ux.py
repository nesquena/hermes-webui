"""Production child-chip labels and blocking classes, including locale parity."""
import json
import subprocess

import pytest

from tests._sidebar_child_status_helpers import FAKE_DOM, ROOT, component_script
from tests.test_child_session_status import NODE, session

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def render(state, reference=False):
    parent = session("parent")
    child = session("child", state, parent_session_id="parent",
                    relationship_type="child_session", archived=reference)
    script = FAKE_DOM + component_script() + "\n" + (ROOT / "static/i18n.js").read_text() + f"""
const locales=Object.keys(LOCALES);
const results=locales.map(locale=>{{
  setLocale(locale);
  const raw={json.dumps([parent] if reference else [parent, child])};
  const refs={json.dumps([parent, child])};
  const result=renderFixture(raw,refs,false,'parent');
  const chip=flatten(result.element).find(e=>(e.className||'').split(' ').includes('session-child-count'));
  return {{locale,chip,state:t('session_attention_{state}_title'),
    hint:t('session_child_toggle_hint',t('session_meta_children',1)),
    archived:LOCALES[locale].session_child_archived}};
}});
console.log(JSON.stringify(results));
"""
    # The real locale loader needs only these additional DOM/storage surfaces.
    script = script.replace("const document={createElement:tag=>new Element(tag)};",
                            "const localStorage={getItem(){return 'en';},setItem(){}};"
                            "const document={documentElement:{},createElement:tag=>new Element(tag)};")
    assert NODE is not None
    result = subprocess.run([NODE], input=script, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("state", ["approval", "clarify"])
def test_blocking_chip_state_first_label_and_tint_class_in_every_locale(state):
    for row in render(state):
        chip = row["chip"]
        assert f"is-attention-{state}" in chip["className"].split(), row["locale"]
        expected = f'{row["state"]} · {row["hint"]}'
        assert chip["title"] == expected, row["locale"]
        assert chip["attributes"]["aria-label"] == expected
        assert expected.count(" · ") == 1 and " — " not in expected


@pytest.mark.parametrize("state", ["approval", "clarify", "streaming", "unread"])
def test_reference_only_chip_localized_static_archived_label(state):
    for row in render(state, reference=True):
        chip = row["chip"]
        assert row.get("archived"), row["locale"]
        label, indicator = chip["children"]
        assert label["className"] == "session-child-count-label"
        assert label["textContent"] == row["archived"]
        assert "session-child-count-state" in indicator["className"].split()
        assert chip["title"].endswith(" · " + row["archived"])
        assert "role" not in chip["attributes"] and "tabindex" not in chip["attributes"]
        assert "click" not in chip["title"].lower()


@pytest.mark.parametrize("state", ["idle", "streaming", "unread", "generic"])
def test_nonblocking_chip_does_not_get_blocking_tint(state):
    for row in render(state):
        assert not any(c in row["chip"]["className"].split()
                       for c in ("is-attention-approval", "is-attention-clarify"))
