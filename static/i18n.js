// ── i18n: locale bundles and t() helper ──────────────────────────────────────
// To add a new language: add an entry to LOCALES below with all keys translated.
// The language code must match a valid BCP 47 tag (used for speech recognition).
// Keys missing in a non-English locale fall back to English automatically.

// ── Registry ────────────────────────────────────────────────────────────────
// English is eager: it is the fallback for every missing key, so it can never
// be the thing that fails to load. Every other language lives in
// `static/locales/<code>.js` and is fetched on demand by `ensureLocale()`.
const LOCALES = {
  en: {
    composer_placeholder_read_only: 'Read-only in WebUI',
    composer_read_only_connection: 'How to connect',
    offline_title: 'Connection lost',
    offline_browser_detail: 'Your browser reports that this device is offline.',
    offline_network_detail: 'Hermes is unreachable from this browser right now.',
    offline_autorefresh: 'I will refresh this page automatically when Hermes is reachable again.',
    offline_check_now: 'Check now',
    offline_checking: 'Checking…',
    offline_stream_waiting: 'Connection lost. Waiting to refresh…',
    _lang: 'en',
    _label: 'English',
    remote_image_open: 'Open image',
    remote_image_reason: 'Remote image not loaded automatically. Opens {host} in a new tab.',
    _speech: 'en-US',
    // boot.js
    cancelling: 'Cancelling\u2026',
    cancel_failed: 'Cancel failed.',
    mic_denied: 'Microphone access denied. Check browser permissions.',
    mic_insecure_origin: 'Voice input needs a secure connection. Open Hermes over HTTPS or from localhost to use the microphone.',
    mic_no_speech: 'No speech detected. Try again.',
    mic_network: 'Speech recognition unavailable.',
    mic_error: 'Voice input error: ',
    // Composer voice buttons (#1488 — distinct labels for dictation vs voice mode)
    voice_dictate: 'Dictate',
    voice_dictate_active: 'Stop dictation',
    voice_recording_active: 'Stop recording',
    dictation_append_label: 'Append dictation to composer',
    dictation_append_desc: 'When ON, dictated text is appended to any text already in the composer (with a space if needed). When OFF, dictated text replaces the composer content.',
    voice_mode_toggle: 'Voice mode',
    voice_mode_toggle_active: 'Exit voice mode',
    // Turn-based voice mode (#1333)
    voice_listening: 'Listening…',
    voice_speaking: 'Speaking…',
    voice_thinking: 'Thinking…',
    voice_error: 'Voice not supported in this browser',
    voice_mode_active: 'Voice mode on',
    voice_mode_off: 'Voice mode off',
    session_imported: 'Session imported',
    session_new_tab_other_profile: 'This session belongs to another profile. Open it from the current tab instead.',
    sessions_source_webui: 'WebUI sessions ({0})',
    sessions_source_cli: 'CLI sessions ({0})',
    project_chip_all: 'All',
    project_chip_unassigned: 'Unassigned',
    project_chip_unassigned_title: 'Show conversations not yet assigned to a project',
    import_failed: 'Import failed: ',
    import_invalid_json: 'Invalid JSON',
    image_pasted: 'Image pasted: ',
    text_pasted: 'Pasted text attached as ',
    // messages.js
    edit_message: 'Edit message',
    regenerate: 'Regenerate response',
    copy: 'Copy',
    copied: 'Copied!',
    copy_failed: 'Copy failed',
    selected_text_reply: 'Reply with selection',
    selected_text_reply_title: 'Append selected chat text as quoted context',
    selected_text_refine: 'Refine',
    selected_text_refine_title: 'Start an editable refinement draft from the selection',
    selected_text_refine_instruction: 'Refine instruction:',
    selected_text_reply_appended: 'Selected text added to composer',
    saved_prompts_empty: 'No saved prompts yet.',
    saved_prompts_save_current: 'Save current input as prompt',
    saved_prompts_saved: 'Prompt saved',
    saved_prompts_delete: 'Delete prompt',
    saved_prompts_empty_input: 'Type a prompt to save first',
    context_block_name_default: 'Context',
    context_block_flush_aria: 'Context blocks will be included when you send',
    context_block_rename_hint: 'Click or press Enter to rename',
    context_block_rename_aria: 'Rename context block',
    context_block_remove: 'Remove context block',

    diff_loading: 'Loading diff',
    diff_error: 'Could not load patch file',
    diff_too_large: 'Patch file too large to display inline',
    media_preview_unauthorized: 'Your WebUI session expired. Reload the page, then try again.',
    media_preview_forbidden: 'WebUI could not access this file. Move or copy it into the active workspace, or check file permissions, then try again.',
    media_preview_not_found: 'This file was moved, deleted, or is no longer available.',
    tree_view: 'Tree',
    raw_view: 'Raw',
    parse_failed_note: 'parse failed',
    you: 'You',
    mcp_servers_title: 'MCP Servers',
    mcp_servers_desc: 'View MCP servers configured in config.yaml.',
    mcp_no_servers: 'No MCP servers configured.',
    mcp_runtime_scope_unavailable: 'Live status for this profile is unavailable right now (a chat turn may be running). Refresh when it finishes.',
    mcp_add_server: '+ Add Server',
    mcp_field_name: 'Server Name',
    mcp_transport_label: 'Transport Type',
    mcp_field_command: 'Command',
    mcp_field_args: 'Arguments (comma-separated)',
    mcp_field_url: 'URL',
    mcp_field_timeout: 'Timeout (seconds)',
    mcp_save: 'Save',
    mcp_cancel: 'Cancel',
    mcp_name_required: 'Server name is required.',
    mcp_url_required: 'URL is required for HTTP transport.',
    mcp_command_required: 'Command is required for stdio transport.',
    mcp_saved: 'MCP server saved.',
    mcp_save_failed: 'Failed to save MCP server.',
    mcp_delete_confirm_title: 'Delete MCP Server',
    mcp_delete_confirm_message: 'Delete MCP server "{0}"? This action cannot be undone.',
    mcp_deleted: 'MCP server deleted.',
    mcp_delete_failed: 'Failed to delete MCP server.',
    mcp_load_failed: 'Failed to load MCP servers.',
    mcp_restart_hint: 'Server changes are read-only here for now. Edit config.yaml and restart Hermes for changes to take effect.',
    mcp_toggle_followup: 'Enable/disable controls are intentionally deferred until MCP reload semantics are explicit.',
    mcp_status_active: 'Active',
    mcp_status_configured: 'Configured',
    mcp_status_disabled: 'Disabled',
    mcp_status_invalid_config: 'Invalid config',
    mcp_status_unknown: 'Unknown',
    mcp_tool_count: '{0} tools',
    mcp_enabled_yes: 'Enabled',
    mcp_enabled_no: 'Disabled',
    mcp_enable_server: 'Enable this MCP server',
    mcp_disable_server: 'Disable this MCP server',
    mcp_enabled_toast: (name) => `MCP server "${name}" enabled.`,
    mcp_disabled_toast: (name) => `MCP server "${name}" disabled.`,
    mcp_toggle_failed: 'Failed to update MCP server.',
    mcp_tools_title: 'MCP Tools',
    mcp_tools_desc: 'Search known tools across active MCP servers.',
    mcp_tools_search_placeholder: 'Search tools by name, server, or description…',
    mcp_tools_no_tools: 'No MCP tools are available from the active runtime inventory.',
    mcp_tools_no_matches: 'No MCP tools match your search.',
    mcp_tools_load_failed: 'Failed to load MCP tools.',
    mcp_tools_schema_empty: 'No schema parameters.',
    mcp_tools_runtime_note: 'Tool inventory only uses already-known active MCP runtime data; the WebUI does not start or probe servers.',
    mcp_tools_summary_no_matches: (query,total) => `No MCP tools matching “${query}” (${total} total MCP tools).`,
    mcp_tools_summary_none: 'No MCP tools to show.',
    mcp_tools_summary_matching: (query) => ` matching “${query}”`,
    mcp_tools_summary_total_note: (total) => ` (${total} total MCP tools)`,
    mcp_tools_summary_showing: (start,end,filtered,searchNote,totalNote,page,pages) => `Showing ${start}-${end} of ${filtered} MCP tools${searchNote}${totalNote}. Page ${page} of ${pages}.`,
    mcp_tools_page_size_prefix: 'Show',
    mcp_tools_page_size_suffix: 'per page',
    mcp_tools_per_page_aria: 'MCP tools per page',
    mcp_tools_inactive_configured_servers: (servers) => `Configured but inactive in this WebUI runtime: ${servers}.`,
    mcp_tools_pagination_label: 'MCP tools pagination',
    mcp_tools_previous_page: '‹ Prev',
    mcp_tools_previous_page_aria: 'Previous MCP tools page',
    mcp_tools_next_page: 'Next ›',
    mcp_tools_next_page_aria: 'Next MCP tools page',
    // PDF preview (#480)
    pdf_loading: 'Loading PDF {0}…',
    pdf_too_large: 'PDF too large for inline preview',
    pdf_no_pages: 'PDF has no pages',
    pdf_error: 'Failed to render PDF preview',
    pdf_download: 'Download PDF',
    pdf_truncated: 'Showing the first {0} of {1} pages — download for the full document',
    // HTML sandbox preview (#482)
    html_loading: 'Loading HTML preview…',
    html_too_large: 'HTML too large for inline preview',
    html_error: 'Failed to render HTML preview',
    html_open_full: 'Open full page',
    html_sandbox_label: 'HTML Preview (sandboxed)',
    thinking: 'Thinking',
    expand_all: 'Expand all',
    collapse_all: 'Collapse all',
    show_earlier_step_one: 'Show 1 earlier step',
    show_earlier_steps: 'Show {0} earlier steps',
    edit_failed: 'Edit failed: ',
    regen_failed: 'Regenerate failed: ',
    reconnect_active: 'A response is still being generated. Reload when ready?',
    reconnect_finished: 'A response was in progress when you last left. Messages may have updated.',
    // approval card
    approval_heading: 'Approval required',
    approval_desc_prefix: 'Dangerous command detected',
    approval_btn_once: 'Allow once',
    approval_btn_once_title: 'Allow this one command (Enter)',
    approval_btn_session: 'Allow session',
    approval_btn_session_title: 'Allow for this conversation session',
    approval_btn_always: 'Always allow',
    approval_btn_always_title: 'Always allow this command pattern',
    approval_btn_deny: 'Deny',
    approval_btn_deny_title: 'Deny — do not run this command',
    approval_responding: 'Responding\u2026',
    approval_gateway_unsupported: 'Approvals require a newer gateway. Upgrade the connected Hermes gateway to enable this.',
    approval_gateway_unsupported_label: 'Approvals not supported',
    approval_pending_count: (n) => `1 of ${n} pending`,
    clarify_heading: 'Clarification needed',
    clarify_hint: 'Pick a choice, or type your own answer below.',
    clarify_other: 'Other',
    clarify_send: 'Send',
    clarify_input_placeholder: 'Type your response…',
    clarify_responding: 'Responding\u2026',
    session_attention_approval: (n) => n === 1 ? 'Approval' : `${n} approvals`,
    session_attention_clarify: (n) => n === 1 ? 'Question' : `${n} questions`,
    session_attention_generic: (n) => n === 1 ? 'Attention' : `${n} items`,
    session_attention_approval_title: 'Waiting for permission decision',
    session_attention_clarify_title: 'Waiting for your answer',
    session_attention_generic_title: 'Waiting for user action',
    untitled: 'Untitled',
    n_messages: (n) => `${n} messages`,
    load_older_messages: '↑ Scroll up or click to load older messages',
    session_jump_start: 'Start',
    session_jump_start_label: 'Jump to beginning of session',
    session_jump_end: 'End',
    session_jump_end_label: 'Jump to end of session',
    session_new_message: 'New message',
    session_new_message_label: 'New message available, jump to end',
    jump_to_question: 'to response',
    jump_to_question_label: 'Jump to the start of this response',
    queued_label: 'Sends after response',
    queued_count: (n) => n === 1 ? '1 queued' : `${n} queued`,
    queued_cancel: 'Cancel queued message',
    model_unavailable: ' (unavailable)',
    model_unavailable_title: 'This model is no longer in your current provider list',
    provider_mismatch_warning: (m,p)=>`"${m}" may not work with your configured provider (${p}). Send anyway, or run \`hermes model\` in your terminal to switch.`,
    provider_mismatch_label: 'Provider mismatch',
    gateway_auth_label: 'Gateway authentication failed',
    gateway_start: 'Start',
    gateway_stop: 'Stop',
    gateway_restart: 'Restart',
    gateway_start_success: 'Gateway start completed.',
    gateway_stop_success: 'Gateway stop completed.',
    gateway_restart_success: 'Gateway restart completed.',
    gateway_start_failed: 'Gateway start failed',
    gateway_stop_failed: 'Gateway stop failed',
    gateway_restart_failed: 'Gateway restart failed',
    gateway_running: 'Running',
    gateway_session: 'session',
    gateway_sessions: 'sessions',
    gateway_status_load_failed: 'Failed to load gateway status',
    gateway_external_managed: 'This Gateway is managed outside WebUI. Start, stop, or restart it where it runs.',
    gateway_local_unproven: 'WebUI hasn\'t confirmed this Gateway runs on this machine. If it does, start it once with {cmd}; WebUI can manage it after that.',
    model_not_found_label: 'Model not found',
    model_custom_label: 'Custom model ID',
    model_custom_placeholder: 'e.g. openai/gpt-5.4',
    model_show_all_models: 'Show all {0} models',
    model_search_placeholder: 'Search models…',
    model_search_no_results: 'No models found',
    model_group_configured: 'Configured',
    model_badge_selected: 'Selected',
    ws_search_placeholder: 'Search workspaces…',
    ws_no_results: 'No workspaces found',
    workspace_switcher_aria: 'Switch workspace. Current workspace: {0}.',
    new_session_workspace_announce: 'New chat started in workspace: {0}.',
    workspace_new_worktree_conversation: 'New conversation in worktree',
    workspace_new_worktree_conversation_meta: 'Create an isolated git worktree for this workspace.',
    workspace_worktree_created: 'Worktree conversation created',
    workspace_worktree_failed: 'Worktree creation failed: ',
    session_worktree_badge: 'Worktree',
    model_scope_advisory: 'Applies to this conversation from your next message.',
    model_scope_toast: 'Applies to this conversation from your next message.',
    // commands.js
    cmd_clear: 'Clear conversation messages',
    cmd_help: 'Show available slash commands',
    cmd_compress: 'Manually compress conversation context (usage: /compress [focus topic])',
    ctx_compress_hint: 'Compress context to free up space →',
    ctx_compress_action: '⚠ Compress now to free context',
    cmd_compact_alias: 'Legacy alias for /compress',
    cmd_model: 'Switch model (e.g. /model gpt-4o)',
    cmd_workspace: 'Switch workspace by name',
    cmd_terminal: 'Open the workspace terminal',
    cmd_new: 'Start a new chat session',
    cmd_usage: 'Toggle token usage display on/off',
    cmd_theme: 'Switch appearance (theme: system/dark/light, skin: default/ares/mono/graphite/slate/poseidon/sisyphus/charizard/sienna/catppuccin/nous/geist-contrast/zeus/verdigris)',
    cmd_personality: 'Switch agent personality',
    cmd_skills: 'List available Hermes skills',
    cmd_use: 'Force a skill for the next message',
    available_commands: 'Available commands:',
    type_slash: 'Type / to see commands',
    conversation_cleared: 'Conversation cleared',
    command_label: 'Command',
    context_compaction_label: 'Context compaction',
    retrieval_context_label: 'Indexed context',
    retrieval_context_preview: 'Earlier messages are stored and retrievable with context tools',
    preserved_task_list_label: 'Preserved task list',
    reference_only_label: 'Reference only',
    model_usage: 'Usage: /model <name>',
    no_model_match: 'No model matching "',
    model_did_you_mean: (m) => ` — did you mean "${m}"?`,
    switched_to: 'Switched to ',
    workspace_usage: 'Usage: /workspace <name>',
    no_workspace_match: 'No workspace matching "',
    switched_workspace: 'Switched to workspace: ',
    workspace_switch_failed: 'Workspace switch failed: ',
    new_session: 'New session created',
    new_session_creating: 'Creating new conversation…',
    compressing: 'Requesting context compression...',
    compress_running_label: 'Compressing',
    compress_complete_label: 'Compression complete',
    auto_compress_label: 'Automatic compression',
    compress_failed_label: 'Compression failed',
    focus_label: 'Focus',
    token_usage_on: 'Token usage on',
    token_usage_off: 'Token usage off',
    usage_cache_hit_detail: 'Cache: {0}% hit ({1} read / {2} write)',
    usage_cached_percent: '{0}% cached',
    theme_usage: 'Usage: /theme ',
    theme_set: 'Theme: ',
    no_active_session: 'No active session',
    cmd_queue: 'Queue a message for the next turn',
    cmd_goal: 'Set or inspect a persistent goal',
    goal_evaluating_progress: 'Evaluating goal progress…',
    goal_working_toward: 'Working toward goal…',
    goal_continuing_toast: 'Continuing toward goal…',
    goal_status_none: 'No active goal. Set one with /goal <text>.',
    goal_status_active: (turns, max_turns, goal) => `⊙ Goal (active, ${turns}/${max_turns} turns): ${goal}`,
    goal_status_paused: (turns, max_turns, reason, goal) => `⏸ Goal (paused, ${turns}/${max_turns}${reason ? `, ${reason}` : ''}): ${goal}`,
    goal_status_done: (turns, max_turns, goal) => `✓ Goal done (${turns}/${max_turns}): ${goal}`,
    goal_set: (turns, goal) => `⊙ Goal set (${turns}-turn budget): ${goal}`,
    goal_paused: (goal) => `⏸ Goal paused: ${goal}`,
    goal_resumed: (goal) => `▶ Goal resumed: ${goal}`,
    goal_cleared: 'Goal cleared.',
    goal_no_goal: 'No active goal.',
    goal_achieved: (reason) => `✓ Goal achieved: ${reason}`,
    goal_paused_budget_exhausted: (turns, max_turns) => `⏸ Goal paused — ${turns}/${max_turns} turns used. Use /goal resume to keep going, or /goal clear to stop.`,
    goal_continuing: (turns, max_turns, reason) => `↻ Continuing toward goal (${turns}/${max_turns}): ${reason}`,
    cmd_interrupt: 'Cancel current turn and send a new message',
    cmd_steer: 'Inject a mid-turn correction without interrupting the agent',
    cmd_queue_no_msg: 'Usage: /queue <message>',
    cmd_queue_not_busy: 'No active task — just send normally',
    cmd_queue_confirm: 'Message queued',
    cmd_interrupt_no_msg: 'Usage: /interrupt <message>',
    cmd_interrupt_confirm: 'Interrupted — sending new message',
    cmd_steer_no_msg: 'Usage: /steer <message>',
    cmd_steer_fallback: 'Steer unavailable — draft restored',
    cmd_steer_delivered: 'Steer delivered — agent will see it on its next tool result',
    steer_leftover_queued: 'Steer queued for next turn',
    busy_steer_fallback: 'Steer unavailable — draft restored',
    steer_fail_no_cached_agent: 'No cached agent is available for steering this run',
    steer_fail_agent_lacks_steer: 'This agent version does not support steer',
    steer_fail_session_not_found: 'Session not found — it may have expired',
    steer_fail_not_running: 'Agent is not currently running',
    steer_fail_stream_dead: 'The agent stream has ended',
    steer_fail_steer_error: 'Steer delivery failed — the agent may have finished',
    steer_fail_network_error: 'Network error — check your connection',
    steer_fail_gateway_steer_uncertain: 'Steer may have reached the agent (no reply from Gateway); your text is kept in the composer',
    steer_fail_unknown: 'Steer unavailable',
    steer_recovery_retry: 'Retry',
    steer_recovery_dismiss: 'Dismiss',
    busy_interrupt_confirm: 'Interrupted — sending new message',
    settings_label_default_message_mode: 'Default message mode',
    settings_desc_default_message_mode: 'Controls what happens when you send a message while the agent is running. Queue waits; Interrupt cancels and starts fresh; Steer injects a correction mid-turn without interrupting. If Steer is unavailable, the draft is restored so you can choose the next action.',
    settings_label_fade_text_effect: 'Fade text effect',
    settings_desc_fade_text_effect: 'Fade newly streamed words in while the assistant is responding. Similar to OpenWebUI; off by default for maximum performance.',
    settings_label_max_tokens: 'Max output tokens',
    settings_desc_max_tokens: 'Caps assistant output for new streaming turns. Leave blank to use the active profile config fallback.',
    settings_placeholder_max_tokens_none: 'No override',
    settings_placeholder_max_tokens_fallback: 'No override (fallback: {0})',
    settings_default_message_mode_queue: 'Queue follow-up',
    settings_default_message_mode_interrupt: 'Interrupt current turn',
    settings_default_message_mode_steer: 'Steer (mid-turn correction)',
    settings_label_busy_placeholder_hint: 'Show busy placeholder hint',
    settings_label_new_chat_on_workspace_switch: 'Start a new chat when switching workspace',
    settings_desc_new_chat_on_workspace_switch: 'When on, selecting a different workspace starts a fresh conversation bound to that workspace and leaves your current chat on its original workspace, avoiding stale cross-workspace context. When off (default), switching workspace changes the current workspace in place.',
    workspace_switched_new_chat: 'Started a new chat in {0}',
    settings_desc_busy_placeholder_hint: 'Shows the composer placeholder with the current busy action while the session is running and the draft is empty.',
    composer_placeholder_busy_queue: 'Enter = queue | /interrupt | /background | /steer',
    composer_placeholder_busy_interrupt: 'Enter = interrupt | /queue | /background | /steer',
    composer_placeholder_busy_steer: 'Enter = steer | /queue | /background | /interrupt',

  slash_skill_badge:'Skill',
  slash_skill_desc:'Invoke this skill',
  cmd_stop:'Stop the current response',
  cmd_title:'Get or set the session title',
  cmd_retry:'Resend the last message',
  cmd_undo:'Remove the last exchange',
  cmd_btw:'Ask a side question (ephemeral)',
  cmd_btw_usage:'/btw <question> — ask a side question using session context',
   cmd_background:'Run a prompt in background',
   cmd_background_usage:'/background <prompt> — run in parallel without blocking',
   cmd_branch:'Fork this conversation into a new session',
   cmd_branch_usage:'/branch [name] — fork conversation (optionally with a name)',
   branch_forked:'Forked into new session',
   branch_failed:'Fork failed: ',
   fork_from_here:'Fork from here',
   forked_from:'Forked from',
   subagent_children:'Subagent sessions',
   btw_asking:'Asking side question...',
  btw_label:'Side question — not in history',
  btw_done:'Side question answered',
  btw_no_answer:'No answer received.',
  btw_failed:'Side question failed: ',
  bg_running:'Running in background...',
  bg_complete:'Background task complete',
  bg_label:'Background result:',
  bg_no_answer:'(no answer)',
  bg_failed:'Background task failed: ',
  process_wakeup_label:'Background wakeup',
  process_wakeup_matched:'Watch pattern matched',
  undo_exchange:'Undo last exchange',
  cmd_status:'Show session info',
  cmd_voice:'Toggle microphone input',
  stream_stopped:'Response stopped.',
  no_active_task:'No active task to stop.',
  cancel_unavailable:'Cancel not available.',
  retry_failed:'Retry failed: ',
  undo_failed:'Undo failed: ',
  undid_n_messages:'Removed',
  undid_messages_suffix:'message(s).',
  status_heading:'Session Status',
  status_session_id:'Session ID',
  status_title:'Title',
  status_model:'Model',
  status_provider:'Provider',
  status_workspace:'Workspace',
  status_personality:'Personality',
  status_messages:'Messages',
  status_messages_total: 'Total messages',
  status_agent_running:'Agent running',
    status_profile: 'Profile',
    status_hermes_home: 'Hermes home',
    status_started: 'Started',
    status_updated: 'Updated',
    status_tokens: 'Tokens',
    status_ephemeral: 'Ephemeral snapshot — not saved to transcript history.',
    status_no_tokens: 'No tokens used',
    status_unknown: 'Unknown',
  status_yes:'Yes',
  status_no:'No',
  status_load_failed:'Failed to load status: ',
  title_current:'Current title',
  title_change_hint:'Use `/title <new name>` to rename.',
  title_set:'Title set to',
  cmd_webui_only_session:'This command is not available for CLI-imported sessions.',
  cmd_voice_use_mic:'Click the mic button in the composer.',
  usage_heading:'Token Usage',
  usage_default_model:'default',
  usage_unknown:'unknown',
  usage_input_tokens:'Input tokens',
  usage_output_tokens:'Output tokens',
  usage_total:'Total tokens',
  usage_estimated_cost:'Estimated cost',
  usage_settings_tip:'Note: cost estimates are approximate.',
  usage_load_failed:'Failed to load usage: ',
    usage_personality_none:'none',
    // Session toolsets (#493)
    session_toolsets:'Session Toolsets',
    session_toolsets_desc:'Use active profile defaults or choose a custom toolset list for this session',
    session_toolsets_global:'Active profile defaults',
    session_toolsets_profile_defaults:'Active profile defaults',
    session_toolsets_custom:'Custom override',
    session_toolsets_use_profile_defaults:'Use active profile defaults',
    session_toolsets_configured_servers:'Configured MCP servers',
    session_toolsets_loading_servers:'Loading configured servers...',
    session_toolsets_no_configured_servers:'No configured MCP servers',
    session_toolsets_placeholder:'tool1, tool2, …',
    session_toolsets_apply:'Apply',
    session_toolsets_clear:'Use defaults',
    session_toolsets_applied:'Toolsets updated',
    session_toolsets_cleared:'Using active profile defaults',
    session_toolsets_failed:'Failed to update toolsets: ',
  untitled:'Untitled',
    no_personalities: 'No personalities found (add them to ~/.hermes/personalities/)',
    available_personalities: 'Available personalities:',
    personality_switch_hint: '\n\nUse `/personality <name>` to switch, or `/personality none` to clear.',
    personalities_load_failed: 'Failed to load personalities',
    personality_cleared: 'Personality cleared',
    personality_set: 'Personality: ',
    failed_colon: 'Failed: ',
    // ui.js
    no_workspace: 'No workspace',
    terminal_open_title: 'Open workspace terminal',
    terminal_no_workspace_title: 'Select a workspace to open terminal',
    terminal_title: 'Terminal',
    terminal_clear: 'Clear',
    terminal_copy_output: 'Copy output',
    terminal_restart: 'Restart',
    terminal_collapse: 'Collapse',
    terminal_expand: 'Expand',
    terminal_close: 'Close',
    terminal_input_placeholder: 'Run a command...',
    terminal_start_failed: 'Terminal start failed: ',
    terminal_input_failed: 'Terminal input failed: ',
    terminal_copy_failed: 'Copy failed: ',
    terminal_error: 'Terminal error',
    terminal_remote_backend_unsupported: 'Embedded terminal is only supported for local terminal backends.',
    workspace_empty_no_path: 'No workspace selected. Set a workspace in Settings \u2192 Workspace to browse files.',
    workspace_empty_dir: 'This workspace is empty.',
    workspace_show_hidden_files: 'Show hidden files',
    workspace_sort_by: 'Sort by',
    workspace_sort_name_asc: 'Name (A → Z)',
    workspace_sort_name_desc: 'Name (Z → A)',
    workspace_sort_created_desc: 'Date created (newest first)',
    workspace_sort_modified_desc: 'Date modified (newest first)',
    workspace_sort_created_unavailable: 'Creation time is not reported by this server or platform.',
    workspace_show_hidden_files_desc: 'Include .DS_Store, .git, node_modules, and other hidden / system files in the file tree.',
    workspace_panel_show: 'Show workspace panel',
    workspace_panel_hide: 'Hide workspace panel',
    workspace_panel_close: 'Close workspace panel',
    workspace_panel_toggle: 'Toggle workspace files panel',
    workspace_panel_views: 'Workspace panel views',
    workspace_parent_directory: 'Parent directory',
    workspace_upload_file: 'Upload file',
    workspace_close_preview: 'Close preview',
    workspace_files_tab: 'Files',
    workspace_artifacts_tab: 'Artifacts',
    workspace_artifact_files_changed: 'Files changed',
    workspace_artifact_source_session: 'session',
    workspace_hidden_files_visible: 'hidden visible',
    workspace_hidden_files_visible_title: 'Hidden files are visible — click for options',
    workspace_options: 'Workspace options',
    dialog_confirm_title: 'Confirm action',
    dialog_prompt_title: 'Enter a value',
    dialog_confirm_btn: 'Confirm',
    // workspace.js
    unsaved_confirm: 'You have unsaved changes in the preview. Discard and navigate?',
    discard: 'Discard',
    save: 'Save',
    edit: 'Edit',
    clear: 'Clear',
    create: 'Create',
    remove: 'Remove',
    save_title: 'Save changes',
    edit_title: 'Edit this file',
    saved: 'Saved',
    save_failed: 'Save failed: ',
    image_load_failed: 'Could not load image',
    file_open_failed: 'Could not open file',
    downloading: (name) => `Downloading ${name}\u2026`,
    double_click_rename: 'Double-click to rename',
    symlink_link_to: 'Symlink → {target}',
    external_link_open_confirm: 'This symlink points outside the workspace. Open it in a read-only external view for this browser session?',
    external_link_read_only: 'This external view is read-only. Create, rename, move, delete, upload, reveal, and editor actions stay disabled here.',
    external_link_grant_expired: 'This external view expired. Reopen the escape-target symlink to authorize it again.',
    session_rename_failed_no_row: 'Could not start rename — row not found.',
    renamed_to: 'Renamed to ',
    rename_failed: 'Rename failed: ',
    moved_to: 'Moved to ',
    move_failed: 'Move failed: ',
    delete_title: 'Delete',
    delete_entry_aria: (name) => `Delete ${name}`,
    tree_external_link_aria: 'external link',
    tree_file_aria: 'file',
    tree_folder_aria: 'folder',
    tree_symlink_aria: 'symlink',
    tree_row_aria: (name, kind, size) => size ? `${name} ${kind}, ${size}` : `${name} ${kind}`,
    workspace_tree_aria: 'Workspace files',
    delete_confirm: (name) => `Delete ${name}?`,
    delete_dir_confirm: (name) => `Delete folder "${name}" and all its contents?`,
    rename_title: 'Rename',
    rename_prompt: 'New name:',
    deleted: 'Deleted ',
    delete_failed: 'Delete failed: ',
    session_delete_busy: 'This conversation is still running. Stop it first.',
    session_delete_unknown_execution: 'This conversation is still finishing background work. Try again in a moment.',
    session_delete_ledger_unavailable: 'This conversation\'s runtime history cannot be cleared right now. Restart the agent and try again.',
    session_delete_cleanup_failed: 'This conversation could not be deleted; its files were kept. Restart the agent and try again.',
     reveal_in_finder: 'Reveal in File Manager',
     reveal_failed: 'Failed to reveal: ',
     copy_file_path: 'Copy file path',
     copy_relative_path: 'Copy relative path',
     open_in_vscode: 'Open in VS Code',
     open_in_vscode_failed: 'Failed to open in VS Code: ',
     download_folder: 'Download Folder',
    path_copied: 'File path copied to clipboard',
    path_copy_failed: 'Failed to copy path: ',
    session_rename: 'Rename conversation',
    session_rename_desc: 'Edit the title of this conversation',
    session_title_regenerate: 'Regenerate title',
    session_title_regenerate_desc: 'Ask Hermes to generate a fresh title from the conversation',
    session_title_regenerating: 'Regenerating title…',
    session_title_regenerated: (title) => `Title regenerated: ${title}`,
    session_title_regenerate_failed: 'Regenerate title failed: ',
    session_copy_link: 'Copy conversation link',
    session_copy_link_desc: 'Copy a direct link to this conversation',
    session_link_copied: 'Conversation link copied to clipboard',
    session_link_copy_failed: 'Failed to copy conversation link: ',
    share_session: 'Share',
    share_session_tooltip: 'Create a public read-only share link',
    share_session_status_active: 'Public share active',
    share_session_existing_confirm: 'This conversation already has a public read-only share link.\n\nChoose Copy existing to reuse that link, or Refresh snapshot to rebuild the public snapshot from the latest visible messages.',
    share_session_copy_existing: 'Copy existing',
    share_session_refresh_snapshot: 'Refresh snapshot',
    share_session_link_copied: 'Share link copied to clipboard',
    share_session_created: 'Public share link created',
    share_session_failed: 'Share failed: ',
    stop_sharing_session: 'Stop sharing',
    stop_sharing_session_tooltip: 'Revoke the public read-only share link for this conversation',
    stop_sharing_session_confirm: 'Revoke this public share link? Anyone opening the old link will see that it is no longer available.',
    share_session_revoked: 'Public share link revoked',
    share_session_revoke_failed: 'Failed to revoke share link: ',
    new_file_prompt: 'New file name (e.g. notes.md):',
    new_file: 'New File',
    new_folder: 'New Folder',
    workspace_root: 'workspace root',
    new_file_prompt_title: 'New file name in {0}:',
    new_folder_prompt_title: 'New folder name in {0}:',
    project_name_prompt: 'Project name:',
    created: 'Created ',
    create_failed: 'Create failed: ',
    new_folder_prompt: 'New folder name:',
    folder_created: 'Created folder ',
    folder_create_failed: 'Create folder failed: ',
  workspace_auto_create_folder: 'Create folder if it doesn\'t exist',
  folder_add_as_space_btn: 'Add as Space',
  folder_add_as_space_msg: 'Add this folder as a new space in your workspace list?',
  folder_add_as_space_title: 'Add as Space?',
    remove_title: 'Remove',
    empty_dir: '(empty)',
    upload_failed: 'Upload failed: ',
    upload_too_large: (maxMb, fileMb) => `File is too large (${fileMb} MB). Maximum upload size is ${maxMb} MB.`,
    uploading: 'Uploading…',
    uploaded: 'Uploaded',
    all_uploads_failed: (n) => `All ${n} upload(s) failed`,
    archive_extracted: (n, c) => `Extracted ${n} file(s) from ${c} archive(s)`,
    session_pin: 'Pin conversation',
    session_unpin: 'Unpin conversation',
    session_pin_desc: 'Keep this conversation at the top',
    session_unpin_desc: 'Remove from pinned',
    session_pin_failed: 'Pin failed: ',
    session_move_project: 'Move to project',
    session_move_project_desc_has: 'Change the project for this conversation',
    session_move_project_desc_none: 'Assign a project to this conversation',
    project_picker_none: 'No project',
    project_picker_new: '+ New project',
    session_archive: 'Archive conversation',
    session_hide_external: 'Hide from list',
    session_restore: 'Restore conversation',
    session_archive_desc: 'Hide this conversation until archived is shown',
    session_archive_worktree_desc: 'Hide this conversation; keep its worktree on disk',
    session_hide_external_desc: 'Hide this imported session from WebUI without deleting the source history.',
    session_restore_desc: 'Bring this conversation back into the main list',
    session_archived: 'Session archived',
    session_archived_worktree: 'Session archived. Worktree remains on disk.',
    session_hidden: 'Session hidden from list',
    session_restored: 'Session restored',
    session_archive_failed: 'Archive failed: ',
    session_duplicate: 'Duplicate conversation',
    session_duplicate_desc: 'Create a copy with the same workspace and model',
    session_export_html: 'Export as HTML',
    session_export_html_desc: 'Download this conversation as a self-contained HTML file',
    session_duplicated: 'Session duplicated',
    session_duplicate_failed: 'Duplicate failed: ',
    session_stop_response: 'Stop response',
    session_stop_response_desc: 'Cancel the running response for this conversation',
    session_delete: 'Delete conversation',
    session_delete_desc: 'Permanently remove this conversation',
    session_delete_confirm: 'Delete this conversation?',
    session_delete_worktree_desc: 'Delete only the WebUI conversation; keep the worktree on disk',
    session_delete_worktree_confirm: (path) => `Delete this conversation? The worktree at ${path} will remain on disk.`,
    session_deleted: 'Conversation deleted',
    session_deleted_worktree: 'Conversation deleted. Worktree remains on disk.',
    session_worktree_remove: 'Remove worktree',
    session_worktree_remove_desc: (path) => `Delete the git worktree at ${path} from disk`,
    session_worktree_remove_confirm: (path) => `Remove git worktree from disk?\n\nPath: ${path}\n\nThis will delete the entire worktree directory. Session data remains in WebUI.`,
    session_worktree_remove_not_exists: (path) => `The worktree at ${path} no longer exists on disk.`,
    session_worktree_remove_confirm_label: 'Remove',
    session_worktree_removed: 'Worktree removed.',
    session_worktree_remove_failed: 'Failed to remove worktree: ',
    session_worktree_remove_status_failed: 'Failed to read worktree status: ',
    session_worktree_remove_locked_by_stream: 'Cannot remove — an active streaming session is using this worktree.',
    session_worktree_remove_locked_by_terminal: 'Cannot remove — an active terminal session is using this worktree.',
    session_worktree_remove_unsafe_blocked: 'Resolve local changes or unpushed commits before removing this worktree.',
    session_worktree_remove_dirty_warning: 'WARNING: This worktree has uncommitted changes which will be lost.',
    session_worktree_remove_untracked_warning: (count) => `${count} untracked file(s) will be permanently deleted.`,
    session_worktree_remove_ahead_warning: (ahead) => `${ahead} unpushed commit(s) will be lost.`,
    session_select_mode: 'Select',
    session_select_mode_desc: 'Select conversations to batch manage',
    session_select_all: 'Select all',
    session_deselect_all: 'Deselect all',
    session_selected_count: '{0} selected',
    session_batch_archive: 'Archive',
    session_batch_delete: 'Delete',
    session_batch_move: 'Move to project',
    session_batch_delete_confirm: 'Delete {0} conversations?',
    session_batch_archive_confirm: 'Archive {0} conversations?',
    session_batch_delete_worktree_confirm: 'Delete {0} conversations? {1} worktree-backed conversation(s) will leave their worktree directories on disk.',
    session_batch_archive_worktree_confirm: 'Archive {0} conversations? {1} worktree-backed conversation(s) will keep their worktree directories on disk.',
    session_no_selection: 'No conversations selected',
    // settings panel
    settings_heading_title: 'Control Center',
    settings_heading_subtitle: 'Preferences, conversation tools, and system controls.',
    settings_section_conversation_title: 'Conversation',
    settings_section_appearance_title: 'Appearance',
    settings_section_appearance_meta: 'Theme, accent colors, and visual style.',
    settings_section_preferences_title: 'Preferences',
    settings_section_preferences_meta: 'Defaults and UI behavior for Hermes Web UI.',
    settings_section_system_title: 'System',
    settings_section_system_meta: 'Instance version and access controls.',
    settings_check_now: 'Check now',
    settings_checking: 'Checking\u2026',
    settings_up_to_date: 'Up to date \u2713',
    update_later: 'Later',
    update_now: 'Update Now',
    update_updating: 'Updating…',
    update_force: 'Force update',
    update_clear_lock_retry: 'Clear lock and retry update',
    update_hard_refresh_now: 'Hard refresh now',
    update_no_target: 'No update target selected. Refresh update status and retry.',
    update_force_active_runs: (n) => `Update anyway (interrupts ${n} run${Number(n) === 1 ? '' : 's'})`,
    update_force_active_runs_title: 'Interrupt active runs?',
    update_force_active_runs_message: (n) => `${n} WebUI agent run${Number(n) === 1 ? ' is' : 's are'} still working. Updating now restarts the server and stops ${Number(n) === 1 ? 'it' : 'them'}; ${Number(n) === 1 ? 'it is' : 'each one is'} saved as interrupted. Wait for ${Number(n) === 1 ? 'it' : 'them'} to finish if you can.`,
    update_force_active_runs_confirm: 'Interrupt and update',
    update_failed_network: 'Update failed: could not reach the WebUI server. It may have restarted or the connection was interrupted. Please wait a few seconds, reload the page, then check the server if it still does not come back.',
    update_failed_prefix: 'Update failed: ',
    // {0} is the target list from _formatUpdateTargetStatus(), count and unit included (#8283)
    settings_updates_available_targets: '{0} available',
    settings_latest_tag_suffix: '·\u00a0Latest\u00a0tag:\u00a0{0}',
    settings_updates_disabled: 'Update checks disabled',
    settings_update_check_failed: 'Update check failed',
    settings_update_no_git: 'Can\'t check for updates (not a git checkout)',
    settings_update_manual_docker: 'Manual update required: run {0}, then recreate the container.',
    settings_label_workspace_panel_open: 'Keep workspace panel open by default',
    settings_desc_workspace_panel_open: 'When enabled, the workspace / file browser panel opens automatically with each new session. You can still close it manually at any time.',
    settings_label_workspace_todos_tab: 'Show Todos tab in workspace panel',
    settings_desc_workspace_todos_tab: 'When enabled, a Todos tab appears in the workspace panel.',
    settings_note_workspace_todos_tab_disabled: 'Disabled while the task list in chat is on.',
    settings_label_session_jump_buttons: 'Show session jump buttons',
    settings_desc_session_jump_buttons: 'Show floating Start and End buttons while reading long session histories.',
    settings_label_screen_reader_mode: 'Screen reader mode',
    settings_desc_screen_reader_mode: 'Optimizes parts of the interface for screen readers. Currently makes terminal output readable and announces new output. Off by default.',
    settings_label_terminal_auto_expand: 'Auto-expand terminal on output',
    settings_desc_terminal_auto_expand: 'Expand the collapsed terminal panel automatically when a running command emits new output.',

    settings_label_session_endless_scroll: 'Load older messages while scrolling up',

    settings_desc_session_endless_scroll: 'When enabled, older messages load automatically as you scroll upward. When disabled, use the older-messages button.',
    settings_label_chat_todos_in_chat: 'Show task list in chat',
    settings_desc_chat_todos_in_chat: 'Show a collapsible task list at the top of the chat area. When enabled, the sidebar Todos panel is hidden to avoid duplication.',
    todos_tray_summary_active: '{0} active · {1} total',
    todos_tray_summary_done: 'All done · {0} total',
    settings_label_auto_scroll_follow: 'Auto-scroll to new content',
    settings_desc_auto_scroll_follow: 'When enabled, the view auto-scrolls to the bottom as new tokens stream in. When disabled, you control the scroll position manually.',
    settings_label_render_user_markdown: 'Render markdown in user messages',
    settings_label_show_titlebar_profile: 'Show profile switcher in titlebar',
    settings_desc_show_titlebar_profile: 'When enabled, a profile switcher button appears in the top-left app titlebar so you can change profiles from any tab. Off by default; the composer footer always has a profile switcher regardless of this setting.',
    settings_desc_render_user_markdown: 'When enabled, bold, italic, links, and other markdown in your own messages are rendered. Off by default; fenced code blocks and math always render regardless of this setting.',
    settings_label_large_text_paste_as_attachment: 'Attach large pasted text as file',
    settings_desc_large_text_paste_as_attachment: 'When enabled, long pasted text becomes a .md attachment instead of filling the composer.',
    settings_label_project_quick_create: 'Show per-project new-conversation buttons',
    settings_desc_project_quick_create: 'When enabled, each project chip in the sidebar gets a + button to start a conversation in that project.',
    settings_label_structured_code: 'JSON/YAML code blocks',
    settings_option_structured_code_auto: 'Auto: Tree for long blocks',
    settings_option_structured_code_on: 'Tree by default',
    settings_option_structured_code_off: 'Raw by default',
    settings_label_structured_code_auto_lines: 'Auto threshold lines',
    settings_desc_structured_code: 'When Auto is selected, JSON/YAML blocks with at least this many lines open in Tree view. Tree always opens the structured viewer; Raw keeps the syntax-highlighted text. You can still switch Tree/Raw on each block.',
    settings_label_worklog_details_expanded_default: 'Open Worklog details automatically',
    settings_desc_worklog_details_expanded_default: 'When enabled, new Worklog details start expanded so tool, thinking, and progress cards are visible without an extra click. When off, Worklog details stay folded by default; per-turn manual collapse/expand choices still win.',
    settings_label_chat_activity_display_mode: 'Activity display',
    settings_desc_chat_activity_display_mode: 'Compact Worklog groups supporting activity into a quiet summary. Transparent Stream keeps each thinking/tool event as its own chronological expandable row. Final answer only hides activity rows while keeping the final answer visible.',
    settings_label_transparent_stream_event_timestamps: 'Show Transparent Stream event timestamps',
    settings_desc_transparent_stream_event_timestamps: 'Keeps the response footer time visible while hiding the per-event timestamp chips.',
    first_token_time: 'Time to first token',
    processed_elapsed: _i18nProcessedElapsedEn,
    worklog_thinking: 'Thinking',
    tool_target_skill_suffix: 'skill',
    tool_action_label: _i18nToolActionLabelEn,
    tool_worklog_summary: _i18nToolWorklogSummaryEn,
    tool_summary_join: _i18nToolSummaryJoinEn,
    done: 'Done',
    settings_option_compact_worklog: 'Compact Worklog',
    settings_option_transparent_stream: 'Transparent Stream',
    settings_option_final_answer_only: 'Final answer only',

    settings_label_tab_visibility: 'Sidebar tabs',
    settings_desc_tab_visibility: 'Choose which tabs appear in the sidebar and rail. Drag chips to reorder them. Chat and Settings are always visible.',
    settings_label_composer_controls: 'Chat footer controls',
    settings_desc_composer_controls: 'Toggle which controls are shown in the chat footer. Drag chips to reorder each footer group.',
    settings_label_composer_situational_controls: 'Situational footer controls',
    settings_desc_composer_situational_controls: 'These controls do not always appear, depending on viewport, mode, or runtime state. You can still toggle them off if needed.',
    composer_control_attach: 'Attach',
    composer_attach_options: 'Attach options',
    composer_attach_photo: 'Take photo',
    composer_attach_files: 'Choose files',
    composer_control_saved_prompts: 'Saved prompts',
    composer_control_mic: 'Mic',
    composer_control_profile: 'Profile',
    composer_control_workspace: 'Workspace',
    composer_control_model: 'Model',
    composer_control_reasoning: 'Reasoning',
    composer_control_context: 'Context',
    composer_control_voice_mode: 'Voice mode',
    composer_control_yolo: 'YOLO',
    composer_control_bg_badge: 'Background badge',
    composer_control_mobile_config: 'Mobile config',
    composer_control_quota_chip: 'Quota chip',
    composer_control_toolsets: 'Toolsets',
    composer_control_status: 'Status',
    open_in_browser: 'Open in browser',
    settings_dropdown_conversation: 'Conversation',
    settings_dropdown_appearance: 'Appearance',
    settings_dropdown_preferences: 'Preferences',
    settings_dropdown_providers: 'Providers',
    settings_dropdown_system: 'System',
    settings_tab_conversation: 'Conversation',
    settings_tab_appearance: 'Appearance',
    settings_tab_preferences: 'Preferences',
    settings_tab_plugins: 'Plugins',
    settings_tab_extensions: 'Extensions',
    settings_extensions_gallery_tab: 'Gallery',
    settings_extensions_installed_tab: 'Installed',
    settings_extensions_diagnostics_tab: 'Diagnostics',
    settings_extensions_gallery_loading: 'Loading gallery…',
    settings_extensions_gallery_empty: 'No extensions found in the registry.',
    settings_extensions_gallery_error: 'Failed to load gallery.',
    ext_gallery_install: 'Install',
    settings_extensions_meta: 'Browse, install, and manage WebUI extensions.',
    settings_extensions_installed_loading: 'Loading installed extensions…',
    settings_extensions_copy_diagnostics: 'Copy diagnostics',
    settings_extensions_trust_model_label: 'Trust model',
    settings_extensions_trust_model_text: 'Extensions run in the WebUI browser origin and can access the same authenticated APIs as this session. Settings and extension-owned storage are browser-local and not for secrets. Only load trusted local extension directories; disabling an extension takes effect after reload for already-injected assets.',
    settings_extensions_runtime_title: 'Extension runtime',
    settings_extensions_runtime_status_from: 'Status from /api/extensions/status; toggles persist a local override for installed manifest entries.',
    settings_extensions_diag_extension_dir_configured: 'Extension dir configured',
    settings_extensions_diag_extension_dir_valid: 'Extension dir valid',
    settings_extensions_diag_manifest_configured: 'Manifest configured',
    settings_extensions_diag_manifest_loaded: 'Manifest loaded',
    settings_extensions_diag_manifest_status: 'Manifest status',
    settings_extensions_diag_manifest_entries: 'Manifest entries inspected',
    settings_extensions_diag_manifest_scripts: 'Manifest script count',
    settings_extensions_diag_manifest_stylesheets: 'Manifest stylesheet count',
    settings_extensions_diag_manifest_sidecars: 'Manifest sidecar count',
    settings_extensions_diag_final_scripts: 'Final script count',
    settings_extensions_diag_final_stylesheets: 'Final stylesheet count',
    settings_extensions_diag_loopback_sidecars: 'Loopback sidecar count',
    settings_extensions_diag_installed_extensions: 'Installed manifest extensions',
    settings_extensions_diag_user_disabled: 'User-disabled extensions',
    settings_extensions_installed_section_title: 'Installed manifest extensions',
    settings_extensions_installed_section_meta: 'Enable or disable already-present local extensions. Reload WebUI to apply injected asset changes to this browser tab.',
    settings_extensions_installed_empty: 'No manifest extensions are installed in the configured bundle.',
    settings_extensions_no_dir: 'No extension directory is configured.',
    ext_none: 'None',
    ext_no_warnings: 'No warnings.',
    ext_state_unknown_ids_hint: 'Some saved disabled-extension overrides no longer match the current manifest; re-added extensions with the same id may stay disabled.',
    ext_assets_title: 'Final public asset URLs',
    ext_assets_meta: 'Same-origin URLs that may be injected into the app shell.',
    ext_assets_scripts: 'Scripts',
    ext_assets_styles: 'Stylesheets',
    ext_warnings_title: 'Sanitized warnings',
    ext_warnings_meta: 'Codes and coarse sources only; paths and rejected values are not shown.',
    ext_sidecars_title: 'Loopback sidecars',
    ext_sidecars_meta: 'Declared local companions; health is checked directly from this browser with WebUI credentials omitted.',
    ext_sidecars_none: 'No loopback sidecars declared.',
    ext_sidecar_origin: 'Origin',
    ext_sidecar_health_path: 'Health path',
    ext_sidecar_health_url: 'Health URL',
    ext_sidecar_proxy: 'Proxy',
    ext_sidecar_proxy_path: 'Proxy path',
    ext_status_true: 'true',
    ext_status_false: 'false',
    ext_gallery_installing: 'Installing…',
    ext_gallery_installed: 'Installed',
    ext_gallery_uninstall: 'Uninstall',
    ext_gallery_uninstalling: 'Uninstalling…',
    ext_gallery_restart_required: 'Restart required',
    ext_gallery_permissions_show: 'Permissions',
    ext_gallery_permissions_hide: 'Hide permissions',
    ext_gallery_permissions_empty: 'No extra permissions declared.',
    ext_gallery_version: 'Version',
    ext_gallery_author: 'Author',
    ext_gallery_capabilities: 'Capabilities',
    ext_gallery_next_step: 'Next step',
    ext_gallery_after_install: 'After install',
    ext_gallery_local_component_required: 'This extension has a local component. Start the required local app or sidecar after installing.',
    ext_gallery_local_app_label: 'Local app',
    ext_gallery_required_suffix: '{0} required',
    ext_gallery_sidecar_required: 'Sidecar required',
    ext_gallery_native_host_required: 'Native host required',
    ext_gallery_open_setup_guide: 'Open setup guide',
    ext_gallery_install_restart_required: 'Extension installed. Restart required to apply.',
    ext_gallery_install_followup: 'Extension installed. See the card for next steps.',
    ext_gallery_install_ok: 'Extension installed successfully.',
    ext_gallery_uninstall_ok: 'Extension uninstalled.',
    settings_plugins_title: 'Plugins',
    plugins_enable_toggle: 'Enable',
    settings_plugins_meta: 'View installed Hermes plugins and the lifecycle hooks they register. This panel is read-only.',
    settings_plugins_empty: 'No Hermes plugins are currently visible. Install or enable plugins from the Hermes CLI/config to see them here.',
    plugins_unnamed: 'Unnamed plugin',
    plugins_no_description: 'No description provided.',
    plugins_no_hooks: 'No registered lifecycle hooks',
    plugins_registered_hooks: 'Registered hooks',
    plugins_enabled: 'Enabled',
    plugins_disabled: 'Disabled',
    plugins_active_provider: 'Active (provider)',
    plugins_provider_no_hooks: 'Provider plugin — no agent-visibility hooks',
    plugins_load_failed: 'Failed to load plugins: ',
    settings_tab_system: 'System',
    settings_tab_help: 'Help',
    settings_help_meta: 'Resources and support for Hermes WebUI.',
    settings_help_docs_label: 'Documentation',
    settings_help_docs_desc: 'Guides, configuration reference, and the full Hermes WebUI README.',
    settings_help_docs_link: 'Open Documentation',
    settings_help_issue_label: 'Having an issue?',
    settings_help_issue_desc: 'Search existing reports or open a new one on GitHub.',
    settings_help_issue_link: 'Open GitHub Issues',
    settings_title: 'Settings',
    settings_search_placeholder: 'Search settings…',
    settings_search_no_results: 'No settings found.',
    settings_save_btn: 'Save Settings',
    settings_label_model: 'Default Model',
    settings_desc_model: 'Used for new conversations. Existing conversations keep their selected model.',
    settings_main_advanced_button_aria: 'Advanced options for main model',
    settings_main_advanced_title: 'Main model options',
    settings_main_advanced_subtitle: 'Advanced config for the default chat model.',
    settings_main_advanced_saved: 'Main model options saved',
    settings_main_advanced_save_failed: 'Failed to save main model options',
    settings_main_advanced_service_tier: 'Service tier',
    settings_main_advanced_service_tier_desc: 'Optional request setting for OpenAI-family providers.',
    settings_main_advanced_service_tier_default: 'Default / off',
    settings_main_advanced_service_tier_priority: 'Priority (fast)',
    settings_label_auxiliary_models: 'Auxiliary Models',
    settings_desc_auxiliary_models: 'Side-task routing for vision, compression, title generation, etc. "Auto" uses your main chat model.',
    settings_btn_reset_aux_models: 'Reset all to auto',
    settings_btn_apply_aux_models: 'Apply changes',
    settings_aux_provider_auto: 'use main model',
    settings_aux_model_auto: 'auto (use provider default)',
    settings_aux_model_custom: 'Custom model…',
    settings_aux_model_custom_prompt: 'Enter model ID:',
    settings_aux_loading: 'Loading auxiliary models…',
    settings_aux_load_failed: 'Could not load auxiliary model settings. Make sure the agent API is available.',
    settings_aux_reset_confirm_title: 'Reset auxiliary models?',
    settings_aux_reset_confirm_msg: 'This will set all auxiliary tasks to auto (use main model).',
    settings_aux_reset_done: 'Auxiliary models reset to auto',
    settings_aux_save_failed: 'Failed to save auxiliary model',
    settings_aux_saved: 'Auxiliary models updated',
    settings_aux_no_changes: 'No changes to apply',
    settings_aux_task_vision: 'Vision',
    settings_aux_task_vision_desc: 'image/screenshot analysis',
    settings_aux_task_compression: 'Compression',
    settings_aux_task_compression_desc: 'context summarization',
    settings_aux_task_web_extract: 'Web extract',
    settings_aux_task_web_extract_desc: 'web page summarization',
    settings_aux_task_approval: 'Approval',
    settings_aux_task_approval_desc: 'smart command approval',
    settings_aux_task_mcp: 'MCP',
    settings_aux_task_mcp_desc: 'MCP tool reasoning',
    settings_aux_task_title_generation: 'Title generation',
    settings_aux_task_title_generation_desc: 'session titles',
    settings_aux_task_skills_hub: 'Skills hub',
    settings_aux_task_skills_hub_desc: 'skills search/install',
    settings_aux_task_curator: 'Curator',
    settings_aux_task_curator_desc: 'skill-usage review pass',
    settings_aux_task_kanban_decomposer: 'Kanban decomposer',
    settings_aux_task_kanban_decomposer_desc: 'task decomposition',
    settings_aux_task_profile_describer: 'Profile describer',
    settings_aux_task_profile_describer_desc: 'profile summaries',
    settings_aux_task_triage_specifier: 'Triage specifier',
    settings_aux_task_triage_specifier_desc: 'issue/task triage specs',
    settings_aux_advanced_button_title: 'Advanced options',
    settings_aux_advanced_button_aria: 'Advanced options for {task}',
    settings_aux_advanced_title: '{task} options',
    settings_aux_advanced_subtitle: 'Advanced config for auxiliary.',
    settings_aux_advanced_save: 'Save options',
    settings_aux_advanced_base_url: 'Base URL',
    settings_aux_advanced_base_url_desc: 'Optional provider endpoint override.',
    settings_aux_advanced_timeout: 'Timeout seconds',
    settings_aux_advanced_timeout_desc: 'Request timeout for this auxiliary task. Blank uses Hermes default.',
    settings_aux_advanced_download_timeout: 'Download timeout seconds',
    settings_aux_advanced_download_timeout_desc: 'Only relevant for tasks that download media/content, e.g. vision. Blank uses default.',
    settings_aux_advanced_max_concurrency: 'Max concurrency',
    settings_aux_advanced_max_concurrency_desc: 'Optional per-task concurrency limit. Blank uses default.',
    settings_aux_advanced_extra_body: 'Extra body JSON',
    settings_aux_advanced_extra_body_desc: 'Optional JSON object merged into the model request body.',
    settings_aux_advanced_api_key: 'API key override',
    settings_aux_advanced_api_key_set_hint: 'API key is set. Leave blank to keep it, or use clear to remove it.',
    settings_aux_advanced_api_key_empty_hint: 'Leave blank to use provider/default credentials.',
    settings_aux_advanced_api_key_clear: 'Clear existing API key override',
    settings_aux_advanced_extra_body_invalid_json: 'Extra body must be valid JSON',
    settings_aux_advanced_extra_body_object_required: 'Extra body must be a JSON object',
    settings_aux_advanced_saved: 'Auxiliary options saved',
    settings_aux_advanced_save_failed: 'Failed to save auxiliary options',
    settings_label_send_key: 'Send Key',
    settings_send_key_enter: 'Enter (Shift+Enter for newline)',
    settings_send_key_ctrl_enter: 'Ctrl+Enter (Enter for newline)',
    settings_send_key_shift_enter: 'Shift+Enter (Enter for newline)',
    settings_label_theme: 'Theme',
    settings_label_skin: 'Skin',
    settings_label_font_size: 'Font size',
    font_size_small: 'Small',
    font_size_default: 'Default',
    font_size_large: 'Large',
    font_size_xlarge: 'Extra Large',
    settings_autosave_saving: 'Saving…',
    settings_autosave_saved: 'Saved',
    settings_autosave_failed: 'Save failed',
    settings_autosave_retry: 'Retry',
    settings_label_language: 'Language',
    settings_label_quota_chip: 'Show provider quota chip in composer',
    settings_desc_quota_chip: 'Displays an ambient remaining-quota indicator (e.g. OpenRouter credit balance) in the composer footer. Default off. Only visible on wide displays (≥1400px) when enabled, to keep the composer uncluttered on laptop and standard desktop widths.',
    settings_label_conversation_outline: 'Show conversation outline',
    settings_desc_conversation_outline: 'Show a desktop-only jump list for user questions in the current conversation. Off by default.',
    settings_label_hide_suggestions: 'Hide new-chat suggestions',
    settings_desc_hide_suggestions: 'Hide the three default suggestion buttons on the empty new-chat screen to avoid accidental taps.',
    settings_label_hide_empty_state_panel: 'Hide new-chat welcome panel',
    settings_desc_hide_empty_state_panel: 'Hide the logo, heading, subtitle, and suggestion buttons on an empty new conversation.',
    settings_label_virtualize_transcript: 'Virtualize long transcripts (experimental)',
    settings_desc_virtualize_transcript: "Experimental. Renders only the visible portion of very long chat transcripts (over 80 messages) to speed up scrolling. Off by default because it can cause scroll flicker on long sessions with large tool-call blocks. Turn it on to try it; leave it off for the full transcript to render at once (and so the browser's Find (Ctrl+F) matches the whole conversation).",
    settings_label_token_usage: 'Show token usage',
    settings_label_token_speed: 'Show token speed (TPS)',
    settings_desc_token_speed: 'Displays tokens per second in assistant message headers while streaming and after a response completes. Off by default.',
    settings_label_sidebar_density: 'Sidebar density',
    cmd_reasoning: 'Toggle thinking visibility (show/hide), set effort level, or check current status',
    settings_label_external_sessions: 'Show non-WebUI sessions',
    settings_label_claude_code_sessions: 'Show Claude Code sessions',
    settings_label_cron_sessions: 'Show cron sessions',
    settings_label_webhook_sessions: 'Show webhook sessions',
    settings_label_kanban_sessions: 'Show kanban sessions',
    settings_label_previous_messaging_sessions: 'Show previous messaging sessions',
    settings_label_sync_insights: 'Sync to insights',
    settings_label_check_updates: 'Check for updates',
    settings_label_update_channel: 'Update channel',
    settings_update_channel_stable: 'Stable (recommended)',
    settings_update_channel_experimental: 'Experimental',
    settings_desc_update_channel: 'Stable gets soak-tested, proven releases. Experimental gets every release as it lands — faster, but may hit rough edges. Switching back to Stable keeps your current version until Stable catches up. Applies to WebUI updates only.',
    settings_label_ignore_agent_updates: 'Ignore Agent updates',
    settings_label_whats_new_summary: "Summarize What's New with AI",
    settings_label_bot_name: 'Default assistant name',
    settings_label_password: 'Access Password',
    settings_label_pinned_limit: 'Pinned conversations limit',
    settings_desc_pinned_limit: 'Maximum number of active conversations that can be pinned in the sidebar. Default is 3.',
    settings_label_dashboard_mode: 'Official Hermes Dashboard',
    settings_desc_dashboard_mode: 'Show a nav-rail link when the official hermes dashboard is reachable. Public reverse-proxy URLs are stored as browser-only links and are never server-probed.',
    settings_dashboard_mode_auto: 'Auto-detect',
    settings_dashboard_mode_always: 'Always show',
    settings_dashboard_mode_never: 'Never show',
    settings_dashboard_save: 'Save dashboard link settings',
    settings_label_gateway_status: 'Gateway Status',
    settings_desc_gateway_status: 'Status of the Hermes gateway (Telegram, Discord, Slack, etc.)',
    gateway_not_configured: 'Gateway not configured',
    gateway_metadata_stale: 'Gateway metadata stale',
    gateway_endpoint_unreachable: 'Gateway endpoint not reachable',
    gateway_not_running: 'Gateway not running',
    gateway_running_label: 'Running',
    gateway_last_active: 'Last active: ',
    gateway_session_count: (n) => `${n} ${n === 1 ? 'session' : 'sessions'}`,
    gateway_load_failed: 'Failed to load gateway status',
    settings_saved: 'Settings saved',
    settings_save_failed: 'Save failed: ',
    settings_load_failed: 'Failed to load settings: ',
    settings_saved_pw: 'Settings saved — password protection enabled and this browser stays signed in',
    settings_saved_pw_updated: 'Settings saved — password updated',
    // login page (used server-side via /api/i18n/login endpoint)
    login_title: 'Sign in',
    login_subtitle: 'Enter your password to continue',
    login_placeholder: 'Password',
    login_btn: 'Sign in',
    login_invalid_pw: 'Invalid password',
    login_conn_failed: 'Connection failed',
    dialog_confirm_title: 'Confirm action',
    dialog_prompt_title: 'Enter a value',
    dialog_confirm_btn: 'Confirm',
    discard: 'Discard',
    clear: 'Clear',
    create: 'Create',
    remove: 'Remove',
    project_name_prompt: 'Project name:',
    // Sidebar & Tabs
    tab_chat: 'Chat',
    tab_tasks: 'Tasks',
    tab_skills: 'Skills',
    tab_memory: 'Memory',
    memory_off_agent_outdated: 'Memory is off: this version of Hermes Agent cannot keep memory separate per profile. Update Hermes Agent to turn it back on.',
    memory_off_agent_no_instance_claim: 'Memory providers are off: this version of Hermes Agent cannot keep them separate across profiles. Built-in memory still works. Update Hermes Agent to turn them back on.',
    memory_off_provider_shared_instance: 'Your memory provider is off for this profile because another profile is already using it. Built-in memory still works. Restart WebUI, or update the provider (for example mnemosyne-hermes 0.7.2 or later).',
    memory_off_notice_dismiss: 'Dismiss memory notice',
    tab_workspaces: 'Spaces',
    tab_profiles: 'Profiles',
    tab_kanban: 'Kanban',
    kanban_board: 'Board',
    kanban_visible_tasks: '{0} visible tasks',
    kanban_search_tasks: 'Search tasks',
    kanban_all_assignees: 'All assignees',
    kanban_all_tenants: 'All tenants',
    kanban_include_archived: 'Include archived',
    kanban_no_matching_tasks: 'No matching tasks',
    kanban_no_data: 'No Kanban data',
    kanban_work_queue_hint: 'This is the Hermes Agent work queue. Create or triage a task, assign it, move it to Ready, then let the dispatcher claim it.',
    kanban_tasks_hidden_by_filters: 'Tasks hidden by filters',
    kanban_clear_filters: 'Clear filters',
    kanban_unavailable: 'Kanban unavailable',
    kanban_read_only: 'Read-only view',
    kanban_empty: 'Empty',
    kanban_task: 'Task',
    kanban_no_description: 'No description',
    kanban_refresh: 'Refresh',
    kanban_status_triage: 'Triage',
    kanban_status_todo: 'Todo',
    kanban_status_scheduled: 'Scheduled',
    kanban_status_ready: 'Ready',
    kanban_status_running: 'Running',
    kanban_status_blocked: 'Blocked',
    kanban_status_done: 'Done',
    kanban_comments_count: 'Comments ({0})',
    kanban_events_count: 'Events ({0})',
    kanban_links: 'Links',
    kanban_parents: 'Parents',
    kanban_children: 'Children',
    kanban_runs_count: 'Runs ({0})',
    kanban_no_comments: 'No comments',
    kanban_no_events: 'No events',
    kanban_no_runs: 'No runs',
    kanban_title: 'Title',
    kanban_description: 'Description',
    kanban_description_placeholder: 'Optional — what needs to happen, acceptance criteria, links',
    kanban_status: 'Status',
    kanban_assignee: 'Assignee',
    kanban_assignee_placeholder: 'Optional — leave blank for any worker',
    kanban_tenant: 'Tenant',
    kanban_tenant_placeholder: 'Optional — project or team slug',
    kanban_priority: 'Priority',
    kanban_priority_hint: 'Higher numbers run first. Default 0.',
    kanban_title_required: 'Title is required.',
    kanban_new_task: 'New task',
    kanban_view_lanes: 'Lanes by profile',
    kanban_view_consolidated: 'Consolidated view',
    kanban_view_lanes_saved: 'Kanban view: lanes by profile',
    kanban_view_consolidated_saved: 'Kanban view: consolidated',
    kanban_view_update_failed: 'Failed to update Kanban view: ',
    kanban_edit_task: 'Edit task',
    kanban_status_original_hint: 'Actual status: {0}. This dialog only supports Triage/Todo/Ready edits.',
    kanban_run_dispatcher: 'Run dispatcher',
    kanban_run_dispatcher_confirm: 'This will claim Ready tasks on this board and spawn worker subprocesses (one per task, up to 8 per click). Continue?',
    kanban_assignee_profiles_label: 'Hermes profiles',
    kanban_assignee_other_label: 'Other (CLI lanes / removed profiles)',
    kanban_assignee_unassigned: '— Unassigned (won\u2019t auto-run) —',
    kanban_ready_needs_assignee: 'You picked Unassigned + Ready. The dispatcher will skip this task. Submit again to confirm, or pick a profile.',
    kanban_dispatch_preview_prefix: 'Preview:',
    kanban_dispatch_run_prefix: 'Dispatched:',
    kanban_bulk_status_aria: 'Bulk status',
    kanban_dispatch_spawned: 'spawned',
    kanban_dispatch_promoted: 'promoted',
    kanban_dispatch_reclaimed: 'reclaimed',
    kanban_dispatch_skipped_unassigned: 'skipped (no assignee)',
    kanban_dispatch_skipped_nonspawnable: 'skipped (unknown profile)',
    kanban_dispatch_auto_blocked: 'auto-blocked',
    kanban_dispatch_timed_out: 'timed out',
    kanban_dispatch_crashed: 'crashed',
    kanban_add_comment: 'Add comment',
    kanban_workspace_kind: 'Workspace kind',
    kanban_workspace_path: 'Workspace path',
    kanban_workspace_path_placeholder: 'Path to workspace',
    kanban_workspace_scratch: 'Scratch',
    kanban_workspace_worktree: 'Worktree',
    kanban_workspace_dir: 'Directory',
    kanban_workspace_path_required: 'Workspace path is required for non-scratch workspaces.',
    kanban_skills: 'Skills',
    kanban_skills_placeholder: 'Comma-separated skill names, e.g. python,git',
    kanban_max_runtime_seconds: 'Max runtime (seconds)',
    kanban_max_runtime_hint: 'Leave empty for unlimited.',
    kanban_max_runtime_invalid: 'Max runtime must be a whole number of seconds greater than 0.',
    kanban_parents_modal: 'Parent dependencies',
    kanban_parents_placeholder: 'Parent task ID',
    kanban_add_dependency: 'Add dependency',
    kanban_remove_dependency: 'Remove dependency',
    kanban_dependency_placeholder: 'Task ID to link',
    kanban_dependency_self: 'A task cannot depend on itself',
      kanban_only_mine: 'Only mine',
      kanban_bulk_action: 'Bulk action',
      kanban_nudge_dispatcher: 'Preview dispatcher',
      kanban_stats: 'Stats',
      kanban_worker_log: 'Worker log',
      kanban_block: 'Block',
      kanban_unblock: 'Unblock',
      kanban_back_to_board: 'Back to board',
      kanban_lanes_by_profile: 'Lanes by profile',
      kanban_new_board: 'New board…',
      kanban_rename_board: 'Board settings…',
      kanban_board_settings: 'Board settings',
      kanban_archive_board: 'Archive current board…',
      kanban_archive_board_confirm: 'Archive board "{name}"? Tasks remain on disk and the board can be restored from kanban/boards/_archived/.',
      kanban_board_archived: 'Board archived',
      kanban_board_name: 'Name',
      kanban_board_slug: 'Slug (lowercase, hyphens)',
      kanban_board_description: 'Description (optional)',
      kanban_board_icon: 'Icon (emoji, optional)',
      kanban_board_color: 'Color (optional)',
      kanban_board_default_workdir: 'Default workspace path',
      kanban_board_default_workdir_placeholder: 'Workspace path (optional)',
      kanban_board_name_required: 'Name is required',
      kanban_board_slug_required: 'Slug is required',
      kanban_card_complete: 'complete',
      kanban_card_archive: 'archive',
      kanban_unassigned: 'unassigned',
    kanban_status_archived: 'Archived',
    tab_todos: 'Todos',
    tab_insights: 'Insights',
    tab_dashboard: 'Hermes Dashboard',
    dashboard_loopback_warning: 'Dashboard is loopback-only on the server. Either browse from the server itself or restart it with --host 0.0.0.0 (insecure).',
    tab_logs: 'Logs',
    tab_settings: 'Settings',
    close_menu: 'Close menu',
    new_conversation: 'New conversation',
    filter_conversations: 'Filter conversations...',
    markdown_table_filter: 'Filter table',
    markdown_table_sort_column: 'Sort column',
    session_time_unknown: 'Unknown',
    session_time_minutes_ago: (n) => `${n}m`,
    session_time_hours_ago: (n) => `${n}h`,
    session_time_days_ago: (n) => `${n}d`,
    session_time_last_week: '1w',
    session_time_bucket_today: 'Today',
    session_time_bucket_yesterday: 'Yesterday',
    session_time_bucket_this_week: 'This week',
    session_time_bucket_last_week: 'Last week',
    session_time_bucket_older: 'Older',
    scheduled_jobs: 'Scheduled jobs',
    new_job: 'New job',
    cron_tab_jobs: 'Jobs',
    cron_tab_scripts: 'Scripts',
    scripts_empty: 'No scripts in this profile yet.',
    scripts_no_directory: 'No scripts directory yet.',
    scripts_unavailable: 'Scripts are unavailable on this platform.',
    scripts_load_failed: 'Could not load scripts.',
    scripts_detail_empty_title: 'Scripts',
    scripts_detail_empty_sub: 'This panel lists the scripts in this profile. To view or edit one, open the file on the server.',
    loading: 'Loading...',
    search_skills: 'Search skills...',
    new_skill: 'New skill',
    personal_memory: 'Personal memory',
    current_task_list: 'Current task list',
    // Logs
    logs_title: 'Logs',
    logs_file: 'File',
    logs_tail: 'Tail',
    logs_auto_refresh: 'Auto-refresh (5s)',
    logs_wrap: 'Wrap lines',
    logs_copy_all: 'Copy all',
    logs_empty: 'No log lines yet.',
    logs_loading: 'Loading logs…',
    logs_load_failed: 'Logs failed to load',
    logs_status_idle: 'Choose a log file to view recent lines.',
    logs_no_mtime: 'not written yet',
    logs_truncated_hint: 'Showing the tail of a large log file; older bytes were skipped to keep memory bounded.',
    logs_copied: 'Logs copied',
    logs_severity: 'Severity',
    logs_severity_all: 'All',
    logs_severity_errors: 'Errors',
    logs_severity_warnings: 'Warnings+',
    logs_filter_active: 'shown (filter active)',

    // Insights
    insights_title: 'Usage Analytics',
    insights_sessions: 'Sessions',
    insights_messages: 'Messages',
    insights_tokens: 'Tokens',
    insights_cost: 'Estimated Cost',
    insights_no_cost: 'N/A',
    insights_models: 'Models',
    insights_activity_by_day: 'Activity by Day',
    insights_activity_by_hour: 'Activity by Hour',
    insights_peak_hour: 'Peak: {hour}',
    insights_token_breakdown: 'Token Breakdown',
    insights_input_tokens: 'Input',
    insights_output_tokens: 'Output',
    insights_total: 'Total',
    insights_daily_tokens: 'Daily Tokens',
    insights_model_name: 'Model',
    insights_model_sessions: 'Sessions',
    insights_model_tokens: 'Tokens',
    insights_model_cost: 'Cost',
    insights_model_cache: 'Cache',
    insights_cache_hit: 'Prompt-cache hit rate',
    insights_model_share: 'Share',
    insights_model_health_title: 'Model health comparison',
    insights_model_health_provider: 'Provider',
    insights_model_health_replacement: 'Replacement guide',
    insights_model_health_cost_per_m: 'Cost / 1M',
    insights_no_usage_data: 'No usage data yet',
    insights_footer: 'Showing data from the last {days} days',
    insights_footer_range: 'Showing data from {start} to {end}',
    insights_custom_range: 'Custom range…',
    insights_range_fallback: 'That range isn’t available. Showing the last {days} days.',
    insights_start_date: 'Start date',
    insights_end_date: 'End date',
    insights_skill_usage_title: 'Skill Usage',
    insights_skill_usage_sub: 'Tool invocation frequency',
    insights_skill_usage_total: 'Total invocations',
    insights_skill_usage_skills_used: 'Skills used',
    insights_skill_usage_no_data: 'No skill usage data yet',
    insights_skill_usage_no_data_hint: 'Skills will appear here once used in conversations.',
    insights_skill_usage_footer: 'Counts from ~/.hermes/skills/',
    insights_skill_usage_col_skill: 'Skill',
    insights_skill_usage_col_uses: 'Uses',
    insights_skill_usage_col_views: 'Views',
    insights_skill_usage_col_share: 'Usage %',
    insights_skill_usage_col_patches: 'Patches',
    insights_period_7d: '7 days',
    insights_period_30d: '30 days',
    insights_period_90d: '90 days',
    insights_period_365d: '365 days',
    wiki_browse: 'Browse wiki',
    wiki_search_placeholder: 'Search pages...',
    wiki_no_pages: 'No wiki pages found',
    wiki_not_configured: 'Wiki not configured',
    workspace_desc: 'Add and switch workspaces for your sessions.',
    session_meta_messages: (n) => `${n} msg${n === 1 ? '' : 's'}`,
    session_meta_children: (n) => `${n} child${n === 1 ? '' : 'ren'}`,
    // Softened label: avoids exposing the technical internal term
    // 'segment' in the default visible badge. User-facing copy remains
    // translatable for locales that prefer a different wording. (#2155)
    session_meta_segments: (n) => `${n} prior turn${n === 1 ? '' : 's'}`,
    session_lineage_toggle_hint: '{0} — earlier context turns are collapsed here. Click to show or hide them.',
    session_lineage_static_hint: '{0} — earlier context turns are collapsed here.',
    session_child_toggle_hint: '{0} — child conversations spawned from this session. Click to show or hide them.',
    session_readonly_title_hint: 'Read-only imported session — {0}',
    session_lineage_segment_untitled: 'Untitled segment',
    session_lineage_segment_open: 'Open lineage segment',
    new_profile: 'New profile',
    transcript: 'Transcript',
    download_transcript: 'Download as Markdown',
    import: 'Import',
    export_session_json: 'JSON',
    export_session_json_tooltip: 'Export full session as JSON',
    export_session_html: 'HTML',
    export_session_html_tooltip: 'Export as a self-contained HTML page',
    import_session_json_tooltip: 'Import session from JSON',
    clear_conversation_btn_tooltip: 'Clear all messages in this conversation',
    // Settings detail
    settings_label_rtl: 'Right-to-left chat layout',
    settings_desc_rtl: 'Flips alignment of chat messages and the composer input for languages like Arabic or Hebrew. Affects only the chat area — sidebar and other panels stay left-to-right.',
    settings_label_sound: 'Notification sound',
    settings_desc_sound: 'Play a sound when the assistant finishes a response.',
    // TTS (#499)
    tts_listen: 'Listen',
    tts_not_supported: 'Speech synthesis not supported in this browser.',
    settings_label_tts: 'Text-to-Speech for responses',
    settings_desc_tts: "Show a speaker button on each assistant message to read it aloud using your browser's speech synthesis.",
    settings_label_tts_auto_read: 'Auto-read responses aloud',
    settings_desc_tts_auto_read: 'Automatically speak each new assistant response when it finishes. Pauses when you start typing.',
    // Composer voice-mode pref (#1488)
    settings_label_voice_mode: 'Hands-free voice mode button',
    settings_desc_voice_mode: 'Show the voice-mode button (audio waveform) next to the dictation mic. Lets you speak naturally — Hermes auto-sends after a pause and reads replies aloud. Requires a browser that supports both speech recognition and TTS.',
    settings_label_raw_audio: 'Send raw audio instead of transcribing',
    settings_desc_raw_audio: 'Record and send the original audio file to the agent instead of converting it to text first. The agent can then transcribe it or process the raw audio (emotion, background noise, custom STT). Like Telegram\'s voice message behavior.',
    voice_send_raw: 'Send raw audio',
    voice_raw_attached: 'Audio attached. Press Send or type more.',
    settings_label_tts_voice: 'Voice',
    settings_desc_tts_voice: "Preferred voice. Populated from your browser's available voices.",
    settings_tts_voice_default_system: 'Default system voice',
    settings_label_tts_rate: 'Speech rate',
    settings_label_tts_pitch: 'Speech pitch',
    settings_label_tts_engine: 'TTS Engine',
    settings_desc_tts_engine: 'Choose speech engine. Edge TTS uses Microsoft neural voices via the server.',
    settings_label_notifications: 'Browser notifications',
    settings_desc_notifications: 'Show a system notification when a response completes while the app is in the background.',
    notifications_enable_btn: 'Enable notifications',
    notifications_test_btn: 'Send test',
    notifications_permission_status: (status) => `Permission: ${status}`,
    notifications_enabled_toast: 'Notifications enabled.',
    notifications_denied: 'Notifications are blocked in this browser. Enable them in browser settings.',
    notifications_unsupported: 'Notifications are not supported in this browser.',
    settings_desc_token_usage: 'Displays input/output token count below each assistant reply. Also toggled with /usage.',
    settings_label_api_redact: 'Redact sensitive data in API responses',
    settings_desc_api_redact: 'Self-hosted users can disable for transparency (not recommended for shared instances).',
    settings_sidebar_density_compact: 'Compact',
    settings_sidebar_density_detailed: 'Detailed',
    settings_desc_sidebar_density: 'Controls how much metadata the session list shows in the left sidebar.',
    settings_label_auto_title_refresh: 'Adaptive title refresh',
    settings_auto_title_refresh_off: 'Off',
    settings_auto_title_refresh_5: 'Every 5 exchanges',
    settings_auto_title_refresh_10: 'Every 10 exchanges',
    settings_auto_title_refresh_20: 'Every 20 exchanges',
    settings_desc_auto_title_refresh: 'Automatically re-generates the session title based on the latest exchange, keeping it relevant as the conversation evolves. Requires an LLM title generation model to be configured.',
    settings_desc_external_sessions: 'Show conversations from CLI, Telegram, Discord, Slack, and other channels in the session list. Click to import and continue.',
    settings_desc_claude_code_sessions: 'Show Claude Code imported sessions in the sidebar, or hide them while leaving other external sessions visible.',
    settings_desc_cron_sessions: 'Surface cron job output as conversations in the sidebar. Only active when non-WebUI sessions are enabled. Defaults off; high-frequency jobs can flood the sidebar.',
    settings_desc_webhook_sessions: 'Surface webhook runs as conversations in the sidebar. Only active when non-WebUI sessions are enabled. Defaults off; high-volume routes can flood the sidebar.',
    settings_desc_kanban_sessions: 'Surface kanban worker runs as conversations in the sidebar. Only active when non-WebUI sessions are enabled. Defaults off; worker runs are internal and can flood the sidebar.',
    settings_desc_previous_messaging_sessions: 'Show older Discord, Telegram, Slack, and Weixin sessions that were replaced by reset or compression.',
    settings_desc_sync_insights: 'Mirrors WebUI token usage to state.db so hermes /insights includes browser session data. Off by default.',
    settings_desc_check_updates: 'Show a banner when newer versions of the WebUI or Agent are available. Runs a background git fetch periodically.',
    settings_desc_ignore_agent_updates: 'Keep WebUI update checks on, but hide Agent update notices and skip Agent update fetches.',
    settings_desc_whats_new_summary: "Changes the What's New action from opening the raw diff first to generating a short, human-readable summary. The regular diff comparison stays available after the summary.",
    settings_desc_bot_name: 'Used for the default profile only. Other profiles use their own profile names.',
    settings_desc_password: 'Enter a new password to set or change it. Leave blank to keep current setting.',
    password_placeholder: 'Enter new password…',
    password_env_var_locked: 'The HERMES_WEBUI_PASSWORD environment variable is currently set and takes precedence. Unset it and restart the server to manage the password from here.',
    password_env_var_locked_placeholder: 'Locked: HERMES_WEBUI_PASSWORD env var is set',
    disable_auth: 'Disable Auth',
    sign_out: 'Sign Out',
    settings_label_shutdown: 'Stop the Hermes WebUI server',
    settings_desc_shutdown_before_cmd: 'Gracefully stops the local WebUI server. Useful if you launched via ',
    settings_desc_shutdown_between_cmds: ' and want to stop without switching to a terminal. You\u2019ll need to relaunch the server (',
    settings_desc_shutdown_after_cmd: ' or the native app) before the WebUI is reachable again.',
    settings_btn_shutdown: 'Stop server',
    settings_shutdown_confirm_title: 'Stop Hermes WebUI',
    settings_shutdown_confirm_message: 'Stop the Hermes WebUI server?',
    settings_shutdown_confirm_btn: 'Stop',
    settings_shutdown_stopped_message: 'Server stopped. You can close this tab.',
    // Providers panel
    providers_tab_title: 'Providers',
    providers_section_title: 'Providers',
    providers_section_meta: 'Manage API keys for AI providers. Changes take effect immediately.',
    providers_status_configured: 'API key configured',
    providers_status_configured_label: 'Configured',
    providers_status_not_configured: 'No API key',
    providers_status_oauth: 'OAuth',
    providers_status_api_key: 'API key',
    providers_status_model: 'Model',
    providers_status_not_configured_label: 'Not configured',
    providers_oauth_hint: 'Authenticated via OAuth. No API key needed.',
    providers_oauth_config_yaml_hint: 'Token configured via config.yaml. To update, edit the providers section in your config.yaml or run hermes auth.',
    providers_oauth_not_configured_hint: 'Not authenticated. Run hermes auth in the terminal to configure this provider.',
    providers_save: 'Save',
    providers_refresh_models: 'Refresh models',
    providers_refreshing: 'Refreshing…',
    providers_models_refreshed: 'Models refreshed',
    providers_remove: 'Remove',
    providers_saving: 'Saving…',
    providers_removing: 'Removing…',
    providers_enter_key: 'Please enter an API key',
    providers_empty: 'No configurable providers found.',
    providers_key_updated: 'API key saved',
    providers_key_removed: 'API key removed',
    providers_key_placeholder_new: 'sk-...',
    providers_key_placeholder_replace: 'Enter new key to replace…',
    provider_quota_title: 'Active provider quota',
    provider_quota_active_provider: 'Active provider',
    provider_quota_last_checked_after_refresh: 'Last checked after refresh',
    provider_quota_last_checked: 'Last checked {0}',
    provider_quota_refresh_usage: 'Refresh usage',
    provider_quota_refreshing: 'Refreshing...',
    provider_quota_refresh_title: 'Refresh provider usage limits now',
    provider_quota_refresh_succeeded: 'Provider usage refreshed',
    provider_quota_refresh_failed: 'Provider usage refresh failed',
    provider_quota_session_limit: '5-hour limit',
    provider_quota_weekly_limit: 'Weekly limit',
    provider_quota_usage_limit: 'Usage limit',
    provider_quota_window_fallback: 'Window',
    provider_quota_metric_remaining: 'Remaining',
    provider_quota_metric_used: 'Used',
    provider_quota_metric_limit: 'Limit',
    provider_quota_status_available: 'available',
    provider_quota_status_exhausted: 'exhausted',
    provider_quota_status_unavailable: 'unavailable',
    provider_quota_status_failed: 'failed',
    provider_quota_status_checked: 'checked',
    provider_quota_status_no_key: 'no key',
    provider_quota_status_invalid_key: 'invalid key',
    provider_quota_status_unsupported: 'unsupported',
    provider_quota_used_meta: '{0} used',
    provider_quota_resets_meta: 'resets {0}',
    provider_quota_credential_pool: 'Credential pool',
    provider_quota_credential_label: 'Credential {0}',
    provider_quota_pool_plans: 'Plans: {0}',
    provider_quota_pool_no_windows: 'No account-limit windows reported.',
    provider_quota_account_limits_loaded: 'Account limits loaded.',
    provider_quota_unavailable: 'Quota status unavailable',
    provider_quota_retry_after: 'Retry after {0}',
    provider_quota_pool_summary_available: '{0}/{1} available',
    provider_quota_pool_summary_exhausted: '{0} exhausted',
    provider_quota_pool_summary_failed: '{0} failed',
    provider_quota_pool_summary_checked: '{0} checked',
    provider_cost_budget_label: 'Monthly budget',
    provider_cost_budget_placeholder: 'e.g. 50.00',
    provider_cost_budget_set: 'Set',
    provider_cost_budget_clear: 'Clear',
    provider_cost_budget_pct: '{0}% of ${1} budget (monthly pace)',
    provider_cost_budget_save_failed: 'Failed to save budget',
    cancel: 'Cancel',
    create_job: 'Create job',
    save_skill: 'Save skill',
    editing: 'Editing',
    // Empty state
    empty_title: 'What can I help with?',
    empty_subtitle: 'Ask anything, run commands, explore files, or manage your scheduled tasks.',
    suggest_files: 'What files are in this workspace?',
    suggest_schedule: "What's on my schedule today?",
    suggest_plan: 'Help me plan a small project.',
    // onboarding
    onboarding_badge: 'FIRST RUN',
    onboarding_title: 'Welcome to Hermes Web UI',
    onboarding_lead: 'A quick guided setup will verify Hermes, save a real provider configuration, choose a workspace and model, and optionally protect the app with a password.',
    onboarding_back: 'Back',
    onboarding_continue: 'Continue',
    onboarding_skip: 'Skip setup',
    onboarding_skipped: 'Setup skipped — using existing config.',
    onboarding_open: 'Open Hermes',
    onboarding_step_system_title: 'System check',
    onboarding_step_system_desc: 'Verify Hermes Agent and config visibility.',
    onboarding_step_setup_title: 'Provider setup',
    onboarding_step_setup_desc: 'Save the minimum Hermes provider config.',
    onboarding_step_workspace_title: 'Workspace + model',
    onboarding_step_workspace_desc: 'Pick defaults for new sessions and chat.',
    onboarding_step_password_title: 'Optional password',
    onboarding_step_password_desc: 'Protect the Web UI before sharing it.',
    onboarding_step_finish_title: 'Finish',
    onboarding_step_finish_desc: 'Review and enter the app.',
    onboarding_notice_system_ready: 'Hermes Agent looks reachable from the Web UI.',
    onboarding_notice_system_unavailable: 'Hermes Agent is not fully available yet. Bootstrap can install it, but provider setup may still require a terminal.',
    onboarding_check_agent: 'Hermes Agent',
    onboarding_check_agent_ready: 'Detected and importable',
    onboarding_check_agent_missing: 'Missing or partially importable',
    onboarding_check_password: 'Password',
    onboarding_check_password_enabled: 'Already enabled',
    onboarding_check_password_disabled: 'Not enabled yet',
    onboarding_check_provider: 'Provider config',
    onboarding_check_provider_ready: 'Ready to chat',
    onboarding_check_provider_partial: 'Saved but incomplete',
    onboarding_check_provider_pending: 'Needs verification',
    onboarding_config_file: 'Config file:',
    onboarding_env_file: '.env file:',
    onboarding_unknown: 'Unknown',
    onboarding_current_provider: 'Current setup:',
    onboarding_missing_imports: 'Missing imports:',
    onboarding_notice_setup_required: 'Choose a simple provider path here. Advanced OAuth flows still belong in the Hermes CLI for now.',
    onboarding_notice_provider_choice_required: 'Hermes is installed, but you still need to choose a provider and save working credentials.',
    onboarding_notice_custom_base_url_required: 'Hermes has a saved provider/model selection, but the custom provider still needs a base URL. Add the API key too if that server requires one.',
    onboarding_notice_provider_auth_required: 'Provider "{0}" is configured but not yet authenticated. Run "hermes auth" or "hermes model" in a terminal to complete setup, then reload the Web UI.',
    onboarding_notice_provider_api_key_required: 'Hermes has a saved provider/model selection but still needs the API key required to chat.',
    onboarding_notice_setup_already_ready: 'A working Hermes provider setup is already detected. You can keep it or replace it here.',
    onboarding_oauth_provider_ready_title: 'Provider already authenticated',
    onboarding_oauth_provider_ready_body: 'This instance is configured to use an OAuth provider (<strong>{provider}</strong>) that was set up via the Hermes CLI. No API key is needed here — click Continue to finish setup.',
    onboarding_oauth_provider_not_ready_title: 'OAuth provider not yet authenticated',
    onboarding_oauth_provider_not_ready_body: 'This instance is configured to use <strong>{provider}</strong>, which uses OAuth rather than an API key. Run <code>hermes auth</code> or <code>hermes model</code> in a terminal to authenticate, then reload the Web UI.',
    onboarding_oauth_switch_hint: 'Or choose a different provider below to switch to an API-key setup:',
    oauth_login_codex: 'Login with Codex (ChatGPT)',
    oauth_codex_step1: 'Step 1: Visit this URL and enter the code',
    oauth_codex_step2: 'Step 2: Enter this code on the page',
    oauth_codex_polling: 'Waiting for authorization...',
    oauth_codex_success: 'Codex OAuth login successful!',
    oauth_codex_error: 'OAuth login failed',
    oauth_codex_expired: 'Code expired, please try again',
    onboarding_notice_workspace: 'These values reuse the same settings APIs as the normal app.',
    onboarding_workspace_label: 'Workspace',
    onboarding_workspace_or_path: 'Or enter a workspace path',
    onboarding_workspace_placeholder: '/home/you/workspace',
    onboarding_provider_label: 'Setup mode',
    onboarding_quick_setup_badge: 'quick setup',
    provider_category_easy_start: 'Easy start',
    provider_category_self_hosted: 'Open / self-hosted',
    provider_category_specialized: 'Specialized',
    onboarding_api_key_label: 'API key',
    onboarding_api_key_placeholder: 'Leave blank to keep an existing saved key',
    onboarding_api_key_label_optional: 'API key (optional)',
    onboarding_api_key_placeholder_optional: 'Leave blank for keyless servers',
    onboarding_api_key_help_keyless: 'Most LM Studio / Ollama / vLLM installs run keyless — leave this blank if your server doesn\'t require authentication. Use the Test connection button to verify.',
    onboarding_api_key_help_prefix: 'Saved as a secret in your Hermes .env file using',
    onboarding_base_url_label: 'Base URL',
    onboarding_base_url_placeholder: 'https://your-endpoint.example/v1',
    onboarding_base_url_help: 'Use this for OpenAI-compatible routers, self-hosted servers, LiteLLM, Ollama, LM Studio, vLLM, or similar endpoints.',
    onboarding_model_label: 'Default model',
    onboarding_workspace_help: 'Pick the model Hermes should use for new chats after setup completes.',
    onboarding_custom_model_placeholder: 'your-model-name',
    onboarding_custom_model_help: 'For custom endpoints, enter the exact model ID your server expects.',
    onboarding_notice_password_enabled: 'A password is already configured. Enter a new one only if you want to replace it.',
    onboarding_notice_password_recommended: 'Optional but recommended if you will expose the UI beyond localhost.',
    onboarding_password_label: 'Password (optional)',
    onboarding_password_placeholder: 'Leave blank to skip',
    onboarding_password_help: 'Passwords are stored through the existing settings API and hashed server-side.',
    onboarding_notice_finish: 'You can reopen Settings later to change any of this.',
    onboarding_not_set: 'Not set',
    onboarding_password_will_enable: 'Will be enabled',
    onboarding_password_will_replace: 'Will be replaced',
    onboarding_password_keep_existing: 'Keep current password',
    onboarding_password_remains_disabled: 'Will remain disabled',
    onboarding_password_skipped: 'Skipped for now',
    onboarding_finish_help: 'Finishing stores <code>onboarding_completed</code> in settings and drops you into the normal app.',
    onboarding_error_choose_workspace: 'Choose a workspace before continuing.',
    onboarding_error_choose_model: 'Choose a model before continuing.',
    onboarding_error_provider_required: 'Choose a setup mode before continuing.',
    onboarding_error_base_url_required: 'Base URL is required for custom endpoints.',
    onboarding_probe_test_button: 'Test connection',
    onboarding_probe_probing: 'Testing connection…',
    onboarding_probe_ok: 'Connected. {n} model(s) available.',
    onboarding_probe_error_generic: 'Could not reach the configured base URL.',
    onboarding_probe_error_invalid_url: 'Base URL must start with http:// or https://.',
    onboarding_probe_error_dns: 'Could not resolve the host. Check the URL or use the host\'s IP address.',
    onboarding_probe_error_connect_refused: 'Connection refused — the server may not be running on that address. From inside Docker, try the host IP instead of localhost.',
    onboarding_probe_error_timeout: 'The endpoint did not respond in time. Check that the server is running and the URL is correct.',
    onboarding_probe_error_http_4xx: 'The endpoint returned a client error. Check authentication and the URL path (typically ends in /v1).',
    onboarding_probe_error_http_5xx: 'The endpoint returned a server error. Check the LM Studio / Ollama server logs.',
    onboarding_probe_error_parse: 'The endpoint did not return a model list in the expected shape. Verify the URL points to the OpenAI-compatible API root.',
    onboarding_probe_error_unreachable: 'Could not reach the configured base URL.',
    onboarding_error_probe_failed: 'Could not validate the configured base URL.',
    onboarding_error_workspace_required: 'Workspace is required.',
    onboarding_error_model_required: 'Model is required.',
    onboarding_complete: 'Onboarding complete',

    // panel/runtime i18n
    error_prefix: 'Error: ',
    default: 'default',
    search: 'Search',
    not_available: 'N/A',
    never: 'never',
    add: 'Add',
    add_failed: 'Add failed: ',
    remove_failed: 'Remove failed: ',
    switch_failed: 'Switch failed: ',
    name_required: 'Name is required',
    content_required: 'Content is required',
    view: 'View',
    dismiss: 'Dismiss',
    disable: 'Disable',
    cron_no_jobs: 'No scheduled jobs found.',
    cron_status_off: 'off',
    cron_status_paused: 'paused',
    cron_status_error: 'error',
    cron_status_active: 'active',
    cron_status_running: 'running\u2026',
    cron_status_needs_attention: 'needs attention',
    cron_attention_desc: 'This recurring job has no next run time. The scheduler may have failed to compute its next run.',
    cron_attention_croniter_hint: 'The Gateway runtime may be missing the croniter package. Restart the Gateway with cron support, then resume this job.',
    cron_attention_resume: 'Resume and recalculate',
    cron_jobs_project: 'Cron Jobs',
    cron_attention_run_once: 'Run once now',
    cron_attention_copy_diagnostics: 'Copy diagnostics',
    cron_diagnostics_copied: 'Cron diagnostics copied',
    cron_next: 'Next',
    cron_last: 'Last',
    cron_run_now: 'Run now',
    cron_pause: 'Pause',
    cron_resume: 'Resume',
    cron_job_name_placeholder: 'Job name',
    cron_schedule_placeholder: 'Schedule',
    cron_prompt_placeholder: 'Prompt',
    cron_last_output: 'Last output',
    cron_expand_prompt: 'Expand prompt',
    cron_collapse_prompt: 'Collapse prompt',
    cron_expand_output: 'Expand output',
    cron_collapse_output: 'Collapse output',
    cron_mode_agent: 'Agent',
    cron_mode_script: 'Script',
    cron_mode_label: 'Mode',
    cron_script_job_banner: 'Runs a script on schedule — stdout is delivered to the target. No agent, prompt, or skills.',
    cron_script_card_title: 'Script',
    cron_script_output: 'Script output',
    cron_script_path_label: 'Script path',
    cron_script_path_hint: 'Resolved under ~/.hermes/scripts/ unless an absolute path. Edit the script file on the server to change behavior.',
    cron_script_badge_title: 'Script job (no agent)',
    cron_workdir_label: 'Working directory',
    cron_view_full_output: 'View full output',
    cron_all_runs: 'All runs',
    cron_hide_runs: 'Hide runs',
    cron_no_runs_yet: '(no runs yet)',
    cron_schedule_required_example: 'Schedule is required (e.g. "0 9 * * *" or "every 1h")',
    cron_schedule_required: 'Schedule is required',
    cron_prompt_required: 'Prompt is required',
    cron_job_created: 'Job created',
    cron_duplicate: 'Duplicate',
    cron_duplicated: 'Job duplicated (paused)',
    cron_job_triggered: 'Job triggered',
    cron_job_paused: 'Job paused',
    cron_job_resumed: 'Job resumed',
    cron_job_updated: 'Job updated',
    cron_model_label: 'Model Override',
    cron_model_hint: 'Override the default model for this job.',
    cron_model_no_agent_hint: 'No-agent jobs run the configured script directly; model is unused.',
    cron_model_use_default: 'Default (use profile/system default)',
    cron_delete_confirm_title: 'Delete cron job',
    cron_delete_confirm_message: 'This cannot be undone.',
    cron_job_deleted: 'Job deleted',
    cron_completion_status: (name, status) => `Cron "${name}" ${status}`,
    status_failed: 'failed',
    status_completed: 'completed',
    todos_no_active: 'No active task list in this session.',
    clear_conversation_title: 'Clear conversation',
    clear_conversation_message: 'Clear all messages? This cannot be undone.',
    clear_failed: 'Clear failed: ',
    skills_no_match: 'No skills match.',
    skill_enabled: 'Enabled',
    skill_disabled: 'Disabled',
    skill_toggle_failed: 'Failed to toggle skill: ',
    linked_files: 'Linked Files',
    skill_load_failed: 'Could not load skill: ',
    skill_file_load_failed: 'Could not load file: ',
    skill_name_required: 'Skill name is required',
    skill_updated: 'Skill updated',
    skill_created: 'Skill created',
    skill_deleted: 'Skill deleted',
    skill_delete_confirm: 'Delete skill "{0}"?',
    skills_empty_title: 'Select a skill',
    skills_empty_sub: 'Pick a skill from the sidebar to view its contents, or create a new one.',
    skills_edit: 'Edit',
    skills_delete: 'Delete',
    skills_back_to: 'Back to {0}',
    tasks_empty_title: 'Select a scheduled job',
    tasks_empty_sub: 'Pick a job from the sidebar to view its details and runs, or create a new one.',
    workspaces_empty_title: 'Select a space',
    workspaces_empty_sub: 'Pick a space from the sidebar to view its files and settings, or add a new one.',
    profiles_empty_title: 'Select a profile',
    profiles_empty_sub: 'Pick an agent profile from the sidebar to view and edit its settings, or create a new one.',
    memory_notes_label: 'memory (notes)',
    memory_saved: 'Memory saved',
    my_notes: 'My Notes',
    memory_project_context_title: 'Project Context',
    memory_project_context_empty: 'No project context file found for this workspace.',
    user_profile: 'User Profile',
    external_notes_sources: 'Third-party notes',
    external_notes_empty: 'No note or knowledge MCP sources are visible yet. Configure Joplin, Obsidian, Notion, llm-wiki, or another notes server to list it here.',
    external_notes_auto_recall_hint: 'Automatic session recall is unchanged; this drawer only shows configured sources and available read/search tools.',
    external_notes_no_tools: 'No read/search tools are currently visible for this source.',
    external_notes_tool_count: (count) => `${count} note tool${Number(count)===1?'':'s'} available`,
    external_notes_configured_hint: 'Tool names are expected from this configured source; live schemas will appear when the WebUI runtime exposes them.',
    external_notes_recent_ai: 'Recently used by AI',
    external_notes_auto: 'auto',
    external_notes_recent_ai_reason: 'Automatic recall',
    external_notes_search_placeholder: 'Search notes…',
    external_notes_search_empty: 'Search a configured notes source to preview notes here.',
    source_active: 'active',
    source_configured: 'configured',
    no_notes_yet: 'No notes yet.',
    no_profile_yet: 'No profile yet.',
    agent_soul: 'Agent Soul',
    no_soul_yet: 'No soul defined yet.',
    workspace_choose_path: 'Choose workspace path',
    workspace_choose_path_meta: 'Add a validated path and switch this conversation',
    workspace_manage: 'Manage workspaces',
    workspace_manage_meta: 'Open the Spaces panel',
    workspace_use_title: 'Use in current session',
    workspace_use: 'Use',
    workspace_add_path_placeholder: 'Add workspace path (e.g. /home/user/my-project)',
    workspace_paths_validated_hint: 'Paths are validated as existing directories before saving.',

    workspace_drag_hint: 'Drag to reorder',
    workspace_reorder_failed: 'Reorder failed',
    workspace_added: 'Workspace added',
    workspace_recovered_notice: (path) => `This session's deleted workspace was recovered to ${path}.`,
    workspace_renamed: 'Workspace renamed',
    workspace_remove_confirm_title: 'Remove workspace',
    workspace_remove_confirm_message: (path) => `Remove "${path}"?`,
    workspace_removed: 'Workspace removed',
    workspace_switch_prompt_title: 'Switch workspace',
    workspace_switch_prompt_message: 'Enter an absolute workspace path to add and switch this conversation to.',
    workspace_switch_prompt_confirm: 'Switch',
    workspace_switch_prompt_placeholder: '/Users/you/project',
    workspace_not_added: 'Workspace was not added',
    workspace_already_saved: 'Workspace already saved — choose it from the list',
    workspace_busy_switch: 'Cannot switch workspace while agent is running',
    discard_file_edits_title: 'Discard file edits?',
    discard_file_edits_message: 'Switching workspaces will discard unsaved file edits in the preview.',
    workspace_switched_to: (name) => `Switched to ${name}`,
    profiles_no_profiles: 'No profiles found.',
    profile_api_keys_configured: 'API keys configured',
    profile_gateway_running: 'Gateway running',
    profile_gateway_stopped: 'Gateway stopped',
    profile_active: 'ACTIVE',
    profile_no_configuration: 'No configuration',
    profile_skill_count: (count) => `${count} skill${count === 1 ? '' : 's'}`,
    profile_use: 'Use',
    profile_switch_title: 'Switch to this profile',
    profile_delete_title: 'Delete this profile',
    profile_default_label: '(default)',
    profile_concept_title: 'Profiles vs workspaces',
    profile_concept_subtitle: 'Use profiles for how the agent works; use workspaces for what files it works on.',
    profile_concept_desc_profiles: 'Agent identity, memory, skills, model/provider config, and connected tools. Create profiles for roles like researcher, writer, marketer, or developer when those roles should carry different context or capabilities.',
    profile_concept_desc_workspaces: 'Project or product folders on disk. Use one workspace per repo/product so chat, terminal, and file browsing point at the right files.',
    profile_concept_desc_together: 'A profile can have a default workspace, but you can switch workspaces any time. Profiles answer "who is working?"; workspaces answer "where are they working?"',
    profile_concept_example: 'Example: create a "researcher" profile with web-search skills enabled and a "my-app" workspace pointing at your project folder. The researcher profile remembers your research preferences; the workspace keeps chat, terminal, and file browsing pointed at that project.',
    profile_concept_label_together: 'Together',
    profile_concept_label_example: 'Example',
    profile_name_placeholder: 'Profile name (lowercase, a-z 0-9 hyphens)',
    profile_clone_label: 'Clone config from active profile',
    profile_model_label: 'Model / provider',
    profile_model_hint: 'Choose from configured providers and models for this new profile.',
    profile_model_use_default: 'Use active profile default',
    profile_base_url_placeholder: 'Base URL (optional, e.g. http://localhost:11434)',
    profile_api_key_placeholder: 'API key (optional)',
    manage_profiles: 'Manage profiles',
    profiles_load_failed: 'Failed to load profiles',
    profile_switched_new_conversation: (name) => `Switched to profile: ${name} — new conversation started`,
    profile_switched: (name) => `Switched to profile: ${name}`,
    profile_name_rule: 'Lowercase letters, numbers, hyphens, underscores only',
    profile_base_url_rule: 'Base URL must start with http:// or https://',
    profile_created: (name) => `Profile created: ${name}`,
    profile_delete_confirm_title: (name) => `Delete profile "${name}"?`,
    profile_delete_confirm_message: 'All sessions, config, skills, and memory for this profile will be permanently deleted. This cannot be undone.',
    profile_deleted: (name) => `Profile deleted: ${name}`,
    active_conversation_none: 'No active conversation selected.',
    active_conversation_meta: (title, count) => `${title} · ${count} message${count === 1 ? '' : 's'}`,
    settings_unsaved_changes: 'You have unsaved changes.',
    sign_out_failed: 'Sign out failed: ',
    disable_auth_confirm_title: 'Disable password protection',
    disable_auth_confirm_message: 'Anyone will be able to access this instance.',
    auth_disabled: 'Auth disabled — password protection removed',
    disable_auth_failed: 'Failed to disable auth: ',
    current_password_label: 'Current Password',
    current_password_placeholder: 'Enter current password\u2026',
    current_password_required: 'Current password is required to change or disable authentication.',
    current_password_incorrect: 'Current password is incorrect.',
    disable_auth_typed_confirm: 'Type DISABLE AUTH to confirm',
    auth_status_password: 'Password Auth',
    auth_status_passkey_only: 'Passkey Only',
    auth_status_unauthenticated: 'Unauthenticated',
    auth_warning_badge: 'Auth disabled',
    auth_disabled_warning_message: 'This instance is accessible without authentication.',
    auth_acknowledged_label: 'I’ve reviewed this risk; show a less-urgent nav warning.',
    auth_ack_save_failed: 'Failed to update acknowledgement: ',
    bg_error_single: (title) => `"${title}" has encountered an error`,
    bg_error_multi: (count) => `${count} sessions have encountered an error`,
    // skill form
    skill_name: 'Name',
    skill_category: 'Category',
    skill_category_placeholder: 'Optional, e.g. devops',
    skill_content: 'SKILL.md content',
    skill_content_placeholder: 'YAML frontmatter + markdown body',
    skill_rename_not_supported: 'Renaming a skill is not supported. Create a new skill and delete the old one to rename.',
    skill_metadata: 'Metadata',
    // cron form
    cron_name_label: 'Name',
    cron_name_placeholder: 'Optional',
    cron_schedule_label: 'Cron expression',
    cron_schedule_preset_label: 'Schedule',
    cron_schedule_preset_hourly: 'Hourly',
    cron_schedule_preset_daily: 'Daily',
    cron_schedule_preset_weekdays: 'Weekdays (Mon–Fri)',
    cron_schedule_preset_weekly: 'Weekly',
    cron_schedule_preset_monthly: 'Monthly',
    cron_schedule_preset_custom: 'Custom',
    cron_schedule_hour_label: 'Hour',
    cron_schedule_minute_label: 'Minute',
    cron_schedule_weekday_label: 'Day of week',
    cron_schedule_month_day_label: 'Day of month',
    cron_schedule_time_label: 'Time',
    cron_schedule_conj_on: 'on',
    cron_schedule_conj_on_day: 'on day',
    cron_schedule_conj_at: 'at',
    cron_schedule_conj_at_minute: 'at minute',
    cron_weekday_sun: 'Sunday',
    cron_weekday_mon: 'Monday',
    cron_weekday_tue: 'Tuesday',
    cron_weekday_wed: 'Wednesday',
    cron_weekday_thu: 'Thursday',
    cron_weekday_fri: 'Friday',
    cron_weekday_sat: 'Saturday',
    cron_schedule_time_hint: 'Time is server time; cron runs server-side.',
    cron_schedule_hint: "Use 'every 1h' or a cron expression for recurring jobs. Bare durations like '30m' run once.",
    cron_schedule_once_warning: "Duration forms like '30m' run once and are removed after running. Use 'every 30m' to keep a recurring job.",
    cron_prompt_label: 'Prompt',
    cron_deliver_label: 'Deliver output to',
    cron_deliver_local: 'Local (save output only)',
    cron_deliver_custom: 'Custom delivery target',
    cron_profile_label: 'Profile',
    cron_profile_server_default: 'server default',
    cron_profile_server_default_hint: 'Uses the WebUI server default profile at run time. Existing jobs without a profile keep this legacy behavior.',
    cron_toast_notifications_label: 'Completion toasts',
    cron_toast_notifications_hint: 'Show a toast when this cron finishes.',
    cron_toast_notifications_enabled: 'Enabled',
    cron_toast_notifications_disabled: 'Disabled',
    cron_badge_notifications_label: 'Tasks badge',
    cron_badge_notifications_hint: 'Count this job\u0027s completions in the Tasks badge and new-run marker. Turn off for high-frequency silent jobs.',
    cron_badge_notifications_enabled: 'Enabled',
    cron_badge_notifications_disabled: 'Disabled',
    cron_skills_label: 'Skills',
    cron_skills_placeholder: 'Add skills (optional)…',
    cron_skills_edit_hint: 'Skill list is not editable after creation.',
    // workspace form
    workspace_name_label: 'Name',
    workspace_name_placeholder: 'Optional friendly name',
    workspace_path_label: 'Path',
    workspace_path_required: 'Path is required',
    workspace_path_readonly: 'Path cannot be changed. Rename only.',
    workspace_new_title: 'New space',
    // profile form
    profile_name_label: 'Name',
    profile_base_url_label: 'Base URL',
    profile_api_key_label: 'API key',
    cmd_yolo: 'Toggle YOLO mode (skip approvals)',
    yolo_no_session: 'No active session',
    yolo_enabled: '⚡ YOLO mode ON — approvals skipped this session',
    yolo_disabled: 'YOLO mode OFF',
    yolo_pill_label: 'YOLO',
    yolo_pill_title_active: 'YOLO mode active — click to disable',
    approval_skip_all: 'Skip all this session',
    approval_skip_all_title: 'Skip all approval prompts for this session',
    // composer action tooltips
    composer_send: 'Send message',
    composer_queue: 'Queue message',
    composer_interrupt: 'Interrupt and send',
    composer_steer: 'Steer current response',
    composer_stop: 'Stop generation',
    composer_disabled_clarify: 'Respond to the clarification request',
    composer_disabled_compression: 'Waiting for compression to finish',
    composer_compression_will_queue: 'Type a message — it will queue and send after compression',
    composer_disabled_empty: 'Type a message to send',
    composer_mobile_workspace: 'Workspace',
    composer_mobile_model: 'Model',
    composer_mobile_quota: 'Quota',
    composer_mobile_reasoning: 'Reasoning',
    composer_mobile_context: 'Context',
    media_audio_label: 'Audio',
    media_svg_label: 'Diagram',
    media_video_label: 'Video',
    media_download: 'Download',
    csv_loading: 'Loading CSV',
    csv_too_large: 'CSV file too large for inline rendering',
    csv_no_data: 'CSV file has insufficient data to render as table',
    csv_error: 'Failed to load CSV file',
    csv_header_note: 'First row shown as table header',
    excalidraw_loading: 'Loading diagram',
    excalidraw_too_large: 'Excalidraw file too large for inline rendering',
    excalidraw_invalid: 'Invalid Excalidraw file format',
    excalidraw_error: 'Failed to load Excalidraw file',
    excalidraw_label: 'Diagram',
    excalidraw_download: 'Download',
    excalidraw_empty: 'Empty diagram',
    excalidraw_render_error: 'Failed to render diagram',
    excalidraw_simplified: 'Simplified SVG preview — not pixel-identical to Excalidraw canvas',
    // ── Checkpoints / Rollback ──
    checkpoint_title: 'Checkpoints',
    checkpoint_empty: 'No checkpoints found for this workspace.',
    checkpoint_loading: 'Loading checkpoints…',
    checkpoint_error: 'Failed to load checkpoints',
    checkpoint_date: 'Date',
    checkpoint_message: 'Message',
    checkpoint_files: 'Files',
    checkpoint_view_diff: 'View diff',
    checkpoint_restore: 'Restore',
    checkpoint_restore_confirm_title: 'Restore checkpoint?',
    checkpoint_restore_confirm_message: (ckpt) => `Restore workspace to checkpoint "${ckpt}"? This will overwrite files with the saved versions. Files added after this checkpoint will not be deleted.`,
    checkpoint_restored: 'Checkpoint restored',
    checkpoint_diff_title: 'Changes in checkpoint',
    checkpoint_diff_no_changes: 'No differences found between this checkpoint and the current workspace.',
    checkpoint_diff_files_changed: (n) => `${n} file${n === 1 ? '' : 's'} changed`,
    // ── Conversation Outline (#2124) ──
    outline_title: 'Outline',
    outline_empty: 'No questions yet.',
    outline_loading: 'Loading…',
    img_lightbox_fit_title: 'Reset zoom to fit (F)',
    pb_bindings_title: 'Project settings — {0}',
    pb_bindings_menu: 'Project settings…',
    pb_close: 'Close',
    pb_field_workspaces: 'Workspaces',
    pb_field_model: 'Model',
    pb_none_inherit: '(none) — inherit default',
    pb_no_workspaces: 'No workspaces bound',
    pb_no_options: 'No options',
    pb_mark_default: '★ Default',
    pb_set_default: 'Set default',
    pb_set_default_title: 'Use this workspace for new chats from the + button',
    pb_unbind_ws_title: 'Unbind this workspace',
    pb_add_workspace_placeholder: 'Add workspace…',
    pb_type_path: 'Type a path…',
    pb_enter_ws_path: 'Enter a new workspace path',
    pb_add: 'Add',
    pb_add_workspace_title: 'Add workspace',
    pb_add_workspace_message: 'Workspace path to bind to this project (auto-registered in the saved list):',
    pb_workspace_already_bound: 'Workspace already bound',
    pb_cancel: 'Cancel',
    pb_save: 'Save',
    pb_updated: 'Project settings saved',
    pb_update_failed: 'Could not save project settings: ',
  },
};

