/**
 * Fox in the Box — Setup wizard
 *
 * Drives a 3-step onboarding flow:
 *   1. OpenRouter API key validation + save
 *   2. Tailscale connect (optional)
 *   3. Complete → redirect to /
 */
(function () {
  'use strict';

  // ── DOM refs ────────────────────────────────────────────────────────────────

  const steps     = document.querySelectorAll('.steps-nav .step');
  const cards     = {
    1: document.getElementById('step-1'),
    2: document.getElementById('step-2'),
    3: document.getElementById('step-3'),
  };

  // Step 1
  const apiKeyInput    = document.getElementById('api-key-input');
  const btnSaveKey     = document.getElementById('btn-save-key');
  const keyError       = document.getElementById('key-error');

  // Step 2
  const btnTsConnect   = document.getElementById('btn-ts-connect');
  const btnTsSkip      = document.getElementById('btn-ts-skip');
  const tsAuthBox      = document.getElementById('ts-auth-box');
  const tsAuthUrl      = document.getElementById('ts-auth-url');
  const tsStatusEl     = document.getElementById('ts-status');
  const tsStatusText   = document.getElementById('ts-status-text');
  const tsSpinner      = document.getElementById('ts-spinner');
  const tsError        = document.getElementById('ts-error');

  // Step 3
  const doneDesc       = document.getElementById('done-desc');
  const btnOpenApp     = document.getElementById('btn-open-app');

  // ── State ───────────────────────────────────────────────────────────────────

  let tsConnected = false;
  let tsPollTimer = null;

  // ── Helpers ─────────────────────────────────────────────────────────────────

  function showError(el, msg) {
    el.textContent = msg;
    el.classList.remove('hidden');
  }

  function hideError(el) {
    el.classList.add('hidden');
    el.textContent = '';
  }

  function setStep(n) {
    // Update step nav
    steps.forEach((el, i) => {
      const stepN = i * 2 + 1; // indices 0,2,4 → steps 1,2,3 (there are dividers)
      // steps-nav contains .step and .step-divider; only .step elements matter
    });
    document.querySelectorAll('.steps-nav .step').forEach((el) => {
      const s = parseInt(el.dataset.step, 10);
      el.classList.remove('active', 'done');
      if (s < n)  el.classList.add('done');
      if (s === n) el.classList.add('active');
    });

    // Show/hide cards
    Object.entries(cards).forEach(([k, card]) => {
      if (parseInt(k, 10) === n) {
        card.classList.remove('hidden');
      } else {
        card.classList.add('hidden');
      }
    });
  }

  async function postJSON(path, body) {
    const res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    return { status: res.status, data };
  }

  // ── Step 1: Save API key ────────────────────────────────────────────────────

  btnSaveKey.addEventListener('click', async () => {
    const key = apiKeyInput.value.trim();
    hideError(keyError);

    if (!key) {
      showError(keyError, 'Please enter your OpenRouter API key.');
      return;
    }
    if (!key.startsWith('sk-')) {
      showError(keyError, 'API key must start with "sk-". Check your key and try again.');
      return;
    }

    btnSaveKey.disabled = true;
    btnSaveKey.textContent = 'Saving…';

    try {
      const { status, data } = await postJSON('/api/setup/openrouter', { key });
      if (status === 200 && data.ok) {
        setStep(2);
      } else {
        showError(keyError, data.error || 'Something went wrong. Please try again.');
      }
    } catch (e) {
      showError(keyError, 'Network error — is the server running?');
    } finally {
      btnSaveKey.disabled = false;
      btnSaveKey.textContent = 'Save & Continue';
    }
  });

  // Allow Enter to submit the key
  apiKeyInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') btnSaveKey.click();
  });

  // ── Step 2: Tailscale ───────────────────────────────────────────────────────

  btnTsConnect.addEventListener('click', async () => {
    btnTsConnect.disabled = true;
    btnTsSkip.disabled    = true;
    hideError(tsError);

    try {
      const { status, data } = await postJSON('/api/setup/tailscale/start', {});
      if (status !== 200 || !data.ok) {
        showError(tsError, data.error || 'Failed to start Tailscale.');
        btnTsConnect.disabled = false;
        btnTsSkip.disabled    = false;
        return;
      }
    } catch (e) {
      showError(tsError, 'Network error — could not reach setup API.');
      btnTsConnect.disabled = false;
      btnTsSkip.disabled    = false;
      return;
    }

    tsStatusEl.classList.remove('hidden');
    tsStatusText.textContent = 'Waiting for Tailscale…';
    startTsPoll();
  });

  btnTsSkip.addEventListener('click', () => {
    stopTsPoll();
    complete(false);
  });

  function startTsPoll() {
    stopTsPoll();
    tsPollTimer = setInterval(pollTsStatus, 1500);
    pollTsStatus(); // immediate first hit
  }

  function stopTsPoll() {
    if (tsPollTimer) { clearInterval(tsPollTimer); tsPollTimer = null; }
  }

  async function pollTsStatus() {
    let data;
    try {
      const res = await fetch('/api/setup/tailscale/status');
      data = await res.json();
    } catch (e) {
      return; // transient error — keep polling
    }

    const status = data.status || 'waiting';

    if (status === 'url_ready' && data.login_url) {
      tsAuthUrl.href = data.login_url;
      tsAuthUrl.textContent = data.login_url;
      tsAuthBox.classList.remove('hidden');
      tsStatusText.textContent = 'Waiting for you to authorise in your browser…';
    }

    if (status === 'connected') {
      stopTsPoll();
      tsSpinner.style.display = 'none';
      tsStatusText.textContent = '✓ Tailscale connected!';
      tsStatusText.style.color = 'var(--success)';
      tsConnected = true;
      // Brief pause so user sees the success state, then move on
      setTimeout(() => complete(true, data.tailnet_url), 1200);
    }

    if (status === 'error') {
      stopTsPoll();
      showError(tsError, data.error || 'Tailscale login failed.');
      tsStatusEl.classList.add('hidden');
      btnTsConnect.disabled = false;
      btnTsSkip.disabled    = false;
    }
  }

  // ── Step 3: Complete ────────────────────────────────────────────────────────

  async function complete(tailscaleOk, tailnetUrl) {
    try {
      await postJSON('/api/setup/complete', { tailscale_connected: tailscaleOk });
    } catch (e) {
      // Non-fatal: the onboarding file couldn't be written — log and continue
      console.error('[setup] complete call failed', e);
    }

    if (tailscaleOk && tailnetUrl) {
      doneDesc.innerHTML =
        `Your Tailscale URL: <strong><a href="${tailnetUrl}" target="_blank" rel="noopener">${tailnetUrl}</a></strong><br>` +
        'Fox is running and reachable from anywhere on your Tailnet.';
    } else if (tailscaleOk) {
      doneDesc.textContent =
        'Fox is running. You can connect Tailscale later from Settings if you need remote access.';
    } else {
      doneDesc.textContent =
        'Fox is running. You can connect Tailscale later from Settings if you need remote access.';
    }

    setStep(3);
  }

  btnOpenApp.addEventListener('click', () => {
    window.location.href = '/';
  });

  // ── Init ────────────────────────────────────────────────────────────────────

  setStep(1);

})();
