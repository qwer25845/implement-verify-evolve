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
# CRITICAL/WARNING also follow the unknown-type contract (no silent 'ok')
check("WARNING undocumented type -> unknown_type", statuses("- [WARNING/perf] x"),
      [("WARNING", "perf", "unknown_type")])
check("CRITICAL no type -> unknown_type", statuses("- [CRITICAL] x"),
      [("CRITICAL", None, "unknown_type")])
check("WARNING documented type stays ok", statuses("- [WARNING/security] x"),
      [("WARNING", "security", "ok")])
check("CRITICAL unknown_type keeps fixed weight",
      s.weight_of({"severity": "CRITICAL", "type": "perf", "status": "unknown_type"}), 100)
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

# ── multi-category: A+B (disjoint -> sum), A|B (overlap -> max) ─────────
check("parse_type_spec single", s.parse_type_spec("security"), (["security"], "single"))
check("parse_type_spec sum", s.parse_type_spec("security+test"), (["security", "test"], "sum"))
check("parse_type_spec max", s.parse_type_spec("reliability|correctness"), (["reliability", "correctness"], "max"))

def first(text):
    return s.parse_findings(text)[0]

fd = first("- [SUGGESTION/security+test] leaks a key and has no test")
check("multi sum parsed", (fd["types"], fd["relation"], fd["status"]), (["security", "test"], "sum", "ok"))
check("multi sum weight", s.weight_of(fd), 25 + 10)
fo = first("- [SUGGESTION/reliability|correctness] missing timeout")
check("multi max parsed", (fo["types"], fo["relation"]), (["reliability", "correctness"], "max"))
check("multi max weight", s.weight_of(fo), 15)
check("multi max weight (uneven)", s.weight_of(first("- [SUGGESTION/security|test] x")), 25)
check("multi tag_str sum", s.tag_str(fd), "SUGGESTION/security+test")
check("multi tag_str max", s.tag_str(fo), "SUGGESTION/reliability|correctness")

# reclassify can return a multi-spec; all parts must be documented
check("reclassify -> multi sum",
      [(g.get("types"), g.get("relation"), g["status"]) for g in s.reclassify(uf, _stub("reliability+test"))],
      [(["reliability", "test"], "sum", "ok")])
check("reclassify multi with undocumented -> failed",
      [g["status"] for g in s.reclassify(uf, _stub("reliability+perf"))], ["reclassify_failed"])

# ── multi-type handshake: direct A+B needs author agreement, else escalate ──
md = s.parse_findings("- [SUGGESTION/security+test] leaks a key and has no test")
check("multi-type pre-handshake ok", md[0]["status"], "ok")
check("multi-type author agrees -> ok",
      [f["status"] for f in s.settle_multi_type(md, lambda _t, _tag: True)], ["ok"])
check("multi-type author disagrees -> agreement_failed",
      [f["status"] for f in s.settle_multi_type(md, lambda _t, _tag: False)], ["agreement_failed"])
check("agreement_failed needs_human", s.needs_human([{"status": "agreement_failed"}]), True)
check("agreement_failed weight escalates", s.weight_of({"status": "agreement_failed"}), CFG["escalate_min"])
check("agreement_failed tag", s.tag_str({"status": "agreement_failed"}), "AGREEMENT_FAILED")
check("single-type untouched by settle",
      [f["status"] for f in s.settle_multi_type(s.parse_findings("- [SUGGESTION/test] a"), lambda _t, _tag: False)], ["ok"])
# a multi-type already settled via reclassify is not re-settled
_rc = s.reclassify(s.parse_findings("- [SUGGESTION/perf] x"), _stub("reliability+test"), lambda _t, _tag: True)
check("reclassified multi not re-settled",
      [f["status"] for f in s.settle_multi_type(_rc, lambda _t, _tag: False)], ["ok"])

# ── two-LLM agreement: author must agree, else escalate ────────────────
_agree = lambda _t, _tag: True
_disagree = lambda _t, _tag: False
check("author agrees -> ok",
      [(g["status"], g.get("agreed")) for g in s.reclassify(uf, _stub("reliability"), _agree)],
      [("ok", True)])
check("author disagrees -> escalate",
      [(g["status"], g.get("disagreed")) for g in s.reclassify(uf, _stub("reliability"), _disagree)],
      [("reclassify_failed", True)])
check("disagree needs_human", s.needs_human(s.reclassify(uf, _stub("reliability"), _disagree)), True)
check("no agreer honors proposal",
      [g["status"] for g in s.reclassify(uf, _stub("reliability"))], ["ok"])

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
# (b) no-escalate path: findings present but none escalating (e.g. SUGGESTION/test=10,
# score>cutoff). Independent of the verdict -> a correct REQUEST_CHANGES (score>cutoff
# per the Verdict rule) still advances path (b); only path (a) requires APPROVE.
check("no-esc #2 not yet", s.decide_convergence(10, "REQUEST_CHANGES", False, 0, 1), (False, 0, 2, ""))
check("no-esc #3 -> stop", s.decide_convergence(10, "REQUEST_CHANGES", False, 0, 2), (True, 0, 3, "no_escalate"))
# escalate resets the no-escalate streak (and low streak)
check("escalate resets", s.decide_convergence(40, "REQUEST_CHANGES", True, 1, 2), (False, 0, 0, ""))

