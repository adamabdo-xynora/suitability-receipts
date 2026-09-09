"""Guarding the eval set itself.

Nothing here asserts that the engine agrees with the set. That is what
`uv run python -m eval` is for, and wiring its verdict into the suite would make a
disagreement between the two look like a broken test rather than a finding.

What this file does assert is that the set is still the thing it claims to be: large
enough, covering every refusal code, holding cases in both directions, carrying the
specific cases the concentration question needs, and — since two of those cases were
settled by keeping the refusal and splitting the finding — still requiring the two halves
of that split to be told apart. A case set silently losing its supported half is the
failure that would make every other number here meaningless.
"""

from decimal import Decimal

import pytest

from eval import CASES, CATEGORY_THRESHOLDS, Category, EvalCase
from eval.cases import NOW
from eval.scorer import Classification, classify, run, score
from suitability_receipts import (
    BREACH_ORIGIN_CODES,
    BreachOrigin,
    Determination,
    RefusalCode,
    determine,
)
from suitability_receipts.engine import DEFAULT_CONFIG
from suitability_receipts.models import Refusal

MINIMUM_CASES = 25
MAXIMUM_CASES = 35


def test_the_set_is_the_declared_size() -> None:
    """Between 25 and 35 cases: small enough to read, large enough to cover the taxonomy."""
    assert MINIMUM_CASES <= len(CASES) <= MAXIMUM_CASES


def test_every_case_is_named_once() -> None:
    """Names are how a failure is reported, so two cases may not share one."""
    names = [case.name for case in CASES]
    assert len(set(names)) == len(names)


def test_every_refusal_code_is_expected_somewhere() -> None:
    """A code no case expects is a rule the eval set does not test."""
    expected = {code for case in CASES for code in case.expected_codes}
    assert expected == set(RefusalCode)


def test_every_category_holds_cases() -> None:
    """A category with a threshold and no cases would report as passing, vacuously."""
    populated = {case.category for case in CASES}
    assert populated == set(Category) == set(CATEGORY_THRESHOLDS)


def test_the_set_covers_both_directions() -> None:
    """A set of refusal cases alone would score an engine that refuses everything full marks."""
    outcomes = {case.expected_outcome for case in CASES}
    assert outcomes == {"supported", "refused"}


def test_the_supported_half_is_substantial() -> None:
    """The over-refusal guard has to be more than a token case."""
    supported = [case for case in CASES if case.expected_outcome == "supported"]
    assert len(supported) >= len(CASES) // 4


def test_expected_codes_agree_with_the_expected_outcome() -> None:
    """A supported case naming codes, or a refusal naming none, is a malformed expectation."""
    for case in CASES:
        if case.expected_outcome == "supported":
            assert not case.expected_codes, case.name
        else:
            assert case.expected_codes, case.name


def test_missing_factors_are_only_expected_of_missing_factor_refusals() -> None:
    """`expected_missing` is evidence on a `missing_kyc_factor` reason and nowhere else."""
    for case in CASES:
        if case.expected_missing is not None:
            assert RefusalCode.MISSING_KYC_FACTOR in case.expected_codes, case.name


def test_an_origin_is_expected_of_exactly_the_cases_whose_codes_state_one() -> None:
    """A case naming a portfolio-reading code must say whose breach it expects.

    Both halves. A case expecting `concentration_breach` without naming an origin would
    score an engine that reports a disposal and a purchase identically as correct, which
    is the conflation this set exists to catch. A case naming an origin for a code that
    cannot carry one is an expectation the engine can never meet or fail.
    """
    for case in CASES:
        states_origin = bool(case.expected_codes & BREACH_ORIGIN_CODES)
        assert (case.expected_origins is not None) is states_origin, case.name


def test_the_set_pins_both_sides_of_the_created_inherited_distinction() -> None:
    """`created` and `reduced` are each expected somewhere, or the finding is untested.

    `increased` is not required here. The eval set is capped at 35 cases and every case
    in it earns its place by standing for a situation an advisor is in; the fourth origin
    is covered exhaustively in `tests/test_engine.py` instead, where adding a case costs
    nothing that has to be traded against another.
    """
    expected = {
        origin for case in CASES if case.expected_origins for origin in case.expected_origins
    }
    assert BreachOrigin.CREATED in expected
    assert BreachOrigin.REDUCED in expected
    assert BreachOrigin.UNCHANGED in expected


def test_several_cases_trip_more_than_one_code() -> None:
    """Refusals carry every reason; a set of single-code cases would never check that."""
    assert sum(len(case.expected_codes) > 1 for case in CASES) >= 2


