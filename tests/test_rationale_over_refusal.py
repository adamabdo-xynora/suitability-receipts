"""The over-refusal regression check.

A verifier that refuses everything is trivially safe and completely useless. Every
rationale in this file is correct: its prose says only what the profile documents, its
declarations match the profile exactly, and its numbers come from the material it
declared. Every one of them MUST pass verification.

This file exists from the first commit of the verifier, not after the first complaint
about it. The refusals in `test_llm_verification.py` prove the verifier can say no; this
file is what stops the next change from making "no" the only thing it can say. When a
check is tightened, a case here failing is the signal that the tightening cost more than
it bought — and the fix is to reconsider the check, not to delete the case.

The set deliberately spans both outcomes. A refusal is the harder half to explain
without either softening it or asserting something undocumented, and it is the half
where an over-strict verifier would do the most damage: the explanation an advisor most
needs is the one for a recommendation that was refused.
"""

import datetime as dt
from dataclasses import dataclass

import pytest

from suitability_receipts import (
    ClientProfile,
    Determination,
    FactorCitation,
    FactorKey,
    Recommendation,
    RiskLevel,
    SuitabilityOutcome,
    determine,
)
from suitability_receipts.llm import (
    DraftedRationale,
    UnsupportedRationale,
    VerifiedRationale,
    verify_rationale,
)
from synthetic import NOW, REVIEWED_ON, a_product, a_profile, a_recommendation

STALE_REVIEW = dt.date(2023, 1, 1)


@dataclass(frozen=True)
class SupportedCase:
    """A rationale that must survive verification, with the world it is about."""

    name: str
    profile: ClientProfile
    recommendation: Recommendation
    prose: str
    cited: tuple[FactorCitation, ...]
    absent: tuple[FactorKey, ...] = ()

    @property
    def outcome(self) -> SuitabilityOutcome:
        """The determination the engine really makes for this case."""
        return determine(self.profile, self.recommendation, now=NOW)

    @property
    def drafted(self) -> DraftedRationale:
        """The rationale as a well-behaved drafter would have produced it."""
        return DraftedRationale(
            prose=self.prose,
            cited_factors=self.cited,
            absent_factors=self.absent,
            drafted_by="SYNTHETIC-drafter",
        )


def cite(key: FactorKey, value: str, documented_on: dt.date = REVIEWED_ON) -> FactorCitation:
    """Build a citation against a synthetic profile."""
    return FactorCitation(key=key, value=value, documented_on=documented_on)


_NO_TOLERANCE = a_profile(risk_tolerance=None)
_STALE = a_profile(last_reviewed=STALE_REVIEW)
_HIGH_RISK_FUND = a_recommendation(
    product=a_product(
        risk_rating=RiskLevel.HIGH,
        risk_rating_source="SYNTHETIC fund facts sheet",
    ),
)

