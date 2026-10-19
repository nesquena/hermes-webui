// ── Accessibility (a11y) shared helpers & live-stream announcer ──────────────
// Centralizes polite/assertive ARIA live-region announcements, focus
// preservation/restoration, and throttled screen-reader status updates for
// chat streaming, tool calls, and transient feedback.

let _a11yPoliteTimer = null;
let _a11yAssertiveTimer = null;
let _a11yLastToolAnnounceAt = 0;
let _a11yActiveStreamSid = null;
let _a11yActiveStreamAnnouncedStart = false;

const A11Y_TOOL_THROTTLE_MS = 3000;
const A11Y_RESPONSE_SPEECH_MAX_CHARS = 600;

function _a11yTranslate(key, fallback) {
  if (typeof t === 'function') {
    const val = t(key);
    if (val && val !== key) return String(val);
  }
  return fallback;
}

function _a11yAssistantName() {
  if (typeof assistantDisplayName === 'function') {
    const name = assistantDisplayName();
    if (name) return String(name);
  }
  return 'Hermes';
}

function _a11yIsActiveSession(sessionId) {
  if (!sessionId) return true;
  if (typeof S !== 'undefined' && S && S.session && S.session.session_id) {
    return S.session.session_id === sessionId;
  }
  return true;
}

function _ensureA11yLiveRegion(id, role, ariaLive) {
  if (typeof document === 'undefined' || !document.getElementById) return null;
  let el = document.getElementById(id);
  if (!el && document.body && typeof document.createElement === 'function') {
    el = document.createElement('div');
    el.id = id;
    el.className = 'sr-only';
    el.setAttribute('role', role);
    el.setAttribute('aria-live', ariaLive);
    el.setAttribute('aria-atomic', 'true');
    document.body.appendChild(el);
  }
  return el;
}

function announceA11yPolite(message, durationMs = 2500) {
  const text = String(message || '').trim();
  if (!text) return;
  const el = _ensureA11yLiveRegion('a11yAnnouncer', 'status', 'polite');
  if (!el) return;
  if (_a11yPoliteTimer) {
    clearTimeout(_a11yPoliteTimer);
    _a11yPoliteTimer = null;
  }
  // Clear first so identical consecutive announcements (e.g. repeated Copy)
  // still trigger a DOM mutation event for screen readers.
  el.textContent = '';
  const applyText = () => {
    el.textContent = text;
    if (durationMs > 0) {
      _a11yPoliteTimer = setTimeout(() => {
        if (el.textContent === text) el.textContent = '';
        _a11yPoliteTimer = null;
      }, durationMs);
    }
  };
  if (typeof requestAnimationFrame === 'function') {
    requestAnimationFrame(applyText);
  } else {
    applyText();
  }
}

function announceA11yAssertive(message, durationMs = 4000) {
  const text = String(message || '').trim();
  if (!text) return;
  const el = _ensureA11yLiveRegion('a11yAlertAnnouncer', 'alert', 'assertive');
  if (!el) return;
  if (_a11yAssertiveTimer) {
    clearTimeout(_a11yAssertiveTimer);
    _a11yAssertiveTimer = null;
  }
  el.textContent = '';
  const applyText = () => {
    el.textContent = text;
    if (durationMs > 0) {
      _a11yAssertiveTimer = setTimeout(() => {
        if (el.textContent === text) el.textContent = '';
        _a11yAssertiveTimer = null;
      }, durationMs);
    }
  };
  if (typeof requestAnimationFrame === 'function') {
    requestAnimationFrame(applyText);
  } else {
    applyText();
  }
}

function a11yRestoreFocus(targetEl) {
  if (!targetEl || typeof targetEl.focus !== 'function') return false;
  if (typeof document !== 'undefined' && targetEl === document.body) return false;
  if (targetEl.isConnected === false) return false;
  try {
    targetEl.focus({ preventScroll: true });
    return true;
  } catch (_) {
    try {
      targetEl.focus();
      return true;
    } catch (_) {
      return false;
    }
  }
}

function a11yPreserveFocusAcross(fn) {
  const prevFocus = (typeof document !== 'undefined' && document.activeElement) ? document.activeElement : null;
  try {
    return fn();
  } finally {
    if (prevFocus && typeof document !== 'undefined' && document.activeElement !== prevFocus) {
      a11yRestoreFocus(prevFocus);
    }
  }
}

function a11ySetStreamBusy(busy, sessionId) {
  if (!_a11yIsActiveSession(sessionId)) return;
  if (typeof document === 'undefined' || !document.getElementById) return;
  const val = busy ? 'true' : 'false';
  const messagesEl = document.getElementById('messages');
  if (messagesEl && typeof messagesEl.setAttribute === 'function') {
    messagesEl.setAttribute('aria-busy', val);
  }
  const liveTurnEl = document.getElementById('liveAssistantTurn');
  if (liveTurnEl && typeof liveTurnEl.setAttribute === 'function') {
    liveTurnEl.setAttribute('aria-busy', val);
  }
}

