/* Analytics page: the calibration refit action. Everything else is rendered
   server-side so the first paint has real numbers. */

(() => {
  const { api, toast, escapeHtml, num } = window.LayaStudio;

  const button = document.getElementById('refit-btn');
  const output = document.getElementById('refit-output');
  if (!button) return;

  button.addEventListener('click', async () => {
    button.disabled = true;
    const original = button.innerHTML;
    button.textContent = 'Refitting…';

    try {
      const result = await api('/api/analytics/calibration/refit', { method: 'POST' });
      toast(result.message, result.snapshots.length ? 'success' : 'warn', 6000);

      output.innerHTML = result.snapshots.length
        ? `<div class="rounded-2xl border border-slate-800 bg-slate-900/60 p-4">
             <p class="text-xs font-medium text-slate-300">Fitted ${result.snapshots.length} slice(s)</p>
             <div class="mt-2 space-y-1.5">
               ${result.snapshots.map((s) => `
                 <div class="flex flex-wrap items-center gap-3 text-[11px]">
                   <span class="font-mono text-slate-300">${escapeHtml(s.question || '(schema-wide)')}</span>
                   <span class="text-slate-500">slope <span class="font-mono text-slate-300">${num(s.slope, 4)}</span></span>
                   <span class="text-slate-500">intercept <span class="font-mono text-slate-300">${num(s.intercept, 4)}</span></span>
                   <span class="text-slate-500">n = ${s.n_samples}</span>
                   ${s.ece != null ? `<span class="text-slate-500">ECE = <span class="font-mono text-slate-300">${num(s.ece, 4)}</span></span>` : ''}
                 </div>`).join('')}
             </div>
             <p class="mt-3 text-[11px] text-slate-500">
               Reload to see the refitted curves. New evaluations will use these maps —
               a positive intercept means the checkpoint's confidence was understating
               accuracy, and the map now corrects it.
             </p>
           </div>`
        : '';
    } catch (error) {
      toast(error.message, 'error');
      output.innerHTML = '';
    } finally {
      button.disabled = false;
      button.innerHTML = original;
    }
  });
})();
