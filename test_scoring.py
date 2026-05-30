#!/usr/bin/env python3
"""Unit tests for scoring.py — value index, fail-safe tag handling, stop rule.

Pure logic, no model calls. Run: python test_scoring.py  (exit 0 = all pass)
"""
import scoring as s

CFG = s.DEFAULT_CONFIG
fails = []


def check(name, got, want):
    if got != want:
        fails.append(f"{name}: got {got!r}, want {want!r}")


def statuses(text):
    return [(f.get("severity"), f.get("type"), f["status"]) for f in s.parse_findings(text)]


# ── parsing: well-formed ──────────────────────────────────────────────
check("ok basic", statuses("- [SUGGESTION/test] a\n- [WARNING/correctness] b"),
      [("SUGGESTION", "test", "ok"), ("WARNING", "correctness", "ok")])
check("none", statuses("FINDINGS:\n- none"), [])
check("no-type -> unknown_type", statuses("- [SUGGESTION] x"), [("SUGGESTION", None, "unknown_type")])
check("spaced/case", statuses("-  [ suggestion / Docs ] y"), [("SUGGESTION", "docs", "ok")])
check("plain prose ignored", statuses("SUMMARY: looks good overall"), [])

# ── parsing: fail-safe (Hermes Critical #1) ───────────────────────────
check("unknown severity", statuses("- [MAJOR/correctness] x"),
      [("MAJOR", "correctness", "unknown_severity")])
check("unknown type", statuses("- [SUGGESTION/perf] x"),
      [("SUGGESTION", "perf", "unknown_type")])
check("numbered list", [f["status"] for f in s.parse_findings("1. [CRITICAL/correctness] x")],
      ["unparseable"])
check("star bullet", [f["status"] for f in s.parse_findings("* [WARNING/correctness] x")],
      ["unparseable"])
check("tag in prose", [f["status"] for f in s.parse_findings("we think [CRITICAL/security] applies")],
      ["unparseable"])
check("malformed brackets", [f["status"] for f in s.parse_findings("- [CRITICAL/correctness/extra] x")],
      ["unparseable"])
check("untagged bullet not dropped", [f["status"] for f in s.parse_findings("- Missing regression test")],
      ["unparseable"])
check("untagged bullet needs human", s.needs_human(s.parse_findings("- Missing regression test")), True)

# ── value index ───────────────────────────────────────────────────────
def w(sev, typ=None, st="ok"):
    return s.weight_of({"severity": sev, "type": typ, "status": st})

check("CRITICAL", w("CRITICAL"), 100)
check("WARNING", w("WARNING", "correctness"), 40)
check("security", w("SUGGESTION", "security"), 25)
check("compatibility", w("SUGGESTION", "compatibility"), 20)
check("reliability", w("SUGGESTION", "reliability"), 15)
check("test", w("SUGGESTION", "test"), 10)
check("style", w("SUGGESTION", "style"), 1)
check("no-type -> conservative", w("SUGGESTION", None, "unknown_type"), CFG["unknown_type_weight"])
check("unknown type -> 15", w("SUGGESTION", "perf", "unknown_type"), CFG["unknown_type_weight"])
check("unknown severity -> escalate_min", w("MAJOR", "x", "unknown_severity"), CFG["escalate_min"])
check("unparseable -> escalate_min", w(None, None, "unparseable"), CFG["escalate_min"])

# ── needs_human ───────────────────────────────────────────────────────
check("nh false (ok only)", s.needs_human(s.parse_findings("- [SUGGESTION/test] a")), False)
check("nh true (unknown sev)", s.needs_human(s.parse_findings("- [BLOCKER/x] a")), True)
check("nh true (unparseable)", s.needs_human(s.parse_findings("* [WARNING/x] a")), True)

# ── reclassify: unknown_type gets ONE more pass, else escalates ─────────
def _stub(answer):
    return lambda _text: answer

uf = s.parse_findings("- [SUGGESTION/perf] slow nested loop")  # -> unknown_type
check("reclassify success -> ok/type",
      [(f["status"], f["type"]) for f in s.reclassify(uf, _stub("reliability"))],
      [("ok", "reliability")])
check("reclassify unclassifiable -> failed",
      [f["status"] for f in s.reclassify(uf, _stub("UNCLASSIFIABLE"))], ["reclassify_failed"])
check("reclassify still-unknown -> failed",
      [f["status"] for f in s.reclassify(uf, _stub("perf"))], ["reclassify_failed"])
check("reclassify leaves ok findings",
      [f["status"] for f in s.reclassify(s.parse_findings("- [SUGGESTION/test] a"), _stub("docs"))], ["ok"])
check("reclassify_failed weight escalates", w(None, None, "reclassify_failed"), CFG["escalate_min"])
check("reclassify_failed needs_human", s.needs_human([{"status": "reclassify_failed"}]), True)

# ── stop rule (incl. needs_human gate) ────────────────────────────────
check("low #1", s.decide_stop(4, "APPROVE", 0, False), (False, 1))
check("low #2 -> stop", s.decide_stop(4, "APPROVE", 1, False), (True, 2))
check("valuable resets", s.decide_stop(10, "APPROVE", 1, False), (False, 0))
check("request_changes never low", s.decide_stop(4, "REQUEST_CHANGES", 1, False), (False, 0))
check("needs_human blocks stop", s.decide_stop(0, "APPROVE", 1, True), (False, 0))

# ── dual convergence: 2x low-score OR 3x no-escalate (stop on either) ──
# (a) low-score path
check("dual low #1", s.decide_convergence(4, "APPROVE", False, 0, 0), (False, 1, 1, ""))
check("dual low #2 -> stop", s.decide_convergence(4, "APPROVE", False, 1, 1), (True, 2, 2, "low_score"))
# (b) no-escalate path: findings present but none escalating (e.g. SUGGESTION/test=10, score>cutoff)
check("no-esc #2 not yet", s.decide_convergence(10, "APPROVE", False, 0, 1), (False, 0, 2, ""))
check("no-esc #3 -> stop", s.decide_convergence(10, "APPROVE", False, 0, 2), (True, 0, 3, "no_escalate"))
# escalate resets the no-escalate streak (and low streak)
check("escalate resets", s.decide_convergence(40, "REQUEST_CHANGES", True, 1, 2), (False, 0, 0, ""))

# ── classify ──────────────────────────────────────────────────────────
esc, app = s.classify(s.parse_findings(
    "- [WARNING/correctness] a\n- [SUGGESTION/test] b\n- [MAJOR/x] c\n- [SUGGESTION/style] d"))
check("escalate count", len(esc), 2)   # WARNING + unknown-severity
check("apply count", len(app), 2)      # test + style

if fails:
    print("FAIL:")
    for f in fails:
        print("  -", f)
    raise SystemExit(1)
print("ALL PASS - scoring (value index + fail-safe tags + stop rule) verified.")
