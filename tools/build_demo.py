#!/usr/bin/env python3
"""Build the standalone demo page.

The live dashboard talks to the FastAPI backend in `witness.api`. For sharing a
link, that backend isn't reachable, so this script captures the API responses
from a real pipeline run and inlines them into a single self-contained HTML
file alongside the frontend's own CSS and JS.

What it produces is a *recording*, not a mock: every response is the genuine
output of running the pipeline against the fixtures in this repo, including the
ledger's real commit hashes and a real `git fsck`. The page says so in a banner,
and its `api()` replays those responses rather than pretending to execute.

Usage:
    python3 tools/build_demo.py [--out witness-demo.html]

The ledger is rebuilt from scratch each run so the captured commit chain matches
what the page displays.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from witness.agents import PERSONAS                      # noqa: E402
from witness.databricks_sync import GENIE_QUERIES        # noqa: E402
from witness.pipeline import Witness                     # noqa: E402

CHECKPOINT = "cp_7f3a91"
SYMBOL = "validate_token"


def capture(ledger_path: Path) -> dict:
    """Run the pipeline for real and record every response the page needs."""
    if ledger_path.exists():
        shutil.rmtree(ledger_path)

    out: dict = {"/api/personas": {"personas": PERSONAS}}

    # Checkpoint-time state: ingest the checkpoint and snapshot the blast radius.
    at_checkpoint = Witness(ROOT, ledger_path, fixture_dir=ROOT / "fixtures")
    out["/api/checkpoints"] = at_checkpoint.list_checkpoints()
    out["POST /api/run-all"] = at_checkpoint.run_all(CHECKPOINT, symbol=SYMBOL)

    # HEAD state: re-run the identical graph command and diff it. This is the
    # stale-claim catch, and it has to come from a second fixture set because
    # "HEAD" means the repo as it stands now, not as it stood at the checkpoint.
    at_head = Witness(ROOT, ledger_path, fixture_dir=ROOT / "fixtures-head")
    out["POST /api/graph/recheck"] = at_head.recheck_symbol(SYMBOL, CHECKPOINT)

    out["/api/status"] = at_head.status()
    out["/api/ledger"] = {**at_head.ledger.stats(),
                          "history": at_head.ledger.history(limit=40)}
    out["/api/ledger/verify"] = at_head.ledger.verify_chain()
    out["POST /api/databricks/sync"] = at_head.databricks.sync().to_dict()
    out["/api/databricks/queries"] = {
        "queries": {k: v["question"] for k, v in GENIE_QUERIES.items()}
    }
    for key in GENIE_QUERIES:
        out[f"/api/databricks/genie/{key}"] = at_head.databricks.genie(key)

    # Every evidence record, so the drawer resolves any cited id.
    evidence = {e.id: e.to_dict() for e in at_head.store.all()}
    for name in at_head.ledger.list("evidence"):
        rec = at_head.ledger.read("evidence", name)
        if rec and rec.get("id"):
            evidence[rec["id"]] = rec
    out["_evidence"] = evidence

    return out


DEMO_API = '''
/* DEMO BUILD ------------------------------------------------------------
 * The live app talks to the FastAPI backend in api.py. This hosted build has
 * no backend, so api() resolves against responses recorded from a real run:
 * real Entire command output, real ledger commits, a real git fsck, and a
 * real stale-claim re-check. Nothing here is invented - but nothing here is
 * live either, so actions replay rather than execute.
 * ---------------------------------------------------------------------- */
const DEMO = window.__WITNESS_DEMO__;
const TOKEN = '';

function demoResolve(path, method) {
  if (method === 'POST') {
    if (path === '/api/graph/recheck')     return DEMO['POST /api/graph/recheck'];
    if (path === '/api/databricks/sync')   return DEMO['POST /api/databricks/sync'];
    if (path === '/api/run-all')           return DEMO['POST /api/run-all'];
    if (path === '/api/graph/snapshot')    return DEMO['POST /api/run-all'].snapshot;
    const stage = { '/api/audit': 'audit', '/api/watch': 'watch', '/api/archive': 'archive',
                    '/api/haunt': 'haunt', '/api/handoff': 'handoff' }[path];
    if (stage) return DEMO['POST /api/run-all'][stage];
    if (path === '/api/referee') {
      const e = new Error('The Referee has no run to compare yet - it needs one Auditor pass '
        + 'either side of the noon constraint, and the constraint has not arrived.');
      e.body = { error: e.message };
      throw e;
    }
  }
  if (path.startsWith('/api/evidence/')) {
    const id = path.split('/').pop();
    const ev = DEMO._evidence[id];
    if (ev) return ev;
    const e = new Error('no evidence recorded with id ' + id);
    e.body = { error: e.message };
    throw e;
  }
  if (DEMO[path]) return DEMO[path];
  const e = new Error('not part of this recorded demo: ' + path);
  e.body = { error: e.message };
  throw e;
}

async function api(path, opts = {}) {
  // A short delay so the pipeline states are legible rather than instantaneous.
  await new Promise((r) => setTimeout(r, 260));
  return demoResolve(path, opts.method || 'GET');
}
'''

BANNER = """<div class="demo-note">
  <span class="demo-k">DEMO BUILD</span>
  <span>Recorded from a real run &mdash; real Entire output, real ledger commits,
  a real <code>git fsck</code>, and a real stale-claim re-check. Actions replay
  that run rather than execute live. The Auditor, Watchman, Archivist, Ghost and
  Messenger show verdict-free output because no <code>ANTHROPIC_API_KEY</code>
  was set &mdash; that is the designed degradation, not a stub.</span>
