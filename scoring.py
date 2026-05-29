"""Pure scoring / stopping logic for the LLM review loop.

No I/O — fully unit-testable. The review loop is reviewer-agnostic: any reviewer
LLM is told (via the injected legend below) to tag each finding as
``[SEVERITY/TYPE]``; this module turns those tags into a numeric value and
decides when the loop has converged.

Stopping rule: when a review's summed finding-value is <= ``stop_cutoff`` for
``stop_consecutive`` APPROVE rounds in a row, the loop is converged. CRITICAL /
WARNING findings are heavy enough that they can never count as "low", so blocking
work never triggers an early stop.
"""
import re

# Default value index + thresholds. Override per-project via config.
DEFAULT_CONFIG = {
    # Weight = how much acting on a finding is worth. Severity dominates;
    # SUGGESTION is refined by TYPE.
    "weights": {
        "CRITICAL": 100,                # blocks merge — always act
        "WARNING": 40,                  # should fix — escalate to a human
        "SUGGESTION/correctness": 15,   # claimed-but-untested behaviour / real risk
        "SUGGESTION/test": 10,          # missing/weak test for existing behaviour
        "SUGGESTION/docs": 8,           # doc <-> code mismatch
        "SUGGESTION/consistency": 4,    # counts, naming, internal mismatch
        "SUGGESTION/style": 1,          # subjective polish / nice-to-have / future
    },
    "default_suggestion_weight": 4,     # SUGGESTION with missing/unknown TYPE
    "stop_cutoff": 5,                   # a review scoring <= this is "low value"
    "stop_consecutive": 2,              # stop after this many low reviews in a row
    "escalate_min": 40,                 # findings >= this are surfaced to a human
}

VALID_TYPES = ("correctness", "test", "docs", "consistency", "style")

# Injected into every review prompt so any reviewer LLM emits the same
# machine-scoreable tags — this is the shared convention.
FINDINGS_LEGEND = (
    "Tag EVERY finding as [SEVERITY/TYPE].\n"
    "SEVERITY: CRITICAL (blocks merge) | WARNING (should fix) | SUGGESTION (optional).\n"
    "TYPE: correctness (logic/behaviour risk) | test (missing/weak test) | "
    "docs (doc<->code mismatch) | consistency (counts, naming, internal mismatch) | "
    "style (subjective polish / nice-to-have / future hardening).\n"
    "Example: - [SUGGESTION/test] Add a regression test for the empty-input path."
)


def parse_findings(review_text):
    """Return a list of (severity, type_or_None) from a review body.

    Recognises lines like ``- [SUGGESTION/test] ...`` (tolerant of case and
    spacing). An explicit ``- none`` line yields no finding.
    """
    findings = []
    for line in review_text.splitlines():
        s = line.strip()
        if re.match(r"^-\s*none\b", s, re.IGNORECASE):
            continue
        m = re.match(r"^-\s*\[\s*([A-Za-z]+)\s*(?:/\s*([A-Za-z]+)\s*)?\]", s)
        if not m:
            continue
        sev = m.group(1).upper()
        typ = (m.group(2) or "").lower() or None
        findings.append((sev, typ))
    return findings


def weight_of(severity, type_, cfg=DEFAULT_CONFIG):
    w = cfg["weights"]
    if severity in ("CRITICAL", "WARNING"):
        return w.get(severity, 0)
    if severity == "SUGGESTION":
        if type_:
            return w.get(f"SUGGESTION/{type_}", cfg["default_suggestion_weight"])
        return cfg["default_suggestion_weight"]
    return 0


def score_review(findings, cfg=DEFAULT_CONFIG):
    return sum(weight_of(sev, typ, cfg) for sev, typ in findings)


def decide_stop(score, verdict, prev_consecutive_low, cfg=DEFAULT_CONFIG):
    """Return (stop: bool, consecutive_low: int).

    A review is "low" when its score <= stop_cutoff AND the verdict is APPROVE.
    Stop once that has held for stop_consecutive reviews in a row.
    """
    low = (score <= cfg["stop_cutoff"]) and (verdict == "APPROVE")
    consecutive = prev_consecutive_low + 1 if low else 0
    stop = consecutive >= cfg["stop_consecutive"]
    return stop, consecutive


def classify(findings, cfg=DEFAULT_CONFIG):
    """Split findings into (escalate_to_human, apply_automatically) by weight."""
    escalate, apply_ = [], []
    for sev, typ in findings:
        target = escalate if weight_of(sev, typ, cfg) >= cfg["escalate_min"] else apply_
        target.append((sev, typ))
    return escalate, apply_
