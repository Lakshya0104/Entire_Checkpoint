/* Witness dashboard.
 *
 * Two rules shape this file:
 *   1. Nothing renders a verdict the API did not send. Missing data shows as
 *      "unverified" or an explicit note - never a plausible placeholder.
 *   2. The Full/Partial context badge is rendered from the API's own redaction
 *      report, on every report surface, before any finding is shown. A reader
 *      must never have to guess whether they are seeing complete context.
 */

const TOKEN = new URLSearchParams(location.search).get('token') || '';

async function api(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  if (TOKEN) headers['X-Witness-Token'] = TOKEN;
  const res = await fetch(path, { ...opts, headers });
  const body = await res.json().catch(() => ({ error: 'non-JSON response' }));
  if (!res.ok) throw Object.assign(new Error(body.detail || body.error || res.statusText), { body });
  return body;
}

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
// textContent everywhere: ledger and command output are untrusted.
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

const state = {
  status: null, personas: {}, cast: [], checkpoints: [],
  selected: null, results: {}, context: null, graph: null,
};

/* ── context badge ──────────────────────────────────────────────── */

function paintBadge(node, report, fallback) {
  if (!node) return;
  if (!report) {
    node.textContent = fallback || 'Context unknown';
    node.dataset.state = 'unknown';
    return;
  }
  node.textContent = report.badge || (report.partial ? 'Partial context' : 'Full context');
  node.dataset.state = report.context_state || (report.partial ? 'partial' : 'full');
  if (report.withheld?.length || report.missing?.length) {
    node.title = 'Withheld: ' + [...(report.withheld || []), ...(report.missing || [])].join(', ');
  }
}

function setContext(report) {
  state.context = report;
  paintBadge($('#hero-badge'), report);
  paintBadge($('#report-badge'), report);
}

/* ── status ─────────────────────────────────────────────────────── */

async function loadStatus() {
  const s = await api('/api/status');
  state.status = s;

  const caps = s.entire.capabilities;
  const where = caps.binary
    ? (caps.checkpoint && caps.graph ? 'Entire CLI connected' : 'Entire CLI partly available')
    : 'Entire CLI absent, replaying recorded output';
  const brain = s.agents.reasoning_available ? 'Claude API live' : 'no API key, verdict-free';
  const store = s.databricks.configured ? 'Delta tables' : 'local mirror';
  $('#hero-meta').textContent = `${where}. ${brain}. ${store}.`;
  $('#dbx-note').textContent = s.databricks.note;
  $('#foot-head').textContent = s.ledger.head
    ? `ledger ${s.ledger.head.slice(0, 12)}` : 'ledger empty';
}

/* ── the six agents ─────────────────────────────────────────────── */

function renderCapabilities() {
  const grid = $('#cast');
  grid.replaceChildren();
  state.cast.forEach((key) => {
    const p = state.personas[key];
    if (!p) return;
    const cap = el('div', 'cap');
    const art = document.createElement('div');
    art.innerHTML = window.WitnessMascots.mascot(p.prop, 46);
    const text = el('div');
    text.append(el('div', 'cap-name', p.name),
                el('div', 'cap-role', p.role),
                el('p', 'cap-blurb', p.blurb));
    cap.append(art.firstElementChild, text);
    grid.append(cap);
  });
}

/* ── checkpoints ────────────────────────────────────────────────── */

async function loadCheckpoints() {
  const list = $('#cp-list');
  list.replaceChildren(el('p', 'muted', 'Loading checkpoints'));
  let data;
  try { data = await api('/api/checkpoints'); }
  catch (e) { list.replaceChildren(el('p', 'muted', `Could not load checkpoints: ${e.message}`)); return; }

  state.checkpoints = data.checkpoints || [];
  list.replaceChildren();
  if (!state.checkpoints.length) {
    list.append(el('p', 'muted', data.note || 'No checkpoints returned.'));
    return;
  }
  state.checkpoints.forEach((cp) => {
    const row = el('div', 'cp');
    row.setAttribute('role', 'button');
    row.setAttribute('tabindex', '0');
    row.setAttribute('aria-selected', 'false');
    const main = el('div');
    main.append(el('div', 'cp-id', cp.id),
                el('div', 'cp-sum', cp.summary || 'No summary recorded'));
    row.append(main, el('span', 'cp-branch', cp.branch || ''));
    const pick = () => selectCheckpoint(cp.id, row);
    row.onclick = pick;
    row.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); } };
    list.append(row);
  });
  selectCheckpoint(state.checkpoints[0].id, list.firstElementChild);
}

