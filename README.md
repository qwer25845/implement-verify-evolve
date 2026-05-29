# llm-review-loop

A small, reviewer-agnostic harness that runs an **LLM code-review loop with a
value-scored stopping rule**. It turns "keep asking an LLM to review until it's
happy" into something that **terminates deterministically** instead of churning
on ever-more-marginal suggestions.

The core problem it solves: when you ask an LLM to "review strictly," it will
almost always surface *one more* low-value suggestion every round — so a naive
review loop never ends. This tool scores each round by the *value* of its
findings and stops once the findings are consistently trivial.

## How it works

1. **Shared tagging convention.** Every review prompt injects a legend telling
   the reviewer to tag each finding as `[SEVERITY/TYPE]`. Because the convention
   lives in the prompt, **any reviewer LLM** (via any CLI) emits the same
   machine-scoreable format — no per-finding re-judging needed.

   ```
   SEVERITY: CRITICAL | WARNING | SUGGESTION
   TYPE:     correctness | test | docs | consistency | style
   - [SUGGESTION/test] Add a regression test for the empty-input path.
   ```

2. **Value index.** Each tag maps to a fixed weight (configurable):

   | tag | weight | meaning |
   |---|---|---|
   | `CRITICAL` | 100 | blocks merge — always act |
   | `WARNING` | 40 | should fix — escalate to a human |
   | `SUGGESTION/correctness` | 15 | claimed-but-untested behaviour / real risk |
   | `SUGGESTION/test` | 10 | missing/weak test for existing behaviour |
   | `SUGGESTION/docs` | 8 | doc ↔ code mismatch |
   | `SUGGESTION/consistency` | 4 | counts, naming, internal mismatch |
   | `SUGGESTION/style` | 1 | subjective polish / nice-to-have / future |

3. **Stopping rule.** A review's score is the sum of its findings' weights. When
   the score is `<= stop_cutoff` (default 5) for `stop_consecutive` (default 2)
   `APPROVE` rounds in a row, the loop is **converged → STOP**. Because
   `CRITICAL`/`WARNING` weigh ≥ the cutoff, blocking work can never trigger an
   early stop.

4. **Apply / escalate gate.** Findings worth `>= escalate_min` (default 40, i.e.
   `CRITICAL`/`WARNING`) are surfaced to a human; lighter suggestions are for the
   driving agent to apply directly.

## Usage

```bash
cp config.example.json config.json   # then edit paths + reviewer_cmd
python review_loop.py --config config.json
```

Each invocation runs **one round** and prints a `RESULT` block with the verdict,
score, `CONSECUTIVE_LOW`, and `DECISION: STOP | CONTINUE`. State (the
consecutive-low counter and per-round history) persists in `state_file` between
runs, so a driving agent (or a cron/loop) can call it repeatedly and stop when it
returns `STOP`. Delete the state file to reset the counter.

### Plugging in a reviewer

`reviewer_cmd` is any command that reads a prompt and prints the review to
stdout. The harness writes the full prompt to a file and substitutes
`{prompt_file}` (the path) or `{prompt_instruction}` (a ready-made "read this
file and follow it" instruction). Examples:

```jsonc
// Hermes Agent (headless one-shot)
"reviewer_cmd": ["hermes", "-z", "{prompt_instruction}"],
"reviewer_env": { "HERMES_HOME": "/path/to/profile" }

// Any CLI that takes a prompt on argv
"reviewer_cmd": ["your-llm-cli", "--prompt-file", "{prompt_file}"]
```

If `repo_dir` is set and `include_patch` is true, the harness also runs
`git format-patch -1 HEAD` and adds the patch to the review targets, so the
reviewer sees the exact diff under review.

## Configuration

See `config.example.json`. All paths and the reviewer command live there; the
code contains no machine-specific paths. The `weights` / `stop_cutoff` /
`stop_consecutive` / `escalate_min` fields are the only "value judgements" — tune
them to taste.

## Tests

```bash
python test_scoring.py   # pure logic, no model calls; exit 0 = all pass
```

## License

MIT