def test_every_case_carries_a_written_reason() -> None:
    """The `why` is the case's actual content: without it the expectation is an assertion."""
    for case in CASES:
        assert len(case.why.split()) >= 25, case.name


def test_every_profile_is_obviously_synthetic() -> None:
    """No fixture may look like it could be a real person."""
    for case in CASES:
        assert case.profile.client_id.startswith("SYNTHETIC-"), case.name
        assert case.recommendation.product.instrument_id.startswith("SYNTHETIC-"), case.name
        for holding in case.profile.holdings:
            assert holding.instrument_id.startswith("SYNTHETIC-"), case.name
            assert "Synthetic Placeholder" in holding.name, case.name


def test_the_concentration_question_is_actually_asked() -> None:
    """The three cases the open question needs, by name, so none can quietly vanish."""
    names = {case.name for case in CASES if case.category is Category.CONCENTRATION}
    assert "a sell that reduces an over-concentration without curing it" in names
    assert "a sell that cures an over-concentration" in names
    assert "a buy that creates an over-concentration" in names


def test_the_question_the_concentration_cases_answered_stays_answered() -> None:
    """The reducing sell and the buy that creates share a code and must not share a finding.

    Named by hand rather than derived, because the value of these two is entirely in
    their being a pair: either alone is satisfied by an engine that labels every breach
    the same way.
    """
    by_name = {case.name: case for case in CASES}
    reduced = by_name["a sell that reduces an over-concentration without curing it"]
    created = by_name["a buy that creates an over-concentration"]
    assert reduced.expected_outcome == created.expected_outcome == "refused"
    assert reduced.expected_codes == created.expected_codes
    assert reduced.expected_origins == frozenset({BreachOrigin.REDUCED})
    assert created.expected_origins == frozenset({BreachOrigin.CREATED})


def test_the_boundary_cases_sit_on_the_configured_constants() -> None:
    """Each configured constant gets a case at the limit and one on either side."""
    boundary = [case for case in CASES if case.category is Category.BOUNDARY]
    assert len(boundary) >= 9
    assert DEFAULT_CONFIG.concentration_limit_fraction == Decimal("0.20")
    assert DEFAULT_CONFIG.profile_review_interval_days == 365


def test_every_case_determines_without_raising() -> None:
    """No case may be a caller error: a `ValueError` is not a suitability answer."""
    for case in CASES:
        outcome = determine(case.profile, case.recommendation, now=NOW)
        assert outcome.outcome in {"supported", "refused"}, case.name


@pytest.mark.parametrize(
    ("expected_outcome", "actual_outcome", "classification"),
    [
        ("supported", "refused", Classification.OVER_REFUSAL),
        ("refused", "supported", Classification.UNDER_REFUSAL),
    ],
)
def test_classify_names_the_direction_of_a_disagreement(
    expected_outcome: str,
    actual_outcome: str,
    classification: Classification,
) -> None:
    """Over- and under-refusal are different failures and must not be reported alike."""
    case = next(case for case in CASES if case.expected_outcome == expected_outcome)
    other = next(result.outcome for result in run() if result.outcome.outcome == actual_outcome)
    assert classify(case, other) is classification


def test_classify_separates_a_wrong_reason_from_a_wrong_outcome() -> None:
    """Refusing for the wrong code is its own verdict, not a pass and not an over-refusal."""
    refusal = next(result.outcome for result in run() if isinstance(result.outcome, Refusal))
    mislabelled = EvalCase(
        name="SYNTHETIC probe",
        category=Category.REFUSAL_CODE,
        profile=CASES[0].profile,
        recommendation=CASES[0].recommendation,
        expected_outcome="refused",
        expected_codes=frozenset({RefusalCode.HORIZON_MISMATCH, RefusalCode.STALE_PROFILE}),
        why="A probe that names codes the engine did not report, to exercise the scorer.",
    )
    assert classify(mislabelled, refusal) is Classification.WRONG_REASON


def test_a_case_that_agrees_with_the_engine_passes() -> None:
    """The scorer must be able to say yes, or every result it produces is meaningless."""
    supported = next(result for result in run() if isinstance(result.outcome, Determination))
    assert classify(supported.case, supported.outcome) is Classification.PASSED


def test_the_report_groups_every_case_exactly_once() -> None:
    """Scoring must not drop or duplicate a case on its way into a category."""
    report = score(run())
    assert len(report.results) == len(CASES)
    assert {result.case.name for result in report.results} == {case.name for case in CASES}
