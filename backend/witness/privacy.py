"""The privacy boundary: nothing raw leaves this machine.

Two things in this stack are external services: the **Databricks** mirror and
the **Claude API** call inside the personas. Neither may receive a raw
checkpoint transcript or raw prompt text. Both paths call through `redact()` —
one function, not two implementations that can drift apart.

The assumption this replaces
----------------------------
Before the privacy boundary, the Auditor recovered intent by shipping the raw
transcript to the model. That assumed the transcript could leave the machine.
It cannot. So intent extraction moved **on-machine** (`extract_requirements`),
and only the structured claims it produces cross the boundary.

What may cross
--------------
Requirement text, diff summary, test pass/fail counts, risk flags, file paths,
symbol names. That is the whole allowlist, and it is enforced by construction:
`redact()` builds a new object out of named fields rather than filtering an
existing one, so a field nobody thought about cannot ride along by default.

What is reported back
---------------------
Every call returns a `RedactionReport` saying which fields were withheld and
which were absent. That is what makes graceful degradation possible: the
pipeline can produce a partial result and say so, rather than guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .redaction import scan_and_redact

# Labels. `redacted` extends the existing evidence-labeling vocabulary rather
# than introducing a parallel privacy mode - a claim resting on withheld input
# is a claim we cannot verify, which is a statement the label system already
# knows how to make.
STATUS_SATISFIED = "satisfied"
STATUS_PARTIAL = "partial"
STATUS_UNVERIFIED = "unverified"
STATUS_CONTRADICTED = "contradicted"
STATUS_REDACTED = "redacted"

EVIDENCE_STATUSES = (
    STATUS_SATISFIED, STATUS_PARTIAL, STATUS_UNVERIFIED,
    STATUS_CONTRADICTED, STATUS_REDACTED,
)

# A status that cannot support a positive claim.
NON_AFFIRMING = (STATUS_UNVERIFIED, STATUS_REDACTED)

# Fields allowed across the boundary, per destination. Anything not listed here
# never leaves, including by accident.
ALLOWED_FIELDS = {
    "claude": (
        "requirement_text", "diff_summary", "files_touched", "symbols",
        "test_passed", "test_failed", "test_names", "risk_flags",
        "checkpoint_id", "branch", "evidence_id", "evidence_kind",
        "command", "exit_code", "status",
    ),
    "databricks": (
        "checkpoint_id", "requirement_text", "status", "evidence_ref",
        "symbol", "score", "verdict", "category", "confidence",
        "owner", "created_at", "expires_at", "branch", "author",
        "files_touched", "risk_flags", "context_state",
    ),
}

# Raw text that must never cross, whatever it is called.
FORBIDDEN_KEYS = (
    "transcript", "raw_output", "stdout", "stderr", "prompt", "system",
    "messages", "reasoning", "evidence_quote", "rationale", "detail",
    "summary", "text", "note", "why", "source",
)


@dataclass
class RedactionReport:
    """What was withheld, and what was simply not there.

    The two are different and the distinction matters: a withheld field means
    the data exists but may not leave; a missing field means we never had it.
    Both force a non-affirming label, but only the first is a privacy event.
    """

    withheld: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    present: list[str] = field(default_factory=list)
    destination: str = ""

    @property
    def redacted_count(self) -> int:
        return len(self.withheld) + len(self.missing)

    @property
    def partial(self) -> bool:
        return self.redacted_count > 0

    @property
    def context_state(self) -> str:
        return "partial" if self.partial else "full"

    def badge(self) -> str:
        """The string the UI shows at the top of every report screen."""
        if not self.partial:
            return "Full context"
        n = self.redacted_count
        return f"Partial — {n} field{'' if n == 1 else 's'} redacted"

    def to_dict(self) -> dict[str, Any]:
        return {
            "context_state": self.context_state,
            "partial": self.partial,
            "badge": self.badge(),
            "withheld": sorted(self.withheld),
            "missing": sorted(self.missing),
            "present": sorted(self.present),
            "redacted_count": self.redacted_count,
            "destination": self.destination,
        }

    def merge(self, other: "RedactionReport") -> "RedactionReport":
        return RedactionReport(
            withheld=sorted(set(self.withheld) | set(other.withheld)),
            missing=sorted(set(self.missing) | set(other.missing)),
            present=sorted(set(self.present) | set(other.present)),
            destination=self.destination or other.destination,
        )


def redact(payload: dict[str, Any], *, destination: str) -> tuple[dict[str, Any], RedactionReport]:
    """The single boundary crossing. Both external paths call this.

    Builds a new dict from the destination's allowlist. Values still pass the
    secret scanner, because an allowed field can carry a leaked key in its text.
    """
    if destination not in ALLOWED_FIELDS:
        raise ValueError(
            f"unknown destination {destination!r}; refusing to send anything "
            f"across an undefined boundary"
        )

    allowed = ALLOWED_FIELDS[destination]
    report = RedactionReport(destination=destination)
    out: dict[str, Any] = {}

    for key in allowed:
        if key not in payload:
            continue
        value = payload[key]
        if value is None or value == "" or value == []:
            report.missing.append(key)
            continue
        out[key] = _clean(value)
        report.present.append(key)

    # Anything the caller offered that is not allowed is withheld, and named so
    # the UI can say how much context the reader is missing.
    for key in payload:
        if key not in allowed and payload.get(key) not in (None, "", []):
            report.withheld.append(key)

    return out, report


def _clean(value: Any) -> Any:
    """Secret-scan every string that survives the allowlist."""
    if isinstance(value, str):
        return scan_and_redact(value).text
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def assert_no_raw_text(payload: Any, *, where: str) -> None:
    """Hard gate: raise if a forbidden key survived into an outbound payload.

    `redact()` builds from an allowlist so this should be unreachable. It is
    here because 'should be unreachable' is exactly the assumption that breaks
    quietly, and this is the last point before the data leaves the machine.
    """
    def walk(node: Any, path: str = "") -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if str(k).lower() in FORBIDDEN_KEYS:
                    raise ValueError(
                        f"refusing to send {where}: field {path}{k!r} is raw "
                        f"text that must not cross the privacy boundary"
                    )
                walk(v, f"{path}{k}.")
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item, path)

    walk(payload)


# ---------------------------------------------------------------------------
# On-machine intent extraction.
#
# This is the work that used to happen inside the model. It is deliberately
# conservative: it recovers candidate requirements from the parts of a
# transcript that state them, and reports what it could not parse rather than
# inferring. Anything it is unsure about becomes a missing field downstream,
# which forces `redacted` / `unverified` rather than a confident guess.
# ---------------------------------------------------------------------------

_SPEAKER = re.compile(r"^\s*(?:\[[^\]]*\]\s*)?(user|human|assistant)\s*:\s*(.*)$",
                      re.IGNORECASE)

_REQUIREMENT_CUE = re.compile(
    r"\b(?:must|should|need(?:s)? to|has to|have to|required to|ensure|make sure|"
    r"do not|don't|never|always|add|implement|keep|maintain)\b",
    re.IGNORECASE,
)

_TEST_LINE = re.compile(
    r"(?:(\d+)\s*(?:tests?\s*)?pass\w*)|(?:(\d+)\s*(?:tests?\s*)?fail\w*)",
    re.IGNORECASE,
)
_TEST_NAME = re.compile(r"\b(test_[A-Za-z0-9_]+)\b")


def _user_turns(transcript: str) -> list[str]:
    """Text of the human turns - where requirements are actually stated."""
    turns: list[str] = []
    current: list[str] | None = None
    for line in transcript.splitlines():
        m = _SPEAKER.match(line)
        if m:
            if current is not None:
                turns.append("\n".join(current))
                current = None
            if m.group(1).lower() in ("user", "human"):
                current = [m.group(2)]
        elif current is not None:
            current.append(line)
    if current is not None:
        turns.append("\n".join(current))
    return turns


def _split_clauses(text: str) -> Iterable[str]:
    """Split a turn into candidate requirement clauses.

    Newlines are collapsed FIRST: transcripts are hard-wrapped, so treating a
    line break as a clause boundary severs sentences mid-requirement ("every
    refresh must" / "issue a new token"). Only real punctuation ends a clause.
    """
    flat = " ".join(text.split())
    for chunk in re.split(r"(?<=[.;])\s+|\s+-\s+", flat):
        chunk = chunk.strip(" .;-")
        if chunk:
            yield chunk


def extract_requirements(transcript: str) -> tuple[list[dict[str, Any]], RedactionReport]:
    """Recover candidate requirements locally. The transcript never leaves.

    Returns the requirements plus a report noting whether extraction was
    complete enough to be trusted.
    """
    report = RedactionReport(destination="local")
    if not transcript or not transcript.strip():
        report.missing.append("transcript")
        return [], report

    requirements: list[dict[str, Any]] = []
    for turn in _user_turns(transcript):
        for clause in _split_clauses(turn):
            if len(clause) < 8 or not _REQUIREMENT_CUE.search(clause):
                continue
            requirements.append({
                "id": f"req-{len(requirements) + 1}",
                "requirement_text": clause,
                "extracted_by": "local",
            })

    if not requirements:
        # The transcript existed but stated nothing we could recognise as a
        # requirement. That is a gap in what we can verify, not an all-clear.
        report.missing.append("requirement_text")
    else:
        report.present.append("requirement_text")
    return requirements, report


def extract_test_results(text: str) -> tuple[dict[str, Any], RedactionReport]:
    """Pass/fail counts and failing test names — structured, never prose."""
    report = RedactionReport(destination="local")
    result: dict[str, Any] = {}
    if not text:
        report.missing.extend(["test_passed", "test_failed"])
        return result, report

    passed = failed = None
    for m in _TEST_LINE.finditer(text):
        if m.group(1) is not None:
            passed = int(m.group(1))
        if m.group(2) is not None:
            failed = int(m.group(2))

    if passed is None and failed is None:
        report.missing.extend(["test_passed", "test_failed"])
    else:
        result["test_passed"] = passed
        result["test_failed"] = failed
        report.present.append("test_passed" if passed is not None else "test_failed")

    names = sorted(set(_TEST_NAME.findall(text)))
    if names:
        result["test_names"] = names
        report.present.append("test_names")
    return result, report


def summarise_diff(diff_stat: str) -> tuple[dict[str, Any], RedactionReport]:
    """File names and counts from a diff — never the diff body."""
    report = RedactionReport(destination="local")
    if not diff_stat or not diff_stat.strip():
        report.missing.append("diff_summary")
        return {}, report

    files: list[str] = []
    for line in diff_stat.splitlines():
        if "|" in line:
            name = line.split("|", 1)[0].strip()
            if name:
                files.append(name)

    if not files:
        report.missing.append("diff_summary")
        return {}, report

    report.present.append("diff_summary")
    return {
        "diff_summary": f"{len(files)} file(s) changed",
        "files_touched": files,
    }, report


def downgrade_for_partial(status: str, report: RedactionReport) -> str:
    """A claim resting on withheld input cannot be presented as verified.

    `satisfied` and `partial` are affirming claims, so they fall back to
    `redacted`. `contradicted` survives: proving something is broken does not
    require complete context, and suppressing it would be the dangerous
    direction to fail in.
    """
    if not report.partial:
        return status
    if status in (STATUS_SATISFIED, STATUS_PARTIAL):
        return STATUS_REDACTED
    return status


def score_is_presentable(statuses: Iterable[str]) -> bool:
    """A numeric risk score is only honest over fully-supported inputs.

    If any input carries `redacted` or `unverified`, averaging produces a clean
    number that hides an unknown. The Watchman reports `unverified` instead.
    """
    return not any(s in NON_AFFIRMING for s in statuses)