/**
 * Eager locale metadata manifest — one row per shipped language.
 *
 * Settings renders its language selector from this (panels.js) and
 * `resolveLocale()` validates against it, so listing every language never
 * costs downloading a translation bundle.
 */
const LOCALE_META = {
  "en": { _label: "English", _lang: "en", _speech: "en-US" },
  "it": { _label: "Italiano", _lang: "it", _speech: "it-IT" },
  "ja": { _label: "日本語", _lang: "ja", _speech: "ja-JP" },
  "ru": { _label: "Русский", _lang: "ru", _speech: "ru-RU" },
  "es": { _label: "Español", _lang: "es", _speech: "es-ES" },
  "de": { _label: "Deutsch", _lang: "de", _speech: "de-DE" },
  "zh": { _label: "简体中文", _lang: "zh", _speech: "zh-CN" },
  "zh-Hant": { _label: "繁體中文", _lang: "zh-Hant", _speech: "zh-TW" },
  "pt": { _label: "Português", _lang: "pt", _speech: "pt-BR" },
  "ko": { _label: "한국어", _lang: "ko", _speech: "ko-KR" },
  "fr": { _label: "Français", _lang: "fr", _speech: "fr-FR" },
  "cs": { _label: "Čeština", _lang: "cs", _speech: "cs-CZ" },
  "tr": { _label: "Türkçe", _lang: "tr", _speech: "tr-TR" },
  "pl": { _label: "Polski", _lang: "pl", _speech: "pl-PL" },
  "vi": { _label: "Tiếng Việt", _lang: "vi", _speech: "vi-VN" },
};

