"""Tests for the rules Witness cannot be allowed to break.

The product's whole claim is 'never invent a verdict'. These tests exist to
make that claim checkable rather than aspirational.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from witness.agents import AgentRunner, _enforce_citations, decay_assumptions
from witness.databricks_sync import DatabricksSync
from witness.entire_adapter import Evidence, EvidenceStore, parse_checkpoint_list
from witness.graph_check import diff_snapshots, extract_surface, GraphSnapshot
from witness.ledger import AuditLedger
from witness.redaction import assert_clean, scan_and_redact


# ── redaction ───────────────────────────────────────────────────────

# Test vectors are assembled at import time rather than written as literals.
# They are entirely synthetic, but a realistic token *shape* sitting in a file
# trips GitHub push protection and every other scanner pointed at this repo -
# which is noise, and the kind of noise this project exists to avoid.
SYNTHETIC_SECRETS = {
    "anthropic":  "sk-" + "ant-api03-" + "AAAAbbbbCCCCdddd" * 3,
    "github":     "ghp" + "_" + "AAAAbbbbCCCCddddEEEEffffGGGGhhhhIIII",
    "aws":        "AKIA" + "IOSFODNN7EXAMPLE",
    "databricks": "dapi" + "0123456789abcdef" * 2,
    "slack":      "xox" + "b-" + "1234567890-abcdefghijklmno",
}


@pytest.mark.parametrize("key", sorted(SYNTHETIC_SECRETS))
def test_named_secrets_are_redacted(key):
    secret = SYNTHETIC_SECRETS[key]
    out = scan_and_redact(f"token is {secret} ok")
    assert secret not in out.text
    assert not out.clean


def test_git_sha_is_not_flagged():
    """The entropy sweep must not drown the report in git object ids."""
    text = "commit a3f9c21e4b5d6f7a8b9c0d1e2f3a4b5c6d7e8f90 touched auth/session.py"
    assert scan_and_redact(text).clean


@pytest.mark.parametrize("text", [
    "WITNESS_ACCESS_TOKEN",              # env var NAME, not a value
    "DATABRICKS_SERVER_HOSTNAME",
    "WITNESS_FIXTURES=fixtures-head",    # assignment of a non-secret value
    "test_hallucinated_citation_downgrades_auditor_finding",
])
def test_entropy_sweep_does_not_flag_documentation(text):
    """False positives make the security report unreadable, so they matter."""
    assert scan_and_redact(text).clean


@pytest.mark.parametrize("text", [
    "DATABRICKS_TOKEN=" + SYNTHETIC_SECRETS["databricks"],
    "MY_KEY=xJ9kQm2vPz7LwR4tYb8NcF6hGd3sEa1uZo5iVn0pXr",
])
def test_secret_in_assignment_still_caught(text):
    """Narrowing the sweep must not open a hole on the right-hand side."""
    assert not scan_and_redact(text).clean


def test_assert_clean_raises_on_leak():
    with pytest.raises(ValueError, match="secret-pattern"):
        assert_clean("key=sk-ant-api03-" + "A" * 40, "test")


def test_redaction_is_not_reversible():
    res = scan_and_redact(SYNTHETIC_SECRETS["aws"])
    assert SYNTHETIC_SECRETS["aws"] not in res.text
    assert res.findings[0].fingerprint.startswith("sha256:")


# ── adapter ─────────────────────────────────────────────────────────

def test_missing_cli_yields_unavailable_not_exception(tmp_path):
    from witness.entire_adapter import EntireAdapter
    a = EntireAdapter(tmp_path, EvidenceStore())
    a._capabilities = {"binary": False, "checkpoint": False, "graph": False}
    ev = a.checkpoint_list()
    assert ev.status == "unavailable"
    assert not ev.ok
    assert ev.stdout == ""          # never a fabricated payload


def test_parse_checkpoint_list_tolerates_shapes():
    def mk(payload):
        return Evidence("e", [], "ok", 0, json.dumps(payload), "", "", 0, "", ".")

    bare = mk([{"id": "cp1", "summary": "s"}])
    wrapped = mk({"checkpoints": [{"id": "cp1", "summary": "s"}]})
    assert parse_checkpoint_list(bare)[0]["id"] == "cp1"
    assert parse_checkpoint_list(wrapped)[0]["id"] == "cp1"
    assert parse_checkpoint_list(mk({"nope": 1})) == []


def test_unparseable_output_yields_no_checkpoints():
    ev = Evidence("e", [], "ok", 0, "not json at all", "", "", 0, "", ".")
    assert parse_checkpoint_list(ev) == []


# ── ledger ──────────────────────────────────────────────────────────

def test_ledger_commits_and_verifies(tmp_path):
    led = AuditLedger(tmp_path / "ledger")
    led.init()
    prov = led.write("checkpoints", "cp1", {"id": "cp1"}, message="ingest: cp1")
    assert prov["commit"]
    assert led.verify_chain()["ok"]


def test_ledger_redacts_before_commit(tmp_path):
    led = AuditLedger(tmp_path / "ledger")
    led.init()
    led.write("checkpoints", "cp1",
              {"id": "cp1", "log": "key sk-ant-api03-" + "B" * 40}, message="m")
    on_disk = (tmp_path / "ledger" / "checkpoints" / "cp1.json").read_text()
    assert "sk-ant-api03-BBBB" not in on_disk
    assert "REDACTED" in on_disk


def test_ledger_slug_blocks_path_traversal(tmp_path):
    led = AuditLedger(tmp_path / "ledger")
    led.init()
    led.write("checkpoints", "../../escaped", {"id": "x"}, message="m")
    assert not (tmp_path / "escaped.json").exists()
    assert list((tmp_path / "ledger" / "checkpoints").glob("*.json"))


def test_ledger_rejects_unknown_subdir(tmp_path):
    led = AuditLedger(tmp_path / "ledger")
    led.init()
    with pytest.raises(Exception):
        led.write("evil", "x", {}, message="m")


def test_no_empty_commits(tmp_path):
    led = AuditLedger(tmp_path / "ledger")
    led.init()
    led.write("checkpoints", "cp1", {"id": "cp1"}, message="m")
    before = len(led.history(100))
    led._commit("nothing changed")
    assert len(led.history(100)) == before


# ── graph staleness ─────────────────────────────────────────────────

def _snap(symbol, raw, **kw):
    return GraphSnapshot(
        id=kw.get("id", "g"), symbol=symbol, checkpoint_id="cp1", depth=2,
        captured_at=kw.get("at", "t"), head_sha=kw.get("sha", "a"),
        evidence_id=kw.get("ev", "ev1"), evidence_status="ok",
        raw_output=raw, surface=extract_surface(raw),
    )


BEFORE = """Impact: validate_token
Callers:
  api/routes.py -> require_auth
  auth/session.py -> refresh_session
