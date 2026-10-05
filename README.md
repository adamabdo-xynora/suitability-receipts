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

### Whose breach it is

`risk_mismatch` and `concentration_breach` are the two rules that read the documented portfolio, so
they are the two that can object to exposure the client already held. Their reasons carry a
`breach_origin`, which the models require of those two codes and forbid on every other:

| `breach_origin` | The recommendation |
| --- | --- |
| `created` | brings the objectionable exposure into being — a purchase of a fund above the ceiling, or a trade that puts a bucket over the limit |
| `increased` | inherits it and adds to it |
| `unchanged` | inherits it and does not move it — a hold, or an unrelated purchase that dilutes the share without disposing of anything |
| `reduced` | inherits it and strictly reduces it, without bringing it inside the objection |

This is not a softer outcome and does not create one. A `reduced` breach refuses with the same force
as a `created` one, and the residual is stated on the receipt in figures — a sell that takes a
position from 40% to 29.41% of the portfolio is refused, and the receipt says 29.41%. What the field
buys is that an advisor unwinding a concentration the client arrived with is no longer recorded
identically to one who built it. The distinction is drawn on the exposure **amount**, never on its
share: growing the portfolio dilutes a percentage without disposing of anything, and dilution does
not earn the receipt a disposal earns.

A rule that cannot establish its basis refuses rather than passing without one. Where a profile is
in tension with itself — a conservative risk tolerance against an aggressive stated objective, say —
the engine takes the reading that does *not* produce a supported result. Those decisions are
documented in the module docstring of `engine.py`, under "Tension between documented factors".

### The three configured constants

Three rules need a number the domain does not supply. All three are decisions made in this
repository, not figures from a citable source, and all three are named constants at the top of
`src/suitability_receipts/engine.py` with their derivation written out:

| Constant | Value | Status |
| --- | --- | --- |
| `CONCENTRATION_LIMIT_FRACTION` | 20% of the post-recommendation portfolio | Boundary measured and kept; **level still a guess**. Not an industry standard and not a regulatory figure. |
| `PROFILE_REVIEW_INTERVAL_DAYS` | 365 days | Boundary measured and kept — the interval is inclusive, so a profile reviewed exactly a year ago still determines. **Level still a guess.** |
| `OBJECTIVE_RISK_CEILING` | a risk ceiling per stated objective | Mechanism measured: the mapping constrains on its own and each entry means the level it names. **Rows still a guess**, and because the ceiling is the minimum over every documented objective, one unsourced row reaches further than it looks. |

"Boundary measured" and "level measured" are different claims, and only the first is made. The eval
set pins each threshold to the cent and to the day — 20.000000% supported and 20.000007% refused,
365 days supported and 366 refused — which establishes that the rules enforce their constants
exactly where the docstrings say. It establishes nothing about whether 20% or 365 is the right
number, because every case that turns on the level was constructed from the level. Measuring that
needs a source of truth outside this repository, and there is none yet.

The first two are overridable per determination through `RuleConfig`, so neither is baked into a
rule. Read a `concentration_breach` refusal as "exceeded the configured limit", never as "exceeded a
required limit".

---

## The LLM layer

The model does two things, and the code around it is arranged so that it cannot do a third.

### It parses

Free text in, a validated `Recommendation` out, through a single forced tool call against a strict
schema — the model returns structure, never prose to be parsed. Every field the source text does not
state is `null` in the schema, and a `null` refuses the parse rather than being read as a default: an
unstated lock-up is not zero days, and unstated redemption terms are not daily liquidity.

The product's **risk rating** has exactly two permitted sources, and no third:

| Source | How it is trusted |
| --- | --- |
| A supplied product record | Used whole. The catalogue is authoritative and anything the model said about that product is ignored. |
| The source text | The model must return the verbatim words that state the rating. Code checks the quotation appears in the source, **re-derives the rating from the quotation itself** (longest match over `RISK_RATING_SPELLINGS`), and refuses if that reading disagrees with what the model recorded. |

If the text states no rating and no record is supplied, parsing fails. There is no middling default,
which would be a guess wearing a neutral face.

### It explains

The model writes prose for a `Determination` or `Refusal` the engine has **already** produced, and
declares which documented factors it relied on as structured `FactorCitation`s — plus, for a
missing-factor refusal, which factors it relied on the profile *not* documenting. Code then checks
every declaration against the profile, through the same function the engine's own
`unsupported_rationale` rule uses, so the two cannot drift apart.

One failed declaration refuses the whole rationale. Nothing is repaired: there is no path that drops
the bad citation and keeps the sentence. **A patched rationale is one nobody reviewed.**

### What verification catches, and what it does not

Verification checks the declarations, plus one thing in the prose: every run of digits must be
traceable to the material the rationale declared. A fabricated figure is caught even when every
citation is correct.