async function selectCheckpoint(id, row) {
  state.selected = id;
  $$('.cp').forEach((n) => n.setAttribute('aria-selected', 'false'));
  row?.setAttribute('aria-selected', 'true');
  $('#g-checkpoint').value = id;
  try { setContext(await api(`/api/context/${id}`)); } catch { /* badge stays unknown */ }
  renderSections();
}

/* ── report sections, one per agent ─────────────────────────────── */

const STAGE = {
  auditor: '/api/audit', riskbot: '/api/watch', ledgerkeep: '/api/archive',
  sleuth: '/api/haunt', scribe: '/api/handoff', warden: '/api/warden',
};

function renderSections() {
  const wrap = $('#report-sections');
  wrap.replaceChildren();
  state.cast.forEach((key) => {
    const p = state.personas[key];
    if (!p) return;
    const card = el('article', 'section');

    const head = el('div', 'section-head');
    const art = document.createElement('div');
    art.innerHTML = window.WitnessMascots.mascot(p.prop, 34);
    const title = el('div');
    title.append(el('div', 'section-title', p.name), el('div', 'section-role', p.role));
    head.append(art.firstElementChild, title);

    const bodyEl = el('div', 'section-body');
    const res = state.results[key];
    if (!res) bodyEl.append(el('p', 'muted', p.blurb));
    else renderFindings(key, res, bodyEl);

    const foot = el('div', 'section-foot');
    if (res?.redaction) {
      const b = el('span', 'ctx');
      paintBadge(b, res.redaction);
      foot.append(b);
    }
    if (res?.error) foot.append(el('span', 'muted', res.error));
    const btn = el('button', 'btn btn-ghost', `Run ${p.name}`);
    btn.disabled = !state.selected;
    btn.onclick = () => runStage(key, btn);
    foot.append(btn);

    card.append(head, bodyEl, foot);
    tilt(card);
    wrap.append(card);
  });
}

function renderFindings(key, res, out) {
  const d = res.data || {};

  if (key === 'auditor') {
    const reqs = d.requirements || [];
    if (!reqs.length) { out.append(el('p', 'muted', d.coverage_note || 'No requirements resolved.')); return; }
    reqs.forEach((r) => {
      const row = el('div', 'req');
      row.append(el('div', 'req-text', r.text || r.requirement_text || ''));
      const meta = el('div', 'req-meta');
      const tag = el('span', 'tag', r.status || 'unverified');
      tag.dataset.s = r.status || 'unverified';
      meta.append(tag);
      if (r.downgraded_from) meta.append(el('span', 'muted', `was ${r.downgraded_from}; ${r.downgrade_reason || ''}`));
      if (r.evidence_id) {
        const link = el('button', 'ev-link', r.evidence_id);
        link.onclick = () => openEvidence(r.evidence_id);
        meta.append(link);
      }
      row.append(meta);
      out.append(row);
    });
    return;
  }

  if (key === 'riskbot') {
    const score = d.score == null ? 'No score' : `${d.score} / 100`;
    out.append(el('p', 'req-text', score));
    out.append(el('p', 'muted', d.score_rationale || ''));
    (d.risks || []).forEach((r) => {
      const row = el('div', 'req');
      row.append(el('div', 'req-text', r.title || ''));
      const meta = el('div', 'req-meta');
      meta.append(el('span', 'tag', `${r.band || 'risk'} · ${r.severity || ''}`.trim()));
      row.append(meta);
      out.append(row);
    });
    return;
  }

  if (key === 'warden') {
    out.append(el('p', 'req-text', d.statement || 'No boundary report yet.'));
    (d.unsafe_to_conclude || []).forEach((u) =>
      out.append(el('p', 'muted', `Do not conclude: ${u}`)));
    if (res.redaction?.withheld?.length)
      out.append(el('p', 'muted', `Withheld: ${res.redaction.withheld.join(', ')}`));
    return;
  }

  if (key === 'ledgerkeep') {
    const list = d.assumptions || [];
    if (!list.length) { out.append(el('p', 'muted', 'No assumptions recorded.')); return; }
    list.forEach((a) => {
      const row = el('div', 'req');
      row.append(el('div', 'req-text', a.text || ''));
      const meta = el('div', 'req-meta');
      meta.append(el('span', 'tag', a.decay_status || 'fresh'));
      if (a.decayed_confidence != null) meta.append(el('span', 'muted', `confidence ${a.decayed_confidence}`));
      row.append(meta);
      out.append(row);
    });
    return;
  }

  if (key === 'sleuth') {
    const items = [...(d.dead_ends || []), ...(d.unfinished || [])];
    if (!items.length) { out.append(el('p', 'muted', 'Nothing unresolved found.')); return; }
    items.forEach((i) => out.append(el('p', 'req-text', i.hypothesis || i.what || '')));
    return;
  }

  if (key === 'scribe') {
    out.append(el('p', 'req-text', d.next_action || d.goal || 'No packet yet.'));
    if (d.resume_confidence != null)
      out.append(el('p', 'muted', `Resume confidence ${Math.round(d.resume_confidence * 100)}%`));
    return;
  }
}

