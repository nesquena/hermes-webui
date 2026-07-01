"""Self-contained HTML export for a hermes-webui session transcript.

Produces a single static HTML file (no external assets, no CDN) that renders
the conversation the same way the WebUI does: Markdown, code blocks, tables,
lists, blockquotes — all styled with an inlined dark theme matching the app.
Double-click the file and it opens in any browser, offline.

Public entry point: render_session_html(session_dict) -> str
"""
from __future__ import annotations

import html
import re
import time
from typing import Any

try:
    from markdown_it import MarkdownIt
    _MD = (
        MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": False})
        .enable("table")
        .enable("strikethrough")
    )
except Exception:  # pragma: no cover - markdown_it always present in webui venv
    _MD = None


def _neutralize_remote_images(rendered_html: str) -> str:
    """Replace any <img> whose src isn't a data: URI with an inert placeholder.

    This is the single chokepoint that keeps the export self-contained. The
    multimodal flattening in _content_to_text() handles structured image_url
    parts, but a *text* message body can carry Markdown image syntax —
    ``![leak](https://host/private.png?sig=...)`` — which markdown_it renders
    into an active ``<img src="https://...">``. Opening the saved file would
    then fire a network request and leak a signed/private URL. Filtering here,
    after rendering, catches every path into the HTML (text Markdown images and
    any future source) regardless of how the <img> was produced. data: URIs are
    already embedded, render offline, and make no request, so they're kept.
    """
    if not rendered_html or "<img" not in rendered_html:
        return rendered_html

    def _repl(match: "re.Match[str]") -> str:
        tag = match.group(0)
        src_m = re.search(r'src\s*=\s*"([^"]*)"', tag) or re.search(
            r"src\s*=\s*'([^']*)'", tag
        )
        src = src_m.group(1) if src_m else ""
        if src.startswith("data:"):
            return tag  # already embedded, offline-safe
        # Inert placeholder mirroring _content_to_text's remote-image handling.
        label = html.escape(src) if src else "image"
        return f"<code>[image: {label}]</code>"

    return re.sub(r"<img\b[^>]*>", _repl, rendered_html, flags=re.IGNORECASE)


def _render_markdown(text: str) -> str:
    """Render Markdown to HTML. Falls back to escaped <pre> if the lib is missing."""
    if not text:
        return ""
    if _MD is None:
        return f"<pre>{html.escape(text)}</pre>"
    try:
        return _neutralize_remote_images(_MD.render(text))
    except Exception:
        return f"<pre>{html.escape(text)}</pre>"


