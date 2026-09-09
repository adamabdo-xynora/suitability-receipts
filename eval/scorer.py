"""Scoring the eval set: per-category thresholds, and a report that names the failure kind.

Why there is no single number
-----------------------------
A blended score lets a soundness failure be paid for with easy passes elsewhere. Thirty
cases right and five wrong reads as 86%, which sounds like a good grade and can hide the
one result that matters. So every category carries its own threshold, every threshold is
a committed constant in this module, and a category that misses its own threshold fails
the run whatever the others did.

Four outcomes, and they are not equally bad
-------------------------------------------
`PASSED`
    The engine's outcome and its refusal codes are both what the case says.
`WRONG_REASON`
    The engine refused, as the case said it should, but not for the codes the case
    named — or not with the missing factors or the breach origin it named. The receipt
    is right at the top and wrong in the part an advisor would act on, and a set that
    only compared supported-versus-refused would score this as a pass.
`OVER_REFUSAL`
    The engine refused something the set says is supported. This is the failure mode a
    determination engine drifts into on its own, because every tightening looks
    responsible in isolation.
`UNDER_REFUSAL`
    The engine supported something the set says must refuse. This is the headline, and
    it is printed above the table rather than in it: the README's claim is that a
    supported receipt means all seven rules ran and none refused, and a single instance
    of this falsifies that claim. It is not a percentage point.

Thresholds
----------
Every category is set to 1.00, and that is a decision rather than a placeholder. The
engine is deterministic and every expectation here was derived by reading the rules, so
there is no sampling noise for a threshold below 1.00 to absorb — a threshold of, say,
0.90 would not be tolerance for measurement error, it would be advance permission for
three cases to be wrong without anyone having to say which three.
"""

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

from eval.cases import CASES, NOW, Category, EvalCase
from suitability_receipts import (
    BreachOrigin,
    Determination,
    FactorKey,
    RefusalCode,
    SuitabilityOutcome,
    determine,
)

__all__ = [
    "CATEGORY_THRESHOLDS",
    "MAX_OVER_REFUSALS",
    "MAX_UNDER_REFUSALS",
    "CaseResult",
    "CategoryScore",
    "Classification",
    "Report",
    "classify",
    "main",
    "render",
    "run",
    "score",
]

CATEGORY_THRESHOLDS: MappingProxyType[Category, Decimal] = MappingProxyType(
    {
        Category.REFUSAL_CODE: Decimal("1.00"),
        Category.SUPPORTED: Decimal("1.00"),
        Category.BOUNDARY: Decimal("1.00"),
        Category.CONCENTRATION: Decimal("1.00"),
        Category.TENSION: Decimal("1.00"),
    },
)
"""The minimum pass rate each category must reach. See the module docstring for why they
are uniform, and why uniform at 1.00 is not the same as having no threshold."""

MAX_UNDER_REFUSALS = 0
"""Supporting something the set says must refuse falsifies what the README claims about
a `Determination`. There is no number of these that is acceptable."""

MAX_OVER_REFUSALS = 0
"""An engine that refuses everything passes every refusal test ever written. Over-refusal
is capped separately from the category rates so that it cannot be diluted by the fact
that most of the set is refusal cases."""

if set(CATEGORY_THRESHOLDS) != set(Category):
    _uncovered = sorted(set(Category) - set(CATEGORY_THRESHOLDS))
    _message = f"CATEGORY_THRESHOLDS does not cover {_uncovered}"
    raise RuntimeError(_message)


class Classification(StrEnum):
    """What happened to one case. Ordered by how bad it is, least first."""

    PASSED = "passed"
    WRONG_REASON = "wrong_reason"
    OVER_REFUSAL = "over_refusal"
    UNDER_REFUSAL = "under_refusal"


@dataclass(frozen=True)
class CaseResult:
    """One case, the outcome the engine produced for it, and the verdict."""

    case: EvalCase
    outcome: SuitabilityOutcome
    classification: Classification

    @property
    def passed(self) -> bool:
        """Whether the engine agreed with the case in both outcome and reasons."""
        return self.classification is Classification.PASSED

    @property
    def actual_codes(self) -> frozenset[RefusalCode]:
        """The refusal codes the engine reported; empty for a supported determination."""
        if isinstance(self.outcome, Determination):
            return frozenset()
        return frozenset(reason.code for reason in self.outcome.reasons)

    @property
    def actual_missing(self) -> frozenset[FactorKey]:
        """The factors the engine reported as undocumented, across every reason."""
        if isinstance(self.outcome, Determination):
            return frozenset()
        return frozenset(key for reason in self.outcome.reasons for key in reason.missing_factors)

    @property
    def actual_origins(self) -> frozenset[BreachOrigin]:
        """The breach origins the engine stated, across every reason that states one."""
        if isinstance(self.outcome, Determination):
            return frozenset()
        return frozenset(
            reason.breach_origin
            for reason in self.outcome.reasons
            if reason.breach_origin is not None
        )


