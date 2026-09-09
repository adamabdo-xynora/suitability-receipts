"""The case set: 35 profiles and recommendations, each with an expectation and a reason.

Every expectation here was derived by reading the inputs against the rules as
`engine.py` documents them, and writing down what those rules say. None was produced by
running the engine. Where the two disagree, the disagreement is the finding — see the
`why` on each case and the report the scorer prints.

Two cases here no longer state a disagreement; they state a resolution. Both expected
`supported` for a `sell` that reduced an exposure the rules were objecting to, the engine
refused both, and the argument was settled against the expectation on the outcome and in
its favour on the finding: the refusals stand, and the receipt now says whether the breach
was created by the recommendation or inherited from the portfolio and reduced by it. The
`why` on each keeps the case it originally made, so a reader can see what was traded away
and what was bought. A revised expectation is worth more when the revision is legible than
when it is rewritten to look as though it was always right.

All client data is synthetic and obviously so, following the convention
`tests/synthetic.py` established: identifiers carry the reserved `SYNTHETIC-` prefix and
every name is a visible placeholder. Nothing here resembles a real person, and no
credential appears anywhere in this package.

The five categories
-------------------
`refusal_code`
    One case per refusal code, plus cases that trip several at once. These pin *which*
    code fires, not merely that something did: a refusal for the right outcome and the
    wrong reason is a different failure and the scorer reports it separately.
`supported`
    Cases that must not refuse. This is the over-refusal guard at the eval level, and it
    is the half a case set most often skips: a determination engine that refuses
    everything passes every refusal test ever written.
`boundary`
    Exactly at each configured constant, and one unit either side. A threshold nobody
    probed at its edge is a threshold nobody has actually tested.
`concentration`
    The question left open when the engine was built: what a `sell` against an
    over-concentrated position should produce. Each case states a position, and together
    they pin the answer that was reached — a disposal that leaves a breach in place still
    refuses, but the reason says `reduced` where a purchase's would say `created`, and
    dilution by a larger denominator counts as neither.
`tension`
    Profiles that disagree with themselves — documented tolerance against stated
    objective. Includes a case where the tension exists and must *not* refuse, because
    "the profile is in tension, therefore refuse" would be its own over-refusal.
"""

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from suitability_receipts import (
    BreachOrigin,
    ClientProfile,
    Currency,
    FactorCitation,
    FactorKey,
    Holding,
    InvestmentKnowledge,
    InvestmentObjective,
    LiquidityRequirement,
    Money,
    Product,
    Recommendation,
    RecommendationRationale,
    RedemptionFrequency,
    RefusalCode,
    RiskLevel,
    TradeAction,
    UpcomingExpense,
)

__all__ = [
    "CASES",
    "NOW",
    "REVIEWED_ON",
    "Category",
    "EvalCase",
]

REVIEWED_ON = dt.date(2025, 6, 1)
"""The documentation date of a profile reviewed recently enough to determine on."""

NOW = dt.datetime(2025, 7, 1, 12, 0, tzinfo=dt.UTC)
"""The injected evaluation time. Fixed for every case: the engine reads no clock, and a
case set that moved with the wall clock would produce a different verdict each day."""

CLIENT_ID = "SYNTHETIC-EVAL-0001"

# Ages measured back from NOW, for the review-interval boundary.
_INSIDE_INTERVAL = dt.date(2024, 7, 2)
"""364 days before NOW: one day inside the configured interval."""
_AT_INTERVAL = dt.date(2024, 7, 1)
"""Exactly 365 days before NOW: the interval itself."""
_PAST_INTERVAL = dt.date(2024, 6, 30)
"""366 days before NOW: one day past the configured interval."""
_LONG_STALE = dt.date(2024, 1, 1)
"""547 days before NOW: stale by any reading of the interval."""


class Category(StrEnum):
    """What a case is evidence about. Scored separately; see `scorer.CATEGORY_THRESHOLDS`."""

    REFUSAL_CODE = "refusal_code"
    SUPPORTED = "supported"
    BOUNDARY = "boundary"
    CONCENTRATION = "concentration"
    TENSION = "tension"


@dataclass(frozen=True)
class EvalCase:
    """One case: the world, the proposal, the expected answer, and why it is expected.

    `expected_codes` is empty exactly when `expected_outcome` is `"supported"`. It is
    the *whole* set of codes the refusal must carry, not a subset: a refusal that fires
    for one right reason and one wrong one has not agreed with the case.

    `expected_missing` is asserted only when supplied. It names the profile factors a
    `missing_kyc_factor` refusal must report, which is what separates four different
    routes to the same code from four copies of one test.

    `expected_origins` is asserted the same way, and names the `BreachOrigin` values the
    refusal must state across its reasons. Without it, a case that expects
    `concentration_breach` cannot tell a `sell` that halves an inherited breach from a
    `buy` that creates one — they carry the same code, and the whole point of these two
    cases is that they are not the same finding.
    """

    name: str
    category: Category
    profile: ClientProfile
    recommendation: Recommendation
    expected_outcome: Literal["supported", "refused"]
    why: str
    expected_codes: frozenset[RefusalCode] = frozenset()
    expected_missing: frozenset[FactorKey] | None = None
    expected_origins: frozenset[BreachOrigin] | None = None


# ---------------------------------------------------------------------------
# Synthetic builders
# ---------------------------------------------------------------------------


def cad(amount: str) -> Money:
    """Build a synthetic CAD amount."""
    return Money(amount=Decimal(amount), currency=Currency.CAD)


def position(letter: str, value: str) -> Holding:
    """Build one synthetic position, in an instrument, issuer, and sector of its own."""
    return Holding(
        instrument_id=f"SYNTHETIC-FUND-{letter}",
        name=f"Synthetic Placeholder Fund {letter}",
        issuer=f"Synthetic Placeholder Issuer {letter}",
        sector=f"synthetic-sector-{letter.lower()}",
        market_value=cad(value),
    )


