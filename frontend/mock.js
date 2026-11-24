// Cableado de desarrollo: responde a /api/* con datos de ejemplo coherentes con
// el contrato. Solo se activa con ?mock=1 o si el token sigue siendo el literal
// __SESSION_TOKEN__ (index.html abierto sin pasar por el servidor). Con el
// servidor real, este archivo no hace nada.
(function () {
  'use strict';
  const meta = document.querySelector('meta[name="session-token"]');
  const active = /[?&]mock=1(&|$)/.test(location.search) || (meta && meta.content === '__SESSION_TOKEN__');
  if (!active) return;

  const sk = (name, kind, extra) => Object.assign({
    name, kind, path: '/mock/' + name, real_path: kind === 'real' ? '/mock/' + name : null,
    link_target: null, has_skill_md: true,
  }, extra || {});

  const DATA = [
    { id: 'agents', name: 'Agents (compartido)', scope: 'global', path: '/home/demo/.agents/skills', dir_exists: true,
      binary_found: null, verified: true, note: null, detected: true,
      skills: [sk('revisar-codigo', 'real'), sk('escribir-tests', 'real'), sk('sin-manifiesto', 'real', { has_skill_md: false })] },
    { id: 'claude-code', name: 'Claude Code', scope: 'global', path: '/home/demo/.claude/skills', dir_exists: true,
      binary_found: '/usr/local/bin/claude', verified: true, note: null, detected: true,
      skills: [sk('revisar-codigo', 'symlink', { link_target: '/home/demo/.agents/skills/revisar-codigo', real_path: '/home/demo/.agents/skills/revisar-codigo' }),
        sk('depurar', 'real'), sk('antigua', 'broken', { link_target: '/home/demo/borrada/antigua' })] },
    { id: 'cursor', name: 'Cursor', scope: 'global', path: '/home/demo/.cursor/skills', dir_exists: true,
      binary_found: null, verified: false, note: 'La ruta de skills de Cursor no está confirmada en su documentación.', detected: false,
      skills: [sk('depurar', 'real')] },
    { id: 'codex', name: 'Codex', scope: 'global', path: '/home/demo/.codex/skills', dir_exists: false,
      binary_found: null, verified: true, note: null, detected: false, skills: [] },
  ];

  function scanData(body) {
    const list = DATA.map((x) => Object.assign({}, x, { scope: body.scope }));
    for (const c of body.custom || []) {
      list.push({ id: c.id, name: c.name, scope: body.scope, path: c.path, dir_exists: true, binary_found: null,
        verified: true, note: null, detected: true, skills: [sk('de-' + c.name, 'real')] });
    }
    return { harnesses: list };
  }

  function planData(body) {
    const chosen = scanData(body).harnesses.filter((x) => body.harness_ids.indexOf(x.id) >= 0);
    const actions = [];
    const names = new Set();
    chosen.forEach((x) => x.skills.forEach((s) => names.add(s.name)));
    names.forEach((name) => {
      const holders = chosen.filter((x) => x.skills.some((s) => s.name === name));
      const reals = holders.filter((x) => x.skills.find((s) => s.name === name).kind === 'real');
      chosen.forEach((x) => {
        const own = x.skills.find((s) => s.name === name);
        const source = reals.length ? reals[0].path + '/' + name : null;
        const a = { skill: name, harness_id: x.id, target: x.path + '/' + name, source, reason: '', resolution: null };
        if (own && own.kind === 'broken') Object.assign(a, { action: 'skipped', reason: 'Enlace roto, repáralo a mano antes de enlazar.' });
        else if (own && own.kind === 'symlink') Object.assign(a, { action: 'already_linked', reason: 'Ya apunta al origen.' });
        else if (own && reals.length > 1) {
          Object.assign(a, { action: 'conflict', reason: 'Carpeta real en ' + reals.length + ' harnesses.' });
          if (body.conflict_rule !== 'none') a.resolution = body.conflict_rule;
        } else if (own) Object.assign(a, { action: 'skipped', reason: 'Es el origen.' });
        else if (!x.dir_exists) Object.assign(a, { action: 'skipped', reason: 'La carpeta del harness no existe.' });
        else if (source) Object.assign(a, { action: 'create_link', reason: 'Falta en este harness.' });
        else return;
        actions.push(a);
      });
    });
    const summary = { create_link: 0, already_linked: 0, conflict: 0, skipped: 0 };
    actions.forEach((a) => { summary[a.action] += 1; });
    return { actions, summary, canonical: body.canonical || null,
      warnings: chosen.some((x) => !x.verified) ? ['Hay harnesses con ruta sin verificar.'] : [] };
  }

  function applyData(body) {
    const plan = planData(body);
    let failed = false;
    const results = plan.actions.map((a) => {
      if (a.action !== 'create_link') return Object.assign({}, a, { status: 'skipped', message: 'Sin cambios.' });
      if (!failed) {
        failed = true;
        return Object.assign({}, a, { status: 'error', message: 'Permiso denegado al crear el enlace simbólico.' });
      }
      return Object.assign({}, a, { status: 'ok', message: 'Enlace creado.' });
    });
    const summary = { ok: 0, error: 0, skipped: 0 };
    results.forEach((r) => { summary[r.status] += 1; });
    return { results, summary };
  }

  const realFetch = window.fetch.bind(window);
  window.fetch = function (url, opts) {
    const path = typeof url === 'string' ? url : url.url;
    if (!/^\/api\//.test(path)) return realFetch(url, opts);
    const body = opts && opts.body ? JSON.parse(opts.body) : {};
    const reply = (data, status) => new Response(JSON.stringify(data), { status: status || 200, headers: { 'Content-Type': 'application/json' } });
    let res;
    if (path === '/api/scan') {
      res = body.scope === 'project' && !body.project_dir ? reply({ error: 'Falta project_dir para el alcance de proyecto.' }, 400) : reply(scanData(body));
    } else if (path === '/api/plan') res = reply(planData(body));
    else if (path === '/api/apply') res = reply(applyData(body));
    else if (path === '/api/pick-folder') res = reply({ path: 'C:\\Users\\demo\\mi-proyecto' });
    else if (path === '/api/shutdown') res = reply({ ok: true });
    else res = reply({ error: 'Ruta de API desconocida.' }, 404);
    return new Promise((r) => setTimeout(() => r(res), 120));
  };
})();