"""


def test_identical_radius_holds():
    d = diff_snapshots(_snap("validate_token", BEFORE), _snap("validate_token", BEFORE))
    assert d["status"] == "holds"


def test_new_caller_is_stale():
    after = BEFORE + "  admin/console.py -> impersonate_user\n"
    d = diff_snapshots(_snap("validate_token", BEFORE), _snap("validate_token", after))
    assert d["status"] == "stale"
    assert "impersonate_user" in d["symbols_added"]


def test_removed_caller_is_stale():
    after = "Impact: validate_token\nCallers:\n  api/routes.py -> require_auth\n"
    d = diff_snapshots(_snap("validate_token", BEFORE), _snap("validate_token", after))
    assert d["status"] == "stale"
    assert "refresh_session" in d["symbols_removed"]


def test_unusable_evidence_never_yields_a_verdict():
    bad = _snap("validate_token", BEFORE)
    bad.evidence_status = "unavailable"
    bad.raw_output = ""
    d = diff_snapshots(bad, _snap("validate_token", BEFORE))
    assert d["status"] == "unverified"


def test_file_paths_do_not_leak_into_symbols():
    surface = extract_surface("  admin/refund.py -> issue_refund\n")
    assert "admin/refund.py" in surface["files"]
    assert "refund.py" not in surface["symbols"]
    assert "issue_refund" in surface["symbols"]


def test_cannot_diff_different_symbols():
    with pytest.raises(ValueError):
        diff_snapshots(_snap("a", BEFORE), _snap("b", BEFORE))


# ── agents ──────────────────────────────────────────────────────────

def test_no_usable_evidence_means_no_verdict():
    runner = AgentRunner(api_key="unused")
    ev = Evidence("e", [], "unavailable", None, "", "no cli", "", 0, "", ".")
    res = runner.run("auditor", [ev])
    assert res.degraded
    assert res.data["requirements"] == []


def test_missing_api_key_is_degraded_not_guessed():
    runner = AgentRunner(api_key=None)
    ev = Evidence("e", [], "ok", 0, "some output", "", "", 0, "", ".")
    res = runner.run("watchman", [ev])
    assert res.degraded
    assert res.data["score"] is None
    assert res.data["verdict"] == "unverified"


def test_hallucinated_citation_downgrades_auditor_finding():
    data = _enforce_citations(
        "auditor",
        {"requirements": [{"text": "x", "status": "satisfied", "evidence_id": "ev_fake"}]},
        {"ev_real"},
    )
    assert data["requirements"][0]["status"] == "unverified"
    assert "citation_error" in data["requirements"][0]


def test_uncited_watchman_risk_is_dropped():
    data = _enforce_citations(
        "watchman",
        {"risks": [{"title": "x", "severity": "critical", "evidence_id": "nope"}]},
        {"ev_real"},
    )
    assert data["risks"] == []


def test_invalid_status_becomes_unverified():
    data = _enforce_citations(
        "auditor",
        {"requirements": [{"text": "x", "status": "definitely_fine", "evidence_id": "ev_real"}]},
        {"ev_real"},
    )
    assert data["requirements"][0]["status"] == "unverified"


def test_assumption_decay_reaches_zero_at_expiry():
    out = decay_assumptions([{
        "text": "a", "confidence": 1.0, "expires_in_days": 1,
        "created_at": "2020-01-01T00:00:00+00:00",
    }])
    assert out[0]["decayed_confidence"] == 0.0
    assert out[0]["decay_status"] == "expired"


# ── databricks ──────────────────────────────────────────────────────

def test_local_mirror_round_trip(tmp_path):
    led = AuditLedger(tmp_path / "ledger")
    led.init()
    led.write("checkpoints", "cp1", {"id": "cp1", "summary": "s"}, message="m")
    led.write("requirements", "cp1", {
        "checkpoint_id": "cp1",
        "requirements": [{"id": "r1", "text": "t", "status": "contradicted",
                          "evidence_id": "ev1"}],
    }, message="m")

    sync = DatabricksSync(led, local_db=tmp_path / "m.db")
    assert sync.backend == "sqlite"          # unconfigured -> local mirror
    res = sync.sync()
    assert res.ok and res.rows_written["requirements"] == 1

    q = sync.genie("unsatisfied_requirements")
    assert q["ok"] and q["row_count"] == 1
    assert q["rows"][0]["status"] == "contradicted"


def test_sync_is_idempotent(tmp_path):
    led = AuditLedger(tmp_path / "ledger")
    led.init()
    led.write("checkpoints", "cp1", {"id": "cp1"}, message="m")
    sync = DatabricksSync(led, local_db=tmp_path / "m.db")
    first = sync.sync().rows_written
    second = sync.sync().rows_written
    assert first == second == {"checkpoints": 1, "requirements": 0, "assumptions": 0,
                               "risk_reports": 0, "graph_snapshots": 0}


def test_sync_without_ledger_fails_cleanly(tmp_path):
    led = AuditLedger(tmp_path / "missing")
    res = DatabricksSync(led, local_db=tmp_path / "m.db").sync()
    assert not res.ok and "not initialised" in res.error


# ── evidence store ──────────────────────────────────────────────────

def test_fixture_round_trip(tmp_path):
    store = EvidenceStore(tmp_path)
    ev = Evidence("e1", ["entire", "checkpoint", "list", "--json"], "ok", 0,
                  '{"checkpoints":[]}', "", "sha", 5, "t", ".")
    store.save_fixture(ev)
    replayed = store.load_fixture(ev.command)
    assert replayed is not None
    assert replayed.status == "replayed"
    assert replayed.source == "fixture"
    assert replayed.stdout == ev.stdout
    assert replayed.id != ev.id      # fresh id per replay, same payload