def diversified_positions() -> tuple[Holding, ...]:
    """Six equal positions of 15,000: a 90,000 portfolio, no bucket above 16.67%."""
    return tuple(position(letter, "15000.00") for letter in "ABCDEF")


def concentrated_positions() -> tuple[Holding, ...]:
    """40,000 in FUND-A beside six 10,000 positions: 40% of a 100,000 portfolio in one name.

    FUND-A's issuer and sector are its alone, so the same 40% breaches all three
    dimensions. That is deliberate: a real single-name concentration is a concentration
    of issuer and sector too, and a case that breached in only one dimension would be
    quieter than the situation it stands for.
    """
    return (
        position("A", "40000.00"),
        *(position(letter, "10000.00") for letter in "BCDEFG"),
    )


def a_profile(**overrides: object) -> ClientProfile:
    """Build a fully documented synthetic profile: medium tolerance, growth, ten years."""
    fields: dict[str, object] = {
        "client_id": CLIENT_ID,
        "last_reviewed": REVIEWED_ON,
        "currency": Currency.CAD,
        "risk_tolerance": RiskLevel.MEDIUM,
        "time_horizon_years": Decimal(10),
        "investment_objectives": (InvestmentObjective.GROWTH,),
        "investment_knowledge": InvestmentKnowledge.GOOD,
        "liquidity_requirement": LiquidityRequirement(
            emergency_reserve=cad("20000.00"),
            upcoming_expenses=(
                UpcomingExpense(
                    description="SYNTHETIC placeholder expense",
                    amount=cad("5000.00"),
                    expected_on=dt.date(2026, 1, 15),
                ),
            ),
        ),
        "annual_income": cad("90000.00"),
        "net_worth": cad("500000.00"),
        "liquid_net_worth": cad("200000.00"),
        "holdings": diversified_positions(),
    }
    fields.update(overrides)
    return ClientProfile.model_validate(fields)


def a_product(**overrides: object) -> Product:
    """Build a synthetic product: medium risk, daily redemption, no lock-up."""
    fields: dict[str, object] = {
        "instrument_id": "SYNTHETIC-FUND-Z",
        "name": "Synthetic Placeholder Fund Z",
        "issuer": "Synthetic Placeholder Issuer Z",
        "sector": "synthetic-sector-z",
        "risk_rating": RiskLevel.MEDIUM,
        "risk_rating_source": "SYNTHETIC fund facts sheet",
        "redemption_frequency": RedemptionFrequency.DAILY,
    }
    fields.update(overrides)
    return Product.model_validate(fields)


def held_product(letter: str, **overrides: object) -> Product:
    """Build the product record for a position the synthetic portfolio already holds."""
    return a_product(
        instrument_id=f"SYNTHETIC-FUND-{letter}",
        name=f"Synthetic Placeholder Fund {letter}",
        issuer=f"Synthetic Placeholder Issuer {letter}",
        sector=f"synthetic-sector-{letter.lower()}",
        **overrides,
    )


def cite(key: FactorKey, value: str, documented_on: dt.date = REVIEWED_ON) -> FactorCitation:
    """Claim that the profile documents `value` for `key`, as of `documented_on`.

    Values are written out by hand rather than read back from the profile through the
    engine's own renderer. A citation built from the engine cannot fail against the
    engine, which would quietly retire the `unsupported_rationale` cases.
    """
    return FactorCitation(key=key, value=value, documented_on=documented_on)


def a_rationale(
    *claims: FactorCitation, text: str = "SYNTHETIC placeholder rationale."
) -> RecommendationRationale:
    """Build the stated reasoning for a recommendation, with the claims it makes."""
    return RecommendationRationale(text=text, cited_factors=claims)


DEFAULT_CLAIMS = (
    cite(FactorKey.RISK_TOLERANCE, "medium"),
    cite(FactorKey.TIME_HORIZON_YEARS, "10"),
)
"""What the default profile documents, cited correctly. Cases that are not about the
rationale use these, so that `unsupported_rationale` stays silent and does not add a
code the case did not intend to test."""


def a_recommendation(**overrides: object) -> Recommendation:
    """Build a synthetic recommendation: buy 10,000 of a medium-risk fund."""
    fields: dict[str, object] = {
        "recommendation_id": "SYNTHETIC-EVAL-REC-0001",
        "client_id": CLIENT_ID,
        "action": TradeAction.BUY,
        "product": a_product(),
        "amount": cad("10000.00"),
        "proposed_on": REVIEWED_ON,
        "rationale": a_rationale(*DEFAULT_CLAIMS),
    }
    fields.update(overrides)
    return Recommendation.model_validate(fields)


def _dated_claims(documented_on: dt.date) -> tuple[FactorCitation, ...]:
    """The default claims, stamped to a profile documented on another date."""
    return (
        cite(FactorKey.RISK_TOLERANCE, "medium", documented_on),
        cite(FactorKey.TIME_HORIZON_YEARS, "10", documented_on),
    )


# ---------------------------------------------------------------------------
# The cases
#
# Ordered by category. Each `why` is the reading that produced the expectation: the
# facts the profile documents, the rule that applies to them, and the answer that rule
# gives. Where the expectation is a position rather than a reading — the concentration
# cases, and the sell in `supported` — the `why` says so in as many words.
# ---------------------------------------------------------------------------

