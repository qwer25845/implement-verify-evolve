"""Pure scoring / stopping logic for the review loop.

No I/O — fully unit-testable. The reviewer tags each finding `[SEVERITY/TYPE]`
(see SCORING_RUBRIC.md, the shared judgement guide). This module turns those tags
into a numeric value and decides when the loop has converged.

Fail-safe principle: an unknown severity, an undocumented type, or a malformed
finding line is NEVER silently scored 0. Unknown severities and unparseable lines
are weighted at `escalate_min` and flag the round `needs_human`, so a broken or
ambiguous review can never be silently converged away.
"""
import re

KNOWN_SEVERITIES = ("CRITICAL", "WARNING", "SUGGESTION")
DOCUMENTED_TYPES = ("security", "compatibility", "reliability", "correctness",
                    "test", "docs", "maintainability", "consistency", "style")

DEFAULT_CONFIG = {
    "weights": {
        "CRITICAL": 100,                  # certain break / data / security / merge-block
        "WARNING": 40,                    # reproducible defect, fix before merge
        "SUGGESTION/security": 25,        # plausible security hardening
        "SUGGESTION/compatibility": 20,   # may break existing API/config/callers
        "SUGGESTION/reliability": 15,     # race / flake / silent failure / leak
        "SUGGESTION/correctness": 15,     # plausible logic risk (unproven)
        "SUGGESTION/test": 10,            # missing/weak test
        "SUGGESTION/docs": 8,             # doc/config mismatch
        "SUGGESTION/maintainability": 6,  # brittle structure / duplication
        "SUGGESTION/consistency": 4,      # naming/count/example mismatch
        "SUGGESTION/style": 1,            # subjective polish
    },
    "unknown_type_weight": 15,            # SUGGESTION with a missing/undocumented type (never style/1)
    "stop_cutoff": 5,                     # a review scoring <= this is "low value"
    "stop_consecutive": 2,                # stop after this many low reviews in a row
    "stop_no_escalate_consecutive": 3,    # OR stop after this many rounds with no escalate
    "escalate_min": 40,                   # findings >= this go to a human
}

# Compact decision tree injected into the review prompt (full guide: SCORING_RUBRIC.md).
FINDINGS_LEGEND = (
    "Tag EVERY finding as [SEVERITY/TYPE] on its own '- ' bullet. Decision tree:\n"
    "SEVERITY (top-down, first match):\n"
    "  CRITICAL  - certainly breaks runtime/tests/data/security now; merge-blocking.\n"
    "  WARNING   - reproducible defect on a real user/compat path; fix before merge.\n"
    "  SUGGESTION- plausible-but-unproven, or optional/polish.\n"
    "TYPE: security | compatibility | reliability | correctness | test | docs | "
    "maintainability | consistency | style. Use the most specific domain "
    "(security/compatibility/reliability before correctness).\n"
    "A finding that genuinely spans two categories may be tagged [SEVERITY/A+B] "
    "(two SEPARATE problems -> both score) or [SEVERITY/A|B] (ONE problem fitting "
    "two categories -> only the larger scores).\n"
    "Verdict: REQUEST_CHANGES if any CRITICAL/WARNING or it must be fixed before "
    "merge; APPROVE only if no findings or only low-value optional suggestions.\n"
    "Use EXACTLY this format. A malformed line or an unknown SEVERITY is treated "
    "as needing human review; an unknown/missing TYPE is re-classified once and "
    "then escalated if it still cannot be mapped. Neither is ever ignored.\n"
    "Example: - [SUGGESTION/test] Add a regression test for the empty-input path."
)

_STRICT = re.compile(r"^-\s*\[\s*([A-Za-z]+)\s*(?:/\s*([A-Za-z+|]+)\s*)?\]")


