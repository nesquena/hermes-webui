// ── i18n: locale bundles and t() helper ──────────────────────────────────────
// To add a new language: add an entry to LOCALES below with all keys translated.
// The language code must match a valid BCP 47 tag (used for speech recognition).
// Keys missing in a non-English locale fall back to English automatically.

const LOCALES = {
  en: {
    _lang: 'en',
    _label: 'English',
    _speech: 'en-US',
    // boot.js
    cancelling: 'Cancelling\u2026',
    cancel_failed: 'Cancel failed: ',
    mic_denied: 'Microphone access denied. Check browser permissions.',
    mic_no_speech: 'No speech detected. Try again.',
    mic_network: 'Speech recognition unavailable.',
    mic_error: 'Voice input error: ',
    session_imported: 'Session imported',
    import_failed: 'Import failed: ',
    import_invalid_json: 'Invalid JSON',
    image_pasted: 'Image pasted: ',
    // messages.js
    edit_message: 'Edit message',
    regenerate: 'Regenerate response',
    copy: 'Copy',
    copied: 'Copied!',
    you: 'You',
    thinking: 'Thinking',
    expand_all: 'Expand all',
    collapse_all: 'Collapse all',
    edit_failed: 'Edit failed: ',
    regen_failed: 'Regenerate failed: ',
    reconnect_active: 'A response is still being generated. Reload when ready?',
    reconnect_finished: 'A response was in progress when you last left. Messages may have updated.',
    untitled: 'Untitled',
    n_messages: (n) => `${n} messages`,
    model_unavailable: ' (unavailable)',
    model_unavailable_title: 'This model is no longer in your current provider list',
    // commands.js
    cmd_help: 'List available commands',
    cmd_clear: 'Clear conversation messages',
    cmd_compact: 'Compress conversation context',
    cmd_model: 'Switch model (e.g. /model gpt-4o)',
    cmd_workspace: 'Switch workspace by name',
    cmd_new: 'Start a new chat session',
    cmd_usage: 'Toggle token usage display on/off',
    cmd_theme: 'Switch theme (dark/light/slate/solarized/monokai/nord/oled)',
    cmd_personality: 'Switch agent personality',
    available_commands: 'Available commands:',
    type_slash: 'Type / to see commands',
    conversation_cleared: 'Conversation cleared',
    model_usage: 'Usage: /model <name>',
    no_model_match: 'No model matching "',
    switched_to: 'Switched to ',
    workspace_usage: 'Usage: /workspace <name>',
    no_workspace_match: 'No workspace matching "',
    switched_workspace: 'Switched to workspace: ',
    workspace_switch_failed: 'Workspace switch failed: ',
    new_session: 'New session created',
    compressing: 'Requesting context compression...',
    token_usage_on: 'Token usage on',
    token_usage_off: 'Token usage off',
    theme_usage: 'Usage: /theme ',
    theme_set: 'Theme: ',
    no_active_session: 'No active session',
    no_personalities: 'No personalities found (add them to ~/.hermes/personalities/)',
    available_personalities: 'Available personalities:',
    personality_switch_hint: '\n\nUse `/personality <name>` to switch, or `/personality none` to clear.',
    personalities_load_failed: 'Failed to load personalities',
    personality_cleared: 'Personality cleared',
    personality_set: 'Personality: ',
    failed_colon: 'Failed: ',
    // ui.js
    no_workspace: 'No workspace',
    // workspace.js
    unsaved_confirm: 'You have unsaved changes in the preview. Discard and navigate?',
    save: 'Save',
    edit: 'Edit',
    save_title: 'Save changes',
    edit_title: 'Edit this file',
    saved: 'Saved',
    save_failed: 'Save failed: ',
    image_load_failed: 'Could not load image',
    file_open_failed: 'Could not open file',
    downloading: (name) => `Downloading ${name}\u2026`,
    double_click_rename: 'Double-click to rename',
    renamed_to: 'Renamed to ',
    rename_failed: 'Rename failed: ',
    delete_title: 'Delete',
    delete_confirm: (name) => `Delete ${name}?`,
    deleted: 'Deleted ',
    delete_failed: 'Delete failed: ',
    new_file_prompt: 'New file name (e.g. notes.md):',
    created: 'Created ',
    create_failed: 'Create failed: ',
    new_folder_prompt: 'New folder name:',
    folder_created: 'Created folder ',
    folder_create_failed: 'Create folder failed: ',
    remove_title: 'Remove',
    empty_dir: '(empty)',
    upload_failed: 'Upload failed: ',
    all_uploads_failed: (n) => `All ${n} upload(s) failed`,
    // settings panel
    settings_title: 'Settings',
    settings_save_btn: 'Save Settings',
    settings_label_model: 'Default Model',
    settings_label_send_key: 'Send Key',
    settings_label_theme: 'Theme',
    settings_label_language: 'Language',
    settings_label_token_usage: 'Show token usage',
    settings_label_cli_sessions: 'Show CLI sessions',
    settings_label_sync_insights: 'Sync to insights',
    settings_label_check_updates: 'Check for updates',
    settings_label_bot_name: 'Assistant Name',
    settings_label_password: 'Access Password',
    settings_saved: 'Settings saved',
    settings_save_failed: 'Save failed: ',
    settings_load_failed: 'Failed to load settings: ',
    settings_saved_pw: 'Settings saved (password set \u2014 login now required)',
    // login page (used server-side via /api/i18n/login endpoint)
    login_title: 'Sign in',
    login_subtitle: 'Enter your password to continue',
    login_placeholder: 'Password',
    login_btn: 'Sign in',
    login_invalid_pw: 'Invalid password',
    login_conn_failed: 'Connection failed',
  },

  zh: {
    _lang: 'zh',
    _label: '\u4e2d\u6587',
    _speech: 'zh-CN',
    // boot.js
    cancelling: '\u6b63\u5728\u53d6\u6d88...',
    cancel_failed: '\u53d6\u6d88\u5931\u8d25\uff1a',
    mic_denied: '\u9ea6\u514b\u98ce\u8bbf\u95ee\u88ab\u62d2\u7edd\uff0c\u8bf7\u68c0\u67e5\u6d4f\u89c8\u5668\u6743\u9650\u3002',
    mic_no_speech: '\u6ca1\u6709\u68c0\u6d4b\u5230\u8bed\u97f3\uff0c\u8bf7\u518d\u8bd5\u4e00\u6b21\u3002',
    mic_network: '\u8bed\u97f3\u8bc6\u522b\u5f53\u524d\u4e0d\u53ef\u7528\u3002',
    mic_error: '\u8bed\u97f3\u8f93\u5165\u51fa\u9519\uff1a',
    session_imported: '\u4f1a\u8bdd\u5df2\u5bfc\u5165',
    import_failed: '\u5bfc\u5165\u5931\u8d25\uff1a',
    import_invalid_json: 'JSON \u65e0\u6548',
    image_pasted: '\u5df2\u7c98\u8d34\u56fe\u7247\uff1a',
    // messages.js
    edit_message: '\u7f16\u8f91\u6d88\u606f',
    regenerate: '\u91cd\u65b0\u751f\u6210\u56de\u590d',
    copy: '\u590d\u5236',
    copied: '\u5df2\u590d\u5236',
    you: '\u4f60',
    thinking: '\u601d\u8003\u8fc7\u7a0b',
    expand_all: '\u5168\u90e8\u5c55\u5f00',
    collapse_all: '\u5168\u90e8\u6298\u53e0',
    edit_failed: '\u7f16\u8f91\u5931\u8d25\uff1a',
    regen_failed: '\u91cd\u65b0\u751f\u6210\u5931\u8d25\uff1a',
    reconnect_active: '\u56de\u590d\u4ecd\u5728\u751f\u6210\u4e2d\uff0c\u51c6\u5907\u597d\u540e\u8981\u91cd\u65b0\u52a0\u8f7d\u5417\uff1f',
    reconnect_finished: '\u4f60\u79bb\u5f00\u65f6\u6709\u56de\u590d\u6b63\u5728\u751f\u6210\uff0c\u6d88\u606f\u5185\u5bb9\u53ef\u80fd\u5df2\u7ecf\u66f4\u65b0\u3002',
    untitled: '\u672a\u547d\u540d',
    n_messages: (n) => `${n} \u6761\u6d88\u606f`,
    model_unavailable: '\uff08\u4e0d\u53ef\u7528\uff09',
    model_unavailable_title: '\u8fd9\u4e2a\u6a21\u578b\u5df2\u7ecf\u4e0d\u5728\u5f53\u524d provider \u5217\u8868\u4e2d',
    // commands.js
    cmd_help: '\u67e5\u770b\u53ef\u7528\u547d\u4ee4',
    cmd_clear: '\u6e05\u7a7a\u5f53\u524d\u5bf9\u8bdd\u6d88\u606f',
    cmd_compact: '\u538b\u7f29\u5bf9\u8bdd\u4e0a\u4e0b\u6587',
    cmd_model: '\u5207\u6362\u6a21\u578b\uff08\u4f8b\u5982 /model gpt-4o\uff09',
    cmd_workspace: '\u6309\u540d\u79f0\u5207\u6362\u5de5\u4f5c\u533a',
    cmd_new: '\u65b0\u5efa\u804a\u5929\u4f1a\u8bdd',
    cmd_usage: '\u5207\u6362 token \u7528\u91cf\u663e\u793a',
    cmd_theme: '\u5207\u6362\u4e3b\u9898\uff08dark/light/slate/solarized/monokai/nord/oled\uff09',
    cmd_personality: '\u5207\u6362 Agent \u4eba\u8bbe',
    available_commands: '\u53ef\u7528\u547d\u4ee4\uff1a',
    type_slash: '\u8f93\u5165 / \u53ef\u67e5\u770b\u547d\u4ee4',
    conversation_cleared: '\u5bf9\u8bdd\u5df2\u6e05\u7a7a',
    model_usage: '\u7528\u6cd5\uff1a/model <name>',
    no_model_match: '\u6ca1\u6709\u5339\u914d\u201c',
    switched_to: '\u5df2\u5207\u6362\u5230 ',
    workspace_usage: '\u7528\u6cd5\uff1a/workspace <name>',
    no_workspace_match: '\u6ca1\u6709\u5339\u914d\u201c',
    switched_workspace: '\u5df2\u5207\u6362\u5de5\u4f5c\u533a\uff1a',
    workspace_switch_failed: '\u5de5\u4f5c\u533a\u5207\u6362\u5931\u8d25\uff1a',
    new_session: '\u5df2\u65b0\u5efa\u4f1a\u8bdd',
    compressing: '\u6b63\u5728\u8bf7\u6c42\u538b\u7f29\u4e0a\u4e0b\u6587...',
    token_usage_on: 'Token \u7528\u91cf\u663e\u793a\u5df2\u5f00\u542f',
    token_usage_off: 'Token \u7528\u91cf\u663e\u793a\u5df2\u5173\u95ed',
    theme_usage: '\u7528\u6cd5\uff1a/theme ',
    theme_set: '\u4e3b\u9898\uff1a',
    no_active_session: '\u5f53\u524d\u6ca1\u6709\u6d3b\u52a8\u4f1a\u8bdd',
    no_personalities: '\u6ca1\u6709\u627e\u5230\u4eba\u8bbe\uff08\u53ef\u6dfb\u52a0\u5230 ~/.hermes/personalities/\uff09',
    available_personalities: '\u53ef\u7528\u4eba\u8bbe\uff1a',
    personality_switch_hint: '\n\n\u4f7f\u7528 `/personality <name>` \u5207\u6362\uff0c\u6216\u7528 `/personality none` \u6e05\u7a7a\u3002',
    personalities_load_failed: '\u52a0\u8f7d\u4eba\u8bbe\u5931\u8d25',
    personality_cleared: '\u4eba\u8bbe\u5df2\u6e05\u7a7a',
    personality_set: '\u5f53\u524d\u4eba\u8bbe\uff1a',
    failed_colon: '\u5931\u8d25\uff1a',
    // ui.js
    no_workspace: '\u672a\u9009\u62e9\u5de5\u4f5c\u533a',
    // workspace.js
    unsaved_confirm: '\u9884\u89c8\u533a\u6709\u672a\u4fdd\u5b58\u4fee\u6539\uff0c\u8981\u653e\u5f03\u66f4\u6539\u5e76\u7ee7\u7eed\u8df3\u8f6c\u5417\uff1f',
    save: '\u4fdd\u5b58',
    edit: '\u7f16\u8f91',
    save_title: '\u4fdd\u5b58\u4fee\u6539',
    edit_title: '\u7f16\u8f91\u6b64\u6587\u4ef6',
    saved: '\u5df2\u4fdd\u5b58',
    save_failed: '\u4fdd\u5b58\u5931\u8d25\uff1a',
    image_load_failed: '\u56fe\u7247\u52a0\u8f7d\u5931\u8d25',
    file_open_failed: '\u65e0\u6cd5\u6253\u5f00\u6587\u4ef6',
    downloading: (name) => `\u6b63\u5728\u4e0b\u8f7d ${name}...`,
    double_click_rename: '\u53cc\u51fb\u91cd\u547d\u540d',
    renamed_to: '\u5df2\u91cd\u547d\u540d\u4e3a ',
    rename_failed: '\u91cd\u547d\u540d\u5931\u8d25\uff1a',
    delete_title: '\u5220\u9664',
    delete_confirm: (name) => `\u8981\u5220\u9664 ${name} \u5417\uff1f`,
    deleted: '\u5df2\u5220\u9664 ',
    delete_failed: '\u5220\u9664\u5931\u8d25\uff1a',
    new_file_prompt: '\u65b0\u6587\u4ef6\u540d\uff08\u4f8b\u5982 notes.md\uff09\uff1a',
    created: '\u5df2\u521b\u5efa ',
    create_failed: '\u521b\u5efa\u5931\u8d25\uff1a',
    new_folder_prompt: '\u65b0\u6587\u4ef6\u5939\u540d\u79f0\uff1a',
    folder_created: '\u5df2\u521b\u5efa\u6587\u4ef6\u5939 ',
    folder_create_failed: '\u521b\u5efa\u6587\u4ef6\u5939\u5931\u8d25\uff1a',
    remove_title: '\u79fb\u9664',
    empty_dir: '(\u7a7a)',
    upload_failed: '\u4e0a\u4f20\u5931\u8d25\uff1a',
    all_uploads_failed: (n) => `${n} \u4e2a\u6587\u4ef6\u5168\u90e8\u4e0a\u4f20\u5931\u8d25`,
    // settings panel
    settings_title: '\u8bbe\u7f6e',
    settings_save_btn: '\u4fdd\u5b58\u8bbe\u7f6e',
    settings_label_model: '\u9ed8\u8ba4\u6a21\u578b',
    settings_label_send_key: '\u53d1\u9001\u5feb\u6377\u952e',
    settings_label_theme: '\u4e3b\u9898',
    settings_label_language: '\u8bed\u8a00',
    settings_label_token_usage: '\u663e\u793a token \u7528\u91cf',
    settings_label_cli_sessions: '\u663e\u793a CLI \u4f1a\u8bdd',
    settings_label_sync_insights: '\u540c\u6b65\u5230 insights',
    settings_label_check_updates: '\u68c0\u67e5\u66f4\u65b0',
    settings_label_bot_name: '\u52a9\u624b\u540d\u79f0',
    settings_label_password: '\u8bbf\u95ee\u5bc6\u7801',
    settings_saved: '\u8bbe\u7f6e\u5df2\u4fdd\u5b58',
    settings_save_failed: '\u4fdd\u5b58\u5931\u8d25\uff1a',
    settings_load_failed: '\u8bbe\u7f6e\u52a0\u8f7d\u5931\u8d25\uff1a',
    settings_saved_pw: '\u8bbe\u7f6e\u5df2\u4fdd\u5b58\uff08\u5bc6\u7801\u5df2\u8bbe\u7f6e\u2014\u73b0\u5728\u9700\u8981\u767b\u5f55\uff09',
    // login page
    login_title: '\u767b\u5f55',
    login_subtitle: '\u8f93\u5165\u5bc6\u7801\u7ee7\u7eed\u4f7f\u7528',
    login_placeholder: '\u5bc6\u7801',
    login_btn: '\u767b\u5f55',
    login_invalid_pw: '\u5bc6\u7801\u9519\u8bef',
    login_conn_failed: '\u8fde\u63a5\u5931\u8d25',
  },
};