_REFUSAL_CODE_CASES: tuple[EvalCase, ...] = (
    EvalCase(
        name="risk tolerance is not documented",
        category=Category.REFUSAL_CODE,
        profile=a_profile(risk_tolerance=None),
        recommendation=a_recommendation(
            rationale=a_rationale(cite(FactorKey.TIME_HORIZON_YEARS, "10")),
        ),
        expected_outcome="refused",
        expected_codes=frozenset({RefusalCode.MISSING_KYC_FACTOR}),
        expected_missing=frozenset({FactorKey.RISK_TOLERANCE}),
        why=(
            "Risk tolerance is a required KYC factor and this profile documents none, so "
            "there is nothing for the fund's medium rating to be compared against. The "
            "refusal must name the absence itself rather than reporting a risk mismatch: "
            "no mismatch has been established, only an inability to look. Nothing else "
            "about the profile is unusual, so this is the whole refusal."
        ),
    ),
    EvalCase(
        name="the portfolio is not documented",
        category=Category.REFUSAL_CODE,
        profile=a_profile(holdings=()),
        recommendation=a_recommendation(),
        expected_outcome="refused",
        expected_codes=frozenset({RefusalCode.MISSING_KYC_FACTOR}),
        expected_missing=frozenset({FactorKey.HOLDINGS}),
        why=(
            "An empty holdings tuple is the same value as an undocumented portfolio, and "
            "the two readings differ: as an empty portfolio, any purchase is 100% of it "
            "and breaches; as an undocumented one, the concentration rule cannot run. "
            "Neither reading supports the recommendation, so the engine should refuse — "
            "and it should refuse for the absence, because that is the fact it actually "
            "has. Everything else here is documented and fine."
        ),
    ),
    EvalCase(
        name="the product is rated above the documented tolerance",
        category=Category.REFUSAL_CODE,
        profile=a_profile(),
        recommendation=a_recommendation(
            product=a_product(risk_rating=RiskLevel.HIGH),
        ),
        expected_outcome="refused",
        expected_codes=frozenset({RefusalCode.RISK_MISMATCH}),
        why=(
            "The profile documents a medium tolerance and a growth objective, which caps "
            "the product at medium-to-high. A high-rated fund is above both. The lock-up, "
            "redemption terms, liquidity and concentration are all unchanged from the "
            "supported baseline, so the risk rating is the only thing wrong and the only "
            "code that should fire. The exposure is the recommendation's own doing — the "
            "profile documents no position in FUND-Z — so the refusal must say `created`, "
            "which is what the case below is the other half of."
        ),
        expected_origins=frozenset({BreachOrigin.CREATED}),
    ),
    EvalCase(
        name="a partial sell of a holding rated above the documented tolerance",
        category=Category.REFUSAL_CODE,
        profile=a_profile(),
        recommendation=a_recommendation(
            action=TradeAction.SELL,
            product=held_product("A", risk_rating=RiskLevel.HIGH),
            amount=cad("5000.00"),
        ),
        expected_outcome="refused",
        expected_codes=frozenset({RefusalCode.RISK_MISMATCH}),
        expected_origins=frozenset({BreachOrigin.REDUCED}),
        why=(
            "POSITION, revised — and the revision is the point, so the original argument "
            "stays here rather than being tidied away. This case was written in the "
            "`supported` category expecting no refusal at all. The reading behind that: "
            "the risk rule did not look at the action, so it treated a sale of a too-risky "
            "holding exactly like a purchase of one, and after this trade the client holds "
            "10,000 of the high-rated fund instead of 15,000 — strictly less of the thing "
            "the tolerance cannot support. Refusing it looked like refusing the remedy for "
            "the condition being refused for, and it left an advisor reducing an unsuitable "
            "position with no receipt for having done so. The engine refused; the "
            "disagreement was recorded rather than argued away. What the argument won was "
            "the finding, not the outcome. The refusal stands, because the objection is to "
            "the product's risk rating, which is a fact about the product that no disposal "
            "changes, and because a determination reading `supported` over a client still "
            "holding a fund above their documented tolerance would assert more than it can "
            "support — the failure this repository exists to prevent. What was actually "
            "wrong was the conflation. The refusal now carries `breach_origin=reduced` and "
            "states both sides of the position in figures, so the receipt for a staged "
            "de-risking is legibly different from the receipt for the purchase that opened "
            "the exposure, and the advisor does get a record of having reduced it. The case "
            "left the `supported` category when its expectation flipped: that category is "
            "the over-refusal guard, and a refusal case sitting in it would falsify what "
            "the category claims about itself. No other rule objects — 5,000 of a 90,000 "
            "portfolio moves no bucket past the limit, a sale consumes no liquidity, and "
            "the fund redeems daily — so `risk_mismatch` is still the only code."
        ),
    ),
    EvalCase(
        name="the product has no redemption window at all",
        category=Category.REFUSAL_CODE,
        profile=a_profile(),
        recommendation=a_recommendation(
            product=a_product(redemption_frequency=RedemptionFrequency.NONE),
        ),
        expected_outcome="refused",
        expected_codes=frozenset({RefusalCode.HORIZON_MISMATCH}),
        why=(
            "A documented ten-year horizon is a statement that the client expects to "
            "exit. A product with no redemption window offers no exit at any price, which "
            "is not a long wait but the absence of one, and it conflicts with any horizon "
            "the profile could document. The rating is medium and within tolerance, so "
            "the horizon is the only conflict."
        ),
    ),
    EvalCase(
        name="the purchase would eat into the documented emergency reserve",
        category=Category.REFUSAL_CODE,
        profile=a_profile(liquid_net_worth=cad("30000.00")),
        recommendation=a_recommendation(),
        expected_outcome="refused",
        expected_codes=frozenset({RefusalCode.LIQUIDITY_CONFLICT}),
        why=(
            "The documented requirement is a 20,000 reserve plus a 5,000 upcoming "
            "expense, 25,000 in total, against 30,000 of liquid net worth. A 10,000 "
            "purchase leaves 20,000, which is below the requirement the client "
            "documented. The purchase is small enough not to concentrate anything, so "
            "liquidity is the only conflict."
        ),
    ),
    EvalCase(
        name="the rationale claims a tolerance the profile does not document",
        category=Category.REFUSAL_CODE,
        profile=a_profile(),
        recommendation=a_recommendation(
            rationale=a_rationale(
                cite(FactorKey.RISK_TOLERANCE, "high"),
                cite(FactorKey.TIME_HORIZON_YEARS, "10"),
            ),
        ),
        expected_outcome="refused",
        expected_codes=frozenset({RefusalCode.UNSUPPORTED_RATIONALE}),
        why=(
            "The profile documents a medium tolerance; the rationale asserts a high one. "
            "The recommendation itself is for a medium-rated fund and would pass every "
            "substantive rule, which is exactly why this case matters: the reasoning is "
            "wrong even though the trade is fine, and a system that only checked the "
            "trade would let a fabricated client fact through on a supported receipt."
        ),
    ),
    EvalCase(
        name="the rationale cites nothing at all",
        category=Category.REFUSAL_CODE,
        profile=a_profile(),
        recommendation=a_recommendation(rationale=a_rationale()),
        expected_outcome="refused",
        expected_codes=frozenset({RefusalCode.MISSING_KYC_FACTOR}),
        expected_missing=frozenset(
            {
                FactorKey.RISK_TOLERANCE,
                FactorKey.TIME_HORIZON_YEARS,
                FactorKey.INVESTMENT_OBJECTIVES,
            },
        ),
        why=(
            "An uncited rationale anchors the recommendation to nothing. It should refuse "
            "as a missing input rather than as an unsupported claim, because no claim was "
            "made: `unsupported_rationale` carries the claims that failed, and there are "
            "none to carry. `missing_kyc_factor` is the only code whose evidence can "
            "express 'a required input was not supplied', so the refusal names the "
            "required factors nothing was said about."
        ),
    ),
    EvalCase(
        name="the profile is older than the review interval",
        category=Category.REFUSAL_CODE,
        profile=a_profile(last_reviewed=_LONG_STALE),
        recommendation=a_recommendation(rationale=a_rationale(*_dated_claims(_LONG_STALE))),
        expected_outcome="refused",
        expected_codes=frozenset({RefusalCode.STALE_PROFILE}),
        why=(
            "Reviewed 2024-01-01 and evaluated 2025-07-01: 547 days, well past the "
            "configured 365. Every documented fact still reads as suitable, and that is "
            "the point — the facts are not wrong, they are too old to determine on, and "
            "the refusal is about their age rather than their content."
        ),
    ),
    EvalCase(
        name="a missing factor alongside a liquidity and a concentration breach",
        category=Category.REFUSAL_CODE,
        profile=a_profile(risk_tolerance=None, liquid_net_worth=cad("30000.00")),
        recommendation=a_recommendation(
            amount=cad("30000.00"),
            rationale=a_rationale(cite(FactorKey.TIME_HORIZON_YEARS, "10")),
        ),
        expected_outcome="refused",
        expected_codes=frozenset(
            {
                RefusalCode.MISSING_KYC_FACTOR,
                RefusalCode.LIQUIDITY_CONFLICT,
                RefusalCode.CONCENTRATION_BREACH,
            },
        ),
        expected_missing=frozenset({FactorKey.RISK_TOLERANCE}),
        expected_origins=frozenset({BreachOrigin.CREATED}),
        why=(
            "Three independent things are wrong and the refusal must carry all three. "
            "Tolerance is undocumented. The 30,000 purchase leaves nothing of a 30,000 "
            "liquid position against a documented 25,000 requirement. And it takes the "
            "new fund to 30,000 of a 120,000 portfolio — 25%, past the configured 20%, "
            "in a portfolio that breached nothing before it, so the concentration reason "
            "must say `created`. Reporting whichever fired first would understate the "
            "state of the file."
        ),
    ),
    EvalCase(
        name="nothing is documented, so the substantive rules cannot run",
        category=Category.REFUSAL_CODE,
        profile=a_profile(
            last_reviewed=_LONG_STALE,
            risk_tolerance=None,
            time_horizon_years=None,
            investment_objectives=(),
            liquidity_requirement=None,
            liquid_net_worth=None,
            holdings=(),
        ),
        recommendation=a_recommendation(
            product=a_product(
                risk_rating=RiskLevel.HIGH,
                redemption_frequency=RedemptionFrequency.NONE,
            ),
            rationale=a_rationale(cite(FactorKey.RISK_TOLERANCE, "medium", _LONG_STALE)),
        ),
        expected_outcome="refused",
        expected_codes=frozenset(
            {
                RefusalCode.MISSING_KYC_FACTOR,
                RefusalCode.UNSUPPORTED_RATIONALE,
                RefusalCode.STALE_PROFILE,
            },
        ),
        expected_missing=frozenset(
            {
                FactorKey.RISK_TOLERANCE,
                FactorKey.TIME_HORIZON_YEARS,
                FactorKey.INVESTMENT_OBJECTIVES,
                FactorKey.LIQUID_NET_WORTH,
                FactorKey.LIQUIDITY_REQUIREMENT,
                FactorKey.HOLDINGS,
            },
        ),
        why=(
            "The product is high risk with no redemption window, and neither of those "
            "should be reported. A risk mismatch is a finding about a comparison, and "
            "with no documented tolerance or objective there is no comparison to make; "
            "the same holds for the horizon. What the engine actually knows is that six "
            "factors are undocumented, that the rationale asserts a tolerance the profile "
            "does not carry, and that the file is 547 days old. Three codes, and the two "
            "that look most alarming are the two it must not claim."
        ),
    ),
)

