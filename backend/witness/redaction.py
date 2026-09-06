"""Secret-pattern scan + redaction.

Non-negotiable per CLAUDE.md section 9: nothing reaches the audit-ledger repo,
BUILDATHON.md, or any pushed artifact without passing through here first.

Two detectors run together:
  1. Named patterns  - high-confidence provider key shapes.
  2. Entropy sweep   - catches long opaque tokens the patterns miss.

Redaction is destructive by design. We keep a fingerprint (sha256 prefix) so
the same secret is recognisable across records without ever storing the value.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

# --------------------------------------------------------------------------
# Named patterns. Ordered most-specific first so the label is meaningful.
# --------------------------------------------------------------------------

NAMED_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("anthropic_api_key", re.compile(r"sk-ant-(?:api\d{2}-)?[A-Za-z0-9_\-]{32,}")),
    ("openai_api_key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{32,}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("github_pat", re.compile(r"github_pat_[A-Za-z0-9_]{60,}")),
    ("slack_token", re.compile(r"xox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("aws_access_key_id", re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}")),
    ("google_api_key", re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    ("databricks_pat", re.compile(r"dapi[0-9a-f]{32}(?:-\d+)?")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("private_key_block", re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |PGP |DSA )?PRIVATE KEY-----"
        r".*?-----END (?:RSA |EC |OPENSSH |PGP |DSA )?PRIVATE KEY-----",
        re.DOTALL,
    )),
    ("bearer_header", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-\.=]{24,}")),
    # key=value / key: "value" assignments for secret-ish names
    ("assigned_secret", re.compile(
        r"(?i)\b(?:api[_\-]?key|secret|passwd|password|token|access[_\-]?key|"
        r"client[_\-]?secret|auth[_\-]?token)\b\s*[:=]\s*[\"']?([A-Za-z0-9_\-\./+]{16,})[\"']?"
    )),
    ("connection_string_password", re.compile(
        r"(?i)\b[a-z0-9+]{2,15}://[^\s:/@]+:([^\s@/]{6,})@"
    )),
]

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Entropy sweep: long opaque runs that look like encoded material.
_CANDIDATE = re.compile(r"[A-Za-z0-9_\-+/=]{28,}")
ENTROPY_THRESHOLD = 4.0

# Things that are long and high-entropy but are NOT secrets. Without this the
# sweep flags every git sha and base64 blob in a transcript, and the report
# becomes noise nobody reads.
_ENTROPY_ALLOW = re.compile(
    r"^(?:"
    r"(?i:[0-9a-f]{7,40})"                 # git object ids
    r"|(?i:sha256[:\-][0-9a-f]{64})"
    r"|(?i:[A-Za-z0-9_\-]*(?:test|example|sample|placeholder|dummy|redacted)[A-Za-z0-9_\-]*)"
    # Env var NAMES are not secrets. Case-sensitive on purpose: a real token is
    # rarely all-caps-with-underscores, and docs are full of these.
    r"|[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+"
    r")$"
)


def shannon_entropy(value: str) -> float:
    """Bits of entropy per character."""
    if not value:
        return 0.0
    counts: dict[str, int] = {}
    for ch in value:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(value)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def fingerprint(value: str) -> str:
    """Stable, non-reversible id for a redacted value."""
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


@dataclass
class Finding:
    kind: str
    fingerprint: str
    preview: str          # first 4 chars only, enough to locate, not to use
    detector: str         # "pattern" | "entropy"
    entropy: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {
            "kind": self.kind,
            "fingerprint": self.fingerprint,
            "preview": self.preview,
            "detector": self.detector,
        }
        if self.entropy is not None:
            d["entropy"] = round(self.entropy, 2)
        return d


@dataclass
class ScanResult:
    text: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.findings

    def to_dict(self) -> dict[str, Any]:
        return {
            "clean": self.clean,
            "count": len(self.findings),
            "findings": [f.to_dict() for f in self.findings],
        }


def _placeholder(kind: str, fp: str) -> str:
    return f"[REDACTED:{kind}:{fp}]"


def scan_and_redact(text: str) -> ScanResult:
    """Replace every detected secret in `text` and report what was found."""
    if not text:
        return ScanResult(text=text)

    findings: list[Finding] = []
    out = text

    for kind, pattern in NAMED_PATTERNS:
        def _sub(m: re.Match[str]) -> str:
            # For patterns with a capture group, only the group is the secret;
            # keeping the surrounding key name preserves readability.
            secret = m.group(1) if m.groups() else m.group(0)
            fp = fingerprint(secret)
            findings.append(Finding(
                kind=kind, fingerprint=fp, preview=secret[:4], detector="pattern",
            ))
            return m.group(0).replace(secret, _placeholder(kind, fp))

        out = pattern.sub(_sub, out)

    # Entropy sweep over what survived the named patterns.
    def _entropy_sub(m: re.Match[str]) -> str:
        token = m.group(0)
        # `NAME=value` swept as one token reads as high-entropy purely because
        # of the name. Judge the value alone; a genuine secret on the right is
        # still caught (and the assigned_secret pattern already ran).
        if "=" in token:
            name, _, value = token.partition("=")
            if _ENV_NAME.match(name) and value:
                token = value
        if _ENTROPY_ALLOW.match(token):
            return token
        e = shannon_entropy(token)
        if e < ENTROPY_THRESHOLD:
            return token
        fp = fingerprint(token)
        findings.append(Finding(
            kind="high_entropy_string", fingerprint=fp, preview=token[:4],
            detector="entropy", entropy=e,
        ))
        return _placeholder("high_entropy_string", fp)

    out = _CANDIDATE.sub(_entropy_sub, out)
    return ScanResult(text=out, findings=findings)


def redact_obj(obj: Any) -> tuple[Any, list[Finding]]:
    """Walk a JSON-shaped structure, redacting every string leaf.

    Keys are scanned too - a dict key is as capable of carrying a token as a
    value, and ledger records are written key-and-all.
    """
    findings: list[Finding] = []

    def walk(node: Any) -> Any:
        if isinstance(node, str):
            res = scan_and_redact(node)
            findings.extend(res.findings)
            return res.text
        if isinstance(node, dict):
            return {walk(k): walk(v) for k, v in node.items()}
        if isinstance(node, (list, tuple)):
            return [walk(v) for v in node]
        return node

    return walk(obj), findings


def assert_clean(text: str, where: str) -> None:
    """Hard gate. Raises if `text` still carries a detectable secret.

    Used on the final write path so a bug upstream fails loudly instead of
    silently committing a key into the tamper-evident ledger.
    """
    res = scan_and_redact(text)
    if not res.clean:
        kinds = sorted({f.kind for f in res.findings})
        raise ValueError(
            f"refusing to write {where}: {len(res.findings)} secret-pattern "
            f"hit(s) survived redaction ({', '.join(kinds)})"
        )


def summarise(findings: Iterable[Finding]) -> dict[str, int]:
    """Counts per kind - what The Watchman puts in its security band."""
    out: dict[str, int] = {}
    for f in findings:
        out[f.kind] = out.get(f.kind, 0) + 1
    return out