CASES: tuple[SupportedCase, ...] = (
    SupportedCase(
        name="supported, one citation, no numbers",
        profile=a_profile(),
        recommendation=a_recommendation(),
        prose=(
            "The recommendation is supported. The client's documented risk tolerance is "
            "medium, and the fund is rated medium risk, so the product sits at the "
            "tolerance the profile records rather than above it."
        ),
        cited=(cite(FactorKey.RISK_TOLERANCE, "medium"),),
    ),
    SupportedCase(
        name="supported, several citations, amounts and a horizon",
        profile=a_profile(),
        recommendation=a_recommendation(),
        prose=(
            "The recommendation is supported. The profile documents a medium risk "
            "tolerance and a time horizon of 10 years, and the purchase of 10,000.00 CAD "
            "is in a daily-redeemable fund with no lock-up, so nothing in the product's "
            "terms outlasts the horizon on file."
        ),
        cited=(
            cite(FactorKey.RISK_TOLERANCE, "medium"),
            cite(FactorKey.TIME_HORIZON_YEARS, "10"),
        ),
    ),
    SupportedCase(
        name="supported, liquidity figures quoted from the documented requirement",
        profile=a_profile(),
        recommendation=a_recommendation(),
        prose=(
            "The recommendation is supported. The documented liquidity requirement is an "
            "emergency reserve of 20000.00 CAD together with 1 upcoming expense totalling "
            "5000.00 CAD, and documented liquid net worth is 120000.00 CAD, which leaves "
            "the requirement covered after the purchase."
        ),
        cited=(
            cite(
                FactorKey.LIQUIDITY_REQUIREMENT,
                "emergency reserve 20000.00 CAD; upcoming expenses: 1 totalling 5000.00 CAD",
            ),
            cite(FactorKey.LIQUID_NET_WORTH, "120000.00 CAD"),
        ),
    ),
    SupportedCase(
        name="supported, portfolio figures and a count written in words",
        profile=a_profile(),
        recommendation=a_recommendation(),
        prose=(
            "The recommendation is supported: all seven rules were evaluated and none "
            "refused. The documented portfolio is 6 holdings totalling 90000.00 CAD, and "
            "the purchase does not take any one instrument, issuer, or sector past the "
            "configured concentration limit."
        ),
        cited=(cite(FactorKey.HOLDINGS, "6 holdings totalling 90000.00 CAD"),),
    ),
    SupportedCase(
        name="supported, the documentation date quoted in the prose",
        profile=a_profile(),
        recommendation=a_recommendation(),
        prose=(
            "The recommendation is supported on the profile documented 2025-06-01. The "
            "documented objective is growth, and the fund's medium rating sits within "
            "what that objective supports."
        ),
        cited=(
            cite(FactorKey.LAST_REVIEWED, "2025-06-01"),
            cite(FactorKey.INVESTMENT_OBJECTIVES, "growth"),
        ),
    ),
    SupportedCase(
        name="refused for a missing factor, explained through a declared absence",
        profile=_NO_TOLERANCE,
        recommendation=a_recommendation(),
        prose=(
            "The recommendation was refused. The profile documents no risk tolerance, so "
            "there is nothing to compare the fund's rating against and the recommendation "
            "cannot be supported. The documented time horizon of 10 years does not stand "
            "in for a tolerance. Documenting the client's risk tolerance is what this "
            "refusal is waiting on."
        ),
        cited=(cite(FactorKey.TIME_HORIZON_YEARS, "10"),),
        absent=(FactorKey.RISK_TOLERANCE,),
    ),
    SupportedCase(
        name="refused for a risk mismatch, stated without softening",
        profile=a_profile(),
        recommendation=_HIGH_RISK_FUND,
        prose=(
            "The recommendation was refused. The fund is rated high risk, and the profile "
            "documents a medium risk tolerance and an objective of growth. The product is "
            "above what the profile supports, so this is not a recommendation that can be "
            "made on the profile as documented."
        ),
        cited=(
            cite(FactorKey.RISK_TOLERANCE, "medium"),
            cite(FactorKey.INVESTMENT_OBJECTIVES, "growth"),
        ),
    ),
    SupportedCase(
        name="refused for a stale profile, quoting the interval from the refusal",
        profile=_STALE,
        recommendation=a_recommendation(),
        prose=(
            "The recommendation was refused. The profile was last reviewed 2023-01-01, "
            "which is further back than the configured review interval of 365 days, so the "
            "documented facts are too old to determine on. A review is what this refusal "
            "is waiting on."
        ),
        cited=(cite(FactorKey.LAST_REVIEWED, "2023-01-01", STALE_REVIEW),),
    ),
)


def case_ids() -> list[str]:
    """Name each case in the test report, so a failure says which one broke."""
    return [case.name for case in CASES]


@pytest.mark.parametrize("case", CASES, ids=case_ids())
def test_a_correct_rationale_survives_verification(case: SupportedCase) -> None:
    """Every case here is correct and must pass. A failure is an over-refusal."""
    verdict = verify_rationale(
        case.profile,
        case.drafted,
        outcome=case.outcome,
        recommendation=case.recommendation,
    )
    if isinstance(verdict, UnsupportedRationale):
        pytest.fail(f"over-refused {case.name!r}: {verdict.detail}")
    assert verdict.rationale.text == case.prose
    assert verdict.rationale.cited_factors == case.cited
    assert verdict.absent_factors == case.absent


def test_the_set_covers_both_outcomes() -> None:
    """Guard the guard: a set of supported-only cases would miss the harder half."""
    outcomes = {case.outcome.outcome for case in CASES}
    assert outcomes == {"supported", "refused"}


def test_the_set_exercises_declared_absences() -> None:
    """An absence is the only way to explain a missing-factor refusal verifiably."""
    assert any(case.absent for case in CASES)


def test_the_set_exercises_numbers_in_the_prose() -> None:
    """Without a digit anywhere, the grounding check could refuse everything unnoticed."""
    assert sum(any(character.isdigit() for character in case.prose) for case in CASES) >= 4


@pytest.mark.parametrize("case", CASES, ids=case_ids())
def test_a_verified_rationale_is_accepted_by_the_engine_too(case: SupportedCase) -> None:
    """The verifier's yes must be the engine's yes: attach it and re-determine."""
    verdict = verify_rationale(
        case.profile,
        case.drafted,
        outcome=case.outcome,
        recommendation=case.recommendation,
    )
    assert isinstance(verdict, VerifiedRationale)
    recommendation = case.recommendation.model_copy(update={"rationale": verdict.rationale})
    outcome = determine(case.profile, recommendation, now=NOW)
    if isinstance(outcome, Determination):
        assert isinstance(case.outcome, Determination)
    else:
        assert "unsupported_rationale" not in {reason.code.value for reason in outcome.reasons}
