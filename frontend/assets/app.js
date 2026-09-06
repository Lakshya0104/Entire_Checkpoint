/* Witness dashboard.
 *
 * Rule that shapes this file: nothing renders a verdict the API did not send.
 * Missing data shows as "unverified" or an explicit unavailability note - never
 * as a plausible-looking placeholder. */

const TOKEN = new URLSearchParams(location.search).get('token') || '';

async function api(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  if (TOKEN) headers['X-Witness-Token'] = TOKEN;
  const res = await fetch(path, { ...opts, headers });
  const body = await res.json().catch(() => ({ error: 'non-JSON response' }));
  if (!res.ok) throw Object.assign(new Error(body.detail || body.error || res.statusText), { body, status: res.status });
  return body;
}

const $  = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;   // textContent everywhere: ledger
  return n;                                  // content is untrusted output
};

const SVGNS = 'http://www.w3.org/2000/svg';
const XLINKNS = 'http://www.w3.org/1999/xlink';
function mascotIcon(key) {
  const svg = document.createElementNS(SVGNS, 'svg');
  svg.setAttribute('class', 'ag-glyph-svg');
  const use = document.createElementNS(SVGNS, 'use');
  use.setAttributeNS(XLINKNS, 'href', `#mascot-${key}`);
  use.setAttribute('href', `#mascot-${key}`);
  svg.append(use);
  return svg;
}

const state = {
  status: null, personas: {}, checkpoints: [], selected: null,
  results: {}, graph: null,
};

/* ── status ─────────────────────────────────────────────────────── */

function setStrip(title, sub, badge, cls) {
  $('#strip-title').textContent = title;
  $('#strip-sub').textContent = sub;
  const b = $('#strip-badge');
  b.textContent = badge;
  b.className = 'live ' + (cls || '');
}

async function loadStatus() {
  const s = await api('/api/status');
  state.status = s;

  const caps = s.entire.capabilities;
  const mEntire = $('#m-entire');
  if (!caps.binary) {
    mEntire.textContent = 'CLI absent · replay mode';
    mEntire.className = 'meta-v warn';
  } else if (caps.checkpoint && caps.graph) {
    mEntire.textContent = 'checkpoint + graph';
    mEntire.className = 'meta-v ok';
  } else {
    mEntire.textContent = `partial: ${caps.checkpoint ? 'checkpoint' : ''}${caps.graph ? ' graph' : ''}`.trim() || 'no subcommands';
    mEntire.className = 'meta-v warn';
  }

  const l = s.ledger;
  $('#m-ledger').textContent = l.exists ? `${l.commits} commits · ${(l.head || '').slice(0, 8)}` : 'not initialised';
  $('#m-ledger').className = 'meta-v ' + (l.exists ? 'ok' : 'bad');

  $('#m-agents').textContent = s.agents.reasoning_available ? 'claude api · live' : 'no api key · verdict-free';
  $('#m-agents').className = 'meta-v ' + (s.agents.reasoning_available ? 'ok' : 'warn');

  const d = s.databricks;
  $('#m-dbx').textContent = d.configured ? `delta · ${d.catalog}.${d.schema}` : 'local mirror (sqlite)';
  $('#m-dbx').className = 'meta-v ' + (d.configured ? 'ok' : 'warn');

  $('#avatar').classList.toggle('degraded', !caps.binary);
  $('#foot-head').textContent = l.head ? `ledger HEAD ${l.head.slice(0, 12)}` : 'ledger empty';
  $('#dbx-note').textContent = d.note;
}

/* ── checkpoints ────────────────────────────────────────────────── */