/**
 * Register a locale bundle. Called by each `static/locales/<code>.js` file.
 * Bundles never mutate `_locale` directly — applying a locale stays
 * `setLocale()`'s job, so a bundle arriving late cannot half-update the UI.
 * @param {string} code
 * @param {Object} bundle
 */
function registerLocale(code, bundle) {
  if (typeof code !== 'string' || !code) return;
  if (!bundle || typeof bundle !== 'object') return;
  LOCALES[code] = bundle;
}

/** @type {Object<string, Promise<boolean>>} in-flight `ensureLocale()` promises */
const _localeLoads = {};

/**
 * Load a locale bundle on demand.
 *
 * English is always resident, so a 404, an offline miss, or a Subresource
 * failure degrades to English rather than to a blank UI. The resolved URL keeps
 * the `?v=` version query that index.html and sw.js use, so the service
 * worker's network-first cache still matches and a cached locale works offline.
 *
 * @param {string} code
 * @returns {Promise<boolean>} true when the requested bundle is available
 */
function ensureLocale(code) {
  const resolved = resolveLocale(code);
  if (!resolved) return Promise.resolve(false);
  if (LOCALES[resolved]) return Promise.resolve(true);
  if (_localeLoads[resolved]) return _localeLoads[resolved];

  _localeLoads[resolved] = new Promise((resolve) => {
    if (typeof document === 'undefined' || !document.createElement) { resolve(false); return; }
    const el = document.createElement('script');
    const bundleVersion = (typeof window !== 'undefined' && window.__HERMES_WEBUI_BUNDLE_VERSION__) || '';
    el.src = 'static/locales/' + encodeURIComponent(resolved) + '.js' + (bundleVersion ? '?v=' + bundleVersion : '');
    el.async = true;
    el.onload = () => resolve(!!LOCALES[resolved]);
    // Transient failures must not poison the cache: drop the entry so a later
    // call (locale switch back, reconnect) retries instead of resolving false
    // forever.
    el.onerror = () => { delete _localeLoads[resolved]; resolve(false); };
    (document.head || document.documentElement).appendChild(el);
  });
  return _localeLoads[resolved];
}

