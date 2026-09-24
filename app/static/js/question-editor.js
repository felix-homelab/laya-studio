/* Visual editor for a Laya question bundle.
 *
 * Replaces the JSON textarea that used to be the only way to author a schema.
 * Each question is a card: a name, a primitive picked from a dropdown, the
 * instructions, and a criteria editor whose shape follows the primitive —
 * label/description rows for ``choice`` and ``noul``, an ordered rubric for
 * ``score``. The result is the same ``{name: {type, instructions, criteria}}``
 * object the API has always taken.
 */

(() => {
  const L = window.LayaStudio;
  if (!L) return;

  const { escapeHtml, icon, iconButton, rowsEditor, enhanceSelects } = L;

  const PRIMITIVES = [
    {
      value: 'choice',
      label: 'choice',
      ink: 'pick one label',
      blanks: () => ({ option_1: '', option_2: '' }),
      addLabel: 'Add option',
      keyPlaceholder: 'label',
      valuePlaceholder: 'what belongs in this label',
    },
    {
      value: 'score',
      label: 'score',
      ink: 'rank on an ordered rubric',
      blanks: () => ['', '', ''],
      addLabel: 'Add level',
      valuePlaceholder: 'level wording, lowest first',
    },
    {
      value: 'noul',
      label: 'noul',
      ink: 'answer true or false',
      blanks: () => ({ yes: '', no: '' }),
      addLabel: 'Add criterion',
      keyPlaceholder: 'label',
      valuePlaceholder: 'what makes it true / false',
    },
  ];

  const meta = (type) => PRIMITIVES.find((entry) => entry.value === type) || PRIMITIVES[0];

  const FIELD_CLASS = 'w-full rounded-lg border border-slate-800 bg-slate-950 px-2.5 py-1.5 text-[11px] '
    + 'text-slate-200 outline-none placeholder:text-slate-600 focus:border-sky-600';

  function sanitizeName(raw) {
    const cleaned = String(raw || '')
      .normalize('NFKD')
      .replace(/[^\w\s-]+/g, '')
      .trim()
      .toLowerCase()
      .replace(/[\s-]+/g, '_')
      .replace(/^_+|_+$/g, '');
    return cleaned;
  }

  function uniqueName(base, taken) {
    const root = sanitizeName(base) || 'question';
    if (!taken.includes(root)) return root;
    let suffix = 2;
    while (taken.includes(`${root}_${suffix}`)) suffix += 1;
    return `${root}_${suffix}`;
  }

  function convertCriteria(criteria, from, to) {
    if (from === to) return criteria;

    if (from === 'score') {
      const levels = Array.isArray(criteria) ? criteria : [];
      if (to === 'choice') {
        const out = {};
        levels.forEach((level, index) => {
          const label = sanitizeName(level) || `option_${index + 1}`;
          out[label] = '';
        });
        return Object.keys(out).length >= 2 ? out : { option_1: '', option_2: '' };
      }
      return { yes: '', no: '' };
    }

    const entries = criteria && typeof criteria === 'object' && !Array.isArray(criteria)
      ? Object.entries(criteria)
      : [];
    if (to === 'score') return entries.length >= 2 ? entries.map(([label]) => label) : ['', '', ''];
    if (to === 'choice') return entries.length >= 2 ? Object.fromEntries(entries) : { option_1: '', option_2: '' };
    return { yes: '', no: '' };
  }

  function shapeOf(entry) {
    if (entry.type === 'score') {
      const levels = Array.isArray(entry.criteria) ? entry.criteria : [];
      // Hand the levels over as they are: the editor renders each one faithfully
      // (see ``listRows``) instead of stringifying it into "[object Object]".
      return { mode: 'list', rows: levels };
    }
    const criteria = entry.criteria && typeof entry.criteria === 'object' && !Array.isArray(entry.criteria)
      ? entry.criteria
      : {};
    return {
      mode: 'pairs',
      rows: Object.entries(criteria).map(([key, value]) => ({ key, value })),
    };
  }

  /* ---------------------------------------------------------------- editor */

  function questionEditor(host, options = {}) {
    let model = fromObject(options.questions || {});
    let onChange = options.onChange;

    host.innerHTML = '<div data-cards class="space-y-3"></div>'
      + '<div data-footer class="mt-3 flex flex-wrap items-center gap-2"></div>';
    const cards = host.querySelector('[data-cards]');
    const footer = host.querySelector('[data-footer]');

    function fromObject(questions) {
      return Object.entries(questions || {}).map(([name, definition]) => ({
        name,
        type: definition && PRIMITIVES.some((entry) => entry.value === definition.type) ? definition.type : 'choice',
        instructions: String((definition && definition.instructions) || ''),
        criteria: definition ? definition.criteria : undefined,
      })).map((entry) => ({ ...entry, criteria: entry.criteria ?? meta(entry.type).blanks() }));
    }

    function emit() {
      if (onChange) onChange(questions());
    }

    function describe(entry) {
      if (entry.type === 'score') {
        const levels = Array.isArray(entry.criteria) ? entry.criteria.length : 0;
        return `${levels} level${levels === 1 ? '' : 's'} · lowest first`;
      }
      const count = entry.criteria && typeof entry.criteria === 'object' && !Array.isArray(entry.criteria)
        ? Object.keys(entry.criteria).length
        : 0;
      return `${count} label${count === 1 ? '' : 's'}`;
    }

    function cardHtml(entry, index) {
      const primitive = meta(entry.type);
      return `
        <article data-index="${index}" class="rounded-xl border border-slate-800 bg-slate-950/60">
          <header class="flex flex-wrap items-center gap-1.5 border-b border-slate-800 px-2.5 py-2">
            <label class="min-w-[8rem] flex-1">
              <span class="sr-only">Question name</span>
              <input data-name value="${escapeHtml(entry.name)}" spellcheck="false" placeholder="question_name"
                class="${FIELD_CLASS} font-mono">
            </label>
            <select data-type aria-label="Primitive for this question"
              class="shrink-0 rounded-lg border border-slate-800 bg-slate-950 px-2 py-1.5 text-[11px] font-medium text-slate-200 outline-none">
              ${PRIMITIVES.map((option) => `<option value="${option.value}"${option.value === entry.type ? ' selected' : ''}>${option.label}</option>`).join('')}
            </select>
            <span class="hidden text-[11px] text-slate-500 sm:inline">${escapeHtml(primitive.ink)}</span>
            <span class="ml-auto flex items-center gap-1">
              ${iconButton('up', 'Move question up')}
              ${iconButton('down', 'Move question down')}
              ${iconButton('remove', 'Remove question')}
            </span>
          </header>
          <div class="space-y-2 px-2.5 py-2.5">
            <label class="block">
              <span class="laya-label">Instructions for the model</span>
              <textarea data-instructions rows="2" spellcheck="false" placeholder="Which department should handle this request?"
                class="${FIELD_CLASS} mt-1 resize-y">${escapeHtml(entry.instructions)}</textarea>
            </label>
            <div>
              <div class="flex items-baseline justify-between gap-2">
                <span class="laya-label">${entry.type === 'score' ? 'Rubric levels' : 'Labels and criteria'}</span>
                <span data-summary class="text-[10px] text-slate-500">${escapeHtml(describe(entry))}</span>
              </div>
              <div data-criteria class="mt-1"></div>
            </div>
            ${entry.type === 'noul'
              ? '<p class="text-[10px] text-slate-500">Describe the negative case with different vocabulary — shared wording is what breaks a one-pass yes/no head.</p>'
              : ''}
            ${entry.type === 'choice'
              ? '<p class="text-[10px] text-slate-500">Keep the shortlist under 11 labels: beyond that Laya\'s shipped temperature is clamped and the confidence stops being a probability.</p>'
              : ''}
          </div>
        </article>`;
    }

    /** An empty criteria set is filled with the primitive's blanks, so what is
     *  on screen is exactly what gets saved.
     *
     *  A `score` whose stored criteria is a *dict* (invalid for Laya, but
     *  reachable through the API) keeps its wording as the levels rather than
     *  being silently blanked: the author wrote those words. */
    function seed(entry) {
      const criteria = entry.criteria;
      if (entry.type === 'score') {
        if (!Array.isArray(criteria) || criteria.length === 0) {
          const levels = criteria && typeof criteria === 'object' ? Object.keys(criteria) : [];
          entry.criteria = levels.length ? levels : meta(entry.type).blanks();
        }
        return entry;
      }
      const empty = !criteria || typeof criteria !== 'object' || Array.isArray(criteria)
        || Object.keys(criteria).length === 0;
      if (empty) entry.criteria = meta(entry.type).blanks();
      return entry;
    }

    function mountCard(node, entry) {
      const primitive = meta(entry.type);
      const shape = shapeOf(entry);
      const criteriaHost = node.querySelector('[data-criteria]');
      rowsEditor(criteriaHost, {
        mode: shape.mode,
        rows: shape.rows,
        addLabel: primitive.addLabel,
        keyPlaceholder: primitive.keyPlaceholder || 'label',
        valuePlaceholder: primitive.valuePlaceholder || 'value',
        onChange: (value) => {
          entry.criteria = value;
          const summary = node.querySelector('[data-summary]');
          if (summary) summary.textContent = describe(entry);
          emit();
        },
      });
    }

    function render() {
      model.forEach(seed);
      cards.innerHTML = model.length
        ? model.map(cardHtml).join('')
        : `<p class="rounded-xl border border-dashed border-slate-800 px-4 py-8 text-center text-xs text-slate-500">
             No questions yet. Add one below — a schema needs at least one.</p>`;
      cards.querySelectorAll('[data-index]').forEach((node) => mountCard(node, model[Number(node.dataset.index)]));
      footer.innerHTML = `
        <span class="text-[10px] uppercase tracking-wider text-slate-600">add</span>
        ${PRIMITIVES.map((primitive) => `
          <button type="button" data-add="${primitive.value}"
            class="inline-flex items-center gap-1 rounded-md border border-dashed border-slate-700 px-2 py-1 text-[11px]
                   text-slate-400 hover:border-sky-600 hover:text-sky-300">${icon('plus', 'h-3 w-3')} ${primitive.label}</button>`).join('')}`;
      enhanceSelects(host);
    }

    function questions() {
      const out = {};
      model.forEach((entry) => {
        const name = sanitizeName(entry.name) || 'question';
        out[name] = { type: entry.type, instructions: entry.instructions, criteria: entry.criteria };
      });
      return out;
    }

    function renameAll() {
      const taken = [];
      model.forEach((entry) => {
        const name = uniqueName(entry.name, taken);
        taken.push(name);
        entry.name = name;
      });
    }

    /* --------------------------------------------------------- interaction */

    cards.addEventListener('input', (event) => {
      const node = event.target.closest('[data-index]');
      if (!node) return;
      const entry = model[Number(node.dataset.index)];
      if (!entry) return;
      if (event.target.matches('[data-name]')) entry.name = event.target.value;
      else if (event.target.matches('[data-instructions]')) entry.instructions = event.target.value;
      else return;
      emit();
    });

    // Capture phase: blur does not bubble, and a name only settles when the
    // field is left. Rewriting it into a key can collide with another card, so
    // leaving an already-clean but duplicated name still has to re-number.
    cards.addEventListener('blur', (event) => {
      if (!event.target.matches('[data-name]')) return;
      const entry = model[Number(event.target.closest('[data-index]').dataset.index)];
      if (!entry) return;
      const clean = sanitizeName(event.target.value) || 'question';
      const collides = model.some((other) => other !== entry && sanitizeName(other.name) === clean);
      if (clean === entry.name && !collides) return;
      entry.name = clean;
      renameAll();
      render();
      emit();
    }, true);

    cards.addEventListener('change', (event) => {
      const node = event.target.closest('[data-index]');
      if (!node) return;
      const entry = model[Number(node.dataset.index)];
      if (!entry || !event.target.matches('select[data-type]')) return;
      const type = event.target.value;
      if (type === entry.type) return;
      entry.criteria = convertCriteria(entry.criteria, entry.type, type);
      entry.type = type;
      render();
      emit();
    });

    cards.addEventListener('click', (event) => {
      const actionNode = event.target.closest('[data-action]');
      const node = event.target.closest('[data-index]');
      if (!actionNode || !node) return;
      const index = Number(node.dataset.index);
      const action = actionNode.dataset.action;
      if (action === 'remove') {
        model.splice(index, 1);
      } else if (action === 'up' && index > 0) {
        [model[index - 1], model[index]] = [model[index], model[index - 1]];
      } else if (action === 'down' && index < model.length - 1) {
        [model[index + 1], model[index]] = [model[index], model[index + 1]];
      } else {
        return;
      }
      render();
      emit();
    });

    footer.addEventListener('click', (event) => {
      const node = event.target.closest('[data-add]');
      if (!node) return;
      const type = node.dataset.add;
      model.push({
        name: uniqueName(type, model.map((entry) => entry.name)),
        type,
        instructions: '',
        criteria: meta(type).blanks(),
      });
      render();
      emit();
    });

    render();

    return {
      questions,
      setQuestions(next) { model = fromObject(next); render(); },
      addQuestion(type) {
        model.push({
          name: uniqueName(type, model.map((entry) => entry.name)),
          type,
          instructions: '',
          criteria: meta(type).blanks(),
        });
        render();
        emit();
      },
      count: () => model.length,
    };
  }

  L.questionEditor = questionEditor;
  L.QUESTION_PRIMITIVES = PRIMITIVES;
})();