function a11yResetStreamState(sessionId) {
  if (sessionId && _a11yActiveStreamSid && _a11yActiveStreamSid !== sessionId) return;
  _a11yActiveStreamSid = null;
  _a11yActiveStreamAnnouncedStart = false;
  _a11yLastToolAnnounceAt = 0;
  a11ySetStreamBusy(false, sessionId);
}

function a11yOnStreamStart(sessionId, opts = {}) {
  if (!_a11yIsActiveSession(sessionId)) return;
  const reconnecting = !!(opts && opts.reconnecting);
  const isSameStreamSid = _a11yActiveStreamSid === sessionId && _a11yActiveStreamAnnouncedStart;
  _a11yActiveStreamSid = sessionId || null;
  a11ySetStreamBusy(true, sessionId);
  if (reconnecting || isSameStreamSid) return;
  _a11yActiveStreamAnnouncedStart = true;
  _a11yLastToolAnnounceAt = 0;
  const name = _a11yAssistantName();
  const thinkingLabel = _a11yTranslate('thinking', 'Thinking');
  announceA11yPolite(`${name}: ${thinkingLabel}…`, 3000);
}

function _a11yFormatToolLabel(toolCall) {
  if (!toolCall) return '';
  const rawName = String(toolCall.name || toolCall.tool || '').trim();
  if (!rawName) return '';
  const cleanName = rawName.replace(/[_-]+/g, ' ');
  const preview = String(toolCall.preview || '').trim();
  if (preview && preview.length <= 60) {
    return `${cleanName} (${preview})`;
  }
  return cleanName;
}

function a11yOnToolStart(toolCall, sessionId, opts = {}) {
  if (!_a11yIsActiveSession(sessionId)) return;
  const label = _a11yFormatToolLabel(toolCall);
  if (!label) return;
  const now = (typeof Date !== 'undefined' && Date.now) ? Date.now() : 0;
  const throttleMs = (opts && typeof opts.throttleMs === 'number') ? opts.throttleMs : A11Y_TOOL_THROTTLE_MS;
  if (_a11yLastToolAnnounceAt > 0 && (now - _a11yLastToolAnnounceAt) < throttleMs) {
    return;
  }
  _a11yLastToolAnnounceAt = now;
  const prefix = _a11yTranslate('a11y_running_tool', 'Running tool');
  announceA11yPolite(`${prefix}: ${label}`, 2500);
}

function a11yOnToolComplete(toolCall, sessionId) {
  if (!_a11yIsActiveSession(sessionId)) return;
  if (!toolCall || !toolCall.is_error) return;
  const label = _a11yFormatToolLabel(toolCall);
  if (!label) return;
  const prefix = _a11yTranslate('a11y_tool_failed', 'Tool failed');
  announceA11yPolite(`${prefix}: ${label}`, 3500);
}

function _a11yExtractResponseText(opts, sessionId) {
  if (typeof opts === 'string') return opts.trim();
  if (!opts || typeof opts !== 'object') return '';
  if (typeof opts.text === 'string' && opts.text.trim()) return opts.text.trim();
  let text = '';
  const msg = opts.message;
  if (msg && typeof msg === 'object') {
    if (typeof _assistantTurnAnchorSettledFinalAnswer === 'function') {
      const anchorFinal = _assistantTurnAnchorSettledFinalAnswer(
        msg,
        msg.content,
        { session_id: sessionId || undefined }
      );
      if (anchorFinal !== null && anchorFinal !== undefined) {
        text = String(anchorFinal || '').trim();
      }
    }
    if (!text && typeof msgContent === 'function') {
      text = String(msgContent(msg) || '').trim();
    }
    if (!text) {
      let raw = msg.content || '';
      if (Array.isArray(raw)) {
        raw = raw.filter(p => p && p.type === 'text').map(p => p.text || '').join('').trim();
      }
      text = String(raw || '').trim();
    }
    if (text && typeof _extractInlineThinkingFromContent === 'function') {
      const split = _extractInlineThinkingFromContent(text, msg.reasoning, { streaming: false });
      if (split && typeof split.content === 'string') text = split.content.trim();
    }
  }
  if (!text && typeof opts.liveDisplayText === 'string') {
    text = opts.liveDisplayText.trim();
  }
  return text;
}