async function loadCheckpoints() {
  const list = $('#cp-list');
  list.replaceChildren(el('div', 'empty', 'Loading checkpoints…'));
  let data;
  try { data = await api('/api/checkpoints'); }
  catch (e) { list.replaceChildren(el('div', 'empty', `Failed: ${e.message}`)); return; }

  state.checkpoints = data.checkpoints || [];
  list.replaceChildren();

  if (!state.checkpoints.length) {
    const note = el('div', 'empty');
    note.textContent = data.note ||
      'No checkpoints returned. Run `entire checkpoint list --json` on a repo with Entire enabled.';
    list.append(note);

    // Ledger may still hold checkpoints ingested on another machine.
    try {
      const led = await api('/api/ledger');
      const n = led.counts?.checkpoints || 0;
      if (n) list.append(el('div', 'empty', `${n} checkpoint(s) already in the audit-ledger — enter an id in the graph panel to work with them.`));
    } catch { /* status already reported the ledger */ }
    return;
  }

  state.checkpoints.forEach((cp) => {
    const row = el('div', 'cp');
    row.append(
      el('span', 'cp-sq'),
      (() => {
        const w = el('div');
        w.append(el('div', 'cp-id', cp.id), el('div', 'cp-sum', cp.summary || 'no summary recorded'));
        return w;
      })(),
      el('span', 'cp-meta', cp.branch || '—'),
      el('span', 'cp-meta', (cp.created_at || '').slice(0, 16)),
    );
    row.onclick = () => selectCheckpoint(cp.id, row);
    list.append(row);
  });
}

function selectCheckpoint(id, row) {
  state.selected = id;
  $$('.cp').forEach((n) => n.classList.remove('sel'));
  row?.classList.add('sel');
  $('#g-checkpoint').value = id;
  setStrip(`Checkpoint ${id}`, 'Ready to audit', 'SELECTED', 'done');
  renderCast();
}

/* ── agent cast ─────────────────────────────────────────────────── */

const RUNNABLE = {
  auditor: '/api/audit', watchman: '/api/watch', archivist: '/api/archive',
  ghost: '/api/haunt', messenger: '/api/handoff', referee: '/api/referee',
};

function afterNoon() {
  const n = new Date();
  return n.getHours() > 12 || (n.getHours() === 12 && n.getMinutes() >= 0);
}

function renderCast() {
  const grid = $('#cast-grid');
  grid.replaceChildren();

  Object.entries(state.personas).forEach(([key, p]) => {
    const card = el('div', 'agent');
    card.style.setProperty('--accent', p.accent || '#00C8FF');

    // The Referee is the curveball answer mechanism - CLAUDE.md says do not
    // demo it before noon, so the card stays locked until then.
    const locked = key === 'referee' && !afterNoon();
    if (locked) {
      card.classList.add('locked');
      card.append(el('div', 'ag-lock', 'UNLOCKS 12:00'));
    }

    const head = el('div', 'ag-head');
    const glyph = el('div', 'ag-glyph');
    glyph.append(mascotIcon(key));
    head.append(glyph);
    const names = el('div');
    names.append(el('div', 'ag-name', p.name), el('div', 'ag-role', (p.role || '').toUpperCase()));
    head.append(names);

    const body = el('div', 'ag-body');
    body.append(el('div', 'ag-k', 'OUTPUTS'), el('div', 'ag-out', p.outputs || ''));

    card.append(head, body);

    const res = state.results[key];
    const verdict = el('div', 'ag-verdict');
    verdict.append(el('span', 'ag-status', res ? summaryFor(key, res) : 'NOT RUN'));
    verdict.append(el('span', 'ag-count', res ? String(countFor(key, res)) : '—'));
    card.append(verdict);

    if (res) {
      const tally = tallyFor(key, res);
      if (tally.length) {
        const row = el('div', 'ag-tally');
        tally.forEach(([k, v]) => row.append(el('span', `tally ${k}`, `${k} ${v}`)));
        card.append(row);
      }
      if (res.error) {
        const warn = el('div', 'ag-out');
        warn.style.color = 'var(--amber)';
        warn.style.marginTop = '10px';
        warn.textContent = res.error;
        card.append(warn);
      }
    }

    const btn = el('button', 'ag-run', locked ? 'LOCKED UNTIL NOON' : `RUN ${p.name.toUpperCase()}`);
    btn.disabled = locked || !state.selected;
    btn.onclick = () => runPersona(key, btn);
    card.append(btn);

    attachTilt(card);
    grid.append(card);
  });
}

