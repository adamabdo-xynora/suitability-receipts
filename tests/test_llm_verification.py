"""Tests for the rationale verifier.

Every declaration a rationale makes is checked against the documented profile, and a
single failure refuses the whole rationale. These tests cover the failures. The
rationales that must *pass* live in `test_rationale_over_refusal.py`, deliberately in a
file of their own: a verifier that refuses everything is trivially safe and useless, and
that has to be as visible as the refusals are.
"""

import datetime as dt

import pytest

from suitability_receipts import (
    ClientProfile,
    FactorCitation,
    FactorKey,
    Recommendation,
    RefusalCode,
    RuleCheck,
    SuitabilityOutcome,
)
from suitability_receipts.engine import RuleInput, check_unsupported_rationale
from suitability_receipts.llm import (
    DraftedRationale,
    RationaleVerdict,
    UnsupportedRationale,
    VerifiedRationale,
    verify_rationale,
)
from synthetic import (
    NOW,
    REVIEWED_ON,
    a_profile,
    a_recommendation,
    an_outcome,
)

PROFILE = a_profile()
RECOMMENDATION = a_recommendation()
OUTCOME = an_outcome()


def cite(key: FactorKey, value: str, documented_on: dt.date = REVIEWED_ON) -> FactorCitation:
    """Build a citation as the drafter would have stamped it."""
    return FactorCitation(key=key, value=value, documented_on=documented_on)


def drafted(
    prose: str,
    *cited: FactorCitation,
    absent: tuple[FactorKey, ...] = (),
) -> DraftedRationale:
    """Build a drafted rationale without calling a model."""
    return DraftedRationale(
        prose=prose,
        cited_factors=cited,
        absent_factors=absent,
        drafted_by="SYNTHETIC-drafter",
    )


def verify(
    draft: DraftedRationale,
    profile: ClientProfile = PROFILE,
    outcome: SuitabilityOutcome = OUTCOME,
    recommendation: Recommendation = RECOMMENDATION,
) -> RationaleVerdict:
    """Verify a draft against the synthetic profile."""
    return verify_rationale(profile, draft, outcome=outcome, recommendation=recommendation)


# ---------------------------------------------------------------------------
# Declarations that do not hold
# ---------------------------------------------------------------------------


def test_citing_a_factor_the_profile_does_not_document() -> None:
    """The required case: a key with nothing behind it in the profile."""
    profile = a_profile(investment_knowledge=None)
    verdict = verify(
        drafted(
            "The client's good investment knowledge supports the purchase.",
            cite(FactorKey.INVESTMENT_KNOWLEDGE, "good"),
        ),
        profile=profile,
        outcome=an_outcome(profile),
    )
    assert isinstance(verdict, UnsupportedRationale)
    assert verdict.code is RefusalCode.UNSUPPORTED_RATIONALE
    assert [c.key for c in verdict.unsupported_citations] == [FactorKey.INVESTMENT_KNOWLEDGE]
    assert "does not document at all" in verdict.detail


def test_citing_a_documented_key_with_the_wrong_value() -> None:
    """The required case: the key is documented, the value is not what is documented."""
    verdict = verify(
        drafted(
            "The client's documented risk tolerance is high.",
            cite(FactorKey.RISK_TOLERANCE, "high"),
        ),
    )
    assert isinstance(verdict, UnsupportedRationale)
    assert [c.value for c in verdict.unsupported_citations] == ["high"]
    assert "but the profile documents 'medium'" in verdict.detail


def test_a_value_that_nearly_matches_is_still_wrong() -> None:
    """The verifier does not round a claim toward the documented value."""
    verdict = verify(
        drafted(
            "The documented time horizon is about ten years.",
            cite(FactorKey.TIME_HORIZON_YEARS, "10.5"),
        ),
    )
    assert isinstance(verdict, UnsupportedRationale)


def test_a_citation_dated_to_another_review_is_unsupported() -> None:
    """A claim dated to a different documentation event is not the documented fact."""
    verdict = verify(
        drafted(
            "The documented risk tolerance is medium.",
            cite(FactorKey.RISK_TOLERANCE, "medium", dt.date(2024, 6, 1)),
        ),
    )
    assert isinstance(verdict, UnsupportedRationale)


def test_declaring_a_documented_factor_absent() -> None:
    """Absences are checked the other way round, and can be wrong the other way round."""
    verdict = verify(
        drafted(
            "The profile documents no risk tolerance.",
            cite(FactorKey.LAST_REVIEWED, REVIEWED_ON.isoformat()),
            absent=(FactorKey.RISK_TOLERANCE,),
        ),
    )
    assert isinstance(verdict, UnsupportedRationale)
    assert verdict.contradicted_absences == (FactorKey.RISK_TOLERANCE,)


def test_declaring_nothing_at_all() -> None:
    """A rationale anchored to nothing is not a rationale this system will carry."""
    verdict = verify(drafted("The recommendation looks fine to me."))
    assert isinstance(verdict, UnsupportedRationale)
    assert "declares no factor" in verdict.detail


def test_declaring_the_same_factor_twice() -> None:
    """Two claims about one factor is a malformed declaration, not a stronger one."""
    verdict = verify(
        drafted(
            "The documented risk tolerance is medium.",
            cite(FactorKey.RISK_TOLERANCE, "medium"),
            cite(FactorKey.RISK_TOLERANCE, "high"),
        ),
    )
    assert isinstance(verdict, UnsupportedRationale)
    assert verdict.duplicate_declarations == (FactorKey.RISK_TOLERANCE,)


