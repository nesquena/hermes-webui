#!/usr/bin/env python3
from __future__ import annotations

import cgi
import json
import os
import re
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))
from run_agent import AIAgent

HOST = os.getenv('HERMES_WEBUI_HOST', '127.0.0.1')
PORT = int(os.getenv('HERMES_WEBUI_PORT', '8787'))
HOME = Path.home()
CONFIG_PATH = Path(os.getenv('HERMES_CONFIG_PATH', str(HOME / '.hermes' / 'config.yaml')))
STATE_DIR = Path(os.getenv('HERMES_WEBUI_STATE_DIR', str(HOME / '.hermes' / 'webui-mvp')))
SESSION_DIR = STATE_DIR / 'sessions'
DEFAULT_WORKSPACE = Path(os.getenv('HERMES_WEBUI_DEFAULT_WORKSPACE', str(HOME / '.hermes' / 'webui-mvp' / 'test-workspace'))).expanduser().resolve()
DEFAULT_MODEL = os.getenv('HERMES_WEBUI_DEFAULT_MODEL', 'openai/gpt-5.4-mini')
MAX_FILE_BYTES = 200_000
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB upload limit
CHAT_LOCK = threading.Lock()
SESSIONS = {}

cfg = {}
if CONFIG_PATH.exists():
    try:
        cfg = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    except Exception:
        cfg = {}
CLI_TOOLSETS = cfg.get('platform_toolsets', {}).get('cli', [
    'browser','clarify','code_execution','cronjob','delegation','file',
    'image_gen','memory','session_search','skills','terminal','todo',
    'tts','vision','web',
])

HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hermes</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg:      #1a1a2e;
    --sidebar: #16213e;
    --border:  rgba(255,255,255,0.08);
    --border2: rgba(255,255,255,0.14);
    --text:    #e8e8f0;
    --muted:   #8888aa;
    --accent:  #e94560;
    --blue:    #7cb9ff;
    --gold:    #c9a84c;
    --code-bg: #0d1117;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 15px; line-height: 1.6;
  }
  body { background: var(--bg); color: var(--text); height: 100vh; overflow: hidden; display: flex; }
  .layout { display: flex; width: 100%; height: 100vh; }

  /* ── Sidebar ── */
  .sidebar {
    width: 260px; background: var(--sidebar);
    border-right: 1px solid var(--border);
    display: flex; flex-direction: column; overflow: hidden; flex-shrink: 0;
  }
  .sidebar-header {
    padding: 20px 18px 16px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; gap: 10px;
  }
  .logo {
    width: 30px; height: 30px; border-radius: 8px;
    background: linear-gradient(135deg, var(--gold), var(--accent));
    display: flex; align-items: center; justify-content: center;
    font-weight: 700; font-size: 13px; color: #fff; flex-shrink: 0;
  }
  .sidebar-header h1 { font-size: 15px; font-weight: 600; }
  .sidebar-section { padding: 14px 14px 8px; }
  .new-chat-btn {
    width: 100%; padding: 9px 12px; border-radius: 8px;
    background: rgba(255,255,255,0.06); border: 1px solid var(--border2);
    color: var(--text); font-size: 13px; cursor: pointer;
    display: flex; align-items: center; gap: 8px; transition: background .15s; margin-bottom: 8px;
  }
  .new-chat-btn:hover { background: rgba(255,255,255,0.11); }
  .session-list { flex: 1; overflow-y: auto; padding: 0 8px 8px; }
  .session-item {
    padding: 8px 10px; border-radius: 7px; cursor: pointer;
    font-size: 13px; color: var(--muted);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    transition: background .12s, color .12s;
  }
  .session-item:hover { background: rgba(255,255,255,0.06); color: var(--text); }
  .session-item.active { background: rgba(124,185,255,0.12); color: var(--blue); }
  .sidebar-bottom { border-top: 1px solid var(--border); padding: 12px 14px; }
  .field-label { font-size: 11px; color: var(--muted); margin-bottom: 5px; }
  select {
    width: 100%; background: rgba(255,255,255,0.05); border: 1px solid var(--border2);
    border-radius: 7px; color: var(--text); padding: 7px 10px; font-size: 13px;
    outline: none; appearance: none; margin-bottom: 10px;
  }
  .workspace-path {
    font-size: 11px; color: var(--muted); padding: 6px 8px;
    background: rgba(0,0,0,.25); border-radius: 6px; word-break: break-all;
    cursor: pointer; margin-bottom: 10px;
  }
  .workspace-path:hover { color: var(--text); }
  .sidebar-actions { display: flex; gap: 6px; }
  .sm-btn {
    flex: 1; padding: 7px 0; border-radius: 7px; font-size: 12px;
    background: rgba(255,255,255,0.06); border: 1px solid var(--border);
    color: var(--muted); cursor: pointer; transition: all .15s; text-align: center;
  }
  .sm-btn:hover { background: rgba(255,255,255,0.11); color: var(--text); }

  /* ── Main ── */
  .main { flex: 1; display: flex; flex-direction: column; overflow: hidden; min-width: 0; }
  .topbar {
    padding: 14px 20px; border-bottom: 1px solid var(--border);
    background: rgba(26,26,46,.95); backdrop-filter: blur(10px);
    display: flex; align-items: center; justify-content: space-between; flex-shrink: 0;
  }
  .topbar-title { font-size: 15px; font-weight: 500; }
  .topbar-meta { font-size: 12px; color: var(--muted); margin-top: 2px; }
  .topbar-chips { display: flex; gap: 6px; }
  .chip {
    font-size: 11px; padding: 3px 9px; border-radius: 999px;
    background: rgba(255,255,255,0.07); border: 1px solid var(--border2); color: var(--muted);
  }
  .chip.model { color: var(--blue); border-color: rgba(124,185,255,0.3); background: rgba(124,185,255,0.08); }

  /* ── Messages ── */
  .messages { flex: 1; overflow-y: auto; display: flex; flex-direction: column; }
  .messages-inner {
    max-width: 780px; margin: 0 auto; width: 100%;
    padding: 24px 20px; display: flex; flex-direction: column;
  }
  .msg-row { padding: 16px 0; border-bottom: 1px solid rgba(255,255,255,.04); }
  .msg-row:last-child { border-bottom: none; }
  .msg-role {
    font-size: 11px; font-weight: 600; text-transform: uppercase;
    letter-spacing: .1em; margin-bottom: 8px; display: flex; align-items: center; gap: 8px;
  }
  .msg-role.user { color: var(--blue); }
  .msg-role.assistant { color: var(--gold); }
  .role-icon {
    width: 22px; height: 22px; border-radius: 6px;
    display: flex; align-items: center; justify-content: center;
    font-size: 11px; font-weight: 700; flex-shrink: 0;
  }
  .role-icon.user { background: rgba(124,185,255,0.2); color: var(--blue); }
  .role-icon.assistant { background: linear-gradient(135deg,var(--gold),var(--accent)); color:#fff; }
  .msg-body { font-size: 14px; line-height: 1.7; color: var(--text); padding-left: 30px; }
  .msg-body p { margin-bottom: 10px; }
  .msg-body p:last-child { margin-bottom: 0; }
  .msg-body ul,.msg-body ol { margin: 6px 0 10px 20px; }
  .msg-body li { margin-bottom: 3px; }
  .msg-body h1,.msg-body h2,.msg-body h3 { margin: 16px 0 6px; font-weight: 600; }
  .msg-body h1{font-size:18px}.msg-body h2{font-size:16px}.msg-body h3{font-size:14px}
  .msg-body strong { color: #fff; font-weight: 600; }
  .msg-body em { color: #c9c9e8; font-style: italic; }
  .msg-body code {
    font-family: "SF Mono","Fira Code",ui-monospace,monospace;
    font-size: 12.5px; background: rgba(0,0,0,.35);
    padding: 1px 5px; border-radius: 4px; color: #f0c27f;
  }
  .msg-body pre {
    background: var(--code-bg); border: 1px solid rgba(255,255,255,.08);
    border-radius: 10px; padding: 14px 16px; overflow-x: auto; margin: 10px 0;
  }
  .msg-body pre code { background:none; padding:0; border-radius:0; color:#e2e8f0; font-size:13px; line-height:1.6; }
  .pre-header {
    font-size: 11px; color: var(--muted); padding: 6px 16px 0;
    background: var(--code-bg); border-radius: 10px 10px 0 0;
    border: 1px solid rgba(255,255,255,.08); border-bottom: none;
  }
  .pre-header + pre { border-radius: 0 0 10px 10px; border-top: none; margin-top: 0; }
  .msg-body blockquote { border-left:3px solid var(--blue); padding-left:14px; color:var(--muted); font-style:italic; margin:10px 0; }
  .msg-body a { color: var(--blue); text-decoration: underline; }
  .msg-body hr { border:none; border-top:1px solid var(--border); margin:14px 0; }

  /* uploaded-file badge in messages */
  .msg-files { display: flex; flex-wrap: wrap; gap: 6px; padding-left: 30px; margin-bottom: 10px; }
  .msg-file-badge {
    display: flex; align-items: center; gap: 5px;
    background: rgba(124,185,255,0.1); border: 1px solid rgba(124,185,255,0.25);
    border-radius: 6px; padding: 4px 9px; font-size: 12px; color: var(--blue);
  }

  /* thinking */
  .thinking { display:flex; align-items:center; gap:6px; color:var(--muted); font-size:13px; padding-left:30px; }
  .dot { width:5px; height:5px; border-radius:50%; background:var(--muted); animation:pulse 1.2s ease-in-out infinite; }
  .dot:nth-child(2){animation-delay:.2s}.dot:nth-child(3){animation-delay:.4s}
  @keyframes pulse{0%,80%,100%{opacity:.25}40%{opacity:1}}

  /* empty state */
  .empty-state {
    flex: 1; display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    gap: 12px; padding: 40px; color: var(--muted);
  }
  .empty-logo {
    width: 60px; height: 60px; border-radius: 16px;
    background: linear-gradient(135deg, var(--gold), var(--accent));
    display: flex; align-items: center; justify-content: center;
    font-size: 26px; font-weight: 700; color: #fff; margin-bottom: 4px;
  }
  .empty-state h2 { font-size: 20px; color: var(--text); font-weight: 600; }
  .empty-state p { font-size: 14px; text-align: center; max-width: 320px; }
  .suggestion-grid { display: flex; flex-direction: column; gap: 8px; margin-top: 12px; width: 100%; max-width: 380px; }
  .suggestion {
    padding: 10px 14px; background: rgba(255,255,255,0.05);
    border: 1px solid var(--border2); border-radius: 10px;
    font-size: 13px; color: var(--muted); cursor: pointer; transition: all .15s; text-align: left;
  }
  .suggestion:hover { background: rgba(255,255,255,0.09); color: var(--text); border-color: var(--blue); }

  /* ── Composer ── */
  .composer-wrap {
    border-top: 1px solid var(--border);
    padding: 12px 20px 16px;
    background: var(--bg); flex-shrink: 0;
  }
  /* drag-over overlay on the entire composer */
  .composer-wrap.drag-over .composer-box {
    border-color: var(--blue);
    background: rgba(124,185,255,0.06);
  }
  .drop-hint {
    display: none; position: absolute; inset: 0;
    align-items: center; justify-content: center;
    background: rgba(124,185,255,0.08);
    border: 2px dashed var(--blue); border-radius: 14px;
    font-size: 14px; color: var(--blue); pointer-events: none; z-index: 10;
    flex-direction: column; gap: 8px;
  }
  .drop-hint svg { opacity: .7; }
  .composer-wrap.drag-over .drop-hint { display: flex; }

  .composer-box {
    max-width: 780px; margin: 0 auto;
    background: rgba(255,255,255,0.05); border: 1px solid var(--border2);
    border-radius: 14px; display: flex; flex-direction: column;
    transition: border-color .15s, background .15s;
    position: relative;
  }
  .composer-box:focus-within { border-color: rgba(124,185,255,0.45); }

  /* attachment tray */
  .attach-tray {
    display: none; flex-wrap: wrap; gap: 6px;
    padding: 10px 14px 0;
  }
  .attach-tray.has-files { display: flex; }
  .attach-chip {
    display: flex; align-items: center; gap: 5px;
    background: rgba(124,185,255,0.12); border: 1px solid rgba(124,185,255,0.3);
    border-radius: 6px; padding: 4px 8px; font-size: 12px; color: var(--blue);
  }
  .attach-chip button {
    background: none; border: none; color: var(--muted); cursor: pointer;
    font-size: 13px; line-height: 1; padding: 0 0 0 3px;
    display: flex; align-items: center;
  }
  .attach-chip button:hover { color: var(--accent); }

  textarea#msg {
    width: 100%; background: transparent; border: none; outline: none;
    color: var(--text); font-size: 14px; line-height: 1.6;
    padding: 14px 16px 6px; resize: none; min-height: 52px; max-height: 200px;
    font-family: inherit;
  }
  textarea#msg::placeholder { color: var(--muted); }
  .composer-footer {
    display: flex; align-items: center; justify-content: space-between;
    padding: 6px 10px 10px;
  }
  .composer-left { display: flex; gap: 2px; align-items: center; }
  .composer-right { display: flex; gap: 6px; align-items: center; }
  .icon-btn {
    width: 32px; height: 32px; border-radius: 7px; background: none;
    border: none; color: var(--muted); cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    font-size: 16px; transition: all .15s;
  }
  .icon-btn:hover { background: rgba(255,255,255,.08); color: var(--text); }
  .icon-btn svg { pointer-events: none; }
  .status-text { font-size: 11px; color: var(--muted); padding-left: 4px; }
  .send-btn {
    padding: 7px 16px; border-radius: 8px; font-size: 13px; font-weight: 600;
    background: var(--blue); border: none; color: #0d1117;
    cursor: pointer; display: flex; align-items: center; gap: 6px;
    transition: all .15s; flex-shrink: 0;
  }
  .send-btn:hover { background: #a0d0ff; }
  .send-btn:disabled { opacity: .4; cursor: not-allowed; }

  /* upload progress bar */
  .upload-bar-wrap {
    display: none; height: 2px; background: var(--border); border-radius: 0 0 14px 14px; overflow: hidden;
  }
  .upload-bar-wrap.active { display: block; }
  .upload-bar { height: 100%; background: var(--blue); width: 0%; transition: width .2s; }

  /* ── Right panel ── */
  .rightpanel {
    width: 300px; background: var(--sidebar);
    border-left: 1px solid var(--border);
    display: flex; flex-direction: column; overflow: hidden; flex-shrink: 0;
  }
  .panel-header {
    padding: 14px 16px; border-bottom: 1px solid var(--border);
    font-size: 12px; font-weight: 600; color: var(--muted);
    text-transform: uppercase; letter-spacing: .08em;
    display: flex; align-items: center; justify-content: space-between;
  }
  .close-preview { cursor: pointer; opacity:.6; }
  .close-preview:hover { opacity:1; }
  .file-tree { flex:1; overflow-y:auto; padding:8px; }
  .file-item {
    display:flex; align-items:center; gap:8px; padding:7px 10px; border-radius:7px;
    cursor:pointer; font-size:13px; color:var(--muted);
    transition:all .12s; white-space:nowrap; overflow:hidden;
  }
  .file-item:hover { background:rgba(255,255,255,.07); color:var(--text); }
  .file-item.active { background:rgba(124,185,255,.12); color:var(--blue); }
  .file-icon { flex-shrink:0; opacity:.7; font-size:12px; }
  .file-name { overflow:hidden; text-overflow:ellipsis; flex:1; }
  .file-size { font-size:11px; color:var(--muted); flex-shrink:0; margin-left:auto; }
  .preview-area { flex:1; overflow:auto; padding:14px; display:none; flex-direction:column; gap:8px; }
  .preview-area.visible { display:flex; }
  .preview-path { font-size:11px; color:var(--muted); word-break:break-all; padding-bottom:8px; border-bottom:1px solid var(--border); }
  .preview-code {
    font-family:"SF Mono","Fira Code",ui-monospace,monospace;
    font-size:12px; line-height:1.55; white-space:pre-wrap; word-break:break-word; color:#c9d1d9;
  }

  ::-webkit-scrollbar{width:5px;height:5px}
  ::-webkit-scrollbar-track{background:transparent}
  ::-webkit-scrollbar-thumb{background:rgba(255,255,255,.12);border-radius:99px}
  ::-webkit-scrollbar-thumb:hover{background:rgba(255,255,255,.2)}
  @media(max-width:900px){.rightpanel{display:none}}
  @media(max-width:640px){.sidebar{display:none}}
</style>
</head>
<body>
<div class="layout">

  <aside class="sidebar">
    <div class="sidebar-header">
      <div class="logo">H</div>
      <h1>Hermes</h1>
    </div>
    <div class="sidebar-section">
      <button class="new-chat-btn" id="btnNewChat">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
        New conversation
      </button>
    </div>
    <div class="session-list" id="sessionList"></div>
    <div class="sidebar-bottom">
      <div class="field-label">Model</div>
      <select id="modelSelect">
        <option value="openai/gpt-5.4-mini">GPT-5.4 Mini</option>
        <option value="anthropic/claude-sonnet-4.6">Claude Sonnet 4.6</option>
      </select>
      <div class="field-label">Workspace</div>
      <div class="workspace-path" id="workspacePath" title="Click to change">—</div>
      <div class="sidebar-actions">
        <button class="sm-btn" id="btnDownload">↓ Transcript</button>
        <button class="sm-btn" id="btnRefreshFiles">⟳ Files</button>
      </div>
    </div>
  </aside>

  <main class="main">
    <div class="topbar">
      <div>
        <div class="topbar-title" id="topbarTitle">Hermes</div>
        <div class="topbar-meta" id="topbarMeta">Start a new conversation</div>
      </div>
      <div class="topbar-chips">
        <div class="chip model" id="modelChip">GPT-5.4 Mini</div>
        <div class="chip" id="wsChip">test-workspace</div>
      </div>
    </div>

    <div class="messages" id="messages">
      <div class="empty-state" id="emptyState">
        <div class="empty-logo">H</div>
        <h2>What can I help with?</h2>
        <p>Ask anything — or drag &amp; drop files into the composer below.</p>
        <div class="suggestion-grid">
          <button class="suggestion" data-msg="What files are in this workspace?">📁 What files are in this workspace?</button>
          <button class="suggestion" data-msg="Summarise what Hermes can do for me.">⚡ Summarise what Hermes can do for me.</button>
          <button class="suggestion" data-msg="Help me plan a small project.">🗺 Help me plan a small project.</button>
        </div>
      </div>
      <div class="messages-inner" id="msgInner"></div>
    </div>

    <div class="composer-wrap" id="composerWrap">
      <div class="composer-box" id="composerBox">
        <div class="drop-hint" id="dropHint">
          <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
          Drop files to add to workspace
        </div>
        <div class="attach-tray" id="attachTray"></div>
        <textarea id="msg" rows="1" placeholder="Message Hermes… (Enter to send, Shift+Enter for newline)"></textarea>
        <div class="composer-footer">
          <div class="composer-left">
            <!-- hidden file input -->
            <input type="file" id="fileInput" multiple style="display:none">
            <button class="icon-btn" id="btnAttach" title="Attach files">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/>
              </svg>
            </button>
            <span class="status-text" id="statusText"></span>
          </div>
          <div class="composer-right">
            <button class="send-btn" id="btnSend">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
              Send
            </button>
          </div>
        </div>
        <div class="upload-bar-wrap" id="uploadBarWrap">
          <div class="upload-bar" id="uploadBar"></div>
        </div>
      </div>
    </div>
  </main>

  <aside class="rightpanel">
    <div class="panel-header">
      <span>Workspace</span>
      <span class="close-preview" id="btnClearPreview">✕</span>
    </div>
    <div class="file-tree" id="fileTree"></div>
    <div class="preview-area" id="previewArea">
      <div class="preview-path" id="previewPath"></div>
      <pre class="preview-code" id="previewCode"></pre>
    </div>
  </aside>
</div>

<script>
const S = { session: null, messages: [], entries: [], busy: false, pendingFiles: [] };
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

/* ── Markdown ── */
function renderMd(raw) {
  let s = raw || '';
  s = s.replace(/```([\w+-]*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    const h = lang ? `<div class="pre-header">${esc(lang)}</div>` : '';
    return `${h}<pre><code>${esc(code.replace(/\n$/, ''))}</code></pre>`;
  });
  s = s.replace(/`([^`\n]+)`/g, (_, c) => `<code>${esc(c)}</code>`);
  s = s.replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>');
  s = s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/\*([^*\n]+)\*/g, '<em>$1</em>');
  s = s.replace(/^### (.+)$/gm, '<h3>$1</h3>');
  s = s.replace(/^## (.+)$/gm, '<h2>$1</h2>');
  s = s.replace(/^# (.+)$/gm, '<h1>$1</h1>');
  s = s.replace(/^---+$/gm, '<hr>');
  s = s.replace(/^> (.+)$/gm, '<blockquote>$1</blockquote>');
  s = s.replace(/((?:^[-*+] .+\n?)+)/gm, block => {
    const items = block.trim().split('\n').map(l => `<li>${l.replace(/^[-*+] /, '')}</li>`).join('');
    return `<ul>${items}</ul>`;
  });
  s = s.replace(/((?:^\d+\. .+\n?)+)/gm, block => {
    const items = block.trim().split('\n').map(l => `<li>${l.replace(/^\d+\. /, '')}</li>`).join('');
    return `<ol>${items}</ol>`;
  });
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  const parts = s.split(/\n{2,}/);
  s = parts.map(p => {
    p = p.trim(); if (!p) return '';
    if (/^<(h[1-6]|ul|ol|pre|hr|blockquote)/.test(p)) return p;
    return `<p>${p.replace(/\n/g, '<br>')}</p>`;
  }).join('\n');
  return s;
}

/* ── UI helpers ── */
function setStatus(t) { $('statusText').textContent = t; }
function setBusy(v) { S.busy = v; $('btnSend').disabled = v; if (!v) setStatus(''); }

function syncTopbar() {
  if (!S.session) return;
  $('topbarTitle').textContent = S.session.title || 'Untitled';
  $('topbarMeta').textContent = `${S.messages.filter(m => m.role !== 'tool').length} messages`;
  const m = S.session.model || '';
  $('modelChip').textContent = m.includes('sonnet') ? 'Sonnet 4.6' : 'GPT-5.4 Mini';
  const ws = S.session.workspace || '';
  $('wsChip').textContent = ws.split('/').slice(-2).join('/') || ws;
  $('workspacePath').textContent = ws;
  $('modelSelect').value = m;
}

function renderMessages() {
  const inner = $('msgInner');
  const vis = S.messages.filter(m => m && m.role && m.role !== 'tool');
  $('emptyState').style.display = vis.length ? 'none' : '';
  inner.innerHTML = '';
  for (const m of vis) {
    let content = m.content || '';
    if (Array.isArray(content)) content = content.map(p => p.text || p.content || '').join('\n');
    const isUser = m.role === 'user';
    const row = document.createElement('div');
    row.className = 'msg-row';
    let filesHtml = '';
    if (m.attachments && m.attachments.length) {
      filesHtml = `<div class="msg-files">${m.attachments.map(f =>
        `<div class="msg-file-badge">📎 ${esc(f)}</div>`).join('')}</div>`;
    }
    row.innerHTML = `
      <div class="msg-role ${m.role}">
        <div class="role-icon ${m.role}">${isUser ? 'U' : 'H'}</div>
        ${isUser ? 'You' : 'Hermes'}
      </div>
      ${filesHtml}
      <div class="msg-body">${isUser ? esc(String(content)).replace(/\n/g,'<br>') : renderMd(String(content))}</div>
    `;
    inner.appendChild(row);
  }
  $('messages').scrollTop = $('messages').scrollHeight;
}

function appendThinking() {
  $('emptyState').style.display = 'none';
  const row = document.createElement('div');
  row.className = 'msg-row'; row.id = 'thinkingRow';
  row.innerHTML = `
    <div class="msg-role assistant"><div class="role-icon assistant">H</div>Hermes</div>
    <div class="thinking"><div class="dot"></div><div class="dot"></div><div class="dot"></div></div>
  `;
  $('msgInner').appendChild(row);
  $('messages').scrollTop = $('messages').scrollHeight;
}
function removeThinking() { const el = $('thinkingRow'); if (el) el.remove(); }

function renderFileTree() {
  const box = $('fileTree'); box.innerHTML = '';
  for (const item of S.entries) {
    const el = document.createElement('div'); el.className = 'file-item';
    const icon = item.type === 'dir' ? '📁' : '📄';
    const size = item.type === 'file' && item.size ? `${(item.size/1024).toFixed(1)}k` : '';
    el.innerHTML = `<span class="file-icon">${icon}</span><span class="file-name">${esc(item.name)}</span>${size ? `<span class="file-size">${size}</span>` : ''}`;
    el.onclick = async () => item.type === 'dir' ? loadDir(item.path) : openFile(item.path);
    box.appendChild(el);
  }
}

/* ── Attachment tray ── */
function renderTray() {
  const tray = $('attachTray');
  tray.innerHTML = '';
  if (!S.pendingFiles.length) { tray.classList.remove('has-files'); return; }
  tray.classList.add('has-files');
  S.pendingFiles.forEach((f, i) => {
    const chip = document.createElement('div'); chip.className = 'attach-chip';
    chip.innerHTML = `📎 ${esc(f.name)} <button title="Remove">✕</button>`;
    chip.querySelector('button').onclick = () => { S.pendingFiles.splice(i, 1); renderTray(); };
    tray.appendChild(chip);
  });
}

function addFiles(files) {
  for (const f of files) {
    if (!S.pendingFiles.find(p => p.name === f.name)) S.pendingFiles.push(f);
  }
  renderTray();
}

/* ── Upload ── */
async function uploadPendingFiles() {
  if (!S.pendingFiles.length || !S.session) return [];
  const names = [];
  const bar = $('uploadBar'); const barWrap = $('uploadBarWrap');
  barWrap.classList.add('active'); bar.style.width = '0%';
  const total = S.pendingFiles.length;
  for (let i = 0; i < total; i++) {
    const f = S.pendingFiles[i];
    const fd = new FormData();
    fd.append('session_id', S.session.session_id);
    fd.append('file', f, f.name);
    try {
      const res = await fetch('/api/upload', { method: 'POST', body: fd });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      names.push(data.filename);
    } catch (e) {
      setStatus(`Upload failed: ${f.name}`);
    }
    bar.style.width = `${Math.round((i + 1) / total * 100)}%`;
  }
  barWrap.classList.remove('active'); bar.style.width = '0%';
  S.pendingFiles = []; renderTray();
  return names;
}

/* ── API ── */
async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
  if (!res.ok) throw new Error(await res.text());
  const ct = res.headers.get('content-type') || '';
  return ct.includes('application/json') ? res.json() : res.text();
}

async function loadDir(path) {
  if (!S.session) return;
  try {
    const data = await api(`/api/list?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(path)}`);
    S.entries = data.entries || []; renderFileTree();
  } catch (e) { console.warn('loadDir', e); }
}

async function openFile(path) {
  if (!S.session) return;
  try {
    const data = await api(`/api/file?session_id=${encodeURIComponent(S.session.session_id)}&path=${encodeURIComponent(path)}`);
    $('previewPath').textContent = data.path;
    $('previewCode').textContent = data.content;
    $('previewArea').classList.add('visible');
    $('fileTree').style.display = 'none';
  } catch (e) { setStatus('Could not open file'); }
}

async function newSession() {
  const data = await api('/api/session/new', { method: 'POST', body: JSON.stringify({ model: $('modelSelect').value }) });
  S.session = data.session; S.messages = data.session.messages || [];
  localStorage.setItem('hermes-webui-session', S.session.session_id);
  syncTopbar(); await loadDir('.'); renderMessages(); renderSessionList();
}

async function loadSession(sid) {
  const data = await api(`/api/session?session_id=${encodeURIComponent(sid)}`);
  S.session = data.session; S.messages = data.session.messages || [];
  syncTopbar(); await loadDir('.'); renderMessages();
}

async function renderSessionList() {
  try {
    const data = await api('/api/sessions');
    const list = $('sessionList'); list.innerHTML = '';
    for (const s of (data.sessions || []).slice(0, 30)) {
      const el = document.createElement('div');
      el.className = 'session-item' + (S.session && s.session_id === S.session.session_id ? ' active' : '');
      el.textContent = s.title || 'Untitled'; el.title = s.title || 'Untitled';
      el.onclick = async () => { await loadSession(s.session_id); renderSessionList(); };
      list.appendChild(el);
    }
  } catch (e) {}
}

async function send() {
  const text = $('msg').value.trim();
  if ((!text && !S.pendingFiles.length) || S.busy) return;
  if (!S.session) await newSession();

  // Upload files first
  setStatus('Uploading files…');
  const uploaded = await uploadPendingFiles();

  // Build user message text, mentioning uploads
  let msgText = text;
  if (uploaded.length && !msgText) msgText = `I've uploaded ${uploaded.length} file(s): ${uploaded.join(', ')}`;
  else if (uploaded.length) msgText = `${text}\n\n[Attached files: ${uploaded.join(', ')}]`;

  $('msg').value = ''; autoResize();
  const userMsg = { role: 'user', content: text || `Uploaded: ${uploaded.join(', ')}`, attachments: uploaded.length ? uploaded : undefined };
  S.messages.push(userMsg);
  renderMessages(); appendThinking(); setBusy(true); setStatus('Hermes is thinking…');

  try {
    const data = await api('/api/chat', {
      method: 'POST',
      body: JSON.stringify({
        session_id: S.session.session_id,
        message: msgText,
        model: $('modelSelect').value,
        workspace: S.session.workspace
      })
    });
    S.session = data.session; S.messages = data.session.messages || [];
    // preserve attachment metadata on last user message
    if (uploaded.length) {
      const lastUser = [...S.messages].reverse().find(m => m.role === 'user');
      if (lastUser) lastUser.attachments = uploaded;
    }
    removeThinking(); syncTopbar(); renderMessages(); await loadDir('.'); renderSessionList(); setBusy(false);
  } catch (e) {
    removeThinking();
    S.messages.push({ role: 'assistant', content: `**Error:** ${e.message}` });
    renderMessages(); setBusy(false); setStatus('Error');
  }
}

function transcript() {
  const lines = [`# Hermes session ${S.session?.session_id || ''}`, '',
    `Workspace: ${S.session?.workspace || ''}`, `Model: ${S.session?.model || ''}`, ''];
  for (const m of S.messages) {
    if (!m || m.role === 'tool') continue;
    let c = m.content || '';
    if (Array.isArray(c)) c = c.map(p => p.text || p.content || '').join('\n');
    lines.push(`## ${m.role}`, '', String(c).trim(), '');
  }
  return lines.join('\n');
}

function autoResize() {
  const el = $('msg'); el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 200) + 'px';
}

/* ── Drag and drop ── */
const wrap = $('composerWrap');
let dragCounter = 0;

// Also allow dropping on the full page
document.addEventListener('dragover', e => e.preventDefault());
document.addEventListener('dragenter', e => {
  e.preventDefault();
  if (e.dataTransfer.types.includes('Files')) { dragCounter++; wrap.classList.add('drag-over'); }
});
document.addEventListener('dragleave', e => {
  dragCounter--;
  if (dragCounter <= 0) { dragCounter = 0; wrap.classList.remove('drag-over'); }
});
document.addEventListener('drop', e => {
  e.preventDefault(); dragCounter = 0; wrap.classList.remove('drag-over');
  const files = Array.from(e.dataTransfer.files);
  if (files.length) { addFiles(files); $('msg').focus(); }
});

/* ── Wire up ── */
$('btnSend').onclick = send;
$('btnAttach').onclick = () => $('fileInput').click();
$('fileInput').onchange = e => { addFiles(Array.from(e.target.files)); e.target.value = ''; };
$('btnNewChat').onclick = async () => { await newSession(); $('msg').focus(); };
$('btnDownload').onclick = () => {
  if (!S.session) return;
  const blob = new Blob([transcript()], { type: 'text/markdown' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = `hermes-${S.session.session_id}.md`;
  a.click(); URL.revokeObjectURL(a.href);
};
$('btnRefreshFiles').onclick = () => { if (S.session) loadDir('.'); };
$('btnClearPreview').onclick = () => { $('previewArea').classList.remove('visible'); $('fileTree').style.display = ''; };
$('workspacePath').onclick = async () => {
  const newWs = prompt('Workspace path:', S.session?.workspace || '');
  if (!newWs || !S.session) return;
  await api('/api/session/update', { method: 'POST', body: JSON.stringify({ session_id: S.session.session_id, workspace: newWs, model: $('modelSelect').value }) });
  S.session.workspace = newWs; syncTopbar(); await loadDir('.');
};
$('modelSelect').onchange = async () => {
  if (!S.session) return;
  await api('/api/session/update', { method: 'POST', body: JSON.stringify({ session_id: S.session.session_id, workspace: S.session.workspace, model: $('modelSelect').value }) });
  S.session.model = $('modelSelect').value; syncTopbar();
};
$('msg').addEventListener('input', autoResize);
$('msg').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } });
document.querySelectorAll('.suggestion').forEach(btn => {
  btn.onclick = () => { $('msg').value = btn.dataset.msg; send(); };
});

(async () => {
  const saved = localStorage.getItem('hermes-webui-session');
  if (saved) { try { await loadSession(saved); await renderSessionList(); return; } catch (e) {} }
  await newSession(); await renderSessionList();
})();
</script>
</body>
</html>
"""

CHAT_LOCK = threading.Lock()

def safe_resolve(root: Path, requested: str) -> Path:
    root = root.expanduser().resolve(); requested = (requested or '.').strip()
    candidate = root if requested in ('', '.') else (root / requested if not os.path.isabs(requested) else Path(requested)).expanduser().resolve()
    candidate.relative_to(root)
    return candidate

def j(handler, payload, status=200):
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
    handler.send_response(status); handler.send_header('Content-Type','application/json; charset=utf-8'); handler.send_header('Content-Length', str(len(raw))); handler.send_header('Cache-Control','no-store'); handler.end_headers(); handler.wfile.write(raw)

def t(handler, payload, status=200, content_type='text/plain; charset=utf-8'):
    raw = payload.encode('utf-8'); handler.send_response(status); handler.send_header('Content-Type', content_type); handler.send_header('Content-Length', str(len(raw))); handler.send_header('Cache-Control','no-store'); handler.end_headers(); handler.wfile.write(raw)

def read_body(handler):
    length = int(handler.headers.get('Content-Length','0') or 0)
    return json.loads(handler.rfile.read(length) or b'{}') if length else {}

class Session:
    def __init__(self, session_id=None, title='Untitled', workspace=str(DEFAULT_WORKSPACE), model=DEFAULT_MODEL, messages=None, created_at=None, updated_at=None):
        self.session_id = session_id or uuid.uuid4().hex[:12]; self.title = title; self.workspace = str(Path(workspace).expanduser().resolve()); self.model = model; self.messages = messages or []; self.created_at = created_at or time.time(); self.updated_at = updated_at or time.time()
    @property
    def path(self): return SESSION_DIR / f'{self.session_id}.json'
    def save(self): self.updated_at = time.time(); self.path.write_text(json.dumps(self.__dict__, ensure_ascii=False, indent=2), encoding='utf-8')
    @classmethod
    def load(cls, sid):
        p = SESSION_DIR / f'{sid}.json'
        if not p.exists(): return None
        return cls(**json.loads(p.read_text(encoding='utf-8')))
    def compact(self): return {'session_id': self.session_id, 'title': self.title, 'workspace': self.workspace, 'model': self.model, 'message_count': len(self.messages), 'created_at': self.created_at, 'updated_at': self.updated_at}

def get_session(sid):
    if sid in SESSIONS: return SESSIONS[sid]
    s = Session.load(sid)
    if s: SESSIONS[sid] = s; return s
    raise KeyError(sid)

def new_session(workspace=None, model=None):
    s = Session(workspace=workspace or str(DEFAULT_WORKSPACE), model=model or DEFAULT_MODEL); SESSIONS[s.session_id] = s; s.save(); return s

def all_sessions():
    out = []
    for p in SESSION_DIR.glob('*.json'):
        try:
            s = Session.load(p.stem)
            if s: out.append(s)
        except Exception: pass
    for s in SESSIONS.values():
        if all(s.session_id != x.session_id for x in out): out.append(s)
    out.sort(key=lambda s: s.updated_at, reverse=True)
    return [s.compact() for s in out]

def list_dir(workspace: Path, rel='.'):
    target = safe_resolve(workspace, rel)
    if not target.exists() or not target.is_dir(): raise FileNotFoundError(rel)
    rows = []
    for item in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        rows.append({'name': item.name, 'path': str(item.relative_to(workspace)), 'type': 'dir' if item.is_dir() else 'file', 'size': None if item.is_dir() else item.stat().st_size})
    return rows[:200]

def read_file_content(workspace: Path, rel):
    target = safe_resolve(workspace, rel)
    if not target.exists() or not target.is_file(): raise FileNotFoundError(rel)
    if target.stat().st_size > MAX_FILE_BYTES: raise ValueError(f'File too large: {target.stat().st_size} bytes')
    text = target.read_text(encoding='utf-8', errors='replace')
    return {'path': str(target.relative_to(workspace)), 'content': text, 'size': target.stat().st_size, 'lines': text.count('\n') + 1}

def title_from(messages, fallback='Untitled'):
    for m in messages:
        if m.get('role') == 'user':
            c = m.get('content','')
            if isinstance(c, list): c = '\n'.join((p.get('text') or '') for p in c if isinstance(p, dict))
            txt = str(c).strip().replace('\n',' ')
            return txt[:64] if txt else fallback
    return fallback

def handle_upload(handler):
    """Parse multipart upload, save file to workspace, return filename."""
    content_type = handler.headers.get('Content-Type', '')
    content_length = int(handler.headers.get('Content-Length', 0))
    if content_length > MAX_UPLOAD_BYTES:
        j(handler, {'error': f'File too large (max {MAX_UPLOAD_BYTES//1024//1024}MB)'}, status=413)
        return

    # Use cgi.FieldStorage to parse multipart
    env = {
        'REQUEST_METHOD': 'POST',
        'CONTENT_TYPE': content_type,
        'CONTENT_LENGTH': str(content_length),
    }
    fs = cgi.FieldStorage(fp=handler.rfile, headers=handler.headers, environ=env)

    session_id = fs.getvalue('session_id', '')
    file_field = fs['file'] if 'file' in fs else None
    if not file_field or not file_field.filename:
        j(handler, {'error': 'No file in request'}, status=400)
        return

    try:
        s = get_session(session_id)
    except KeyError:
        j(handler, {'error': 'Session not found'}, status=404)
        return

    workspace = Path(s.workspace)
    # Sanitise filename
    safe_name = re.sub(r'[^\w.\-]', '_', Path(file_field.filename).name)[:200]
    dest = workspace / safe_name
    dest.write_bytes(file_field.file.read())
    j(handler, {'filename': safe_name, 'path': str(dest), 'size': dest.stat().st_size})

class Handler(BaseHTTPRequestHandler):
    server_version = 'HermesCoWorkMVP/0.3'
    def log_message(self, fmt, *args): print('[webui]', fmt % args)

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            if parsed.path in ('/', '/index.html'): return t(self, HTML, content_type='text/html; charset=utf-8')
            if parsed.path == '/health': return j(self, {'status':'ok', 'sessions': len(SESSIONS)})
            if parsed.path == '/api/session':
                sid = parse_qs(parsed.query).get('session_id', [''])[0]
                if not sid:
                    s = new_session(); return j(self, {'session': s.compact() | {'messages': s.messages}})
                s = get_session(sid); return j(self, {'session': s.compact() | {'messages': s.messages}})
            if parsed.path == '/api/sessions': return j(self, {'sessions': all_sessions()})
            if parsed.path == '/api/list':
                qs = parse_qs(parsed.query); s = get_session(qs.get('session_id', [''])[0])
                return j(self, {'entries': list_dir(Path(s.workspace), qs.get('path', ['.'])[0]), 'path': qs.get('path', ['.'])[0]})
            if parsed.path == '/api/file':
                qs = parse_qs(parsed.query); s = get_session(qs.get('session_id', [''])[0])
                return j(self, read_file_content(Path(s.workspace), qs.get('path', [''])[0]))
            return j(self, {'error':'not found'}, status=404)
        except Exception as e:
            return j(self, {'error': str(e), 'trace': traceback.format_exc()}, status=500)

    def do_POST(self):
        try:
            parsed = urlparse(self.path)
            if parsed.path == '/api/upload':
                return handle_upload(self)
            body = read_body(self)
            if parsed.path == '/api/session/new':
                s = new_session(workspace=body.get('workspace'), model=body.get('model'))
                return j(self, {'session': s.compact() | {'messages': s.messages}})
            if parsed.path == '/api/session/update':
                s = get_session(body['session_id']); s.workspace = str(Path(body.get('workspace', s.workspace)).expanduser().resolve()); s.model = body.get('model', s.model); s.save()
                return j(self, {'session': s.compact() | {'messages': s.messages}})
            if parsed.path == '/api/chat':
                s = get_session(body['session_id']); msg = str(body.get('message', '')).strip()
                if not msg: return j(self, {'error':'empty message'}, status=400)
                workspace = Path(body.get('workspace') or s.workspace).expanduser().resolve()
                s.workspace = str(workspace); s.model = body.get('model') or s.model
                old_cwd = os.environ.get('TERMINAL_CWD'); os.environ['TERMINAL_CWD'] = str(workspace)
                try:
                    with CHAT_LOCK:
                        agent = AIAgent(model=s.model, platform='cli', quiet_mode=True, enabled_toolsets=CLI_TOOLSETS, session_id=s.session_id)
                        result = agent.run_conversation(user_message=msg, conversation_history=s.messages, task_id=s.session_id)
                finally:
                    if old_cwd is None: os.environ.pop('TERMINAL_CWD', None)
                    else: os.environ['TERMINAL_CWD'] = old_cwd
                s.messages = result.get('messages') or s.messages; s.title = title_from(s.messages, s.title); s.save()
                return j(self, {'answer': result.get('final_response') or '', 'status': 'done' if result.get('completed', True) else 'partial', 'session': s.compact() | {'messages': s.messages}})
            return j(self, {'error':'not found'}, status=404)
        except Exception as e:
            return j(self, {'error': str(e), 'trace': traceback.format_exc()}, status=500)

def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True); SESSION_DIR.mkdir(parents=True, exist_ok=True)
    DEFAULT_WORKSPACE.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f'Hermes Co-Work MVP listening on http://{HOST}:{PORT}')
    print(f'Default workspace: {DEFAULT_WORKSPACE}')
    print(f'Default model: {DEFAULT_MODEL}')
    httpd.serve_forever()

if __name__ == '__main__':
    main()