_SUPPORTED_CASES: tuple[EvalCase, ...] = (
    EvalCase(
        name="a fully documented profile and an ordinary purchase",
        category=Category.SUPPORTED,
        profile=a_profile(),
        recommendation=a_recommendation(),
        expected_outcome="supported",
        why=(
            "Every KYC factor is documented. A medium-rated fund against a medium "
            "tolerance and a growth objective; daily redemption and no lock-up against a "
            "ten-year horizon; a 10,000 purchase against 200,000 of liquid net worth and "
            "a 25,000 requirement; 10% of the resulting portfolio in the new fund; a "
            "rationale citing exactly what the profile says; reviewed 30 days ago. There "
            "is nothing for any of the seven rules to catch, and a set made only of edge "
            "cases would never notice if that stopped being true."
        ),
    ),
    EvalCase(
        name="a hold of a position already in the portfolio",
        category=Category.SUPPORTED,
        profile=a_profile(),
        recommendation=a_recommendation(
            action=TradeAction.HOLD,
            product=held_product("A"),
            amount=cad("15000.00"),
        ),
        expected_outcome="supported",
        why=(
            "A hold changes nothing: no liquidity is consumed and no exposure moves, so "
            "the portfolio the rules see is the one they already accept — six equal "
            "positions at 16.67% each. Recommending that a suitable position be kept is "
            "the least eventful recommendation there is, and it must not refuse."
        ),
    ),
    EvalCase(
        name="a switch that fits within documented liquidity",
        category=Category.SUPPORTED,
        profile=a_profile(),
        recommendation=a_recommendation(action=TradeAction.SWITCH),
        expected_outcome="supported",
        why=(
            "A switch is counted gross — the recommendation does not document what is "
            "being switched out of, so nothing is netted. Counted gross it is a 10,000 "
            "consumption against 200,000 of liquid net worth, which clears the 25,000 "
            "requirement with room to spare. The conservative reading and the generous "
            "one agree here, so a switch this size must be supported."
        ),
    ),
    EvalCase(
        name="a conservative client and a low-risk product",
        category=Category.SUPPORTED,
        profile=a_profile(
            risk_tolerance=RiskLevel.LOW,
            investment_objectives=(InvestmentObjective.CAPITAL_PRESERVATION,),
            time_horizon_years=Decimal(3),
        ),
        recommendation=a_recommendation(
            product=a_product(risk_rating=RiskLevel.LOW),
            rationale=a_rationale(
                cite(FactorKey.RISK_TOLERANCE, "low"),
                cite(FactorKey.INVESTMENT_OBJECTIVES, "capital_preservation"),
            ),
        ),
        expected_outcome="supported",
        why=(
            "Both the documented tolerance and the capital-preservation objective cap the "
            "product at low, and the fund is rated low: at the ceiling, not above it. The "
            "three-year horizon is 1,095 days against a daily-redeemable fund with no "
            "lock-up. A conservative client is the one an over-strict engine refuses by "
            "reflex, and there is nothing here to refuse."
        ),
    ),
    EvalCase(
        name="a liquidity requirement with no upcoming expenses",
        category=Category.SUPPORTED,
        profile=a_profile(
            liquidity_requirement=LiquidityRequirement(emergency_reserve=cad("20000.00")),
        ),
        recommendation=a_recommendation(),
        expected_outcome="supported",
        why=(
            "The requirement is documented and consists of a reserve alone. An empty list "
            "of upcoming expenses is a documented fact — the client has none — and must "
            "not be read as an undocumented requirement the way an empty holdings tuple "
            "is. The distinction matters: 20,000 required against 190,000 remaining is "
            "comfortably clear, and refusing here would be refusing a fully documented "
            "profile for the shape of one of its fields."
        ),
    ),
    EvalCase(
        name="a sophisticated client, a speculative objective, a high-risk fund",
        category=Category.SUPPORTED,
        profile=a_profile(
            risk_tolerance=RiskLevel.HIGH,
            investment_objectives=(InvestmentObjective.SPECULATION,),
            investment_knowledge=InvestmentKnowledge.SOPHISTICATED,
            time_horizon_years=Decimal(20),
        ),
        recommendation=a_recommendation(
            product=a_product(
                risk_rating=RiskLevel.HIGH,
                redemption_frequency=RedemptionFrequency.ANNUAL,
                lock_up_days=1000,
                recommended_holding_period_years=Decimal(5),
            ),
            rationale=a_rationale(
                cite(FactorKey.RISK_TOLERANCE, "high"),
                cite(FactorKey.INVESTMENT_OBJECTIVES, "speculation"),
            ),
        ),
        expected_outcome="supported",
        why=(
            "Tolerance and objective agree at the top of the scale, so the ceiling is "
            "high and a high-rated fund sits at it. The 20-year horizon is 7,300 days "
            "against a 1,000-day lock-up, a 366-day worst-case redemption wait and a "
            "five-year recommended holding period. Every term clears. The engine must be "
            "able to say yes to risk the profile actually documents, or the risk rule is "
            "just a preference for low-rated products."
        ),
    ),
)

