"""Focused coverage for split locale loading and command descriptions."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.i18n_locale_loader import locale_sources


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_locale_timeout_falls_back_and_late_bundle_does_not_activate():
    core_path = ROOT / "static" / "i18n-core.js"
    french_path = ROOT / "static" / "locales" / "fr.js"
    script = f"""
const fs = require('fs');
const vm = require('vm');
const timers = new Map();
const scripts = [];
const storage = {{'hermes-lang':'fr'}};
let nextTimer = 1;
const ctx = {{
  URL,
  setTimeout(fn) {{ const id=nextTimer++; timers.set(id,fn); return id; }},
  clearTimeout(id) {{ timers.delete(id); }},
  localStorage: {{
    getItem(key) {{ return storage[key] || null; }},
    setItem(key,value) {{ storage[key]=String(value); }},
  }},
  document: {{
    baseURI:'https://example.test/', currentScript:null,
    documentElement:{{lang:''}}, querySelectorAll:()=>[],
    createElement:()=>({{async:false}}),
    head:{{appendChild(script){{scripts.push(script);}}}},
  }},
}};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync({json.dumps(str(core_path))},'utf8'),ctx);
const started=vm.runInContext('loadLocale()',ctx);
const timeout=[...timers.values()][0];
timers.clear();
timeout();
Promise.resolve(started).then(result=>{{
  vm.runInContext(fs.readFileSync({json.dumps(str(french_path))},'utf8'),ctx);
  scripts[0].onload();
  process.stdout.write(JSON.stringify({{
    status:result.status,
    active:vm.runInContext('getActiveLocale()',ctx),
    stored:storage['hermes-lang'],
  }}));
}});
"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {
        "status": "fallback",
        "active": "en",
        "stored": "en",
    }


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_late_script_callbacks_do_not_reject_a_locale_retry():
    core_path = ROOT / "static" / "i18n-core.js"
    french_path = ROOT / "static" / "locales" / "fr.js"
    script = f"""
const fs=require('fs');
const vm=require('vm');
const timers=new Map();
const scripts=[];
let nextTimer=1;
const ctx={{
  URL,
  setTimeout(fn){{const id=nextTimer++;timers.set(id,fn);return id;}},
  clearTimeout(id){{timers.delete(id);}},
  localStorage:{{getItem:()=> 'fr',setItem(){{}}}},
  document:{{
    baseURI:'https://example.test/',currentScript:null,
    documentElement:{{lang:''}},querySelectorAll:()=>[],
    createElement:()=>({{async:false}}),
    head:{{appendChild(script){{scripts.push(script);}}}},
  }},
}};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync({json.dumps(str(core_path))},'utf8'),ctx);
const expired=vm.runInContext("ensureLocale('fr').catch(()=> 'expired')",ctx);
const oldScript=scripts[0];
[...timers.values()][0]();
expired.then(async()=>{{
  const retry=vm.runInContext("ensureLocale('fr').then(()=> 'loaded',()=> 'rejected')",ctx);
  oldScript.onload();
  oldScript.onerror();
  await Promise.resolve();
  const stillPending=vm.runInContext("Boolean(_LOCALE_LOAD_PROMISES.fr)",ctx);
  vm.runInContext(fs.readFileSync({json.dumps(str(french_path))},'utf8'),ctx);
  scripts[1].onload();
  process.stdout.write(JSON.stringify({{stillPending,result:await retry}}));
}});
"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {"stillPending": True, "result": "loaded"}


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_slash_command_descriptions_follow_the_active_locale():
    commands_path = ROOT / "static" / "commands.js"
    source = commands_path.read_text(encoding="utf-8")
    start = source.index("const COMMANDS=[")
    end = source.index("\nfunction parseCommand", start)
    command_table = source[start:end]
    function_names = sorted(set(re.findall(r"fn:(\w+)", command_table)))
    functions = ",".join(f"{name}:()=>{{}}" for name in function_names)
    script = f"""
const vm=require('vm');
let locale='English';
const ctx={{t(key){{return locale+':'+key;}},setLocale(value){{locale=value;}},{functions}}};
vm.createContext(ctx);
vm.runInContext({json.dumps(command_table)},ctx);
const before=vm.runInContext("COMMANDS.find(c=>c.name==='help').desc",ctx);
ctx.setLocale('French');
const after=vm.runInContext("COMMANDS.find(c=>c.name==='help').desc",ctx);
const subarg=vm.runInContext("SLASH_SUBARG_SOURCES.model.desc",ctx);
process.stdout.write(JSON.stringify({{before,after,subarg}}));
"""
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {
        "before": "English:cmd_help",
        "after": "French:cmd_help",
        "subarg": "French:cmd_model",
    }


def test_locale_sources_are_cached():
    assert locale_sources() is locale_sources()