@dataclass(frozen=True)
class CategoryScore:
    """One category's tally, its rate, and whether it cleared its committed threshold."""

    category: Category
    results: tuple[CaseResult, ...]

    @property
    def total(self) -> int:
        """How many cases the category holds."""
        return len(self.results)

    @property
    def passed(self) -> int:
        """How many of them the engine agreed with entirely."""
        return sum(result.passed for result in self.results)

    def count(self, classification: Classification) -> int:
        """How many results carry `classification`."""
        return sum(result.classification is classification for result in self.results)

    @property
    def rate(self) -> Decimal:
        """The pass rate, exactly. An empty category scores zero rather than dividing."""
        if self.total == 0:  # pragma: no cover - the set is never empty
            return Decimal(0)
        return Decimal(self.passed) / Decimal(self.total)

    @property
    def threshold(self) -> Decimal:
        """The committed minimum for this category."""
        return CATEGORY_THRESHOLDS[self.category]

    @property
    def met(self) -> bool:
        """Whether the category cleared its own threshold."""
        return self.rate >= self.threshold


@dataclass(frozen=True)
class Report:
    """Every result, grouped, with the two absolute caps applied on top of the rates."""

    scores: tuple[CategoryScore, ...]

    @property
    def results(self) -> tuple[CaseResult, ...]:
        """Every result, in case order."""
        return tuple(result for score in self.scores for result in score.results)

    def count(self, classification: Classification) -> int:
        """How many results across the whole set carry `classification`."""
        return sum(score.count(classification) for score in self.scores)

    @property
    def failures(self) -> tuple[CaseResult, ...]:
        """Every result the engine and the set disagreed on."""
        return tuple(result for result in self.results if not result.passed)

    @property
    def passed(self) -> bool:
        """Whether every category cleared its threshold and both caps held."""
        return (
            all(score.met for score in self.scores)
            and self.count(Classification.UNDER_REFUSAL) <= MAX_UNDER_REFUSALS
            and self.count(Classification.OVER_REFUSAL) <= MAX_OVER_REFUSALS
        )


def classify(case: EvalCase, outcome: SuitabilityOutcome) -> Classification:
    """Compare what the engine did against what the case says it should have done.

    A refusal must carry exactly the codes the case names — not a superset, not a
    subset. When the case also names the factors a missing-factor refusal must report,
    those are compared too: four routes to `missing_kyc_factor` are four different
    claims, and a check that only compared codes would collapse them into one.

    `expected_origins` is compared the same way, and for the same reason. A `sell` that
    reduces an inherited breach and a `buy` that creates one both refuse under the same
    code; a set that stopped at the code would score the engine full marks for reporting
    them identically, which is precisely the conflation these cases were written about.
    """
    if case.expected_outcome != outcome.outcome:
        return (
            Classification.OVER_REFUSAL
            if case.expected_outcome == "supported"
            else Classification.UNDER_REFUSAL
        )
    if isinstance(outcome, Determination):
        return Classification.PASSED

    actual_codes = frozenset(reason.code for reason in outcome.reasons)
    if actual_codes != case.expected_codes:
        return Classification.WRONG_REASON
    if case.expected_missing is not None:
        actual_missing = frozenset(
            key for reason in outcome.reasons for key in reason.missing_factors
        )
        if actual_missing != case.expected_missing:
            return Classification.WRONG_REASON
    if case.expected_origins is not None:
        actual_origins = frozenset(
            reason.breach_origin for reason in outcome.reasons if reason.breach_origin is not None
        )
        if actual_origins != case.expected_origins:
            return Classification.WRONG_REASON
    return Classification.PASSED


def run(cases: Sequence[EvalCase] = CASES) -> tuple[CaseResult, ...]:
    """Determine every case and classify the result. The engine is asked, never consulted.

    "Asked, never consulted" is the whole discipline of this file: the expectation was
    written before the answer was known, and this function only records whether the two
    agree.
    """
    results: list[CaseResult] = []
    for case in cases:
        outcome = determine(case.profile, case.recommendation, now=NOW)
        results.append(
            CaseResult(case=case, outcome=outcome, classification=classify(case, outcome)),
        )
    return tuple(results)