def _content_to_text(content: Any) -> str:
    """Flatten message content (str or multimodal list) into Markdown text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                if c.get("type") == "text" or "text" in c:
                    parts.append(str(c.get("text", "")))
                elif c.get("type") in ("image_url", "image"):
                    url = ""
                    if isinstance(c.get("image_url"), dict):
                        url = c["image_url"].get("url", "")
                    url = url or c.get("url", "")
                    if url:
                        # Keep the export self-contained and avoid leaking
                        # private/signed URLs: only inline data: URIs (already
                        # embedded, render offline). Remote http(s) images are
                        # NOT rendered as <img> (that would fire a network
                        # request on open) — show an inert placeholder + the
                        # URL as plain text instead.
                        if url.startswith("data:"):
                            parts.append(f"![image]({url})")
                        else:
                            parts.append(f"`[image: {url}]`")
                else:
                    parts.append(f"`[{c.get('type', 'content')}]`")
            else:
                parts.append(str(c))
        return "\n\n".join(p for p in parts if p)
    return str(content or "")


def _fmt_ts(t: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(t)))
    except Exception:
        return ""


_ROLE_LABELS = {
    "user": ("You", "role-user"),
    "assistant": ("Assistant", "role-assistant"),
    "system": ("System", "role-system"),
    "tool": ("Tool", "role-tool"),
}

# Inlined theme matching the WebUI. Light is the default (:root); dark applies
# when <html class="dark">. Palettes are copied from the WebUI's style.css gold
# theme so the export follows whatever appearance the user has active.
_CSS = """
:root{--bg:#FEFCF7;--panel:#F3EEE3;--panel2:#FAF7F0;--border:#E0D8C8;
--text:#1A1610;--muted:#5C5344;--accent:#B8860B;--user:#0288A8;--assistant:#3D8B40;
--code-bg:#F5F0E5;--code-border:#E0D8C8;--code-text:#8b4513;
--badge-user-bg:rgba(2,136,168,.12);--badge-user-text:#0288A8;
--badge-assistant-bg:rgba(61,139,64,.12);--badge-assistant-text:#3D8B40;
--badge-system-bg:rgba(92,83,68,.14);--badge-system-text:#5C5344;
--badge-tool-bg:rgba(184,134,11,.14);--badge-tool-text:#8B6508;
--row-stripe:rgba(0,0,0,.02);--subtle:rgba(0,0,0,.02);}
:root.dark{--bg:#0D0D1A;--panel:#1A1A2E;--panel2:#141425;--border:#2A2A45;
--text:#FFF8DC;--muted:#C0C0C0;--accent:#FFD700;--user:#4DD0E1;--assistant:#4CAF50;
--code-bg:#1A1A2E;--code-border:#2A2A45;--code-text:#f0c27f;
--badge-user-bg:rgba(77,208,225,.16);--badge-user-text:#4DD0E1;
--badge-assistant-bg:rgba(76,175,80,.16);--badge-assistant-text:#56d364;
--badge-system-bg:rgba(192,192,192,.16);--badge-system-text:#C0C0C0;
--badge-tool-bg:rgba(255,191,0,.16);--badge-tool-text:#FFBF00;
--row-stripe:rgba(255,255,255,.025);--subtle:rgba(255,255,255,.02);}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",Roboto,Helvetica,Arial,sans-serif;}
.wrap{max-width:860px;margin:0 auto;padding:32px 20px 80px;}
header.doc-head{border-bottom:1px solid var(--border);padding-bottom:20px;margin-bottom:28px;}
header.doc-head h1{margin:0 0 10px;font-size:24px;font-weight:650;}
.meta{color:var(--muted);font-size:13px;line-height:1.9;}
.meta b{color:var(--text);font-weight:600;}
.msg{margin:0 0 22px;border:1px solid var(--border);border-radius:12px;overflow:hidden;background:var(--panel);}
.msg-head{display:flex;align-items:center;gap:10px;padding:10px 16px;background:var(--panel2);
border-bottom:1px solid var(--border);font-size:13px;}
.badge{font-weight:650;padding:2px 10px;border-radius:999px;font-size:12px;letter-spacing:.2px;}
.role-user .badge{background:var(--badge-user-bg);color:var(--badge-user-text);}
.role-assistant .badge{background:var(--badge-assistant-bg);color:var(--badge-assistant-text);}
.role-system .badge{background:var(--badge-system-bg);color:var(--badge-system-text);}
.role-tool .badge{background:var(--badge-tool-bg);color:var(--badge-tool-text);}
.ts{color:var(--muted);margin-left:auto;font-size:12px;}
.msg-body{padding:4px 18px 8px;}
.msg-body>:first-child{margin-top:8px}.msg-body>:last-child{margin-bottom:8px}
.msg-body p{margin:10px 0;}
.msg-body h1,.msg-body h2,.msg-body h3,.msg-body h4{margin:18px 0 10px;line-height:1.3;font-weight:640;}
.msg-body h1{font-size:21px}.msg-body h2{font-size:18px}.msg-body h3{font-size:16px}
.msg-body a{color:var(--accent);text-decoration:none}.msg-body a:hover{text-decoration:underline}
.msg-body ul,.msg-body ol{padding-left:24px;margin:10px 0;}
.msg-body li{margin:4px 0;}
.msg-body code{background:var(--code-bg);border:1px solid var(--code-border);border-radius:5px;color:var(--code-text);
padding:.15em .4em;font-size:.88em;font-family:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace;}
.msg-body pre{background:var(--code-bg);border:1px solid var(--code-border);border-radius:10px;
padding:14px 16px;overflow:auto;margin:12px 0;}
.msg-body pre code{background:none;border:0;padding:0;color:var(--text);font-size:13px;line-height:1.55;}
.msg-body blockquote{border-left:3px solid var(--border);margin:12px 0;padding:2px 16px;color:var(--muted);}
.msg-body table{border-collapse:collapse;margin:14px 0;width:100%;font-size:14px;display:block;overflow-x:auto;}
.msg-body th,.msg-body td{border:1px solid var(--border);padding:8px 12px;text-align:left;}
.msg-body th{background:var(--panel2);font-weight:640;}
.msg-body tr:nth-child(even) td{background:var(--row-stripe);}
.msg-body img{max-width:100%;border-radius:8px;}
.msg-body hr{border:0;border-top:1px solid var(--border);margin:18px 0;}
details.reasoning{margin:6px 0 4px;border:1px dashed var(--border);border-radius:8px;background:var(--subtle);}
details.reasoning summary{cursor:pointer;padding:8px 14px;color:var(--muted);font-size:13px;user-select:none;}
details.reasoning[open] summary{border-bottom:1px solid var(--border);}
details.reasoning .reasoning-body{padding:4px 16px 10px;color:var(--muted);font-size:13.5px;}
footer.doc-foot{margin-top:36px;padding-top:18px;border-top:1px solid var(--border);
color:var(--muted);font-size:12px;text-align:center;}
"""


def _palette_to_css(palette: dict) -> str:
    """Turn a {var-name: value} dict into a `:root{...}` override block.

    Var names may be given with or without the leading `--`. Values are sanitised
    to a conservative charset (colors, numbers, a few CSS units/functions) so an
    untrusted palette can't break out of the style block.
    """
    if not isinstance(palette, dict) or not palette:
        return ""
    decls = []
    for raw_name, raw_val in palette.items():
        name = str(raw_name).strip().lstrip("-")
        if not name or not re.fullmatch(r"[A-Za-z0-9-]+", name):
            continue
        val = str(raw_val).strip()
        # Allow hex/rgb/hsla colors, numbers, %, px, var(), color-mix(), commas, spaces.
        if not val or not re.fullmatch(r"[#A-Za-z0-9.,%()\-\s]+", val):
            continue
        if len(val) > 120:
            continue
        # Reject IE-only expression() — it evaluates JS in older IE and
        # contradicts the "no active code in <style>" guarantee.
        if re.search(r"expression\s*\(", val, re.IGNORECASE):
            continue
        decls.append(f"--{name}:{val};")
    if not decls:
        return ""
    # Use both :root and :root.dark so the captured palette wins regardless of
    # theme mode. _CSS defines :root.dark{…} at specificity (0,0,2,0) which
    # beats a plain :root{…} (0,0,1,0). By emitting both selectors the override
    # matches dark-mode specificity and source-order ensures it wins in light too.
    return ":root,:root.dark{" + "".join(decls) + "}"


def render_session_html(session: dict, theme: str = "dark", palette: dict | None = None) -> str:
    """Build a complete, self-contained HTML document for the session.

    theme: "dark" or "light" — selects which inlined fallback palette is active.
    palette: optional {css-var: value} dict captured from the live WebUI
        (getComputedStyle of :root). When provided it is injected last so the
        export matches the user's exact active theme AND skin, not just the
        built-in dark/light fallback.
    """
    html_class = ' class="dark"' if str(theme).lower() != "light" else ""
    palette_css = _palette_to_css(palette or {})
    title = (session.get("title") or "Hermes Conversation").strip()
    sid = session.get("session_id", "")
    model = session.get("model", "")
    provider = session.get("model_provider", "")
    created = _fmt_ts(session.get("created_at"))
    updated = _fmt_ts(session.get("updated_at"))
    messages = session.get("messages") or []
    # Skip system messages by default (usually long boilerplate); keep user/assistant/tool.
    visible = [m for m in messages if isinstance(m, dict) and m.get("role") != "system"]

    blocks = []
    for m in visible:
        role = m.get("role", "")
        label, cls = _ROLE_LABELS.get(role, (role or "?", "role-system"))
        ts = _fmt_ts(m.get("timestamp"))
        ts_html = f'<span class="ts">{html.escape(ts)}</span>' if ts else ""
        body_md = _content_to_text(m.get("content"))
        body_html = _render_markdown(body_md)
        reasoning = m.get("reasoning")
        reasoning_html = ""
        if reasoning and isinstance(reasoning, str) and reasoning.strip():
            reasoning_html = (
                '<details class="reasoning"><summary>💭 Reasoning</summary>'
                f'<div class="reasoning-body">{_render_markdown(reasoning)}</div></details>'
            )
        blocks.append(
            f'<section class="msg {cls}">'
            f'<div class="msg-head"><span class="badge">{html.escape(label)}</span>{ts_html}</div>'
            f'<div class="msg-body">{reasoning_html}{body_html}</div>'
            f'</section>'
        )

    meta_lines = []
    if sid:
        meta_lines.append(f"<div>Session: <b>{html.escape(sid)}</b></div>")
    if model:
        prov = f" · {html.escape(provider)}" if provider else ""
        meta_lines.append(f"<div>Model: <b>{html.escape(str(model))}</b>{prov}</div>")
    if created or updated:
        meta_lines.append(
            f"<div>Created: <b>{html.escape(created)}</b> · Updated: <b>{html.escape(updated)}</b></div>"
        )
    meta_lines.append(f"<div>Messages: <b>{len(visible)}</b></div>")
    meta_html = "".join(meta_lines)

    exported = time.strftime("%Y-%m-%d %H:%M:%S")
    return f"""<!DOCTYPE html>
<html lang="en"{html_class}>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{_CSS}{palette_css}</style>
</head>
<body>
<div class="wrap">
<header class="doc-head">
<h1>{html.escape(title)}</h1>
<div class="meta">{meta_html}</div>
</header>
<main>
{''.join(blocks)}
</main>
<footer class="doc-foot">Exported from Hermes WebUI on {exported}</footer>
</div>
</body>
</html>"""