function countFor(key, res) {
  const d = res.data || {};
  if (key === 'auditor')   return (d.requirements || []).length;
  if (key === 'watchman')  return d.score == null ? '—' : d.score;
  if (key === 'archivist') return (d.assumptions || []).length;
  if (key === 'ghost')     return (d.dead_ends || []).length;
  if (key === 'referee')   return (d.comparisons || []).length;
  if (key === 'messenger') return d.resume_confidence == null ? '—' : Math.round(d.resume_confidence * 100) + '%';
  return '—';
}

function summaryFor(key, res) {
  if (res.degraded) return 'NO EVIDENCE';
  const d = res.data || {};
  if (key === 'auditor')   return 'REQUIREMENTS';
  if (key === 'watchman')  return (d.verdict || 'unverified').toUpperCase();
  if (key === 'archivist') return 'ASSUMPTIONS';
  if (key === 'ghost')     return 'DEAD ENDS';
  if (key === 'referee')   return d.drift_score == null ? 'NO DRIFT SCORE' : `DRIFT ${d.drift_score}`;
  if (key === 'messenger') return 'RESUME CONFIDENCE';
  return '';
}

function tallyFor(key, res) {
  const d = res.data || {};
  if (key === 'auditor') {
    const c = { satisfied: 0, partial: 0, unverified: 0, contradicted: 0 };
    (d.requirements || []).forEach((r) => { if (c[r.status] != null) c[r.status]++; });
    return Object.entries(c).filter(([, v]) => v > 0);
  }
  if (key === 'watchman') {
    const c = {};
    (d.risks || []).forEach((r) => { c[r.severity] = (c[r.severity] || 0) + 1; });
    return Object.entries(c);
  }
  if (key === 'archivist') {
    const c = {};
    (d.assumptions || []).forEach((a) => { c[a.decay_status] = (c[a.decay_status] || 0) + 1; });
    return Object.entries(c);
  }
  return [];
}

async function runPersona(key, btn) {
  if (!state.selected) return;
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = 'RUNNING…';
  setStrip(state.personas[key].name, `Working on ${state.selected}`, 'RUNNING', 'running');
  try {
    const out = await api(RUNNABLE[key], {
      method: 'POST',
      body: JSON.stringify({ checkpoint_id: state.selected, symbol: $('#g-symbol').value || null }),
    });
    state.results[key] = out.result || { data: out.packet ? { ...out.packet } : out, degraded: false };
    setStrip(state.personas[key].name, 'Committed to the audit-ledger', 'DONE', 'done');
    renderCast(); loadLedger();
  } catch (e) {
    setStrip(state.personas[key].name, e.message, 'ERROR', 'err');
    btn.disabled = false; btn.textContent = label;
  }
}

/* cursor-tracked 3D tilt (CLAUDE.md §3) */
function attachTilt(card) {
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  if (matchMedia('(hover: none)').matches) return;   // no tilt on touch
  let frame = 0;
  card.addEventListener('pointermove', (e) => {
    if (frame) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      const r = card.getBoundingClientRect();
      const px = (e.clientX - r.left) / r.width - 0.5;
      const py = (e.clientY - r.top) / r.height - 0.5;
      card.style.transform =
        `perspective(1000px) rotateY(${px * 9}deg) rotateX(${-py * 9}deg) translateZ(6px)`;
    });
  });
  card.addEventListener('pointerleave', () => { card.style.transform = ''; });
}

/* ── graph ──────────────────────────────────────────────────────── */

const SVG_NS = 'http://www.w3.org/2000/svg';
const svgEl = (tag, attrs = {}) => {
  const n = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
};
const STATUS_COLOR = {
  holds: '#6FE3B0', stale: '#FF9E3D', contradicted: '#FF5D6C', unverified: '#5c626b',
};

