/* Review queue: inspect, claim, approve, correct, reject. */

(() => {
  const { api, toast, escapeHtml, pct } = window.LayaStudio;

  const list = document.getElementById('review-list');
  const detail = document.getElementById('review-detail');

  /** Status chip colours, kept identical to the server-rendered list in review.html. */
  function statusTone(status) {
    return {
      approved: 'bg-emerald-500/15 text-emerald-300',
      corrected: 'bg-amber-500/15 text-amber-300',
      in_review: 'bg-sky-500/15 text-sky-300',
      rejected: 'bg-rose-500/15 text-rose-300',
    }[status] || 'bg-slate-700/40 text-slate-300';
  }

  async function open(itemId) {
    try {
      const item = await api(`/api/review/items/${itemId}`);
      render(item);
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  function render(item) {
    const alternatives = (item.alternatives || []).length
      ? `<div class="mt-3 space-y-2">
           <p class="text-[11px] font-medium uppercase tracking-wider text-slate-500">Alternatives the model considered</p>
           ${item.alternatives.map((alt) => `
             <div>
               <div class="flex items-baseline justify-between text-[11px]">
                 <span class="text-slate-400">${escapeHtml(alt.label)}</span>
                 <span class="font-mono text-slate-500">${pct(alt.probability)}</span>
               </div>
               <div class="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-slate-800">
                 <span class="block h-full rounded-full bg-slate-600" style="width: ${alt.probability * 100}%"></span>
               </div>
             </div>`).join('')}
         </div>` : '';

    const options = [item.model_choice, ...(item.alternatives || []).map((a) => a.label)]
      .filter((v, i, arr) => v && arr.indexOf(v) === i);

    detail.innerHTML = `
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 class="text-sm font-semibold text-slate-100">Item #${item.id}</h2>
          <p class="mt-0.5 text-[11px] text-slate-500">
            reason <span class="font-mono">${escapeHtml(item.reason)}</span>
            · priority ${item.priority}
            ${item.assignee ? `· assigned to ${escapeHtml(item.assignee)}` : ''}
          </p>
        </div>
        <span class="rounded-full px-2.5 py-1 text-[11px] font-medium ${statusTone(item.status)}">${escapeHtml(item.status)}</span>
      </div>

      <div class="mt-4 rounded-xl border border-slate-800 bg-slate-950/60 p-4">
        <p class="text-[11px] font-medium uppercase tracking-wider text-slate-500">State</p>
        <pre class="mt-2 max-h-56 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-slate-300">${escapeHtml(item.state_text || JSON.stringify(item.state, null, 2))}</pre>
      </div>

      <div class="mt-4 grid grid-cols-2 gap-3">
        <div class="rounded-xl border border-slate-800 bg-slate-950/50 px-3.5 py-2.5">
          <p class="text-[11px] text-slate-500">Model answer</p>
          <p class="mt-0.5 text-lg font-semibold text-slate-100">${escapeHtml(item.model_choice ?? '—')}</p>
        </div>
        <div class="rounded-xl border border-slate-800 bg-slate-950/50 px-3.5 py-2.5">
          <p class="text-[11px] text-slate-500">Confidence</p>
          <p class="mt-0.5 text-lg font-semibold text-slate-100">${pct(item.model_confidence)}</p>
        </div>
      </div>

      ${alternatives}

      ${item.status === 'pending' ? `
        <div class="mt-4 flex gap-2">
          <input id="assignee" placeholder="your name"
            class="flex-1 rounded-lg border border-slate-800 bg-slate-950 px-2.5 py-1.5 text-xs text-slate-200 outline-none focus:border-sky-600">
          <button type="button" id="claim-btn"
            class="rounded-lg border border-slate-800 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-800">Claim</button>
        </div>` : ''}

      <div class="mt-4 rounded-xl border border-slate-800 bg-slate-950/50 p-4">
        <p class="text-xs font-medium text-slate-300">Resolve</p>
        <p class="mt-0.5 text-[11px] text-slate-500">
          Approving or correcting writes a ground-truth label back onto the evaluation, which is
          what the calibration monitor learns from.
        </p>

        <label class="mt-3 block">
          <span class="text-[11px] font-medium uppercase tracking-wider text-slate-500">Correct answer</span>
          <input id="corrected-label" list="item-labels" placeholder="the label you would have assigned"
            value="${escapeHtml(item.corrected_label || item.model_choice || '')}"
            class="mt-1 w-full rounded-lg border border-slate-800 bg-slate-950 px-2.5 py-1.5 text-xs text-slate-200 outline-none focus:border-sky-600">
        </label>
        <datalist id="item-labels">
          ${options.map((v) => `<option value="${escapeHtml(v)}"></option>`).join('')}
        </datalist>

        <label class="mt-3 block">
          <span class="text-[11px] font-medium uppercase tracking-wider text-slate-500">Notes</span>
          <textarea id="review-notes" rows="2"
            class="mt-1 w-full resize-y rounded-lg border border-slate-800 bg-slate-950 px-2.5 py-2 text-xs text-slate-200 outline-none focus:border-sky-600">${escapeHtml(item.notes || '')}</textarea>
        </label>

        <div class="mt-3 flex flex-wrap gap-2">
          <button type="button" data-resolve="approved"
            class="flex-1 rounded-lg bg-emerald-600 px-3 py-2 text-xs font-semibold text-white hover:bg-emerald-500">Approve</button>
          <button type="button" data-resolve="corrected"
            class="flex-1 rounded-lg bg-amber-600 px-3 py-2 text-xs font-semibold text-white hover:bg-amber-500">Correct</button>
          <button type="button" data-resolve="rejected"
            class="rounded-lg border border-rose-500/40 px-3 py-2 text-xs font-medium text-rose-300 hover:bg-rose-500/10">Reject</button>
        </div>
      </div>

      ${item.workflow_id ? `
        <button type="button" id="retry-btn"
          class="mt-3 w-full rounded-xl border border-slate-800 px-3 py-2 text-xs text-slate-400 hover:text-slate-200">
          Re-run the workflow with this state
        </button>` : ''}`;

    const claimBtn = document.getElementById('claim-btn');
    if (claimBtn) {
      claimBtn.addEventListener('click', async () => {
        const assignee = document.getElementById('assignee').value.trim();
        if (!assignee) { toast('Enter your name first', 'warn'); return; }
        try {
          const updated = await api(`/api/review/items/${item.id}/claim`, {
            method: 'POST',
            body: JSON.stringify({ assignee }),
          });
          toast('Claimed', 'success');
          render(updated);
        } catch (error) {
          toast(error.message, 'error');
        }
      });
    }

    detail.querySelectorAll('[data-resolve]').forEach((button) => {
      button.addEventListener('click', async () => {
        const status = button.dataset.resolve;
        const corrected = document.getElementById('corrected-label').value.trim();
        const notes = document.getElementById('review-notes').value.trim();
        if (status === 'corrected' && !corrected) {
          toast('A correction needs a label', 'warn');
          return;
        }
        try {
          const updated = await api(`/api/review/items/${item.id}/resolve`, {
            method: 'POST',
            body: JSON.stringify({ status, corrected_label: corrected || null, notes }),
          });
          toast(`Marked ${updated.status}`, 'success');
          render(updated);
          refreshList();
        } catch (error) {
          toast(error.message, 'error');
        }
      });
    });

    const retryBtn = document.getElementById('retry-btn');
    if (retryBtn) {
      retryBtn.addEventListener('click', async () => {
        try {
          const result = await api(`/api/review/items/${item.id}/retry`, { method: 'POST' });
          toast(`Re-ran as run #${result.run_id} → ${result.effective_route}`, 'success', 6000);
        } catch (error) {
          toast(error.message, 'error');
        }
      });
    }
  }

  async function refreshList() {
    const status = new URLSearchParams(window.location.search).get('status') || 'open';
    try {
      const rows = await api(`/api/review/items?status=${encodeURIComponent(status)}&limit=200`);
      list.innerHTML = rows.length
        ? rows.map((item) => `
          <button type="button" data-item-id="${item.id}" class="flex w-full items-start gap-3 px-5 py-3 text-left hover:bg-slate-800/40">
            <span class="min-w-0 flex-1">
              <span class="block truncate text-xs text-slate-300">${escapeHtml((item.state_text || '(empty state)').slice(0, 140))}</span>
              <span class="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
                <span class="font-mono">${escapeHtml(item.question || '—')}</span>
                <span>→ <span class="text-slate-300">${escapeHtml(item.model_choice || '—')}</span></span>
                <span>${pct(item.model_confidence, 0)}</span>
                <span class="text-slate-600">${escapeHtml(item.reason)}</span>
              </span>
            </span>
            <span class="shrink-0 rounded-full px-2 py-0.5 text-[11px] ${statusTone(item.status)}">${escapeHtml(item.status)}</span>
          </button>`).join('')
        : '<div class="p-5"><p class="rounded-xl border border-dashed border-slate-800 px-4 py-10 text-center text-xs text-slate-500">Queue is empty.</p></div>';

      list.querySelectorAll('[data-item-id]').forEach((node) => {
        node.addEventListener('click', () => open(Number(node.dataset.itemId)));
      });
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  list.querySelectorAll('[data-item-id]').forEach((node) => {
    node.addEventListener('click', () => open(Number(node.dataset.itemId)));
  });

  const params = new URLSearchParams(window.location.search);
  if (params.get('run')) {
    api(`/api/workflows/runs/${params.get('run')}`)
      .then((run) => {
        const items = run.action_logs || [];
        const queued = items.find((log) => log.kind === 'review');
        if (queued && queued.response && queued.response.review_item_id) {
          open(queued.response.review_item_id);
        } else {
          toast('That run did not queue a review item', 'warn');
        }
      })
      .catch((error) => toast(error.message, 'error'));
  }
})();