</div>"""

BANNER_CSS = """
/* demo-build notice - deliberately not a card; it is an annotation on the page */
.demo-note {
  max-width: 1320px; margin: 18px auto -14px; padding: 12px 22px;
  display: flex; gap: 14px; align-items: baseline; flex-wrap: wrap;
  font-size: 11px; line-height: 1.7; color: var(--ink-2);
  border-left: 2px solid var(--amber);
}
.demo-note .demo-k {
  font-size: 9px; letter-spacing: .24em; color: var(--amber); flex: none;
}
.demo-note code {
  font-family: var(--mono); font-size: 10.5px; color: var(--ghost);
  background: rgba(255,255,255,.05); padding: 1px 5px; border-radius: 2px;
}
"""

REST_STRIP = """  setStrip('Audit pipeline',
    live ? 'Select a checkpoint to begin'
         : replaying
           ? 'Entire CLI absent — replaying recorded command output from fixtures/'
           : 'Entire CLI not detected and no fixtures recorded — no verdicts can be asserted',
    'IDLE', 'idle');"""

DEMO_STRIP = """  setStrip('Audit pipeline',
    'Recorded run · select a checkpoint, or re-check validate_token below',
    'DEMO', 'idle');"""


def build(data: dict) -> str:
    front = ROOT / "frontend"
    html = (front / "index.html").read_text()
    css = (front / "assets" / "styles.css").read_text()
    js = (front / "assets" / "app.js").read_text()

    # The artifact host supplies <head>/<body>, so ship only the page content.
    body = html.split("<body>", 1)[1].rsplit("</body>", 1)[0]
    body = body.replace('<script src="/assets/app.js"></script>', "")
    body = body.replace("<main id=\"top\">", "<main id=\"top\">\n" + BANNER, 1)
    fonts = re.search(r'<link href="https://fonts\.googleapis[^>]+>', html).group(0)

    js = DEMO_API + "\n" + js[js.index("const $  ="):]
    if REST_STRIP in js:
        js = js.replace(REST_STRIP, DEMO_STRIP)

    payload = json.dumps(data, separators=(",", ":"), default=str)
    return (
        f"<title>Witness</title>\n{fonts}\n"
        f"<style>\n{css}\n{BANNER_CSS}\n</style>\n"
        f"{body}\n"
        f"<script>window.__WITNESS_DEMO__ = {payload};</script>\n"
        f"<script>\n{js}\n</script>\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="witness-demo.html")
    ap.add_argument("--ledger", default="audit-ledger")
    args = ap.parse_args()

    data = capture(Path(args.ledger))
    page = build(data)
    out = Path(args.out)
    out.write_text(page)

    recheck = data["POST /api/graph/recheck"]
    print(f"recheck status : {recheck['status']}")
    print(f"ledger commits : {data['/api/ledger']['commits']}")
    print(f"evidence ids   : {len(data['_evidence'])}")
    print(f"wrote          : {out} ({out.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