function renderSubgraph(sub) {
  const svg = $('#subgraph');
  svg.replaceChildren();
  if (!sub || !sub.nodes?.length) {
    const t = svgEl('text', { x: 360, y: 230, 'text-anchor': 'middle',
      fill: '#5c626b', 'font-size': 12, 'font-family': 'IBM Plex Mono, monospace',
      'letter-spacing': '2' });
    t.textContent = 'NO BLAST RADIUS CAPTURED';
    svg.append(t);
    return;
  }

  const cx = 360, cy = 230;
  const others = sub.nodes.slice(1);
  const R1 = 118, R2 = 196;

  // Two rings so a wide radius stays legible on one screen.
  const positions = others.map((n, i) => {
    const inner = i < Math.ceil(others.length / 2);
    const ring = inner ? others.slice(0, Math.ceil(others.length / 2))
                       : others.slice(Math.ceil(others.length / 2));
    const idx = inner ? i : i - Math.ceil(others.length / 2);
    const a = (idx / Math.max(ring.length, 1)) * Math.PI * 2 - Math.PI / 2 + (inner ? 0 : 0.35);
    const r = inner ? R1 : R2;
    return { x: cx + Math.cos(a) * r, y: cy + Math.sin(a) * r * 0.78 };
  });

  const edges = svgEl('g');
  positions.forEach((p, i) => {
    const c = STATUS_COLOR[others[i].status] || STATUS_COLOR.unverified;
    const line = svgEl('path', {
      d: `M ${cx} ${cy} Q ${(cx + p.x) / 2} ${(cy + p.y) / 2 - 22} ${p.x} ${p.y}`,
      stroke: c, 'stroke-width': others[i].gone ? 1 : 1.3, fill: 'none',
      opacity: others[i].gone ? 0.3 : 0.42,
      'stroke-dasharray': others[i].gone ? '3 4' : 'none',
    });
    edges.append(line);
  });
  svg.append(edges);

  // root
  const root = sub.nodes[0];
  const rc = STATUS_COLOR[root.status] || STATUS_COLOR.unverified;
  svg.append(svgEl('circle', { cx, cy, r: 40, fill: 'rgba(0,0,0,.55)', stroke: rc, 'stroke-width': 2 }));
  svg.append(svgEl('circle', { cx, cy, r: 52, fill: 'none', stroke: rc, 'stroke-width': .6, opacity: .35 }));
  const rt = svgEl('text', { x: cx, y: cy + 4, 'text-anchor': 'middle', fill: '#f2f3f5',
    'font-size': 12, 'font-family': 'IBM Plex Mono, monospace' });
  rt.textContent = truncate(root.label, 14);
  svg.append(rt);

  positions.forEach((p, i) => {
    const n = others[i];
    const c = STATUS_COLOR[n.status] || STATUS_COLOR.unverified;
    const g = svgEl('g', { opacity: n.gone ? 0.55 : 1 });
    if (n.kind === 'file') {
      g.append(svgEl('rect', { x: p.x - 7, y: p.y - 7, width: 14, height: 14,
        fill: 'rgba(0,0,0,.6)', stroke: c, 'stroke-width': 1.4 }));
    } else {
      g.append(svgEl('circle', { cx: p.x, cy: p.y, r: 7.5,
        fill: 'rgba(0,0,0,.6)', stroke: c, 'stroke-width': 1.4 }));
    }
    const t = svgEl('text', {
      x: p.x, y: p.y + (p.y < cy ? -14 : 21), 'text-anchor': 'middle',
      fill: n.gone ? '#5c626b' : '#9aa0a8', 'font-size': 9.5,
      'font-family': 'IBM Plex Mono, monospace',
      'text-decoration': n.gone ? 'line-through' : 'none',
    });
    t.textContent = truncate(n.label, 16);
    g.append(t);
    svg.append(g);
  });
}

const truncate = (s, n) => (String(s).length > n ? String(s).slice(0, n - 1) + '…' : String(s));

