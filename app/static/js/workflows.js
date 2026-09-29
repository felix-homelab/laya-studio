/* Workflows: the block script, manual runs, run inspector with the action log. */

(() => {
  const { api, toast, escapeHtml, pct, num, copy } = window.LayaStudio;

  const form = document.getElementById('workflow-form');
  const list = document.getElementById('wf-list');
  const runsList = document.getElementById('runs-list');
  const runDetail = document.getElementById('run-detail');
  const runOutput = document.getElementById('run-output');
  const runSelect = document.getElementById('run-workflow-select');
  const summary = document.getElementById('wf-script-summary');

  const els = {
    title: document.getElementById('wf-form-title'),
    name: document.getElementById('wf-name'),
    description: document.getElementById('wf-description'),
    schema: document.getElementById('wf-schema'),
    trigger: document.getElementById('wf-trigger'),
    question: document.getElementById('wf-question'),
    auto: document.getElementById('wf-auto'),
    verify: document.getElementById('wf-verify'),
  };

  /** Mirrors the service's own defaults: the ladder is visible from block one. */
  const DEFAULT_STEPS = [
    { on: 'auto', kind: 'noop', config: { detail: 'Auto-executed {choice} at {confidence} confidence.' } },
    { on: 'verify', kind: 'noop', config: { detail: 'Verified {choice} via the generative fallback.' } },
    { on: 'review', kind: 'review', config: { reason: 'low_confidence', priority: 50 } },
  ];

  let editing = null;

  const thresholds = () => ({
    auto: Number(els.auto.value || 0),
    verify: Number(els.verify.value || 0),
  });

  function hints() {
    const { auto, verify } = thresholds();
    const act = pct(auto, 0);
    const middle = pct(verify, 0);
    return {
      auto: `confidence ≥ ${act} — act on Laya's answer`,
      verify: `${middle} ≤ confidence < ${act} — ask the fallback model first`,
      review: `confidence < ${middle} — a human decides`,
      always: 'runs on every route, including escalations',
    };
  }

  const blocks = window.LayaStudio.blockBuilder(document.getElementById('wf-blocks'), {
    steps: DEFAULT_STEPS,
    hints: hints(),
    onChange: updateSummary,
  });

  const state = window.LayaStudio.stateEditor(document.getElementById('run-state-editor'), {
    rows: 5,
    placeholder: 'Subject: Duplicate charge on invoice #4411\n\nWe were billed twice and will cancel if this is not resolved.',
  });

  function updateSummary(steps) {
    const current = steps || blocks.steps();
    if (!current.length) {
      summary.textContent = 'no blocks yet — everything routes to review';
      return;
    }
    const counts = current.reduce((acc, step) => {
      acc[step.on] = (acc[step.on] || 0) + 1;
      return acc;
    }, {});
    const mix = ['auto', 'verify', 'review', 'always']
      .filter((route) => counts[route])
      .map((route) => `${counts[route]} on ${route}`)
      .join(' · ');
    summary.textContent = `${current.length} block${current.length === 1 ? '' : 's'} — ${mix}`;
  }

  function newWorkflow() {
    editing = null;
    els.title.textContent = 'New workflow';
    els.name.value = '';
    els.description.value = '';
    els.trigger.value = 'api';
    // The page renders the configured ladder as the inputs' defaults.
    els.auto.value = els.auto.defaultValue || '0.90';
    els.verify.value = els.verify.defaultValue || '0.60';
    blocks.setHints(hints());
    blocks.setSteps(DEFAULT_STEPS);
    updateSummary();
    loadQuestions();
  }

  async function loadQuestions() {
    const schemaId = els.schema.value;
    if (!schemaId) return;
    try {
      const schema = await api(`/api/schemas/${schemaId}`);
      const previous = els.question.value;
      els.question.innerHTML = '<option value="">auto (first choice question)</option>'
        + Object.entries(schema.definition.questions).map(([name, definition]) =>
          `<option value="${escapeHtml(name)}">${escapeHtml(name)} · ${escapeHtml(definition.type || 'choice')}</option>`).join('');
      els.question.value = previous;
    } catch { /* the auto option is always available */ }
  }

  async function open(workflowId) {
    try {
      const workflow = await api(`/api/workflows/${workflowId}`);
      editing = workflow;
      els.title.textContent = `Editing ${workflow.name}`;
      els.name.value = workflow.name;
      els.description.value = workflow.description || '';
      els.schema.value = workflow.schema_id ?? els.schema.value;
      els.trigger.value = workflow.trigger_type;
      els.auto.value = workflow.auto_threshold;
      els.verify.value = workflow.verify_threshold;
      blocks.setHints(hints());
      blocks.setSteps(workflow.steps);
      updateSummary();
      await loadQuestions();
      els.question.value = workflow.decision_question || '';
      updateIngestUrl(workflow.key);
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  function updateIngestUrl(key) {
    document.getElementById('ingest-url').textContent = `${window.location.origin}/api/ingest/${key || ''}`;
  }

  /** Show the ingest URL of the workflow picked in the run box.
   *
   * The URL is keyed by ``workflow.key``, which only the API knows: the options
   * carry the display name, and a slug of that need not match the real key. */
  async function refreshIngestUrl() {
    const workflowId = runSelect.value;
    if (!workflowId) { updateIngestUrl(); return; }
    try {
      const workflow = await api(`/api/workflows/${workflowId}`);
      if (runSelect.value === workflowId) updateIngestUrl(workflow.key);
    } catch { /* non-fatal */ }
  }

  /** Badge colours for one action-log entry. */
  function actionTone(status) {
    if (status === 'ok') return 'bg-emerald-500/15 text-emerald-300';
    if (status === 'skipped') return 'bg-slate-700/40 text-slate-400';
    return 'bg-rose-500/15 text-rose-300';
  }

  function renderRun(payload, evaluation) {
    const route = payload.run ? payload.run.route : payload.route;
    const effective = payload.effective_route || route;
    const tone = { auto: 'emerald', verify: 'amber', review: 'rose' }[effective] || 'slate';

    const verification = payload.verification;
    const verificationBlock = verification ? `
      <div class="mt-3 rounded-xl border border-slate-800 bg-slate-950/50 p-3">
        <p class="text-[11px] font-medium uppercase tracking-wider text-slate-500">Generative fallback</p>
        <p class="mt-1 text-xs text-slate-300">
          status <span class="font-mono">${escapeHtml(verification.status)}</span>
          ${verification.model ? `· model <span class="font-mono">${escapeHtml(verification.model)}</span>` : ''}
          ${verification.agrees === null || verification.agrees === undefined ? '' : `· ${verification.agrees ? 'agreed with Laya' : 'disagreed'}`}
        </p>
        ${verification.choice ? `<p class="mt-1 text-xs text-slate-400">verdict: <span class="font-medium text-slate-200">${escapeHtml(verification.choice)}</span></p>` : ''}
        ${verification.rationale ? `<p class="mt-1 text-xs text-slate-500">${escapeHtml(verification.rationale)}</p>` : ''}
        ${verification.error ? `<p class="mt-1 text-xs text-amber-300">${escapeHtml(verification.error)}</p>` : ''}
      </div>` : '';

    const actions = (payload.actions || []).length ? `
      <div class="mt-3 rounded-xl border border-slate-800 bg-slate-950/50 p-3">
        <p class="text-[11px] font-medium uppercase tracking-wider text-slate-500">Actions</p>
        <div class="mt-2 space-y-1.5">
          ${payload.actions.map((a) => `
            <div class="flex items-start gap-2 text-[11px]">
              <span class="rounded px-1.5 py-0.5 font-mono ${actionTone(a.status)}">${escapeHtml(a.kind)}</span>
              <span class="text-slate-400">${escapeHtml(a.detail || a.error || '')}</span>
            </div>`).join('')}
        </div>
      </div>` : '';

    runOutput.innerHTML = `
      <div class="rounded-xl border border-${tone}-500/30 bg-${tone}-500/10 p-4">
        <p class="text-xs font-semibold text-${tone}-200">
          Classified as <span class="font-mono">${escapeHtml(evaluation ? evaluation.top_choice ?? '—' : '—')}</span>
          at ${pct(evaluation ? evaluation.confidence : null)}
        </p>
        <p class="mt-1 text-[11px] text-${tone}-200/80">
          route <span class="font-mono">${escapeHtml(route)}</span>
          ${effective !== route ? `→ escalated to <span class="font-mono">${escapeHtml(effective)}</span>` : ''}
        </p>
      </div>
      ${verificationBlock}
      ${actions}
      ${payload.error ? `<p class="mt-3 text-[11px] text-amber-300">${escapeHtml(payload.error)}</p>` : ''}`;
  }

  function renderHistoryRun(run) {
    const actions = run.actions || [];
    runDetail.innerHTML = `
      <div class="bg-slate-950/40 px-5 py-4">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="text-xs font-semibold text-slate-200">Run #${run.id}</p>
            <p class="mt-0.5 text-[11px] text-slate-500">
              ${escapeHtml(run.status)} · route <span class="font-mono">${escapeHtml(run.route || '—')}</span>
              · ${pct(run.confidence)} · ${num(actions.length, 0)} action(s)
            </p>
          </div>
          ${run.evaluation_id ? `<span class="font-mono text-[11px] text-slate-500">evaluation #${run.evaluation_id}</span>` : ''}
        </div>
        ${run.error ? `<p class="mt-2 text-[11px] text-rose-300">${escapeHtml(run.error)}</p>` : ''}
        <div class="mt-3 space-y-1.5">
          ${actions.map((a) => `
            <div class="flex items-start gap-2 rounded-lg bg-slate-900/60 px-3 py-2 text-[11px]">
              <span class="rounded px-1.5 py-0.5 font-mono ${actionTone(a.status)}">
                ${escapeHtml(a.kind)}
              </span>
              <span class="min-w-0 flex-1 text-slate-400">${escapeHtml(a.detail || a.error || '')}</span>
            </div>`).join('') || '<p class="text-[11px] text-slate-500">No actions recorded.</p>'}
        </div>
        <div class="mt-3 flex flex-wrap gap-2">
          <a href="/review?run=${run.id}" class="rounded-lg border border-slate-800 px-2.5 py-1 text-[11px] text-slate-400 hover:text-slate-200">Open in review queue</a>
        </div>
      </div>`;
  }

  /** Wire the run rows: the server-rendered ones at startup, and every refresh. */
  function wireRunRows() {
    runsList.querySelectorAll('[data-run-id]').forEach((node) => {
      node.addEventListener('click', () => openRun(Number(node.dataset.runId)));
    });
  }

  async function refreshRuns() {
    try {
      const rows = await api('/api/workflows/runs?limit=25');
      runsList.innerHTML = rows.length
        ? rows.map((run) => `
          <button type="button" data-run-id="${run.id}" class="flex w-full items-center gap-3 px-5 py-3 text-left hover:bg-slate-800/40">
            <span class="min-w-0 flex-1">
              <span class="block truncate font-mono text-[11px] text-slate-500">run #${run.id} · workflow ${run.workflow_id}</span>
              <span class="mt-0.5 block text-xs text-slate-300">${escapeHtml(run.final_choice ?? '—')} · ${pct(run.confidence)}</span>
            </span>
            <span class="shrink-0 rounded-full px-2 py-0.5 text-[11px] ${run.route === 'auto' ? 'bg-emerald-500/15 text-emerald-300' : run.route === 'verify' ? 'bg-amber-500/15 text-amber-300' : run.route === 'review' ? 'bg-rose-500/15 text-rose-300' : 'bg-slate-700/40 text-slate-400'}">
              ${escapeHtml(run.route || '—')}
            </span>
          </button>`).join('')
        : '<p class="px-5 py-6 text-center text-xs text-slate-500">No runs yet.</p>';

      wireRunRows();
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  async function openRun(runId) {
    try {
      renderHistoryRun(await api(`/api/workflows/runs/${runId}`));
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  function scriptLine(workflow) {
    const counts = (workflow.steps || []).reduce((acc, step) => {
      acc[step.on] = (acc[step.on] || 0) + 1;
      return acc;
    }, {});
    const mix = ['auto', 'verify', 'review', 'always']
      .filter((route) => counts[route])
      .map((route) => `${counts[route]} ${route}`)
      .join(' · ');
    return `${workflow.trigger_type} → ${mix || 'no blocks'}`;
  }

  /**
   * Wire the rows the server rendered at first paint.
   *
   * The shell ships the workflow list, so this has to run on startup as well as
   * after a refresh — otherwise clicking a row does nothing until something else
   * happens to re-render the list.
   */
  function wireWorkflowRows() {
    list.querySelectorAll('[data-workflow-id]').forEach((node) => {
      node.addEventListener('click', () => open(Number(node.dataset.workflowId)));
    });
    list.querySelectorAll('[data-toggle-workflow]').forEach((node) => {
      node.addEventListener('click', async () => {
        try {
          await api(`/api/workflows/${node.dataset.toggleWorkflow}`, {
            method: 'PUT',
            body: JSON.stringify({ enabled: node.dataset.enabled !== 'true' }),
          });
          refreshWorkflows();
        } catch (error) {
          toast(error.message, 'error');
        }
      });
    });
    list.querySelectorAll('[data-run-workflow]').forEach((node) => {
      node.addEventListener('click', () => {
        // A programmatic value set fires no ``change``, so refresh the URL here.
        runSelect.value = node.dataset.runWorkflow;
        refreshIngestUrl();
        state.elements.textarea.focus();
      });
    });
  }

  async function refreshWorkflows() {
    try {
      const rows = await api('/api/workflows');
      list.innerHTML = rows.length
        ? rows.map((w) => `
          <div class="flex items-center gap-3 px-5 py-3">
            <button type="button" data-workflow-id="${w.id}" class="min-w-0 flex-1 text-left">
              <span class="block truncate text-xs text-slate-300">${escapeHtml(w.name)}</span>
              <span class="mt-0.5 block font-mono text-[11px] text-slate-500">${escapeHtml(w.key)} · ${escapeHtml(scriptLine(w))}</span>
            </button>
            <button type="button" data-toggle-workflow="${w.id}" data-enabled="${w.enabled}"
              class="rounded-lg border border-slate-800 px-2 py-1 text-[11px] text-slate-400 hover:text-slate-200">
              ${w.enabled ? 'disable' : 'enable'}
            </button>
            <button type="button" data-run-workflow="${w.id}"
              class="rounded-lg bg-slate-800 px-2 py-1 text-[11px] text-slate-200 hover:bg-slate-700">run</button>
          </div>`).join('')
        : '<p class="px-5 py-6 text-center text-xs text-slate-500">No workflows yet.</p>';

      wireWorkflowRows();
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const name = els.name.value.trim();
    if (!name) { toast('Give the workflow a name', 'warn'); return; }
    if (!els.schema.value) { toast('Pick a schema', 'warn'); return; }

    const steps = blocks.steps();
    if (!steps.length) { toast('Add at least one block to the script', 'warn'); return; }

    const payload = {
      name,
      description: els.description.value.trim(),
      schema_id: Number(els.schema.value),
      trigger_type: els.trigger.value,
      steps,
      decision_question: els.question.value || null,
      auto_threshold: Number(els.auto.value),
      verify_threshold: Number(els.verify.value),
    };

    try {
      if (editing) {
        await api(`/api/workflows/${editing.id}`, { method: 'PUT', body: JSON.stringify(payload) });
        toast(`Saved ${name}`, 'success');
      } else {
        const created = await api('/api/workflows', { method: 'POST', body: JSON.stringify(payload) });
        toast(`Created ${created.name}`, 'success');
        editing = created;
      }
      refreshWorkflows();
    } catch (error) {
      toast(error.message, 'error', 6000);
    }
  });

  /** The state box is a string in free-text mode and an object in fields mode. */
  function statePayload() {
    const value = state.value();
    if (!value) return null;
    return typeof value === 'string' ? { text: value } : { state: value };
  }

  document.getElementById('run-live').addEventListener('click', async () => {
    const workflowId = runSelect.value;
    if (!workflowId) { toast('Create a workflow first', 'warn'); return; }
    const payload = statePayload();
    if (!payload) { toast('Give the run a state', 'warn'); return; }

    try {
      const result = await api(`/api/workflows/${workflowId}/run`, {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      renderRun(result, result.evaluation);
      refreshRuns();
    } catch (error) {
      toast(error.message, 'error');
    }
  });

  document.getElementById('run-dry').addEventListener('click', async () => {
    const workflowId = runSelect.value;
    const payload = statePayload();
    if (!workflowId || !payload) { toast('Pick a workflow and give it a state', 'warn'); return; }

    try {
      const workflow = await api(`/api/workflows/${workflowId}`);
      const result = await api(`/api/ingest/${workflow.key}`, {
        method: 'POST',
        body: JSON.stringify({ ...payload, dry_run: true }),
      });
      renderRun(result, result.evaluation);
    } catch (error) {
      toast(error.message, 'error');
    }
  });

  els.schema.addEventListener('change', loadQuestions);
  ['input', 'change'].forEach((type) => {
    els.auto.addEventListener(type, () => blocks.setHints(hints()));
    els.verify.addEventListener(type, () => blocks.setHints(hints()));
  });
  document.getElementById('copy-ingest').addEventListener('click', () => {
    copy(document.getElementById('ingest-url').textContent);
  });
  runSelect.addEventListener('change', refreshIngestUrl);
  document.getElementById('wf-reset').addEventListener('click', newWorkflow);

  wireRunRows();
  wireWorkflowRows();

  newWorkflow();
  refreshIngestUrl();
})();