_CEILING_CLAIMS = (
    cite(FactorKey.RISK_TOLERANCE, "high"),
    cite(FactorKey.INVESTMENT_OBJECTIVES, "growth"),
)
"""Cited correctly against `_growth_ceiling_profile`, whose tolerance is high and whose
objective is growth — so the objective, not the tolerance, is what sets the ceiling."""


def _growth_ceiling_profile() -> ClientProfile:
    """A profile whose ceiling comes from the objective: high tolerance, growth objective."""
    return a_profile(risk_tolerance=RiskLevel.HIGH)


_BOUNDARY_CASES: tuple[EvalCase, ...] = (
    EvalCase(
        name="reviewed one day inside the review interval",
        category=Category.BOUNDARY,
        profile=a_profile(last_reviewed=_INSIDE_INTERVAL),
        recommendation=a_recommendation(rationale=a_rationale(*_dated_claims(_INSIDE_INTERVAL))),
        expected_outcome="supported",
        why=(
            "364 days old against a configured interval of 365. Inside the interval by a "
            "day is inside it, and the recommendation is the supported baseline in every "
            "other respect."
        ),
    ),
    EvalCase(
        name="reviewed exactly at the review interval",
        category=Category.BOUNDARY,
        profile=a_profile(last_reviewed=_AT_INTERVAL),
        recommendation=a_recommendation(rationale=a_rationale(*_dated_claims(_AT_INTERVAL))),
        expected_outcome="supported",
        why=(
            "Exactly 365 days old. The interval is the age a profile is allowed to reach, "
            "not the age at which it fails: a profile reviewed annually is due for review "
            "on day 365 and is not yet overdue. This is the case that pins which side of "
            "the comparison the boundary falls on, and it must be supported for the "
            "constant to mean 'an annual cadence' rather than 'slightly under a year'."
        ),
    ),
    EvalCase(
        name="reviewed one day past the review interval",
        category=Category.BOUNDARY,
        profile=a_profile(last_reviewed=_PAST_INTERVAL),
        recommendation=a_recommendation(rationale=a_rationale(*_dated_claims(_PAST_INTERVAL))),
        expected_outcome="refused",
        expected_codes=frozenset({RefusalCode.STALE_PROFILE}),
        why=(
            "366 days old, one day past the configured interval, and everything else is "
            "the supported baseline. If this does not refuse, the interval is not a "
            "threshold at all."
        ),
    ),
    EvalCase(
        name="a purchase one cent below the concentration limit",
        category=Category.BOUNDARY,
        profile=a_profile(),
        recommendation=a_recommendation(amount=cad("22499.99")),
        expected_outcome="supported",
        why=(
            "22,499.99 into a 90,000 portfolio makes a total of 112,499.99, of which the "
            "new fund is 22,499.99 — a hair under the 22,499.998 the 20% limit allows. "
            "The six existing positions sit at 13.33% each. One cent is the smallest step "
            "the money type can take, so this is genuinely one unit below the limit."
        ),
    ),
    EvalCase(
        name="a purchase exactly at the concentration limit",
        category=Category.BOUNDARY,
        profile=a_profile(),
        recommendation=a_recommendation(amount=cad("22500.00")),
        expected_outcome="supported",
        why=(
            "22,500 into a 90,000 portfolio is exactly 20% of the resulting 112,500. The "
            "constant is documented as the maximum share permitted, so the maximum itself "
            "is permitted and the rule must refuse only what exceeds it. Five equal "
            "positions at 20% each is the most concentrated portfolio the limit is "
            "supposed to allow; if this refused, the limit would in practice be 'under "
            "20%' and the documented derivation would no longer describe it."
        ),
    ),
    EvalCase(
        name="a purchase one cent above the concentration limit",
        category=Category.BOUNDARY,
        profile=a_profile(),
        recommendation=a_recommendation(amount=cad("22500.01")),
        expected_outcome="refused",
        expected_codes=frozenset({RefusalCode.CONCENTRATION_BREACH}),
        expected_origins=frozenset({BreachOrigin.CREATED}),
        why=(
            "22,500.01 of a 112,500.01 portfolio is 20.000008%, past the limit by the "
            "smallest amount the money type can express. The purchase clears liquidity "
            "with 177,499.99 remaining, so concentration is the only code, and the "
            "portfolio it went into breached nothing, so the origin is `created` by a "
            "cent. A threshold that does not fire one cent above itself is not enforced, "
            "and an origin that could not attribute a one-cent breach to the trade that "
            "caused it would not be attributing anything."
        ),
    ),
    EvalCase(
        name="a product one rank below the ceiling the objective implies",
        category=Category.BOUNDARY,
        profile=_growth_ceiling_profile(),
        recommendation=a_recommendation(
            product=a_product(risk_rating=RiskLevel.MEDIUM),
            rationale=a_rationale(*_CEILING_CLAIMS),
        ),
        expected_outcome="supported",
        why=(
            "Tolerance is high, so the growth objective is what sets the ceiling: "
            "medium-to-high. A medium-rated fund is one rank below it. The ceiling is a "
            "cap and not a target, so anything at or under it passes."
        ),
    ),
    EvalCase(
        name="a product exactly at the ceiling the objective implies",
        category=Category.BOUNDARY,
        profile=_growth_ceiling_profile(),
        recommendation=a_recommendation(
            product=a_product(risk_rating=RiskLevel.MEDIUM_TO_HIGH),
            rationale=a_rationale(*_CEILING_CLAIMS),
        ),
        expected_outcome="supported",
        why=(
            "The documented tolerance is high and would permit anything; the growth "
            "objective caps the product at medium-to-high, and the fund is rated exactly "
            "that. The mapping says which risk each objective *supports*, so the level it "
            "names must be reachable. If this refused, every entry in the table would in "
            "effect mean the rank below the one it names."
        ),
    ),
    EvalCase(
        name="a product one rank above the ceiling the objective implies",
        category=Category.BOUNDARY,
        profile=_growth_ceiling_profile(),
        recommendation=a_recommendation(
            product=a_product(risk_rating=RiskLevel.HIGH),
            rationale=a_rationale(*_CEILING_CLAIMS),
        ),
        expected_outcome="refused",
        expected_codes=frozenset({RefusalCode.RISK_MISMATCH}),
        expected_origins=frozenset({BreachOrigin.CREATED}),
        why=(
            "A high-rated fund against a growth objective, one rank above the ceiling the "
            "objective implies. The documented tolerance is high and would permit it, "
            "which is the point: the objective must be able to lower the ceiling on its "
            "own, or the mapping decorates the determination instead of constraining it. "
            "It is a purchase of a fund the profile documents no position in, so the "
            "origin is `created`."
        ),
    ),
)