async function runStage(key, btn) {
  if (!state.selected) return;
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = 'Running';
  try {
    const out = await api(STAGE[key], {
      method: 'POST',
      body: JSON.stringify({ checkpoint_id: state.selected, symbol: $('#g-symbol').value || null }),
    });
    state.results[key] = out.result || out;
    if (out.result?.redaction) setContext(out.result.redaction);
    renderSections(); loadLedger();
  } catch (e) {
    btn.disabled = false; btn.textContent = label;
    alert(e.message);
  }
}

/* cursor-tracked 3D tilt, kept from the original identity */
function tilt(card) {
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  if (matchMedia('(hover: none)').matches) return;
  let frame = 0;
  card.addEventListener('pointermove', (e) => {
    if (frame) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      const r = card.getBoundingClientRect();
      const px = (e.clientX - r.left) / r.width - 0.5;
      const py = (e.clientY - r.top) / r.height - 0.5;
      card.style.transform =
        `perspective(1100px) rotateY(${px * 5}deg) rotateX(${-py * 5}deg) translateZ(4px)`;
    });
  });
  card.addEventListener('pointerleave', () => { card.style.transform = ''; });
}

/* ── graph ──────────────────────────────────────────────────────── */

const SEVERITY = {
  holds: '#6FE3B0', stale: '#FF9E3D',
  contradicted: '#FF5D6C', unverified: '#6b7280',
};

let fg = null;
let highlighted = new Set();

function graphInstance() {
  if (fg) return fg;
  const mount = $('#graph-canvas');
  fg = ForceGraph()(mount)
    .backgroundColor('rgba(0,0,0,0)')
    .nodeRelSize(5)
    .nodeId('id')
    .nodeLabel((n) => `${n.label} — ${n.status}`)
    .nodeColor((n) => SEVERITY[n.status] || SEVERITY.unverified)
    .linkColor((l) => (highlighted.size && !highlighted.has(l.target.id ?? l.target)
      ? 'rgba(255,255,255,0.05)' : 'rgba(127,255,212,0.34)'))
    .linkWidth((l) => (highlighted.has(l.target.id ?? l.target) ? 2.4 : 0.9))
    .linkDirectionalParticles((l) => (highlighted.has(l.target.id ?? l.target) ? 3 : 0))
    .linkDirectionalParticleWidth(2.2)
    .linkDirectionalParticleColor(() => '#7FFFD4')
    .onNodeClick((n) => exploreNode(n))
    .nodeCanvasObjectMode(() => 'after')
    .nodeCanvasObject((node, ctx, scale) => {
      // Node size encodes impact radius; the label is drawn by hand so it can
      // dim with the rest of the graph when a blast radius is isolated.
      const dim = highlighted.size && !highlighted.has(node.id);
      ctx.font = `${11 / scale}px 'IBM Plex Mono', monospace`;
      ctx.fillStyle = dim ? 'rgba(160,168,178,0.25)' : '#a2a8b0';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      ctx.fillText(node.label, node.x, node.y + (node.val || 4) + 2);
    });

  // Default forces pack a small graph into an unreadable blob. Push nodes
  // apart and give links room, then frame the result once it settles.
  fg.d3Force('charge').strength(-420).distanceMax(420);
  fg.d3Force('link').distance((l) => (l.source.kind === 'root' ? 110 : 70));
  fg.d3Force('center').strength(0.6);
  fg.onEngineStop(() => fg.zoomToFit(420, 70));

  const resize = () => fg.width(mount.clientWidth).height(mount.clientHeight);
  resize();
  window.addEventListener('resize', resize);
  return fg;
}

