"""The eval set and its scorer.

What this exists for
--------------------
The engine had rules and no evidence they were calibrated. Three numbers in
`engine.py` — `CONCENTRATION_LIMIT_FRACTION`, `PROFILE_REVIEW_INTERVAL_DAYS`, and
`OBJECTIVE_RISK_CEILING` — are labelled there as repository choices with no derivation.
This package is what turns them into measured decisions or, where the evidence does not
reach, records plainly that they are still guesses.

It is also where a question left open during the engine build gets an answer: a `sell`
that reduces an over-concentrated position without curing it currently refuses. The
concentration cases in `cases.py` state what each of those should be, and why, and the
scorer reports where the engine disagrees.

How the expectations were derived
---------------------------------
By reading each case's profile and recommendation against the rules as documented, and
writing down what the rules say the answer is — never by running the engine and
recording its output. A case whose expectation came from the engine tests nothing: it
can only ever confirm that the engine agrees with itself. Every case carries a `why`
naming the reading that produced its expectation, so a disagreement between the set and
the engine is a disagreement between two stated positions rather than between an
assertion and a number.

That is also why `cases.py` builds its own synthetic profiles instead of importing
`tests/synthetic.py`. The fixtures there were built alongside the rules; a case set
built on them inherits their assumptions about what a normal profile looks like. The
convention is the same — `SYNTHETIC-` identifiers, obviously-placeholder names — but the
data is stated independently.

Running it
----------
    uv run python -m eval

Exits non-zero when any category misses its threshold, so CI can gate on it.
"""

from eval.cases import CASES, Category, EvalCase
from eval.scorer import (
    CATEGORY_THRESHOLDS,
    MAX_OVER_REFUSALS,
    MAX_UNDER_REFUSALS,
    CaseResult,
    Classification,
    Report,
    main,
    run,
    score,
)

__all__ = [
    "CASES",
    "CATEGORY_THRESHOLDS",
    "MAX_OVER_REFUSALS",
    "MAX_UNDER_REFUSALS",
    "CaseResult",
    "Category",
    "Classification",
    "EvalCase",
    "Report",
    "main",
    "run",
    "score",
]
