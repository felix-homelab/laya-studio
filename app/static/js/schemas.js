/* Schema library: list, create, edit, lint, fork, revisions. */

(() => {
  const { api, toast, escapeHtml, debounce } = window.LayaStudio;

  const list = document.getElementById('schema-list');
  const search = document.getElementById('schema-search');
  const showTemplates = document.getElementById('show-templates');

  const els = {
    title: document.getElementById('editor-title'),
    subtitle: document.getElementById('editor-subtitle'),
    revision: document.getElementById('editor-revision'),
    name: document.getElementById('editor-name'),
    tags: document.getElementById('editor-tags'),
    description: document.getElementById('editor-description'),
    lint: document.getElementById('editor-lint'),
    save: document.getElementById('editor-save'),
    saveLabel: document.getElementById('editor-save-label'),
    fork: document.getElementById('editor-fork'),
    del: document.getElementById('editor-delete'),
    revisionsPanel: document.getElementById('revisions-panel'),
    revisionsList: document.getElementById('revisions-list'),
  };

  const questions = window.LayaStudio.questionEditor(document.getElementById('editor-questions'), {
    questions: { decision: { type: 'choice', instructions: 'Which category does this belong to?', criteria: { first: '', second: '' } } },
    onChange: () => lintLater(),
  });

  let editing = null; // null = creating

  const lintLater = debounce(lint, 400);

  async function lint() {
    const payload = questions.questions();
    if (!Object.keys(payload).length) {
      els.lint.innerHTML = '<p class="text-[11px] text-rose-300">A schema needs at least one question.</p>';
      return false;
    }
    try {
      const result = await api('/api/playground/lint', {
        method: 'POST',
        body: JSON.stringify({ questions: payload }),
      });
      els.lint.innerHTML = result.findings.length
        ? result.findings.map((f) => `
            <p class="text-[11px] ${f.severity === 'error' ? 'text-rose-300' : 'text-amber-300'}">
              <span class="font-semibold">${f.severity}</span>
              ${f.question ? `<span class="font-mono text-slate-400">${escapeHtml(f.question)}</span>: ` : ''}
              ${escapeHtml(f.message)}</p>`).join('')
        : '<p class="text-[11px] text-emerald-300">Schema looks clean.</p>';
      return result.ok;
    } catch (error) {
      els.lint.innerHTML = `<p class="text-[11px] text-rose-300">${escapeHtml(error.message)}</p>`;
      return false;
    }
  }

  async function refreshList() {
    const params = new URLSearchParams();
    if (search.value.trim()) params.set('search', search.value.trim());
    params.set('include_templates', showTemplates.checked ? 'true' : 'false');
    try {
      const rows = await api(`/api/schemas?${params}`);
      list.innerHTML = rows.length
        ? rows.map((schema) => `
          <button type="button" data-schema-id="${schema.id}"
            class="rounded-2xl border ${editing && editing.id === schema.id ? 'border-sky-600' : 'border-slate-800'} bg-slate-900/60 p-4 text-left transition hover:border-slate-700">
            <div class="flex items-start justify-between gap-3">
              <div class="min-w-0">
                <p class="truncate text-sm font-semibold text-slate-100">${escapeHtml(schema.name)}</p>
                <p class="mt-0.5 font-mono text-[11px] text-slate-500">${escapeHtml(schema.key)}</p>
              </div>
              ${schema.is_template ? '<span class="shrink-0 rounded-full bg-indigo-500/15 px-2 py-0.5 text-[11px] text-indigo-300">template</span>' : ''}
            </div>
            <p class="mt-2 line-clamp-2 text-xs text-slate-400">${escapeHtml(schema.description || 'No description.')}</p>
            <div class="mt-3 flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
              <span class="rounded-full bg-slate-700/40 px-2 py-0.5 text-slate-300">${escapeHtml(schema.primitive)}</span>
              <span>r${schema.current_revision}</span>
              ${(schema.tags || []).slice(0, 3).map((t) => `<span class="text-slate-600">#${escapeHtml(t)}</span>`).join('')}
            </div>
          </button>`).join('')
        : '<p class="md:col-span-2 rounded-xl border border-dashed border-slate-800 px-4 py-10 text-center text-xs text-slate-500">No schemas match.</p>';

      list.querySelectorAll('[data-schema-id]').forEach((node) => {
        node.addEventListener('click', () => open(Number(node.dataset.schemaId)));
      });
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  function resetEditor() {
    editing = null;
    els.title.textContent = 'New schema';
    els.subtitle.textContent = 'Each question is a card: name it, pick a primitive, describe the criteria.';
    els.revision.innerHTML = '';
    els.name.value = '';
    els.tags.value = '';
    els.description.value = '';
    questions.setQuestions({
      decision: { type: 'choice', instructions: 'Which category does this belong to?', criteria: { first: '', second: '' } },
    });
    els.saveLabel.textContent = 'Create schema';
    els.fork.classList.add('hidden');
    els.del.classList.add('hidden');
    els.revisionsPanel.classList.add('hidden');
    lint();
    refreshList();
  }

  async function open(schemaId) {
    try {
      const schema = await api(`/api/schemas/${schemaId}`);
      editing = schema;
      els.title.textContent = schema.name;
      els.subtitle.textContent = schema.is_template
        ? 'Built-in template. Fork it to make an editable copy.'
        : 'Editing mints a new immutable revision when the questions change.';
      els.revision.innerHTML = `<span class="rounded-full bg-slate-700/40 px-2 py-0.5 text-[11px] text-slate-300">r${schema.current_revision}</span>`;
      els.name.value = schema.name;
      els.tags.value = (schema.tags || []).join(', ');
      els.description.value = schema.description || '';
      questions.setQuestions(schema.definition.questions);
      els.saveLabel.textContent = schema.is_template ? 'Fork to edit' : 'Save changes';
      els.fork.classList.remove('hidden');
      els.del.classList.toggle('hidden', schema.is_template);
      els.revisionsPanel.classList.remove('hidden');
      lint();
      loadRevisions(schema.id);
      refreshList();
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  async function loadRevisions(schemaId) {
    try {
      const rows = await api(`/api/schemas/${schemaId}/revisions`);
      els.revisionsList.innerHTML = rows.map((rev) => `
        <div class="flex items-start justify-between gap-3 px-3 py-2.5">
          <div class="min-w-0">
            <p class="text-xs text-slate-300">r${rev.revision}</p>
            <p class="mt-0.5 truncate text-[11px] text-slate-500">${escapeHtml(rev.note || '')}</p>
          </div>
          <span class="shrink-0 text-[11px] text-slate-500">${escapeHtml(rev.created_at.slice(0, 16).replace('T', ' '))}</span>
        </div>`).join('');
    } catch (error) {
      els.revisionsList.innerHTML = `<p class="px-3 py-3 text-[11px] text-rose-300">${escapeHtml(error.message)}</p>`;
    }
  }

  function payloadFromEditor() {
    const bundle = questions.questions();
    if (!Object.keys(bundle).length) { toast('Add at least one question', 'error'); return null; }
    if (!els.name.value.trim()) { toast('Give the schema a name', 'warn'); return null; }
    return {
      name: els.name.value.trim(),
      description: els.description.value.trim(),
      tags: els.tags.value.split(',').map((t) => t.trim()).filter(Boolean),
      questions: bundle,
    };
  }

  els.save.addEventListener('click', async () => {
    const payload = payloadFromEditor();
    if (!payload) return;

    const ok = await lint();
    if (!ok) { toast('Fix the lint errors before saving', 'warn'); return; }

    try {
      if (!editing) {
        const created = await api('/api/schemas', { method: 'POST', body: JSON.stringify(payload) });
        toast(`Created “${created.name}”`, 'success');
        await open(created.id);
      } else if (editing.is_template) {
        const forked = await api(
          `/api/schemas/${editing.id}/fork?name=${encodeURIComponent(payload.name)}`,
          { method: 'POST' },
        );
        await api(`/api/schemas/${forked.id}`, {
          method: 'PUT',
          body: JSON.stringify({ ...payload, note: `Forked from ${editing.key}` }),
        });
        toast(`Forked into “${payload.name}”`, 'success');
        await open(forked.id);
      } else {
        const updated = await api(`/api/schemas/${editing.id}`, {
          method: 'PUT',
          body: JSON.stringify({ ...payload, note: 'Edited via the schema library' }),
        });
        toast(`Saved — now at r${updated.current_revision}`, 'success');
        await open(updated.id);
      }
      refreshList();
    } catch (error) {
      toast(error.message, 'error', 6000);
    }
  });

  els.del.addEventListener('click', async () => {
    if (!editing || editing.is_template) return;
    if (!window.confirm(`Delete “${editing.name}”? Evaluations keep their captured questions.`)) return;
    try {
      await api(`/api/schemas/${editing.id}`, { method: 'DELETE' });
      toast('Schema deleted', 'success');
      resetEditor();
    } catch (error) {
      toast(error.message, 'error');
    }
  });

  els.fork.addEventListener('click', async () => {
    if (!editing) return;
    const name = window.prompt('Name for the forked schema:', `${editing.name} (copy)`);
    if (!name) return;
    try {
      const forked = await api(`/api/schemas/${editing.id}/fork?name=${encodeURIComponent(name)}`, { method: 'POST' });
      toast(`Forked into “${forked.name}”`, 'success');
      await open(forked.id);
    } catch (error) {
      toast(error.message, 'error');
    }
  });

  document.getElementById('new-schema').addEventListener('click', resetEditor);
  search.addEventListener('input', debounce(refreshList, 250));
  showTemplates.addEventListener('change', refreshList);

  resetEditor();
})();
