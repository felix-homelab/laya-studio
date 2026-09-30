/* Shared helpers: API access, toasts, formatting, small DOM utilities. */

const LayaStudio = (() => {
  async function api(path, options = {}) {
    // ``headers`` is merged, not spread with the rest: a caller's own headers
    // must not wipe out the JSON content type.
    const { headers, ...rest } = options;
    const response = await fetch(path, {
      ...rest,
      headers: options.body instanceof FormData
        ? (headers || {})
        : { 'Content-Type': 'application/json', ...(headers || {}) },
    });

    const text = await response.text();
    let payload = null;
    if (text) {
      try { payload = JSON.parse(text); } catch { payload = text; }
    }

    if (!response.ok) {
      const detail = payload && payload.detail
        ? (typeof payload.detail === 'string' ? payload.detail : JSON.stringify(payload.detail))
        : `HTTP ${response.status}`;
      const error = new Error(detail);
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  function toast(message, kind = 'info', timeout = 4200) {
    const root = document.getElementById('toast-root');
    if (!root) return;

    const tones = {
      info: 'border-slate-700 bg-slate-900 text-slate-200',
      success: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-200',
      error: 'border-rose-500/40 bg-rose-500/10 text-rose-200',
      warn: 'border-amber-500/40 bg-amber-500/10 text-amber-200',
    };

    const node = document.createElement('div');
    node.className = `toast pointer-events-auto max-w-sm rounded-xl border px-4 py-3 text-sm shadow-lg ${tones[kind] || tones.info}`;
    node.textContent = message;
    root.appendChild(node);
    setTimeout(() => node.remove(), timeout);
  }

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, (c) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  }

  // ``Number.isFinite(Number(...))``, not ``Number.isNaN(value)``: the latter
  // does not coerce, so a string like "n/a" slipped through and rendered "NaN%".
  function pct(value, digits = 1) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
    return `${(Number(value) * 100).toFixed(digits)}%`;
  }

  function num(value, digits = 2) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
    return Number(value).toFixed(digits);
  }

  function bandClasses(band) {
    return {
      act: 'bg-emerald-500/15 text-emerald-300 ring-emerald-500/30',
      verify: 'bg-amber-500/15 text-amber-300 ring-amber-500/30',
      escalate: 'bg-rose-500/15 text-rose-300 ring-rose-500/30',
    }[band] || 'bg-slate-500/15 text-slate-300 ring-slate-500/30';
  }

  function barTone(band) {
    return { act: 'bg-emerald-500', verify: 'bg-amber-500', escalate: 'bg-rose-500' }[band] || 'bg-sky-500';
  }

  /** Chip colours for a job/run/action status. Mirrors ``status_color`` in
   *  ``app/templating.py`` so client-rendered chips match the first paint. */
  function statusClasses(status) {
    return {
      succeeded: 'bg-emerald-500/15 text-emerald-300',
      ok: 'bg-emerald-500/15 text-emerald-300',
      approved: 'bg-emerald-500/15 text-emerald-300',
      running: 'bg-sky-500/15 text-sky-300',
      in_review: 'bg-sky-500/15 text-sky-300',
      pending: 'bg-slate-500/15 text-slate-300',
      skipped: 'bg-slate-500/15 text-slate-300',
      corrected: 'bg-amber-500/15 text-amber-300',
      failed: 'bg-rose-500/15 text-rose-300',
      rejected: 'bg-rose-500/15 text-rose-300',
    }[status] || 'bg-slate-500/15 text-slate-300';
  }

  /**
   * One-line summary of a state dict for a table cell.
   *
   * Structured values are shown as JSON rather than through ``String()``, which
   * would print ``[object Object]`` and tell the reader nothing.
   *
   * Returns plain, *unescaped* text: callers must pass it through
   * ``escapeHtml`` before putting it into ``innerHTML``.
   */
  function summarizeState(state, limit = 140) {
    return Object.entries(state || {})
      .map(([key, value]) => `${key}: ${value === null || typeof value !== 'object' ? value : JSON.stringify(value)}`)
      .join(' · ')
      .slice(0, limit);
  }

  function debounce(fn, wait = 300) {
    let timer;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), wait);
    };
  }

  async function copy(text) {
    try {
      await navigator.clipboard.writeText(text);
      toast('Copied to clipboard', 'success', 1800);
    } catch {
      toast('Clipboard unavailable', 'warn');
    }
  }

  /** Render the probability breakdown block shared by all primitives. */
  function renderBreakdown(entry, options = {}) {
    const limit = options.limit ?? 8;
    const rows = (entry.breakdown || []).slice(0, limit);
    if (!rows.length) {
      return '<p class="text-xs text-slate-500">No probability distribution was returned for this question.</p>';
    }
    return rows.map((row) => `
      <div class="prob-bar space-y-1">
        <div class="flex items-baseline justify-between gap-3 text-xs">
          <span class="${row.is_top ? 'font-semibold text-slate-100' : 'text-slate-400'}">${escapeHtml(row.label)}</span>
          <span class="font-mono ${row.is_top ? 'text-slate-200' : 'text-slate-500'}">${pct(row.probability)}</span>
        </div>
        <div class="h-1.5 w-full overflow-hidden rounded-full bg-slate-800">
          <span class="block h-full rounded-full ${row.is_top ? 'bg-sky-500' : 'bg-slate-600'}"
                style="width: ${Math.max(row.probability * 100, 0.5)}%"></span>
        </div>
      </div>`).join('');
  }

  function questionCard(entry, context = {}) {
    const headline = entry.type === 'noul'
      ? `<span class="font-mono text-2xl font-semibold ${entry.noul >= 0.5 ? 'text-rose-300' : 'text-emerald-300'}">${pct(entry.noul)}</span>
         <span class="ml-2 text-xs text-slate-500">calibrated P(true)</span>`
      : entry.type === 'score'
        ? `<span class="font-mono text-2xl font-semibold text-sky-300">${num(entry.score)}</span>
           <span class="text-xs text-slate-500">/ ${num(entry.max_score, 1)}</span>
           ${entry.level ? `<span class="ml-2 text-xs text-slate-400">${escapeHtml(entry.level)}</span>` : ''}`
        : `<span class="text-lg font-semibold text-slate-100">${escapeHtml(entry.choice ?? '—')}</span>`;

    const calibrated = entry.calibrated
      ? `<span class="rounded-full bg-sky-500/15 px-2 py-0.5 text-[10px] font-medium text-sky-300"
               title="Laya Studio applied a fitted logistic map to this confidence: p' = sigmoid(${num(entry.calibration_slope, 3)} * logit(p) + ${num(entry.calibration_intercept, 3)})">
           calibrated
         </span>`
      : '<span class="rounded-full bg-slate-500/15 px-2 py-0.5 text-[10px] font-medium text-slate-400" title="No recalibration applied; this is the value the model reported">raw</span>';

    // Laya carries a trained act/escalate head. Surface its opinion rather than
    // hiding it — it is a second view on the same question, not a substitute for
    // the confidence ladder.
    const act = (entry.act_probability === null || entry.act_probability === undefined)
      ? ''
      : `<span class="rounded-full bg-violet-500/15 px-2 py-0.5 text-[10px] font-medium text-violet-300"
               title="Laya's trained act head: its own view on whether to act automatically or escalate">
           act ${pct(entry.act_probability)}
         </span>`;

    return `
      <article class="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
        <header class="flex items-start justify-between gap-3">
          <div class="min-w-0">
            <p class="truncate text-xs font-medium uppercase tracking-wider text-slate-500">${escapeHtml(entry.question)}</p>
            <p class="mt-0.5 line-clamp-2 text-xs text-slate-500">${escapeHtml(entry.instructions || '')}</p>
          </div>
          <span class="inline-flex shrink-0 items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-semibold ring-1 ring-inset ${bandClasses(entry.band)}">
            <span class="h-1.5 w-1.5 rounded-full bg-current"></span>${escapeHtml(entry.band)}
          </span>
        </header>

        <div class="mt-3 flex flex-wrap items-center gap-2">
          ${headline}
          ${calibrated}
          ${act}
          ${entry.confidence !== null && entry.confidence !== undefined
            ? `<span class="text-xs text-slate-500">
                 confidence ${pct(entry.confidence)}
                 ${entry.calibrated && entry.raw_confidence !== null && entry.raw_confidence !== undefined
                   ? `<span class="text-slate-600">(was ${pct(entry.raw_confidence)})</span>` : ''}
               </span>` : ''}
        </div>
        ${entry.type === 'score' && entry.level
          ? `<p class="mt-2 text-[11px] text-slate-500">
               expected ${num(entry.score, 2)} on a 0–${num(entry.max_score, 0)} rubric ·
               most likely level <span class="text-slate-300">${escapeHtml(entry.level)}</span>
             </p>` : ''}

        <div class="mt-4 space-y-2.5">${renderBreakdown(entry, context)}</div>
      </article>`;
  }

  function routingLine(routing, engine) {
    if (!routing || !routing.model) return '';
    return `
      <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-slate-500">
        <span class="rounded bg-slate-800 px-1.5 py-0.5 font-mono text-slate-300">${escapeHtml(routing.model)}</span>
        <span>${escapeHtml(routing.reason || '')}</span>
        <span class="text-slate-600">engine: ${escapeHtml(engine || 'unknown')}</span>
      </div>`;
  }

  /* ----------------------------------------------------- engine readiness */

  /** How long to wait between readiness probes while Laya is still loading. */
  const ENGINE_POLL_MS = 5000;

  /**
   * Follow the engine until it settles, and patch the result into the page.
   *
   * The loading banner, the sidebar card and the header dot are all rendered by the
   * server from the state that existed at page load, and the banner promises it
   * "disappears on its own" — which, until now, nothing made true except a reload.
   * On a cold cache that is minutes of a page insisting the engine is still coming up.
   *
   * `GET /api/health` is in the request-log skip list
   * (``app/observability/middleware.py``), so probing it costs one local row count
   * and writes nothing to the log store.
   *
   * Patched in place rather than by reloading, deliberately: a reload would discard
   * whatever the operator had typed into the playground, a schema editor or a
   * workflow block script.
   */
  function watchEngineState() {
    const initial = document.body.dataset.engineState;
    if (!initial || initial === 'ready' || initial === 'failed') return; // already settled

    const poll = async () => {
      let health;
      try {
        health = await api('/api/health');
      } catch {
        return; // the panel cannot answer health: not this poller's problem
      }
      const engine = (health && health.engine) || {};
      if (engine.state === 'ready' || engine.state === 'failed') {
        applyEngineState(engine.state, engine);
        return;
      }
      window.setTimeout(poll, ENGINE_POLL_MS);
    };

    window.setTimeout(poll, ENGINE_POLL_MS);
  }

  /** Write one settled engine state into the three places that show it. */
  function applyEngineState(state, engine) {
    const ready = state === 'ready';
    const error = engine.error || 'Laya could not be loaded.';
    const name = engine.name || 'laya';

    const banner = document.getElementById('engine-banner');
    if (banner) {
      if (ready) {
        banner.remove();
      } else {
        // Mirrors the server-rendered `failed` banner in base.html.
        banner.className = 'mb-5 rounded-2xl border border-rose-500/30 bg-rose-500/10 px-5 py-4';
        banner.innerHTML = `
          <p class="text-sm font-semibold text-rose-200">Laya could not be loaded</p>
          <pre class="mt-2 overflow-auto whitespace-pre-wrap font-mono text-[11px] leading-relaxed text-rose-200/80">${escapeHtml(error)}</pre>
          <p class="mt-2 text-xs text-rose-200/80">
            Run <span class="font-mono">laya-studio check</span> to diagnose the environment.
          </p>`;
      }
    }

    const dot = document.getElementById('engine-dot');
    if (dot) dot.className = `h-1.5 w-1.5 rounded-full ${ready ? 'bg-emerald-400' : 'bg-rose-400'}`;

    const label = document.getElementById('engine-status');
    if (label) {
      label.textContent = ready && engine.device ? `${name} · ${engine.device}` : name;
    }

    const card = document.getElementById('engine-card');
    if (card) {
      const checkpoints = ready && Array.isArray(engine.loaded_checkpoints)
        ? engine.loaded_checkpoints.filter(Boolean).join(', ')
        : '';
      // Kept to the two facts the operator acts on rather than reproducing the
      // server's fuller card; the next navigation renders that one properly.
      card.innerHTML = ready
        ? `<div class="rounded-xl border border-emerald-500/30 bg-emerald-500/10 p-3">
             <p class="flex items-center gap-1.5 text-[11px] font-semibold text-emerald-300">
               <span class="h-1.5 w-1.5 rounded-full bg-emerald-400"></span> Laya runtime
             </p>
             <p class="mt-1 text-[11px] leading-snug text-emerald-200/80">
               ready on ${escapeHtml(engine.device || 'auto')}${checkpoints ? ` · ${escapeHtml(checkpoints)}` : ''}
             </p>
           </div>`
        : `<div class="rounded-xl border border-rose-500/30 bg-rose-500/10 p-3">
             <p class="flex items-center gap-1.5 text-[11px] font-semibold text-rose-300">
               Engine unavailable
             </p>
             <p class="mt-1 text-[11px] leading-snug text-rose-200/80">${escapeHtml(error)}</p>
           </div>`;
    }
  }

  watchEngineState();

  return {
    api, toast, escapeHtml, pct, num, bandClasses, barTone, statusClasses, debounce, copy,
    questionCard, routingLine, summarizeState,
  };
})();

window.LayaStudio = LayaStudio;