# ── auto-fixable CRITICAL/WARNING (Gate C mandatory for semantic, waived for text) ──
def tri(**kw):
    base = {"agreed": True, "determinate": True, "local": True,
            "sensitive": False, "semantic": True, "verifiable": True}
    base.update(kw)
    return base

check("autofix all gates pass", s.is_auto_fixable(tri())[0], True)
check("autofix all gates reason", s.is_auto_fixable(tri())[1], "auto_fixable")
check("autofix needs agreement (E)", s.is_auto_fixable(tri(agreed=False))[0], False)
check("autofix E reason", s.is_auto_fixable(tri(agreed=False))[1], "gate_E_no_agreement")
check("autofix needs determinate (A)", s.is_auto_fixable(tri(determinate=False))[0], False)
check("autofix needs local (B)", s.is_auto_fixable(tri(local=False))[0], False)
check("autofix sensitive blocks (D)", s.is_auto_fixable(tri(sensitive=True))[0], False)
check("autofix D reason", s.is_auto_fixable(tri(sensitive=True))[1], "gate_D_sensitive_surface")
# Gate C is MANDATORY for a semantic change
check("autofix semantic needs verifiable (C)", s.is_auto_fixable(tri(verifiable=False))[0], False)
check("autofix C reason", s.is_auto_fixable(tri(verifiable=False))[1], "gate_C_unverifiable")
# ...but WAIVED for a purely non-semantic text fix
check("autofix nonsemantic waives C", s.is_auto_fixable(tri(semantic=False, verifiable=False))[0], True)
check("autofix nonsemantic reason", s.is_auto_fixable(tri(semantic=False, verifiable=False))[1], "auto_fixable_nonsemantic")
# a non-semantic fix on a sensitive surface still blocks (Gate D checked first)
check("autofix nonsemantic still blocked by D", s.is_auto_fixable(tri(semantic=False, verifiable=False, sensitive=True))[0], False)
# fail-safe: missing gates never auto-fix
check("autofix empty triage", s.is_auto_fixable({})[0], False)
_t = tri(); del _t["sensitive"]
check("autofix missing sensitive => treated sensitive (block)", s.is_auto_fixable(_t)[0], False)
_t = tri(); del _t["semantic"]
check("autofix missing semantic => semantic default (verifiable ok)", s.is_auto_fixable(_t)[0], True)
_t = tri(verifiable=False); del _t["semantic"]
check("autofix missing semantic + not verifiable => block", s.is_auto_fixable(_t)[0], False)

def crit(autofix=None, sev="CRITICAL"):
    f = {"severity": sev, "type": None, "status": "ok", "text": "x"}
    if autofix is not None:
        f["autofix"] = autofix
    return f

check("eligible critical ok", s.auto_fix_eligible(crit()), True)
check("eligible warning ok", s.auto_fix_eligible(crit(sev="WARNING")), True)
check("not eligible suggestion", s.auto_fix_eligible({"severity": "SUGGESTION", "status": "ok"}), False)
check("not eligible unparseable", s.auto_fix_eligible({"status": "unparseable"}), False)
check("is_auto_fixed true", s.is_auto_fixed(crit({"auto_fixable": True})), True)
check("is_auto_fixed false flag", s.is_auto_fixed(crit({"auto_fixable": False})), False)
check("is_auto_fixed needs the flag", s.is_auto_fixed(crit()), False)

# classify: an auto-fixed CRITICAL goes to apply_, an un-triaged CRITICAL escalates
esc2, app2 = s.classify([crit({"auto_fixable": True}), crit()])
check("autofix routed to apply", (len(esc2), len(app2)), (1, 1))
# a needs-human status never auto-fixes, even if mislabeled
esc3, _ = s.classify([{"severity": None, "status": "unparseable", "autofix": {"auto_fixable": True}}])
check("unparseable never auto-fixed", len(esc3), 1)

# has_blocking_activity: an auto-fixed CRITICAL still counts (no false convergence)
check("blocking incl auto-fixed", s.has_blocking_activity([crit({"auto_fixable": True})]), True)
check("blocking false for low-only", s.has_blocking_activity(s.parse_findings("- [SUGGESTION/style] x")), False)
# tag_str annotates an auto-fixed finding
check("tag_str autofix marker", s.tag_str(crit({"auto_fixable": True})), "CRITICAL (auto-fix)")
check("tag_str no marker untriaged", s.tag_str(crit()), "CRITICAL")

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
