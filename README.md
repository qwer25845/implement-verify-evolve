# IVE — Implement · Verify · Evolve

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
- `CRITICAL`/`WARNING` findings **stop the loop for a human** — they are never
  auto-applied.
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
