# Scoring rubric — shared classification guide

This is the **shared contract** both the reviewer LLM and any consumer use to
turn a review finding into a `[SEVERITY/TYPE]` tag. Numbers alone drift between
reviewers, so the *judgement* lives here as a decision tree + worked examples +
explicit handling for cases the table cannot judge.

## Two axes

A finding is tagged `[SEVERITY/TYPE]`.

- **SEVERITY** = how strongly to act (drives escalate / auto-fix / stop).
- **TYPE** = the problem domain (drives the SUGGESTION weight).

## SEVERITY decision tree (evaluate top-down; first match wins)

1. Does it **definitely** break runtime behaviour, tests, data safety, or
   security *right now* (reproducible failure, merge-blocking)? → **CRITICAL**
2. Is there a **reproducible logic defect** likely to hit a real user/compat
   path, or a silent-failure / backward-compat break that **should be fixed
   before merge** (high confidence + real impact)? → **WARNING**
3. Otherwise it is optional / unproven / polish → **SUGGESTION** (pick a TYPE).

The CRITICAL↔WARNING↔SUGGESTION line is **confidence × impact ×
reproducibility**:

| | confidence | impact | reproducible? |
|---|---|---|---|
| CRITICAL | certain (it breaks) | severe (data/security/merge) | yes, now |
| WARNING | high (reviewer can describe the defect) | real user/compat path | plausibly |
| SUGGESTION/correctness | plausible only | possible | not shown |

## TYPE table (SUGGESTION weights; CRITICAL/WARNING use the fixed severity weight)

| TYPE | scope | weight |
|---|---|---:|
| `security` | secret leak, injection, auth bypass, unsafe deserialization | 25 |
| `compatibility` | breaks existing API / config / on-disk format / callers | 20 |
| `reliability` | race, flakiness, silent failure, resource leak, retry/timeout gap | 15 |
| `correctness` | logic/behaviour risk (plausible, not proven) | 15 |
| `test` | missing/weak test for existing behaviour | 10 |
| `docs` | README / docstring / config-example ↔ code mismatch | 8 |
| `maintainability` | brittle structure, duplication, dead code | 6 |
| `consistency` | naming, counts, examples, comments internally disagree | 4 |
| `style` | subjective polish, formatting, nice-to-have, future hardening | 1 |

`security`, `compatibility`, `reliability` are split out of `correctness` on
purpose: in an autonomous loop they must never collapse into `style`/`consistency`.

## Tie-breakers (reduce ambiguity)

- **Domain beats catch-all.** If a finding fits `security`/`compatibility`/
  `reliability`, use that, not `correctness`.
- **Severity is set first, TYPE second.** A proven exploit is `CRITICAL`/`WARNING`
  (severity), even though its domain is `security`.
- **No reproduction shown ⇒ at most SUGGESTION.** Claims without a described
  failure path cannot be WARNING/CRITICAL.
- **Security severity:** a *demonstrated or obvious* exploit/exposure — a
  committed live secret, an injection with a **shown** payload, or credentials
  logged in plaintext — is `CRITICAL/security`. A real weakness with **no shown
  exploit path** is `WARNING/security`; speculative hardening is
  `SUGGESTION/security`.
- **Test-vs-correctness:** if behaviour is fine but a test is missing → `test`;
  if behaviour itself is suspect → `correctness` (or higher).
- **Docs-vs-consistency:** user-facing doc/config wrong → `docs`; internal-only
  mismatch (comment, count, variable name) → `consistency`.

## Worked examples (gold judgements)

| finding | tag |
|---|---|
| Hardcoded API token committed in source | `CRITICAL/security` |
| SQL built from a request param with a **shown** injection payload | `CRITICAL/security` |
| `eval()` on a field currently validated upstream (no shown bypass) | `WARNING/security` |
| Renaming a public config key with no alias — breaks existing configs | `WARNING/compatibility` |
| Unawaited coroutine → task silently never runs | `WARNING/reliability` |
| Division by user input with no guard → possible crash, not shown to reproduce | `SUGGESTION/correctness` |
| Public function's documented behaviour has no test | `SUGGESTION/test` |
| README example uses a flag the code no longer accepts | `SUGGESTION/docs` |
| Same parsing block copy-pasted in three adapters | `SUGGESTION/maintainability` |
| Doc says "12 tests" but the file has 14 | `SUGGESTION/consistency` |
| Prefer a list comprehension over the explicit loop | `SUGGESTION/style` |
| Tests fail on the current diff | `CRITICAL/test` |
| Off-by-one that drops the last element (reviewer shows the case) | `WARNING/correctness` |

## Exception handling (cases the table CANNOT judge)

The table is fail-safe: **ambiguity escalates, it never silently scores 0.**

1. **Unknown SEVERITY** (not `CRITICAL`/`WARNING`/`SUGGESTION`, e.g. `MAJOR`,
   `BLOCKER`, `NIT`): do **not** score 0. Weight = `escalate_min` and the round
   is flagged `needs_human` — an unrecognised severity is treated as at least
   human-review-worthy.
2. **Missing or unknown TYPE** on a `SUGGESTION` (`[SUGGESTION]` with no type, or
   e.g. `SUGGESTION/perf`): TYPE is mandatory, so this is scored conservatively at
   `unknown_type_weight` (= `correctness`/15, never `style`/1) and flagged — never
   the low default, so it cannot quietly count as "low" and allow convergence.
3. **Malformed / unparseable finding-like line** (`*`/numbered bullet, a
   `[SEVERITY/TYPE]` buried in prose, missing brackets): collected as
   `UNPARSEABLE`. Any UNPARSEABLE line flags the round `needs_human` (the
   reviewer broke format and a real finding may be hidden).
4. **Verdict ↔ findings contradiction:** `REQUEST_CHANGES` with `- none`, or
   `APPROVE` alongside a `CRITICAL`/`WARNING`, is a reviewer error → flag
   `needs_human`; never auto-stop on it.
5. **Empty / no VERDICT line:** reviewer error → `needs_human`, not a stop.

Any `needs_human` flag forces escalation regardless of the numeric score, so a
malformed or ambiguous review can never be silently converged away.

## Verdict rule (for the reviewer prompt)

- Use `REQUEST_CHANGES` if any `CRITICAL`/`WARNING` exists, or total score >
  `stop_cutoff`, or any finding must be fixed before merge.
- Use `APPROVE` only if there are no findings, or only low-value optional
  suggestions whose total score ≤ `stop_cutoff`.

## Aggregate vs per-finding (documented policy)

Escalation is decided **per finding** (any finding ≥ `escalate_min`, or any
`needs_human` flag). Convergence/stop is decided by a **dual rule** (stop on
either): (a) aggregate score ≤ `stop_cutoff` and verdict APPROVE for
`stop_consecutive` rounds, **or** (b) no escalate-level finding for
`stop_no_escalate_consecutive` rounds. Many small findings can keep the loop
alive even when none needs a human.

Missing/unknown-type suggestions are **non-escalating**: their conservative
weight (15) blocks criterion (a) (it exceeds `stop_cutoff`), but — like any
non-escalating suggestion — they do not block criterion (b). Recurring,
non-escalating findings therefore converge via (b) by design; only
`needs_human` flags (unknown severity, malformed line, verdict contradiction)
reset the no-escalate streak.
