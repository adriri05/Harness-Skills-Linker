// Pestaña de archivos de instrucciones (issue #33). Usa los ayudantes de app.js.
// Todo texto que viene de la API son rutas y nombres del disco: siempre con textContent.
(function () {
  'use strict';
  const { $, h, plural, api, showBanner, state: app, actionTable, renderResults, refreshHistory } = window.HSL;

  const state = { items: [], selected: new Set(), plan: null, stale: false, busy: false };

  const RULES = [
    ['none', 'Solo marcar conflictos'],
    ['prefer_canonical', 'Preferir el archivo canónico'],
    ['move_to_canonical', 'Mover duplicados al canónico'],
    ['move_all_to_canonical', 'Mover todos al canónico'],
  ];
  const ACTION_LABELS = { create_link: 'Crear enlace', already_linked: 'Ya enlazado', conflict: 'Conflicto', skipped: 'Omitido' };
  const ACTION_ORDER = ['create_link', 'conflict', 'already_linked', 'skipped'];
  const KIND_LABELS = {
    real: 'archivo real', symlink: 'enlace', broken: 'enlace roto', invalid: 'no es un archivo',
    missing: 'no existe', none: 'sin archivo en este alcance',
  };

  function scopeBody() {
    const b = window.HSL.baseBody();
    return { scope: b.scope, project_dir: b.project_dir };
  }

  function planBody() {
    return Object.assign(scopeBody(), {
      harness_ids: Array.from(state.selected),
      canonical: $('instr-canonical').value || null,
      conflict_rule: $('instr-rule').value,
    });
  }

  function usable(item) { return !!item.path; }

  function kindText(item) {
    let t = KIND_LABELS[item.kind] || item.kind;
    if (item.kind === 'symlink' && item.link_target) t += ' hacia ' + item.link_target;
    if (item.kind === 'real' && item.hardlinked) t += ' (enlace duro)';
    if (item.kind === 'invalid' && item.detail) t = item.detail;
    return t;
  }

  function renderList() {
    const box = $('instr-list');
    box.replaceChildren();
    box.classList.toggle('harness-scroll', state.items.length > 0);
    if (!state.items.length) {
      box.append(h('p', { class: 'muted harness-empty', text: 'Indica la ruta del proyecto para ver sus archivos de instrucciones.' }));
      return;
    }
    for (const it of state.items) {
      const cb = h('input', { type: 'checkbox', 'aria-label': 'Incluir ' + it.name, disabled: !usable(it) });
      cb.checked = state.selected.has(it.id);
      cb.addEventListener('change', () => {
        if (cb.checked) state.selected.add(it.id); else state.selected.delete(it.id);
        onSelectionChange();
      });
      const body = h('div', null,
        h('div', { class: 'instr-head' },
          h('span', { class: 'harness-name', text: it.name }),
          h('span', { class: 'harness-count', text: kindText(it) }),
          it.detected ? h('span', { class: 'harness-status st-ok', text: 'Detectado' }) : null),
        it.path ? h('div', { class: 'harness-path', text: it.path }) : null);
      if (it.note) body.append(h('div', { class: 'harness-meta', text: it.note }));
      if (it.source) {
        body.append(h('div', { class: 'harness-meta' }, 'Fuente: ', h('a', { href: it.source, target: '_blank', rel: 'noreferrer noopener', text: it.source })));
      }
      box.append(h('div', { class: 'instr-row' }, cb, body));
    }
  }

  function renderOptions() {
    const canon = $('instr-canonical');
    const prevCanon = canon.value;
    canon.replaceChildren(h('option', { value: '', text: 'Ninguno' }));
    const rule = $('instr-rule');
    const prevRule = rule.value;
    rule.replaceChildren(...RULES.map(([v, t]) => h('option', { value: v, text: t })));
    for (const it of state.items) {
      if (!state.selected.has(it.id) || !usable(it)) continue;
      canon.append(h('option', { value: it.id, text: it.name + ' (' + it.path + ')' }));
      rule.append(h('option', { value: 'prefer_harness:' + it.id, text: 'Preferir ' + it.name }));
    }
    if (Array.from(canon.options).some((o) => o.value === prevCanon)) canon.value = prevCanon;
    if (Array.from(rule.options).some((o) => o.value === prevRule)) rule.value = prevRule;
  }

  function onSelectionChange() {
    renderOptions();
    invalidate();
  }

  function invalidate() {
    if (state.plan) state.stale = true;
    updateApply();
  }

  function unresolved(plan) {
    return plan.actions.filter((a) => a.action === 'conflict' && !a.resolution).length;
  }

  function updateApply() {
    const btn = $('btn-instr-apply');
    const hint = $('instr-hint');
    let reason = '';
    const plan = state.plan;
    if (!plan || app.stopped) reason = '';
    else if (state.stale) reason = 'La selección cambió. Calcula el plan de nuevo.';
    else if (!plan.summary.create_link) reason = 'No hay enlaces que crear.';
    else if (unresolved(plan)) reason = plural(unresolved(plan), 'conflicto sin resolver', 'conflictos sin resolver') + '. Elige una regla y calcula el plan de nuevo.';
    btn.disabled = state.busy || app.stopped || !plan || !!reason;
    hint.textContent = reason;
    hint.hidden = !reason;
  }

  function setBusy(busy) {
    state.busy = busy;
    for (const id of ['btn-instr-scan', 'btn-instr-plan']) $(id).disabled = busy || app.stopped;
    updateApply();
  }

  async function scan() {
    if (app.stopped) return;
    const b = scopeBody();
    if (b.scope === 'project' && !b.project_dir) {
      state.items = [];
      state.selected = new Set();
      state.plan = null;
      $('instr-plan').replaceChildren();
      renderList();
      renderOptions();
      updateApply();
      return;
    }
    try {
      const data = await api('/api/instructions/scan', b);
      state.items = data.harnesses || [];
      const ids = new Set(state.items.filter(usable).map((x) => x.id));
      const keep = new Set(Array.from(state.selected).filter((id) => ids.has(id)));
      if (!keep.size) for (const it of state.items) if (usable(it) && it.kind !== 'missing' && it.kind !== 'none') keep.add(it.id);
      state.selected = keep;
      renderList();
      onSelectionChange();
    } catch (e) {
      showBanner('No se pudieron leer los archivos de instrucciones: ' + e.message);
    }
  }

  function renderPlan() {
    const box = $('instr-plan');
    box.replaceChildren();
    const plan = state.plan;
    if (!plan) return;
    if (plan.warnings && plan.warnings.length) {
      box.append(h('ul', { class: 'warnings' }, ...plan.warnings.map((w) => h('li', { text: w }))));
    }
    if (!plan.actions.length) {
      box.append(h('p', { class: 'muted', text: 'No hay archivos que enlazar. Selecciona harnesses con archivo de instrucciones en este alcance.' }));
      return;
    }
    box.append(h('ul', { class: 'summary' },
      ...ACTION_ORDER.map((k) => h('li', { text: ACTION_LABELS[k] + ': ' + (plan.summary[k] || 0) }))));
    if (plan.canonical) box.append(h('p', { class: 'muted', text: 'Archivo canónico: ' + plan.canonical }));
    for (const kind of ACTION_ORDER) {
      const group = plan.actions.filter((a) => a.action === kind);
      if (!group.length) continue;
      box.append(h('div', { class: 'group' },
        h('h3', { text: ACTION_LABELS[kind] + ' (' + group.length + ')' }),
        actionTable(group, true, 'Archivo')));
    }
  }

  async function calcPlan() {
    if (state.selected.size < 1) {
      showBanner('Selecciona al menos un harness para calcular el plan.');
      return;
    }
    const rule = $('instr-rule').value;
    if (['prefer_canonical', 'move_to_canonical', 'move_all_to_canonical'].includes(rule) && !$('instr-canonical').value) {
      showBanner('Esta regla necesita un archivo canónico. Elige uno o cambia la regla.');
      return;
    }
    showBanner('');
    setBusy(true);
    try {
      const plan = await api('/api/instructions/plan', planBody());
      plan.actions = plan.actions || [];
      plan.summary = plan.summary || {};
      state.plan = plan;
      state.stale = false;
      $('results-section').hidden = true;
      renderPlan();
    } catch (e) {
      showBanner('No se pudo calcular el plan: ' + e.message);
    } finally {
      setBusy(false);
    }
  }

  function confirmMove() {
    const dlg = $('confirm-dialog');
    $('confirm-text').textContent = 'Los archivos reales se moverán a ' + (state.plan && state.plan.canonical) +
      ' y se sustituirán por enlaces.';
    if (typeof dlg.showModal !== 'function') return Promise.resolve(window.confirm($('confirm-text').textContent));
    return new Promise((resolve) => {
      dlg.addEventListener('close', () => resolve(dlg.returnValue === 'ok'), { once: true });
      dlg.returnValue = 'cancel';
      dlg.showModal();
    });
  }

  async function applyPlan() {
    if (['move_to_canonical', 'move_all_to_canonical'].includes($('instr-rule').value) && !(await confirmMove())) return;
    showBanner('');
    setBusy(true);
    try {
      const data = await api('/api/instructions/apply', planBody());
      renderResults(data);
      state.stale = true;
      await refreshHistory();
      window.HSL.changed();
    } catch (e) {
      showBanner('No se pudieron aplicar los enlaces: ' + e.message);
    } finally {
      setBusy(false);
    }
  }

  $('btn-instr-scan').addEventListener('click', scan);
  $('btn-instr-plan').addEventListener('click', calcPlan);
  $('btn-instr-apply').addEventListener('click', applyPlan);
  $('instr-canonical').addEventListener('change', invalidate);
  $('instr-rule').addEventListener('change', invalidate);
  window.HSL.on('busy', (busy) => { if (busy !== state.busy) setBusy(busy); });
  window.HSL.on('change', scan);
  renderOptions();
  scan();
})();
