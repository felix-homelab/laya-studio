/* A/B testing: revision pickers, the dataset row builder, delta rendering. */

(() => {
  const { api, toast, escapeHtml, pct, num, icon, summarizeState } = window.LayaStudio;

  const form = document.getElementById('ab-form');
  const runBtn = document.getElementById('ab-run');
  const detail = document.getElementById('ab-detail');
  const list = document.getElementById('ab-list');
  const schemaSelect = document.getElementById('ab-schema');
  const revA = document.getElementById('ab-rev-a');
  const revB = document.getElementById('ab-rev-b');
  const rowsHost = document.getElementById('ab-dataset-rows');
  const rowCount = document.getElementById('ab-dataset-count');

  let datasetMode = 'job';
  let dataset = [];
  let rowRefs = [];

  /* ------------------------------------------------------------- the rows */

  function collectRows() {
    dataset = rowRefs.map((ref) => {
      const row = {};
      const value = ref.editor.value();
      if (value !== null && value !== undefined && value !== '') row.state = value;
      const label = ref.label.value.trim();
      if (label) row.label = label;
      return row;
    });
  }

  function updateCount() {
    const ready = rowRefs.filter((ref) => ref.editor.value()).length;
    rowCount.textContent = `${ready} of ${rowRefs.length} row${rowRefs.length === 1 ? '' : 's'} ready`;
  }

  function buildRow(row, index) {
    const card = document.createElement('div');
    card.className = 'rounded-lg border border-slate-800 bg-slate-950/60 p-2.5';
    card.innerHTML = `
      <div class="flex items-center justify-between gap-2">
        <span class="text-[10px] font-semibold uppercase tracking-wider text-slate-600">row ${index + 1}</span>
        <button type="button" data-remove aria-label="Remove row ${index + 1}" title="Remove this row"
          class="grid h-6 w-6 place-items-center rounded-md border border-slate-800 text-slate-500 hover:text-rose-300">
          ${icon('trash', 'h-3 w-3')}
        </button>
      </div>
      <div data-state class="mt-1.5"></div>
      <div class="mt-2">
        <span class="laya-label">Expected answer (optional)</span>
        <input data-label list="ab-labels" placeholder="the label the answers should land on"
          class="mt-1 w-full rounded-lg border border-slate-800 bg-slate-950 px-2.5 py-1.5 text-xs text-slate-200 outline-none placeholder:text-slate-600 focus:border-sky-600">
      </div>`;

    const editor = window.LayaStudio.stateEditor(card.querySelector('[data-state]'), {
      rows: 2,
      placeholder: 'we were billed twice for March',
    });
    if (row.state !== undefined) editor.setValue(row.state);

    const label = card.querySelector('[data-label]');
    label.value = row.label || '';
    label.addEventListener('input', () => { collectRows(); updateCount(); });

    card.querySelector('[data-remove]').addEventListener('click', () => {
      collectRows();
      dataset.splice(index, 1);
      renderRows();
    });

    rowRefs.push({ editor, label });
    rowsHost.appendChild(card);
  }

  function renderRows(focusLast = false) {
    rowRefs = [];
    rowsHost.innerHTML = '';
    if (!dataset.length) dataset = [{}];
    dataset.forEach(buildRow);
    updateCount();
    if (focusLast) {
      const last = rowRefs[rowRefs.length - 1];
      if (last) last.editor.elements.textarea.focus();
    }
  }

  /* --------------------------------------------------------- paste import */

  function splitCsv(line) {
    const cells = [];
    let current = '';
    let quoted = false;
    Array.from(line).forEach((character) => {
      if (character === '"') { quoted = !quoted; return; }
      if (character === ',' && !quoted) { cells.push(current.trim()); current = ''; return; }
      current += character;
    });
    cells.push(current.trim());
    return cells;
  }

  function parsePasted(raw) {
    const text = raw.trim();
    if (!text) return [];

    if (text.startsWith('[') || text.startsWith('{')) {
      const parsed = JSON.parse(text);
      return (Array.isArray(parsed) ? parsed : [parsed]).map((entry) => {
        if (entry === null || typeof entry !== 'object') return { state: String(entry) };
        if (entry.state !== undefined) return { state: entry.state, label: entry.label };
        const rest = { ...entry };
        const label = rest.label;
        delete rest.label;
        const keys = Object.keys(rest);
        return { state: keys.length === 1 ? rest[keys[0]] : rest, label };
      });
    }

    const lines = text.split(/\r?\n/).map((line) => line.trimEnd()).filter((line) => line.trim() && !line.trim().startsWith('#'));
    if (!lines.length) return [];

    const header = splitCsv(lines[0]).map((cell) => cell.toLowerCase());
    const textAt = header.findIndex((cell) => cell === 'text' || cell === 'state');
    const labelAt = header.findIndex((cell) => cell === 'label');

    if (textAt >= 0 || labelAt >= 0) {
      return lines.slice(1).map((line) => {
        const cells = splitCsv(line);
        const row = {};
        if (textAt >= 0 && cells[textAt]) row.state = cells[textAt];
        if (labelAt >= 0 && cells[labelAt]) row.label = cells[labelAt];
        return row;
      }).filter((row) => Object.keys(row).length);
    }

    return lines.map((line) => ({ state: line.trim() }));
  }

  document.getElementById('ab-paste-add').addEventListener('click', () => {
    const paste = document.getElementById('ab-paste');
    collectRows();
    try {
      const imported = parsePasted(paste.value);
      if (!imported.length) { toast('Nothing to import', 'warn'); return; }
      dataset = dataset.filter((row) => row.state !== undefined);
      dataset.push(...imported);
      renderRows();
      paste.value = '';
      toast(`Imported ${imported.length} row${imported.length === 1 ? '' : 's'}`, 'success');
    } catch (error) {
      toast(`Could not read that: ${error.message}`, 'error');
    }
  });

  document.getElementById('ab-add-row').addEventListener('click', () => {
    collectRows();
    dataset = dataset.filter((row) => row.state !== undefined);
    dataset.push({});
    renderRows(true);
  });

  function setDatasetMode(mode) {
    datasetMode = mode;
    document.querySelectorAll('[data-dataset-mode]').forEach((other) => {
      const on = other.dataset.datasetMode === mode;
      other.className = `flex-1 rounded-md px-2 py-1 text-[11px] font-medium ${on ? 'bg-slate-800 text-slate-200' : 'text-slate-400 hover:text-slate-200'}`;
      other.setAttribute('aria-pressed', String(on));
    });
    document.getElementById('dataset-job').classList.toggle('hidden', mode !== 'job');
    document.getElementById('dataset-rows').classList.toggle('hidden', mode !== 'rows');
  }

  document.querySelectorAll('[data-dataset-mode]').forEach((button) => {
    button.addEventListener('click', () => setDatasetMode(button.dataset.datasetMode));
  });

  /* -------------------------------------------------------------- schema */

  /** Offer the schema's own answer labels as suggestions for the label column. */
  async function loadLabels() {
    const schemaId = schemaSelect.value;
    if (!schemaId) return;
    try {
      const schema = await api(`/api/schemas/${schemaId}`);
      const labels = [];
      Object.values(schema.definition.questions || {}).forEach((definition) => {
        const criteria = definition && definition.criteria;
        if (!criteria) return;
        const values = Array.isArray(criteria) ? criteria : Object.keys(criteria);
        values.forEach((value) => {
          const label = String(value);
          if (label && !labels.includes(label)) labels.push(label);
        });
      });
      document.getElementById('ab-labels').innerHTML = labels
        .map((label) => `<option value="${escapeHtml(label)}"></option>`).join('');
    } catch { /* suggestions are a bonus, not a requirement */ }
  }

  async function loadRevisions() {
    const schemaId = schemaSelect.value;
    if (!schemaId) return;
    try {
      const revisions = await api(`/api/schemas/${schemaId}/revisions`);
      const options = revisions
        .map((r) => `<option value="${r.id}">r${r.revision} · ${escapeHtml(r.note || '')}</option>`)
        .join('');
      revA.innerHTML = options;
      revB.innerHTML = options;
      if (revisions.length > 1) {
        revA.value = String(revisions[1].id);
        revB.value = String(revisions[0].id);
      }
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  async function loadJobs() {
    try {
      const jobs = await api('/api/bulk/jobs?limit=30');
      document.getElementById('ab-job').innerHTML = jobs.length
        ? '<option value="">Select a bulk job…</option>' + jobs
          .map((j) => `<option value="${j.id}">${escapeHtml(j.name)} · ${j.processed_rows} rows</option>`)
          .join('')
        : '<option value="">No bulk jobs yet</option>';
      // With nothing to reuse, start the user on the row builder.
      if (!jobs.length) setDatasetMode('rows');
    } catch {
      /* a missing job list is not worth interrupting the user for */
      setDatasetMode('rows');
    }
  }

  /* ------------------------------------------------------------ rendering */

  function metric(label, value, hint = '', tone = 'text-slate-100') {
    return `
      <div class="rounded-xl border border-slate-800 bg-slate-950/50 px-3.5 py-2.5">
        <p class="text-[11px] font-medium uppercase tracking-wider text-slate-500">${label}</p>
        <p class="mt-0.5 text-xl font-semibold ${tone}">${value}</p>
        ${hint ? `<p class="mt-0.5 text-[11px] text-slate-500">${hint}</p>` : ''}
      </div>`;
  }

  function render(test) {
    const s = test.summary || {};
    const items = test.items || [];

    const verdictTone = s.accuracy_delta == null
      ? 'text-slate-100'
      : s.accuracy_delta > 0 ? 'text-emerald-300' : s.accuracy_delta < 0 ? 'text-rose-300' : 'text-slate-100';

    const metrics = `
      <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        ${metric('Flip rate', s.flip_rate != null ? pct(s.flip_rate) : '—',
          `${s.flip_count ?? 0} of ${s.total ?? 0} rows changed answer`,
          s.flip_rate > 0.3 ? 'text-amber-300' : 'text-slate-100')}
        ${metric('Agreement', s.agreement != null ? pct(s.agreement) : '—', 'identical answers across revisions')}
        ${metric('Accuracy Δ (B − A)', s.accuracy_delta != null
            ? (s.accuracy_delta >= 0 ? '+' : '') + pct(s.accuracy_delta)
            : '—',
          s.labelled ? `${s.labelled} labelled rows` : 'needs a label column',
          verdictTone)}
        ${metric('Confidence Δ', s.mean_confidence_delta != null
            ? (s.mean_confidence_delta >= 0 ? '+' : '') + num(s.mean_confidence_delta, 4)
            : '—',
          `A ${pct(s.mean_confidence_a)} · B ${pct(s.mean_confidence_b)}`)}
      </div>`;

    const gain = (s.fixed_by_b != null) ? `
      <div class="mt-4 grid gap-3 sm:grid-cols-3">
        ${metric('Fixed by B', s.fixed_by_b ?? 0, 'A wrong, B right', 'text-emerald-300')}
        ${metric('Broken by B', s.broken_by_b ?? 0, 'A right, B wrong', 'text-rose-300')}
        ${metric('Net gain', (s.net_gain > 0 ? '+' : '') + (s.net_gain ?? 0), 'fixed minus broken',
          s.net_gain > 0 ? 'text-emerald-300' : s.net_gain < 0 ? 'text-rose-300' : 'text-slate-100')}
      </div>` : '';

    const interpretation = s.interpretation ? `
      <p class="mt-4 rounded-xl border border-slate-700 bg-slate-800/40 px-4 py-3 text-xs text-slate-300">
        ${escapeHtml(s.interpretation)}
      </p>` : '';

    const transitions = (s.transitions || []).length ? `
      <div class="mt-4 rounded-xl border border-slate-800 bg-slate-950/50 p-4">
        <p class="text-xs font-medium text-slate-300">Where the answers moved</p>
        <div class="mt-2.5 space-y-2">
          ${s.transitions.map((t) => `
            <div class="flex items-center gap-2 text-[11px]">
              <span class="rounded bg-slate-800 px-1.5 py-0.5 font-mono text-slate-300">${escapeHtml(t.from)}</span>
              <span class="text-slate-600">→</span>
              <span class="rounded bg-sky-500/15 px-1.5 py-0.5 font-mono text-sky-300">${escapeHtml(t.to)}</span>
              <span class="ml-auto font-mono text-slate-500">${t.count}</span>
            </div>`).join('')}
        </div>
      </div>` : '';

    const table = items.length ? `
      <div class="mt-4 overflow-hidden rounded-xl border border-slate-800">
        <table class="w-full text-left text-xs">
          <thead class="bg-slate-950/60 text-[11px] uppercase tracking-wider text-slate-500">
            <tr>
              <th class="px-3 py-2 font-medium">#</th>
              <th class="px-3 py-2 font-medium">State</th>
              <th class="px-3 py-2 font-medium">A</th>
              <th class="px-3 py-2 font-medium">B</th>
              <th class="px-3 py-2 font-medium">Label</th>
              <th class="px-3 py-2 font-medium text-right">Δ conf.</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-800">
            ${items.map((item) => {
              const delta = item.confidence_a != null && item.confidence_b != null
                ? item.confidence_b - item.confidence_a : null;
              const stateText = summarizeState(item.state, 120);
              return `
                <tr class="${item.flipped ? 'bg-amber-500/5' : ''}">
                  <td class="px-3 py-2 font-mono text-slate-500">${item.row_index}</td>
                  <td class="max-w-[300px] px-3 py-2 text-slate-400"><span class="line-clamp-2">${escapeHtml(stateText)}</span></td>
                  <td class="px-3 py-2 text-slate-300">${escapeHtml(item.choice_a ?? '—')}</td>
                  <td class="px-3 py-2 ${item.flipped ? 'font-semibold text-amber-300' : 'text-slate-300'}">${escapeHtml(item.choice_b ?? '—')}</td>
                  <td class="px-3 py-2 text-slate-500">${escapeHtml(item.label ?? '—')}</td>
                  <td class="px-3 py-2 text-right font-mono ${delta == null ? 'text-slate-600' : delta > 0 ? 'text-emerald-300' : delta < 0 ? 'text-rose-300' : 'text-slate-400'}">
                    ${delta == null ? '—' : (delta >= 0 ? '+' : '') + num(delta, 3)}
                  </td>
                </tr>`;
            }).join('')}
          </tbody>
        </table>
      </div>` : '';

    detail.innerHTML = `
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 class="text-sm font-semibold text-slate-100">${escapeHtml(test.name)}</h2>
          <p class="mt-0.5 text-xs text-slate-400">
            r${s.revision_a ?? '?'} vs r${s.revision_b ?? '?'} · schema #${test.schema_id ?? '—'} · ${escapeHtml(test.status)}
          </p>
        </div>
        <button type="button" id="ab-rerun"
          class="rounded-lg border border-slate-800 px-2.5 py-1.5 text-xs text-slate-400 hover:text-slate-200">Rerun</button>
      </div>
      <div class="mt-4">${metrics}</div>
      ${gain}
      ${interpretation}
      ${transitions}
      ${table}`;

    const rerun = document.getElementById('ab-rerun');
    if (rerun) {
      rerun.addEventListener('click', async () => {
        try {
          const updated = await api(`/api/ab/tests/${test.id}/run`, { method: 'POST' });
          toast('Rerun complete', 'success');
          render(updated);
          refreshList();
        } catch (error) {
          toast(error.message, 'error');
        }
      });
    }
  }

  async function open(testId) {
    try {
      render(await api(`/api/ab/tests/${testId}`));
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  async function refreshList() {
    try {
      const rows = await api('/api/ab/tests?limit=30');
      list.innerHTML = rows.length
        ? rows.map((test) => `
          <button type="button" data-test-id="${test.id}" class="flex w-full items-center gap-3 px-5 py-3 text-left hover:bg-slate-800/40">
            <span class="min-w-0 flex-1">
              <span class="block truncate text-xs text-slate-300">${escapeHtml(test.name)}</span>
              <span class="mt-0.5 block text-[11px] text-slate-500">
                ${test.summary && test.summary.flip_rate != null ? pct(test.summary.flip_rate, 0) + ' flip rate · ' + test.summary.total + ' rows' : 'not run'}
              </span>
            </span>
            <span class="shrink-0 rounded-full bg-slate-700/40 px-2 py-0.5 text-[11px] text-slate-300">${escapeHtml(test.status)}</span>
          </button>`).join('')
        : '<p class="px-5 py-6 text-center text-xs text-slate-500">No tests yet.</p>';

      list.querySelectorAll('[data-test-id]').forEach((node) => {
        node.addEventListener('click', () => open(Number(node.dataset.testId)));
      });
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();

    if (revA.value === revB.value) { toast('Pick two different revisions', 'warn'); return; }

    const payload = {
      name: document.getElementById('ab-name').value.trim() || 'A/B comparison',
      schema_id: Number(schemaSelect.value),
      revision_a_id: Number(revA.value),
      revision_b_id: Number(revB.value),
    };

    if (datasetMode === 'rows') {
      collectRows();
      const rows = dataset.filter((row) => row.state !== undefined && row.state !== null && row.state !== '');
      if (!rows.length) { toast('Build at least one row with a state', 'warn'); return; }
      payload.dataset = rows;
    } else {
      const jobId = document.getElementById('ab-job').value;
      if (!jobId) { toast('Select a bulk job, or switch to building rows', 'warn'); return; }
      payload.source_job_id = Number(jobId);
    }

    runBtn.disabled = true;
    const original = runBtn.innerHTML;
    runBtn.textContent = 'Comparing…';

    try {
      const test = await api('/api/ab/tests', { method: 'POST', body: JSON.stringify(payload) });
      toast('Comparison complete', 'success');
      render(test);
      refreshList();
    } catch (error) {
      toast(error.message, 'error', 7000);
    } finally {
      runBtn.disabled = false;
      runBtn.innerHTML = original;
    }
  });

  schemaSelect.addEventListener('change', () => { loadRevisions(); loadLabels(); });
  list.querySelectorAll('[data-test-id]').forEach((node) => {
    node.addEventListener('click', () => open(Number(node.dataset.testId)));
  });

  renderRows();
  loadRevisions();
  loadLabels();
  loadJobs();
})();