_CONCENTRATION_CASES: tuple[EvalCase, ...] = (
    EvalCase(
        name="a sell that reduces an over-concentration without curing it",
        category=Category.CONCENTRATION,
        profile=a_profile(holdings=concentrated_positions()),
        recommendation=a_recommendation(
            action=TradeAction.SELL,
            product=held_product("A"),
            amount=cad("15000.00"),
        ),
        expected_outcome="refused",
        expected_codes=frozenset({RefusalCode.CONCENTRATION_BREACH}),
        expected_origins=frozenset({BreachOrigin.REDUCED}),
        why=(
            "POSITION, stated before scoring and then revised by what the scoring showed. "
            "The original argument, kept because the revision only makes sense against it: "
            "the client holds 40,000 of FUND-A in a 100,000 portfolio — 40%, twice the "
            "configured limit, and a breach that existed before anyone proposed anything. "
            "Selling 15,000 takes it to 25,000 of 85,000: 29.41%, still over the limit but "
            "materially closer to it. The engine refused, and the refusal was a statement "
            "about the portfolio rather than about the recommendation. Two things were "
            "wrong with that. First, it gave the same verdict to a trade that halves the "
            "breach and a trade that creates one, so the code stopped distinguishing the "
            "advisor's proposal from the client's history. Second, staged de-risking is "
            "ordinary practice, and every stage but the last refused — the rule refused "
            "the remedy for the condition it was refusing for. The case therefore asked "
            "for `supported`, while naming the cost of that honestly: the receipt would "
            "say 'supported' about a portfolio still holding 29.41% in one name, and a "
            "`Determination` has nowhere to record a residual breach. That cost is what "
            "decided it. Asserting support for a portfolio a third of the way into one "
            "name is asserting more than the engine can support, which is the thing this "
            "repository argues against, and no amount of improvement makes an over-limit "
            "portfolio an under-limit one. So the alternative the case itself named is the "
            "one taken: keep refusing, and say which of the two situations it is. The "
            "outcome stays, the conflation does not. The reason now carries "
            "`breach_origin=reduced` and its detail gives every breaching bucket as 29.41% "
            "against the 40.00% it was, so the residual is visible and so is the direction "
            "of travel — the advisor has a receipt for the stage they completed, and it "
            "cannot be mistaken for a receipt for having caused the concentration."
        ),
    ),
    EvalCase(
        name="a sell that cures an over-concentration",
        category=Category.CONCENTRATION,
        profile=a_profile(holdings=concentrated_positions()),
        recommendation=a_recommendation(
            action=TradeAction.SELL,
            product=held_product("A"),
            amount=cad("30000.00"),
        ),
        expected_outcome="supported",
        why=(
            "Selling 30,000 of the 40,000 position leaves 10,000 of a 70,000 portfolio: "
            "14.29%, inside the limit, and the six other positions land at 14.29% each as "
            "the total shrinks. No bucket breaches afterwards, no liquidity is consumed, "
            "and the fund is medium-rated against a medium tolerance. This must be "
            "supported under any reading of the concentration rule, which is what makes "
            "it the control for the case above: the two differ only in the size of the "
            "sale, so a disagreement between them isolates the rule's treatment of a "
            "residual breach rather than of sales in general."
        ),
    ),
    EvalCase(
        name="a buy that creates an over-concentration",
        category=Category.CONCENTRATION,
        profile=a_profile(),
        recommendation=a_recommendation(amount=cad("30000.00")),
        expected_outcome="refused",
        expected_codes=frozenset({RefusalCode.CONCENTRATION_BREACH}),
        expected_origins=frozenset({BreachOrigin.CREATED}),
        why=(
            "A well-diversified 90,000 portfolio, and a 30,000 purchase that takes the "
            "new fund to 25% of the resulting 120,000 — past the configured limit in "
            "instrument, issuer and sector at once. Here the breach is entirely the "
            "recommendation's doing, which is precisely the case the rule exists for, and "
            "the origin must say `created`. It is the control for the reducing sell above: "
            "the two now share a code and must not share a finding, so a set that checked "
            "only the code would score an engine that reports them identically as correct "
            "on both. Liquidity clears at 170,000 remaining, so this is a single-code "
            "refusal."
        ),
    ),
    EvalCase(
        name="an unrelated buy into an already over-concentrated portfolio",
        category=Category.CONCENTRATION,
        profile=a_profile(holdings=concentrated_positions()),
        recommendation=a_recommendation(),
        expected_outcome="refused",
        expected_codes=frozenset({RefusalCode.CONCENTRATION_BREACH}),
        expected_origins=frozenset({BreachOrigin.UNCHANGED}),
        why=(
            "The 40% position in FUND-A is untouched by a 10,000 purchase of an unrelated "
            "fund, and the purchase does lower A's share to 36.36% by enlarging the "
            "denominator. That is not remediation: nothing was disposed of, the client's "
            "exposure to FUND-A is the same 40,000 it was, and the arithmetic improvement "
            "is an artefact of putting more money in. Refusing is right, and the "
            "distinction from the reducing sell is exactly the one the rule should be "
            "drawing — a disposal reduces exposure, growth of the denominator dilutes it. "
            "So the origin must be `unchanged` and not `reduced`, even though the "
            "percentage fell. This is the case that forces the rule to read the exposure "
            "amount rather than the fraction: a rule that classified on the share would "
            "call this a reduction and hand a dilution the receipt earned by a sale."
        ),
    ),
    EvalCase(
        name="a hold on an over-concentrated position",
        category=Category.CONCENTRATION,
        profile=a_profile(holdings=concentrated_positions()),
        recommendation=a_recommendation(
            action=TradeAction.HOLD,
            product=held_product("A"),
            amount=cad("40000.00"),
        ),
        expected_outcome="refused",
        expected_codes=frozenset({RefusalCode.CONCENTRATION_BREACH}),
        expected_origins=frozenset({BreachOrigin.UNCHANGED}),
        why=(
            "A hold is a recommendation to keep the 40% position exactly as it is. "
            "Nothing about the breach changes, and the recommendation is that it should "
            "not. This is the contrast that keeps the distinction drawn above narrow: "
            "'the recommendation reduced the breach' has to mean an actual reduction, and "
            "a hold reduces nothing, so the origin is `unchanged`. It is inherited, like "
            "the reducing sell, and it is not doing anything about it — which is the whole "
            "difference between the two, and now the only difference visible on the "
            "receipt, since both refuse under the same code with the same 40.00% figure."
        ),
    ),
)

