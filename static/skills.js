let _skillRegistryReady = false;
let _skillRegistryPromise = null;
let _skillRegistry = new Map();

function normalizeSkillSlug(value) {
  const raw = String(value || '').trim().toLowerCase().replace(/^\//, '');
  if(!raw) return '';
  return raw
    .replace(/[\s_]+/g, '-')
    .replace(/[^a-z0-9-]/g, '')
    .replace(/-{2,}/g, '-')
    .replace(/^-+|-+$/g, '');
}

async function loadSkillRegistry(force=false) {
  if(_skillRegistryReady && !force) return _skillRegistry;
  if(_skillRegistryPromise && !force) return _skillRegistryPromise;

  _skillRegistryPromise = (async () => {
    try {
      const data = await api('/api/skills');
      const next = new Map();
      for(const skill of (data && data.skills) || []){
        if(skill && skill.disabled) continue;
        const name = String(skill && skill.name || '').trim();
        const slug = normalizeSkillSlug(name);
        if(!slug) continue;
        if(!next.has(slug)){
          next.set(slug, {
            name,
            slug,
            description: String(skill && skill.description || '').trim(),
            category: String(skill && skill.category || '').trim(),
          });
        }
      }
      _skillRegistry = next;
    } catch(_) {
      _skillRegistry = new Map();
    } finally {
      _skillRegistryReady = true;
      _skillRegistryPromise = null;
    }
    return _skillRegistry;
  })();

  return _skillRegistryPromise;
}

function getSkillBySlug(slug) {
  return _skillRegistry.get(normalizeSkillSlug(slug)) || null;
}

function getSkillByMentionToken(token) {
  return getSkillBySlug(token);
}

function getRegisteredSkills() {
  return Array.from(_skillRegistry.values()).sort((a,b)=>a.name.localeCompare(b.name));
}

function getSkillAutocompleteEntries() {
  return Array.from(_skillRegistry.values())
    .sort((a,b)=>a.slug.localeCompare(b.slug))
    .map(skill => ({
      name: skill.slug,
      desc: skill.description || (typeof t === 'function' ? t('slash_skill_desc') : 'Skill'),
      source: 'skill',
      skillName: skill.name,
    }));
}

function createSkillChip(skill) {
  const chip = document.createElement('span');
  chip.className = 'skill-chip';
  chip.textContent = skill.name;
  chip.title = `Skill: ${skill.name}`;
  chip.setAttribute('aria-label', `Skill ${skill.name}`);
  return chip;
}

const SKILL_MENTION_SKIP_TAGS = new Set(['PRE', 'A', 'SCRIPT', 'STYLE', 'TEXTAREA', 'INPUT', 'BUTTON']);
const SKILL_MENTION_TOKEN_RE = /(^|[^A-Za-z0-9_-])(`\/?([A-Za-z0-9][A-Za-z0-9_-]*)`|\/?([A-Za-z0-9][A-Za-z0-9_-]*))(?=$|[^A-Za-z0-9_-])/g;

function shouldSkipSkillMentionNode(node) {
  for(let parent=node.parentNode; parent&&parent.nodeType===1; parent=parent.parentNode){
    const tag = parent.tagName;
    if(!tag) continue;
    if(SKILL_MENTION_SKIP_TAGS.has(tag)) return true;
    if(parent.classList&&parent.classList.contains('skill-chip')) return true;
  }
  return false;
}

function nearestInlineSkillMentionCodeParent(node) {
  for(let parent=node&&node.parentNode; parent&&parent.nodeType===1; parent=parent.parentNode){
    if(parent.tagName==='PRE') return null;
    if(parent.tagName==='CODE') return parent;
  }
  return null;
}

function highlightSkillMentionsInTextNode(node) {
  if(!node||!node.nodeValue) return false;
  const text = node.nodeValue;

  SKILL_MENTION_TOKEN_RE.lastIndex = 0;
  const parts = [];
  let last = 0;
  let matched = false;

  for(let m=SKILL_MENTION_TOKEN_RE.exec(text); m; m=SKILL_MENTION_TOKEN_RE.exec(text)){
    const prefix = m[1] || '';
    const tokenStart = m.index + prefix.length;
    const matchedText = m[2] || '';
    const skillName = m[3] || m[4] || '';
    const skill = getSkillByMentionToken(skillName);
    if(!skill) continue;

    const tokenEnd = tokenStart + matchedText.length;
    parts.push(document.createTextNode(text.slice(last, tokenStart)));
    const chip = createSkillChip(skill);
    const codeParent = nearestInlineSkillMentionCodeParent(node);
    if(codeParent && codeParent.textContent.trim()===matchedText && text.trim()===matchedText){
      codeParent.parentNode.replaceChild(chip, codeParent);
      return true;
    }
    parts.push(chip);
    last = tokenEnd;
    matched = true;
  }

  if(!matched) return false;

  parts.push(document.createTextNode(text.slice(last)));
  const frag = document.createDocumentFragment();
  for(const part of parts){ frag.appendChild(part); }
  node.parentNode.replaceChild(frag, node);
  return true;
}

function highlightSkillsInRenderedMessages(container) {
  if(!container||!_skillRegistry.size) return;
  const bodies = container.classList && container.classList.contains('msg-body')
    ? [container]
    : Array.from(container.querySelectorAll('.msg-body'));
  bodies.forEach(body => {
    const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
    const textNodes = [];
    let current = null;
    while((current = walker.nextNode())){
      if(!shouldSkipSkillMentionNode(current)) textNodes.push(current);
    }
    for(const node of textNodes) highlightSkillMentionsInTextNode(node);
  });
}

function highlightSkillsInMessages(container) {
  const root = container || (typeof $ === 'function' && $('msgInner')) || document.getElementById('msgInner');
  if(!root) return;
  loadSkillRegistry().then(() => highlightSkillsInRenderedMessages(root));
}

const COMPOSER_SKILL_TOKEN_RE = /(^|\s)\/?([A-Za-z0-9][A-Za-z0-9_-]*)(?=\s)/g;

function findCompletedComposerSkillMentions(text) {
  const mentions = [];
  COMPOSER_SKILL_TOKEN_RE.lastIndex = 0;
  for(let m=COMPOSER_SKILL_TOKEN_RE.exec(String(text || '')); m; m=COMPOSER_SKILL_TOKEN_RE.exec(String(text || ''))){
    const token = m[2] || '';
    const skill = getSkillByMentionToken(token);
    if(skill) mentions.push({token, skill, start: m.index + (m[1] || '').length, end: COMPOSER_SKILL_TOKEN_RE.lastIndex});
  }
  return mentions;
}

function appendOverlayText(fragment, text) {
  if(!text) return;
  const span = document.createElement('span');
  span.className = 'composer-overlay-text';
  span.textContent = text;
  fragment.appendChild(span);
}

function renderComposerSkillOverlay(text, mentions) {
  const fragment = document.createDocumentFragment();
  let last = 0;
  for(const mention of mentions){
    appendOverlayText(fragment, text.slice(last, mention.start));
    fragment.appendChild(createSkillChip(mention.skill));
    last = mention.end;
  }
  appendOverlayText(fragment, text.slice(last));
  // Preserve an empty visible line box so the transparent textarea and overlay align.
  if(!text) appendOverlayText(fragment, ' ');
  return fragment;
}

async function updateComposerSkillPreview(opts={}) {
  const overlay = (typeof $ === 'function' && $('composerSkillOverlay')) || document.getElementById('composerSkillOverlay');
  const textarea = (typeof $ === 'function' && $('msg')) || document.getElementById('msg');
  if(!overlay || !textarea) return;
  await loadSkillRegistry();
  const text = String(textarea.value || '');
  let mentions = findCompletedComposerSkillMentions(text);
  if(opts && opts.force && !mentions.length){
    const forcedToken = text.trim().replace(/^\//, '');
    const forcedSkill = getSkillByMentionToken(forcedToken);
    if(forcedSkill) mentions = [{token: forcedToken, skill: forcedSkill, start: text.indexOf(forcedToken), end: text.indexOf(forcedToken) + forcedToken.length}];
  }
  overlay.innerHTML = '';
  overlay.appendChild(renderComposerSkillOverlay(text, mentions));
  overlay.scrollTop = textarea.scrollTop;
  overlay.scrollLeft = textarea.scrollLeft;
}

function initComposerSkillOverlayScrollSync() {
  const overlay = (typeof $ === 'function' && $('composerSkillOverlay')) || document.getElementById('composerSkillOverlay');
  const textarea = (typeof $ === 'function' && $('msg')) || document.getElementById('msg');
  if(!overlay || !textarea || textarea._composerSkillOverlayScrollSync) return;
  textarea._composerSkillOverlayScrollSync = true;
  textarea.addEventListener('scroll', () => {
    overlay.scrollTop = textarea.scrollTop;
    overlay.scrollLeft = textarea.scrollLeft;
  }, {passive:true});
}

if(document.readyState==='loading') document.addEventListener('DOMContentLoaded', initComposerSkillOverlayScrollSync);
else initComposerSkillOverlayScrollSync();

window.normalizeSkillSlug = normalizeSkillSlug;
window.loadSkillRegistry = loadSkillRegistry;
window.getSkillBySlug = getSkillBySlug;
window.getSkillByMentionToken = getSkillByMentionToken;
window.getRegisteredSkills = getRegisteredSkills;
window.getSkillAutocompleteEntries = getSkillAutocompleteEntries;
window.highlightSkillsInMessages = highlightSkillsInMessages;
window.findCompletedComposerSkillMentions = findCompletedComposerSkillMentions;
window.updateComposerSkillPreview = updateComposerSkillPreview;