const _I18N_TOOL_ACTION_TEXT_EN = {
    shell: { running: 'Running', done: 'Ran', fail: 'run', fallback: 'a command' },
    read: { running: 'Reading', done: 'Read', fail: 'read', fallback: 'a file' },
    list: { running: 'Listing', done: 'Listed', fail: 'list', fallback: 'files' },
    search: { running: 'Searching', done: 'Searched', fail: 'search', fallback: 'workspace' },
    web: { running: 'Checking', done: 'Checked', fail: 'check', fallback: 'web data' },
    write: { running: 'Updating', done: 'Updated', fail: 'update', fallback: 'a file' },
    skill: { running: 'Loading', done: 'Loaded', fail: 'load', fallback: 'a skill' },
    memory: { running: 'Saving', done: 'Saved', fail: 'save', fallback: 'memory' },
    delegate: { running: 'Delegating', done: 'Delegated', fail: 'delegate', fallback: 'a task' },
    unknown: { running: 'Running', done: 'Ran', fail: 'run', fallback: 'a tool' },
};
const _I18N_TOOL_SUMMARY_TEXT_EN = {
    shell: { running: ['Running a command', 'Running {n} commands'], done: ['Ran a command', 'Ran {n} commands'] },
    read: { running: ['Reading a file', 'Reading {n} files'], done: ['Read a file', 'Read {n} files'] },
    list: { running: ['Listing files', 'Listing {n} items'], done: ['Listed files', 'Listed {n} files'] },
    search: { running: ['Searching workspace', 'Searching workspace {n} times'], done: ['Searched workspace', 'Searched workspace {n} times'] },
    web: { running: ['Checking web', 'Checking web {n} times'], done: ['Checked the web', 'Checked the web {n} times'] },
    write: { running: ['Updating a file', 'Updating {n} files'], done: ['Updated a file', 'Updated {n} files'] },
    skill: { running: ['Loading a skill', 'Loading {n} skills'], done: ['Loaded a skill', 'Loaded {n} skills'] },
    memory: { running: ['Saving memory', 'Saving {n} memory updates'], done: ['Saved memory', 'Saved {n} memory updates'] },
    delegate: { running: ['Delegating a task', 'Delegating {n} tasks'], done: ['Delegated a task', 'Delegated {n} tasks'] },
    unknown: { running: ['Running a tool', 'Running {n} tools'], done: ['Ran a tool', 'Ran {n} tools'] },
};
function _i18nRuPlural(n, one, few, many) {
  const value = Math.abs(Number(n) || 0);
  const mod10 = value % 10;
  const mod100 = value % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) return few;
  return many;
}
const _I18N_TOOL_ACTION_TEXT_RU = {
    shell: { running: 'Выполняю', done: 'Выполнена', fail: 'выполнить', fallback: 'команду' },
    read: { running: 'Читаю', done: 'Прочитан', fail: 'прочитать', fallback: 'файл' },
    list: { running: 'Показываю список', done: 'Показан список', fail: 'показать список', fallback: 'файлов' },
    search: { running: 'Ищу', done: 'Поиск выполнен', fail: 'найти', fallback: 'в рабочей области' },
    web: { running: 'Проверяю', done: 'Проверено', fail: 'проверить', fallback: 'веб-данные' },
    write: { running: 'Обновляю', done: 'Обновлён', fail: 'обновить', fallback: 'файл' },
    skill: { running: 'Загружаю', done: 'Загружен', fail: 'загрузить', fallback: 'навык' },
    memory: { running: 'Сохраняю', done: 'Сохранена', fail: 'сохранить', fallback: 'память' },
    delegate: { running: 'Делегирую', done: 'Делегировано', fail: 'делегировать', fallback: 'задачу' },
    unknown: { running: 'Выполняю', done: 'Выполнен', fail: 'выполнить', fallback: 'инструмент' },
};
const _I18N_TOOL_SUMMARY_TEXT_RU = {
    shell: { running: ['Выполняется команда', 'Выполняются {n} команды', 'Выполняется {n} команд'], done: ['Выполнена команда', 'Выполнены {n} команды', 'Выполнено {n} команд'] },
    read: { running: ['Читается файл', 'Читаются {n} файла', 'Читается {n} файлов'], done: ['Прочитан файл', 'Прочитаны {n} файла', 'Прочитано {n} файлов'] },
    list: { running: ['Показывается список файлов', 'Показываются {n} списка', 'Показывается {n} списков'], done: ['Показан список файлов', 'Показаны {n} списка', 'Показано {n} списков'] },
    search: { running: ['Идёт поиск в рабочей области', 'Идут {n} поиска в рабочей области', 'Идёт {n} поисков в рабочей области'], done: ['Поиск в рабочей области выполнен', 'Выполнены {n} поиска в рабочей области', 'Выполнено {n} поисков в рабочей области'] },
    web: { running: ['Выполняется проверка веба', 'Выполняются {n} проверки веба', 'Выполняется {n} проверок веба'], done: ['Проверка веба выполнена', 'Выполнены {n} проверки веба', 'Выполнено {n} проверок веба'] },
    write: { running: ['Обновляется файл', 'Обновляются {n} файла', 'Обновляется {n} файлов'], done: ['Обновлён файл', 'Обновлены {n} файла', 'Обновлено {n} файлов'] },
    skill: { running: ['Загружается навык', 'Загружаются {n} навыка', 'Загружается {n} навыков'], done: ['Загружен навык', 'Загружены {n} навыка', 'Загружено {n} навыков'] },
    memory: { running: ['Сохраняется память', 'Сохраняются {n} обновления памяти', 'Сохраняется {n} обновлений памяти'], done: ['Память сохранена', 'Сохранены {n} обновления памяти', 'Сохранено {n} обновлений памяти'] },
    delegate: { running: ['Делегируется задача', 'Делегируются {n} задачи', 'Делегируется {n} задач'], done: ['Задача делегирована', 'Делегированы {n} задачи', 'Делегировано {n} задач'] },
    unknown: { running: ['Выполняется инструмент', 'Выполняются {n} инструмента', 'Выполняется {n} инструментов'], done: ['Инструмент выполнен', 'Выполнены {n} инструмента', 'Выполнено {n} инструментов'] },
};
const _I18N_TOOL_ACTION_TEXT_ZH = {
    shell: { running: '正在运行', done: '已运行', fail: '运行', fallback: '命令' },
    read: { running: '正在读取', done: '已读取', fail: '读取', fallback: '文件' },
    list: { running: '正在列出', done: '已列出', fail: '列出', fallback: '文件' },
    search: { running: '正在搜索', done: '已搜索', fail: '搜索', fallback: '代码' },
    web: { running: '正在检查', done: '已检查', fail: '检查', fallback: '网页' },
    write: { running: '正在更新', done: '已更新', fail: '更新', fallback: '文件' },
    skill: { running: '正在读取', done: '已读取', fail: '读取', fallback: '技能' },
    memory: { running: '正在保存', done: '已保存', fail: '保存', fallback: '记忆' },
    delegate: { running: '正在委派', done: '已委派', fail: '委派', fallback: '任务' },
    unknown: { running: '正在运行', done: '已运行', fail: '运行', fallback: '工具' },
};
const _I18N_TOOL_SUMMARY_TEXT_ZH = {
    shell: { running: ['正在运行命令', '正在运行 {n} 条命令'], done: ['已运行命令', '已运行 {n} 条命令'] },
    read: { running: ['正在读取文件', '正在读取 {n} 个文件'], done: ['已读取文件', '已读取 {n} 个文件'] },
    list: { running: ['正在列出文件', '正在列出 {n} 个项目'], done: ['已列出文件', '已列出 {n} 个项目'] },
    search: { running: ['正在搜索代码', '正在搜索代码 {n} 次'], done: ['已搜索代码', '已搜索代码 {n} 次'] },
    web: { running: ['正在检查网页', '正在检查网页 {n} 次'], done: ['已检查网页', '已检查网页 {n} 次'] },
    write: { running: ['正在更新文件', '正在更新 {n} 个文件'], done: ['已更新文件', '已更新 {n} 个文件'] },
    skill: { running: ['正在读取技能', '正在读取 {n} 个技能'], done: ['已读取技能', '已读取 {n} 个技能'] },
    memory: { running: ['正在保存记忆', '正在保存 {n} 条记忆'], done: ['已保存记忆', '已保存 {n} 条记忆'] },
    delegate: { running: ['正在委派任务', '正在委派 {n} 个任务'], done: ['已委派任务', '已委派 {n} 个任务'] },
    unknown: { running: ['正在运行工具', '正在运行 {n} 个工具'], done: ['已运行工具', '已运行 {n} 个工具'] },
};
const _I18N_TOOL_ACTION_TEXT_ZH_HANT = {
    shell: { running: '正在執行', done: '已執行', fail: '執行', fallback: '命令' },
    read: { running: '正在讀取', done: '已讀取', fail: '讀取', fallback: '檔案' },
    list: { running: '正在列出', done: '已列出', fail: '列出', fallback: '檔案' },
    search: { running: '正在搜尋', done: '已搜尋', fail: '搜尋', fallback: '程式碼' },
    web: { running: '正在檢查', done: '已檢查', fail: '檢查', fallback: '網頁' },
    write: { running: '正在更新', done: '已更新', fail: '更新', fallback: '檔案' },
    skill: { running: '正在讀取', done: '已讀取', fail: '讀取', fallback: '技能' },
    memory: { running: '正在儲存', done: '已儲存', fail: '儲存', fallback: '記憶' },
    delegate: { running: '正在委派', done: '已委派', fail: '委派', fallback: '任務' },
    unknown: { running: '正在執行', done: '已執行', fail: '執行', fallback: '工具' },
};
const _I18N_TOOL_SUMMARY_TEXT_ZH_HANT = {
    shell: { running: ['正在執行命令', '正在執行 {n} 條命令'], done: ['已執行命令', '已執行 {n} 條命令'] },
    read: { running: ['正在讀取檔案', '正在讀取 {n} 個檔案'], done: ['已讀取檔案', '已讀取 {n} 個檔案'] },
    list: { running: ['正在列出檔案', '正在列出 {n} 個項目'], done: ['已列出檔案', '已列出 {n} 個項目'] },
    search: { running: ['正在搜尋程式碼', '正在搜尋程式碼 {n} 次'], done: ['已搜尋程式碼', '已搜尋程式碼 {n} 次'] },
    web: { running: ['正在檢查網頁', '正在檢查網頁 {n} 次'], done: ['已檢查網頁', '已檢查網頁 {n} 次'] },
    write: { running: ['正在更新檔案', '正在更新 {n} 個檔案'], done: ['已更新檔案', '已更新 {n} 個檔案'] },
    skill: { running: ['正在讀取技能', '正在讀取 {n} 個技能'], done: ['已讀取技能', '已讀取 {n} 個技能'] },
    memory: { running: ['正在儲存記憶', '正在儲存 {n} 條記憶'], done: ['已儲存記憶', '已儲存 {n} 條記憶'] },
    delegate: { running: ['正在委派任務', '正在委派 {n} 個任務'], done: ['已委派任務', '已委派 {n} 個任務'] },
    unknown: { running: ['正在執行工具', '正在執行 {n} 個工具'], done: ['已執行工具', '已執行 {n} 個工具'] },
};
function _i18nProcessedElapsed(prefix, duration) {
  return duration ? `${prefix} ${duration}` : prefix;
}
function _i18nProcessedElapsedEn(duration) {
  return _i18nProcessedElapsed('Processed', duration);
}
function _i18nProcessedElapsedRu(duration) {
  return _i18nProcessedElapsed('Обработано', duration);
}
function _i18nProcessedElapsedZh(duration) {
  return _i18nProcessedElapsed('已处理', duration);
}
function _i18nProcessedElapsedZhHant(duration) {
  return _i18nProcessedElapsed('已處理', duration);
}
function _i18nToolActionLabelFromMap(map, kind, state, target, display, failed) {
  const verbs = map[kind] || map.unknown || _I18N_TOOL_ACTION_TEXT_EN.unknown;
  const object = target || verbs.fallback || display || 'tool';
  if (failed) return `Failed to ${verbs.fail || 'run'} ${object}`;
  return `${verbs[state] || verbs.running} ${object}`;
}
function _i18nToolActionLabelEn(kind, state, target, display, failed) {
  return _i18nToolActionLabelFromMap(_I18N_TOOL_ACTION_TEXT_EN, kind, state, target, display, failed);
}
function _i18nToolActionLabelRu(kind, state, target, display, failed) {
  const verbs = _I18N_TOOL_ACTION_TEXT_RU[kind] || _I18N_TOOL_ACTION_TEXT_RU.unknown;
  const object = target || verbs.fallback || display || 'инструмент';
  if (failed) return `Не удалось ${verbs.fail || 'выполнить'} ${object}`;
  return `${verbs[state] || verbs.running} ${object}`;
}
function _i18nToolActionLabelZh(kind, state, target, display, failed) {
  if (failed) {
    const verbs = _I18N_TOOL_ACTION_TEXT_ZH[kind] || _I18N_TOOL_ACTION_TEXT_ZH.unknown;
    return `未能${verbs.fail || '运行'} ${target || verbs.fallback || display || '工具'}`;
  }
  return _i18nToolActionLabelFromMap(_I18N_TOOL_ACTION_TEXT_ZH, kind, state, target, display, failed);
}
function _i18nToolActionLabelZhHant(kind, state, target, display, failed) {
  if (failed) {
    const verbs = _I18N_TOOL_ACTION_TEXT_ZH_HANT[kind] || _I18N_TOOL_ACTION_TEXT_ZH_HANT.unknown;
    return `未能${verbs.fail || '執行'} ${target || verbs.fallback || display || '工具'}`;
  }
  return _i18nToolActionLabelFromMap(_I18N_TOOL_ACTION_TEXT_ZH_HANT, kind, state, target, display, failed);
}
function _i18nToolWorklogSummaryFromMap(map, kind, state, count) {
  const n = Math.max(1, Number(count) || 1);
  const form = (map[kind] || map.unknown || _I18N_TOOL_SUMMARY_TEXT_EN.unknown)[state] || map.unknown.running;
  return (n === 1 ? form[0] : form[1]).replace('{n}', String(n));
}
function _i18nToolWorklogSummaryEn(kind, state, count) {
  return _i18nToolWorklogSummaryFromMap(_I18N_TOOL_SUMMARY_TEXT_EN, kind, state, count);
}
function _i18nToolWorklogSummaryRu(kind, state, count) {
  const n = Math.max(1, Number(count) || 1);
  const form = (_I18N_TOOL_SUMMARY_TEXT_RU[kind] || _I18N_TOOL_SUMMARY_TEXT_RU.unknown)[state]
    || _I18N_TOOL_SUMMARY_TEXT_RU.unknown.running;
  const picked = _i18nRuPlural(n, form[0], form[1], form[2]);
  return picked.replace('{n}', String(n));
}
function _i18nToolWorklogSummaryZh(kind, state, count) {
  return _i18nToolWorklogSummaryFromMap(_I18N_TOOL_SUMMARY_TEXT_ZH, kind, state, count);
}
function _i18nToolWorklogSummaryZhHant(kind, state, count) {
  return _i18nToolWorklogSummaryFromMap(_I18N_TOOL_SUMMARY_TEXT_ZH_HANT, kind, state, count);
}
function _i18nToolSummaryJoinEn(parts) {
  return Array.isArray(parts) ? parts.filter(Boolean).join(', ') : '';
}
function _i18nToolSummaryJoinRu(parts) {
  return Array.isArray(parts) ? parts.filter(Boolean).join(', ') : '';
}
function _i18nToolSummaryJoinCjk(parts) {
  const items = Array.isArray(parts) ? parts.filter(Boolean) : [];
  if (items.length <= 1) return items[0] || '';
  if (items.length === 2) return `${items[0]}和${items[1]}`;
  return `${items.slice(0, -1).join('、')}和${items[items.length - 1]}`;
}