def parse_type_spec(spec):
    """Parse a TYPE spec into (types, relation).

    'security'              -> (['security'], 'single')
    'security+test'         -> (['security','test'], 'sum')   # two separate problems
    'reliability|correctness' -> (['reliability','correctness'], 'max')  # one problem, two lenses
    An ambiguous multi-spec with no '+' defaults to 'max' (never double-counts).
    """
    spec = (spec or "").strip().lower()
    if not spec:
        return [], "single"
    if "+" in spec:
        parts = [p.strip() for p in spec.split("+") if p.strip()]
        return parts, ("sum" if len(parts) > 1 else "single")
    if "|" in spec:
        parts = [p.strip() for p in spec.split("|") if p.strip()]
        return parts, ("max" if len(parts) > 1 else "single")
    return [spec], "single"


def parse_findings(text):
    """Return a list of finding dicts {severity, type, status}.

    status: 'ok' | 'unknown_severity' | 'unknown_type' | 'unparseable'.
    A '- none' line yields nothing. A finding-like line that breaks format
    (wrong bullet, tag-in-prose, malformed brackets) becomes 'unparseable'
    rather than being dropped.
    """
    findings = []
    for line in text.splitlines():
        s = line.strip()
        if not s or re.match(r"^-\s*none\b", s, re.IGNORECASE):
            continue
        m = _STRICT.match(s)
        if m:
            sev = m.group(1).upper()
            typ_raw = (m.group(2) or "").strip().lower() or None
            desc = s[m.end():].strip()
            if sev not in KNOWN_SEVERITIES:
                findings.append({"severity": sev, "type": typ_raw, "status": "unknown_severity", "text": desc})
            elif sev == "SUGGESTION":
                types, rel = parse_type_spec(typ_raw)
                if types and all(t in DOCUMENTED_TYPES for t in types):
                    f = {"severity": sev, "status": "ok", "text": desc}
                    if len(types) == 1:
                        f["type"] = types[0]
                    else:  # multi-type: + = sum (disjoint), | = max (overlap)
                        f["type"], f["types"], f["relation"] = None, types, rel
                    findings.append(f)
                else:  # missing or some undocumented type -> re-classify once, else escalate
                    findings.append({"severity": sev, "type": typ_raw, "status": "unknown_type", "text": desc})
            else:  # CRITICAL / WARNING use the fixed severity weight regardless of type
                findings.append({"severity": sev, "type": typ_raw, "status": "ok", "text": desc})
            continue
        # Not strict. Treat as a malformed finding (never silently drop) when it is
        # either a finding-like bullet without a clean tag (e.g. "- Missing test",
        # "* [WARNING/x]", "1. ..."), or a severity-like tag buried in prose.
        is_bullet = bool(re.match(r"^([-*+]|\d+[.)])\s+\S", s))
        lead = re.search(r"\[\s*([A-Za-z]+)", s)
        sev_like = bool(lead) and (lead.group(1).upper() in KNOWN_SEVERITIES or lead.group(1).isupper())
        if is_bullet or sev_like:
            findings.append({"severity": None, "type": None, "status": "unparseable", "text": s})
    return findings


def reclassify(findings, classifier, cfg=DEFAULT_CONFIG):
    """Give every ``unknown_type`` finding ONE more precise classification pass.

    ``classifier(text)`` should return a documented TYPE or None. On success the
    finding is rewritten to ``ok`` with that type; if it still cannot be
    classified it becomes ``reclassify_failed`` (which escalates to a human).
    """
    out = []
    for f in findings:
        if f.get("status") != "unknown_type":
            out.append(f)
            continue
        types, rel = parse_type_spec(classifier(f.get("text", "")))
        if types and all(t in DOCUMENTED_TYPES for t in types):
            g = {**f, "status": "ok", "reclassified": True}
            g.pop("types", None)
            g.pop("relation", None)
            if len(types) == 1:
                g["type"] = types[0]
            else:
                g["type"], g["types"], g["relation"] = None, types, rel
            out.append(g)
        else:
            out.append({**f, "status": "reclassify_failed"})
    return out


