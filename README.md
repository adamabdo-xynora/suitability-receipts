# suitability-receipts

A suitability determination that cannot be traced to documented client facts is not a weak
recommendation — it is a compliance failure. This system refuses rather than producing one.

**Input:** a documented client profile (KYC factors) plus a proposed investment recommendation,
which may arrive as free text.

**Output:** exactly one of two things.

- A **Determination** — the recommendation is supported, carrying the specific documented profile
  factors that justify it, each recorded so that later code can re-check it against the profile
  rather than take it on trust.
- A **Refusal** — naming exactly what is missing, mismatched, or unsupported.

Never a maybe. Never a hedge. Never a recommendation resting on an unstated assumption.

---

## The design principle

> **The language model parses and explains. It never determines suitability.**

Determination is made by deterministic code against the documented profile. The model's job is to
read free text and turn it into structured claims. Every claim it makes is then checked against the
profile by code that cannot be talked out of its answer.

The model may not:

- decide suitability,
- assign a risk rating that is absent from the source,
- infer a client fact that is not documented,
- soften a refusal.

Every design decision in this repository follows from that principle. If a change would let the
model's output reach the determination without passing through a deterministic check, the change is
wrong regardless of how well it reads.

### How the models enforce it

Two invariants live in the type layer, not in the engine's good intentions, so that code which
skips a check cannot produce a valid result at all:

1. **A `Determination` must record a passing check for every code in the refusal taxonomy.**
   Construction fails if any rule is unaccounted for. A supported result is therefore proof that
   all seven rules ran — not merely that none of them happened to fire.
2. **Every passing check must cite at least one documented profile factor as its basis.** There is
   no such thing as a rule that passed for no stated reason.

Each cited factor carries the profile field it came from, the value as documented, and the date it
was documented — so a verifier holding the profile can recompute the citation instead of believing
it. The `Determination` also carries a digest of the exact profile and recommendation it was
computed from, which is what makes it a receipt rather than an assertion.

---

## The refusal taxonomy

These are the product. Each is a separately checked rule with its own tests.

| Code | Refuses when |
| --- | --- |
| `missing_kyc_factor` | A factor required to determine suitability is absent — no documented risk tolerance, no time horizon, no stated objective. |
| `risk_mismatch` | The product's risk rating exceeds the documented risk tolerance. |
| `horizon_mismatch` | A lock-up, redemption schedule, or recommended holding period conflicts with the documented time horizon. |
| `liquidity_conflict` | The recommendation would breach a documented liquidity requirement — an emergency reserve, or a known upcoming expense. |
| `concentration_breach` | Post-recommendation allocation exceeds a configured threshold in a single holding, sector, or issuer. |
| `unsupported_rationale` | The model's stated reasoning cites a client fact that does not appear in the documented profile. |
| `stale_profile` | The profile's last-reviewed date exceeds the configured review interval. |

A single refusal may carry more than one reason. Reporting only the first one found would itself be
a form of hedging. Every rule runs on every determination; the engine does not short-circuit.

A rule that cannot establish its basis refuses rather than passing without one. Where a profile is
in tension with itself — a conservative risk tolerance against an aggressive stated objective, say —
the engine takes the reading that does *not* produce a supported result. Those decisions are
documented in the module docstring of `engine.py`, under "Tension between documented factors".

### The two configured thresholds

Two rules need a number that the domain does not supply. Both numbers are decisions made in this
repository, not figures from a citable source, and both are named constants at the top of
`src/suitability_receipts/engine.py` with their derivation written out:

| Constant | Value | Status |
| --- | --- | --- |
| `CONCENTRATION_LIMIT_FRACTION` | 20% of the post-recommendation portfolio | Placeholder. Not an industry standard and not a regulatory figure. Chosen only so the rule has a definite boundary; to be calibrated against the eval set. |
| `PROFILE_REVIEW_INTERVAL_DAYS` | 365 days | A repository choice encoding an annual cadence, in exact days so leap years do not shift it. No regulator is cited for it, here or in the code. |

