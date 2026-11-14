"""Every guide feature has inert, grounded tour metadata; visibility stays runtime-owned."""
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'static/help-feature-targets.js'
CATALOG = json.loads((ROOT / 'static/help-features.js').read_text().split(' = ', 1)[1].strip().removesuffix(';'))
TARGETS = json.loads(SCRIPT.read_text().split('  const entries = ', 1)[1].split(';\n  for ', 1)[0])
# Exclude the documentation itself: citing a made-up selector in the guide must
# not make a source-grounding assertion pass.
SOURCES = [(ROOT / 'static/index.html').read_text()] + [
    path.read_text() for path in (ROOT / 'static').glob('*.js')
    if not path.name.startswith(('help-', 'module-content'))
]
SOURCE = '\n'.join(SOURCES)


class Markup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.sections = set()
        self.panels = set()
        self.ancestors = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.stack.append((tag, attrs))
        if attrs.get('data-settings-section'):
            self.sections.add(attrs['data-settings-section'])
        if 'panel-view' in attrs.get('class', '').split() and attrs.get('id', '').startswith('panel'):
            self.panels.add(attrs['id'][5:].lower())
        if attrs.get('data-panel'):
            self.panels.add(attrs['data-panel'])
        if attrs.get('id'):
            section = next((a['id'][12:].lower() for _, a in reversed(self.stack)
                            if a.get('id', '').startswith('settingsPane')), None)
            if section:
                self.ancestors[attrs['id']] = section
        if tag in ('meta', 'link', 'input', 'img', 'br', 'hr', 'source', 'wbr'):
            self.stack.pop()

    def handle_endtag(self, tag):
        for n in range(len(self.stack) - 1, -1, -1):
            if self.stack[n][0] == tag:
                self.stack = self.stack[:n]
                break


HTML = Markup()
HTML.feed(SOURCES[0])


def test_every_feature_is_mapped_once_with_existing_navigation_names():
    assert set(TARGETS) == {feature['id'] for feature in CATALOG['features']}
    assert len(TARGETS) == len(CATALOG['features'])
    for feature_id, entry in TARGETS.items():
        assert {'panel', 'targets'} <= set(entry) <= {'panel', 'section', 'targets', 'entryTargets', 'entryHint'}, feature_id
        assert ('entryTargets' in entry) == ('entryHint' in entry), feature_id
        if 'entryTargets' in entry:
            assert entry['entryTargets'] and isinstance(entry['entryTargets'], list)
            assert set(entry['entryHint']) == {'nl', 'en'}
            assert all(isinstance(value, str) and value.strip() for value in entry['entryHint'].values())
        assert entry['panel'] in HTML.panels, feature_id
        assert isinstance(entry['targets'], list)
        assert len(entry['targets']) == len(set(entry['targets'])), feature_id
        if 'section' in entry:
            assert entry['panel'] == 'settings'
            assert entry['section'] in HTML.sections
        for selector in entry['targets'] + entry.get('entryTargets', []):
            assert isinstance(selector, str) and selector
            assert selector.startswith(('#', '.', '['))
            assert not re.search(r'(?:https?:|javascript:|onclick|onchange|nth-child|nth-of-type)', selector)
            # IDs must appear in actual application source, never only in docs.
            for element_id in re.findall(r'#([a-zA-Z][\w-]*)', selector):
                assert re.search(r'(?<![\w-])' + re.escape(element_id) + r'(?![\w-])', SOURCE), (feature_id, selector)
            for css_class in re.findall(r'\.([a-zA-Z][\w-]*)', selector):
                assert css_class in SOURCE, (feature_id, selector)
            for action in re.findall(r"\[data-help-action='([^']+)'\]", selector):
                assert 'data-help-action="' + action + '"' in SOURCE, (feature_id, selector)
            for tour in re.findall(r"\[data-tour='([^']+)'\]", selector):
                # A JS-created control has the same DOM attribute as an HTML
                # data-tour. Require a literal assignment, not a mention of the target.
                assert ('data-tour="' + tour + '"' in SOURCE or
                        re.search(r'''\.dataset\.tour\s*=\s*(['"])''' + re.escape(tour) + r'\1', SOURCE)), (feature_id, selector)


def test_static_settings_controls_use_their_real_pane():
    for feature_id, entry in TARGETS.items():
        for selector in entry['targets'] + entry.get('entryTargets', []):
            if not re.fullmatch(r'#[\w-]+', selector):
                continue
            actual = HTML.ancestors.get(selector[1:])
            if actual:
                assert entry['panel'] == 'settings', feature_id
                assert entry['section'] == actual, (feature_id, actual)