def weight_of(finding, cfg=DEFAULT_CONFIG):
    st = finding.get("status", "ok")
    if st in ("unknown_severity", "unparseable", "reclassify_failed"):
        return cfg["escalate_min"]            # never 0 — force attention
    sev, typ = finding.get("severity"), finding.get("type")
    if sev in ("CRITICAL", "WARNING"):
        return cfg["weights"].get(sev, 0)
    if sev == "SUGGESTION":
        if st == "unknown_type":
            return cfg["unknown_type_weight"]
        types = finding.get("types")
        if types:  # multi-type: sum if disjoint (+), max if overlapping (|)
            ws = [cfg["weights"].get(f"SUGGESTION/{t}", cfg["unknown_type_weight"]) for t in types]
            return sum(ws) if finding.get("relation") == "sum" else max(ws)
        return cfg["weights"].get(f"SUGGESTION/{typ}", cfg["unknown_type_weight"])
    return 0


def needs_human(findings):
    """True if any finding could not be judged by the table (incl. an
    unknown_type that failed re-classification)."""
    return any(f.get("status") in ("unknown_severity", "unparseable", "reclassify_failed")
               for f in findings)


def score_review(findings, cfg=DEFAULT_CONFIG):
    return sum(weight_of(f, cfg) for f in findings)


def decide_stop(score, verdict, prev_consecutive_low, needs_human_flag=False, cfg=DEFAULT_CONFIG):
    """Return (stop, consecutive_low). A round is 'low' only when score <=
    stop_cutoff, verdict is APPROVE, and nothing needs a human."""
    low = (score <= cfg["stop_cutoff"]) and (verdict == "APPROVE") and not needs_human_flag
    consecutive = prev_consecutive_low + 1 if low else 0
    stop = consecutive >= cfg["stop_consecutive"]
    return stop, consecutive


def decide_convergence(score, verdict, escalated, prev_low, prev_no_esc, cfg=DEFAULT_CONFIG):
    """Dual stopping rule. Converge if EITHER:
      (a) score <= stop_cutoff and verdict APPROVE, for stop_consecutive rounds, OR
      (b) no escalate-level finding, for stop_no_escalate_consecutive rounds.

    `escalated` = this round raised a CRITICAL/WARNING/needs-human finding.
    Returns (stop, low_streak, no_escalate_streak, reason).
    """
    low = (score <= cfg["stop_cutoff"]) and (verdict == "APPROVE")
    low_streak = prev_low + 1 if low else 0
    no_esc_streak = prev_no_esc + 1 if not escalated else 0
    if low_streak >= cfg["stop_consecutive"]:
        return True, low_streak, no_esc_streak, "low_score"
    if no_esc_streak >= cfg.get("stop_no_escalate_consecutive", 3):
        return True, low_streak, no_esc_streak, "no_escalate"
    return False, low_streak, no_esc_streak, ""


def classify(findings, cfg=DEFAULT_CONFIG):
    """Split into (escalate_to_human, apply_automatically) by weight."""
    escalate, apply_ = [], []
    for f in findings:
        (escalate if weight_of(f, cfg) >= cfg["escalate_min"] else apply_).append(f)
    return escalate, apply_


def tag_str(finding):
    """Human-readable tag, e.g. 'SUGGESTION/test' or 'UNPARSEABLE'."""
    st = finding.get("status")
    if st == "unparseable":
        return "UNPARSEABLE"
    if st == "reclassify_failed":
        return "RECLASSIFY_FAILED"
    if st == "unknown_severity":
        return f"UNKNOWN_SEV:{finding.get('severity')}"
    sev = finding.get("severity")
    types = finding.get("types")
    if types:  # multi-type: + = disjoint/sum, | = overlap/max
        conn = "+" if finding.get("relation") == "sum" else "|"
        return f"{sev or '?'}/{conn.join(types)}"
    typ = finding.get("type")
    s = sev if sev else "?"
    if typ:
        s += f"/{typ}" + ("(unknown)" if st == "unknown_type" else "")
    return s