It does **not** read the prose for meaning. A rationale can declare three correct citations and
still assert something undocumented in a sentence — a qualitative claim, a number written in words,
or a true citation used to dress an unrelated assertion — and this code will return
`VerifiedRationale`. So a verified rationale means *every client fact it declared is documented as
declared, and every digit it wrote is traceable*. It does not mean the prose is true. Read it as a
checked bibliography, not a checked argument. The full list of gaps is in the module docstring of
`llm/verification.py`, written out rather than summarised, because the width of that gap is exactly
what a reader of a verified rationale is entitled to know.

### The over-refusal check

A verifier that refuses everything is trivially safe and useless. `tests/test_rationale_over_refusal.py`
holds a set of rationales that are correct, fully supported, and **must** pass — spanning both
outcomes, because a refusal is the harder half to explain without softening it, and the half where
an over-strict verifier does the most damage. It was written with the verifier, not after the first
complaint about it. When a check is tightened and a case there fails, that is the signal the
tightening cost more than it bought.

---

## The eval set

```sh
uv run python -m eval        # prints the report; exits non-zero when a category misses
```

35 cases in `eval/cases.py`, each a `ClientProfile`, a `Recommendation`, an expected outcome, and a
short written reason for that expectation. **Every expectation was derived by reading the inputs
against the documented rules, never by running the engine and recording what it said.** A case
whose expectation came from the engine can only confirm that the engine agrees with itself.

The set is scored by category, with the thresholds committed as constants in `eval/scorer.py`.
There is no blended number anywhere in the report, because a blended number lets a soundness
failure be paid for with easy passes. The report separates four outcomes that a
supported-versus-refused comparison would collapse into two:

| | |
| --- | --- |
| **passed** | the outcome and every refusal code are what the case says |
| **wrong reason** | refused as expected, but not for the codes the case named — right at the top of the receipt, wrong in the part an advisor acts on |
| **over-refusal** | refused something the set says is supported |
| **under-refusal** | supported something the set says must refuse — printed **above** the table, because the README's claim is that a supported receipt means all seven rules ran and none refused, and one instance falsifies that claim |

15 of the 35 cases must *not* refuse. That half is the one most case sets skip, and skipping it
scores an engine that refuses everything at full marks. A case may also name the `breach_origin` it
expects, and 11 do — without that, a set would score an engine that reported a disposal and a
purchase identically as correct on both.

### What it currently reports

35 of 35 agree with the engine: no under-refusals, no over-refusals, no wrong-reason refusals, and
every boundary and factor-tension case lands where the reading said it would.

It did not start there. Two cases disagreed, both over-refusals, and both from the same root cause —
**the risk and concentration rules did not look at the recommendation's action**:

- A partial **sell of a holding rated above the documented tolerance** refused for `risk_mismatch`,
  though it left the client holding strictly less of the thing the tolerance cannot support.
- A **sell that reduces an over-concentration without curing it** refused for
  `concentration_breach`, giving the same verdict to a trade that halves a breach and a trade that
  creates one.

Neither was resolved by moving the expectation to wherever the engine already was. Both cases asked
for `supported` and named the cost of that honestly — a receipt reading "supported" over a portfolio
still a third of the way into one name — and it was that cost which decided it. **The refusals
stand, and the conflation does not**: each of those two rules now states a `breach_origin`, so the
receipt says whether the recommendation created the breach or inherited and reduced it. The
`why` on each case keeps the argument it originally made alongside the answer that was reached; the
history of the decision is the point, and a revised expectation rewritten to look as though it was
always right is worth less than one that shows its working.

One thing the change surfaced that nobody had asked about: because every bucket is checked and not
only the ones traded, a **disposal can create a breach in a bucket it never touches** — selling one
position shrinks the portfolio the others are measured against, so a holding sitting exactly at the
limit can be over it afterwards without having moved. That is reported as `created`, because it is,
and it is why a reason covering several breaches carries the most severe origin among them rather
than the one belonging to the instrument being traded. It was found by a property test, not by a
case somebody thought of.

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

The same suite runs inside a container, offline and with no credential in it:

```sh
docker build --target test -t suitability-receipts:test .
docker run --rm suitability-receipts:test uv run pytest      # 334 tests, no key, no network
docker build -t suitability-receipts .                       # the lean runtime image
```

Both stages install with `uv sync --locked`, so an image is built from the committed lockfile or not
at all. The runtime image is worth being clear about: it holds the library, its two dependencies and
nothing else — no pytest, no ruff, no `eval/`, and no way for `ANTHROPIC_API_KEY` to reach a layer,
since there is no `ARG`, no `ENV` and no `.env` in the build context. There is no long-running
process here to run in it, and the `Dockerfile` says so rather than inventing a `CMD`. The image
worth having is the test one.

### Container image

The runtime image is published to GHCR on every version tag, by a workflow whose gate runs the 334
tests, the 35 eval cases, `ruff check` and `mypy --strict` inside the test image first — the push
step is unreachable unless all four pass.

    docker pull ghcr.io/adamabdo-xynora/suitability-receipts:0.1.1
    docker run --rm ghcr.io/adamabdo-xynora/suitability-receipts:0.1.1 \
      python -c "from suitability_receipts import determine; print(determine)"

