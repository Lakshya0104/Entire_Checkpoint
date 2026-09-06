"""Witness CLI.

    witness init                       initialise the audit-ledger repo
    witness status                     what is available on this machine
    witness checkpoints                list checkpoints via Entire
    witness ingest <id>                pull a checkpoint into the ledger
    witness audit <id> [--label L]     Auditor pass
    witness watch <id> [--symbol S]    Watchman risk report
    witness archive <id>               Archivist assumptions
    witness haunt <id>                 Ghost dead-end recovery
    witness handoff <id> [--full]      Messenger packet
    witness snapshot <symbol> <id>     capture blast radius at checkpoint time
    witness recheck <symbol> <id>      re-run at HEAD, diff, flag stale claims
    witness referee <id>               pre/post curveball goal drift
    witness run <id> [--symbol S]      everything, in order
    witness sync                       mirror ledger -> Databricks
    witness genie [key]                run a Genie-style query
    witness verify                     re-verify the ledger hash chain
    witness serve [--port N]           dashboard + API
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .databricks_sync import GENIE_QUERIES
from .pipeline import Witness


def _out(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="witness", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo", default=os.environ.get("WITNESS_REPO", "."))
    p.add_argument("--ledger", default=os.environ.get("WITNESS_LEDGER", "audit-ledger"))
    p.add_argument("--fixtures", default=os.environ.get("WITNESS_FIXTURES", "fixtures"))
    p.add_argument("--record", action="store_true",
                   help="save live Entire output as replayable fixtures")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")
    sub.add_parser("status")
    sub.add_parser("checkpoints")
    sub.add_parser("verify")
    sub.add_parser("sync")

    for name in ("ingest", "archive", "haunt"):
        s = sub.add_parser(name)
        s.add_argument("checkpoint_id")

    s = sub.add_parser("audit"); s.add_argument("checkpoint_id"); s.add_argument("--label", default="primary")
    s = sub.add_parser("watch"); s.add_argument("checkpoint_id"); s.add_argument("--symbol")
    s = sub.add_parser("handoff"); s.add_argument("checkpoint_id"); s.add_argument("--full", action="store_true")
    s = sub.add_parser("run"); s.add_argument("checkpoint_id"); s.add_argument("--symbol")
    s = sub.add_parser("snapshot"); s.add_argument("symbol"); s.add_argument("checkpoint_id"); s.add_argument("--depth", type=int, default=2)
    s = sub.add_parser("recheck"); s.add_argument("symbol"); s.add_argument("checkpoint_id")
    s = sub.add_parser("referee"); s.add_argument("checkpoint_id")
    s.add_argument("--before", default="pre-curveball"); s.add_argument("--after", default="post-curveball")
    s = sub.add_parser("genie"); s.add_argument("key", nargs="?", default=None)
    s = sub.add_parser("serve"); s.add_argument("--port", type=int, default=8000); s.add_argument("--host", default="127.0.0.1")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.cmd == "serve":
        os.environ.setdefault("WITNESS_REPO", args.repo)
        os.environ.setdefault("WITNESS_LEDGER", args.ledger)
        import uvicorn
        if not os.environ.get("WITNESS_ACCESS_TOKEN") and args.host != "127.0.0.1":
            print("refusing to bind a non-loopback host without WITNESS_ACCESS_TOKEN set.\n"
                  "This dashboard exposes reasoning traces and transcript spans.",
                  file=sys.stderr)
            return 2
        uvicorn.run("witness.api:app", host=args.host, port=args.port, reload=False)
        return 0

    w = Witness(args.repo, args.ledger,
                fixture_dir=args.fixtures if os.path.exists(args.fixtures) else None,
                record=args.record)

    if args.cmd == "init":
        _out({"ledger": w.ledger.init(), "databricks": w.databricks.create_tables()})
    elif args.cmd == "status":
        _out(w.status())
    elif args.cmd == "checkpoints":
        _out(w.list_checkpoints())
    elif args.cmd == "ingest":
        _out(w.ingest(args.checkpoint_id))
    elif args.cmd == "audit":
        _out(w.audit(args.checkpoint_id, label=args.label))
    elif args.cmd == "watch":
        _out(w.watch(args.checkpoint_id, symbol=args.symbol))
    elif args.cmd == "archive":
        _out(w.archive(args.checkpoint_id))
    elif args.cmd == "haunt":
        _out(w.haunt(args.checkpoint_id))
    elif args.cmd == "handoff":
        _out(w.handoff(args.checkpoint_id, redacted=not args.full))
    elif args.cmd == "snapshot":
        _out(w.snapshot_symbol(args.symbol, args.checkpoint_id, args.depth))
    elif args.cmd == "recheck":
        _out(w.recheck_symbol(args.symbol, args.checkpoint_id))
    elif args.cmd == "referee":
        _out(w.referee(args.checkpoint_id, args.before, args.after))
    elif args.cmd == "run":
        _out(w.run_all(args.checkpoint_id, symbol=args.symbol))
    elif args.cmd == "sync":
        _out(w.databricks.sync().to_dict())
    elif args.cmd == "verify":
        _out(w.ledger.verify_chain())
    elif args.cmd == "genie":
        if not args.key:
            _out({"available": {k: v["question"] for k, v in GENIE_QUERIES.items()}})
        else:
            _out(w.databricks.genie(args.key))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
