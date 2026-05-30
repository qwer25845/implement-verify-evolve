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

## Multi-category findings (one error spanning two types)

Sometimes one finding does not fit *exactly* one type — it can reasonably be
judged as both A and B. When the reviewer proposes this **and the author agrees**,
score it by whether the two categories are disjoint or overlapping:

- **Disjoint** (no intersection — two genuinely separate problems described
  together): tag `[SEVERITY/A+B]` → **both scores are added** (`weight(A) +
  weight(B)`). Example: a block that both leaks a secret *and* has no test →
  `[SUGGESTION/security+test]` = 25 + 10 = 35.
- **Overlapping** (one underlying problem seen through two lenses): tag
  `[SEVERITY/A|B]` → **only the larger score counts** (`max(weight(A),
  weight(B))`), so the same issue is never double-counted. Example: a missing
  timeout that is both a reliability and a correctness concern →
  `[SUGGESTION/reliability|correctness]` = max(15, 15) = 15.

**Two-LLM agreement.** A multi-category (and any re-classified) finding is settled
by a handshake: the reviewer (LLM B) proposes the classification, and the code
author (LLM A) is asked whether it agrees. If **both agree**, the score is
finalized and — when it is below the escalation threshold — the loop auto-fixes
and continues **without human escalation**. If they **disagree**, the finding
escalates to a human to resolve. (With no author configured, a well-formed
proposal whose types are all documented is honored.) An ambiguous multi-spec with
no `+` defaults to `|` (max) so it can never silently inflate the score.

## Exception handling (cases the table CANNOT judge)

The table is fail-safe: **ambiguity escalates, it never silently scores 0.**

1. **Unknown SEVERITY** (not `CRITICAL`/`WARNING`/`SUGGESTION`, e.g. `MAJOR`,
   `BLOCKER`, `NIT`): do **not** score 0. Weight = `escalate_min` and the round
   is flagged `needs_human` — an unrecognised severity is treated as at least
   human-review-worthy.
2. **Missing or unknown TYPE** on any finding (`[SUGGESTION]` with no type,
   `[SUGGESTION/perf]`, `[WARNING/perf]`, `[CRITICAL]` with no type, …): TYPE is
   mandatory for every severity, so the finding gets **one more precise
   re-classification pass** (the reviewer is asked to map it to exactly one
   documented type). If that succeeds the finding takes the documented type and is
   scored normally (a `CRITICAL`/`WARNING` keeps its fixed severity weight); if it
   still cannot be classified it becomes `reclassify_failed` → **escalates** (needs
   human). It is never silently accepted as `ok`.
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

