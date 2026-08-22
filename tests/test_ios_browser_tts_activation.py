"""Regression coverage for delayed browser TTS on iOS Safari/PWA.

Mobile Safari only permits later, asynchronous speech synthesis after an
utterance has first been queued during a trusted user gesture.  Hermes receives
the assistant response long after the Send / Voice Mode tap, so the browser TTS
path must be primed while that activation is still live.
"""

from pathlib import Path
import json
import subprocess


REPO = Path(__file__).resolve().parents[1]


def _extract_function(src: str, name: str) -> str:
    anchor = f"function {name}("
    start = src.find(anchor)
    assert start != -1, f"{name}() must exist"
    body_start = src.find("{", start)
    assert body_start != -1, f"{name}() must have a body"
    depth = 1
    idx = body_start + 1
    while depth and idx < len(src):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
        idx += 1
    assert depth == 0, f"{name}() body must balance braces"
    return src[start:idx]


def test_prime_helper_obeys_activation_and_browser_engine():
    src = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
    helper = _extract_function(src, "_primeBrowserTtsFromGesture")

    harness = f"""
let _browserTtsPrimeUtterance=null;
{helper}

function runCase(active, engine, autoRead, force) {{
  const calls=[];
  global.window={{speechSynthesis:{{}}}};
  Object.defineProperty(global, 'navigator', {{
    value:{{userActivation:{{isActive:active}}}},
    configurable:true,
  }});
  global.localStorage={{getItem:(key)=>{{
    if(key==='hermes-tts-engine') return engine;
    if(key==='hermes-tts-auto-read') return autoRead;
    return null;
  }}}};
  global.SpeechSynthesisUtterance=function(text){{this.text=text;}};
  global.speechSynthesis={{speak:(utter)=>calls.push(utter)}};
  const result=_primeBrowserTtsFromGesture(force);
  return {{result, count:calls.length, text:calls[0]&&calls[0].text}};
}}

process.stdout.write(JSON.stringify([
  runCase(true, 'browser', 'true', false),
  runCase(false, 'browser', 'true', false),
  runCase(true, 'edge', 'true', false),
  runCase(true, 'browser', 'false', false),
  runCase(true, 'browser', 'false', true),
]));
"""
    proc = subprocess.run(
        ["node", "-e", harness],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=True,
    )
    results = json.loads(proc.stdout)
    assert results == [
        {"result": True, "count": 1, "text": ""},
        {"result": False, "count": 0},
        {"result": False, "count": 0},
        {"result": False, "count": 0},
        {"result": True, "count": 1, "text": ""},
    ]


def test_user_gesture_prime_unlocks_a_later_async_reply():
    src = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
    helper = _extract_function(src, "_primeBrowserTtsFromGesture")
    harness = f"""
let _browserTtsPrimeUtterance=null;
{helper}

let active=true;
let unlocked=false;
const spoken=[];
global.window={{speechSynthesis:{{}}}};
Object.defineProperty(global, 'navigator', {{
  value:{{userActivation:{{get isActive(){{return active;}}}}}},
  configurable:true,
}});
global.localStorage={{getItem:(key)=>{{
  if(key==='hermes-tts-engine') return 'browser';
  if(key==='hermes-tts-auto-read') return 'true';
  return null;
}}}};
global.SpeechSynthesisUtterance=function(text){{this.text=text;}};
global.speechSynthesis={{speak:(utter)=>{{
  if(active && utter.text==='') unlocked=true;
  if(utter.text && (active || unlocked)) spoken.push(utter.text);
}}}};

// Send tap: unlock with an empty utterance while activation is live.
_primeBrowserTtsFromGesture(false);
// Model response: user activation has expired by the time auto-read fires.
active=false;
speechSynthesis.speak(new SpeechSynthesisUtterance('Hermes reply'));
process.stdout.write(JSON.stringify({{unlocked, spoken}}));
"""
    proc = subprocess.run(
        ["node", "-e", harness],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(proc.stdout) == {
        "unlocked": True,
        "spoken": ["Hermes reply"],
    }


def test_send_primes_before_any_async_or_busy_guard():
    src = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
    send = _extract_function(src, "send")
    prime_at = send.find("_primeBrowserTtsFromGesture(false)")
    assert prime_at != -1
    assert prime_at < send.find("if (_sendInProgress)")
    assert prime_at < send.find("await ") if "await " in send else True


def test_voice_mode_stops_then_primes_before_busy_or_listening():
    src = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
    activate = _extract_function(src, "_activate")
    stop_at = activate.find("stopTTS()")
    prime_at = activate.find("_primeBrowserTtsFromGesture(true)")
    busy_at = activate.find("S.busy")
    listen_at = activate.find("_startListening()")
    assert -1 not in (stop_at, prime_at, busy_at, listen_at)
    assert stop_at < prime_at < busy_at < listen_at