def score(results: Sequence[CaseResult]) -> Report:
    """Group results by category, in the order the categories are declared."""
    grouped: dict[Category, list[CaseResult]] = {category: [] for category in Category}
    for result in results:
        grouped[result.case.category].append(result)
    return Report(
        scores=tuple(
            CategoryScore(category=category, results=tuple(grouped[category]))
            for category in Category
            if grouped[category]
        ),
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_RULE = "=" * 92
_THIN = "-" * 92
_HEADER = (
    f"{'category':<16}{'n':>4}{'pass':>6}{'reason':>8}{'over':>6}"
    f"{'under':>7}{'rate':>9}{'min':>8}  verdict"
)


def _percent(value: Decimal) -> str:
    """Render a rate as a percentage to two places."""
    return f"{value * 100:.2f}%"


def _codes(codes: frozenset[RefusalCode]) -> str:
    """Render a code set for the report, in a fixed order."""
    return ", ".join(sorted(code.value for code in codes)) if codes else "(supported)"


def _keys(keys: frozenset[FactorKey]) -> str:
    """Render a factor-key set for the report, in a fixed order."""
    return ", ".join(sorted(key.value for key in keys)) if keys else "(none)"


def _origins(origins: frozenset[BreachOrigin]) -> str:
    """Render a breach-origin set for the report, in a fixed order."""
    return ", ".join(sorted(origin.value for origin in origins)) if origins else "(none)"


def _wrap(text: str, width: int, indent: str) -> list[str]:
    """Wrap `text` to `width`, prefixing every line with `indent`.

    Hand-rolled rather than `textwrap.fill`, so the report's shape is set here rather
    than by a library default that could change under it.
    """
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}" if current else word
        if len(candidate) > width and current:
            lines.append(indent + current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(indent + current)
    return lines


def _headline(report: Report) -> list[str]:
    """The under-refusal banner, printed above the table because it is not a rate."""
    under = report.count(Classification.UNDER_REFUSAL)
    if under == 0:
        return ["UNDER-REFUSAL: none. No case the set says must refuse was supported.", ""]
    lines = [
        f"!! UNDER-REFUSAL: {under} case(s) the set says must refuse were SUPPORTED.",
        "!! A supported receipt asserts that all seven rules ran and none refused.",
        "!! One instance of this falsifies that claim. It is not a percentage point.",
    ]
    lines.extend(
        f"!!   - {result.case.name}"
        for result in report.failures
        if result.classification is Classification.UNDER_REFUSAL
    )
    lines.append("")
    return lines


def _table(report: Report) -> list[str]:
    """The per-category table. One row per category, and no blended total anywhere."""
    lines = [_HEADER, _THIN]
    for score_ in report.scores:
        verdict = "ok" if score_.met else "FAIL"
        lines.append(
            f"{score_.category.value:<16}{score_.total:>4}{score_.passed:>6}"
            f"{score_.count(Classification.WRONG_REASON):>8}"
            f"{score_.count(Classification.OVER_REFUSAL):>6}"
            f"{score_.count(Classification.UNDER_REFUSAL):>7}"
            f"{_percent(score_.rate):>9}{_percent(score_.threshold):>8}  {verdict}",
        )
    lines.append(_THIN)
    lines.append(
        "No total row: a blended number would let a soundness failure be paid for "
        "with easy passes.",
    )
    return lines


def _failure(result: CaseResult) -> list[str]:
    """One failing case, in full: what was expected, what the engine did, and why."""
    case = result.case
    lines = [
        f"[{result.classification.value}] {case.name}  ({case.category.value})",
        f"  the set expects : {case.expected_outcome} — {_codes(case.expected_codes)}",
        f"  the engine did  : {result.outcome.outcome} — {_codes(result.actual_codes)}",
    ]
    if case.expected_missing is not None:
        lines.append(f"  missing expected: {_keys(case.expected_missing)}")
        lines.append(f"  missing reported: {_keys(result.actual_missing)}")
    if case.expected_origins is not None:
        lines.append(f"  origin expected : {_origins(case.expected_origins)}")
        lines.append(f"  origin reported : {_origins(result.actual_origins)}")
    lines.append("  why the set expects that:")
    lines.extend(_wrap(case.why, 86, "    "))
    lines.append("")
    return lines


def render(report: Report) -> str:
    """Render the whole report. Deterministic: same set, same engine, same bytes."""
    lines = [
        _RULE,
        f"SUITABILITY ENGINE EVAL — {len(report.results)} cases",
        _RULE,
        "",
    ]
    lines.extend(_headline(report))
    lines.extend(_table(report))
    lines.append("")
    failures = report.failures
    if failures:
        lines.append(f"DISAGREEMENTS ({len(failures)})")
        lines.append(_THIN)
        for result in failures:
            lines.extend(_failure(result))
    lines.append(_RULE)
    lines.append("RESULT: PASS" if report.passed else "RESULT: FAIL")
    lines.append(_RULE)
    return "\n".join(lines)


def main() -> int:
    """Run the set, print the report, and return an exit code CI can gate on."""
    report = score(run())
    print(render(report))
    return 0 if report.passed else 1


if __name__ == "__main__":  # pragma: no cover - exercised through `python -m eval`
    sys.exit(main())