def test_empty_prose_is_refused() -> None:
    """A correct bibliography attached to nothing explains nothing."""
    verdict = verify(drafted("   ", cite(FactorKey.RISK_TOLERANCE, "medium")))
    assert isinstance(verdict, UnsupportedRationale)
    assert "empty" in verdict.detail


def test_every_failure_is_reported_not_only_the_first() -> None:
    """Naming one of several problems would understate what is wrong."""
    verdict = verify(
        drafted(
            "The client is a high-risk investor with 40 years of experience.",
            cite(FactorKey.RISK_TOLERANCE, "high"),
            absent=(FactorKey.NET_WORTH,),
        ),
    )
    assert isinstance(verdict, UnsupportedRationale)
    assert verdict.unsupported_citations
    assert verdict.contradicted_absences
    assert verdict.ungrounded_numbers == ("40",)


# ---------------------------------------------------------------------------
# Numbers in the prose
# ---------------------------------------------------------------------------


def test_a_fabricated_number_is_caught_even_when_every_citation_is_correct() -> None:
    """The one check that reads the prose rather than the declarations."""
    verdict = verify(
        drafted(
            "The documented time horizon is 10 years and liquid net worth is 999999.00 CAD.",
            cite(FactorKey.TIME_HORIZON_YEARS, "10"),
        ),
    )
    assert isinstance(verdict, UnsupportedRationale)
    assert verdict.ungrounded_numbers == ("999999",)


def test_a_number_from_an_undeclared_factor_is_ungrounded() -> None:
    """A figure no check cited and the rationale did not declare has nothing behind it."""
    verdict = verify(
        drafted(
            "Net worth is 500000.00 CAD.",
            cite(FactorKey.TIME_HORIZON_YEARS, "10"),
        ),
    )
    assert isinstance(verdict, UnsupportedRationale)
    assert verdict.ungrounded_numbers == ("500000",)


def test_grouping_separators_do_not_make_a_number_ungrounded() -> None:
    """A grouped number is the same number; the check is not a formatting test."""
    verdict = verify(
        drafted(
            "The $10,000 purchase leaves the 10-year horizon intact.",
            cite(FactorKey.TIME_HORIZON_YEARS, "10"),
        ),
    )
    assert isinstance(verdict, VerifiedRationale)


def test_a_wrong_number_cannot_ground_itself() -> None:
    """The grounding corpus uses documented values, never the values the model claimed."""
    verdict = verify(
        drafted(
            "The documented time horizon is 40 years.",
            cite(FactorKey.TIME_HORIZON_YEARS, "40"),
        ),
    )
    assert isinstance(verdict, UnsupportedRationale)
    assert verdict.ungrounded_numbers == ("40",)


# ---------------------------------------------------------------------------
# Agreement with the engine
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "value"),
    [
        (FactorKey.RISK_TOLERANCE, "medium"),
        (FactorKey.RISK_TOLERANCE, "high"),
        (FactorKey.TIME_HORIZON_YEARS, "10"),
        (FactorKey.TIME_HORIZON_YEARS, "9"),
        (FactorKey.INVESTMENT_OBJECTIVES, "growth"),
        (FactorKey.HOLDINGS, "6 holdings totalling 90000.00 CAD"),
        (FactorKey.NET_WORTH, "500000.00 CAD"),
        (FactorKey.NET_WORTH, "500000 CAD"),
    ],
)
def test_the_verifier_and_the_engine_agree_on_what_supported_means(
    key: FactorKey,
    value: str,
) -> None:
    """Two definitions of "supported" would eventually disagree, so there is only one."""
    citation = cite(key, value)
    recommendation = a_recommendation(
        rationale=RECOMMENDATION.rationale.model_copy(update={"cited_factors": (citation,)}),
    )
    engine_says = check_unsupported_rationale(
        RuleInput(profile=PROFILE, recommendation=recommendation, as_of=NOW.date()),
    )
    verdict = verify(drafted("A rationale.", citation), recommendation=recommendation)
    assert isinstance(engine_says, RuleCheck) == isinstance(verdict, VerifiedRationale)


# ---------------------------------------------------------------------------
# The verifier never edits
# ---------------------------------------------------------------------------


def test_a_verified_rationale_is_the_one_that_was_drafted() -> None:
    """No path drops the bad citation and keeps the sentence."""
    citations = (
        cite(FactorKey.RISK_TOLERANCE, "medium"),
        cite(FactorKey.TIME_HORIZON_YEARS, "10"),
    )
    prose = "Medium risk tolerance and a 10-year horizon support a medium-risk fund."
    verdict = verify(drafted(prose, *citations))
    assert isinstance(verdict, VerifiedRationale)
    assert verdict.rationale.text == prose
    assert verdict.rationale.cited_factors == citations
    assert verdict.drafted_by == "SYNTHETIC-drafter"


def test_a_refused_rationale_produces_no_rationale_at_all() -> None:
    """There is no partially-verified result to attach to a recommendation."""
    verdict = verify(
        drafted(
            "Risk tolerance is high.",
            cite(FactorKey.RISK_TOLERANCE, "high"),
            cite(FactorKey.TIME_HORIZON_YEARS, "10"),
        ),
    )
    assert isinstance(verdict, UnsupportedRationale)
    assert not hasattr(verdict, "rationale")


def test_the_refusal_is_not_a_receipt() -> None:
    """Receipts are the engine's to issue; the verifier reports, the engine refuses."""
    verdict = verify(drafted("Nothing declared."))
    assert isinstance(verdict, UnsupportedRationale)
    assert verdict.code is RefusalCode.UNSUPPORTED_RATIONALE
    assert not hasattr(verdict, "receipt_id")
    assert not hasattr(verdict, "reasons")
