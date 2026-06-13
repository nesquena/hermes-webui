from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text()


def test_topbar_uses_session_total_for_lazy_loaded_transcripts():
    assert "function _topbarMessageMetaText()" in UI_JS
    assert "const isTruncated=!!(typeof _messagesTruncated!=='undefined'&&_messagesTruncated);" in UI_JS
    # Truncated transcripts surface the server total as "loaded of total".
    assert "return `${loadedCount} loaded of ${totalCount} messages`;" in UI_JS
    # Fully-loaded transcripts use the tool-row-filtered loadedCount, NOT the
    # raw server total (which counts role:\"tool\" rows the topbar excludes).
    assert "return t('n_messages',loadedCount);" in UI_JS


def test_load_earlier_indicator_names_server_side_older_count():
    assert "const serverOlderCount=hasServerOlder&&Number.isFinite(Number(_oldestIdx))?Math.max(0,Number(_oldestIdx)):0;" in UI_JS
    assert "Load earlier messages (${serverOlderCount} older)" in UI_JS


def test_sync_topbar_does_not_count_only_loaded_tail_messages():
    block = UI_JS[UI_JS.index("function syncTopbar(){") : UI_JS.index("function msgContent", UI_JS.index("function syncTopbar(){"))]
    assert "const metaText=_topbarMessageMetaText();" in block
    assert "t('n_messages',vis.length)" not in block
    assert "S.messages.filter(m=>m&&m.role&&m.role!=='tool')" not in block
    assert "document.title=sessionTitle+' \\u2014 '+assistantDisplayName();" in block
    assert "document.title='● '+document.title;" in block
    assert "const pendingPrefix=(typeof activeSessionHasPendingPromptAttention==='function'&&activeSessionHasPendingPromptAttention())?'● ':'';" not in block

    sessions_js = (ROOT / "static" / "sessions.js").read_text()
    fn = sessions_js[
        sessions_js.index("async function _ensureMessagesLoaded") :
        sessions_js.index("function _messageComparableText", sessions_js.index("async function _ensureMessagesLoaded"))
    ]
    assert "_messagesTruncated = !!data.session._messages_truncated;" in fn
    assert "S.session.message_count=Number(data.session.message_count || msgs.length);" in fn
    after_count_update = fn[fn.index("S.session.message_count=Number(data.session.message_count || msgs.length);") :]
    assert "syncTopbar();" in after_count_update