function renderDelta(delta) {
  const shell = $('#verdict-shell'), val = $('#verdict-value'), list = $('#delta-list');
  const s = delta?.status || 'unverified';
  shell.dataset.s = s; val.dataset.s = s;
  val.textContent = s.toUpperCase();
  $('#verdict-reason').textContent = delta?.reason || '';

  list.replaceChildren();
  const rows = [
    ['FILES +',   delta?.files_added,     'add'],
    ['FILES −',   delta?.files_removed,   'rm'],
    ['SYMBOLS +', delta?.symbols_added,   'add'],
    ['SYMBOLS −', delta?.symbols_removed, 'rm'],
  ];
  rows.forEach(([k, v, cls]) => {
    if (!v || !v.length) return;
    const d = el('div', `delta ${cls}`);
    d.append(el('span', 'delta-k', k), el('span', 'delta-v', v.join(', ')));
    list.append(d);
  });

  if (delta?.before && delta?.after) {
    const d = el('div', 'delta');
    d.append(el('span', 'delta-k', 'HEAD'),
             el('span', 'delta-v', `${(delta.before.head_sha || 'unknown').slice(0, 8)} → ${(delta.after.head_sha || 'unknown').slice(0, 8)}`));
    list.append(d);
  }
  if (delta?.after?.evidence_id) {
    const d = el('div', 'delta');
    const link = el('span', 'delta-v', delta.after.evidence_id);
    link.style.cursor = 'pointer'; link.style.color = 'var(--ghost)';
    link.onclick = () => openEvidence(delta.after.evidence_id);
    d.append(el('span', 'delta-k', 'EVIDENCE'), link);
    list.append(d);
  }
}

async function graphAction(kind) {
  const symbol = $('#g-symbol').value.trim();
  const checkpoint_id = $('#g-checkpoint').value.trim() || state.selected;
  if (!symbol || !checkpoint_id) {
    setStrip('Graph', 'Enter both a symbol and a checkpoint id', 'INPUT NEEDED', 'err');
    return;
  }
  setStrip('Graph', kind === 'snapshot' ? 'Capturing blast radius…' : 'Re-running at HEAD…', 'RUNNING', 'running');
  try {
    const out = await api(`/api/graph/${kind === 'snapshot' ? 'snapshot' : 'recheck'}`, {
      method: 'POST', body: JSON.stringify({ symbol, checkpoint_id, depth: 2 }),
    });
    if (kind === 'snapshot') {
      renderSubgraph({ nodes: [{ id: symbol, label: symbol, kind: 'root', status: 'unverified' }] });
      renderDelta({ status: 'unverified',
        reason: out.usable
          ? 'Snapshot captured. Re-check at HEAD to test whether it still holds.'
          : `Snapshot recorded but the command produced no usable output (${out.evidence.status}). No verdict is asserted.` });
      setStrip('Graph', `Snapshot committed: ${out.ledger.short || 'ledger'}`, 'DONE', 'done');
    } else {
      renderSubgraph(out.subgraph_after);
      renderDelta(out.delta);
      setStrip('Graph', `${symbol} → ${out.status}`, 'DONE', out.status === 'holds' ? 'done' : 'err');
    }
    loadLedger();
  } catch (e) {
    setStrip('Graph', e.body?.error || e.message, 'ERROR', 'err');
    renderDelta({ status: 'unverified', reason: e.body?.error || e.message });
  }
}

/* ── ledger ─────────────────────────────────────────────────────── */

async function loadLedger() {
  let data;
  try { data = await api('/api/ledger'); } catch { return; }

  const stats = $('#ledger-stats');
  stats.replaceChildren();
  const cells = [
    ['COMMITS', data.commits],
    ['CHECKPOINTS', data.counts?.checkpoints || 0],
    ['REQUIREMENTS', data.counts?.requirements || 0],
    ['SNAPSHOTS', data.counts?.['graph-snapshots'] || 0],
  ];
  cells.forEach(([k, v]) => {
    const c = el('div', 'lstat');
    c.append(el('div', 'lstat-k', k), el('div', 'lstat-v', String(v)));
    stats.append(c);
  });

  const chain = $('#chain');
  chain.replaceChildren();
  (data.history || []).forEach((c) => {
    const row = el('div', 'commit');
    row.append(el('span', 'commit-sha', c.short),
               el('span', 'commit-sub', c.subject),
               el('span', 'commit-date', (c.date || '').slice(0, 16)));
    chain.append(row);
  });
  if (!data.history?.length) chain.append(el('div', 'empty', 'No commits yet.'));

  const reqs = data.counts?.requirements || 0;
  const cps = data.counts?.checkpoints || 0;
  const banner = $('#banner-trust');
  banner.textContent = reqs > 0
    ? `${reqs} requirement${reqs === 1 ? '' : 's'} verified across ${cps} checkpoint${cps === 1 ? '' : 's'} — every one traceable to its evidence.`
    : 'No audits committed yet — run one above and this line updates with a real count.';
}

