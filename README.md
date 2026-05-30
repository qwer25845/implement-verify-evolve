# Implement · Verify · Evolve

> **I**mplement · **V**erify · **E**volve — *IVE* for short.

A turnkey **autonomous dev loop** for LLM coding agents. Give it a task and it
drives an implementer LLM and a reviewer LLM around a closed loop until the work
**converges** — and stops cleanly instead of churning forever on ever-more-trivial
review nits.

```
        ┌─────────────────────────────────────────────────────┐
        ▼                                                       │
   Implement  ──►  Verify  ──►  Evolve  ──►  converged? ──┐     │
   (LLM A edits     (tests +     (apply fixes /            │ no  │
    the repo)        scored       feed failures back)      └─────┘
                     review                          yes ──►  DONE
                     LLM B)                  high-severity ─►  HUMAN
```

- **I — Implement:** an agentic implementer LLM (LLM A) edits the target repo to
  satisfy the task (and, later, to fix failing tests or apply review suggestions).
- **V — Verify:** run the project's test command, then run one *scored* review
  round with a reviewer LLM (LLM B).
- **E — Evolve:** feed test failures / applicable suggestions back to the
  implementer and iterate — until the review loop **converges**, a
  human-intervention finding is raised, or `max_iterations` is hit.

## Why a *scored* loop

When you ask an LLM to "review strictly," it almost always finds *one more*
low-value suggestion every round, so a naive loop never terminates. IVE makes the
reviewer tag every finding `[SEVERITY/TYPE]`, assigns each tag a fixed value, and
**stops once findings are consistently low-value**:

| tag | weight | meaning |
|---|---|---|
| `CRITICAL` | 100 | blocks — always act |
| `WARNING` | 40 | should fix — **escalate to a human** |
| `SUGGESTION/correctness` | 15 | claimed-but-untested behaviour / real risk |
| `SUGGESTION/test` | 10 | missing/weak test for existing behaviour |
| `SUGGESTION/docs` | 8 | doc ↔ code mismatch |
| `SUGGESTION/consistency` | 4 | counts, naming, internal mismatch |
| `SUGGESTION/style` | 1 | subjective polish / nice-to-have |

A round's score = Σ finding weights. When the score is `<= stop_cutoff` (default
5) for `stop_consecutive` (default 2) `APPROVE` rounds in a row → **converged**.
Findings worth `>= escalate_min` (40, i.e. `CRITICAL`/`WARNING`) **halt the loop
for a human**; lighter suggestions are applied automatically by the implementer.

### Auto-fixing high-severity findings

Severity (impact) and *auto-fixability* (how determinate the repair) are
independent: a `CRITICAL` whose fix is the single obvious change can be repaired
without a human, while a `WARNING` whose fix is a judgement call cannot. Each
`CRITICAL`/`WARNING` is run through an **auto-fix triage** and self-repaired
(routed to the implementer) instead of escalated **only** when all five gates hold:

- **A** determinate fix · **B** local & reversible · **D** no sensitive surface
  (security/auth/data/public-API) · **E** reviewer **and** author both agree it is
  the one obvious fix;
- **C** verifiable — *mandatory* for any change that alters behaviour, *waived*
  only for a purely non-semantic text fix (comments/docstrings/docs/log copy).

Any gate failing (or any unanswered gate — fail-safe) escalates to a human, as
does any `needs_human` finding. See `SCORING_RUBRIC.md` → "Auto-fixable
CRITICAL/WARNING" for the full definition and worked examples.

**Enabling it (opt-out with a safety interlock).** The E-gate is a real two-sided
check only when a separate `author_cmd` (LLM A) is configured — without one it
degrades to the reviewer's self-report. So `auto_fix` defaults **on** when an
`author_cmd` is set and **off** when it is not. An explicit `"auto_fix": true` /
`false` always wins (use `true` to force reviewer-only auto-fix without an author,
`false` to disable it even with one). For a genuine check, point `author_cmd` at a
**different** model than the reviewer:

```jsonc
"reviewer_cmd": ["hermes", "-z", "{prompt_instruction}"],   // LLM B (reviewer)
"author_cmd":   ["claude", "-p"]                            // LLM A (author) — a different model
```

The author prompt is delivered on **stdin** when `author_cmd` has no `{prompt}` /
`{prompt_file}` / `{prompt_instruction}` placeholder (more robust than passing long
text as a CLI argument); otherwise the placeholder is substituted into argv.

## Usage

```bash
cp config.example.json config.json     # edit repo_dir, implementer_cmd, test_cmd, reviewer_cmd
python orchestrate.py --config config.json --task "Implement X with tests"
```

Exit codes: `0` converged (done), `3` max_iterations, `4` human escalation,
`1` reviewer error.

### Review-only mode

To run just the scored review loop (no implementer) against an existing diff:

```bash
python review_loop.py --config config.json    # one scored round; repeat & stop on DECISION: STOP
```

### Plugging in the two LLMs

Both commands are configurable and reviewer/implementer can be **different
models** (independent review is healthier):

```jsonc
// implementer (agentic — must edit files in repo_dir)
"implementer_cmd": ["your-coding-agent", "--prompt-file", "{prompt_file}"],
// reviewer (reads a prompt, prints the review to stdout)
"reviewer_cmd": ["hermes", "-z", "{prompt_instruction}"],
"reviewer_env": { "HERMES_HOME": "/path/to/profile" }
```

`{prompt_file}` → path to the generated prompt; `{prompt_instruction}` → a ready
"read this file and follow it" instruction; `{prompt}` (implementer) → the prompt
text inline.

## Safety

- **The implementer edits files in place and runs unattended.** Run it against a
  clean git working tree — ideally a throwaway `git worktree` — so every change is
  diffable and reversible.
- `max_iterations` is a hard cap; the loop always terminates.
- `CRITICAL`/`WARNING` findings **stop the loop for a human** unless `auto_fix` is
  active (on by default once an `author_cmd` is configured), in which case they are
  self-repaired *only* when the five auto-fix gates all pass (determinate, local,
  verifiable, no sensitive surface, two-LLM-agreed); anything ambiguous, sensitive,
  or unverifiable still escalates. Set `"auto_fix": false` to always stop for a human.
- Prefer a **different model** for review than for implementation.

## Layout

| file | role |
|---|---|
| `orchestrate.py` | IVE driver (Implement→Verify→Evolve) + pure `decide_next_action` |
| `review_loop.py` | one scored review round (`run_round`) + standalone CLI |
| `scoring.py` | pure value index + stop rule + `[SEVERITY/TYPE]` tagging legend |
| `test_scoring.py`, `test_loop_control.py` | unit tests (no model calls) |
| `config.example.json` | all machine-specific settings; no paths in code |

## Tests

```bash
python test_scoring.py && python test_loop_control.py   # exit 0 = all pass
```

## License

MIT