Missing/unknown-type suggestions are first re-classified (exception #2): they
either resolve to a documented type (scored normally) or become
`reclassify_failed` and escalate. Only escalating/`needs_human` findings (unknown
severity, malformed line, verdict contradiction, reclassify-failure) reset the
no-escalate streak; ordinary low-value suggestions converge via criterion (b) by
design.

## Auto-fixable `CRITICAL`/`WARNING` (self-repair vs. escalate)

By default every `CRITICAL`/`WARNING` escalates to a human. But *severity*
(impact — how bad if left unfixed) and *auto-fixability* (fix-determinacy — how
unambiguous the repair) are **independent axes**: a `CRITICAL` whose repair is the
single obvious change is auto-fixable; a `WARNING` whose repair is a judgement call
is not. When the loop runs with `auto_fix` enabled, a high-severity finding may be
**self-repaired instead of escalated** — but only when every gate below holds.

### The five gates

A `CRITICAL`/`WARNING` is **auto-fixable** only if ALL hold:

- **A. Determinate fix** — exactly one obvious correct change; no choice among
  alternatives, no policy/design/API/data-model decision. (restore call order, add
  a missing `await`, fix an intended off-by-one, correct a misspelled identifier,
  re-add an accidentally deleted line.)
- **B. Local & reversible** — small, one site / few lines, trivially diffable and
  revertible; not a cross-cutting refactor.
- **C. Verifiable** — an objective check (an existing failing test → green, or a
  new test pinning the intended behaviour) can prove the fix red→green.
  **Gate C is MANDATORY for any *semantic* change** (one that alters executable
  behaviour) and is **waived only for a purely *non-semantic* text fix** (see the
  exception below).
- **D. No sensitive surface** — does not change security posture, secrets, auth,
  data migration/deletion, or a public API/contract (where "right" is itself a
  decision).
- **E. Two-LLM agreement** — the reviewer (B) owns the diagnosis (its triage
  includes a diagnosis-confidence check, `DIAGNOSIS_AGREED`) and proposes the fix;
  the author (A) then *independently* agrees the fix is the single obvious,
  determinate change. Auto-fix requires both. The author takes the reviewer's
  factual diagnosis as given (the reviewer owns diagnosis confidence — asking a
  second model to re-derive an unseen fact only makes it abstain) and concentrates
  its independent judgement on whether the repair is unambiguous; this is what
  catches a "looks obvious but is really a design choice" fix. (Reuses the handshake
  from "Two-LLM agreement" above.)

**Any gate fails → escalate to a human.** Fail-safe: a gate the triage leaves
unanswered counts as *not satisfied* (and an unanswered "sensitive?" counts as
*sensitive*), so missing information always escalates, never auto-fixes.

### Gate C — the non-semantic exception (why it is mandatory for code)

Gate C is mandatory for any change that alters executable behaviour because an
"obvious" fix can be a hidden regression: e.g. two LLMs both agree `if x > 0`
"obviously" should be `if x >= 0`, but applying it turns a *different* existing
test red. Only an objective red→green check catches that — so semantic changes
must be verifiable to auto-fix.

The single exception is a **purely non-semantic text fix**, where no objective
runtime test is meaningful or possible:

- comments, docstrings, Markdown docs, or spelling/grammar in log or UI strings,
- **no executable logic changed**, and
- it does not touch a sensitive surface (Gate D still applies — a contractual or
  safety-critical string is *not* "non-semantic").

Such a fix may auto-apply with Gate C waived. (In practice these are usually better
re-tagged below `CRITICAL`/`WARNING` in the first place — a log typo is a
`SUGGESTION`, not a `CRITICAL`.) Everything else that changes behaviour must pass
Gate C.

### Worked examples (auto-fix triage)

| finding | gates | outcome |
|---|---|---|
| Deleted `return` line; an existing test already fails | A,B,C,D,E ✓ | `CRITICAL` → **auto-fix** |
| Off-by-one drops last element; existing test fails | A,B,C,D,E ✓ | `WARNING` → **auto-fix** |
| Missing `await`; verifiable with a spy/fake async (no live net) | A,B,C,D,E ✓ | `WARNING` → **auto-fix** |
| Missing `await` but *no* reliable test can be written | C ✗ | **escalate** |
| `if x > 0`→`>= 0` looks obvious, but the fix turns another test red | C ✗ (regression) | **escalate** |
| Log string typo `Conection`→`Connection` (non-semantic, no logic) | C waived | **auto-fix** (prefer re-tag to `SUGGESTION`) |
| `except ValueError`→`except Exception` "restore" the narrow catch | A ✗ / D (error semantics) | **escalate** |
| SQL injection fix | D ✗ (security) | **escalate** |
| Race needing a locking strategy | A ✗ (design choice) | **escalate** |
| Public-API break | D ✗ (contract) | **escalate** |
| Vulnerable dependency version bump | B ✗ / D ✗ (broad, security) | **escalate** |

### How it integrates

- High-severity findings carry an `auto_fixable` axis set by an **auto-fix triage**:
  the reviewer (B) answers gates A–D + *semantic?* + a diagnosis-confidence check +
  the exact fix, and the author (A) then *independently* agrees the fix is the single
  obvious determinate change (E), taking the reviewer's factual diagnosis as given.
- Escalation becomes: **escalate ⟺ (weight ≥ `escalate_min`) AND NOT
  (auto_fixable AND two-LLM-agreed)**. An auto-fixed high finding is routed to the
  *apply* lane (the implementer makes the fix) instead of the *escalate* lane.
- A `needs_human` status (unknown severity/type, unparseable line, reclassify
  failure, verdict contradiction) is **never** auto-fixable — it always escalates.
- An auto-fixed high finding still counts as high-severity *activity*, so it resets
  the no-escalate convergence streak: the loop never declares "converged" while it
  is actively self-repairing breakage.
- Auto-fix is **opt-out with a safety interlock** (`config.auto_fix`): unset, it
  defaults **on** only when an `author_cmd` is configured, because the E-gate is a
  genuine two-sided check only with a separate author LLM; with no author it
  degrades to the reviewer's self-report, so it stays off unless explicitly forced.
  An explicit `true`/`false` always wins.
