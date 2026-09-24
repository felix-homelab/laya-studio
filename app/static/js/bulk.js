/* Bulk testing: upload, job metrics, row preview. */

(() => {
  const { api, toast, escapeHtml, pct, num, barTone, summarizeState } = window.LayaStudio;

  const form = document.getElementById('upload-form');
  const uploadBtn = document.getElementById('upload-btn');
  const jobList = document.getElementById('job-list');
  const detail = document.getElementById('job-detail');
  const fileInput = document.getElementById('file-input');
  const stateColumn = document.getElementById('state-column');
  const labelColumn = document.getElementById('label-column');

  /* ------------------------------------------------------------------------
   * The column fields are pickers, not free text: as soon as a file is chosen
   * we read its header (CSV) or the keys of its first object (JSON) and offer
   * them in a dropdown. They stay typable, so a column you know is there still
   * works before the file is read.
   * ---------------------------------------------------------------------- */

  const columnList = document.createElement('datalist');
  columnList.id = 'bulk-columns';
  document.body.appendChild(columnList);

  function offerColumns(columns) {
    columnList.innerHTML = columns.map((name) => `<option value="${escapeHtml(name)}"></option>`).join('');
    [stateColumn, labelColumn].forEach((input) => {
      if (input.getAttribute('list')) return;
      input.setAttribute('list', 'bulk-columns');
      window.LayaStudio.enhanceSelects(input);
    });
  }

  function columnsOf(text, filename) {
    const trimmed = text.trim();
    if (!trimmed) return [];
    const looksJson = /\.json$/i.test(filename || '') || trimmed.startsWith('{') || trimmed.startsWith('[');
    if (looksJson) {
      try {
        const parsed = JSON.parse(trimmed);
        const first = Array.isArray(parsed) ? parsed[0] : parsed;
        return first && typeof first === 'object' && !Array.isArray(first) ? Object.keys(first) : [];
      } catch { /* not JSON after all — try the CSV header below */ }
    }
    return trimmed.split(/\r?\n/, 1)[0]
      .split(',')
      .map((cell) => cell.trim().replace(/^"|"$/g, ''))
      .filter(Boolean);
  }

  fileInput.addEventListener('change', async () => {
    const file = fileInput.files && fileInput.files[0];
    if (!file) return;
    try {
      const columns = columnsOf(await file.text(), file.name);
      offerColumns(columns);
      if (columns.length) toast(`${columns.length} column(s) in ${file.name}`, 'info', 2400);
    } catch { /* an unreadable preview must never block the upload */ }
  });

  function metricCard(label, value, hint = '') {
    return `
      <div class="rounded-xl border border-slate-800 bg-slate-950/50 px-3.5 py-2.5">
        <p class="text-[11px] font-medium uppercase tracking-wider text-slate-500">${label}</p>
        <p class="mt-0.5 text-xl font-semibold text-slate-100">${value}</p>
        ${hint ? `<p class="mt-0.5 text-[11px] text-slate-500">${hint}</p>` : ''}
      </div>`;
  }

  function renderJob(job) {
    const summary = job.summary || {};
    const items = job.items || [];

    const metrics = `
      <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        ${metricCard('Rows', `${summary.processed ?? 0}/${summary.total ?? job.total_rows}`,
          summary.failed ? `${summary.failed} failed` : 'all scored')}
        ${metricCard('Accuracy', summary.accuracy != null ? pct(summary.accuracy) : '—',
          summary.labelled ? `${summary.labelled} labelled rows` : 'no label column')}
        ${metricCard('Calibration (ECE)', summary.ece != null ? num(summary.ece, 3) : '—',
          summary.ece != null ? (summary.ece < 0.05 ? 'well calibrated' : summary.ece < 0.15 ? 'moderate' : 'refit needed') : 'needs labels')}
        ${metricCard('Mean confidence', summary.mean_confidence != null ? pct(summary.mean_confidence) : '—',
          summary.brier != null ? `Brier ${num(summary.brier, 3)}` : '')}
      </div>`;

    const bandCounts = summary.band_counts;
    const bandBlock = bandCounts ? `
      <div class="mt-4 rounded-xl border border-slate-800 bg-slate-950/50 p-4">
        <p class="text-xs font-medium text-slate-300">How the hybrid ladder would have routed these rows</p>
        <div class="mt-2.5 grid gap-2 sm:grid-cols-3">
          ${[['auto', 'Auto-execute'], ['verify', 'Verify / LLM'], ['review', 'Human review']].map(([key, label]) => {
            const count = bandCounts[key] || 0;
            const total = summary.labelled || 1;
            return `
              <div>
                <div class="flex items-baseline justify-between text-[11px]">
                  <span class="text-slate-400">${label}</span>
                  <span class="font-mono text-slate-300">${count} · ${pct(count / total)}</span>
                </div>
                <div class="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-slate-800">
                  <span class="block h-full rounded-full ${barTone(key === 'review' ? 'escalate' : key)}"
                        style="width: ${(count / total) * 100}%"></span>
                </div>
              </div>`;
          }).join('')}
        </div>
      </div>` : '';

    const distribution = summary.prediction_distribution || {};
    const distTotal = Object.values(distribution).reduce((a, b) => a + b, 0) || 1;
    const distBlock = Object.keys(distribution).length ? `
      <div class="mt-4 rounded-xl border border-slate-800 bg-slate-950/50 p-4">
        <p class="text-xs font-medium text-slate-300">Prediction distribution</p>
        <div class="mt-2.5 space-y-2">
          ${Object.entries(distribution).sort((a, b) => b[1] - a[1]).map(([label, count]) => `
            <div>
              <div class="flex items-baseline justify-between text-[11px]">
                <span class="text-slate-400">${escapeHtml(label)}</span>
                <span class="font-mono text-slate-300">${count}</span>
              </div>
              <div class="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-slate-800">
                <span class="block h-full rounded-full bg-sky-500" style="width: ${(count / distTotal) * 100}%"></span>
              </div>
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
              <th class="px-3 py-2 font-medium">Predicted</th>
              <th class="px-3 py-2 font-medium">Label</th>
              <th class="px-3 py-2 font-medium text-right">Conf.</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-800">
            ${items.map((item) => {
              const correct = item.label && item.predicted
                && item.label.toLowerCase() === item.predicted.toLowerCase();
              const stateText = summarizeState(item.state, 140);
              return `
                <tr class="${item.ok ? '' : 'bg-rose-500/5'}">
                  <td class="px-3 py-2 font-mono text-slate-500">${item.row_index}</td>
                  <td class="max-w-[380px] px-3 py-2 text-slate-400"><span class="line-clamp-2">${escapeHtml(stateText)}</span></td>
                  <td class="px-3 py-2 text-slate-200">${escapeHtml(item.predicted ?? '—')}</td>
                  <td class="px-3 py-2 ${item.label ? (correct ? 'text-emerald-300' : 'text-rose-300') : 'text-slate-600'}">
                    ${escapeHtml(item.label ?? '—')}
                  </td>
                  <td class="px-3 py-2 text-right font-mono text-slate-400">${pct(item.confidence)}</td>
                </tr>`;
            }).join('')}
          </tbody>
        </table>
      </div>` : '';

    const errors = (summary.errors || []).filter(Boolean);
    const errorBlock = errors.length ? `
      <div class="mt-4 space-y-1.5">
        ${errors.map((e) => `<p class="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-[11px] text-rose-200">${escapeHtml(e)}</p>`).join('')}
      </div>` : '';

    const calibrationNote = summary.calibration_note
      ? `<p class="mt-3 text-[11px] text-slate-500">${escapeHtml(summary.calibration_note)}</p>` : '';

    detail.innerHTML = `
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 class="text-sm font-semibold text-slate-100">${escapeHtml(job.name)}</h2>
          <p class="mt-0.5 text-xs text-slate-400">
            ${escapeHtml(job.filename || 'pasted data')} · schema #${job.schema_id ?? '—'} ·
            ${escapeHtml(job.status)}
          </p>
        </div>
        <div class="flex gap-2">
          <button type="button" id="rerun-job"
            class="flex items-center gap-1.5 rounded-lg border border-slate-800 px-2.5 py-1.5 text-xs text-slate-400 hover:text-slate-200">Rerun</button>
          <button type="button" id="delete-job"
            class="flex items-center gap-1.5 rounded-lg border border-rose-500/40 px-2.5 py-1.5 text-xs text-rose-300 hover:bg-rose-500/10">Delete</button>
        </div>
      </div>
      <div class="mt-4">${metrics}</div>
      ${bandBlock}
      ${distBlock}
      ${calibrationNote}
      ${errorBlock}
      ${table}`;

    document.getElementById('rerun-job').addEventListener('click', async () => {
      try {
        const updated = await api(`/api/bulk/jobs/${job.id}/rerun`, { method: 'POST' });
        toast('Rerun complete', 'success');
        renderJob(updated);
        refreshList();
      } catch (error) {
        toast(error.message, 'error');
      }
    });

    document.getElementById('delete-job').addEventListener('click', async () => {
      if (!window.confirm('Delete this job and its rows?')) return;
      try {
        await api(`/api/bulk/jobs/${job.id}`, { method: 'DELETE' });
        toast('Job deleted', 'success');
        detail.innerHTML = '<div class="rounded-xl border border-dashed border-slate-800 px-4 py-10 text-center text-xs text-slate-500">Job deleted.</div>';
        refreshList();
      } catch (error) {
        toast(error.message, 'error');
      }
    });
  }

  async function openJob(jobId) {
    try {
      const job = await api(`/api/bulk/jobs/${jobId}?item_limit=200`);
      renderJob(job);
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  async function refreshList() {
    try {
      const rows = await api('/api/bulk/jobs?limit=30');
      jobList.innerHTML = rows.length
        ? rows.map((job) => `
          <button type="button" data-job-id="${job.id}" class="flex w-full items-center gap-3 px-5 py-3 text-left hover:bg-slate-800/40">
            <span class="min-w-0 flex-1">
              <span class="block truncate text-xs text-slate-300">${escapeHtml(job.name)}</span>
              <span class="mt-0.5 block text-[11px] text-slate-500">
                ${job.processed_rows}/${job.total_rows} rows ·
                ${job.summary && job.summary.accuracy != null ? pct(job.summary.accuracy) + ' accuracy' : 'no labels'}
              </span>
            </span>
            <span class="shrink-0 rounded-full px-2 py-0.5 text-[11px] ${job.status === 'failed' ? 'bg-rose-500/15 text-rose-300' : 'bg-emerald-500/15 text-emerald-300'}">${escapeHtml(job.status)}</span>
          </button>`).join('')
        : '<p class="px-5 py-6 text-center text-xs text-slate-500">No jobs yet.</p>';

      jobList.querySelectorAll('[data-job-id]').forEach((node) => {
        node.addEventListener('click', () => openJob(Number(node.dataset.jobId)));
      });
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const file = document.getElementById('file-input').files[0];
    if (!file) { toast('Choose a CSV or JSON file', 'warn'); return; }

    const body = new FormData();
    body.append('file', file);
    body.append('schema_id', document.getElementById('schema-select').value);
    const stateColumn = document.getElementById('state-column').value.trim();
    const labelColumn = document.getElementById('label-column').value.trim();
    const name = document.getElementById('job-name').value.trim();
    if (stateColumn) body.append('state_column', stateColumn);
    if (labelColumn) body.append('label_column', labelColumn);
    if (name) body.append('name', name);

    uploadBtn.disabled = true;
    const original = uploadBtn.innerHTML;
    uploadBtn.textContent = 'Scoring rows…';

    try {
      const job = await api('/api/bulk/upload', { method: 'POST', body });
      toast(`Scored ${job.processed_rows} rows`, 'success');
      renderJob(job);
      refreshList();
    } catch (error) {
      toast(error.message, 'error', 7000);
    } finally {
      uploadBtn.disabled = false;
      uploadBtn.innerHTML = original;
    }
  });

  jobList.querySelectorAll('[data-job-id]').forEach((node) => {
    node.addEventListener('click', () => openJob(Number(node.dataset.jobId)));
  });
})();