function _a11yCleanMarkdownForSpeech(rawText) {
  let text = String(rawText || '').trim();
  if (!text) return { text: '', hadCodeBlock: false };
  // Strip <think>...</think> blocks if any remain
  text = text.replace(/<think>[\s\S]*?<\/think>/gi, ' ');
  // Detect and replace fenced code blocks with a brief spoken placeholder
  const codeBlockRe = /(^|\n)[ ]{0,3}```(?:[\s\S]*?\n)?[ ]{0,3}```(?=\n|$)/g;
  let hadCodeBlock = false;
  const codePlaceholder = _a11yTranslate('a11y_code_block_omitted', 'Code block omitted.');
  text = text.replace(codeBlockRe, () => {
    hadCodeBlock = true;
    return ` ${codePlaceholder} `;
  });
  // Keep inline code text (strip backticks only, not the identifier inside)
  text = text.replace(/`([^`]+)`/g, '$1');
  // Strip bold/italic markers while keeping text
  text = text.replace(/\*\*(.+?)\*\*/g, '$1');
  text = text.replace(/\*(.+?)\*/g, '$1');
  text = text.replace(/__(.+?)__/g, '$1');
  text = text.replace(/_(.+?)_/g, '$1');
  // Strip heading hashes
  text = text.replace(/^#{1,6}\s+/gm, '');
  // Keep link labels, strip URLs
  text = text.replace(/\[([^\]]+)\]\([^)]+\)/g, '$1');
  // Replace MEDIA: paths
  text = text.replace(/MEDIA:[^\s]+/g, 'attached media');
  // Strip HTML tags
  text = text.replace(/<[^>]+>/g, ' ');
  // Collapse whitespace
  text = text.replace(/\s+/g, ' ').trim();
  return { text, hadCodeBlock };
}

function _a11yTruncateForSpeech(text, maxChars) {
  if (text.length <= maxChars) return { preview: text, truncated: false };
  const slice = text.slice(0, maxChars);
  // Prefer breaking at the last sentence boundary if one exists in the second half
  const sentenceMatch = slice.match(/^([\s\S]*[.!?])(?:\s+|$)/);
  if (sentenceMatch && sentenceMatch[1].length >= Math.floor(maxChars * 0.45)) {
    return { preview: sentenceMatch[1].trim(), truncated: true };
  }
  // Otherwise break at the last word boundary
  const lastSpace = slice.lastIndexOf(' ');
  const cut = lastSpace >= Math.floor(maxChars * 0.5) ? slice.slice(0, lastSpace).trim() : slice.trim();
  return { preview: `${cut}…`, truncated: true };
}

function a11yFormatAssistantReplyAnnouncement(rawText, opts = {}) {
  const name = _a11yAssistantName();
  const maxChars = (opts && typeof opts.maxChars === 'number' && opts.maxChars > 0)
    ? opts.maxChars
    : A11Y_RESPONSE_SPEECH_MAX_CHARS;
  const { text, hadCodeBlock } = _a11yCleanMarkdownForSpeech(rawText);
  if (!text) {
    const doneLabel = _a11yTranslate('a11y_response_complete', 'Response complete');
    return `${name}: ${doneLabel}`;
  }
  const { preview, truncated } = _a11yTruncateForSpeech(text, maxChars);
  if (truncated || hadCodeBlock) {
    const suffix = _a11yTranslate('a11y_full_response_in_transcript', 'Full response ready in transcript.');
    return `${name}: ${preview} ${suffix}`;
  }
  return `${name}: ${preview}`;
}

function a11yOnStreamDone(sessionId, opts = {}) {
  const isActive = _a11yIsActiveSession(sessionId);
  a11yResetStreamState(sessionId);
  if (!isActive) return;
  const rawText = _a11yExtractResponseText(opts, sessionId);
  const announcement = a11yFormatAssistantReplyAnnouncement(rawText, opts);
  // Pass durationMs = 0 so longer spoken responses are never cleared mid-speech
  // by a timer; the live region will be cleared on the next announcement.
  announceA11yPolite(announcement, 0);
}

function a11yOnStreamCancel(sessionId) {
  const isActive = _a11yIsActiveSession(sessionId);
  a11yResetStreamState(sessionId);
  if (!isActive) return;
  const cancelLabel = _a11yTranslate('a11y_response_cancelled', 'Response cancelled');
  announceA11yPolite(cancelLabel, 3000);
}

function a11yOnStreamError(sessionId, errorMessage) {
  const isActive = _a11yIsActiveSession(sessionId);
  a11yResetStreamState(sessionId);
  if (!isActive) return;
  const detail = String(errorMessage || '').trim();
  const fallback = _a11yTranslate('a11y_response_error', 'Response interrupted by an error');
  announceA11yAssertive(detail ? `${fallback}: ${detail}` : fallback, 4500);
}

if (typeof window !== 'undefined') {
  window.HermesA11y = Object.freeze({
    announcePolite: announceA11yPolite,
    announceAssertive: announceA11yAssertive,
    restoreFocus: a11yRestoreFocus,
    preserveFocusAcross: a11yPreserveFocusAcross,
    setStreamBusy: a11ySetStreamBusy,
    resetStreamState: a11yResetStreamState,
    onStreamStart: a11yOnStreamStart,
    onToolStart: a11yOnToolStart,
    onToolComplete: a11yOnToolComplete,
    onStreamDone: a11yOnStreamDone,
    onStreamCancel: a11yOnStreamCancel,
    onStreamError: a11yOnStreamError,
    formatAssistantReplyAnnouncement: a11yFormatAssistantReplyAnnouncement,
  });
}