const _I18N_TOOL_ACTION_TEXT_VI = {
    shell: { running: 'Đang chạy', done: 'Đã chạy', fail: 'chạy', fallback: 'lệnh' },
    read: { running: 'Đang đọc', done: 'Đã đọc', fail: 'đọc', fallback: 'tệp' },
    list: { running: 'Đang liệt kê', done: 'Đã liệt kê', fail: 'liệt kê', fallback: 'tệp' },
    search: { running: 'Đang tìm kiếm', done: 'Đã tìm kiếm', fail: 'tìm kiếm', fallback: 'workspace' },
    web: { running: 'Đang kiểm tra', done: 'Đã kiểm tra', fail: 'kiểm tra', fallback: 'dữ liệu web' },
    write: { running: 'Đang cập nhật', done: 'Đã cập nhật', fail: 'cập nhật', fallback: 'tệp' },
    skill: { running: 'Đang tải', done: 'Đã tải', fail: 'tải', fallback: 'kỹ năng' },
    memory: { running: 'Đang lưu', done: 'Đã lưu', fail: 'lưu', fallback: 'bộ nhớ' },
    delegate: { running: 'Đang ủy quyền', done: 'Đã ủy quyền', fail: 'ủy quyền', fallback: 'tác vụ' },
    unknown: { running: 'Đang chạy', done: 'Đã chạy', fail: 'chạy', fallback: 'công cụ' },
};
const _I18N_TOOL_SUMMARY_TEXT_VI = {
    shell: { running: ['Đang chạy lệnh', 'Đang chạy {n} lệnh'], done: ['Đã chạy lệnh', 'Đã chạy {n} lệnh'] },
    read: { running: ['Đang đọc tệp', 'Đang đọc {n} tệp'], done: ['Đã đọc tệp', 'Đã đọc {n} tệp'] },
    list: { running: ['Đang liệt kê tệp', 'Đang liệt kê {n} mục'], done: ['Đã liệt kê tệp', 'Đã liệt kê {n} tệp'] },
    search: { running: ['Đang tìm kiếm workspace', 'Đang tìm kiếm workspace {n} lần'], done: ['Đã tìm kiếm workspace', 'Đã tìm kiếm workspace {n} lần'] },
    web: { running: ['Đang kiểm tra web', 'Đang kiểm tra web {n} lần'], done: ['Đã kiểm tra web', 'Đã kiểm tra web {n} lần'] },
    write: { running: ['Đang cập nhật tệp', 'Đang cập nhật {n} tệp'], done: ['Đã cập nhật tệp', 'Đã cập nhật {n} tệp'] },
    skill: { running: ['Đang tải kỹ năng', 'Đang tải {n} kỹ năng'], done: ['Đã tải kỹ năng', 'Đã tải {n} kỹ năng'] },
    memory: { running: ['Đang lưu bộ nhớ', 'Đang lưu {n} cập nhật bộ nhớ'], done: ['Đã lưu bộ nhớ', 'Đã lưu {n} cập nhật bộ nhớ'] },
    delegate: { running: ['Đang ủy quyền tác vụ', 'Đang ủy quyền {n} tác vụ'], done: ['Đã ủy quyền tác vụ', 'Đã ủy quyền {n} tác vụ'] },
    unknown: { running: ['Đang chạy công cụ', 'Đang chạy {n} công cụ'], done: ['Đã chạy công cụ', 'Đã chạy {n} công cụ'] },
};
function _i18nProcessedElapsedVi(duration) {
  return _i18nProcessedElapsed('Đã xử lý', duration);
}
function _i18nToolActionLabelVi(kind, state, target, display, failed) {
  const verbs = _I18N_TOOL_ACTION_TEXT_VI[kind] || _I18N_TOOL_ACTION_TEXT_VI.unknown;
  const object = target || verbs.fallback || display || 'công cụ';
  if (failed) return `Không thể ${verbs.fail || 'chạy'} ${object}`;
  return `${verbs[state] || verbs.running} ${object}`;
}
function _i18nToolWorklogSummaryVi(kind, state, count) {
  return _i18nToolWorklogSummaryFromMap(_I18N_TOOL_SUMMARY_TEXT_VI, kind, state, count);
}

