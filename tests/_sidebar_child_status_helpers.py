"""Shared real attach/row-render component for Node and isolated browser checks."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def component_script(source=None):
    source = source or (ROOT / "static/sessions.js").read_text(encoding="utf-8")
    names = [
        "_isChildSession", "_isForkWithResolvableParent", "_sessionLineageKey",
        "_sidebarLineageKeyForRow", "_sessionLineageContainsSession",
        "_sessionTimestampMs", "_sessionDisplayTitle", "_sessionTitleTags", "_formatSessionModelWithGateway",
        "_sessionSegmentCount", "_lineageReportCacheKey", "_lineageLocalSegmentCount",
        "_lineageReportNeedsFetch", "_lineageSegmentsForRender", "_sessionLineageBadgeTooltip",
        "_attachChildSessionsToSidebarRows", "_sessionAttentionState",
        "_isSessionEffectivelyStreaming", "_hasPendingUserMessageSignal",
        "_sessionStateTooltip", "_sessionChildBadgeTooltip", "_hasUnreadForSession",
    ]
    # Extract the navigation seams together: listeners consume modified gestures
    # through the real profile guard and URL builder, not a no-op adapter.
    for name in ["_wireSessionNewTabListeners", "_consumeSessionNewTabClick",
                 "_openSessionUrlInNewTab", "_newTabOpenSupported",
                 "_newTabOwningProfileAllowed", "_markSessionUrlExact",
                 "_sessionUrlForSid", "_sidebarSessionProfileName",
                 "_profileMatchesActiveProfile"]:
        if "function " + name + "(" in source:
            names.append(name)
    # New helper is optional so the same harness exercises the exact prior head.
    if "function _sessionRowHasLineageSummary(" in source:
        names.append("_sessionRowHasLineageSummary")
    if "function _createChildSessionStateIndicator(" in source:
        names.append("_createChildSessionStateIndicator")
    functions = []
    for name in names:
        match = re.search(r"^function " + name + r"\(.*?^\}", source, re.M | re.S)
        assert match, name
        functions.append(match.group())
    start = source.index("  function _renderOneSession(")
    end = source.index("    return el;\n  }", start) + len("    return el;\n  }")
    functions.append(source[start:end])
    return r"""
let activeSidForSidebar = 'other';
const S={session:null,busy:false};
const _showArchived=false, _sessionSelectMode=false, _showAllProfiles=false;
const _allSessionsScope={allProfiles:false,profile:'default'};
const _expandedChildSessionKeys=new Set(), _sessionSwipeReturnOffsets=new Map();
const _allProjects=[], _lineageReportInflight=new Map(), _lineageReportCache=new Map();
const _expandedLineageKeys=new Set();
const animateRefresh=false, searchQueryRaw='';
const SESSION_LONG_PRESS_DELAY_MS=400;
const SESSION_ARCHIVE_SWIPE_THRESHOLD_PX=128, SESSION_DELETE_SWIPE_THRESHOLD_PX=128, SESSION_SWIPE_CANCEL_RATIO=0.75;
let _renamingSid=null, _sessionActionMenu=null;
const ICONS={more:'...',pin:'P'};
const opened=[];
function t(key, value){
  if(key==='session_meta_children') return value+' children';
  if(key==='session_child_toggle_hint') return value+' (click to show or hide)';
  if(key==='session_child_archived') return 'Child sessions (archived)';
  if(key==='session_child_archived_short') return 'Archived child';
  if(key==='session_child_attention') return 'Child session: '+value;
  if(key==='session_child_open') return 'Open child session';
  if(key==='session_child_running') return 'Child session is running';
  if(key==='session_child_unread') return 'Unread child completion';
  if(key==='session_attention_approval_title') return 'Waiting for permission decision';
  if(key==='session_attention_clarify_title') return 'Waiting for your answer';
  if(key==='session_attention_generic_title') return 'Waiting for user action';
  return key;
}
function _isSessionLocallyStreaming(s){return !!(S.busy&&S.session&&S.session.session_id===s.session_id);}
let fixtureSessions=[];
function _hasSessionCompletionUnread(sid){return fixtureSessions.some(s=>s.session_id===sid&&s.has_unread);}
function _getSessionViewedCounts(){return Object.fromEntries(fixtureSessions.map(s=>[s.session_id,{message_count:s.message_count,transcript_generation:0}]));}
function _sessionTranscriptGenerationForUnread(){return 0;}
function _sessionViewedCountRecord(record){return record;}
function _setSessionViewedCount(){throw new Error('Fixture must seed viewed counts');}
function _isReadOnlySession(s){return !!s.read_only;}
function _isMessagingSession(){return false;}
function _rememberRenderedStreamingState(){}
function _rememberRenderedSessionSnapshot(){}
function _sessionTitleIsDefaultWebUI(){return false;}
function _sessionFullTitleTooltip(raw){return raw;}
function _formatRelativeSessionTime(){return '1m';}
function _sessionSearchContentPreview(){return '';}
function _nestedChildTitle(s){return s.title;}
function _buildSessionRenameStarter(){return ()=>{};}
function _makeSessionSwipeAffordance(direction){
  const el=document.createElement('span');
  el.className='session-swipe-affordance session-swipe-affordance-'+direction;
  return el;
}
async function _openSidebarSession(s, options){opened.push({sid:s.session_id,options});}
function _getChannelLabel(){return '';}
function renderSessionListFromCache(){if(typeof repaint==='function')repaint();}
""" + "\n".join(functions) + r"""
function renderFixture(raw, references, expanded=false, active='other'){
  activeSidForSidebar=active;
  fixtureSessions=references||raw;
  S.session=raw.find(s=>s.session_id===active)||null;
  _expandedChildSessionKeys.clear();
  if(expanded) _expandedChildSessionKeys.add('parent');
  const rows=_attachChildSessionsToSidebarRows([raw[0]],raw,references);
  return {row:rows[0],element:_renderOneSession(rows[0])};
}
"""


FAKE_DOM = r"""
const window={_sidebarDensity:'compact'};
class Element {
  constructor(tag){this.tag=tag;this.children=[];this.dataset={};this.attributes={};this.style={setProperty(){},removeProperty(){}};}
  setAttribute(k,v){this.attributes[k]=v;}
  appendChild(e){this.children.push(e);return e;}
  append(...els){this.children.push(...els);}
  addEventListener(){}
  get classList(){return {add:()=>{},remove:()=>{},toggle:()=>{}};}
}
const document={createElement:tag=>new Element(tag)};
function flatten(el){return [el,...el.children.flatMap(flatten)];}
"""
