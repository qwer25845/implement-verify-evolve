#!/usr/bin/env python3
"""Calibration harness: do Claude (gold) and the reviewer LLM tag findings the
same way under SCORING_RUBRIC.md?

One invocation = one calibration loop:
  - pick a rotated subset of the example bank (varied per loop),
  - send the rubric + the example descriptions to the reviewer (reviewer_cmd in
    config.json) and ask for one `[SEVERITY/TYPE]` tag per example,
  - compare each tag to the gold tag (the rubric-derived Claude judgement),
  - require ALL to match; track consecutive fully-agreeing loops in
    .calibration_state.json (resets on any mismatch).

Gold tags are authored to be unambiguous under the rubric; a mismatch means the
rubric (or that example) needs tightening - refine, then the counter restarts.

Run: python scoring_calibration.py --loop <n> [--config config.json] [--n 10]
"""
import os
import re
import sys
import json
import argparse
import subprocess

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))

# Example bank: each is a finding description + its gold [SEVERITY/TYPE] under
# SCORING_RUBRIC.md. Phrased so confidence/impact/reproducibility and domain are
# unambiguous. (id, description, gold)
EXAMPLES = [
    (1, "A committed config file contains a real, live AWS secret access key.", "CRITICAL/security"),
    (2, "SQL is built by concatenating a request parameter directly; the reviewer shows an injecting payload.", "CRITICAL/security"),
    (3, "User passwords are written to the application log in plaintext on every login.", "CRITICAL/security"),
    (4, "The current test suite fails on this diff.", "CRITICAL/test"),
    (5, "Renames the public env var DB_URL to DATABASE_URL with no backward-compat alias; existing deployments break.", "WARNING/compatibility"),
    (6, "Changes the on-disk cache file format with no migration; previously written cache files fail to load.", "WARNING/compatibility"),
    (7, "An async function is called without await, so its work silently never runs; the reviewer points at the exact line.", "WARNING/reliability"),
    (8, "An outbound HTTP call has no timeout; the reviewer explains how a slow peer hangs the worker indefinitely.", "WARNING/reliability"),
    (9, "An off-by-one makes the loop skip the last element; the reviewer demonstrates it with a concrete input.", "WARNING/correctness"),
    (10, "A division by a user-supplied count could throw if it is zero, but no failing case is shown and the value may never be zero.", "SUGGESTION/correctness"),
    (11, "A newly added public function has no unit test.", "SUGGESTION/test"),
    (12, "A public function documented to return sorted output has no test covering the ordering.", "SUGGESTION/test"),
    (13, "The README quick-start uses a CLI flag --foo that the argument parser no longer accepts.", "SUGGESTION/docs"),
    (14, "A docstring's example output is stale and no longer matches the current formatting.", "SUGGESTION/docs"),
    (15, "The same 20-line retry block is copy-pasted across four modules.", "SUGGESTION/maintainability"),
    (16, "An internal helper has an unused parameter that should be removed; no behavior impact.", "SUGGESTION/maintainability"),
    (17, "A comment says 'returns a list' but the function returns a tuple; behavior is correct, only the comment is wrong.", "SUGGESTION/consistency"),
    (18, "The PR description claims 14 tests but the file defines 13.", "SUGGESTION/consistency"),
    (19, "Suggests renaming a local variable for readability; no behavior change.", "SUGGESTION/style"),
    (20, "Prefer f-strings over percent-formatting in one module.", "SUGGESTION/style"),
]


def _run(cmd, cwd=None, env=None, timeout=420):
    return subprocess.run(cmd, cwd=cwd, env=env, timeout=timeout,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def normalize(tag):
    return re.sub(r"\s+", "", (tag or "")).upper().strip("[]")


def pick(loop, n):
    """Deterministic rotated subset of n examples for this loop (varied, no RNG)."""
    k = len(EXAMPLES)
    start = (loop * 7) % k          # stride 7 is coprime-ish with 20 -> rotates well
    return [EXAMPLES[(start + i * 3) % k] for i in range(min(n, k))]


def build_prompt(rubric, items):
    lines = [
        "You are calibrating a code-review scoring rubric. Using ONLY the rubric "
        "below, classify each numbered finding with exactly one tag [SEVERITY/TYPE].",
        "Output ONLY lines of the form `<id>: SEVERITY/TYPE` (e.g. `3: WARNING/reliability`). "
        "No prose, no extra text.",
        "",
        "=== RUBRIC ===", rubric, "=== END RUBRIC ===", "",
        "Findings to classify:",
    ]
    for i, desc, _ in items:
        lines.append(f"{i}: {desc}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, required=True)
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--n", type=int, default=10)
    args = ap.parse_args()

    cfg = json.load(open(args.config, encoding="utf-8"))
    rubric = open(os.path.join(HERE, "SCORING_RUBRIC.md"), encoding="utf-8").read()
    items = pick(args.loop, args.n)
    gold = {i: normalize(g) for i, _, g in items}

    prompt_file = os.path.join(HERE, ".calibration_prompt.txt")
    with open(prompt_file, "w", encoding="utf-8") as fh:
        fh.write(build_prompt(rubric, items))
    instruction = (f"Read the file {prompt_file.replace(os.sep, '/')} and follow "
                   "the instructions inside it exactly.")
    cmd = [a.replace("{prompt_instruction}", instruction).replace("{prompt_file}", prompt_file.replace(os.sep, "/"))
           for a in cfg["reviewer_cmd"]]
    env = dict(os.environ)
    env.update(cfg.get("reviewer_env", {}))
    z = _run(cmd, cwd=cfg.get("reviewer_cwd"), env=env, timeout=cfg.get("timeout", 420))
    out = (z.stdout or "").strip()

    reviewer = {}
    for m in re.finditer(r"(?m)^\s*(\d+)\s*[:.\)]\s*\[?\s*([A-Za-z]+\s*/\s*[A-Za-z]+|[A-Za-z]+)\s*\]?", out):
        reviewer[int(m.group(1))] = normalize(m.group(2))

    rows, disagreements = [], []
    for i, desc, g in items:
        h = reviewer.get(i, "(missing)")
        ok = (h == gold[i])
        rows.append((i, gold[i], h, ok))
        if not ok:
            disagreements.append((i, desc, gold[i], h))

    all_agree = bool(rows) and all(r[3] for r in rows) and len(reviewer) >= len(items)

    state_file = os.path.join(HERE, ".calibration_state.json")
    try:
        state = json.load(open(state_file, encoding="utf-8"))
    except (OSError, ValueError):
        state = {"consecutive_agree": 0, "history": []}
    state["consecutive_agree"] = state.get("consecutive_agree", 0) + 1 if all_agree else 0
    state["history"].append({"loop": args.loop, "all_agree": all_agree,
                             "consecutive_agree": state["consecutive_agree"],
                             "disagreements": [[i, g, h] for i, _, g, h in disagreements]})
    json.dump(state, open(state_file, "w", encoding="utf-8"), indent=2)

    print(f"=== CALIBRATION loop {args.loop} ===")
    if not out:
        print("WARN: empty reviewer output. stderr tail:\n", (z.stderr or "")[-800:])
    for i, g, h, ok in rows:
        print(f"  #{i:<2} gold={g:<28} reviewer={h:<28} {'OK' if ok else 'MISMATCH'}")
    print("ALL_AGREE:", all_agree)
    print("CONSECUTIVE_AGREE:", state["consecutive_agree"], "/ 10")
    if disagreements:
        print("DISAGREEMENTS:")
        for i, desc, g, h in disagreements:
            print(f"  #{i}: {desc}\n      gold={g} reviewer={h}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
