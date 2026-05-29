#!/usr/bin/env python3
"""Unit tests for the value-scored stopping rule (scoring.py).

Pure logic — no model/CLI calls. Verifies the [SEVERITY/TYPE] parsing
convention, the value index (incl. fallbacks), and the cutoff + consecutive-low
stop rule. Run: python test_scoring.py   (exit 0 = all pass)
"""
import scoring as s

CFG = s.DEFAULT_CONFIG
fails = []


def check(name, got, want):
    if got != want:
        fails.append(f"{name}: got {got!r}, want {want!r}")


# parsing convention
check("basic", s.parse_findings("- [SUGGESTION/test] a\n- [WARNING/correctness] b"),
      [("SUGGESTION", "test"), ("WARNING", "correctness")])
check("none", s.parse_findings("FINDINGS:\n- none"), [])
check("no-type", s.parse_findings("- [SUGGESTION] x"), [("SUGGESTION", None)])
check("spaced/case", s.parse_findings("-  [ suggestion / Docs ] y"), [("SUGGESTION", "docs")])
check("ignores prose", s.parse_findings("SUMMARY: not a finding"), [])

# value index
check("critical", s.weight_of("CRITICAL", None), 100)
check("warning", s.weight_of("WARNING", "correctness"), 40)
check("sugg/test", s.weight_of("SUGGESTION", "test"), 10)
check("sugg/style", s.weight_of("SUGGESTION", "style"), 1)
check("sugg/unknown", s.weight_of("SUGGESTION", "bogus"), CFG["default_suggestion_weight"])
check("sugg/none", s.weight_of("SUGGESTION", None), CFG["default_suggestion_weight"])
check("sum", s.score_review([("SUGGESTION", "test"), ("SUGGESTION", "style"), ("WARNING", None)]), 51)

# stop rule
check("low #1", s.decide_stop(4, "APPROVE", 0), (False, 1))
check("low #2 -> stop", s.decide_stop(4, "APPROVE", 1), (True, 2))
check("valuable resets", s.decide_stop(10, "APPROVE", 1), (False, 0))
check("request_changes never low", s.decide_stop(4, "REQUEST_CHANGES", 1), (False, 0))
check("warning never low", s.decide_stop(s.score_review([("WARNING", None)]), "APPROVE", 1), (False, 0))

# apply/escalate gate
esc, app = s.classify([("WARNING", "correctness"), ("SUGGESTION", "test"),
                       ("CRITICAL", None), ("SUGGESTION", "style")])
check("escalate", sorted(esc), sorted([("WARNING", "correctness"), ("CRITICAL", None)]))
check("apply", sorted(app), sorted([("SUGGESTION", "test"), ("SUGGESTION", "style")]))

# convergence: two trivial-only rounds in a row -> stop
consec, stops = 0, []
for f in [("SUGGESTION", "consistency"), ("SUGGESTION", "style")]:
    st, consec = s.decide_stop(s.score_review([f]), "APPROVE", consec)
    stops.append(st)
check("trivial converges", stops, [False, True])

if fails:
    print("FAIL:")
    for f in fails:
        print("  -", f)
    raise SystemExit(1)
print("ALL PASS - scoring/stop rule verified.")
