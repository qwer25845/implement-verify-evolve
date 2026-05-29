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

if fails:
    print("FAIL:")
    for f in fails:
        print("  -", f)
    raise SystemExit(1)
print("ALL PASS - IVE loop control verified.")
