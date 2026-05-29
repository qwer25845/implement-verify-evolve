#!/usr/bin/env python3
"""Config-driven LLM review round with a value-scored stopping rule.

``run_round(cfg, work_dir)`` performs ONE round and returns a result dict; it is
imported by the IVE orchestrator (orchestrate.py) and also runnable standalone:

    python review_loop.py --config config.json

Everything machine-specific lives in the JSON config (see config.example.json);
this file contains no hard-coded paths.
"""
import os
import re
import sys
import json
import argparse
import subprocess
import datetime

import scoring

try:  # robust against non-UTF-8 consoles (e.g. Windows cp949)
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def _run(cmd, cwd=None, env=None, timeout=600):
    return subprocess.run(cmd, cwd=cwd, env=env, timeout=timeout,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def merged_cfg(cfg):
    """Fill scoring defaults so a partial config still works."""
    out = dict(scoring.DEFAULT_CONFIG)
    for k in ("weights", "default_suggestion_weight", "stop_cutoff",
              "stop_consecutive", "escalate_min"):
        if k in cfg:
            out[k] = cfg[k]
    return out


def build_prompt(cfg, head, patch_path):
    targets = list(cfg.get("review_targets", []))
    if patch_path:
        targets.append({"label": f"Patch for commit {head}", "path": patch_path})
    lines = [cfg.get("pr_context", "Review the following change."), "",
             "Read ALL of these files before reviewing:"]
    for i, t in enumerate(targets, 1):
        lines.append(f"{i}. {t.get('label', 'file')}: {t['path']}")
    lines += [
        "",
        "Review for: correctness/logic, design fit, test adequacy, docs accuracy, "
        "backward compatibility. Be strict but fair; do not invent problems.",
        "",
        "Respond with EXACTLY this format and nothing after SUMMARY:",
        "VERDICT: APPROVE or REQUEST_CHANGES",
        f"HEAD: {head}",
        "FINDINGS:",
        "- [SEVERITY/TYPE] one concise finding",
        "(one bullet per finding; if there are none write exactly: - none)",
        "SUMMARY: two or three sentences.",
        "",
        scoring.FINDINGS_LEGEND,
    ]
    return "\n".join(lines)


def run_round(cfg, work_dir):
    """Run one review round. Returns a result dict; also writes log + state."""
    score_cfg = merged_cfg(cfg)
    log_file = os.path.join(work_dir, cfg.get("log_file", "review-log.md"))
    state_file = os.path.join(work_dir, cfg.get("state_file", ".review_state.json"))
    prompt_file = os.path.join(work_dir, cfg.get("prompt_file", ".review_prompt.txt"))

    head, patch_path = "n/a", None
    repo = cfg.get("repo_dir")
    if repo:
        head = _run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()[:9] or "n/a"
        if cfg.get("include_patch"):
            out_dir = cfg.get("patch_out_dir", work_dir)
            for f in os.listdir(out_dir):
                if f.endswith(".patch") and f.startswith("0001-"):
                    os.remove(os.path.join(out_dir, f))
            _run(["git", "format-patch", "-1", "HEAD", "-o", out_dir], cwd=repo)
            patch_path = next((os.path.join(out_dir, f) for f in os.listdir(out_dir)
                               if f.endswith(".patch") and f.startswith("0001-")), None)

    with open(prompt_file, "w", encoding="utf-8") as fh:
        fh.write(build_prompt(cfg, head, patch_path))

    instruction = (f"Read the file {prompt_file.replace(os.sep, '/')} and follow "
                   "the instructions inside it exactly.")
    cmd = [a.replace("{prompt_file}", prompt_file.replace(os.sep, "/"))
            .replace("{prompt_instruction}", instruction) for a in cfg["reviewer_cmd"]]
    env = dict(os.environ)
    env.update(cfg.get("reviewer_env", {}))
    z = _run(cmd, cwd=cfg.get("reviewer_cwd"), env=env, timeout=cfg.get("timeout", 600))
    review = (z.stdout or "").strip()

    vm = re.search(r"VERDICT:\s*(APPROVE|REQUEST_CHANGES)", review)
    verdict = vm.group(1) if vm else "?"
    findings = scoring.parse_findings(review)
    score = scoring.score_review(findings, score_cfg)
    escalate, apply_ = scoring.classify(findings, score_cfg)

    state = _load_json(state_file, {"consecutive_low": 0, "rounds": []})
    stop, consecutive = scoring.decide_stop(score, verdict, state.get("consecutive_low", 0), score_cfg)
    state["consecutive_low"] = consecutive
    state.setdefault("rounds", []).append({
        "head": head, "verdict": verdict, "score": score,
        "findings": [f"{s}/{t or '-'}" for s, t in findings],
        "consecutive_low": consecutive, "stop": stop})

    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(log_file, "a", encoding="utf-8") as fh:
        fh.write(f"\n\n## Review round - {ts} - HEAD {head} - "
                 f"score={score} consec_low={consecutive} {'STOP' if stop else 'CONTINUE'}\n\n")
        fh.write(review if review else "(empty review - see stderr)\n")
        fh.write("\n")
    with open(state_file, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)

    return {
        "head": head, "verdict": verdict, "score": score, "review": review,
        "findings": [f"{s}/{t or '-'}" for s, t in findings],
        "escalate": [f"{s}/{t or '-'}" for s, t in escalate],
        "apply": [f"{s}/{t or '-'}" for s, t in apply_],
        "stop": stop, "consecutive_low": consecutive,
        "ok": bool(review), "stderr": (z.stderr or "")[-1500:],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=os.getenv("REVIEW_CONFIG", "config.json"))
    args = ap.parse_args()
    cfg = _load_json(args.config, None)
    if cfg is None:
        print(f"ERROR: config not found/invalid: {args.config}", file=sys.stderr)
        return 2
    work_dir = os.path.dirname(os.path.abspath(args.config)) or "."
    r = run_round(cfg, work_dir)
    if not r["ok"]:
        print("WARN: empty review (reviewer/auth/timeout?)")
        sys.stderr.write(r["stderr"])
        return 1
    print("=== RESULT ===")
    print("HEAD:", r["head"])
    print("VERDICT:", r["verdict"])
    print("FINDINGS:", r["findings"] or "none")
    print("SCORE:", r["score"])
    print("CONSECUTIVE_LOW:", r["consecutive_low"])
    print("DECISION:", "STOP - converged" if r["stop"] else "CONTINUE")
    print("ESCALATE_TO_HUMAN:", r["escalate"] or "none")
    print("APPLY_AUTOMATICALLY:", r["apply"] or "none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