// Active locale — defaults to English; overridden by loadLocale() at boot.
let _locale = LOCALES.en;

/**
 * Translate a key. Falls back to English if the key is missing in the active locale.
 * Supports function values (for interpolated strings): call t('key', arg).
 * @param {string} key
 * @param {...*} args - forwarded to function-valued translations
 * @returns {string}
 */
function t(key, ...args) {
  const val = _locale[key] ?? LOCALES.en[key];
  if (val === undefined) return key;  // final fallback: return key itself
  return typeof val === 'function' ? val(...args) : val;
}

/**
 * Switch locale by language code (e.g. 'en', 'zh').
 * Persists to localStorage and updates the <html lang> attribute.
 * @param {string} lang
 */
function setLocale(lang) {
  const resolved = LOCALES[lang] ? lang : 'en';
  _locale = LOCALES[resolved];
  localStorage.setItem('hermes-lang', resolved);
  document.documentElement.lang = _locale._speech || resolved;
}

/**
 * Load locale from localStorage (called once at boot, before DOMContentLoaded).
 * Server-persisted preference is applied later in loadSettingsPanel().
 */
function loadLocale() {
  const saved = localStorage.getItem('hermes-lang');
  setLocale(saved && LOCALES[saved] ? saved : 'en');
}

/**
 * Re-stamp all [data-i18n] elements in the DOM with the current locale.
 * Safe to call at any time — missing keys fall back to English.
 * Call after setLocale() to make static HTML text update without a reload.
 */
function applyLocaleToDOM() {
  document.querySelectorAll('[data-i18n]').forEach(el => {
    const key = el.getAttribute('data-i18n');
    const val = t(key);
    if (val && val !== key) el.textContent = val;
  });
}

// Apply saved locale immediately so there's no flash of English on reload.
loadLocale();
