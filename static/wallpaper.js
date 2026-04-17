// ── Wallpaper ──────────────────────────────────────────────────────────────
// Applies a custom user-uploaded background image and brightness adjustment.
// Storage is server-side at ~/.hermes/webui/wallpaper-<hash>.{ext}; brightness
// is a CSS variable on :root, persisted in settings.json.
//
// Public API:
//   applyWallpaper()           — boot + post-upload: read /api/wallpaper/info
//                                and update DOM (wallpaper div + brightness var)
//   uploadWallpaper(file)      — POST raw bytes to /api/wallpaper
//   removeWallpaper()          — POST /api/wallpaper/delete + clear DOM
//   setBrightness(percent)     — update --wallpaper-brightness CSS var
//                                (does NOT persist; persistence is via Save in panel)

const _WALLPAPER_ALLOWED_TYPES = new Set([
  'image/jpeg', 'image/png', 'image/webp',
]);
const _WALLPAPER_MAX_BYTES = 10_000_000;

async function applyWallpaper(){
  let info;
  try{
    info = await api('/api/wallpaper/info');
  }catch(e){
    // Silent failure on boot -- user just sees theme color (fallback).
    console.warn('applyWallpaper failed', e);
    return;
  }
  const root = document.documentElement;
  const div = document.getElementById('wallpaper');
  if(!div) return;

  // Brightness applies regardless of whether a wallpaper is set (no-op if
  // no wallpaper, but the CSS var should still be in sync with settings).
  if(typeof info.brightness === 'number'){
    root.style.setProperty('--wallpaper-brightness', String(info.brightness));
  }

  if(info.has_wallpaper && info.file){
    // ?v=<hash> for cache busting -- file name itself includes the hash but
    // browsers may key cache by URL, so the query string is belt-and-suspenders.
    const hash = info.file.replace(/^wallpaper-/, '').replace(/\.[a-z]+$/, '');
    div.style.backgroundImage = `url("/api/wallpaper?v=${encodeURIComponent(hash)}")`;
    div.setAttribute('data-active', '');
  }else{
    div.style.backgroundImage = '';
    div.removeAttribute('data-active');
  }
}

async function uploadWallpaper(file){
  if(!file) return;
  // Local pre-check: type + size
  if(!_WALLPAPER_ALLOWED_TYPES.has(file.type)){
    showToast(t('wallpaper_invalid_format'));
    return;
  }
  if(file.size > _WALLPAPER_MAX_BYTES){
    showCenterAlert(t('wallpaper_size_too_large'));
    return;
  }
  try{
    const buf = await file.arrayBuffer();
    const res = await fetch('/api/wallpaper', {
      method: 'POST',
      credentials: 'include',
      headers: {'Content-Type': file.type},
      body: buf,
    });
    if(!res.ok){
      let msg;
      try{ msg = (await res.json()).error; }catch(_){ msg = await res.text(); }
      // Backend rejects oversize with HTTP 413; surface in the prominent
      // center alert so users notice immediately, not a corner toast.
      if(res.status === 413){
        showCenterAlert(msg || t('wallpaper_size_too_large'));
      }else{
        showToast(msg || ('Upload failed: ' + res.status));
      }
      return;
    }
    await applyWallpaper();
    showToast(t('wallpaper_uploaded'));
  }catch(e){
    showToast(t('failed_colon') + e.message);
  }
}

async function removeWallpaper(){
  try{
    await api('/api/wallpaper/delete', {method:'POST', body:JSON.stringify({})});
    await applyWallpaper();  // refreshes div to inactive state
    showToast(t('wallpaper_removed'));
  }catch(e){
    showToast(t('failed_colon') + e.message);
  }
}

function setBrightness(percent){
  // Slider is 10..150; CSS variable expects 0.1..1.5.
  const decimal = Math.max(0.1, Math.min(1.5, percent / 100));
  document.documentElement.style.setProperty('--wallpaper-brightness', String(decimal));
}

// ── Center alert ───────────────────────────────────────────────────────────
// Prominent center-of-screen modal for important warnings (e.g. file too
// large) where a corner toast is too easy to miss. Auto-dismisses after
// 4s and on click/Escape. Uses inline styles so it works without CSS edits
// and doesn't depend on theme variables (always visible regardless of bg).
function showCenterAlert(msg){
  // Remove any existing alert before showing a new one
  const existing = document.getElementById('wallpaperCenterAlert');
  if(existing) existing.remove();

  const overlay = document.createElement('div');
  overlay.id = 'wallpaperCenterAlert';
  overlay.style.cssText = [
    'position:fixed', 'inset:0', 'z-index:99999',
    'display:flex', 'align-items:center', 'justify-content:center',
    'background:rgba(0,0,0,0.45)', 'backdrop-filter:blur(2px)',
    'animation:fadeIn 0.15s ease-out',
  ].join(';');

  const card = document.createElement('div');
  card.style.cssText = [
    'max-width:420px', 'min-width:280px',
    'padding:24px 28px', 'border-radius:12px',
    'background:#2a1f2e', 'color:#fff',
    'border:1px solid rgba(233,69,96,0.55)',
    'box-shadow:0 10px 40px rgba(0,0,0,0.5)',
    'font-size:15px', 'line-height:1.5',
    'text-align:center',
  ].join(';');

  const icon = document.createElement('div');
  icon.textContent = '⚠️';
  icon.style.cssText = 'font-size:36px;margin-bottom:8px;';
  card.appendChild(icon);

  const text = document.createElement('div');
  text.textContent = msg;
  text.style.cssText = 'margin-bottom:14px;color:#f0e0e0;';
  card.appendChild(text);

  const btn = document.createElement('button');
  btn.textContent = 'OK';
  btn.style.cssText = [
    'padding:7px 20px', 'border-radius:6px', 'border:none',
    'background:rgba(233,69,96,0.85)', 'color:#fff',
    'cursor:pointer', 'font-size:14px', 'font-weight:600',
  ].join(';');
  card.appendChild(btn);

  overlay.appendChild(card);

  function dismiss(){
    overlay.remove();
    document.removeEventListener('keydown', onKey);
  }
  function onKey(e){
    if(e.key === 'Escape' || e.key === 'Enter') dismiss();
  }
  overlay.onclick = (e) => { if(e.target === overlay) dismiss(); };
  btn.onclick = dismiss;
  document.addEventListener('keydown', onKey);

  document.body.appendChild(overlay);
  // Auto-dismiss after 4s in case user walks away
  setTimeout(() => { if(document.body.contains(overlay)) dismiss(); }, 4000);
}