Both are overridable per determination through `RuleConfig`, so neither is baked into a rule. Read a
`concentration_breach` refusal as "exceeded the configured limit", never as "exceeded a required
limit".

---

## Quickstart

Requires [uv](https://docs.astral.sh/uv/). Nothing else — uv fetches Python 3.12 itself.

```sh
git clone <this repo> && cd suitability-receipts
uv sync                       # create .venv and install everything
uv run pytest                 # run the tests
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict src tests
```

No API key is needed for any of the above, and none will ever be needed to run the determination
rules. The rules are pure functions over documented data.

### Mutation testing

`mutmut` over `src/` generates 573 mutants. The suite kills 542 and 31 survive — a score of 94.59%.

The score is the less interesting half. All 31 survivors were inspected individually, and every one
is an equivalent mutant rather than a test gap. They fall into two groups. Some are substitutions on
defensive branches that no input can reach: `check_risk_mismatch` re-checks that risk tolerance is
documented, but `check_missing_kyc_factor` has already refused by then, so that branch is
unreachable through `determine` and a mutation of it changes nothing observable. The rest are
changes with no observable effect at all — the contents of error messages, a sort key that is
redundant because tuple ordering already produces the same order, and default-value substitutions
where the alternative yields identical arithmetic.

No survivor was killed by adding an assertion that merely restates the implementation. That is the
only reason the accounting is worth writing down: a mutation score reported without it is a number
standing in for an argument.

One configuration requirement makes the run meaningful. `pytest` needs `pythonpath = ["src"]`,
because mutmut runs the suite from a copied tree while the editable install resolves the package
back to the original source. Without it, every mutant imports unmutated code and reports as
surviving untouched — a score that is meaningless rather than merely bad.

---

## What obligation this models

> **PLACEHOLDER — PENDING VERIFICATION. Do not rely on this section; it has not been written yet.**
>
> This section will describe the specific regulatory obligations this system is intended to model,
> including the relevant CIRO and FINRA Rule 2111 requirements, with citations to primary sources.
>
> It is deliberately empty. The maintainer is verifying the requirements against primary sources by
> hand and will write this section personally. Nothing here has been drafted from memory: in a
> compliance repository, a confident wrong citation is worse than an absent one.
>
> Until this section is written, treat this repository as an engineering artifact only. It makes no
> claim to satisfy any regulatory obligation, and nothing in it is legal or compliance advice.

---

## Project status

Built so far:

- The pydantic v2 domain models — `ClientProfile`, `Recommendation`, `Determination`, `Refusal` —
  with validation at the boundary.
- **The determination engine.** All seven rules, each its own pure function over the models, with
  the evaluation time injected: no clock reads, no environment reads, no model calls.
- Toolchain, CI, and tests proving the toolchain works end to end.

Not built yet:

- **The LLM layer.** Nothing calls a model.
- **Property-based tests** over the engine.

---

## Layout

```
src/suitability_receipts/
  __init__.py       package exports
  models.py         the four domain models and their supporting types
  engine.py         the seven rules and the `determine` entry point
tests/
  test_models.py    model validation tests
  test_engine.py    one section per rule, plus the entry point
```

## Handling of secrets

- **No credentials anywhere in this repository**, including in fixtures and tests.
- Every client profile used as test data is synthetic and obviously so — names are placeholders in
  an obviously-fake form, and identifiers use the reserved `SYNTHETIC-` prefix. If a test fixture
  ever looks like it could be a real person, that is a bug.
- **`ANTHROPIC_API_KEY` is read in exactly one place**, and that place does not exist yet. When the
  LLM layer is added it will live at `src/suitability_receipts/llm/client.py`, which will be the
  only module in the repository permitted to read the environment for credentials. Every other
  module receives an already-constructed client by injection. Determination code never touches it —
  the rules do not call a model.