That import is the whole of what this image is for, and it is published as a library layer rather
than as something to start. It has no `CMD` and no `ENTRYPOINT`, for the reason given above: it
cannot run the tests, it cannot run `eval/`, and with no credential it raises `MissingApiKeyError`.
Published for `linux/amd64` and `linux/arm64`.

The image carries signed build provenance, so you can check that these bytes came from this
repository's CI rather than from someone with push access to the registry:

    gh attestation verify oci://ghcr.io/adamabdo-xynora/suitability-receipts:0.1.1 --owner adamabdo-xynora

`0.1.0` remains published, `linux/amd64` only and without an attestation. Its digest has not
changed and will not: a version that alters its bytes is not a version.

### Mutation testing

`mutmut` over `src/` generates 573 mutants. The suite kills 542 and 31 survive — a score of 94.59%.

> **Stale as of the `breach_origin` change.** Those figures were measured before `BreachOrigin`, the
> two origin classifiers and the before/after exposure split were added, so the mutant count is no
> longer 573 and the accounting below no longer enumerates every survivor. The run has not been
> repeated. Treat the number as the last measurement rather than the current one until it is.

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
- **Property-based tests** over the engine, and a mutation run with its survivors accounted for.
- **The LLM layer and the rationale verifier**, described in the section above. Every test in it
  runs against a stub client: no test needs a key or a network.
- **The eval set and its scorer**, described in the section above, with two open disagreements
  reported rather than resolved by editing the cases.
- Toolchain, CI, and tests proving the toolchain works end to end.

Not built yet:

- **A calibration for the levels.** The eval set measures where each configured constant is
  enforced, not whether it is set in the right place, so `CONCENTRATION_LIMIT_FRACTION`,
  `PROFILE_REVIEW_INTERVAL_DAYS` and the rows of `OBJECTIVE_RISK_CEILING` stay labelled as
  repository choices. `RISK_RATING_SPELLINGS` is untouched by the eval set entirely.
- **A decision on whether a full disposal should still refuse.** Selling a position *entirely*
  still refuses under `risk_mismatch`, with origin `reduced`: that rule objects to the product's
  risk rating, which is a fact about the product that no disposal changes. Whether the objection
  should lift once the exposure reaches zero is a real question and a different one from the
  created-versus-inherited split, and it is not answered here. `tests/test_engine.py` pins the
  current answer at the boundary rather than leaving it to chance.
- **The regulatory section** below, which the maintainer is verifying by hand.

---

## Layout

```
src/suitability_receipts/
  __init__.py         package exports; imports no SDK
  models.py           the four domain models and their supporting types
  engine.py           the seven rules and the `determine` entry point
  llm/
    __init__.py       the model layer's exports
    client.py         the only module that reads ANTHROPIC_API_KEY, and the only
                      one that imports the SDK; forced tool use, strict schemas
    parsing.py        free text -> a validated Recommendation, or a refusal to parse
    rationale.py      the drafting prompt, the tool schema, and `explain`
    verification.py   the verifier: pure, client-free, and where "no" is decided
tests/
  synthetic.py                    synthetic fixtures and the stub client
  test_models.py                  model validation tests
  test_engine.py                  one section per rule, plus the entry point
  test_engine_properties.py       Hypothesis properties over the engine
  test_llm_client.py              credential handling and the shape of the call
  test_llm_parsing.py             where a risk rating may come from, and refusals
  test_llm_verification.py        the declarations that do not hold
  test_rationale_over_refusal.py  the rationales that MUST pass
  test_llm_boundary.py            the boundary, enforced by parsing the source tree
  test_eval_set.py                guards the eval set's shape, not the engine's score
eval/
  cases.py            35 cases, each with the reading that produced its expectation
  scorer.py           per-category thresholds, the four verdicts, and the report
  __main__.py         `uv run python -m eval`
```

## Handling of secrets

- **No credentials anywhere in this repository**, including in fixtures and tests.
- Every client profile used as test data is synthetic and obviously so — names are placeholders in
  an obviously-fake form, and identifiers use the reserved `SYNTHETIC-` prefix. If a test fixture
  ever looks like it could be a real person, that is a bug.
- **`ANTHROPIC_API_KEY` is read in exactly one place**: `src/suitability_receipts/llm/client.py`,
  the only module in the repository permitted to read the environment for credentials. Every other
  module receives an already-constructed client by injection. Determination code never touches it —
  the rules do not call a model.
- That is enforced, not documented and hoped for. `tests/test_llm_boundary.py` parses every module
  under `src/` and fails if any file other than `client.py` imports `os` or `anthropic`, and if
  `client.py` ever stops reading the environment. The key is passed to the SDK explicitly rather
  than left to its own credential resolution, so there is exactly one way a credential enters the
  process.