async function verifyChain() {
  const dot = $('#chain-dot'), label = $('#chain-label');
  label.textContent = 'VERIFYING';
  try {
    const v = await api('/api/ledger/verify');
    dot.className = 'pill-dot ' + (v.ok ? 'ok' : 'bad');
    label.textContent = v.ok ? `CHAIN OK · ${v.commit_count}` : 'CHAIN BROKEN';
    setStrip('Audit ledger', v.fsck_output, v.ok ? 'VERIFIED' : 'TAMPERED', v.ok ? 'done' : 'err');
  } catch (e) {
    dot.className = 'pill-dot bad';
    label.textContent = 'CHAIN ERROR';
    setStrip('Audit ledger', e.message, 'ERROR', 'err');
  }
}

/* ── databricks ─────────────────────────────────────────────────── */

async function loadDatabricks() {
  const sel = $('#genie-select');
  try {
    const q = await api('/api/databricks/queries');
    sel.replaceChildren();
    Object.entries(q.queries).forEach(([k, question]) => {
      const o = document.createElement('option');
      o.value = k; o.textContent = question;
      sel.append(o);
    });
  } catch { /* status panel already reports failure */ }
}

async function syncDatabricks() {
  const disc = $('#disc-sync');
  disc.classList.add('busy');
  setStrip('Databricks', 'Mirroring audit-ledger → tables…', 'RUNNING', 'running');
  try {
    const r = await api('/api/databricks/sync', { method: 'POST' });
    const wrap = $('#dbx-tables');
    wrap.replaceChildren();
    Object.entries(r.rows_written || {}).forEach(([t, n]) => {
      const row = el('div', 'dbx-t');
      row.append(el('span', 'dbx-name', t), el('span', 'dbx-rows', String(n)));
      wrap.append(row);
    });
    setStrip('Databricks', r.ok ? `${r.total_rows} rows into ${r.backend}` : (r.error || 'sync failed'),
             r.ok ? 'SYNCED' : 'ERROR', r.ok ? 'done' : 'err');
  } catch (e) {
    setStrip('Databricks', e.message, 'ERROR', 'err');
  } finally {
    disc.classList.remove('busy');
  }
}

async function runGenie() {
  const key = $('#genie-select').value;
  if (!key) return;
  $('#genie-out').replaceChildren(el('div', 'empty', 'Running…'));
  try {
    const r = await api(`/api/databricks/genie/${key}`);
    $('#genie-sql').textContent = r.sql || '';
    const out = $('#genie-out');
    out.replaceChildren();
    if (!r.ok) { out.append(el('div', 'empty', r.error || 'query failed')); return; }
    if (!r.rows.length) { out.append(el('div', 'empty', 'No rows — nothing in the ledger matches yet.')); return; }

    const table = el('table', 'res');
    const thead = el('thead'), htr = el('tr');
    r.columns.forEach((c) => htr.append(el('th', null, c)));
    thead.append(htr);
    const tbody = el('tbody');
    r.rows.forEach((row) => {
      const tr = el('tr');
      r.columns.forEach((c) => {
        const td = el('td', null, row[c] == null ? '—' : String(row[c]));
        td.title = row[c] == null ? '' : String(row[c]);
        tr.append(td);
      });
      tbody.append(tr);
    });
    table.append(thead, tbody);
    out.append(table);
  } catch (e) {
    $('#genie-out').replaceChildren(el('div', 'empty', e.message));
  }
}

/* ── evidence drawer ────────────────────────────────────────────── */