const _I18N_TOOL_ACTION_TEXT_PL = {
    shell: { running: 'Uruchamianie', done: 'Uruchomiono', fail: 'uruchomić', fallback: 'polecenie' },
    read: { running: 'Odczytywanie', done: 'Odczytano', fail: 'odczytać', fallback: 'plik' },
    list: { running: 'Listowanie', done: 'Wylistowano', fail: 'wylistować', fallback: 'plik' },
    search: { running: 'Przeszukiwanie', done: 'Przeszukano', fail: 'przeszukać', fallback: 'obszar roboczy' },
    web: { running: 'Sprawdzanie', done: 'Sprawdzono', fail: 'sprawdzić', fallback: 'dane internetowe' },
    write: { running: 'Zapisywanie', done: 'Zapisano', fail: 'zapisać', fallback: 'plik' },
    skill: { running: 'Wczytywanie', done: 'Wczytano', fail: 'wczytać', fallback: 'umiejętność' },
    memory: { running: 'Zapisywanie', done: 'Zapisano', fail: 'zapisać', fallback: 'pamięć' },
    delegate: { running: 'Delegowanie', done: 'Delegowano', fail: 'delegować', fallback: 'zadanie' },
    unknown: { running: 'Uruchamianie', done: 'Uruchomiono', fail: 'uruchomić', fallback: 'narzędzie' },
};
const _I18N_TOOL_SUMMARY_TEXT_PL = {
    shell: { running: ['Uruchamianie polecenia', 'Uruchamianie {n} poleceń'], done: ['Uruchomiono polecenie', 'Uruchomiono {n} poleceń'] },
    read: { running: ['Odczytywanie pliku', 'Odczytywanie {n} plików'], done: ['Odczytano plik', 'Odczytano {n} plików'] },
    list: { running: ['Listowanie pliku', 'Listowanie {n} plików'], done: ['Wylistowano plik', 'Wylistowano {n} plików'] },
    search: { running: ['Przeszukiwanie obszaru roboczego', 'Przeszukiwanie {n}-krotne obszaru roboczego'], done: ['Przeszukano obszar roboczy', 'Przeszukano {n}-krotnie obszar roboczy'] },
    web: { running: ['Sprawdzanie stron', 'Sprawdzanie {n} stron'], done: ['Sprawdzono strony', 'Sprawdzono {n} stron'] },
    write: { running: ['Zapisywanie pliku', 'Zapisywanie {n} plików'], done: ['Zapisano plik', 'Zapisano {n} plików'] },
    skill: { running: ['Wczytywanie umiejętności', 'Wczytywanie {n} umiejętności'], done: ['Wczytano umiejętność', 'Wczytano {n} umiejętności'] },
    memory: { running: ['Zapisywanie pamięci', 'Zapisywanie {n} wpisów pamięci'], done: ['Zapisano pamięć', 'Zapisano {n} wpisów pamięci'] },
    delegate: { running: ['Delegowanie zadania', 'Delegowanie {n} zadań'], done: ['Delegowano zadanie', 'Delegowano {n} zadań'] },
    unknown: { running: ['Uruchamianie narzędzia', 'Uruchamianie {n} narzędzi'], done: ['Uruchomiono narzędzie', 'Uruchomiono {n} narzędzi'] },
};
function _i18nProcessedElapsedPl(duration) {
  return _i18nProcessedElapsed('Przetworzono', duration);
}
function _i18nToolActionLabelPl(kind, state, target, display, failed) {
  const verbs = _I18N_TOOL_ACTION_TEXT_PL[kind] || _I18N_TOOL_ACTION_TEXT_PL.unknown;
  const object = target || verbs.fallback || display || 'narzędzie';
  if (failed) return `Nie udało się ${verbs.fail || 'uruchomić'} ${object}`;
  return `${verbs[state] || verbs.running} ${object}`;
}
function _i18nToolWorklogSummaryPl(kind, state, count) {
  return _i18nToolWorklogSummaryFromMap(_I18N_TOOL_SUMMARY_TEXT_PL, kind, state, count);
}
function _i18nToolSummaryJoinPl(parts) {
  return Array.isArray(parts) ? parts.filter(Boolean).join(', ') : '';
}
function _i18nToolSummaryJoinVi(parts) {
  return Array.isArray(parts) ? parts.filter(Boolean).join(', ') : '';
}

