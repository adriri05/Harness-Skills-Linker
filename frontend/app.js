// Lógica de UI y llamadas a la API local (issues #10, #11 y #12).
// Todo texto que viene de la API son rutas y nombres del disco: se escribe
// siempre con textContent, nunca con innerHTML.
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const state = {
    harnesses: [],
    selected: new Set(),
    custom: [],
    scope: 'global',
    plan: null,        // último plan calculado
    planStale: false,  // la selección cambió después de calcularlo
    busy: false,
    stopped: false,
  };

  const RULES = [
    ['none', 'Sin regla (marcar conflictos)'],
    ['prefer_canonical', 'Preferir la carpeta canónica'],
    ['move_to_canonical', 'Mover a la carpeta canónica'],
  ];

  const ACTION_LABELS = {
    create_link: 'Crear enlace',
    already_linked: 'Ya enlazado',
    conflict: 'Conflicto',
    skipped: 'Omitido',
  };
  const ACTION_ORDER = ['create_link', 'conflict', 'already_linked', 'skipped'];
  const KIND_LABELS = { real: 'carpeta real', symlink: 'enlace', broken: 'enlace roto' };

  // ---------- utilidades ----------
  function h(tag, props, ...children) {
    const node = document.createElement(tag);
    if (props) {
      for (const [k, v] of Object.entries(props)) {
        if (v === null || v === undefined || v === false) continue;
        if (k === 'class') node.className = v;
        else if (k === 'text') node.textContent = v;
        else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
        else node.setAttribute(k, v === true ? '' : v);
      }
    }
    for (const c of children) {
      if (c === null || c === undefined || c === false) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }

  function plural(n, one, many) {
    return n + ' ' + (n === 1 ? one : many);
  }

  function token() {
    const meta = document.querySelector('meta[name="session-token"]');
    return meta ? meta.content : '';
  }

  async function api(path, body) {
    let res;
    try {
      res = await fetch(path, {
        method: 'POST',
        headers: { 'X-Session-Token': token(), 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {}),
      });
    } catch (e) {
      throw new Error('No se pudo conectar con el servidor local. Comprueba que sigue en marcha y recarga la página.');
    }
    let data = null;
    try { data = await res.json(); } catch (e) { /* cuerpo no JSON */ }
    if (!res.ok) {
      throw new Error((data && data.error) || 'El servidor respondió con el error ' + res.status + '.');
    }
    return data;
  }

  function showBanner(message, info) {
    const b = $('banner');
    b.textContent = message || '';
    b.className = info ? 'info' : '';
    b.hidden = !message;
  }

  function loadCustom() {
    try {
      const raw = localStorage.getItem('hsl-custom');
      const list = raw ? JSON.parse(raw) : [];
      if (Array.isArray(list)) {
        state.custom = list.filter((c) => c && typeof c.id === 'string' && typeof c.name === 'string' && typeof c.path === 'string');
      }
    } catch (e) { /* sin almacenamiento: solo memoria */ }
  }
  function saveCustom() {
    try { localStorage.setItem('hsl-custom', JSON.stringify(state.custom)); } catch (e) { /* ignorar */ }
  }

  function baseBody() {
    return {
      scope: state.scope,
      project_dir: state.scope === 'project' ? ($('project-dir').value.trim() || null) : null,
      custom: state.custom.length ? state.custom : null,
    };
  }

  function planBody() {
    return Object.assign(baseBody(), {
      harness_ids: Array.from(state.selected),
      canonical: $('canonical').value || null,
      conflict_rule: $('conflict-rule').value,
    });
  }

  function setBusy(busy) {
    state.busy = busy;
    for (const id of ['btn-scan', 'btn-plan']) $(id).disabled = busy || state.stopped;
    updateApply();
  }

  // ---------- harnesses (#10) ----------
  function harnessStatus(hn) {
    if (hn.detected) return ['Detectado', 'st-ok'];
    if (hn.dir_exists) return ['Carpeta sin binario', 'st-warn'];
    return ['No detectado', 'st-off'];
  }

  function renderHarnesses() {
    const box = $('harness-list');
    box.replaceChildren();
    if (!state.harnesses.length) {
      box.append(h('p', { class: 'muted', text: 'No hay harnesses para este alcance. Cambia el alcance, indica la ruta del proyecto o añade una carpeta personalizada.' }));
      return;
    }
    for (const hn of state.harnesses) {
      const cbId = 'sel-' + hn.id;
      const skills = hn.skills || [];
      const broken = skills.filter((s) => s.kind === 'broken').length;
      const [label, cls] = harnessStatus(hn);
      const cb = h('input', { type: 'checkbox', id: cbId, disabled: !hn.dir_exists, 'aria-label': 'Incluir ' + hn.name });
      cb.checked = state.selected.has(hn.id);
      cb.addEventListener('change', () => {
        if (cb.checked) state.selected.add(hn.id); else state.selected.delete(hn.id);
        onSelectionChange();
      });

      const meta = [plural(skills.length, 'skill', 'skills')];
      if (broken) meta.push(plural(broken, 'enlace roto', 'enlaces rotos'));
      if (hn.binary_found) meta.push('binario en ' + hn.binary_found);
      if (!hn.dir_exists) meta.push('la carpeta no existe');

      const card = h('div', { class: 'harness' },
        cb,
        h('label', { class: 'harness-name', for: cbId, text: hn.name }),
        h('span', { class: 'harness-status ' + cls, text: label }),
        h('div', { class: 'harness-path', text: hn.path }),
        h('div', { class: 'harness-meta', text: meta.join(' · ') }),
      );
      if (!hn.verified) {
        card.append(h('div', { class: 'harness-meta st-warn', text: 'Ruta sin verificar' + (hn.note ? ': ' + hn.note : '.') }));
      } else if (hn.note) {
        card.append(h('div', { class: 'harness-meta', text: hn.note }));
      }
      if (skills.length) {
        const list = h('ul', { class: 'skill-list' });
        for (const s of skills) {
          let detail = KIND_LABELS[s.kind] || s.kind;
          if (s.kind === 'symlink' && s.link_target) detail += ' hacia ' + s.link_target;
          if (!s.has_skill_md && s.kind !== 'broken') detail += ', sin SKILL.md';
          list.append(h('li', { title: s.path }, h('span', { class: 'skill-name', text: s.name }),
            h('span', { class: 'kind' + (s.kind === 'broken' ? ' st-bad' : ' muted'), text: detail })));
        }
        card.append(h('details', null, h('summary', { text: 'Skills (' + skills.length + ')' }), list));
      }
      box.append(card);
    }
  }

  function renderCanonical() {
    const sel = $('canonical');
    const prev = sel.value;
    sel.replaceChildren(h('option', { value: '', text: 'Ninguna' }));
    for (const hn of state.harnesses) {
      if (state.selected.has(hn.id)) sel.append(h('option', { value: hn.path, text: hn.name + ' (' + hn.path + ')' }));
    }
    if (Array.from(sel.options).some((o) => o.value === prev)) sel.value = prev;
    renderRules();
  }

  function renderRules() {
    const rule = $('conflict-rule');
    const prev = rule.value;
    rule.replaceChildren();
    for (const [v, t] of RULES) rule.append(h('option', { value: v, text: t }));
    for (const hn of state.harnesses) {
      if (state.selected.has(hn.id)) rule.append(h('option', { value: 'prefer_harness:' + hn.id, text: 'Preferir ' + hn.name }));
    }
    if (Array.from(rule.options).some((o) => o.value === prev)) rule.value = prev;
  }

  async function scan() {
    if (state.stopped) return;
    if (state.scope === 'project' && !$('project-dir').value.trim()) {
      state.harnesses = [];
      renderHarnesses();
      showBanner('Indica la ruta del proyecto para escanear su alcance.', true);
      return;
    }
    showBanner('');
    setBusy(true);
    try {
      const data = await api('/api/scan', baseBody());
      state.harnesses = data.harnesses || [];
      const ids = new Set(state.harnesses.map((x) => x.id));
      const keep = new Set(Array.from(state.selected).filter((id) => ids.has(id)));
      if (!keep.size) for (const hn of state.harnesses) if (hn.detected) keep.add(hn.id);
      state.selected = keep;
      renderHarnesses();
      onSelectionChange();
    } catch (e) {
      showBanner('No se pudo escanear: ' + e.message);
    } finally {
      setBusy(false);
    }
  }

  function onSelectionChange() {
    renderCanonical();
    invalidatePlan();
  }

  // ---------- carpetas personalizadas ----------
  function isAbsolute(p) {
    return p.startsWith('/') || p.startsWith('~') || /^[A-Za-z]:[\\/]/.test(p) || p.startsWith('\\\\');
  }

  function addCustom(ev) {
    ev.preventDefault();
    const name = $('custom-name').value.trim();
    const path = $('custom-path').value.trim();
    const err = $('custom-error');
    err.hidden = true;
    if (!name || !path) {
      err.textContent = 'Completa el nombre y la ruta de la carpeta.';
      err.hidden = false;
      return;
    }
    if (!isAbsolute(path)) {
      err.textContent = 'La ruta debe ser absoluta, por ejemplo /home/usuario/skills o C:\\skills.';
      err.hidden = false;
      return;
    }
    if (state.custom.some((c) => c.path === path)) {
      err.textContent = 'Esa carpeta ya está en la lista.';
      err.hidden = false;
      return;
    }
    const id = 'custom-' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
    state.custom.push({ id, name, path });
    state.selected.add(id);
    saveCustom();
    $('custom-form').reset();
    scan();
  }

  // ---------- plan (#11, #12) ----------
  function invalidatePlan() {
    if (state.plan) state.planStale = true;
    updateApply();
    const hint = $('apply-hint');
    if (state.planStale) {
      hint.textContent = 'La selección cambió después de calcular el plan. Calcula el plan de nuevo.';
      hint.hidden = false;
    }
  }

  function unresolvedConflicts(plan) {
    return plan.actions.filter((a) => a.action === 'conflict' && !a.resolution).length;
  }

  function updateApply() {
    const btn = $('btn-apply');
    const hint = $('apply-hint');
    let reason = '';
    if (state.stopped) reason = '';
    else if (!state.plan) reason = '';
    else if (state.planStale) reason = 'La selección cambió después de calcular el plan. Calcula el plan de nuevo.';
    else if (!state.plan.summary.create_link) reason = 'El plan no tiene enlaces por crear.';
    else if (unresolvedConflicts(state.plan)) reason = plural(unresolvedConflicts(state.plan), 'conflicto sin resolver', 'conflictos sin resolver') + '. Elige una regla de conflicto y calcula el plan de nuevo.';
    btn.disabled = state.busy || state.stopped || !state.plan || !!reason;
    hint.textContent = reason;
    hint.hidden = !reason;
  }

  function actionTable(actions) {
    const rows = actions.map((a) => h('tr', null,
      h('td', { text: a.skill }),
      h('td', { text: a.harness_id }),
      h('td', { class: 'path', text: a.target || '' }),
      h('td', { class: 'path', text: a.source || '' }),
      h('td', { text: [a.reason, a.resolution ? 'Resolución: ' + a.resolution : ''].filter(Boolean).join('. ') }),
    ));
    return h('table', null,
      h('thead', null, h('tr', null, ...['Skill', 'Harness', 'Destino', 'Origen', 'Motivo'].map((t) => h('th', { scope: 'col', text: t })))),
      h('tbody', null, ...rows));
  }

  function renderPlan() {
    const box = $('plan');
    box.replaceChildren();
    const plan = state.plan;
    if (!plan) return;
    if (!plan.actions.length) {
      box.append(h('p', { class: 'muted', text: 'No hay skills que enlazar entre los harnesses seleccionados. Selecciona al menos dos harnesses con skills.' }));
      return;
    }
    const sum = plan.summary;
    box.append(h('ul', { class: 'summary' },
      ...ACTION_ORDER.map((k) => h('li', { text: ACTION_LABELS[k] + ': ' + (sum[k] || 0) }))));
    if (plan.canonical) box.append(h('p', { class: 'muted', text: 'Carpeta canónica: ' + plan.canonical }));
    if (plan.warnings && plan.warnings.length) {
      box.append(h('ul', { class: 'warnings' }, ...plan.warnings.map((w) => h('li', { text: w }))));
    }
    for (const kind of ACTION_ORDER) {
      const group = plan.actions.filter((a) => a.action === kind);
      if (!group.length) continue;
      box.append(h('div', { class: 'group' },
        h('h3', { text: ACTION_LABELS[kind] + ' (' + group.length + ')' }),
        actionTable(group)));
    }
  }

  async function calcPlan() {
    if (state.selected.size < 1) {
      showBanner('Selecciona al menos un harness para calcular el plan.');
      return;
    }
    const rule = $('conflict-rule').value;
    if ((rule === 'prefer_canonical' || rule === 'move_to_canonical') && !$('canonical').value) {
      showBanner('La regla elegida necesita una carpeta canónica. Selecciónala o cambia la regla.');
      return;
    }
    showBanner('');
    setBusy(true);
    try {
      const plan = await api('/api/plan', planBody());
      plan.actions = plan.actions || [];
      plan.summary = plan.summary || {};
      state.plan = plan;
      state.planStale = false;
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
    $('confirm-text').textContent = 'Las skills que son carpetas reales se moverán a ' + $('canonical').value +
      ' y se sustituirán por enlaces. Comprueba el plan antes de continuar.';
    if (typeof dlg.showModal !== 'function') return Promise.resolve(window.confirm($('confirm-text').textContent));
    return new Promise((resolve) => {
      dlg.addEventListener('close', () => resolve(dlg.returnValue === 'ok'), { once: true });
      dlg.returnValue = 'cancel';
      dlg.showModal();
    });
  }

  async function applyPlan() {
    if ($('conflict-rule').value === 'move_to_canonical' && !(await confirmMove())) return;
    showBanner('');
    setBusy(true);
    try {
      const data = await api('/api/apply', planBody());
      renderResults(data);
      // Tras aplicar, el plan anterior ya no describe el estado del disco.
      state.planStale = true;
      scan();
    } catch (e) {
      showBanner('No se pudieron aplicar los enlaces: ' + e.message);
    } finally {
      setBusy(false);
    }
  }

  function renderResults(data) {
    const sec = $('results-section');
    const box = $('results');
    box.replaceChildren();
    const results = data.results || [];
    const sum = data.summary || {};
    box.append(h('ul', { class: 'summary' },
      h('li', { class: 'st-ok', text: 'Correctos: ' + (sum.ok || 0) }),
      h('li', { class: sum.error ? 'st-bad' : '', text: 'Con error: ' + (sum.error || 0) }),
      h('li', { text: 'Omitidos: ' + (sum.skipped || 0) })));
    if (!results.length) {
      box.append(h('p', { class: 'muted', text: 'El servidor no aplicó ninguna acción.' }));
    } else {
      const rows = results.map((r) => h('tr', null,
        h('td', { class: r.status === 'ok' ? 'st-ok' : r.status === 'error' ? 'st-bad' : 'muted', text: r.status === 'ok' ? 'Correcto' : r.status === 'error' ? 'Error' : 'Omitido' }),
        h('td', { text: r.skill }),
        h('td', { text: r.harness_id }),
        h('td', { class: 'path', text: r.target || '' }),
        h('td', { text: r.message || '' })));
      box.append(h('table', null,
        h('thead', null, h('tr', null, ...['Estado', 'Skill', 'Harness', 'Destino', 'Detalle'].map((t) => h('th', { scope: 'col', text: t })))),
        h('tbody', null, ...rows)));
    }
    if (sum.error) {
      box.append(h('p', { class: 'st-bad', text: 'Algunos enlaces fallaron. Revisa el detalle de cada fila; en Windows crear enlaces simbólicos requiere el Modo Desarrollador o permisos de administrador.' }));
    }
    sec.hidden = false;
    sec.scrollIntoView({ block: 'nearest' });
  }

  // ---------- servidor ----------
  async function shutdown() {
    if (!window.confirm('Se detendrá el servidor local. ¿Continuar?')) return;
    try {
      await api('/api/shutdown', {});
    } catch (e) {
      showBanner('No se pudo detener el servidor: ' + e.message);
      return;
    }
    state.stopped = true;
    for (const el of document.querySelectorAll('button, input, select')) el.disabled = true;
    showBanner('El servidor se detuvo. Ya puedes cerrar esta pestaña.', true);
  }

  // ---------- arranque ----------
  function init() {
    const rule = $('conflict-rule');
    loadCustom();

    document.querySelectorAll('input[name="scope"]').forEach((r) => r.addEventListener('change', () => {
      state.scope = r.value;
      $('project-row').hidden = state.scope !== 'project';
      state.selected = new Set();
      scan();
    }));
    $('project-dir').addEventListener('change', () => { state.selected = new Set(); scan(); });
    $('canonical').addEventListener('change', invalidatePlan);
    rule.addEventListener('change', invalidatePlan);
    $('custom-form').addEventListener('submit', addCustom);
    $('btn-scan').addEventListener('click', scan);
    $('btn-plan').addEventListener('click', calcPlan);
    $('btn-apply').addEventListener('click', applyPlan);
    $('btn-shutdown').addEventListener('click', shutdown);
    renderCanonical();
    scan();
  }

  init();
})();