function toGraphData(sub) {
  const nodes = (sub.nodes || []).map((n, i) => ({
    id: n.id, label: n.label, status: n.status, kind: n.kind, gone: n.gone,
    // Impact radius: the root is biggest, files next, leaf symbols smallest.
    val: n.kind === 'root' ? 14 : n.kind === 'file' ? 7 : 5,
  }));
  const ids = new Set(nodes.map((n) => n.id));
  const links = (sub.edges || [])
    .filter((e) => ids.has(e.source) && ids.has(e.target))
    .map((e) => ({ source: e.source, target: e.target, status: e.status }));
  return { nodes, links };
}

function renderGraph(sub) {
  const empty = $('#graph-empty');
  if (!sub || !(sub.nodes || []).length) { empty.hidden = false; return; }
  empty.hidden = true;
  highlighted = new Set();
  state.graph = sub;
  const g = graphInstance();
  g.graphData(toGraphData(sub));
  g.d3ReheatSimulation();
}

async function exploreNode(node) {
  // Clicking a node re-runs (or replays) impact for that symbol and isolates
  // the affected path. This is the interactive proof of graph use.
  const cp = $('#g-checkpoint').value.trim() || state.selected;
  if (!cp) return;
  $('#g-symbol').value = node.id;

  const data = fg.graphData();
  highlighted = new Set(
    data.links.filter((l) => (l.source.id ?? l.source) === node.id)
              .map((l) => l.target.id ?? l.target));
  highlighted.add(node.id);
  fg.nodeColor(fg.nodeColor());          // force a repaint with new dimming

  try {
    const out = await api('/api/graph/recheck', {
      method: 'POST', body: JSON.stringify({ symbol: node.id, checkpoint_id: cp, depth: 2 }),
    });
    renderVerdict(out.delta);
    renderGraph(out.subgraph_after);
  } catch (e) {
    // No snapshot for this symbol is a normal state, not an error to hide.
    renderVerdict({ status: 'unverified', reason: e.body?.error || e.message });
  }
}

function renderVerdict(delta) {
  const box = $('#verdict');
  const s = delta?.status || 'unverified';
  box.dataset.state = s;
  $('#verdict-state').textContent = { holds: 'Holds', stale: 'Stale',
    contradicted: 'Contradicted', unverified: 'Unverified' }[s] || s;
  $('#verdict-why').textContent = delta?.reason || '';
  $('#graph-badge').textContent = delta?.symbol ? `Symbol ${delta.symbol}` : 'No symbol loaded';
  $('#graph-badge').dataset.state = 'unknown';

  const rows = $('#verdict-rows');
  rows.replaceChildren();
  const add = (label, value, kind) => {
    if (!value || (Array.isArray(value) && !value.length)) return;
    const dl = el('dl', 'vrow');
    if (kind) dl.dataset.kind = kind;
    dl.append(el('dt', null, label), el('dd', null, Array.isArray(value) ? value.join(', ') : value));
    rows.append(dl);
  };
  add('Files added', delta?.files_added, 'added');
  add('Files removed', delta?.files_removed, 'removed');
  add('Symbols added', delta?.symbols_added, 'added');
  add('Symbols removed', delta?.symbols_removed, 'removed');
  if (delta?.before && delta?.after)
    add('HEAD', `${(delta.before.head_sha || 'unknown').slice(0, 8)} → ${(delta.after.head_sha || 'unknown').slice(0, 8)}`);
  if (delta?.after?.evidence_id) {
    const dl = el('dl', 'vrow');
    const dd = el('dd');
    const link = el('button', 'ev-link', delta.after.evidence_id);
    link.onclick = () => openEvidence(delta.after.evidence_id);
    dd.append(link);
    dl.append(el('dt', null, 'Evidence'), dd);
    rows.append(dl);
  }
}

