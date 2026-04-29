// static/js/vendo-chip.js
// Renders a small chip in the sidebar bottom showing the signed-in Vendo
// user. Hidden when not behind Vendo SSO. Spec:
// docs/superpowers/specs/2026-04-28-hermes-vendo-sso-design.md

const PALETTE = [
  '#2563eb', // blue
  '#9333ea', // purple
  '#db2777', // pink
  '#dc2626', // red
  '#ea580c', // orange
  '#16a34a', // green
  '#0891b2', // cyan
  '#475569', // slate
];

function pickColor(userId) {
  // Take the first 8 hex chars of the user_id (or its hash if non-hex)
  // and mod by palette length. Deterministic per user.
  const hex = (userId || '').replace(/[^0-9a-f]/gi, '').slice(0, 8);
  const n = parseInt(hex || '0', 16);
  return PALETTE[(isNaN(n) ? 0 : n) % PALETTE.length];
}

function initials(name) {
  const parts = (name || '').trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return '?';
  if (parts.length === 1) return parts[0][0].toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

async function loadVendoChip() {
  let res;
  try {
    res = await fetch('/api/vendo/identity', {
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json' },
    });
  } catch (_e) {
    return;  // network error — leave chip hidden
  }
  if (!res.ok) return;  // 404 means SSO off; nothing to show
  const id = await res.json();
  if (!id || !id.user_id) return;

  const root = document.getElementById('vendo-identity-chip');
  if (!root) return;
  const avatar = root.querySelector('.vendo-identity-chip__avatar');
  const nameEl = root.querySelector('.vendo-identity-chip__name');

  avatar.style.backgroundColor = pickColor(id.user_id);
  avatar.textContent = initials(id.name);
  nameEl.textContent = id.name;

  root.addEventListener('click', () => {
    window.open(id.dashboard_url, '_blank', 'noopener');
  });

  // Right-click / long-press for "Sign out of Vendo".
  root.addEventListener('contextmenu', (ev) => {
    ev.preventDefault();
    if (confirm('Sign out of Vendo? This signs you out of every Vendo deployment.')) {
      window.location.href = id.logout_url;
    }
  });

  root.removeAttribute('hidden');
}

// Run after DOM ready.
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', loadVendoChip);
} else {
  loadVendoChip();
}