def test_chat_includes_composer_bot_voice_workspace_and_approval_controls():
    for feature in ('chat.send', 'chat.model', 'chat.reasoning', 'chat.people', 'chat.tools',
                    'chat.context', 'chat.attach', 'chat.prompts', 'chat.mentions',
                    'voice.dictation', 'voice.loop', 'voice.realtime', 'bots.select',
                    'files.workspace', 'files.browse', 'sessions.new', 'sessions.search',
                    'sessions.split', 'governance.once', 'governance.clarify'):
        row = TARGETS['webui.' + feature]
        assert row['panel'] == 'chat'
        assert row['targets'], feature
    assert TARGETS['webui.sessions.json']['section'] == 'conversation'
    assert TARGETS['webui.settings.theme']['section'] == 'appearance'
    assert TARGETS['webui.connections.oauth']['section'] == 'providers'


def test_planned_and_opaque_views_do_not_claim_visible_controls():
    for feature in ('planned.gateway', 'planned.reporter', 'planned.central_memory',
                    'memory.graph', 'memory.triples', 'memory.facts', 'memory.timeline',
                    'memory.overview', 'memory.recall', 'memory.preferences'):
        assert TARGETS['webui.' + feature]['targets'] == []
    assert TARGETS['webui.chat.edit']['targets'] == ["[data-help-action='edit-message']", '.msg-edit-area']
    assert '#builderTab' not in {s for row in TARGETS.values() for s in row['targets']}
    # The group itself is an entry point, not a fabricated exact menu action.
    assert TARGETS['webui.sessions.organize']['targets'] == ['.session-actions-trigger']
    assert TARGETS['webui.sessions.delete']['targets'] == []


def test_browser_export_is_deeply_frozen_and_has_no_executable_values():
    script = """
const fs=require('node:fs'),vm=require('node:vm');
const context={window:{}};vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);
const value=context.window.SynthPulseFeatureTargets;
if(!Object.isFrozen(value))throw Error('mutable export');
for(const entry of Object.values(value)){
 if(!Object.isFrozen(entry)||!Object.isFrozen(entry.targets))throw Error('mutable entry');
 for(const [key,item] of Object.entries(entry)){
  if(key==='targets'||key==='entryTargets'){if(!Object.isFrozen(item)||!item.every(s=>typeof s==='string'))throw Error('non-string target');}
  else if(key==='entryHint'){if(!Object.isFrozen(item)||!Object.values(item).every(s=>typeof s==='string'))throw Error('mutable or invalid hint');}
  else if(typeof item!=='string')throw Error('executable value');
 }
}
console.log(JSON.stringify(value));
"""
    result = subprocess.run(['node', '-e', script, str(SCRIPT)], check=True, capture_output=True, text=True)
    assert json.loads(result.stdout) == TARGETS


def test_closed_drawers_forms_and_responsive_controls_have_honest_entry_hints():
    expected = {
        'chat.model': '#composerMobileConfigBtn',
        'chat.reasoning': '#composerMobileConfigBtn',
        'files.workspace': '#composerMobileConfigBtn',
        'sessions.search': '#btnHamburger',
        'sessions.delete': '.session-actions-trigger',
        'tasks.prompt': "[data-tour='tasks-new']",
        'tasks.notify': "[data-tour='tasks-new']",
        'tasks.script': '.cron-item:has(.cron-script-badge)',
        'tasks.results': '.cron-item:not(.readonly)',
        'tasks.run': '.cron-item:not(.readonly)',
        'files.create': '#btnWorkspacePanelEdgeToggle',
        'files.hidden': '#btnWorkspacePrefs',
        'bots.instructions': "[data-builder-tab='1']",
    }
    for feature, selector in expected.items():
        row = TARGETS['webui.' + feature]
        assert selector in row.get('entryTargets', []), feature
        assert selector not in row['targets'], feature
        assert row['entryHint']['nl'] and row['entryHint']['en']
    for feature in ('planned.gateway', 'planned.reporter', 'planned.central_memory', 'governance.once'):
        assert 'entryTargets' not in TARGETS['webui.' + feature]


def test_task_read_views_are_not_replaced_by_unrelated_creation_or_refresh_actions():
    assert "[data-tour='tasks-new']" not in TARGETS['webui.tasks.script']['entryTargets']
    assert '.cron-script-card' in TARGETS['webui.tasks.script']['targets']
    assert TARGETS['webui.tasks.results']['targets'] == ['#cronDetailRuns']


def test_apps_targets_are_real_cards_and_directory_controls():
    from api.app_catalog import REGISTRY
    for app_id in REGISTRY:
        assert TARGETS['webui.apps.' + app_id] == {
            'panel': 'apps', 'targets': ['#mainApps [data-app-id="' + app_id + '"]']}
    for feature_id, target in (('overview', '#mainApps'), ('search', '#appsSearch'),
                               ('categories', '#appsCategory'), ('language', '#appsLanguage')):
        assert TARGETS['webui.apps.' + feature_id] == {'panel': 'apps', 'targets': [target]}
    assert TARGETS['webui.modules.apps'] == TARGETS['webui.apps.overview']
