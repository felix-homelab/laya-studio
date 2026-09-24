/* Scratch-style workflow step builder.
 *
 * A workflow is a *script*: a hat block says what starts it, then each rung of
 * the confidence ladder is a stack of colour-coded action blocks that can be
 * dragged, reordered, retargeted and deleted. There is no JSON to type — every
 * block is a set of named controls, and the values it produces are exactly the
 * ``{on, kind, config}`` steps the API expects.
 */

(() => {
  const L = window.LayaStudio;
  if (!L) return;

  const { escapeHtml, icon, enhanceSelects, chipsField, rowsEditor, iconButton } = L;

  /* ------------------------------------------------------------ definitions */

  const KIND_META = {
    webhook: {
      label: 'webhook',
      tone: 'sky',
      ink: 'POST the decision to a URL',
      blanks: () => ({ url: '', method: 'POST', payload: {}, headers: {} }),
    },
    email: {
      label: 'email',
      tone: 'violet',
      ink: 'send a notification',
      blanks: () => ({
        to: [],
        subject: 'Laya Studio workflow: {route}',
        body: 'A Laya Studio workflow routed an item.\n\n'
          + 'Workflow  : {workflow_key}\nRoute     : {route}\nQuestion  : {question}\n'
          + 'Answer    : {choice}\nConfidence: {confidence}\n\nState:\n{state_text}\n',
      }),
    },
    review: {
      label: 'review',
      tone: 'amber',
      ink: 'park it in the human queue',
      blanks: () => ({ reason: 'low_confidence', priority: 50 }),
    },
    noop: {
      label: 'note',
      tone: 'slate',
      ink: 'record what happened and stop',
      blanks: () => ({ detail: 'Auto-executed {choice} at {confidence}.' }),
    },
  };

  const ROUTES = [
    { on: 'auto', tone: 'emerald', fallback: 'confidence clears the act bar' },
    { on: 'verify', tone: 'amber', fallback: 'confidence lands in the middle band' },
    { on: 'review', tone: 'rose', fallback: 'confidence is too low to act' },
    { on: 'always', tone: 'slate', fallback: 'runs on every route' },
  ];

  const TONE = {
    sky: { frame: 'border-sky-500/40', body: 'bg-sky-500/10', chip: 'bg-sky-500/20 text-sky-200', rule: 'border-sky-500/20' },
    violet: { frame: 'border-violet-500/40', body: 'bg-violet-500/10', chip: 'bg-violet-500/20 text-violet-200', rule: 'border-violet-500/20' },
    amber: { frame: 'border-amber-500/40', body: 'bg-amber-500/10', chip: 'bg-amber-500/20 text-amber-200', rule: 'border-amber-500/20' },
    emerald: { frame: 'border-emerald-500/40', body: 'bg-emerald-500/10', chip: 'bg-emerald-500/20 text-emerald-200', rule: 'border-emerald-500/20' },
    rose: { frame: 'border-rose-500/40', body: 'bg-rose-500/10', chip: 'bg-rose-500/20 text-rose-200', rule: 'border-rose-500/20' },
    slate: { frame: 'border-slate-700', body: 'bg-slate-800/40', chip: 'bg-slate-700/60 text-slate-300', rule: 'border-slate-700' },
  };

  /** Placeholders every config string is formatted against, by the API. */
  const TOKENS = [
    'choice', 'confidence', 'route', 'question', 'state_text',
    'evaluation_id', 'workflow_key', 'workflow_name', 'reason', 'priority',
  ];

  const REASONS = ['low_confidence', 'ambiguous', 'policy', 'escalated', 'manual'];

  const REASON_LIST_ID = 'laya-review-reasons';

  const FIELD_CLASS = 'w-full rounded-lg border border-slate-800 bg-slate-950 px-2.5 py-1.5 text-[11px] '
    + 'text-slate-200 outline-none placeholder:text-slate-600 focus:border-sky-600';

  function tokensRow() {
    return `
      <div class="flex flex-wrap items-center gap-1 border-t border-slate-800 px-2.5 py-1.5">
        <span class="text-[10px] uppercase tracking-wider text-slate-600">insert</span>
        ${TOKENS.map((token) => `
          <button type="button" data-token="${token}" title="Insert the ${token} of this run"
            class="rounded border border-slate-800 bg-slate-950/60 px-1.5 py-0.5 font-mono text-[10px]
                   text-slate-400 hover:border-slate-600 hover:text-slate-200">{${token}}</button>`).join('')}
      </div>`;
  }

  function fieldsHtml(step) {
    const config = step.config || {};

    if (step.kind === 'webhook') {
      const methods = ['POST', 'PUT', 'PATCH', 'GET', 'DELETE'];
      const current = String(config.method || 'POST').toUpperCase();
      return `
        <div class="grid gap-2 sm:grid-cols-[minmax(0,1fr)_7rem]">
          <label class="block">
            <span class="laya-label">URL to call</span>
            <input data-field="url" value="${escapeHtml(config.url ?? '')}" spellcheck="false"
              placeholder="https://example.com/hooks/laya" class="${FIELD_CLASS} mt-1 font-mono">
          </label>
          <label class="block">
            <span class="laya-label">Method</span>
            <select data-field="method" class="${FIELD_CLASS} mt-1">
              ${methods.map((method) => `<option value="${method}"${current === method ? ' selected' : ''}>${method}</option>`).join('')}
            </select>
          </label>
        </div>
        <p class="text-[10px] text-slate-500">
          Leave the URL empty and this block logs <span class="font-mono">skipped</span> rather than failing the run.
        </p>
        <div class="grid gap-2 sm:grid-cols-2">
          <div>
            <span class="laya-label">JSON body</span>
            <div data-rows="payload" class="mt-1"></div>
            <p class="mt-1 text-[10px] text-slate-500">Empty sends the standard payload (route, choice, confidence, state).</p>
          </div>
          <div>
            <span class="laya-label">Headers</span>
            <div data-rows="headers" class="mt-1"></div>
          </div>
        </div>`;
    }

    if (step.kind === 'email') {
      return `
        <div>
          <span class="laya-label">Send to</span>
          <div data-chips="to" class="mt-1"></div>
          <p class="mt-1 text-[10px] text-slate-500">
            Type an address and press Enter. Without SMTP configured the block logs the attempt and skips the send.
          </p>
        </div>
        <label class="block">
          <span class="laya-label">Subject</span>
          <input data-field="subject" value="${escapeHtml(config.subject ?? '')}" class="${FIELD_CLASS} mt-1">
        </label>
        <label class="block">
          <span class="laya-label">Body</span>
          <textarea data-field="body" rows="4" spellcheck="false"
            class="${FIELD_CLASS} mt-1 resize-y font-mono">${escapeHtml(config.body ?? '')}</textarea>
        </label>`;
    }

    if (step.kind === 'review') {
      return `
        <div class="grid gap-2 sm:grid-cols-[minmax(0,1fr)_7rem]">
          <label class="block">
            <span class="laya-label">Reason recorded on the item</span>
            <input data-field="reason" list="${REASON_LIST_ID}" value="${escapeHtml(config.reason ?? 'low_confidence')}" class="${FIELD_CLASS} mt-1">
          </label>
          <label class="block">
            <span class="laya-label">Priority</span>
            <input data-field="priority" type="number" min="0" max="100" step="5"
              value="${escapeHtml(config.priority ?? 0)}" class="${FIELD_CLASS} mt-1 font-mono">
          </label>
        </div>
        <p class="text-[10px] text-slate-500">Higher priority sorts nearer the top of the review queue.</p>`;
    }

    return `
      <label class="block">
        <span class="laya-label">What this block records in the action log</span>
        <textarea data-field="detail" rows="2" spellcheck="false"
          class="${FIELD_CLASS} mt-1 resize-y font-mono">${escapeHtml(config.detail ?? '')}</textarea>
      </label>`;
  }

  function normalizeStep(step) {
    const kind = step && KIND_META[step.kind] ? step.kind : 'noop';
    const on = ROUTES.some((route) => route.on === (step && step.on)) ? step.on : 'auto';
    return { on, kind, config: { ...(step && step.config ? step.config : {}) } };
  }

  /* ---------------------------------------------------------------- builder */

  function blockBuilder(host, options = {}) {
    let model = (options.steps || []).map(normalizeStep);
    let hints = options.hints || {};
    let onChange = options.onChange;
    let dragFrom = null;

    host.innerHTML = '<div data-script class="laya-script space-y-4"></div>';
    const script = host.querySelector('[data-script]');

    function emit() {
      if (onChange) onChange(steps());
    }

    function blockAt(index) {
      return model[index];
    }

    function siblingsOf(on) {
      return model.map((step, index) => ({ step, index })).filter((entry) => entry.step.on === on);
    }

    function blockHtml(step, index) {
      const meta = KIND_META[step.kind];
      const tone = TONE[meta.tone];
      return `
        <article data-index="${index}" draggable="true"
          class="laya-block group min-w-0 rounded-xl border ${tone.frame} ${tone.body}">
          <header class="flex flex-wrap items-center gap-1.5 border-b ${tone.rule} px-2.5 py-1.5">
            <span data-grip class="cursor-grab text-slate-500 hover:text-slate-300" title="Drag to reorder">${icon('grip', 'h-3.5 w-3.5')}</span>
            <span class="text-[10px] font-semibold uppercase tracking-wider text-slate-500">run</span>
            <select data-kind aria-label="What this block does"
              class="rounded-md border ${tone.frame} bg-slate-950/70 px-2 py-1 text-[11px] font-medium text-slate-200 outline-none">
              ${Object.entries(KIND_META).map(([kind, entry]) => `<option value="${kind}"${kind === step.kind ? ' selected' : ''}>${entry.label}</option>`).join('')}
            </select>
            <span class="text-[10px] font-semibold uppercase tracking-wider text-slate-500">when</span>
            <select data-on aria-label="Which route runs this block"
              class="rounded-md border ${tone.frame} bg-slate-950/70 px-2 py-1 text-[11px] font-medium text-slate-200 outline-none">
              ${ROUTES.map((route) => `<option value="${route.on}"${route.on === step.on ? ' selected' : ''}>${route.on}</option>`).join('')}
            </select>
            <span class="hidden text-[11px] text-slate-500 sm:inline">${escapeHtml(meta.ink)}</span>
            <span class="ml-auto flex items-center gap-1 opacity-60 transition group-hover:opacity-100">
              ${iconButton('up', 'Move block up')}
              ${iconButton('down', 'Move block down')}
              ${iconButton('remove', 'Delete block')}
            </span>
          </header>
          <div data-fields class="space-y-2 px-2.5 py-2.5">${fieldsHtml(step)}</div>
          ${tokensRow()}
        </article>`;
    }

    function groupHtml(route) {
      const entries = siblingsOf(route.on);
      const tone = TONE[route.tone];
      const hint = hints[route.on] || route.fallback;
      const stack = entries.length
        ? entries.map(({ step, index }) => blockHtml(step, index)).join('')
        : '<p class="rounded-lg border border-dashed border-slate-800 px-3 py-2 text-[11px] text-slate-600">Nothing runs on this route yet.</p>';
      return `
        <section class="laya-group" data-group="${route.on}">
          <header class="mb-1.5 flex flex-wrap items-center gap-2">
            <span class="rounded-md px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider ${tone.chip}">route ${route.on}</span>
            <span data-hint class="text-[11px] text-slate-500">${escapeHtml(hint)}</span>
            <span class="ml-auto text-[11px] text-slate-600">${entries.length} block${entries.length === 1 ? '' : 's'}</span>
          </header>
          <div data-stack="${route.on}" class="laya-stack space-y-2 pl-4">${stack}</div>
          <div class="mt-1.5 flex flex-wrap items-center gap-1 pl-4">
            ${Object.entries(KIND_META).map(([kind, entry]) => `
              <button type="button" data-add="${kind}"
                class="rounded-md border border-dashed border-slate-700 px-2 py-0.5 text-[10px] text-slate-400
                       hover:border-slate-500 hover:text-slate-200">+ ${entry.label}</button>`).join('')}
          </div>
        </section>`;
    }

    function mountBlock(node, step) {
      const config = step.config;
      node.querySelectorAll('[data-rows]').forEach((rowsHost) => {
        const key = rowsHost.dataset.rows;
        // Raw values, not stringified: ``pairRows`` infers a type per value so a
        // nested object keeps its shape instead of becoming "[object Object]".
        const rows = Object.entries(config[key] || {}).map(([entryKey, value]) => ({ key: entryKey, value }));
        rowsEditor(rowsHost, {
          mode: 'pairs',
          rows,
          addLabel: 'Add',
          keyPlaceholder: key === 'headers' ? 'header' : 'field',
          onChange: (value) => { config[key] = value; emit(); },
        });
      });
      node.querySelectorAll('[data-chips]').forEach((chipsHost) => {
        const key = chipsHost.dataset.chips;
        const values = Array.isArray(config[key]) ? config[key] : (config[key] ? [config[key]] : []);
        chipsField(chipsHost, {
          values,
          placeholder: 'name@example.com',
          onChange: (value) => { config[key] = value; emit(); },
        });
      });
    }

    function render() {
      script.innerHTML = ROUTES.map(groupHtml).join('')
        + `<datalist id="${REASON_LIST_ID}">${REASONS.map((reason) => `<option value="${reason}"></option>`).join('')}</datalist>`;
      script.querySelectorAll('[data-index]').forEach((node) => {
        mountBlock(node, model[Number(node.dataset.index)]);
      });
      enhanceSelects(script);
    }

    /* ------------------------------------------------------------ structure */

    function append(on, kind) {
      model.push({ on, kind, config: KIND_META[kind].blanks() });
      render();
      emit();
    }

    function removeAt(index) {
      model.splice(index, 1);
      render();
      emit();
    }

    /** Swap a block with its neighbour *inside the same route*.
     *
     * Swapping two positions leaves every other block's relative order intact,
     * so the other stacks never shuffle when you nudge one block. */
    function nudge(index, delta) {
      const step = blockAt(index);
      if (!step) return;
      const siblings = siblingsOf(step.on);
      const at = siblings.findIndex((entry) => entry.index === index);
      const target = siblings[at + delta];
      if (!target) return;
      model[index] = target.step;
      model[target.index] = step;
      render();
      emit();
    }

    /* ---------------------------------------------------------- interaction */

    script.addEventListener('input', (event) => {
      if (!event.target.matches('input, textarea')) return;
      const node = event.target.closest('[data-index]');
      const field = event.target.dataset.field;
      if (!node || !field || field === 'method') return;
      const step = blockAt(Number(node.dataset.index));
      if (!step) return;
      step.config[field] = field === 'priority' ? Number(event.target.value) : event.target.value;
      emit();
    });

    script.addEventListener('change', (event) => {
      const node = event.target.closest('[data-index]');
      if (!node) return;
      const index = Number(node.dataset.index);
      const step = blockAt(index);
      if (!step) return;

      if (event.target.matches('select[data-kind]')) {
        const kind = event.target.value;
        if (!KIND_META[kind] || kind === step.kind) return;
        step.kind = kind;
        step.config = { ...KIND_META[kind].blanks(), ...step.config };
        render();
        emit();
      } else if (event.target.matches('select[data-on]')) {
        const on = event.target.value;
        if (!ROUTES.some((route) => route.on === on) || on === step.on) return;
        step.on = on;
        render();
        emit();
      } else if (event.target.dataset.field === 'method') {
        step.config.method = event.target.value;
        emit();
      }
    });

    script.addEventListener('focusin', (event) => {
      const block = event.target.closest('[data-index]');
      if (!block) return;
      if (event.target.matches('input, textarea')) block.__field = event.target;
    });

    script.addEventListener('click', (event) => {
      const tokenNode = event.target.closest('[data-token]');
      if (tokenNode) {
        insertToken(tokenNode.closest('[data-index]'), tokenNode.dataset.token);
        return;
      }

      const addNode = event.target.closest('[data-add]');
      if (addNode) {
        append(addNode.closest('[data-group]').dataset.group, addNode.dataset.add);
        return;
      }

      const actionNode = event.target.closest('[data-action]');
      if (!actionNode) return;
      const block = actionNode.closest('[data-index]');
      const index = Number(block.dataset.index);
      const action = actionNode.dataset.action;
      if (action === 'remove') removeAt(index);
      else if (action === 'up') nudge(index, -1);
      else if (action === 'down') nudge(index, 1);
    });

    function insertToken(block, token) {
      if (!block) return;
      const target = block.__field || block.querySelector('[data-fields] textarea, [data-fields] input');
      if (!target) return;
      const text = `{${token}}`;
      const start = target.selectionStart ?? target.value.length;
      const end = target.selectionEnd ?? start;
      target.value = target.value.slice(0, start) + text + target.value.slice(end);
      target.focus();
      target.setSelectionRange(start + text.length, start + text.length);
      target.dispatchEvent(new Event('input', { bubbles: true }));
    }

    /* -------------------------------------------------------- drag and drop */

    function clearDropMarks() {
      script.querySelectorAll('.laya-drop').forEach((node) => node.classList.remove('laya-drop'));
      script.querySelectorAll('.laya-block').forEach((node) => node.classList.remove('laya-dragging'));
    }

    script.addEventListener('dragstart', (event) => {
      const block = event.target.closest('[data-index]');
      if (!block) return;
      dragFrom = Number(block.dataset.index);
      clearDropMarks();
      block.classList.add('laya-dragging');
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', String(dragFrom));
    });

    script.addEventListener('dragend', () => {
      dragFrom = null;
      clearDropMarks();
    });

    script.addEventListener('dragover', (event) => {
      const stack = event.target.closest('[data-stack]');
      if (!stack || dragFrom === null) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = 'move';
      const before = stack.querySelector('.laya-drop');
      if (before) before.classList.remove('laya-drop');
      const marker = event.target.closest('[data-index]');
      if (marker && Number(marker.dataset.index) !== dragFrom) {
        marker.classList.add('laya-drop');
      } else {
        stack.classList.add('laya-drop');
      }
    });

    script.addEventListener('drop', (event) => {
      const stack = event.target.closest('[data-stack]');
      if (!stack || dragFrom === null) return;
      event.preventDefault();

      const step = blockAt(dragFrom);
      const rest = model.filter((_, index) => index !== dragFrom);
      step.on = stack.dataset.stack;

      let position = rest.length;
      const marker = event.target.closest('[data-index]');
      const reference = marker ? blockAt(Number(marker.dataset.index)) : null;
      if (reference && reference !== step) {
        const at = rest.indexOf(reference);
        const rect = marker.getBoundingClientRect();
        position = (at >= 0 ? at : rest.length) + (event.clientY < rect.top + rect.height / 2 ? 0 : 1);
      }

      rest.splice(Math.max(0, Math.min(position, rest.length)), 0, step);
      model = rest;
      dragFrom = null;
      clearDropMarks();
      render();
      emit();
    });

    /* --------------------------------------------------------------- public */

    function steps() {
      return model.map((step) => ({ on: step.on, kind: step.kind, config: { ...step.config } }));
    }

    render();

    function paintHints() {
      script.querySelectorAll('[data-group]').forEach((section) => {
        const route = ROUTES.find((entry) => entry.on === section.dataset.group);
        const node = section.querySelector('[data-hint]');
        if (route && node) node.textContent = hints[route.on] || route.fallback;
      });
    }

    return {
      steps,
      setSteps(next) { model = (next || []).map(normalizeStep); render(); },
      /** Only the ladder captions change, so the blocks keep their focus. */
      setHints(next) { hints = next || {}; paintHints(); },
      onChange(handler) { onChange = handler; },
      add(on, kind) { append(on, kind); },
    };
  }

  L.blockBuilder = blockBuilder;
  L.WORKFLOW_KINDS = KIND_META;
})();
