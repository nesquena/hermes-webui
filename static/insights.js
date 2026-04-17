// hermes-webui -- Insights panel (section 6 of add-dashboards-and-pixel-office).
//
// Renders four subviews into #mainInsights:
//   1. Summary cards (fed by /api/stats/summary)
//   2. Token trend: main line (messages.token_count per day)
//                   + stacked bar below (sessions input/output/cache/reasoning)
//   3. Response time distribution (5 buckets, 7/30d toggle)
//   4. Heatmap 7x24 (last 7 days)
//   5. Models table (sessions GROUP BY model)
//
// Everything is pure vanilla JS + inline SVG -- no third-party chart libs.

(function() {
  'use strict';

  const state = {
    granularity: 'day',       // day | week | month
    responseWindow: 30,       // 7 | 30
    loaded: false,
    inflight: null,
  };

  // ── Public entry ──
  window.showInsights = async function() {
    const root = document.getElementById('mainInsights');
    if (!root) return;
    if (!state.loaded) { await _loadAll(); state.loaded = true; }
  };

  window.refreshInsights = function() {
    _loadAll(true);
  };

  window.setInsightsGranularity = function(g) {
    state.granularity = g;
    _updateGranularityPills();
    _loadTimeseries();
  };

  window.setInsightsResponseWindow = function(w) {
    state.responseWindow = w;
    _updateResponseWindowPills();
    _loadResponseTime();
  };

  // ── Data loaders ──
  async function _loadAll(refresh = false) {
    _updateGranularityPills();
    _updateResponseWindowPills();
    await Promise.all([
      _loadSummary(refresh),
      _loadTimeseries(refresh),
      _loadResponseTime(refresh),
      _loadHeatmap(refresh),
      _loadModels(refresh),
    ]);
    _updateUpdatedAt();
  }

  async function _loadSummary(refresh = false) {
    try {
      const data = await _get('/api/stats/summary', refresh);
      _renderSummaryCards(data);
    } catch (e) { _renderError('insightsSummaryCards', e); }
  }

  async function _loadTimeseries(refresh = false) {
    try {
      const [total, split] = await Promise.all([
        _get(`/api/stats/timeseries?granularity=${state.granularity}&window=30&source=total`, refresh),
        _get(`/api/stats/timeseries?granularity=${state.granularity}&window=30&source=split`, refresh),
      ]);
      _renderTokenTimeseries(document.getElementById('insightsTimeseriesChart'), total);
      _renderSplitBars(document.getElementById('insightsSplitChart'), split);
    } catch (e) { _renderError('insightsTimeseriesChart', e); }
  }

  async function _loadResponseTime(refresh = false) {
    try {
      const data = await _get(`/api/stats/response-time?window=${state.responseWindow}`, refresh);
      _renderResponseTime(document.getElementById('insightsResponseChart'), data);
    } catch (e) { _renderError('insightsResponseChart', e); }
  }

  async function _loadHeatmap(refresh = false) {
    try {
      const data = await _get('/api/stats/heatmap?window=7', refresh);
      _renderHeatmap(document.getElementById('insightsHeatmap'), data);
    } catch (e) { _renderError('insightsHeatmap', e); }
  }

  async function _loadModels(refresh = false) {
    try {
      const data = await _get('/api/stats/models?window=30', refresh);
      _renderModelsTable(document.getElementById('insightsModelsTable'), data);
    } catch (e) { _renderError('insightsModelsTable', e); }
  }

  function _get(path, refresh) {
    if (refresh) {
      path += (path.includes('?') ? '&' : '?') + 'refresh=1';
    }
    // api() is defined in ui.js — goes through authenticated fetch
    return window.api(path);
  }

  // ── Renderers ──

  function _renderSummaryCards(data) {
    const el = document.getElementById('insightsSummaryCards');
    if (!el) return;
    if (!data || (data.total_messages === 0 && data.total_input_tokens === 0)) {
      el.innerHTML = `<div class="insights-empty">${_t('insights_empty')}</div>`;
      _setSidebarSummary('—');
      return;
    }
    const cards = [
      { label: _t('tab_insights') + ' · msgs', value: _fmt(data.total_messages) },
      { label: 'input tokens',  value: _fmt(data.total_input_tokens) },
      { label: 'output tokens', value: _fmt(data.total_output_tokens) },
      { label: 'cost (USD)',    value: (data.total_cost_usd || 0).toFixed(2) },
      { label: 'active webui',  value: data.active_webui_sessions || 0 },
      { label: 'last activity', value: data.last_activity_ts ? _relTime(data.last_activity_ts) : '—' },
    ];
    el.innerHTML = cards.map(c => `
      <div class="insights-card">
        <div class="insights-card-label">${_esc(c.label)}</div>
        <div class="insights-card-value">${_esc(c.value)}</div>
      </div>`).join('');
    _setSidebarSummary(`${_fmt(data.total_messages)} msgs · ${_fmt(data.total_input_tokens + data.total_output_tokens)} tokens`);
  }

  function _setSidebarSummary(text) {
    const el = document.getElementById('insightsSummary');
    if (el) el.textContent = text;
  }

  // Token trend: area + line
  function _renderTokenTimeseries(container, data) {
    if (!container) return;
    const points = (data && data.points) || [];
    if (!points.length) {
      container.innerHTML = `<div class="insights-empty">${_t('insights_empty')}</div>`;
      return;
    }
    const W = 600, H = 200, PAD_L = 46, PAD_R = 10, PAD_T = 10, PAD_B = 24;
    const IW = W - PAD_L - PAD_R, IH = H - PAD_T - PAD_B;
    const max = Math.max(1, ...points.map(p => p.total || 0));
    const xs = (i) => PAD_L + (points.length <= 1 ? IW / 2 : (i / (points.length - 1)) * IW);
    const ys = (v) => PAD_T + IH - (v / max) * IH;
    const linePts = points.map((p, i) => `${xs(i).toFixed(1)},${ys(p.total).toFixed(1)}`).join(' ');
    const areaPath = `M${xs(0).toFixed(1)},${ys(0).toFixed(1)} L${linePts.split(' ').join(' L')} L${xs(points.length - 1).toFixed(1)},${ys(0).toFixed(1)} Z`;
    // Y-axis ticks
    const ticks = [0, Math.round(max / 2), max];
    const yTicks = ticks.map(v => `
      <g>
        <line x1="${PAD_L}" y1="${ys(v).toFixed(1)}" x2="${W - PAD_R}" y2="${ys(v).toFixed(1)}" stroke="var(--border)" stroke-dasharray="2,3"/>
        <text x="${PAD_L - 6}" y="${ys(v).toFixed(1) + 3}" text-anchor="end" fill="var(--muted)" font-size="9">${_fmt(v)}</text>
      </g>`).join('');
    // X labels: show first, mid, last
    const xLabels = [0, Math.floor(points.length / 2), points.length - 1].filter((v, i, a) => a.indexOf(v) === i).map(i => `
      <text x="${xs(i).toFixed(1)}" y="${H - 6}" text-anchor="middle" fill="var(--muted)" font-size="9">${_esc(points[i].date)}</text>`).join('');
    container.innerHTML = `
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" style="width:100%;height:100%">
        ${yTicks}
        <path d="${areaPath}" fill="var(--accent)" fill-opacity="0.15"/>
        <polyline points="${linePts}" fill="none" stroke="var(--accent)" stroke-width="1.6" stroke-linejoin="round"/>
        ${xLabels}
      </svg>`;
  }

  // Split stacked bar (input / output / cache / reasoning)
  function _renderSplitBars(container, data) {
    if (!container) return;
    const points = (data && data.points) || [];
    if (!points.length) {
      container.innerHTML = `<div class="insights-empty" style="padding:16px">${_t('insights_empty')}</div>`;
      return;
    }
    // Group by date (sum across models for the chart).
    const byDate = {};
    for (const p of points) {
      const d = p.date;
      const entry = byDate[d] || (byDate[d] = { date: d, input: 0, output: 0, cache_read: 0, reasoning: 0 });
      entry.input += p.input; entry.output += p.output;
      entry.cache_read += p.cache_read; entry.reasoning += p.reasoning;
    }
    const dates = Object.keys(byDate).sort();
    const W = 600, H = 120, PAD_L = 46, PAD_R = 10, PAD_T = 8, PAD_B = 20;
    const IW = W - PAD_L - PAD_R, IH = H - PAD_T - PAD_B;
    const max = Math.max(1, ...dates.map(d => byDate[d].input + byDate[d].output + byDate[d].cache_read + byDate[d].reasoning));
    const barW = Math.max(2, IW / Math.max(dates.length, 1) - 2);
    const segColors = {
      input:      'var(--accent)',
      output:     '#60a5fa',
      cache_read: '#a78bfa',
      reasoning:  '#fbbf24',
    };
    const bars = dates.map((d, i) => {
      const e = byDate[d];
      const x = PAD_L + i * (IW / Math.max(dates.length, 1));
      let y = PAD_T + IH;
      const segs = ['input', 'output', 'cache_read', 'reasoning'].map(k => {
        const h = (e[k] / max) * IH;
        y -= h;
        return `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" height="${h.toFixed(1)}" fill="${segColors[k]}"/>`;
      }).join('');
      return `<g><title>${_esc(d)} · in:${_fmt(e.input)} out:${_fmt(e.output)} cache:${_fmt(e.cache_read)} reason:${_fmt(e.reasoning)}</title>${segs}</g>`;
    }).join('');
    container.innerHTML = `
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" style="width:100%;height:100%">
        <text x="${PAD_L - 6}" y="${PAD_T + 8}" text-anchor="end" fill="var(--muted)" font-size="9">${_fmt(max)}</text>
        <text x="${PAD_L - 6}" y="${PAD_T + IH}" text-anchor="end" fill="var(--muted)" font-size="9">0</text>
        ${bars}
      </svg>
      <div style="display:flex;gap:14px;font-size:10px;color:var(--muted);padding:4px 6px 0 ${PAD_L}px">
        <span><span style="display:inline-block;width:8px;height:8px;background:${segColors.input};margin-right:4px"></span>input</span>
        <span><span style="display:inline-block;width:8px;height:8px;background:${segColors.output};margin-right:4px"></span>output</span>
        <span><span style="display:inline-block;width:8px;height:8px;background:${segColors.cache_read};margin-right:4px"></span>cache</span>
        <span><span style="display:inline-block;width:8px;height:8px;background:${segColors.reasoning};margin-right:4px"></span>reasoning</span>
      </div>`;
  }

  function _renderResponseTime(container, data) {
    if (!container) return;
    const buckets = (data && data.buckets) || [];
    if (!data || !data.total) {
      container.innerHTML = `<div class="insights-empty">${_t('insights_empty')}</div>`;
      return;
    }
    const W = 600, H = 200, PAD_L = 46, PAD_R = 10, PAD_T = 10, PAD_B = 30;
    const IW = W - PAD_L - PAD_R, IH = H - PAD_T - PAD_B;
    const max = Math.max(1, ...buckets.map(b => b.count));
    const barW = Math.max(10, IW / buckets.length - 10);
    const bars = buckets.map((b, i) => {
      const x = PAD_L + i * (IW / buckets.length) + 5;
      const h = (b.count / max) * IH;
      const y = PAD_T + IH - h;
      return `
        <g>
          <title>${_esc(b.label)}: ${b.count} messages${b.min_ms ? ` · min ${b.min_ms}ms max ${b.max_ms}ms` : ''}</title>
          <rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" height="${h.toFixed(1)}" fill="var(--accent)" rx="2"/>
          <text x="${(x + barW / 2).toFixed(1)}" y="${(y - 4).toFixed(1)}" text-anchor="middle" fill="var(--text)" font-size="10">${b.count}</text>
          <text x="${(x + barW / 2).toFixed(1)}" y="${H - 8}" text-anchor="middle" fill="var(--muted)" font-size="10">${_esc(b.label)}</text>
        </g>`;
    }).join('');
    container.innerHTML = `
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" style="width:100%;height:100%">
        ${bars}
      </svg>`;
  }

  function _renderHeatmap(container, data) {
    if (!container) return;
    const cells = (data && data.cells) || [];
    const DAYS = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
    const max = Math.max(1, ...cells.flat());
    const hourHeader = `<div class="hm-hours">
      <div></div>
      ${Array.from({length: 24}, (_, h) => `<div class="hm-hour-label">${h}</div>`).join('')}
    </div>`;
    const rows = cells.map((row, d) => {
      const r = row.map((v, h) => {
        const intensity = Math.log10((v || 0) + 1) / Math.log10(max + 1);
        const alpha = v > 0 ? 0.12 + intensity * 0.8 : 0.04;
        return `<div class="hm-cell" style="background:rgba(201,168,76,${alpha.toFixed(3)})" title="${DAYS[d]} ${h}:00 — ${v} messages"></div>`;
      }).join('');
      return `<div class="hm-label">${DAYS[d]}</div>${r}`;
    }).join('');
    container.innerHTML = hourHeader + rows;
  }

  function _renderModelsTable(container, data) {
    if (!container) return;
    const models = (data && data.models) || [];
    if (!models.length) {
      container.innerHTML = `<div class="insights-empty">${_t('insights_empty')}</div>`;
      return;
    }
    container.innerHTML = `
      <table>
        <thead>
          <tr>
            <th>Model</th>
            <th>Input</th>
            <th>Output</th>
            <th>Messages</th>
            <th>Cost (USD)</th>
            <th>%</th>
          </tr>
        </thead>
        <tbody>
          ${models.map(m => `
            <tr>
              <td>${_esc(m.model)}</td>
              <td>${_fmt(m.input_tokens)}</td>
              <td>${_fmt(m.output_tokens)}</td>
              <td>${_fmt(m.message_count)}</td>
              <td>${(m.estimated_cost_usd || 0).toFixed(2)}</td>
              <td>${(m.pct || 0).toFixed(1)}</td>
            </tr>`).join('')}
        </tbody>
      </table>`;
  }

  function _renderError(id, err) {
    const el = document.getElementById(id);
    if (el) el.innerHTML = `<div class="insights-empty">Error: ${_esc(String(err && err.message || err || 'unknown'))}</div>`;
  }

  // ── Helpers ──
  function _t(key) { return (window.t && window.t(key)) || key; }
  function _esc(s) { return (window.esc && window.esc(s)) || String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
  function _fmt(n) { n = Number(n || 0); if (Math.abs(n) >= 1e9) return (n / 1e9).toFixed(1) + 'B'; if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(1) + 'M'; if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(1) + 'K'; return String(n); }
  function _relTime(ts) {
    const dt = Date.now() / 1000 - ts;
    if (dt < 60) return Math.round(dt) + 's ago';
    if (dt < 3600) return Math.round(dt / 60) + 'm ago';
    if (dt < 86400) return Math.round(dt / 3600) + 'h ago';
    return Math.round(dt / 86400) + 'd ago';
  }
  function _updateUpdatedAt() {
    const el = document.getElementById('insightsUpdatedAt');
    if (el) el.textContent = 'Updated ' + new Date().toLocaleTimeString();
  }
  function _updateGranularityPills() {
    document.querySelectorAll('.insights-chart-ctrls [data-granularity]').forEach(el => {
      el.classList.toggle('active', el.dataset.granularity === state.granularity);
    });
  }
  function _updateResponseWindowPills() {
    document.querySelectorAll('.insights-chart-ctrls [data-window]').forEach(el => {
      el.classList.toggle('active', Number(el.dataset.window) === state.responseWindow);
    });
  }
})();