async function graphAction(kind) {
  const symbol = $('#g-symbol').value.trim();
  const checkpoint_id = $('#g-checkpoint').value.trim() || state.selected;
  if (!symbol || !checkpoint_id) {
    renderVerdict({ status: 'unverified', reason: 'Enter both a symbol and a checkpoint id.' });
    return;
  }
  try {
    const out = await api(`/api/graph/${kind}`, {
      method: 'POST', body: JSON.stringify({ symbol, checkpoint_id, depth: 2 }),
    });
    if (kind === 'snapshot') {
      renderVerdict({ status: 'unverified', symbol,
        reason: out.usable
          ? 'Snapshot captured. Re-check at HEAD to test whether it still holds.'
          : `Snapshot recorded, but the command produced no usable output (${out.evidence.status}). No verdict is asserted.` });
    } else {
      renderGraph(out.subgraph_after);
      renderVerdict(out.delta);
    }
    loadLedger();
  } catch (e) {
    renderVerdict({ status: 'unverified', reason: e.body?.error || e.message });
  }
}

/* ── ledger ─────────────────────────────────────────────────────── */

async function loadLedger() {
  let data;
  try { data = await api('/api/ledger'); } catch { return; }

  const figures = $('#ledger-figures');
  figures.replaceChildren();
  [['Commits', data.commits], ['Checkpoints', data.counts?.checkpoints || 0],
   ['Requirements', data.counts?.requirements || 0], ['Snapshots', data.counts?.['graph-snapshots'] || 0]]
    .forEach(([k, v]) => {
      const d = el('dl', 'figure');
      d.append(el('dt', null, k), el('dd', null, String(v)));
      figures.append(d);
    });

  const commits = $('#commits');
  commits.replaceChildren();
  (data.history || []).forEach((c) => {
    const row = el('div', 'commit');
    row.append(el('span', 'commit-sha', c.short),
               el('span', 'commit-msg', c.subject),
               el('span', 'commit-at', (c.date || '').slice(0, 16)));
    commits.append(row);
  });
  if (!data.history?.length) commits.append(el('p', 'muted', 'No commits yet.'));

  const reqs = data.counts?.requirements || 0;
  $('#trust-line').textContent =
    `${data.commits} ledger commits, hash-verified. ${reqs} requirement records written.`;
}

async function verifyChain() {
  const btn = $('#verify-chain');
  const label = btn.textContent;
  btn.textContent = 'Verifying';
  try {
    const v = await api('/api/ledger/verify');
    btn.textContent = v.ok ? `Ledger verified (${v.commit_count})` : 'Ledger broken';
    $('#trust-line').textContent = v.ok
      ? `${v.commit_count} ledger commits verified by git fsck. No corruption detected.`
      : `git fsck reported: ${v.fsck_output}`;
  } catch (e) {
    btn.textContent = label;
    $('#trust-line').textContent = e.message;
  }
}

/* ── warehouse ──────────────────────────────────────────────────── */

async function loadQueries() {
  try {
    const q = await api('/api/databricks/queries');
    const sel = $('#genie-select');
    sel.replaceChildren();
    Object.entries(q.queries).forEach(([k, question]) => {
      const o = document.createElement('option');
      o.value = k; o.textContent = question;
      sel.append(o);
    });
  } catch { /* status already reported */ }
}

async function syncWarehouse() {
  const disc = $('#disc-sync');
  disc.classList.add('busy');
  try {
    const r = await api('/api/databricks/sync', { method: 'POST' });
    const wrap = $('#dbx-tables');
    wrap.replaceChildren();
    Object.entries(r.rows_written || {}).forEach(([t, n]) => {
      const row = el('div', 'table-row');
      row.append(el('span', null, t), el('span', 'table-rows', String(n)));
      wrap.append(row);
    });
  } finally { disc.classList.remove('busy'); }
}

