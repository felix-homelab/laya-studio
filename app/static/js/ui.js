/* Shared UI primitives.
 *
 * Framework-free, no build step — same constraints as the rest of
 * ``app/static/js``. Everything is styled with the Tailwind utility classes the
 * server-rendered pages already use, so a widget dropped anywhere inherits that
 * spot's look.
 *
 *   Select        every native <select> — and every <input list="…"> backed by a
 *                 <datalist> — is upgraded in place to a custom popup. The native
 *                 control stays in the DOM as the source of truth, so page code
 *                 keeps reading ``.value`` and listening for ``change``.
 *   chips         a text input that turns what you type into removable tags.
 *   rowsEditor    the structured editor the config surfaces are built from:
 *                 key/value pairs (optionally typed) or an ordered string list.
 *   stateEditor   free text *or* typed key/value fields, for a Laya state.
 */

(() => {
  const L = window.LayaStudio;
  if (!L) return;

  const { escapeHtml } = L;

  /* --------------------------------------------------------------- icons */

  const ICON_PATHS = {
    chevron: '<path d="m6 9 6 6 6-6"/>',
    check: '<path d="M4 12.5 9 17.5 20 6.5"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
    remove: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
    grip: '<path d="M9 5h.01M9 12h.01M9 19h.01M15 5h.01M15 12h.01M15 19h.01"/>',
    up: '<path d="M12 19V5M5 12l7-7 7 7"/>',
    down: '<path d="M12 5v14M19 12l-7 7-7-7"/>',
    x: '<path d="M6 6l12 12M18 6 6 18"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  };

  function icon(name, cls = 'h-3.5 w-3.5') {
    return `<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"
      stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON_PATHS[name] || ''}</svg>`;
  }

  /* ------------------------------------------------------------- dropdown */

  /** Class tokens that size a control rather than skin it. */
  const LAYOUT_TOKEN = /^(?:w-|mt-|mb-|ml-|mr-|min-w-|max-w-|flex-1$|grow|self-|order-|basis-|col-span-|row-span-)/;

  function splitTokens(el) {
    const tokens = Array.from(el.classList).filter((t) => t !== 'hidden');
    return {
      measure: tokens.filter((t) => LAYOUT_TOKEN.test(t)),
      skin: tokens.filter((t) => !LAYOUT_TOKEN.test(t)),
      hidden: el.classList.contains('hidden'),
    };
  }

  function makePopup({ searchable = false } = {}) {
    const popup = document.createElement('div');
    popup.className = 'laya-popup fixed z-[70] hidden overflow-hidden rounded-xl border border-slate-700 '
      + 'bg-slate-900 shadow-2xl shadow-black/50';
    popup.innerHTML = `
      ${searchable ? `
      <div class="laya-popup-search hidden items-center gap-2 border-b border-slate-800 px-2.5 py-2">
        ${icon('search', 'h-3.5 w-3.5 shrink-0 text-slate-500')}
        <input type="text" spellcheck="false" placeholder="Search…" aria-label="Filter options"
          class="min-w-0 flex-1 bg-transparent text-xs text-slate-200 outline-none placeholder:text-slate-600">
      </div>` : ''}
      <div class="laya-popup-list max-h-64 overflow-y-auto p-1" role="listbox" tabindex="-1"></div>
      <p class="laya-popup-empty hidden px-3 py-4 text-center text-[11px] text-slate-500">No matches.</p>`;
    return popup;
  }

  /** Anchor a fixed-position popup to a trigger, flipping above when it must. */
  function placePopup(popup, trigger) {
    const rect = trigger.getBoundingClientRect();
    const minWidth = Math.max(rect.width, 160);
    const left = Math.min(
      Math.max(8, rect.left),
      Math.max(8, window.innerWidth - minWidth - 8),
    );
    popup.style.left = `${left}px`;
    popup.style.minWidth = `${minWidth}px`;
    popup.style.maxWidth = `${Math.max(minWidth, window.innerWidth - left - 16)}px`;
    popup.style.top = '0px';
    const height = popup.getBoundingClientRect().height;
    const below = rect.bottom + 6;
    const above = rect.top - 6 - height;
    popup.style.top = `${below + height > window.innerHeight - 8 && above > 8 ? above : below}px`;
  }

  /** Upgrade one native <select>. Idempotent. */
  function enhanceSelect(select) {
    if (!(select instanceof HTMLSelectElement) || select.dataset.layaSelect) return;
    if (!select.parentNode) return; // nothing to wrap it in yet
    select.dataset.layaSelect = 'ready';

    const { measure, skin, hidden } = splitTokens(select);
    const describedBy = select.getAttribute('aria-label') || '';

    const wrap = document.createElement('div');
    wrap.className = ['laya-select relative', ...measure].join(' ');
    if (hidden) wrap.classList.add('hidden');

    const trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.className = ['flex w-full items-center justify-between gap-2 text-left', ...skin].join(' ');
    trigger.setAttribute('aria-haspopup', 'listbox');
    trigger.setAttribute('aria-expanded', 'false');
    if (describedBy) trigger.setAttribute('aria-label', describedBy);
    trigger.innerHTML = `<span data-value class="min-w-0 flex-1 truncate"></span>
      <span class="shrink-0 text-slate-500">${icon('chevron', 'h-3.5 w-3.5')}</span>`;

    const popup = makePopup({ searchable: true });
    const valueEl = trigger.querySelector('[data-value]');
    const searchWrap = popup.querySelector('.laya-popup-search');
    const searchInput = popup.querySelector('.laya-popup-search input');
    const list = popup.querySelector('.laya-popup-list');
    const empty = popup.querySelector('.laya-popup-empty');

    select.parentNode.insertBefore(wrap, select);
    wrap.appendChild(select);
    wrap.appendChild(trigger);
    wrap.appendChild(popup);
    select.classList.add('hidden');
    select.setAttribute('aria-hidden', 'true');
    select.tabIndex = -1;

    let visible = [];
    let active = -1;
    let open = false;

    const options = () => Array.from(select.options).map((option) => ({
      value: option.value,
      text: option.textContent.trim() || option.value,
      disabled: option.disabled,
    }));

    function syncTrigger() {
      const chosen = select.options[select.selectedIndex];
      const text = chosen ? chosen.textContent.trim() : '';
      valueEl.textContent = text || select.dataset.placeholder || '—';
      valueEl.classList.toggle('text-slate-500', !text);
      trigger.disabled = select.disabled;
    }

    function highlight() {
      list.querySelectorAll('[data-index]').forEach((node) => {
        const on = Number(node.dataset.index) === active;
        node.classList.toggle('bg-slate-800', on);
        node.classList.toggle('text-slate-100', on);
      });
      const node = list.querySelector(`[data-index="${active}"]`);
      if (node) {
        try { node.scrollIntoView({ block: 'nearest' }); } catch { /* not every host implements it */ }
      }
    }

    function renderList() {
      const query = searchInput.value.trim().toLowerCase();
      visible = options().filter((option) => !query || option.text.toLowerCase().includes(query));
      list.innerHTML = visible.map((option, index) => `
        <div role="option" data-index="${index}" aria-selected="${option.value === select.value}"
          class="flex items-center justify-between gap-2 rounded-lg px-2.5 py-1.5 text-xs
                 ${option.value === select.value ? 'bg-sky-500/15 text-sky-200' : 'text-slate-300'}
                 ${option.disabled ? 'cursor-not-allowed opacity-40' : 'cursor-pointer hover:bg-slate-800'}">
          <span class="min-w-0 flex-1 truncate">${escapeHtml(option.text)}</span>
          ${option.value === select.value ? icon('check', 'h-3.5 w-3.5 shrink-0') : ''}
        </div>`).join('');
      const none = visible.length === 0;
      list.classList.toggle('hidden', none);
      empty.classList.toggle('hidden', !none);
      active = visible.findIndex((option) => option.value === select.value);
      if (active < 0) active = visible.findIndex((option) => !option.disabled);
      highlight();
    }

    function openMenu() {
      if (open || trigger.disabled) return;
      open = true;
      trigger.setAttribute('aria-expanded', 'true');
      searchInput.value = '';
      searchWrap.classList.toggle('hidden', select.options.length < 9);
      searchWrap.classList.toggle('flex', select.options.length >= 9);
      renderList();
      popup.style.visibility = 'hidden';
      popup.classList.remove('hidden');
      placePopup(popup, trigger);
      popup.style.visibility = '';
      if (!searchWrap.classList.contains('hidden')) searchInput.focus();
    }

    function closeMenu(refocus = false) {
      if (!open) return;
      open = false;
      trigger.setAttribute('aria-expanded', 'false');
      popup.classList.add('hidden');
      if (refocus) trigger.focus();
    }

    function move(delta) {
      if (!visible.length) return;
      let next = active;
      for (let step = 0; step < visible.length; step += 1) {
        next = (next + delta + visible.length) % visible.length;
        if (!visible[next].disabled) break;
      }
      active = next;
      highlight();
    }

    function choose(index) {
      const option = visible[index];
      if (!option || option.disabled) return;
      const changed = option.value !== select.value;
      if (changed) select.value = option.value;
      closeMenu(true);
      if (changed) select.dispatchEvent(new Event('change', { bubbles: true }));
    }

    trigger.addEventListener('click', () => (open ? closeMenu() : openMenu()));
    trigger.addEventListener('keydown', (event) => {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        if (!open) openMenu(); else move(event.key === 'ArrowDown' ? 1 : -1);
      } else if (open && (event.key === 'Enter' || event.key === ' ')) {
        event.preventDefault();
        choose(active);
      } else if (event.key === 'Escape' && open) {
        event.preventDefault();
        closeMenu();
      }
    });

    list.addEventListener('click', (event) => {
      const node = event.target.closest('[data-index]');
      if (node) choose(Number(node.dataset.index));
    });
    list.addEventListener('mousemove', (event) => {
      const node = event.target.closest('[data-index]');
      if (!node) return;
      active = Number(node.dataset.index);
      highlight();
    });

    searchInput.addEventListener('input', renderList);
    searchInput.addEventListener('keydown', (event) => {
      if (event.key === 'ArrowDown') { event.preventDefault(); move(1); }
      else if (event.key === 'ArrowUp') { event.preventDefault(); move(-1); }
      else if (event.key === 'Home') { event.preventDefault(); active = 0; highlight(); }
      else if (event.key === 'End') { event.preventDefault(); active = visible.length - 1; highlight(); }
      else if (event.key === 'Enter') { event.preventDefault(); choose(active); }
      else if (event.key === 'Escape') { event.preventDefault(); closeMenu(true); }
      else if (event.key === 'Tab') { closeMenu(); }
    });

    document.addEventListener('keydown', (event) => {
      if (!open) return;
      if (event.key === 'Escape') { event.preventDefault(); closeMenu(true); }
      else if (event.key === 'ArrowDown') { event.preventDefault(); move(1); }
      else if (event.key === 'ArrowUp') { event.preventDefault(); move(-1); }
    });
    document.addEventListener('pointerdown', (event) => {
      if (open && !wrap.contains(event.target) && !popup.contains(event.target)) closeMenu();
    });
    const reposition = (event) => {
      if (!open) return;
      if (event && event.target && popup.contains(event.target)) return;
      placePopup(popup, trigger);
    };
    window.addEventListener('scroll', reposition, true);
    window.addEventListener('resize', reposition);

    // The native select stays authoritative: page code can keep assigning
    // `.value` or rewriting its options and the trigger follows.
    ['value', 'disabled'].forEach((property) => {
      const native = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, property);
      Object.defineProperty(select, property, {
        configurable: true,
        get() { return native.get.call(select); },
        set(next) { native.set.call(select, next); syncTrigger(); },
      });
    });
    select.classList.add('laya-select-source');
    new MutationObserver(() => {
      syncTrigger();
      if (open) renderList();
    }).observe(select, { childList: true, subtree: true, characterData: true });

    syncTrigger();
  }

  /** Upgrade a text input that points at a <datalist> into a popup combobox. */
  function enhanceDatalistInput(input) {
    if (!(input instanceof HTMLInputElement) || input.dataset.layaCombobox) return;
    const listId = input.getAttribute('list');
    if (!listId || !input.parentNode || !document.getElementById(listId)) return;
    input.dataset.layaCombobox = 'ready';
    // Drop the native association: otherwise both popups open at once.
    input.removeAttribute('list');
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('autocomplete', 'off');

    const { measure, skin, hidden } = splitTokens(input);
    const wrap = document.createElement('div');
    wrap.className = ['laya-select relative', ...measure].join(' ');
    if (hidden) wrap.classList.add('hidden');

    const popup = makePopup({ searchable: false });
    const list = popup.querySelector('.laya-popup-list');
    const empty = popup.querySelector('.laya-popup-empty');
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);
    wrap.appendChild(popup);
    input.className = skin.join(' ');

    let visible = [];
    let active = -1;
    let open = false;
    // Filtering only starts once the user types. Opening on focus shows every
    // suggestion, even when the field already holds one of them — otherwise a
    // pre-filled answer would hide all the alternatives.
    let filtering = false;

    function catalogue() {
      const datalist = document.getElementById(listId);
      return datalist ? Array.from(datalist.options).map((option) => option.value).filter(Boolean) : [];
    }

    function highlight() {
      list.querySelectorAll('[data-index]').forEach((node) => {
        const on = Number(node.dataset.index) === active;
        node.classList.toggle('bg-slate-800', on);
        node.classList.toggle('text-slate-100', on);
      });
    }

    function renderList() {
      const query = filtering ? input.value.trim().toLowerCase() : '';
      visible = catalogue().filter((value) => !query || value.toLowerCase().includes(query));
      list.innerHTML = visible.map((value, index) => `
        <div role="option" data-index="${index}" data-value="${escapeHtml(value)}"
          class="cursor-pointer truncate rounded-lg px-2.5 py-1.5 text-xs
                 ${value === input.value ? 'bg-sky-500/15 text-sky-200' : 'text-slate-300'} hover:bg-slate-800">
          ${escapeHtml(value)}
        </div>`).join('');
      const none = visible.length === 0;
      list.classList.toggle('hidden', none);
      empty.classList.toggle('hidden', !none);
      active = -1;
      highlight();
    }

    function openMenu() {
      const values = catalogue();
      if (open || values.length === 0) return;
      open = true;
      filtering = false;
      renderList();
      popup.style.visibility = 'hidden';
      popup.classList.remove('hidden');
      placePopup(popup, input);
      popup.style.visibility = '';
    }

    function closeMenu() {
      if (!open) return;
      open = false;
      popup.classList.add('hidden');
    }

    function pick(value) {
      input.value = value;
      closeMenu();
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
    }

    input.addEventListener('focus', openMenu);
    input.addEventListener('input', () => {
      filtering = true;
      if (open) renderList(); else openMenu();
    });
    input.addEventListener('blur', () => window.setTimeout(closeMenu, 120));
    input.addEventListener('keydown', (event) => {
      if (!open) {
        if (event.key === 'ArrowDown') { event.preventDefault(); openMenu(); }
        return;
      }
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        active = Math.min(active + 1, visible.length - 1);
        highlight();
      } else if (event.key === 'ArrowUp') {
        event.preventDefault();
        active = Math.max(active - 1, 0);
        highlight();
      } else if (event.key === 'Enter' && active >= 0) {
        event.preventDefault();
        pick(visible[active]);
      } else if (event.key === 'Escape') {
        event.preventDefault();
        closeMenu();
      }
    });
    list.addEventListener('mousedown', (event) => {
      const node = event.target.closest('[data-index]');
      if (!node) return;
      event.preventDefault();
      pick(node.dataset.value);
    });
  }

  /** Upgrade every dropdown inside ``root`` (and keep watching for new ones). */
  function enhanceSelects(root = document) {
    const scope = root instanceof Element ? root : document;
    if (scope instanceof HTMLSelectElement && !scope.dataset.layaSelect) enhanceSelect(scope);
    scope.querySelectorAll('select:not([data-laya-select])').forEach(enhanceSelect);
    if (scope instanceof HTMLInputElement) enhanceDatalistInput(scope);
    scope.querySelectorAll('input[list]:not([data-laya-combobox])').forEach(enhanceDatalistInput);
  }

  function watchDropdowns() {
    const scan = (records) => {
      records.forEach((record) => {
        record.addedNodes.forEach((node) => {
          if (node.nodeType === 1) enhanceSelects(node);
        });
      });
    };
    new MutationObserver(scan).observe(document.documentElement, { childList: true, subtree: true });
    enhanceSelects(document);
  }

  /* --------------------------------------------------------------- chips */

  /** A free-text field that turns entries into removable tags. */
  function chipsField(host, { values = [], placeholder = 'Add…', onChange } = {}) {
    let items = [...values];
    host.innerHTML = `
      <div class="laya-chips flex flex-wrap items-center gap-1.5 rounded-lg border border-slate-800 bg-slate-950 px-2 py-1.5
                  focus-within:border-sky-600">
        <span data-chips class="contents"></span>
        <input type="text" spellcheck="false" placeholder="${escapeHtml(placeholder)}"
          class="min-w-[7rem] flex-1 bg-transparent text-xs text-slate-200 outline-none placeholder:text-slate-600">
      </div>`;
    const box = host.querySelector('[data-chips]');
    const input = host.querySelector('input');

    function emit() {
      if (onChange) onChange([...items]);
    }

    function render() {
      box.innerHTML = items.map((value, index) => `
        <span class="inline-flex max-w-full items-center gap-1 rounded-md bg-slate-800 px-2 py-0.5 text-[11px] text-slate-200">
          <span class="truncate">${escapeHtml(value)}</span>
          <button type="button" data-remove="${index}" aria-label="Remove ${escapeHtml(value)}"
            class="text-slate-500 hover:text-rose-300">${icon('x', 'h-3 w-3')}</button>
        </span>`).join('');
    }

    function add(raw) {
      const value = String(raw || '').trim().replace(/,$/, '');
      if (!value) return;
      if (!items.includes(value)) items = [...items, value];
      render();
      emit();
    }

    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ',') {
        event.preventDefault();
        add(input.value);
        input.value = '';
      } else if (event.key === 'Backspace' && !input.value && items.length) {
        items = items.slice(0, -1);
        render();
        emit();
      }
    });
    input.addEventListener('blur', () => {
      if (input.value.trim()) { add(input.value); input.value = ''; }
    });
    box.addEventListener('click', (event) => {
      const node = event.target.closest('[data-remove]');
      if (!node) return;
      items.splice(Number(node.dataset.remove), 1);
      render();
      emit();
    });

    render();
    return {
      value: () => [...items],
      setValues(next) { items = [...(next || [])]; render(); },
      focus: () => input.focus(),
    };
  }

  /* ---------------------------------------------------------------- rows */

  const VALUE_TYPES = [
    { value: 'text', label: 'text' },
    { value: 'number', label: 'number' },
    { value: 'boolean', label: 'true / false' },
    { value: 'json', label: 'JSON' },
  ];

  function coerce(value, type) {
    if (type === 'number') {
      const parsed = Number(value);
      return value !== '' && Number.isFinite(parsed) ? parsed : value;
    }
    if (type === 'boolean') return value === 'true' || value === 'yes';
    if (type === 'json') {
      try { return JSON.parse(value); } catch { return value; }
    }
    return value;
  }

  function inferType(value) {
    if (typeof value === 'number') return 'number';
    if (typeof value === 'boolean') return 'boolean';
    if (value !== null && typeof value === 'object') return 'json';
    return 'text';
  }

  function displayValue(value, type) {
    if (value === null || value === undefined) return '';
    if (type === 'json') return JSON.stringify(value);
    // Last resort for a caller that pinned the type to text but loaded a
    // structure: show the data. `String({})` would print "[object Object]",
    // which reads as a bug instead of as the value it is.
    if (typeof value === 'object') return JSON.stringify(value);
    if (type === 'boolean') return value ? 'true' : 'false';
    return String(value);
  }

  /**
   * Normalise the rows a caller hands in.
   *
   * The pair editor takes ``{key, value, type?}`` rows; the list editor takes
   * plain values. ``type`` is optional on both, and a missing one is *inferred
   * from the loaded value* rather than assumed to be text — that is what keeps a
   * nested object or a number in a webhook payload from being flattened into
   * ``"[object Object]"`` and lost on the next save.
   */
  function pairRows(rows) {
    return (rows || []).map((row) => {
      const type = row.type || inferType(row.value);
      return { key: String(row.key ?? ''), value: displayValue(row.value, type), type };
    });
  }

  /** ``mode: 'list'`` takes the values themselves — nothing wraps them, so an
   *  object *value* is rendered as JSON rather than mistaken for a row. */
  function listRows(rows) {
    return (rows || []).map((value) => {
      const type = inferType(value);
      return { value: displayValue(value, type), type };
    });
  }

  function iconButton(name, label, extra = '') {
    return `<button type="button" data-action="${name}" aria-label="${escapeHtml(label)}" title="${escapeHtml(label)}"
      class="grid h-7 w-7 shrink-0 place-items-center rounded-md border border-slate-800 text-slate-500
             hover:border-slate-700 hover:text-slate-200 ${extra}">${icon(name)}</button>`;
  }

  function addButton(label, extra = '') {
    return `<button type="button" data-rows-action="add"
      class="inline-flex items-center gap-1 rounded-md border border-dashed border-slate-700 px-2 py-1 text-[11px]
             text-slate-400 hover:border-sky-600 hover:text-sky-300 ${extra}">
      ${icon('plus', 'h-3 w-3')} ${escapeHtml(label)}</button>`;
  }

  /**
   * A row control.
   *
   * Deliberately *not* ``data-action``: the rows editor is embedded inside cards
   * and script blocks that delegate their own buttons on ``[data-action]``, and
   * sharing the attribute meant a row's ✕ could be read as "delete the whole
   * question/block". The two namespaces keep the handlers from ever meeting.
   */
  function rowButton(name, label) {
    return `<button type="button" data-rows-action="${name}" aria-label="${escapeHtml(label)}" title="${escapeHtml(label)}"
      class="grid h-7 w-7 shrink-0 place-items-center rounded-md border border-slate-800 text-slate-500
             hover:border-slate-700 hover:text-slate-200">${icon(name)}</button>`;
  }

  /**
   * Structured row editor.
   *
   * ``mode: 'pairs'`` edits an object (``{key, value, type?}`` rows);
   * ``mode: 'list'`` edits an ordered array of plain values. ``typed`` adds the value-type
   * picker per row; without it the type is inferred from whatever was loaded,
   * so numbers, booleans and nested JSON survive a round trip.
   * Structural edits re-render; typing mutates the model in place so focus and
   * caret survive.
   */
  function rowsEditor(host, options = {}) {
    const mode = options.mode || 'pairs';
    const typed = Boolean(options.typed);
    const keyPlaceholder = options.keyPlaceholder || 'key';
    const valuePlaceholder = options.valuePlaceholder || 'value';
    const addLabel = options.addLabel || (mode === 'list' ? 'Add level' : 'Add field');
    const onChange = options.onChange;

    let rows = mode === 'list' ? listRows(options.rows) : pairRows(options.rows);

    function emit() {
      if (onChange) onChange(value());
    }

    function render(focusIndex = -1) {
      host.innerHTML = `
        <div data-rows class="space-y-1.5"></div>
        <div class="mt-1.5">${addButton(addLabel)}</div>`;
      const list = host.querySelector('[data-rows]');
      list.innerHTML = rows.length ? rows.map((row, index) => `
        <div class="flex items-start gap-1.5" data-row="${index}">
          ${mode === 'pairs' ? `
            <input data-field="key" value="${escapeHtml(row.key)}" placeholder="${escapeHtml(keyPlaceholder)}" spellcheck="false"
              class="w-[38%] min-w-0 rounded-lg border border-slate-800 bg-slate-950 px-2 py-1.5 font-mono text-[11px] text-slate-200 outline-none focus:border-sky-600">` : ''}
          <input data-field="value" value="${escapeHtml(row.value)}" placeholder="${escapeHtml(valuePlaceholder)}" spellcheck="false"
            class="min-w-0 flex-1 rounded-lg border border-slate-800 bg-slate-950 px-2 py-1.5 ${mode === 'pairs' && !typed ? 'font-mono' : ''} text-[11px] text-slate-200 outline-none focus:border-sky-600">
          ${typed && mode === 'pairs' ? `
            <select data-field="type" aria-label="Value type" class="shrink-0 rounded-lg border border-slate-800 bg-slate-950 px-2 py-1.5 text-[11px] text-slate-300 outline-none">
              ${VALUE_TYPES.map((type) => `<option value="${type.value}"${type.value === row.type ? ' selected' : ''}>${type.label}</option>`).join('')}
            </select>` : ''}
          <span class="flex shrink-0 items-center gap-1">
            ${mode === 'list' ? rowButton('up', 'Move up') + rowButton('down', 'Move down') : ''}
            ${rowButton('remove', 'Remove')}
          </span>
        </div>`).join('') : '<p class="px-1 py-2 text-[11px] text-slate-500">Nothing yet.</p>';
      enhanceSelects(host);
      if (focusIndex >= 0) {
        const field = host.querySelector(`[data-row="${focusIndex}"] [data-field="${mode === 'pairs' ? 'key' : 'value'}"]`);
        if (field) field.focus();
      }
    }

    host.addEventListener('input', (event) => {
      const row = event.target.closest('[data-row]');
      if (!row) return;
      const field = event.target.dataset.field;
      if (!field) return;
      const index = Number(row.dataset.row);
      const entry = rows[index];
      if (!entry) return;
      if (field === 'type') entry.type = event.target.value; else entry[field] = event.target.value;
      emit();
    });
    host.addEventListener('change', (event) => {
      const row = event.target.closest('[data-row]');
      if (!row || event.target.dataset.field !== 'type') return;
      const type = event.target.value;
      const entry = rows[Number(row.dataset.row)];
      let raw = entry.value;
      if (type === 'boolean') raw = /^(true|yes|1)$/i.test(String(raw).trim()) ? 'true' : 'false';
      entry.type = type;
      entry.value = raw;
      render(Number(row.dataset.row));
      emit();
    });
    host.addEventListener('click', (event) => {
      const node = event.target.closest('[data-rows-action]');
      if (!node) return;
      const action = node.dataset.rowsAction;
      const row = node.closest('[data-row]');

      if (action === 'add') {
        rows.push(mode === 'list' ? { value: '' } : { key: '', value: '', type: 'text' });
        render(rows.length - 1);
        emit();
        return;
      }
      if (!row) return;
      const index = Number(row.dataset.row);
      if (action === 'remove') {
        rows.splice(index, 1);
        render(Math.max(0, index - 1));
      } else if (action === 'up' && index > 0) {
        [rows[index - 1], rows[index]] = [rows[index], rows[index - 1]];
        render(index - 1);
      } else if (action === 'down' && index < rows.length - 1) {
        [rows[index + 1], rows[index]] = [rows[index], rows[index + 1]];
        render(index + 1);
      }
      emit();
    });

    function value() {
      if (mode === 'list') {
        return rows
          .map((row) => coerce(row.value, row.type))
          .filter((entry) => String(entry).trim() !== '');
      }
      const out = {};
      rows.forEach((row) => {
        const key = String(row.key || '').trim();
        if (!key) return;
        out[key] = coerce(row.value, row.type);
      });
      return out;
    }

    render();
    return {
      value,
      setRows(next) {
        rows = mode === 'list' ? listRows(next) : pairRows(next);
        render();
      },
      rerender: render,
    };
  }

  /* ----------------------------------------------------------- state box */

  const STATE_PLACEHOLDER = 'Subject: Duplicate charge on invoice #4411\n\nWe were billed twice and will cancel if this is not resolved.';

  /**
   * A Laya state: either free text or typed key/value fields. Both drafts are
   * kept, so flipping between the two modes never loses what you typed.
   */
  function stateEditor(host, { text = '', fields = [], placeholder = STATE_PLACEHOLDER, rows = 9, mode = 'text', onChange } = {}) {
    let currentMode = mode === 'fields' ? 'fields' : 'text';
    let fieldValues = Array.isArray(fields) ? fields : Object.entries(fields || {}).map(([key, value]) => ({ key, value, type: inferType(value) }));

    host.innerHTML = `
      <div class="flex items-center justify-between gap-2">
        <div class="flex gap-1 rounded-lg bg-slate-950/60 p-1" role="tablist" aria-label="State input mode">
          <button type="button" data-mode="text" role="tab" class="rounded-md px-2.5 py-1 text-[11px] font-medium"></button>
          <button type="button" data-mode="fields" role="tab" class="rounded-md px-2.5 py-1 text-[11px] font-medium"></button>
        </div>
        <span data-state-hint class="text-[11px] text-slate-500"></span>
      </div>
      <div data-state-text class="mt-2"></div>
      <div data-state-fields class="mt-2"></div>`;

    const tabs = host.querySelectorAll('[data-mode]');
    const textPane = host.querySelector('[data-state-text]');
    const fieldsPane = host.querySelector('[data-state-fields]');
    const hint = host.querySelector('[data-state-hint]');

    const area = document.createElement('textarea');
    area.rows = rows;
    area.spellcheck = false;
    area.placeholder = placeholder;
    area.className = 'w-full resize-y rounded-xl border border-slate-800 bg-slate-950 px-3 py-2.5 font-mono text-xs '
      + 'leading-relaxed text-slate-200 outline-none placeholder:text-slate-600 focus:border-sky-600';
    area.addEventListener('input', () => {
      describe();
      if (onChange) onChange(value());
    });
    textPane.appendChild(area);

    const fieldEditor = rowsEditor(fieldsPane, {
      mode: 'pairs',
      typed: true,
      keyPlaceholder: 'field (e.g. subject)',
      valuePlaceholder: 'value',
      addLabel: 'Add field',
      rows: fieldValues,
      onChange: () => { describe(); if (onChange) onChange(value()); },
    });

    function describe() {
      if (currentMode === 'text') {
        const raw = area.value.trim();
        let label = 'Plain text';
        if (raw.startsWith('{') || raw.startsWith('[')) {
          try { JSON.parse(raw); label = 'Structured JSON state'; } catch { label = 'Invalid JSON — sent as plain text'; }
        }
        hint.textContent = raw ? `${label} · ${raw.length} chars` : 'Empty';
        return;
      }
      const count = Object.keys(fieldEditor.value()).length;
      hint.textContent = count ? `${count} structured field${count === 1 ? '' : 's'}` : 'No fields yet';
    }

    function showMode(next) {
      currentMode = next;
      tabs.forEach((tab) => {
        const on = tab.dataset.mode === next;
        tab.className = `rounded-md px-2.5 py-1 text-[11px] font-medium ${on ? 'bg-slate-800 text-slate-100' : 'text-slate-400 hover:text-slate-200'}`;
        tab.setAttribute('aria-selected', String(on));
        tab.textContent = tab.dataset.mode === 'text' ? 'Free text' : 'Fields';
      });
      textPane.classList.toggle('hidden', next !== 'text');
      fieldsPane.classList.toggle('hidden', next !== 'fields');
      describe();
    }

    tabs.forEach((tab) => tab.addEventListener('click', () => showMode(tab.dataset.mode)));

    function value() {
      if (currentMode === 'text') {
        const raw = area.value.trim();
        if (!raw) return null;
        if (raw.startsWith('{') || raw.startsWith('[')) {
          try {
            const parsed = JSON.parse(raw);
            if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) return parsed;
          } catch { /* fall through to plain text */ }
        }
        return raw;
      }
      const fields = fieldEditor.value();
      return Object.keys(fields).length ? fields : null;
    }

    showMode(currentMode);
    return {
      value,
      text: () => area.value,
      setText(next) {
        area.value = next || '';
          showMode('text');
        describe();
      },
      setValue(next) {
        if (next && typeof next === 'object' && !Array.isArray(next)) {
          const entries = Object.entries(next).map(([key, value]) => ({ key, value, type: inferType(value) }));
          fieldEditor.setRows(entries);
          showMode(entries.length ? 'fields' : 'text');
        } else {
          area.value = next == null ? '' : String(next);
              showMode('text');
        }
        describe();
      },
      elements: { textarea: area },
      refresh: describe,
    };
  }

  /* ------------------------------------------------------------- exports */

  L.icon = icon;
  L.enhanceSelects = enhanceSelects;
  L.chipsField = chipsField;
  L.rowsEditor = rowsEditor;
  L.stateEditor = stateEditor;
  L.iconButton = iconButton;
  L.placePopup = placePopup;
  L.makePopup = makePopup;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', watchDropdowns, { once: true });
  } else {
    watchDropdowns();
  }
})();