// Czech (cs) tool/worklog helpers — Slavic plural forms (1 / few 2-4 / many 5+ handled by _i18nToolWorklogSummaryFromMap's singular/plural split; UI uses n===1 vs plural).
function _i18nCsPlural(n, one, few, many) {
  const abs = Math.abs(Number(n) || 0);
  if (abs === 1) return one;
  if (abs >= 2 && abs <= 4) return few;
  return many;
}
const _I18N_TOOL_ACTION_TEXT_CS = {
    shell: { running: 'Spouštění', done: 'Spuštěno', fail: 'spustit', fallback: 'příkaz' },
    read: { running: 'Čtení', done: 'Přečteno', fail: 'přečíst', fallback: 'soubor' },
    list: { running: 'Výpis', done: 'Vypsáno', fail: 'vypsat', fallback: 'soubory' },
    search: { running: 'Prohledávání', done: 'Prohledáno', fail: 'prohledat', fallback: 'pracovní prostor' },
    web: { running: 'Kontrola', done: 'Zkontrolováno', fail: 'zkontrolovat', fallback: 'webová data' },
    write: { running: 'Aktualizace', done: 'Aktualizováno', fail: 'aktualizovat', fallback: 'soubor' },
    skill: { running: 'Načítání', done: 'Načteno', fail: 'načíst', fallback: 'dovednost' },
    memory: { running: 'Ukládání', done: 'Uloženo', fail: 'uložit', fallback: 'paměť' },
    delegate: { running: 'Delegování', done: 'Delegováno', fail: 'delegovat', fallback: 'úkol' },
    unknown: { running: 'Spouštění', done: 'Spuštěno', fail: 'spustit', fallback: 'nástroj' },
};
const _I18N_TOOL_SUMMARY_TEXT_CS = {
    shell: { running: ['Spouštění příkazu', 'Spouštění {n} příkazů'], done: ['Spuštěn příkaz', 'Spuštěno {n} příkazů'] },
    read: { running: ['Čtení souboru', 'Čtení {n} souborů'], done: ['Přečten soubor', 'Přečteno {n} souborů'] },
    list: { running: ['Výpis souboru', 'Výpis {n} souborů'], done: ['Vypsán soubor', 'Vypsáno {n} souborů'] },
    search: { running: ['Prohledávání prac. prostoru', 'Prohledávání prac. prostoru {n}×'], done: ['Prohledán prac. prostor', 'Prohledán prac. prostor {n}×'] },
    web: { running: ['Kontrola webu', 'Kontrola webu {n}×'], done: ['Zkontrolován web', 'Zkontrolován web {n}×'] },
    write: { running: ['Aktualizace souboru', 'Aktualizace {n} souborů'], done: ['Aktualizován soubor', 'Aktualizováno {n} souborů'] },
    skill: { running: ['Načítání dovednosti', 'Načítání {n} dovedností'], done: ['Načtena dovednost', 'Načteno {n} dovedností'] },
    memory: { running: ['Ukládání paměti', 'Ukládání {n} záznamů paměti'], done: ['Uložena paměť', 'Uloženo {n} záznamů paměti'] },
    delegate: { running: ['Delegování úkolu', 'Delegování {n} úkolů'], done: ['Delegován úkol', 'Delegováno {n} úkolů'] },
    unknown: { running: ['Spouštění nástroje', 'Spouštění {n} nástrojů'], done: ['Spuštěn nástroj', 'Spuštěno {n} nástrojů'] },
};
function _i18nProcessedElapsedCs(duration) {
  return _i18nProcessedElapsed('Zpracováno', duration);
}
function _i18nToolActionLabelCs(kind, state, target, display, failed) {
  const verbs = _I18N_TOOL_ACTION_TEXT_CS[kind] || _I18N_TOOL_ACTION_TEXT_CS.unknown;
  const object = target || verbs.fallback || display || 'nástroj';
  if (failed) return `Nepodařilo se ${verbs.fail || 'spustit'} ${object}`;
  return `${verbs[state] || verbs.running} ${object}`;
}
function _i18nToolWorklogSummaryCs(kind, state, count) {
  return _i18nToolWorklogSummaryFromMap(_I18N_TOOL_SUMMARY_TEXT_CS, kind, state, count);
}
function _i18nToolSummaryJoinCs(parts) {
  return Array.isArray(parts) ? parts.filter(Boolean).join(', ') : '';
}