async function runQuery() {
  const key = $('#genie-select').value;
  if (!key) return;
  const out = $('#genie-out');
  out.replaceChildren(el('p', 'muted', 'Running'));
  try {
    const r = await api(`/api/databricks/genie/${key}`);
    $('#genie-sql').textContent = r.sql || '';
    out.replaceChildren();
    if (!r.ok) { out.append(el('p', 'muted', r.error || 'Query failed')); return; }
    if (!r.rows.length) { out.append(el('p', 'muted', 'No rows match yet.')); return; }
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
  } catch (e) { out.replaceChildren(el('p', 'muted', e.message)); }
}

/* ── evidence drawer ────────────────────────────────────────────── */

async function openEvidence(id) {
  $('#drawer').hidden = false; $('#scrim').hidden = false;
  $('#drawer-id').textContent = id;
  const body = $('#drawer-body');
  body.replaceChildren(el('p', 'muted', 'Loading'));
  try {
    const ev = await api(`/api/evidence/${id}`);
    body.replaceChildren();
    const tags = el('div', 'ev-tags');
    [ev.status, `exit ${ev.exit_code ?? '—'}`, `${ev.duration_ms}ms`, ev.source || 'live']
      .forEach((t) => tags.append(el('span', 'tag', String(t))));
    if (ev.secret_findings?.length)
      tags.append(el('span', 'tag', `${ev.secret_findings.length} redacted`));
    body.append(el('p', 'ev-key', 'Status'), tags,
                el('p', 'ev-key', 'Command'), el('div', 'ev-box', ev.command_str || (ev.command || []).join(' ')),
                el('p', 'ev-key', 'Output sha256'), el('div', 'ev-box', ev.stdout_sha256 || '—'),
                el('p', 'ev-key', 'Output (stays on this machine)'),
                el('div', 'ev-out', ev.stdout || ev.stderr || '(no output)'));
  } catch (e) { body.replaceChildren(el('p', 'muted', e.message)); }
}

function closeDrawer() { $('#drawer').hidden = true; $('#scrim').hidden = true; }

/* ── run everything ─────────────────────────────────────────────── */

async function runAll() {
  if (!state.selected) return;
  const btn = $('#run-all');
  btn.disabled = true; btn.textContent = 'Running';
  try {
    const out = await api('/api/run-all', {
      method: 'POST',
      body: JSON.stringify({ checkpoint_id: state.selected, symbol: $('#g-symbol').value || null }),
    });
    const map = { audit: 'auditor', watch: 'riskbot', archive: 'ledgerkeep',
                  haunt: 'sleuth', handoff: 'scribe', warden: 'warden' };
    Object.entries(map).forEach(([stage, key]) => {
      if (out[stage]?.result) state.results[key] = out[stage].result;
    });
    if (out.context) setContext(out.context);
    renderSections(); loadLedger(); loadStatus();
  } catch (e) { alert(e.message); }
  finally { btn.disabled = false; btn.textContent = 'Run risk check'; }
}

/* ── boot ───────────────────────────────────────────────────────── */

async function boot() {
  $('#run-all').onclick = runAll;
  $('#verify-chain').onclick = verifyChain;
  $('#bleed-cta').onclick = () => $('#report').scrollIntoView({ behavior: 'smooth' });
  $('#g-snapshot').onclick = () => graphAction('snapshot');
  $('#g-recheck').onclick = () => graphAction('recheck');
  $('#disc-recheck').onclick = () => graphAction('recheck');
  $('#disc-sync').onclick = syncWarehouse;
  $('#genie-run').onclick = runQuery;
  $('#drawer-close').onclick = closeDrawer;
  $('#scrim').onclick = closeDrawer;
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });

  try {
    const p = await api('/api/personas');
    state.personas = p.personas;
    state.cast = p.cast || Object.keys(p.personas);
  } catch { /* grid stays empty */ }
  renderCapabilities();
  renderSections();

  await Promise.allSettled([loadStatus(), loadCheckpoints(), loadLedger(), loadQueries()]);
}

boot();