_TENSION_CASES: tuple[EvalCase, ...] = (
    EvalCase(
        name="a low tolerance against a speculative objective",
        category=Category.TENSION,
        profile=a_profile(
            risk_tolerance=RiskLevel.LOW,
            investment_objectives=(InvestmentObjective.SPECULATION,),
        ),
        recommendation=a_recommendation(
            rationale=a_rationale(
                cite(FactorKey.RISK_TOLERANCE, "low"),
                cite(FactorKey.INVESTMENT_OBJECTIVES, "speculation"),
            ),
        ),
        expected_outcome="refused",
        expected_codes=frozenset({RefusalCode.RISK_MISMATCH}),
        expected_origins=frozenset({BreachOrigin.CREATED}),
        why=(
            "The file says two incompatible things: a low tolerance, which caps the "
            "product at low, and a speculative objective, which would allow anything. A "
            "medium-rated fund is above the first and below the second. A profile that "
            "contradicts itself is not evidence for the half that permits the trade, so "
            "the low tolerance wins and this refuses. The alternative — reading the "
            "objective as an update to the tolerance — is the engine deciding which of "
            "two documented facts it prefers, which is exactly the judgement it is built "
            "not to make."
        ),
    ),
    EvalCase(
        name="a high tolerance against a capital-preservation objective",
        category=Category.TENSION,
        profile=a_profile(
            risk_tolerance=RiskLevel.HIGH,
            investment_objectives=(InvestmentObjective.CAPITAL_PRESERVATION,),
        ),
        recommendation=a_recommendation(
            rationale=a_rationale(
                cite(FactorKey.RISK_TOLERANCE, "high"),
                cite(FactorKey.INVESTMENT_OBJECTIVES, "capital_preservation"),
            ),
        ),
        expected_outcome="refused",
        expected_codes=frozenset({RefusalCode.RISK_MISMATCH}),
        expected_origins=frozenset({BreachOrigin.CREATED}),
        why=(
            "The mirror image of the case above, and it must resolve the same way. Here "
            "the tolerance is the permissive half and the objective is the restrictive "
            "one, so a rule that simply trusted the documented tolerance would support a "
            "medium-rated fund for a client whose stated aim is preserving capital. The "
            "conservative reading has to win regardless of which field it comes from, or "
            "it is not a principle, only a preference for one field over another."
        ),
    ),
    EvalCase(
        name="two objectives in tension with each other",
        category=Category.TENSION,
        profile=a_profile(
            risk_tolerance=RiskLevel.HIGH,
            investment_objectives=(
                InvestmentObjective.CAPITAL_PRESERVATION,
                InvestmentObjective.SPECULATION,
            ),
        ),
        recommendation=a_recommendation(
            product=a_product(risk_rating=RiskLevel.LOW),
            rationale=a_rationale(
                cite(FactorKey.RISK_TOLERANCE, "high"),
                cite(FactorKey.INVESTMENT_OBJECTIVES, "capital_preservation, speculation"),
            ),
        ),
        expected_outcome="supported",
        why=(
            "The profile documents both preserving capital and speculating, and a high "
            "tolerance besides. The lowest ceiling among them is low, and the fund is "
            "rated low, so the most conservative reading of a self-contradicting file "
            "still admits this product. The case is here to show that resolving tension "
            "conservatively is not the same as refusing on tension: the resolution lowers "
            "the ceiling, and a product under the lowered ceiling passes."
        ),
    ),
    EvalCase(
        name="a tension that does not bite",
        category=Category.TENSION,
        profile=a_profile(risk_tolerance=RiskLevel.LOW),
        recommendation=a_recommendation(
            product=a_product(risk_rating=RiskLevel.LOW),
            rationale=a_rationale(
                cite(FactorKey.RISK_TOLERANCE, "low"),
                cite(FactorKey.INVESTMENT_OBJECTIVES, "growth"),
            ),
        ),
        expected_outcome="supported",
        why=(
            "A low tolerance and a growth objective disagree, and it does not matter: the "
            "conservative reading caps the product at low and the fund is rated low. This "
            "is the over-refusal guard for the tension rule. The failure it is watching "
            "for is an engine that treats the presence of a contradiction as a refusal in "
            "itself, which would refuse a great many real files for a mismatch that never "
            "touches the product actually being recommended."
        ),
    ),
)

CASES: tuple[EvalCase, ...] = (
    *_REFUSAL_CODE_CASES,
    *_SUPPORTED_CASES,
    *_BOUNDARY_CASES,
    *_CONCENTRATION_CASES,
    *_TENSION_CASES,
)
"""The whole set, in category order."""
