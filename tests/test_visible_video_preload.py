"""Behavioral tests for visibility-driven chat video preloading."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def _extract_function(name: str) -> str:
    start = UI_JS.index(f"function {name}(")
    brace = UI_JS.index("{", start)
    depth = 1
    i = brace + 1
    while depth:
        if UI_JS[i] == "{":
            depth += 1
        elif UI_JS[i] == "}":
            depth -= 1
        i += 1
    return UI_JS[start:i]


def _run_node(body: str) -> dict:
    script = "\n".join(
        [
            "let _mediaVisibilityObserver=null;",
            _extract_function("_promoteVisibleVideoPreload"),
            _extract_function("_observeVideoPreload"),
            _extract_function("_unobserveVideoPreload"),
            _extract_function("_initMediaVisibilityObserver"),
            body,
        ]
    )
    result = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_offscreen_video_stays_metadata_only_until_first_intersection():
    result = _run_node(
        r"""
let callback=null;
const observed=[];
const unobserved=[];
global.IntersectionObserver=class {
  constructor(cb,options){callback=cb;this.options=options;}
  observe(v){observed.push(v);}
  unobserve(v){unobserved.push(v);}
};
const video={
  dataset:{}, preload:'metadata', paused:true, readyState:1, loads:0,
  matches:s=>s==='.msg-media-video', load(){this.loads+=1;}
};
_initMediaVisibilityObserver();
_observeVideoPreload(video);
const before={preload:video.preload,loads:video.loads,observed:observed.length};
callback([{target:video,isIntersecting:false}]);
const offscreen={preload:video.preload,loads:video.loads,unobserved:unobserved.length};
callback([{target:video,isIntersecting:true}]);
const first={preload:video.preload,loads:video.loads,unobserved:unobserved.length};
callback([{target:video,isIntersecting:true}]);
const second={preload:video.preload,loads:video.loads,unobserved:unobserved.length};
console.log(JSON.stringify({before,offscreen,first,second,options:_mediaVisibilityObserver.options}));
"""
    )

    assert result["before"] == {"preload": "metadata", "loads": 0, "observed": 1}
    assert result["offscreen"] == {"preload": "metadata", "loads": 0, "unobserved": 0}
    assert result["first"] == {"preload": "auto", "loads": 1, "unobserved": 1}
    assert result["second"] == {"preload": "auto", "loads": 1, "unobserved": 2}
    assert result["options"]["rootMargin"] == "300px 0px"


def test_removed_video_is_unobserved_before_it_intersects():
    script = "\n".join(
        [
            "let _mediaVisibilityObserver=null;",
            _extract_function("_promoteVisibleVideoPreload"),
            _extract_function("_observeVideoPreload"),
            _extract_function("_unobserveVideoPreload"),
            _extract_function("_initMediaVisibilityObserver"),
            _extract_function("_initMediaPlaybackObserver"),
            r"""
let ioCallback=null,mutationCallback=null;
const observed=[],unobserved=[];
global.IntersectionObserver=class {
  constructor(cb){ioCallback=cb;}
  observe(v){observed.push(v);}
  unobserve(v){unobserved.push(v);}
};
global.MutationObserver=class {
  constructor(cb){mutationCallback=cb;}
  observe(){}
};
const documentVideos=[];
global.document={
  body:{}, readyState:'complete',
  querySelectorAll:()=>documentVideos,
  addEventListener:()=>{}
};
global.window={};
global._applyMediaPlaybackRate=()=>{};
const video={nodeType:1,isConnected:true,dataset:{},preload:'metadata',loads:0,load(){this.loads+=1;},matches:s=>s==='.msg-media-video'||s==='audio,video',querySelectorAll:()=>[]};
_initMediaPlaybackObserver();
mutationCallback([{addedNodes:[video],removedNodes:[]}]);
mutationCallback([{addedNodes:[],removedNodes:[video]}]);
video.isConnected=false;
ioCallback([{target:video,isIntersecting:true}]);
console.log(JSON.stringify({observed:observed.length,unobserved:unobserved.length,preload:video.preload,loads:video.loads,marker:video.dataset.visiblePreload||null}));
""",
        ]
    )
    result = subprocess.run(
        ["node", "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "observed": 1,
        "unobserved": 2,
        "preload": "metadata",
        "loads": 0,
        "marker": None,
    }
