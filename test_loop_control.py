#!/usr/bin/env python3
"""Unit tests for the IVE loop control logic (orchestrate.decide_next_action).

Pure branch logic - no subprocess/model calls. Run: python test_loop_control.py
"""
import orchestrate as o

fails = []


def check(name, got, want):
    if got != want:
        fails.append(f"{name}: got {got!r}, want {want!r}")


APPROVE_DONE = {"escalate": [], "stop": True, "apply": []}
APPROVE_APPLY = {"escalate": [], "stop": False, "apply": ["SUGGESTION/test"]}
APPROVE_CONTINUE = {"escalate": [], "stop": False, "apply": []}
ESCALATE = {"escalate": ["WARNING/correctness"], "stop": False, "apply": []}

check("max cap (==)", o.decide_next_action(True, APPROVE_DONE, 10, 10), "max_iterations")
check("max cap (>)", o.decide_next_action(False, None, 12, 10), "max_iterations")
check("tests fail", o.decide_next_action(False, None, 1, 10), "fix_tests")
check("tests pass, no review", o.decide_next_action(True, None, 1, 10), "review")
check("escalate", o.decide_next_action(True, ESCALATE, 2, 10), "human_escalate")
check("done", o.decide_next_action(True, APPROVE_DONE, 2, 10), "done")
check("apply", o.decide_next_action(True, APPROVE_APPLY, 2, 10), "apply_suggestions")
check("continue", o.decide_next_action(True, APPROVE_CONTINUE, 2, 10), "continue_review")

# priority: a high-severity finding must escalate even if stop/apply also set
mixed = {"escalate": ["CRITICAL/-"], "stop": True, "apply": ["SUGGESTION/test"]}
check("escalate beats done", o.decide_next_action(True, mixed, 2, 10), "human_escalate")

# failing tests take priority over the (stale) review of an earlier state
check("fix_tests beats review", o.decide_next_action(False, APPROVE_DONE, 2, 10), "fix_tests")

# parse_verdict: only a line-start VERDICT counts; a stray prose mention -> '?'
import review_loop as rl
import scoring as sc
check("verdict line", rl.parse_verdict("VERDICT: APPROVE\nHEAD: x"), "APPROVE")
check("verdict lowercase", rl.parse_verdict("verdict: request_changes"), "REQUEST_CHANGES")
check("verdict in prose ignored", rl.parse_verdict("we lean to VERDICT: APPROVE here"), "?")
check("no verdict", rl.parse_verdict("nothing here"), "?")

# verdict_anomaly: verdict <-> findings contradictions go to a human
check("missing verdict", rl.verdict_anomaly("?", []), True)
check("request_changes + no findings", rl.verdict_anomaly("REQUEST_CHANGES", []), True)
check("approve + warning", rl.verdict_anomaly("APPROVE", sc.parse_findings("- [WARNING/x] a")), True)
check("approve clean ok", rl.verdict_anomaly("APPROVE", sc.parse_findings("- [SUGGESTION/test] a")), False)
check("request_changes + findings ok", rl.verdict_anomaly("REQUEST_CHANGES", sc.parse_findings("- [WARNING/x] a")), False)

# merged_cfg must DEEP-merge weights (partial override keeps CRITICAL/WARNING)
m = rl.merged_cfg({"weights": {"SUGGESTION/style": 2}})
check("partial weights keeps CRITICAL", m["weights"]["CRITICAL"], 100)
check("partial weights keeps WARNING", m["weights"]["WARNING"], 40)
check("partial weights applies override", m["weights"]["SUGGESTION/style"], 2)

if fails:
    print("FAIL:")
    for f in fails:
        print("  -", f)
    raise SystemExit(1)
print("ALL PASS - IVE loop control verified.")