// Active locale — defaults to English; overridden by loadLocale() at boot.
let _locale = LOCALES.en;

/**
 * Resolve an incoming locale tag to a known LOCALES key.
 * Supports exact keys, case-insensitive matches, and a few common aliases
 * (e.g. zh-CN -> zh, zh-TW -> zh-Hant). Returns null when unresolved.
 * @param {string} lang
 * @returns {string|null}
 */
function resolveLocale(lang) {
  if (typeof lang !== 'string') return null;
  const raw = lang.trim();
  if (!raw) return null;
  // Validate against LOCALE_META, not LOCALES: a language is a valid choice
  // even before its bundle has been fetched. Checking LOCALES would reject
  // every lazy code and make ensureLocale() unreachable.
  const known = (code) => Object.prototype.hasOwnProperty.call(LOCALE_META, code);
  if (known(raw)) return raw;

  const lower = raw.toLowerCase().replace(/_/g, '-');

  // Case-insensitive direct match first.
  const direct = Object.keys(LOCALE_META).find((k) => k.toLowerCase() === lower);
  if (direct) return direct;

  // Common Chinese variants.
  if (lower === 'zh' || lower.startsWith('zh-cn') || lower.startsWith('zh-sg') || lower.startsWith('zh-hans')) {
    return known('zh') ? 'zh' : null;
  }
  if (lower.startsWith('zh-tw') || lower.startsWith('zh-hk') || lower.startsWith('zh-mo') || lower.startsWith('zh-hant')) {
    return known('zh-Hant') ? 'zh-Hant' : null;
  }

  // Fallback to base language subtag (e.g. en-US -> en).
  const base = lower.split('-')[0];
  const baseMatch = Object.keys(LOCALE_META).find((k) => k.toLowerCase() === base);
  return baseMatch || null;
}

/**
 * Resolve locale with precedence:
 *   1) primary     (typically server setting)
 *   2) fallback    (typically localStorage)
 *   3) fallback2   (typically browser navigator hint — first-visit only)
 *   4) 'en'        (final fallback)
 *
 * Resolve a language preference to a known LOCALES key, applying a strict
 * `primary -> fallback -> fallback2 -> 'en'` precedence chain.
 *
 * #7622 (round 3): the server no longer injects `"language": "en"` as a
 * schema default — `load_settings()` reports `None` (the key is absent
 * from the response) until the user explicitly picks a locale in the
 * Settings modal.  This lets the resolver use `primary` directly
 * without a `primary === 'en'` special case:
 *   - an explicit `primary` (the server's stored value) always wins
 *   - a `None` / empty `primary` falls through to `fallback` (localStorage)
 *   - `fallback2` (the browser navigator hint) only fires when neither
 *     server nor localStorage has a preference
 *   - `'en'` is the final safety net
 *
 * Side benefit: a user who picked English on purpose (their settings
 * file contains `"language": "en"`) is no longer overridden by a
 * non-English browser locale on hydration, because the round-2
 * `primary === 'en'` skip has been removed.
 *
 * @param {string|null|undefined} primary   server-stored language
 * @param {string|null|undefined} fallback  localStorage `hermes-lang`
 * @param {string|null|undefined} fallback2 browser navigator hint
 * @returns {string}
 */
function resolvePreferredLocale(primary, fallback, fallback2) {
  return resolveLocale(primary)
    || resolveLocale(fallback)
    || resolveLocale(fallback2)
    || 'en';
}

/**
 * Detect the browser's preferred language for first-visit locale
 * selection.  Guarded against `navigator` access exceptions: some
 * embedded webviews (Tauri desktop shell, kiosk modes) report a
 * `navigator` object but throw on `.languages` / `.language` reads.
 * Callers (loadLocale, boot.js, panels.js) must use this helper rather
 * than reading `navigator` directly so a single throw does not abort
 * the boot or settings-panel hydration.
 *
 * @returns {string|null} the first entry of `navigator.languages`, or
 *   `navigator.language`, or `null` when neither is reachable.
 */
function _detectBrowserLanguageHint() {
  try {
    const nav = (typeof navigator !== 'undefined') ? navigator : null;
    if (!nav) return null;
    if (Array.isArray(nav.languages) && nav.languages.length) {
      return nav.languages[0] || null;
    }
    if (typeof nav.language === 'string' && nav.language) {
      return nav.language;
    }
    return null;
  } catch (_) {
    return null;
  }
}

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
  if (typeof val === 'function') return val(...args);
  if (args.length) {
    // Locale strings can use numbered placeholders like {0} and {1}.
    return String(val).replace(/\{(\d+)\}/g, (match, idx) => (
      Object.prototype.hasOwnProperty.call(args, idx) ? String(args[idx]) : match
    ));
  }
  return val;
}

/**
 * Switch locale by language code (e.g. 'en', 'zh').
 * Persists to localStorage and updates the <html lang> attribute.
 * @param {string} lang
 */
function setLocale(lang) {
  const resolved = resolveLocale(lang) || 'en';
  // Not fetched yet? Fall back to English instead of `undefined` so `t()` keeps
  // returning real strings; `ensureLocale()` re-applies the locale once it lands.
  _locale = LOCALES[resolved] || LOCALES.en;
  try { localStorage.setItem('hermes-lang', resolved); } catch (_) {}
  document.documentElement.lang = _locale._speech || resolved;
  // Lazy bundles: setLocale() stays synchronous so every existing caller keeps
  // working unchanged, and the locale is applied once the bundle lands.
  if (!LOCALES[resolved]) {
    ensureLocale(resolved).then((ok) => {
      if (!ok || !LOCALES[resolved]) return;
      // Lost the race to a newer setLocale()? Leave the newer choice alone.
      if (resolveLocale(localStorage.getItem('hermes-lang')) !== resolved) return;
      _locale = LOCALES[resolved];
      document.documentElement.lang = _locale._speech || resolved;
      if (typeof applyLocaleToDOM === 'function') applyLocaleToDOM();
    }).catch(() => {});
  }
}

/**
 * Load locale from localStorage (called once at boot, before DOMContentLoaded).
 * Server-persisted preference is applied later in loadSettingsPanel().
 *
 * #7622: on the FIRST visit (no stored preference), fall back to the
 * browser's preferred language (`navigator.languages[0]` /
 * `navigator.language`) so non-English speakers don't have to dig into
 * Settings after every fresh install.  This is strictly a first-visit
 * hint — the moment the user picks a language in Settings (or the
 * server-side preference is applied via `loadSettingsPanel`), the
 * stored value takes over and the browser hint is never consulted
 * again for that browser profile.
 */
function loadLocale() {
  let stored = null;
  try { stored = localStorage.getItem('hermes-lang'); } catch (_) {}
  // Only consult the browser hint when no stored preference exists.
  // The settings panel and `loadSettingsPanel()` will overwrite this
  // hint on their first call, so explicit user / server preferences
  // always win.  #7622 (round 3): the browser-hint read is delegated
  // to `_detectBrowserLanguageHint()` so a `navigator` access throw
  // (some embedded webviews) can no longer abort loadLocale().
  setLocale(resolvePreferredLocale(stored, _detectBrowserLanguageHint()));
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
  document.querySelectorAll('[data-i18n-title]').forEach(el => {
    const key = el.getAttribute('data-i18n-title');
    const val = t(key);
    if (!val || val === key) return;
    if (el.hasAttribute('data-tooltip')) {
      // Custom CSS tooltip is in use (#1775) — sync it and explicitly clear
      // the native `title` attribute so the slow ~1.5s browser tooltip never
      // co-fires alongside the fast custom tooltip.
      el.setAttribute('data-tooltip', val);
      if (el.hasAttribute('title')) el.removeAttribute('title');
    } else {
      // Element opted out of custom tooltips — fall back to the native title.
      el.title = val;
    }
  });
  document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
    const key = el.getAttribute('data-i18n-placeholder');
    const val = t(key);
    if (val && val !== key) el.placeholder = val;
  });
  document.querySelectorAll('[data-i18n-aria-label]').forEach(el => {
    const key = el.getAttribute('data-i18n-aria-label');
    const val = t(key);
    if (val && val !== key) el.setAttribute('aria-label', val);
  });
  // Dynamic text that also carries a data-i18n fallback key (e.g. the chat-todos
  // summary #chatTodosSummary with data-i18n="tab_todos") is clobbered by the
  // re-stamp above: opening Settings — or any locale change — reset the live
  // "1 active · 1 total" summary to the static "Todos" label. Repaint the live
  // values after the restamp (guarded: i18n.js loads standalone in some tests).
  if (typeof renderChatTodos === 'function') renderChatTodos();
  if (typeof syncWorkspacePanelUI === 'function') syncWorkspacePanelUI();
  if (typeof syncAppTitlebar === 'function') syncAppTitlebar();
  // #8517: the memory-off notice copy is set from code, not data-i18n.
  if (typeof syncMemoryOffNotice === 'function') syncMemoryOffNotice();
}

// Apply saved locale immediately so there's no flash of English on reload.
loadLocale();
