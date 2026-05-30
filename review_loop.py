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


def _run(cmd, cwd=None, env=None, timeout=600, input=None):
    return subprocess.run(cmd, cwd=cwd, env=env, timeout=timeout, input=input,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def merged_cfg(cfg):
    """Fill scoring defaults so a partial config still works.

    ``weights`` is DEEP-merged (a partial override must not drop the CRITICAL /
    WARNING defaults to 0 and break escalation)."""
    out = dict(scoring.DEFAULT_CONFIG)
    out["weights"] = dict(scoring.DEFAULT_CONFIG["weights"])
    if isinstance(cfg.get("weights"), dict):
        out["weights"].update(cfg["weights"])
    for k in ("unknown_type_weight", "stop_cutoff", "stop_consecutive",
              "stop_no_escalate_consecutive", "escalate_min"):
        if k in cfg:
            out[k] = cfg[k]
    return out


def auto_fix_enabled(cfg):
    """Opt-out with a safety interlock: auto-fix defaults ON only when an
    ``author_cmd`` is configured, because the E-gate (two-LLM agreement) is a real
    two-sided check only with a separate author LLM; without one it would degrade to
    the reviewer's self-report. An explicit ``cfg["auto_fix"]`` (true/false) always
    wins. Returns a bool."""
    af = cfg.get("auto_fix")
    return bool(cfg.get("author_cmd")) if af is None else bool(af)


def _reclassify_call(text, cfg, work_dir):
    """Ask the reviewer to map ONE finding to a single documented TYPE (a more
    precise second pass). Returns a lowercase type word, or None if it cannot."""
    types = ", ".join(scoring.DOCUMENTED_TYPES)
    prompt = (
        f"Classify this single code-review finding using ONLY these types: {types}.\n"
        "Reply with ONLY one of:\n"
        "  TYPE          (one problem, one category)\n"
        "  TYPE1+TYPE2   (TWO separate problems -> both count)\n"
        "  TYPE1|TYPE2   (ONE problem fitting two categories -> the larger counts)\n"
        "  UNCLASSIFIABLE\n"
        "No prose.\n\nFinding: " + (text or ""))
    pf = os.path.abspath(os.path.join(work_dir, ".reclassify_prompt.txt"))
    with open(pf, "w", encoding="utf-8") as fh:
        fh.write(prompt)
    instruction = (f"Read the file {pf.replace(os.sep, '/')} and follow the "
                   "instruction inside it exactly.")
    cmd = [a.replace("{prompt_file}", pf.replace(os.sep, "/")).replace("{prompt_instruction}", instruction)
           for a in cfg["reviewer_cmd"]]
    env = dict(os.environ)
    env.update(cfg.get("reviewer_env", {}))
    z = _run(cmd, cwd=cfg.get("reviewer_cwd"), env=env, timeout=cfg.get("timeout", 420))
    return _parse_reclassify_reply(z.stdout)


def _parse_reclassify_reply(out):
    """Parse a reclassification reply into a type spec ('a' / 'a+b' / 'a|b') or
    None (escalate). Robust: documented types are collected by first appearance
    (reviewers don't always obey "no prose"), up to two joined with the connector
    they used.

    Fail-safe: an explicit UNCLASSIFIABLE escalates (returns None) even if a stray
    type word also appears (e.g. "UNCLASSIFIABLE, maybe reliability"), so an
    ambiguous/hedged reply can never bypass the reclassify-failed human escalation."""
    out = (out or "").lower()
    if re.search(r"\bunclassifiable\b", out):
        return None
    hits = sorted((m.start(), t) for t in scoring.DOCUMENTED_TYPES
                  for m in [re.search(r"\b" + re.escape(t) + r"\b", out)] if m)
    found = [t for _, t in hits][:2]
    if not found:
        return None
    if len(found) == 1:
        return found[0]
    return ("+" if "+" in out else "|").join(found)


def _run_author(prompt, cfg, work_dir, prompt_name=".author_prompt.txt"):
    """Deliver ``prompt`` to the author LLM (LLM A, via author_cmd) and return its
    raw stdout. Delivery is robust by default: if author_cmd contains NO
    ``{prompt}`` / ``{prompt_file}`` / ``{prompt_instruction}`` placeholder, the
    prompt is piped on **stdin** (e.g. ``["claude", "-p"]``), which avoids the
    arg-length/escaping flakiness of passing long text as a single CLI argument.
    Otherwise the placeholder is substituted into argv."""
    placeholders = ("{prompt}", "{prompt_file}", "{prompt_instruction}")
    use_stdin = not any(ph in a for a in cfg["author_cmd"] for ph in placeholders)
    pf = os.path.abspath(os.path.join(work_dir, prompt_name))
    with open(pf, "w", encoding="utf-8") as fh:
        fh.write(prompt)
    instruction = (f"Read the file {pf.replace(os.sep, '/')} and follow the "
                   "instruction inside it exactly.")
    cmd = [a.replace("{prompt_file}", pf.replace(os.sep, "/"))
            .replace("{prompt_instruction}", instruction)
            .replace("{prompt}", prompt)
           for a in cfg["author_cmd"]]
    env = dict(os.environ)
    env.update(cfg.get("author_env", {}))
    z = _run(cmd, cwd=cfg.get("author_cwd"), env=env, timeout=cfg.get("timeout", 420),
             input=(prompt if use_stdin else None))
    return z.stdout or ""


def _agree_word(out):
    """Parse a free AGREE/DISAGREE reply: DISAGREE wins if present, else AGREE."""
    o = (out or "").upper()
    if re.search(r"\bDISAGREE\b", o):
        return False
    return bool(re.search(r"\bAGREE\b", o))


def _author_agrees(text, proposed_tag, cfg, work_dir):
    """Two-LLM handshake: ask the code author (LLM A) whether it agrees with the
    reviewer's classification. Returns True/False. With no author_cmd, honor the
    proposal (returns True)."""
    if not cfg.get("author_cmd"):
        return True
    prompt = ("You are the AUTHOR of the code under review. A reviewer classified "
              f"this finding as {proposed_tag}. Do you AGREE that classification is "
              "correct? Reply with ONLY: AGREE or DISAGREE.\n\nFinding: " + (text or ""))
    return _agree_word(_run_author(prompt, cfg, work_dir))


def _author_agrees_fix(text, fix, cfg, work_dir):
    """E-gate (author side) for auto-fix: have the author (LLM A) INDEPENDENTLY
    re-rate the proposed fix in the same structured form as the reviewer. Returns
    True only when the author both agrees the fix may be auto-applied AND does not
    flag it as non-determinate (a free-form reply was unreliable — structured
    yes/no parses cleanly). With no author_cmd, honor the reviewer's proposal."""
    if not cfg.get("author_cmd"):
        return True
    prompt = (
        "You are LLM A, the code author, independently re-rating a reviewer's "
        "auto-fix proposal. Assume the finding's factual description is accurate. "
        "Answer EXACTLY these two lines and nothing else:\n"
        "DETERMINATE: yes/no   (is the PROPOSED FIX one concrete edit any competent "
        "author writes the same way - restore a deleted line, add a missing `await`, "
        "fix an off-by-one, correct a typo - as opposed to choosing among approaches "
        "or a design/policy/API decision such as 'add a locking strategy', refactor, "
        "pick an algorithm?)\n"
        "AGREE: yes/no         (do you agree this fix may be applied automatically "
        "without a human?)\n\n"
        f"Finding: {text or ''}\nProposed fix: {fix or '(unspecified)'}")
    return _autofix_author_ok(_run_author(prompt, cfg, work_dir, ".autofix_author_prompt.txt"))


def _autofix_author_ok(out):
    """Parse the author's structured E-gate reply. Fail-safe: BOTH the AGREE and
    DETERMINATE lines must be EXPLICITLY affirmative. A missing/None line (an
    incomplete or malformed author reply) counts as disagreement, so it never
    permits a high-severity auto-fix — honoring the rubric's 'missing information
    escalates' policy."""
    return _yesno(out, "AGREE") is True and _yesno(out, "DETERMINATE") is True


def _yesno(out, key):
    """Read a 'KEY: yes/no' line (case-insensitive, line-anchored). Returns
    True/False, or None if the key is absent (treated as unknown by the gates)."""
    m = re.search(r"(?mi)^\s*" + re.escape(key) + r"\s*:\s*(yes|no|true|false|y|n)\b", out or "")
    return None if not m else m.group(1).lower() in ("yes", "true", "y")


def _autofix_triage_call(text, cfg, work_dir):
    """Auto-fix triage for ONE high-severity finding: ask the reviewer the five
    gate questions in a machine-readable form. Returns a triage dict
    {diagnosis_agreed, determinate, local, semantic, verifiable, sensitive, fix}
    (bool/None + a one-line fix), or None on an empty reply."""
    prompt = (
        "You are triaging ONE high-severity (CRITICAL/WARNING) code-review finding "
        "to decide whether it can be auto-fixed safely or must go to a human.\n"
        "Answer EXACTLY these seven lines and nothing else:\n"
        "DIAGNOSIS_AGREED: yes/no  (are you confident the finding is real and you know the cause?)\n"
        "DETERMINATE: yes/no       (is there exactly ONE obvious correct fix, no design/policy/API choice?)\n"
        "LOCAL: yes/no             (small, one site / few lines, trivially reversible?)\n"
        "SEMANTIC: yes/no          (does the fix change executable behaviour? answer 'no' ONLY for a pure\n"
        "                           text fix: comments, docstrings, docs, or log/UI spelling/grammar)\n"
        "VERIFIABLE: yes/no        (can an objective automated test prove the fix red->green?)\n"
        "SENSITIVE: yes/no         (does it touch security, secrets, auth, data migration/deletion,\n"
        "                           or a public API/contract?)\n"
        "FIX: <one line describing the exact change>\n\n"
        "Finding: " + (text or ""))
    pf = os.path.abspath(os.path.join(work_dir, ".autofix_prompt.txt"))
    with open(pf, "w", encoding="utf-8") as fh:
        fh.write(prompt)
    instruction = (f"Read the file {pf.replace(os.sep, '/')} and follow the "
                   "instruction inside it exactly.")
    cmd = [a.replace("{prompt_file}", pf.replace(os.sep, "/")).replace("{prompt_instruction}", instruction)
           for a in cfg["reviewer_cmd"]]
    env = dict(os.environ)
    env.update(cfg.get("reviewer_env", {}))
    z = _run(cmd, cwd=cfg.get("reviewer_cwd"), env=env, timeout=cfg.get("timeout", 420))
    out = (z.stdout or "")
    if not out.strip():
        return None
    fix = re.search(r"(?mi)^\s*FIX:\s*(.+)$", out)
    return {
        "diagnosis_agreed": _yesno(out, "DIAGNOSIS_AGREED"),
        "determinate": _yesno(out, "DETERMINATE"),
        "local": _yesno(out, "LOCAL"),
        "semantic": _yesno(out, "SEMANTIC"),
        "verifiable": _yesno(out, "VERIFIABLE"),
        "sensitive": _yesno(out, "SENSITIVE"),
        "fix": fix.group(1).strip() if fix else "",
    }


def parse_verdict(text):
    """Extract the verdict from a VERDICT: line (must start a line, not appear in
    prose). Returns 'APPROVE' / 'REQUEST_CHANGES' / '?' (the last forces a human)."""
    m = re.search(r"(?mi)^\s*VERDICT:\s*(APPROVE|REQUEST_CHANGES)\b", text or "")
    return m.group(1).upper() if m else "?"


def verdict_anomaly(verdict, findings):
    """True if the verdict contradicts the findings (a reviewer error that must
    go to a human): no/blank verdict, REQUEST_CHANGES with no findings, or
    APPROVE alongside a CRITICAL/WARNING."""
    sevs = {f.get("severity") for f in findings}
    return (verdict == "?"
            or (verdict == "REQUEST_CHANGES" and not findings)
            or (verdict == "APPROVE" and bool({"CRITICAL", "WARNING"} & sevs)))


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

    verdict = parse_verdict(review)
    findings = scoring.parse_findings(review)
    # Unknown/missing TYPE gets one precise re-classification pass; if it still
    # cannot be mapped to a documented type it escalates (reclassify_failed).
    if any(f.get("status") == "unknown_type" for f in findings):
        findings = scoring.reclassify(
            findings,
            lambda t: _reclassify_call(t, cfg, work_dir),
            lambda t, tag: _author_agrees(t, tag, cfg, work_dir))
    # Multi-category tags the reviewer proposed DIRECTLY ([SEV/A+B], [SEV/A|B]) are
    # settled by the same two-LLM handshake: the author must agree, else escalate
    # (SCORING_RUBRIC.md "Multi-category findings"). No-op when none are present.
    findings = scoring.settle_multi_type(
        findings, lambda t, tag: _author_agrees(t, tag, cfg, work_dir))
    # Auto-fix triage: a CRITICAL/WARNING may be self-repaired instead of escalated
    # when the five gates hold. Gate C (verifiable) is mandatory for a semantic
    # change and waived only for a purely non-semantic text fix. See SCORING_RUBRIC.md
    # "Auto-fixable CRITICAL/WARNING".
    #
    # Opt-OUT default with a safety interlock (see auto_fix_enabled): unset, it
    # defaults ON only when an author_cmd is configured so the E-gate is a real
    # two-LLM check; an explicit cfg["auto_fix"] always wins.
    if auto_fix_enabled(cfg):
        for f in findings:
            if not scoring.auto_fix_eligible(f):
                continue
            tri = _autofix_triage_call(f.get("text", ""), cfg, work_dir)
            if not tri:
                continue                      # no triage -> stays escalated (safe default)
            # Gate E: reviewer's diagnosis confidence AND the author's agreement
            # that the proposed fix is the single obvious one.
            agreed = tri.get("diagnosis_agreed") is True
            if agreed:
                agreed = _author_agrees_fix(f.get("text", ""), tri.get("fix", ""), cfg, work_dir)
            triage = {**tri, "agreed": agreed}
            ok, why = scoring.is_auto_fixable(triage, score_cfg)
            f["autofix"] = {"auto_fixable": ok, "reason": why, **triage}

    nh = scoring.needs_human(findings) or verdict_anomaly(verdict, findings)
    score = scoring.score_review(findings, score_cfg)
    escalate, apply_ = scoring.classify(findings, score_cfg)
    esc_str = [scoring.tag_str(f) for f in escalate] or (["NEEDS_HUMAN"] if nh else [])

    # Human-escalation drives the loop's hard stop; but high-severity *activity*
    # (even when auto-fixed) must reset the no-escalate convergence streak so the
    # loop never converges while it is still self-repairing breakage.
    blocking = scoring.has_blocking_activity(findings, score_cfg) or nh
    state = _load_json(state_file, {"consecutive_low": 0, "consecutive_no_escalate": 0, "rounds": []})
    stop, low_streak, no_esc_streak, reason = scoring.decide_convergence(
        score, verdict, blocking,
        state.get("consecutive_low", 0), state.get("consecutive_no_escalate", 0), score_cfg)
    state["consecutive_low"] = low_streak
    state["consecutive_no_escalate"] = no_esc_streak
    state.setdefault("rounds", []).append({
        "head": head, "verdict": verdict, "score": score, "needs_human": nh,
        "findings": [scoring.tag_str(f) for f in findings],
        "consecutive_low": low_streak, "consecutive_no_escalate": no_esc_streak,
        "stop": stop, "stop_reason": reason})

    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(log_file, "a", encoding="utf-8") as fh:
        fh.write(f"\n\n## Review round - {ts} - HEAD {head} - score={score} "
                 f"consec_low={low_streak} no_esc={no_esc_streak} "
                 f"{('STOP:' + reason) if stop else 'CONTINUE'}\n\n")
        fh.write(review if review else "(empty review - see stderr)\n")
        fh.write("\n")
    with open(state_file, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)

    return {
        "head": head, "verdict": verdict, "score": score, "review": review,
        "findings": [scoring.tag_str(f) for f in findings],
        "escalate": esc_str,
        "apply": [scoring.tag_str(f) for f in apply_],
        "auto_fixed": [scoring.tag_str(f) for f in apply_ if scoring.is_auto_fixed(f)],
        "stop": stop, "stop_reason": reason, "needs_human": nh,
        "consecutive_low": low_streak, "consecutive_no_escalate": no_esc_streak,
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
    print("CONSECUTIVE_LOW:", r["consecutive_low"], "/", scoring.DEFAULT_CONFIG["stop_consecutive"],
          " NO_ESCALATE:", r["consecutive_no_escalate"], "/", scoring.DEFAULT_CONFIG["stop_no_escalate_consecutive"])
    print("DECISION:", f"STOP - converged ({r['stop_reason']})" if r["stop"] else "CONTINUE")
    print("ESCALATE_TO_HUMAN:", r["escalate"] or "none")
    print("APPLY_AUTOMATICALLY:", r["apply"] or "none")
    if r.get("auto_fixed"):
        print("AUTO_FIXED (high-severity self-repair):", r["auto_fixed"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
