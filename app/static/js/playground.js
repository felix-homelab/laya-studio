/* Playground: state + question editors, live linting, results, history. */

(() => {
  const { api, toast, escapeHtml, pct, num, questionCard, routingLine, debounce } = window.LayaStudio;

  const form = document.getElementById('eval-form');
  const routePreview = document.getElementById('route-preview');
  const schemaSelect = document.getElementById('schema-select');
  const primarySelect = document.getElementById('primary-question');
  const modelSelect = document.getElementById('model-select');
  const calibrationToggle = document.getElementById('apply-calibration');
  const lintPanel = document.getElementById('lint-panel');
  const resultPanel = document.getElementById('result-panel');
  const historyList = document.getElementById('history-list');
  const evaluateBtn = document.getElementById('evaluate-btn');

  const SAMPLES = {
    email: 'Subject: Duplicate charge on invoice #4411\nFrom: dana@acme.example\n\nHi, we were billed twice for March. Please refund the duplicate today or we will cancel our plan.',
    blocked: 'Subject: API returns 500 on webhook delivery\n\nSince this morning every webhook call returns a 500 and our production sync is blocked. We need this fixed today.',
    multilingual: '件名: 請求書の重複について\n\n同じ月に二回請求されています。返金してください。',
  };

  let currentSchema = null;
  let lastResult = null;
  const evaluateLabel = evaluateBtn.innerHTML;

  const state = window.LayaStudio.stateEditor(document.getElementById('state-editor'), {
    rows: 9,
    onChange: () => probeRoute(),
  });

  const questions = window.LayaStudio.questionEditor(document.getElementById('questions-editor'), {
    onChange: () => { syncPrimaryOptions(); lintLater(); },
  });

  const lintLater = debounce(lint, 600);

  /* ------------------------------------------------------------ utilities */

  function currentSchemaId() {
    const value = schemaSelect.value;
    return value ? Number(value) : null;
  }

  /* ------------------------------------------------- schema + question sync */

  function syncPrimaryOptions() {
    const bundle = questions.questions();
    const previous = primarySelect.value;
    primarySelect.innerHTML = '';

    Object.entries(bundle).forEach(([name, definition]) => {
      const option = document.createElement('option');
      option.value = name;
      option.textContent = `${name} · ${(definition && definition.type) || 'choice'}`;
      primarySelect.appendChild(option);
    });

    // Prefer the first choice question, which is what the backend would pick.
    const entries = Object.entries(bundle);
    const preferred = entries.find(([, definition]) => definition && definition.type === 'choice') || entries[0];
    primarySelect.value = bundle[previous] ? previous : (preferred ? preferred[0] : '');
  }

  async function loadSchema(schemaId) {
    if (!schemaId) return;
    try {
      currentSchema = await api(`/api/schemas/${schemaId}`);
      questions.setQuestions(currentSchema.definition.questions);
      syncPrimaryOptions();
      lint();
    } catch (error) {
      toast(`Could not load schema: ${error.message}`, 'error');
    }
  }

  async function lint() {
    const bundle = questions.questions();
    if (!Object.keys(bundle).length) {
      lintPanel.innerHTML = '<p class="text-[11px] text-rose-300">Add at least one question.</p>';
      return false;
    }
    try {
      const result = await api('/api/playground/lint', {
        method: 'POST',
        body: JSON.stringify({ questions: bundle }),
      });
      lintPanel.innerHTML = result.findings.length
        ? result.findings.map((f) => `
            <p class="text-[11px] ${f.severity === 'error' ? 'text-rose-300' : 'text-amber-300'}">
              <span class="font-semibold">${f.severity}</span>
              ${f.question ? `<span class="font-mono text-slate-400">${escapeHtml(f.question)}</span>: ` : ''}
              ${escapeHtml(f.message)}
            </p>`).join('')
        : '<p class="text-[11px] text-emerald-300">Schema looks clean.</p>';
      return result.ok;
    } catch (error) {
      lintPanel.innerHTML = `<p class="text-[11px] text-rose-300">${escapeHtml(error.message)}</p>`;
      return false;
    }
  }

  /* ----------------------------------------------------------- route probe */

  const probeRoute = debounce(async () => {
    const value = state.value();
    if (!value) {
      routePreview.classList.add('hidden');
      return;
    }
    try {
      const result = await api('/api/playground/route', {
        method: 'POST',
        body: JSON.stringify({ state: value }),
      });
      const routing = result.routing || {};
      routePreview.classList.remove('hidden');
      routePreview.innerHTML = routingLine(routing, 'router');
    } catch {
      routePreview.classList.add('hidden');
    }
  }, 500);

  /* ------------------------------------------------------- render a result */

  function renderResult(result) {
    lastResult = result;
    const results = result.question_results || [];

    const headline = `
      <div class="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p class="text-[11px] font-medium uppercase tracking-wider text-slate-500">
            ${escapeHtml(result.primary_question || 'decision')}
          </p>
          <p class="mt-1 flex flex-wrap items-baseline gap-2">
            <span class="text-2xl font-semibold text-slate-50">${escapeHtml(result.top_choice ?? '—')}</span>
            <span class="text-sm text-slate-400">${pct(result.confidence)} confidence</span>
            ${result.calibrated
              ? '<span class="rounded-full bg-sky-500/15 px-2 py-0.5 text-[10px] font-medium text-sky-300">calibrated</span>'
              : '<span class="rounded-full bg-slate-500/15 px-2 py-0.5 text-[10px] font-medium text-slate-400">raw score</span>'}
          </p>
        </div>
        <div class="text-right text-[11px] text-slate-500">
          <p><span class="font-mono text-slate-300">${num(result.latency_ms, 1)} ms</span> forward pass</p>
          <p class="mt-0.5">engine <span class="font-mono text-slate-300">${escapeHtml(result.engine)}</span> · #${result.id ?? '—'}</p>
        </div>
      </div>
      <div class="mt-3">${routingLine(result.routing, result.engine)}</div>`;

    const grid = results.length
      ? `<div class="mt-5 grid gap-3 md:grid-cols-2">${results.map((q) => questionCard(q)).join('')}</div>`
      : '';

    const warnings = (result.warnings || []).length
      ? `<div class="mt-4 space-y-1.5">
           ${result.warnings.map((w) => `
             <p class="flex items-start gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-[11px] text-amber-200">
               <span class="mt-0.5">⚠</span><span>${escapeHtml(w)}</span>
             </p>`).join('')}
         </div>`
      : '';

    const labelOptions = results
      .flatMap((q) => (q.breakdown || []).map((b) => b.label))
      .filter((v, i, a) => a.indexOf(v) === i);

    const labelBlock = result.id ? `
      <div class="mt-5 rounded-xl border border-slate-800 bg-slate-950/50 p-4">
        <p class="text-xs font-medium text-slate-300">Ground truth</p>
        <p class="mt-0.5 text-[11px] text-slate-500">
          Labelling rows is what makes the calibration monitor work — it compares stated
          confidence against observed accuracy. Pick one of the answers above, or type your own.
        </p>
        <div class="mt-2.5 flex flex-wrap gap-2">
          <input id="label-input" list="label-options" placeholder="actual answer"
            class="min-w-[180px] flex-1 rounded-lg border border-slate-800 bg-slate-950 px-2.5 py-1.5 text-xs text-slate-200 outline-none focus:border-sky-600">
          <datalist id="label-options">
            ${labelOptions.map((v) => `<option value="${escapeHtml(v)}"></option>`).join('')}
          </datalist>
          <button type="button" id="save-label"
            class="rounded-lg bg-slate-800 px-3 py-1.5 text-xs font-medium text-slate-200 hover:bg-slate-700">Save label</button>
        </div>
      </div>` : '';

    resultPanel.innerHTML = headline + warnings + grid + labelBlock;

    const saveLabel = document.getElementById('save-label');
    if (saveLabel) {
      saveLabel.addEventListener('click', async () => {
        const value = document.getElementById('label-input').value.trim();
        if (!value) { toast('Enter the correct answer first', 'warn'); return; }
        try {
          await api(`/api/playground/evaluations/${result.id}/label`, {
            method: 'POST',
            body: JSON.stringify({ label: value, label_source: 'human' }),
          });
          toast('Label saved — this row now counts toward calibration', 'success');
        } catch (error) {
          toast(error.message, 'error');
        }
      });
    }
  }

  /* -------------------------------------------------------------- history */

  async function loadHistory() {
    try {
      const rows = await api('/api/playground/history?limit=20');
      if (!rows.length) {
        historyList.innerHTML = '<p class="px-5 py-6 text-center text-xs text-slate-500">Nothing yet.</p>';
        return;
      }
      historyList.innerHTML = rows.map((row) => `
        <button type="button" data-history-id="${row.id}"
          class="flex w-full items-center gap-3 px-5 py-3 text-left hover:bg-slate-800/40">
          <span class="min-w-0 flex-1">
            <span class="block truncate text-xs text-slate-300">${escapeHtml(row.state_text.slice(0, 120))}</span>
            <span class="mt-0.5 block text-[11px] text-slate-500">
              ${escapeHtml(row.primary_question || '—')} · ${num(row.latency_ms, 1)} ms · ${escapeHtml(row.source)}
            </span>
          </span>
          <span class="shrink-0 text-right">
            <span class="block text-xs font-medium text-slate-200">${escapeHtml(row.top_choice ?? '—')}</span>
            <span class="block text-[11px] text-slate-500">${pct(row.confidence)}</span>
          </span>
        </button>`).join('');

      historyList.querySelectorAll('[data-history-id]').forEach((node) => {
        node.addEventListener('click', async () => {
          try {
            const detail = await api(`/api/playground/evaluations/${node.dataset.historyId}`);
            state.setValue(detail.state);
            renderResult(detail);
          } catch (error) {
            toast(error.message, 'error');
          }
        });
      });
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  /* ------------------------------------------------------------- handlers */

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const value = state.value();
    if (!value) { toast('Give the run a state', 'warn'); return; }

    const bundle = questions.questions();
    const ok = await lint();
    if (!ok) { toast('Fix the question errors first', 'warn'); return; }

    evaluateBtn.disabled = true;
    evaluateBtn.textContent = 'Evaluating…';

    try {
      const payload = {
        state: value,
        schema_id: currentSchemaId(),
        model: modelSelect.value || null,
        primary_question: primarySelect.value || null,
        apply_calibration: calibrationToggle.checked,
        persist: true,
      };
      if (Object.keys(bundle).length) payload.questions = bundle;

      const result = await api('/api/playground/evaluate', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      renderResult(result);
      loadHistory();
    } catch (error) {
      toast(error.message, 'error', 7000);
    } finally {
      evaluateBtn.disabled = false;
      evaluateBtn.innerHTML = '▶ Run evaluation';
    }
  });

  schemaSelect.addEventListener('change', () => loadSchema(currentSchemaId()));
  document.getElementById('reset-questions').addEventListener('click', () => loadSchema(currentSchemaId()));
  document.getElementById('refresh-history').addEventListener('click', loadHistory);

  document.getElementById('save-as-schema').addEventListener('click', async () => {
    const bundle = questions.questions();
    if (!Object.keys(bundle).length) { toast('Add at least one question', 'error'); return; }
    const name = window.prompt('Name for the new schema:', `${currentSchema ? currentSchema.name : 'Schema'} (copy)`);
    if (!name) return;
    try {
      const created = await api('/api/schemas', {
        method: 'POST',
        body: JSON.stringify({
          name,
          description: currentSchema ? currentSchema.description : '',
          questions: bundle,
          tags: [],
        }),
      });
      toast(`Saved “${created.name}”`, 'success');
      const option = document.createElement('option');
      option.value = created.id;
      option.textContent = created.name;
      schemaSelect.appendChild(option);
      schemaSelect.value = String(created.id);
      loadSchema(created.id);
    } catch (error) {
      toast(error.message, 'error');
    }
  });

  document.querySelectorAll('[data-sample]').forEach((button) => {
    button.addEventListener('click', () => {
      state.setText(SAMPLES[button.dataset.sample] || '');
      probeRoute();
    });
  });

  /* --------------------------------------------------------------- start */

  if (schemaSelect.value) {
    loadSchema(Number(schemaSelect.value));
  } else {
    lintPanel.innerHTML = '<p class="text-[11px] text-slate-500">No schemas yet — create one on the Schemas page.</p>';
  }
  loadHistory();
})();