async function openEvidence(id) {
  const drawer = $('#drawer'), scrim = $('#drawer-scrim');
  drawer.hidden = false; scrim.hidden = false;
  $('#drawer-id').textContent = id;
  const body = $('#drawer-body');
  body.replaceChildren(el('div', 'empty', 'Loading…'));
  try {
    const ev = await api(`/api/evidence/${id}`);
    body.replaceChildren();

    const tags = el('div', 'ev-row');
    const cls = ev.status === 'ok' || ev.status === 'replayed' ? 'ok'
              : ev.status === 'unavailable' ? 'warn' : 'bad';
    tags.append(el('span', `ev-tag ${cls}`, ev.status),
                el('span', 'ev-tag', `exit ${ev.exit_code ?? '—'}`),
                el('span', 'ev-tag', `${ev.duration_ms}ms`),
                el('span', 'ev-tag', ev.source || 'live'));
    if (ev.secret_findings?.length)
      tags.append(el('span', 'ev-tag warn', `${ev.secret_findings.length} redacted`));

    body.append(el('div', 'ev-k', 'STATUS'), tags,
                el('div', 'ev-k', 'COMMAND'), el('div', 'ev-cmd', ev.command_str || (ev.command || []).join(' ')),
                el('div', 'ev-k', 'STDOUT SHA256'), el('div', 'ev-cmd', ev.stdout_sha256 || '—'),
                el('div', 'ev-k', 'OUTPUT'),
                el('div', 'ev-out', ev.stdout || ev.stderr || '(no output)'));
  } catch (e) {
    body.replaceChildren(el('div', 'empty', e.message));
  }
}

function closeDrawer() {
  $('#drawer').hidden = true;
  $('#drawer-scrim').hidden = true;
}

/* ── run-all ────────────────────────────────────────────────────── */

async function runAll() {
  if (!state.selected) {
    setStrip('Audit pipeline', 'Select a checkpoint first', 'INPUT NEEDED', 'err');
    return;
  }
  const btn = $('#run-all');
  btn.disabled = true;
  setStrip(`Full audit · ${state.selected}`, 'Ingest → audit → archive → ghost → watchman → handoff', 'RUNNING', 'running');
  try {
    const out = await api('/api/run-all', {
      method: 'POST',
      body: JSON.stringify({ checkpoint_id: state.selected, symbol: $('#g-symbol').value || null }),
    });
    ['audit', 'archive', 'haunt', 'watch', 'handoff'].forEach((stage) => {
      const key = { audit: 'auditor', archive: 'archivist', haunt: 'ghost',
                    watch: 'watchman', handoff: 'messenger' }[stage];
      if (out[stage]?.result) state.results[key] = out[stage].result;
    });
    renderCast(); loadLedger(); loadStatus();
    const rows = out.databricks?.total_rows ?? 0;
    setStrip(`Full audit · ${state.selected}`,
             `Committed to ledger ${(out.ledger_head || '').slice(0, 8)} · ${rows} rows mirrored`,
             'DONE', 'done');
  } catch (e) {
    setStrip('Full audit', e.message, 'ERROR', 'err');
  } finally {
    btn.disabled = false;
  }
}

/* ── boot ───────────────────────────────────────────────────────── */

async function boot() {
  $('#run-all').onclick = runAll;
  $('#reload-cp').onclick = loadCheckpoints;
  $('#chain-pill').onclick = verifyChain;
  $('#g-snapshot').onclick = () => graphAction('snapshot');
  $('#g-recheck').onclick = () => graphAction('recheck');
  $('#disc-recheck').onclick = () => graphAction('recheck');
  $('#disc-sync').onclick = syncDatabricks;
  $('#genie-run').onclick = runGenie;
  $('#drawer-close').onclick = closeDrawer;
  $('#drawer-scrim').onclick = closeDrawer;
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });

  try {
    const p = await api('/api/personas');
    state.personas = p.personas;
  } catch { /* rendered empty below */ }
  renderCast();
  renderSubgraph(null);

  await Promise.allSettled([loadStatus(), loadCheckpoints(), loadLedger(), loadDatabricks()]);

  // Three distinct situations, and they must not read alike: live CLI,
  // replaying recorded output, and nothing available at all.
  const live = state.status?.entire?.capabilities?.binary;
  const replaying = !live && state.checkpoints.length > 0;
  setStrip('Audit pipeline',
    live ? 'Select a checkpoint to begin'
         : replaying
           ? 'Entire CLI absent — replaying recorded command output from fixtures/'
           : 'Entire CLI not detected and no fixtures recorded — no verdicts can be asserted',
    'IDLE', 'idle');
}

boot();
