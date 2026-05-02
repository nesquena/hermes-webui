"""Test HU-03.3 — Hero avatar + greeting logic.

Validates:
- dashboard.js syntax
- Greeting i18n keys exist in en and pt-BR
- Hero HTML elements present in index.html
- Hero CSS classes and animations present in style.css
"""

import json
import subprocess
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
I18N = STATIC / "i18n.js"

GREETING_KEYS = [
    "greeting_good_morning",
    "greeting_good_afternoon",
    "greeting_good_evening",
    "greeting_welcome_back",
    "greeting_summary",
    "hero_status_online",
]


def _greeting_data() -> dict:
    script = textwrap.dedent(
        f"""
        const fs = require('fs');
        const vm = require('vm');
        const src = fs.readFileSync({json.dumps(str(I18N))}, 'utf8');
        const ctx = {{
          localStorage: {{ getItem: () => null, setItem: () => {{}} }},
          document: {{ documentElement: {{ lang: '' }}, querySelectorAll: () => [] }},
        }};
        vm.createContext(ctx);
        vm.runInContext(src, ctx);
        const out = vm.runInContext(`(() => {{
          const en = LOCALES.en;
          const pt = LOCALES['pt-BR'];
          return {{
            enKeys: Object.keys(en || {{}}),
            ptKeys: Object.keys(pt || {{}}),
            vals: {{
              greeting_good_morning: {{ en: en.greeting_good_morning, pt: pt.greeting_good_morning }},
              greeting_good_afternoon: {{ en: en.greeting_good_afternoon, pt: pt.greeting_good_afternoon }},
              greeting_good_evening: {{ en: en.greeting_good_evening, pt: pt.greeting_good_evening }},
              greeting_welcome_back: {{ en: en.greeting_welcome_back, pt: pt.greeting_welcome_back }},
              hero_status_online: {{ en: en.hero_status_online, pt: pt.hero_status_online }},
            }},
          }};
        }})()`, ctx);
        process.stdout.write(JSON.stringify(out));
        """
    )
    proc = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    )
    return json.loads(proc.stdout)


DATA = _greeting_data()
EN_KEYS = set(DATA["enKeys"])
PT_KEYS = set(DATA["ptKeys"])


# ── i18n keys ──────────────────────────────────────────────────────────────

def test_all_greeting_keys_exist_in_en():
    missing = [k for k in GREETING_KEYS if k not in EN_KEYS]
    assert not missing, f"Missing en keys: {missing}"


def test_all_greeting_keys_exist_in_pt_br():
    missing = [k for k in GREETING_KEYS if k not in PT_KEYS]
    assert not missing, f"Missing pt-BR keys: {missing}"


def test_pt_br_greetings_are_portuguese():
    v = DATA["vals"]
    assert "Bom dia" in v["greeting_good_morning"]["pt"]
    assert "Boa tarde" in v["greeting_good_afternoon"]["pt"]
    assert "Boa noite" in v["greeting_good_evening"]["pt"]
    assert "OPERACIONAL" in v["hero_status_online"]["pt"]


def test_en_greetings_are_english():
    assert "OPERATIONAL" in DATA["vals"]["hero_status_online"]["en"]


# ── dashboard.js ───────────────────────────────────────────────────────────

def test_dashboard_js_syntax():
    result = subprocess.run(
        ["node", "--check", str(STATIC / "dashboard.js")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"Syntax error: {result.stderr}"


def test_dashboard_js_has_greeting_logic():
    code = (STATIC / "dashboard.js").read_text()
    assert "_getGreetingKey" in code
    assert "greeting_good_morning" in code
    assert "greeting_good_afternoon" in code
    assert "greeting_good_evening" in code
    assert "heroGreetingTime" in code


# ── HTML elements ──────────────────────────────────────────────────────────

def test_hero_card_in_index_html():
    html = (STATIC / "index.html").read_text()
    assert 'class="hero-card"' in html
    assert 'class="hero-avatar"' in html
    assert 'class="hero-status-pill"' in html
    assert 'id="heroGreetingTime"' in html
    assert 'class="hero-greeting-welcome"' in html
    assert 'class="hero-greeting-summary"' in html
    assert "neo-avatar.svg" in html


# ── CSS ────────────────────────────────────────────────────────────────────

def test_hero_css_in_style():
    css = (STATIC / "style.css").read_text()
    assert ".hero-card" in css
    assert ".hero-avatar" in css
    assert ".hero-status-pill" in css
    assert ".hero-greeting" in css
    assert "hover-float" in css
    assert "pulse-glow" in css
    assert "prefers-reduced-motion" in css
