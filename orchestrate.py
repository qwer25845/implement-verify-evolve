#!/usr/bin/env python3
"""Implement / Verify / Evolve (IVE): a turnkey autonomous dev loop.

Given a task, it drives:

  Implement  - an implementer LLM (LLM A) edits the target repo to satisfy the
               task (and, on later iterations, to fix tests or apply review
               suggestions).
  Verify     - run the project's test command, then run one scored review round
               (LLM B) via review_loop.run_round.
  Evolve     - feed test failures / applicable review suggestions back to the
               implementer and iterate, until the review loop converges (STOP) or
               a human-intervention finding (CRITICAL/WARNING) is raised.

Stops on: convergence (review STOP), human escalation (CRITICAL/WARNING finding),
or max_iterations. The implementer command is agentic and EDITS FILES IN PLACE -
run against a clean git tree (ideally a throwaway worktree); see README safety
notes. Everything machine-specific lives in the JSON config.

    python orchestrate.py --config config.json --task "Implement X"
"""
import os
import sys
import json
import argparse
import subprocess

import review_loop

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# ── Pure control logic (no I/O - unit-tested in test_loop_control.py) ──────
def decide_next_action(tests_passed, review, iteration, max_iterations):
    """Decide the next step of the IVE loop.

    Args:
        tests_passed: bool result of the most recent test run.
        review: result dict from review_loop.run_round, or None if no review has
            been run since the last code change.
        iteration / max_iterations: hard safety cap.

    Returns one of:
        'max_iterations'    - cap hit; give up and report.
        'fix_tests'         - tests failing; implementer must fix.
        'review'            - tests pass but no fresh review yet; run one.
        'human_escalate'    - review raised a CRITICAL/WARNING finding.
        'done'              - review loop converged (STOP).
        'apply_suggestions' - low-severity suggestions for the implementer.
        'continue_review'   - tests pass, nothing to apply, not yet converged;
                              re-review to advance the consecutive-low counter.
    """
    if iteration >= max_iterations:
        return "max_iterations"
    if not tests_passed:
        return "fix_tests"
    if review is None:
        return "review"
    if review.get("escalate"):
        return "human_escalate"
    if review.get("stop"):
        return "done"
    if review.get("apply"):
        return "apply_suggestions"
    return "continue_review"


# ── I/O harness ────────────────────────────────────────────────────────────
def _run(cmd, cwd=None, env=None, timeout=1800):
    return subprocess.run(cmd, cwd=cwd, env=env, timeout=timeout,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def run_implementer(cfg, prompt):
    """Invoke the agentic implementer LLM to edit the repo. {prompt} / {prompt_file}
    are substituted in implementer_cmd."""
    work_dir = cfg["_work_dir"]
    pf = os.path.join(work_dir, cfg.get("implementer_prompt_file", ".implementer_prompt.txt"))
    with open(pf, "w", encoding="utf-8") as fh:
        fh.write(prompt)
    cmd = [a.replace("{prompt_file}", pf.replace(os.sep, "/")).replace("{prompt}", prompt)
           for a in cfg["implementer_cmd"]]
    env = dict(os.environ)
    env.update(cfg.get("implementer_env", {}))
    return _run(cmd, cwd=cfg.get("repo_dir"), env=env, timeout=cfg.get("implementer_timeout", 1800))


def run_tests(cfg):
    if not cfg.get("test_cmd"):
        return True, "(no test_cmd configured; skipping)"
    z = _run(cfg["test_cmd"], cwd=cfg.get("repo_dir"), timeout=cfg.get("test_timeout", 1800))
    out = ((z.stdout or "") + "\n" + (z.stderr or "")).strip()
    return z.returncode == 0, out[-4000:]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=os.getenv("REVIEW_CONFIG", "config.json"))
    ap.add_argument("--task", default=None, help="task text (overrides config.task / task_file)")
    args = ap.parse_args()

    cfg = review_loop._load_json(args.config, None)
    if cfg is None:
        print(f"ERROR: config not found/invalid: {args.config}", file=sys.stderr)
        return 2
    work_dir = os.path.dirname(os.path.abspath(args.config)) or "."
    cfg["_work_dir"] = work_dir

    task = args.task or cfg.get("task")
    if not task and cfg.get("task_file"):
        task = open(cfg["task_file"], encoding="utf-8").read()
    if not task:
        print("ERROR: no task (use --task, config.task, or config.task_file)", file=sys.stderr)
        return 2

    max_it = cfg.get("max_iterations", 10)
    base = (f"TASK:\n{task}\n\nYou are editing the repository at {cfg.get('repo_dir')}. "
            "Make the minimal correct changes and keep the project's tests green.")
    feedback = ""          # extra context appended for the implementer
    needs_impl = True      # first iteration always implements
    review = None
    tests_passed = False
    it = 0

    while True:
        it += 1
        action_cap = decide_next_action(tests_passed, review, it - 1, max_it)
        if action_cap == "max_iterations":
            print(f"STOP: max_iterations ({max_it}) reached without convergence.")
            return 3

        if needs_impl:
            print(f"\n[iter {it}] IMPLEMENT")
            zi = run_implementer(cfg, base + ("\n\n" + feedback if feedback else ""))
            if zi.returncode != 0:
                print("  implementer exited non-zero:\n", (zi.stderr or "")[-800:])
            print(f"[iter {it}] VERIFY: tests")
            tests_passed, test_out = run_tests(cfg)
            print("  tests:", "PASS" if tests_passed else "FAIL")
            if not tests_passed:
                feedback = f"The tests are failing. Fix the code. Test output:\n{test_out}"
                review = None
                continue

        # tests pass -> review
        print(f"[iter {it}] VERIFY: review")
        review = review_loop.run_round(cfg, work_dir)
        if not review["ok"]:
            print("  review empty (reviewer error); aborting.\n", review["stderr"])
            return 1
        print(f"  verdict={review['verdict']} score={review['score']} "
              f"consec_low={review['consecutive_low']} findings={review['findings'] or 'none'}")

        action = decide_next_action(tests_passed, review, it, max_it)
        if action == "done":
            print(f"\nDONE: review loop converged at HEAD {review['head']} "
                  f"(consec_low={review['consecutive_low']}). Tests green.")
            return 0
        if action == "human_escalate":
            print("\nHUMAN ESCALATION: reviewer raised a high-severity finding "
                  f"({review['escalate']}). Stopping for human review.\n\n{review['review']}")
            return 4
        if action == "max_iterations":
            print(f"STOP: max_iterations ({max_it}) reached without convergence.")
            return 3
        if action == "apply_suggestions":
            print(f"[iter {it}] EVOLVE: apply suggestions {review['apply']}")
            if review.get("auto_fixed"):
                print(f"  (self-repairing high-severity findings: {review['auto_fixed']})")
            feedback = ("Apply these reviewer suggestions, then keep tests green:\n"
                        + review["review"])
            needs_impl = True
            continue
        # continue_review: tests pass, nothing to apply, not yet converged -> re-review only
        print(f"[iter {it}] EVOLVE: re-review to confirm convergence")
        needs_impl = False
        continue


if __name__ == "__main__":
    sys.exit(main())
