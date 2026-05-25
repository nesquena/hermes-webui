let _skillRegistryReady = false;
let _skillRegistryPromise = null;
let _skillRegistry = new Map();

async function loadSkillRegistry() {
  if(_skillRegistryReady) return _skillRegistry;
  if(_skillRegistryPromise) return _skillRegistryPromise;

  _skillRegistryPromise = (async () => {
    try {
      const data = await api('/api/skills');
      const next = new Map();
      for(const skill of (data && data.skills) || []){
        if(skill && skill.disabled) continue;
        const name = String(skill && skill.name || '').trim();
        if(!name) continue;
        if(!next.has(name)){
          next.set(name, { name });
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

function getSkillByMentionToken(token) {
  const raw = String(token || '').trim().replace(/^\//, '');
  return _skillRegistry.get(raw) || null;
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
const SKILL_MENTION_TOKEN_RE = /(^|\s)(\/?([A-Za-z0-9][A-Za-z0-9_-]*))(?=$|\s)/g;

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
    const skillName = m[3] || '';
    const skill = getSkillByMentionToken(skillName);
    if(!skill) continue;

    const tokenEnd = tokenStart + matchedText.length;
    const codeParent = nearestInlineSkillMentionCodeParent(node);
    const isSlashMention = matchedText.startsWith('/');
    if(!isSlashMention && !codeParent) continue;

    parts.push(document.createTextNode(text.slice(last, tokenStart)));
    const chip = createSkillChip(skill);
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

window.highlightSkillsInMessages = highlightSkillsInMessages;
