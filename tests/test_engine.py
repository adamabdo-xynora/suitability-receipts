"""Tests for the determination engine.

Each of the seven rules is tested on its own, at its threshold and one unit either
side, followed by the entry point: coverage of the taxonomy, merged refusals, and
byte-identical determinism.

All client data here is synthetic and obviously so: identifiers carry a `SYNTHETIC-`
prefix and names are placeholders. No real client data and no credentials appear in
this repository, including in fixtures.
"""

import datetime as dt
from decimal import Decimal

import pytest

from suitability_receipts import (
    BreachOrigin,
    ClientProfile,
    Currency,
    Determination,
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
    Refusal,
    RefusalCode,
    RefusalReason,
    RiskLevel,
    RuleCheck,
    RuleConfig,
    RuleInput,
    TradeAction,
    UpcomingExpense,
    determine,
)
from suitability_receipts.engine import (
    RULES,
    RuleOutcome,
    breach_origin_of,
    check_concentration_breach,
    check_horizon_mismatch,
    check_liquidity_conflict,
    check_missing_kyc_factor,
    check_risk_mismatch,
    check_stale_profile,
    check_unsupported_rationale,
)

REVIEWED_ON = dt.date(2025, 6, 1)
NOW = dt.datetime(2025, 7, 1, 12, 0, tzinfo=dt.UTC)


def cad(amount: str) -> Money:
    """Build a synthetic CAD amount."""
    return Money(amount=Decimal(amount), currency=Currency.CAD)


def a_citation(
    key: FactorKey = FactorKey.RISK_TOLERANCE,
    value: str = "medium",
    documented_on: dt.date = REVIEWED_ON,
) -> FactorCitation:
    """Build a citation against the synthetic profile."""
    return FactorCitation(key=key, value=value, documented_on=documented_on)


def diversified_holdings() -> tuple[Holding, ...]:
    """Six equal positions, each in its own instrument, issuer, and sector.

    Six of 15,000 total 90,000, so each is 16.67% — under the configured 20% limit with
    room for a purchase, which keeps the baseline profile a supported one.
    """
    return tuple(
        Holding(
            instrument_id=f"SYNTHETIC-FUND-{letter}",
            name=f"Synthetic Placeholder Fund {letter}",
            issuer=f"Synthetic Placeholder Issuer {letter}",
            sector=f"synthetic-sector-{letter.lower()}",
            market_value=cad("15000.00"),
        )
        for letter in ("A", "B", "C", "D", "E", "F")
    )


def concentrated_holdings() -> tuple[Holding, ...]:
    """40,000 in FUND-A beside six 10,000 positions: 40% of a 100,000 portfolio.

    FUND-A's issuer and sector are its alone, so the same 40% breaches all three
    dimensions at once — which is what a real single-name concentration looks like.
    """
    return (
        Holding(
            instrument_id="SYNTHETIC-FUND-A",
            name="Synthetic Placeholder Fund A",
            issuer="Synthetic Placeholder Issuer A",
            sector="synthetic-sector-a",
            market_value=cad("40000.00"),
        ),
        *(
            Holding(
                instrument_id=f"SYNTHETIC-FUND-{letter}",
                name=f"Synthetic Placeholder Fund {letter}",
                issuer=f"Synthetic Placeholder Issuer {letter}",
                sector=f"synthetic-sector-{letter.lower()}",
                market_value=cad("10000.00"),
            )
            for letter in ("B", "C", "D", "E", "F", "G")
        ),
    )


def held_product(letter: str, **overrides: object) -> Product:
    """Build the product record for a position the synthetic portfolio already holds."""
    return a_product(
        instrument_id=f"SYNTHETIC-FUND-{letter}",
        name=f"Synthetic Placeholder Fund {letter}",
        issuer=f"Synthetic Placeholder Issuer {letter}",
        sector=f"synthetic-sector-{letter.lower()}",
        **overrides,
    )


def a_profile(**overrides: object) -> ClientProfile:
    """Build a fully documented synthetic client profile."""
    fields: dict[str, object] = {
        "client_id": "SYNTHETIC-0001",
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
        "liquid_net_worth": cad("120000.00"),
        "holdings": diversified_holdings(),
    }
    fields.update(overrides)
    return ClientProfile.model_validate(fields)


def a_product(**overrides: object) -> Product:
    """Build a synthetic product with documented terms."""
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


def a_recommendation(**overrides: object) -> Recommendation:
    """Build a synthetic recommendation whose rationale cites the documented profile."""
    fields: dict[str, object] = {
        "recommendation_id": "SYNTHETIC-REC-0001",
        "client_id": "SYNTHETIC-0001",
        "action": TradeAction.BUY,
        "product": a_product(),
        "amount": cad("10000.00"),
        "proposed_on": REVIEWED_ON,
        "rationale": RecommendationRationale(
            text="Placeholder rationale for a synthetic recommendation.",
            cited_factors=(a_citation(),),
        ),
    }
    fields.update(overrides)
    return Recommendation.model_validate(fields)


def an_input(
    profile: ClientProfile | None = None,
    recommendation: Recommendation | None = None,
    as_of: dt.date | None = None,
    config: RuleConfig | None = None,
) -> RuleInput:
    """Build the input a single rule sees."""
    return RuleInput(
        profile=a_profile() if profile is None else profile,
        recommendation=a_recommendation() if recommendation is None else recommendation,
        as_of=NOW.date() if as_of is None else as_of,
        config=RuleConfig() if config is None else config,
    )


def passed(outcome: RuleOutcome) -> RuleCheck:
    """Assert a rule passed, and return the check it recorded."""
    assert isinstance(outcome, RuleCheck)
    return outcome


def refused(outcome: RuleOutcome) -> RefusalReason:
    """Assert a rule refused, and return the reason it gave."""
    assert isinstance(outcome, RefusalReason)
    return outcome


def refusal_for(outcome: object) -> Refusal:
    """Assert `outcome` is a refusal and return it."""
    assert isinstance(outcome, Refusal)
    return outcome


def codes(refusal: Refusal) -> tuple[RefusalCode, ...]:
    """Return the codes a refusal carries, in order."""
    return tuple(reason.code for reason in refusal.reasons)


# ---------------------------------------------------------------------------
# missing_kyc_factor
# ---------------------------------------------------------------------------


def test_missing_kyc_passes_on_a_fully_documented_profile() -> None:
    """A profile documenting every required factor passes, citing all three."""
    check = passed(check_missing_kyc_factor(an_input()))
    assert check.code == RefusalCode.MISSING_KYC_FACTOR
    assert tuple(citation.key for citation in check.basis) == (
        FactorKey.RISK_TOLERANCE,
        FactorKey.TIME_HORIZON_YEARS,
        FactorKey.INVESTMENT_OBJECTIVES,
    )


@pytest.mark.parametrize(
    ("field", "value", "key"),
    [
        ("risk_tolerance", None, FactorKey.RISK_TOLERANCE),
        ("time_horizon_years", None, FactorKey.TIME_HORIZON_YEARS),
        ("investment_objectives", (), FactorKey.INVESTMENT_OBJECTIVES),
    ],
)
def test_missing_kyc_refuses_each_required_factor_in_turn(
    field: str,
    value: object,
    key: FactorKey,
) -> None:
    """Each required factor, absent on its own, is named in the refusal."""
    outcome = check_missing_kyc_factor(an_input(profile=a_profile(**{field: value})))
    assert refused(outcome).code == RefusalCode.MISSING_KYC_FACTOR
    assert refused(outcome).missing_factors == (key,)


def test_missing_kyc_reports_every_absent_factor_not_only_the_first() -> None:
    """Two absent factors are both named."""
    profile = a_profile(risk_tolerance=None, investment_objectives=())
    outcome = check_missing_kyc_factor(an_input(profile=profile))
    assert refused(outcome).missing_factors == (
        FactorKey.RISK_TOLERANCE,
        FactorKey.INVESTMENT_OBJECTIVES,
    )


# ---------------------------------------------------------------------------
# risk_mismatch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rating", "refuses"),
    [
        (RiskLevel.LOW_TO_MEDIUM, False),
        (RiskLevel.MEDIUM, False),
        (RiskLevel.MEDIUM_TO_HIGH, True),
    ],
)
def test_risk_mismatch_at_the_tolerance_boundary(rating: RiskLevel, *, refuses: bool) -> None:
    """A medium tolerance admits medium and below, and refuses one step above."""
    recommendation = a_recommendation(product=a_product(risk_rating=rating))
    outcome = check_risk_mismatch(an_input(recommendation=recommendation))
    assert isinstance(outcome, RuleCheck) is not refuses


def test_risk_mismatch_takes_the_conservative_side_of_a_profile_in_tension() -> None:
    """A high tolerance with a capital-preservation objective does not license risk.

    The tension is resolved toward the lower ceiling, which is the reading that refuses.
    """
    profile = a_profile(
        risk_tolerance=RiskLevel.HIGH,
        investment_objectives=(InvestmentObjective.CAPITAL_PRESERVATION,),
    )
    outcome = check_risk_mismatch(an_input(profile=profile))
    assert refused(outcome).code == RefusalCode.RISK_MISMATCH
    assert tuple(citation.key for citation in refused(outcome).conflicting_factors) == (
        FactorKey.RISK_TOLERANCE,
        FactorKey.INVESTMENT_OBJECTIVES,
        FactorKey.HOLDINGS,
    )


def test_risk_mismatch_takes_the_conservative_of_two_objectives_in_tension() -> None:
    """Objectives that disagree with each other resolve to the lower ceiling."""
    profile = a_profile(
        risk_tolerance=RiskLevel.HIGH,
        investment_objectives=(
            InvestmentObjective.SPECULATION,
            InvestmentObjective.CAPITAL_PRESERVATION,
        ),
    )
    assert check_risk_mismatch(an_input(profile=profile)).code == RefusalCode.RISK_MISMATCH


def test_risk_mismatch_never_raises_the_ceiling_above_the_documented_tolerance() -> None:
    """A speculative objective does not lift a low tolerance."""
    profile = a_profile(
        risk_tolerance=RiskLevel.LOW,
        investment_objectives=(InvestmentObjective.SPECULATION,),
    )
    assert check_risk_mismatch(an_input(profile=profile)).code == RefusalCode.RISK_MISMATCH


def test_risk_mismatch_refuses_when_it_cannot_establish_a_basis() -> None:
    """Without a documented tolerance the rule refuses rather than passing."""
    outcome = check_risk_mismatch(an_input(profile=a_profile(risk_tolerance=None)))
    assert refused(outcome).code == RefusalCode.MISSING_KYC_FACTOR
    assert refused(outcome).missing_factors == (FactorKey.RISK_TOLERANCE,)


# ---------------------------------------------------------------------------
# risk_mismatch: whose exposure it is
#
# The rule refuses the same cases it always did. What is under test here is the
# `BreachOrigin` it now states, which is the only thing separating "the advisor proposed
# an unsuitable purchase" from "the advisor is unwinding one".
# ---------------------------------------------------------------------------

HIGH_RATED = {"risk_rating": RiskLevel.HIGH}
"""Above the medium tolerance and the medium-to-high growth ceiling of `a_profile`."""


def risk_origin(**recommendation_overrides: object) -> BreachOrigin:
    """Refuse a high-rated product under `a_profile` and return the origin stated."""
    recommendation = a_recommendation(**recommendation_overrides)
    outcome = check_risk_mismatch(an_input(recommendation=recommendation))
    assert refused(outcome).code == RefusalCode.RISK_MISMATCH
    origin = refused(outcome).breach_origin
    assert origin is not None
    return origin


def test_risk_mismatch_states_created_for_a_purchase_of_a_product_not_held() -> None:
    """A buy of a fund the profile documents no position in creates the exposure."""
    assert risk_origin(product=a_product(**HIGH_RATED)) is BreachOrigin.CREATED


def test_risk_mismatch_states_increased_for_a_purchase_of_a_product_already_held() -> None:
    """Adding to a position already above tolerance is worse than opening one is new."""
    assert (
        risk_origin(product=held_product("A", **HIGH_RATED), amount=cad("5000.00"))
        is BreachOrigin.INCREASED
    )


def test_risk_mismatch_states_reduced_for_a_partial_sell_of_a_held_position() -> None:
    """The case the eval set found: a disposal is not a purchase, and must not read as one."""
    assert (
        risk_origin(
            action=TradeAction.SELL,
            product=held_product("A", **HIGH_RATED),
            amount=cad("5000.00"),
        )
        is BreachOrigin.REDUCED
    )


@pytest.mark.parametrize("amount", ["14999.99", "15000.00", "15000.01"])
def test_risk_mismatch_still_refuses_a_sell_at_and_beyond_the_whole_position(
    amount: str,
) -> None:
    """Selling the entire position, or trying to oversell it, still refuses as `reduced`.

    The boundary of the carve-out that was *not* made. The objection is to the product's
    rating, which is a fact about the product and not about the size of the position, so
    reducing the position to nothing does not lift it. A sale larger than the holding is
    clamped to the holding, so it reduces exactly as far and no further.
    """
    assert (
        risk_origin(
            action=TradeAction.SELL,
            product=held_product("A", **HIGH_RATED),
            amount=cad(amount),
        )
        is BreachOrigin.REDUCED
    )


def test_risk_mismatch_states_unchanged_for_a_hold_of_a_held_position() -> None:
    """A hold moves nothing: it is inherited, and the recommendation is to keep it."""
    assert (
        risk_origin(
            action=TradeAction.HOLD,
            product=held_product("A", **HIGH_RATED),
            amount=cad("15000.00"),
        )
        is BreachOrigin.UNCHANGED
    )


def test_risk_mismatch_states_unchanged_for_a_sell_of_a_position_not_documented() -> None:
    """A disposal of something the profile does not document holding reduces nothing.

    Zero before and zero after. Reporting `reduced` here would credit the recommendation
    with a disposal the documented facts do not show it making.
    """
    assert (
        risk_origin(action=TradeAction.SELL, product=a_product(**HIGH_RATED))
        is BreachOrigin.UNCHANGED
    )


@pytest.mark.parametrize(
    ("action", "origin"),
    [
        (TradeAction.BUY, BreachOrigin.CREATED),
        (TradeAction.SWITCH, BreachOrigin.CREATED),
        (TradeAction.SELL, BreachOrigin.UNCHANGED),
        (TradeAction.HOLD, BreachOrigin.UNCHANGED),
    ],
)
def test_risk_mismatch_reads_an_undocumented_portfolio_conservatively(
    action: TradeAction,
    origin: BreachOrigin,
) -> None:
    """With no documented portfolio, a purchase creates and a disposal reduces nothing.

    A profile documenting no holdings cannot establish that a position already existed,
    so the rule records the reading it can defend rather than the one that reads best.
    The rule still runs: the client is told the product is too risky, not merely that
    their portfolio is undocumented.
    """
    recommendation = a_recommendation(action=action, product=held_product("A", **HIGH_RATED))
    outcome = check_risk_mismatch(
        an_input(profile=a_profile(holdings=()), recommendation=recommendation),
    )
    assert refused(outcome).breach_origin is origin


def test_risk_mismatch_cites_the_portfolio_only_when_the_profile_documents_one() -> None:
    """A rule may not cite a fact it could not read; it may not omit one it did."""
    recommendation = a_recommendation(product=a_product(**HIGH_RATED))
    documented = check_risk_mismatch(an_input(recommendation=recommendation))
    assert FactorKey.HOLDINGS in {c.key for c in refused(documented).conflicting_factors}

    undocumented = check_risk_mismatch(
        an_input(profile=a_profile(holdings=()), recommendation=recommendation),
    )
    assert FactorKey.HOLDINGS not in {c.key for c in refused(undocumented).conflicting_factors}


def test_risk_mismatch_states_both_sides_of_the_position_in_the_detail() -> None:
    """The residual is on the receipt in figures, not summarised into an adjective."""
    recommendation = a_recommendation(
        action=TradeAction.SELL,
        product=held_product("A", **HIGH_RATED),
        amount=cad("5000.00"),
    )
    detail = refused(check_risk_mismatch(an_input(recommendation=recommendation))).detail
    assert "15000.00 CAD" in detail
    assert "10000.00 CAD" in detail


def test_risk_mismatch_passes_without_reading_the_portfolio() -> None:
    """A product within the ceiling passes citing what it weighed, and no more.

    The portfolio is read only to classify a refusal, so a supported determination must
    not claim to have consulted it.
    """
    check = passed(check_risk_mismatch(an_input()))
    assert tuple(citation.key for citation in check.basis) == (
        FactorKey.RISK_TOLERANCE,
        FactorKey.INVESTMENT_OBJECTIVES,
    )


# ---------------------------------------------------------------------------
# horizon_mismatch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("lock_up_days", "refuses"), [(364, False), (365, False), (366, True)])
def test_horizon_mismatch_at_the_lock_up_boundary(lock_up_days: int, *, refuses: bool) -> None:
    """A one-year horizon admits a lock-up of exactly 365 days, and no more."""
    profile = a_profile(time_horizon_years=Decimal("1.0"))
    recommendation = a_recommendation(product=a_product(lock_up_days=lock_up_days))
    outcome = check_horizon_mismatch(an_input(profile=profile, recommendation=recommendation))
    assert isinstance(outcome, RuleCheck) is not refuses


@pytest.mark.parametrize(("period", "refuses"), [("0.9", False), ("1.0", False), ("1.1", True)])
def test_horizon_mismatch_at_the_holding_period_boundary(period: str, *, refuses: bool) -> None:
    """A recommended holding period equal to the horizon fits; longer does not."""
    profile = a_profile(time_horizon_years=Decimal("1.0"))
    product = a_product(recommended_holding_period_years=Decimal(period))
    outcome = check_horizon_mismatch(
        an_input(profile=profile, recommendation=a_recommendation(product=product)),
    )
    assert isinstance(outcome, RuleCheck) is not refuses


@pytest.mark.parametrize(("horizon", "refuses"), [("0.3", False), ("0.2", True)])
def test_horizon_mismatch_at_the_redemption_window_boundary(horizon: str, *, refuses: bool) -> None:
    """A quarterly window can require 92 days, which a 0.2-year horizon cannot absorb."""
    profile = a_profile(time_horizon_years=Decimal(horizon))
    product = a_product(redemption_frequency=RedemptionFrequency.QUARTERLY)
    outcome = check_horizon_mismatch(
        an_input(profile=profile, recommendation=a_recommendation(product=product)),
    )
    assert isinstance(outcome, RuleCheck) is not refuses


def test_horizon_mismatch_refuses_a_product_with_no_redemption_window() -> None:
    """No window at all conflicts with any documented horizon, however long."""
    product = a_product(redemption_frequency=RedemptionFrequency.NONE)
    outcome = check_horizon_mismatch(an_input(recommendation=a_recommendation(product=product)))
    assert outcome.code == RefusalCode.HORIZON_MISMATCH
    assert "no redemption window" in refused(outcome).detail


def test_horizon_mismatch_reports_every_conflicting_term() -> None:
    """A product that conflicts three ways says so three ways."""
    profile = a_profile(time_horizon_years=Decimal("1.0"))
    product = a_product(
        lock_up_days=800,
        redemption_frequency=RedemptionFrequency.NONE,
        recommended_holding_period_years=Decimal("5.0"),
    )
    outcome = check_horizon_mismatch(
        an_input(profile=profile, recommendation=a_recommendation(product=product)),
    )
    assert refused(outcome).detail.count(";") == 2


def test_horizon_mismatch_refuses_when_it_cannot_establish_a_basis() -> None:
    """Without a documented horizon the rule refuses rather than passing."""
    outcome = check_horizon_mismatch(an_input(profile=a_profile(time_horizon_years=None)))
    assert outcome.code == RefusalCode.MISSING_KYC_FACTOR
    assert refused(outcome).missing_factors == (FactorKey.TIME_HORIZON_YEARS,)


# ---------------------------------------------------------------------------
# liquidity_conflict
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "refuses"),
    [("94999.99", False), ("95000.00", False), ("95000.01", True)],
)
def test_liquidity_conflict_at_the_reserve_boundary(amount: str, *, refuses: bool) -> None:
    """Liquid net worth of 120,000 less a 25,000 requirement leaves exactly 95,000."""
    recommendation = a_recommendation(amount=cad(amount))
    outcome = check_liquidity_conflict(an_input(recommendation=recommendation))
    assert isinstance(outcome, RuleCheck) is not refuses


def test_liquidity_conflict_counts_a_switch_as_consuming_liquidity() -> None:
    """A switch does not document what funds it, so it is counted gross."""
    recommendation = a_recommendation(action=TradeAction.SWITCH, amount=cad("95000.01"))
    outcome = check_liquidity_conflict(an_input(recommendation=recommendation))
    assert outcome.code == RefusalCode.LIQUIDITY_CONFLICT


@pytest.mark.parametrize("action", [TradeAction.SELL, TradeAction.HOLD])
def test_liquidity_conflict_ignores_actions_that_consume_nothing(action: TradeAction) -> None:
    """A sell or a hold does not draw down liquid net worth."""
    recommendation = a_recommendation(action=action, amount=cad("500000.00"))
    outcome = check_liquidity_conflict(an_input(recommendation=recommendation))
    assert isinstance(outcome, RuleCheck)


@pytest.mark.parametrize(
    ("field", "key"),
    [
        ("liquid_net_worth", FactorKey.LIQUID_NET_WORTH),
        ("liquidity_requirement", FactorKey.LIQUIDITY_REQUIREMENT),
    ],
)
def test_liquidity_conflict_refuses_when_it_cannot_establish_a_basis(
    field: str,
    key: FactorKey,
) -> None:
    """Each factor the rule needs, absent, produces a missing-factor refusal."""
    outcome = check_liquidity_conflict(an_input(profile=a_profile(**{field: None})))
    assert outcome.code == RefusalCode.MISSING_KYC_FACTOR
    assert refused(outcome).missing_factors == (key,)


# ---------------------------------------------------------------------------
# concentration_breach
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "refuses"),
    [("22499.99", False), ("22500.00", False), ("22500.01", True)],
)
def test_concentration_breach_at_the_configured_limit(amount: str, *, refuses: bool) -> None:
    """22,500 into a 90,000 portfolio is exactly 20% of the resulting 112,500."""
    outcome = check_concentration_breach(
        an_input(recommendation=a_recommendation(amount=cad(amount))),
    )
    assert isinstance(outcome, RuleCheck) is not refuses


def test_concentration_breach_honours_a_configured_limit() -> None:
    """The threshold is configuration, not a constant baked into the rule."""
    recommendation = a_recommendation(amount=cad("10000.00"))
    config = RuleConfig(concentration_limit_fraction=Decimal("0.05"))
    outcome = check_concentration_breach(an_input(recommendation=recommendation, config=config))
    assert outcome.code == RefusalCode.CONCENTRATION_BREACH


def test_concentration_breach_aggregates_by_issuer() -> None:
    """Two instruments from one issuer concentrate on that issuer."""
    holdings = (
        *diversified_holdings()[:5],
        Holding(
            instrument_id="SYNTHETIC-FUND-G",
            name="Synthetic Placeholder Fund G",
            issuer="Synthetic Placeholder Issuer A",
            sector="synthetic-sector-g",
            market_value=cad("15000.00"),
        ),
    )
    outcome = check_concentration_breach(an_input(profile=a_profile(holdings=holdings)))
    assert outcome.code == RefusalCode.CONCENTRATION_BREACH
    assert "issuer Synthetic Placeholder Issuer A" in refused(outcome).detail


def test_concentration_breach_reports_every_breached_dimension() -> None:
    """A purchase can breach the instrument, issuer, and sector limits at once."""
    outcome = check_concentration_breach(
        an_input(recommendation=a_recommendation(amount=cad("50000.00"))),
    )
    detail = refused(outcome).detail
    assert detail.index("instrument ") < detail.index("issuer ") < detail.index("sector ")


def test_concentration_breach_refuses_a_sell_that_does_not_cure_the_breach() -> None:
    """A trade that improves an over-concentrated portfolio without curing it is refused.

    Stated as a decision rather than discovered as a surprise: the engine reports that
    the post-recommendation portfolio breaches the limit. Judging an improvement good
    enough is a human's call, not the engine's.
    """
    holdings = (
        Holding(
            instrument_id="SYNTHETIC-FUND-A",
            name="Synthetic Placeholder Fund A",
            issuer="Synthetic Placeholder Issuer A",
            sector="synthetic-sector-a",
            market_value=cad("80000.00"),
        ),
        *diversified_holdings()[1:],
    )
    product = a_product(
        instrument_id="SYNTHETIC-FUND-A",
        issuer="Synthetic Placeholder Issuer A",
        sector="synthetic-sector-a",
    )
    recommendation = a_recommendation(
        action=TradeAction.SELL,
        product=product,
        amount=cad("30000.00"),
    )
    outcome = check_concentration_breach(
        an_input(profile=a_profile(holdings=holdings), recommendation=recommendation),
    )
    assert outcome.code == RefusalCode.CONCENTRATION_BREACH


def test_concentration_breach_refuses_an_undocumented_portfolio() -> None:
    """An empty holdings tuple is read as undocumented, not as an empty portfolio."""
    outcome = check_concentration_breach(an_input(profile=a_profile(holdings=())))
    assert outcome.code == RefusalCode.MISSING_KYC_FACTOR
    assert refused(outcome).missing_factors == (FactorKey.HOLDINGS,)


def test_concentration_breach_passes_when_the_portfolio_is_fully_sold() -> None:
    """Selling the only position leaves nothing to be concentrated in."""
    holdings = diversified_holdings()[:1]
    product = a_product(
        instrument_id="SYNTHETIC-FUND-A",
        issuer="Synthetic Placeholder Issuer A",
        sector="synthetic-sector-a",
    )
    recommendation = a_recommendation(
        action=TradeAction.SELL,
        product=product,
        amount=cad("15000.00"),
    )
    outcome = check_concentration_breach(
        an_input(profile=a_profile(holdings=holdings), recommendation=recommendation),
    )
    assert isinstance(outcome, RuleCheck)


# ---------------------------------------------------------------------------
# concentration_breach: whose breach it is
#
# `concentrated_holdings` is 40,000 of FUND-A in a 100,000 portfolio: 40%, twice the
# configured limit, and a breach that exists before anything is recommended. Every case
# below refuses. What differs is what the receipt says about who caused it.
# ---------------------------------------------------------------------------


def concentration_origin(
    profile: ClientProfile | None = None,
    **recommendation_overrides: object,
) -> BreachOrigin:
    """Refuse for concentration and return the origin stated."""
    recommendation = a_recommendation(**recommendation_overrides)
    outcome = check_concentration_breach(
        an_input(
            profile=a_profile(holdings=concentrated_holdings()) if profile is None else profile,
            recommendation=recommendation,
        ),
    )
    assert refused(outcome).code == RefusalCode.CONCENTRATION_BREACH
    origin = refused(outcome).breach_origin
    assert origin is not None
    return origin


def test_concentration_breach_states_created_for_a_purchase_that_causes_it() -> None:
    """A 30,000 buy into a diversified 90,000 portfolio is the rule's original case."""
    assert concentration_origin(profile=a_profile(), amount=cad("30000.00")) is BreachOrigin.CREATED


def test_concentration_breach_states_reduced_for_a_sell_that_does_not_cure_it() -> None:
    """The eval set's finding: a disposal that halves a breach is not a trade that made it."""
    assert (
        concentration_origin(
            action=TradeAction.SELL,
            product=held_product("A"),
            amount=cad("15000.00"),
        )
        is BreachOrigin.REDUCED
    )


def test_concentration_breach_states_increased_for_a_purchase_into_the_breach() -> None:
    """Adding to the concentrated name is inherited and made worse, not created."""
    assert (
        concentration_origin(product=held_product("A"), amount=cad("5000.00"))
        is BreachOrigin.INCREASED
    )


def test_concentration_breach_states_unchanged_for_a_hold() -> None:
    """A hold is a recommendation to leave the breach exactly where it is."""
    assert (
        concentration_origin(
            action=TradeAction.HOLD,
            product=held_product("A"),
            amount=cad("40000.00"),
        )
        is BreachOrigin.UNCHANGED
    )


def test_concentration_breach_reads_a_dilution_as_unchanged_not_as_a_reduction() -> None:
    """An unrelated purchase lowers the share without disposing of anything.

    FUND-A falls from 40% to 36.36% because the portfolio grew, and the client's holding
    of it is the same 40,000 it was. This is the case that forces the classification onto
    the exposure amount: a rule reading the share would call this a reduction and hand a
    dilution the receipt a disposal earns.
    """
    assert concentration_origin() is BreachOrigin.UNCHANGED
    assert (
        "36.36%"
        in refused(
            check_concentration_breach(
                an_input(profile=a_profile(holdings=concentrated_holdings())),
            ),
        ).detail
    )


@pytest.mark.parametrize(
    ("amount", "refuses"),
    [("24999.99", True), ("25000.00", False), ("25000.01", False)],
)
def test_concentration_breach_at_the_boundary_a_disposal_has_to_reach(
    amount: str,
    *,
    refuses: bool,
) -> None:
    """Where a reducing sell stops breaching: 40,000 of A in 100,000, sell x.

    A is over the limit while 40,000 - x > 0.20 x (100,000 - x), which holds up to but
    not including x = 25,000. So 24,999.99 refuses as `reduced` and 25,000.00 cures it —
    the limit being inclusive, exactly 20% is inside it. This is the boundary the
    `reduced` origin lives against: one cent short of curing is still a refusal, and the
    origin is what tells that receipt apart from the one for causing the breach.
    """
    recommendation = a_recommendation(
        action=TradeAction.SELL,
        product=held_product("A"),
        amount=cad(amount),
    )
    outcome = check_concentration_breach(
        an_input(
            profile=a_profile(holdings=concentrated_holdings()),
            recommendation=recommendation,
        ),
    )
    assert isinstance(outcome, RuleCheck) is not refuses
    if refuses:
        assert refused(outcome).breach_origin is BreachOrigin.REDUCED


def test_concentration_breach_reports_the_most_severe_origin_when_buckets_disagree() -> None:
    """A disposal can reduce one breach and create another; the receipt says created.

    50,000 of A beside three 25,000 positions is a 125,000 portfolio: A breaches at 40%
    and the other three sit at exactly 20%, inside the inclusive limit. Selling 25,000 of
    A takes A to 25% — still over, and reduced — while shrinking the portfolio the others
    are measured against until each of them is at 25% too, over a line none of them
    moved across on its own.

    Both facts belong on the receipt, and the reason's own origin is the more severe of
    them: a recommendation that creates a breach while reducing another must not read as
    a reduction.
    """
    holdings = (
        Holding(
            instrument_id="SYNTHETIC-FUND-A",
            name="Synthetic Placeholder Fund A",
            issuer="Synthetic Placeholder Issuer A",
            sector="synthetic-sector-a",
            market_value=cad("50000.00"),
        ),
        *(
            Holding(
                instrument_id=f"SYNTHETIC-FUND-{letter}",
                name=f"Synthetic Placeholder Fund {letter}",
                issuer=f"Synthetic Placeholder Issuer {letter}",
                sector=f"synthetic-sector-{letter.lower()}",
                market_value=cad("25000.00"),
            )
            for letter in ("B", "C", "D")
        ),
    )
    recommendation = a_recommendation(
        action=TradeAction.SELL,
        product=held_product("A"),
        amount=cad("25000.00"),
    )
    outcome = check_concentration_breach(
        an_input(profile=a_profile(holdings=holdings), recommendation=recommendation),
    )
    reason = refused(outcome)
    assert reason.breach_origin is BreachOrigin.CREATED
    assert "reduced by this recommendation without curing it" in reason.detail
    assert "created by this recommendation" in reason.detail


def test_concentration_breach_states_what_each_bucket_was_before() -> None:
    """The residual and the starting point are both on the receipt, in figures."""
    recommendation = a_recommendation(
        action=TradeAction.SELL,
        product=held_product("A"),
        amount=cad("15000.00"),
    )
    detail = refused(
        check_concentration_breach(
            an_input(
                profile=a_profile(holdings=concentrated_holdings()),
                recommendation=recommendation,
            ),
        ),
    ).detail
    assert "29.41%" in detail
    assert "was 40.00%" in detail
    assert "reduces it without bringing it inside the limit" in detail


# ---------------------------------------------------------------------------
# breach_origin_of
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("origins", "expected"),
    [
        ([BreachOrigin.REDUCED], BreachOrigin.REDUCED),
        ([BreachOrigin.REDUCED, BreachOrigin.UNCHANGED], BreachOrigin.UNCHANGED),
        ([BreachOrigin.REDUCED, BreachOrigin.INCREASED], BreachOrigin.INCREASED),
        ([BreachOrigin.REDUCED, BreachOrigin.CREATED], BreachOrigin.CREATED),
        ([BreachOrigin.CREATED, BreachOrigin.REDUCED], BreachOrigin.CREATED),
        (list(BreachOrigin), BreachOrigin.CREATED),
    ],
)
def test_breach_origin_of_takes_the_most_severe(
    origins: list[BreachOrigin],
    expected: BreachOrigin,
) -> None:
    """The order is fixed and does not depend on the order the origins arrive in."""
    assert breach_origin_of(origins) is expected


def test_breach_origin_of_refuses_to_summarise_nothing() -> None:
    """There is no neutral origin, so an empty summary raises rather than inventing one."""
    with pytest.raises(ValueError, match="no breach origins"):
        breach_origin_of([])


# ---------------------------------------------------------------------------
# unsupported_rationale
# ---------------------------------------------------------------------------


def test_unsupported_rationale_passes_a_claim_the_profile_documents() -> None:
    """A claim matching the documented value passes, citing what it checked."""
    outcome = check_unsupported_rationale(an_input())
    assert passed(outcome).basis == (a_citation(),)


def test_unsupported_rationale_tolerates_whitespace_and_case() -> None:
    """A claim is compared on its content, not its formatting."""
    rationale = RecommendationRationale(
        text="Placeholder rationale.",
        cited_factors=(a_citation(value="  MEDIUM  "),),
    )
    outcome = check_unsupported_rationale(
        an_input(recommendation=a_recommendation(rationale=rationale)),
    )
    assert isinstance(outcome, RuleCheck)


@pytest.mark.parametrize(
    "claim",
    [
        a_citation(value="high"),
        a_citation(key=FactorKey.NET_WORTH, value="1000000.00 CAD"),
        a_citation(documented_on=dt.date(2024, 1, 1)),
    ],
    ids=["wrong value", "value the profile does not document", "wrong documentation date"],
)
def test_unsupported_rationale_refuses_a_claim_the_profile_does_not_support(
    claim: FactorCitation,
) -> None:
    """A near miss is unsupported; the engine does not repair the claim."""
    rationale = RecommendationRationale(text="Placeholder rationale.", cited_factors=(claim,))
    outcome = check_unsupported_rationale(
        an_input(recommendation=a_recommendation(rationale=rationale)),
    )
    assert outcome.code == RefusalCode.UNSUPPORTED_RATIONALE
    assert refused(outcome).unsupported_claims == (claim,)


def test_unsupported_rationale_refuses_a_rationale_that_cites_nothing() -> None:
    """A rationale anchored to nothing cannot be checked, so it is refused."""
    rationale = RecommendationRationale(text="Trust me.", cited_factors=())
    outcome = check_unsupported_rationale(
        an_input(recommendation=a_recommendation(rationale=rationale)),
    )
    assert outcome.code == RefusalCode.MISSING_KYC_FACTOR
    assert "cites no documented factor" in refused(outcome).detail


def test_unsupported_rationale_reports_every_unsupported_claim() -> None:
    """Two bad claims are both reported."""
    claims = (a_citation(value="high"), a_citation(key=FactorKey.ANNUAL_INCOME, value="1.00 CAD"))
    rationale = RecommendationRationale(text="Placeholder rationale.", cited_factors=claims)
    outcome = check_unsupported_rationale(
        an_input(recommendation=a_recommendation(rationale=rationale)),
    )
    assert refused(outcome).unsupported_claims == claims


# ---------------------------------------------------------------------------
# stale_profile
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("age_days", "refuses"), [(364, False), (365, False), (366, True)])
def test_stale_profile_at_the_review_interval_boundary(age_days: int, *, refuses: bool) -> None:
    """A profile exactly at the configured interval is not yet stale."""
    outcome = check_stale_profile(an_input(as_of=REVIEWED_ON + dt.timedelta(days=age_days)))
    assert isinstance(outcome, RuleCheck) is not refuses


def test_stale_profile_honours_a_configured_interval() -> None:
    """The interval is configuration, not a constant baked into the rule."""
    config = RuleConfig(profile_review_interval_days=10)
    as_of = REVIEWED_ON + dt.timedelta(days=11)
    outcome = check_stale_profile(an_input(as_of=as_of, config=config))
    assert outcome.code == RefusalCode.STALE_PROFILE
    assert refused(outcome).conflicting_factors[0].key == FactorKey.LAST_REVIEWED


def test_stale_profile_reads_the_injected_date_not_the_clock() -> None:
    """The rule's answer is a function of `as_of`, which the caller supplies."""
    stale = check_stale_profile(an_input(as_of=dt.date(2030, 1, 1)))
    fresh = check_stale_profile(an_input(as_of=REVIEWED_ON))
    assert stale.code == RefusalCode.STALE_PROFILE
    assert isinstance(fresh, RuleCheck)


# ---------------------------------------------------------------------------
# determine
# ---------------------------------------------------------------------------


def test_determine_supports_a_documented_recommendation() -> None:
    """The baseline synthetic case passes all seven rules."""
    outcome = determine(a_profile(), a_recommendation(), now=NOW)
    assert isinstance(outcome, Determination)
    assert tuple(check.code for check in outcome.checks) == tuple(code for code, _ in RULES)
    assert all(check.basis for check in outcome.checks)
    assert outcome.decided_at == NOW
    assert outcome.profile_last_reviewed == REVIEWED_ON


def test_determine_runs_every_rule_in_the_taxonomy() -> None:
    """A supported determination is proof that all seven rules ran."""
    outcome = determine(a_profile(), a_recommendation(), now=NOW)
    assert isinstance(outcome, Determination)
    assert {check.code for check in outcome.checks} == set(RefusalCode)


@pytest.mark.parametrize(
    ("field", "value", "key"),
    [
        ("risk_tolerance", None, FactorKey.RISK_TOLERANCE),
        ("time_horizon_years", None, FactorKey.TIME_HORIZON_YEARS),
        ("investment_objectives", (), FactorKey.INVESTMENT_OBJECTIVES),
    ],
)
def test_determine_refuses_a_profile_missing_each_required_factor_in_turn(
    field: str,
    value: object,
    key: FactorKey,
) -> None:
    """Whichever required factor is absent, the refusal names it exactly once."""
    outcome = refusal_for(determine(a_profile(**{field: value}), a_recommendation(), now=NOW))
    missing = next(
        reason for reason in outcome.reasons if reason.code == RefusalCode.MISSING_KYC_FACTOR
    )
    assert key in missing.missing_factors
    assert codes(outcome).count(RefusalCode.MISSING_KYC_FACTOR) == 1


def test_determine_merges_missing_factors_reported_by_several_rules() -> None:
    """Both the KYC rule and the rules that needed the factor are represented."""
    outcome = refusal_for(determine(a_profile(risk_tolerance=None), a_recommendation(), now=NOW))
    missing = next(
        reason for reason in outcome.reasons if reason.code == RefusalCode.MISSING_KYC_FACTOR
    )
    assert missing.missing_factors == (FactorKey.RISK_TOLERANCE,)
    assert "Required KYC factors not documented" in missing.detail
    assert "risk_mismatch cannot be evaluated" in missing.detail


def test_determine_reports_every_rule_that_fired() -> None:
    """A recommendation that trips five rules is refused for five reasons, not one."""
    profile = a_profile(
        risk_tolerance=RiskLevel.LOW,
        investment_objectives=(InvestmentObjective.CAPITAL_PRESERVATION,),
        time_horizon_years=Decimal("1.0"),
        last_reviewed=dt.date(2023, 1, 1),
    )
    recommendation = a_recommendation(
        product=a_product(
            risk_rating=RiskLevel.HIGH,
            lock_up_days=1000,
            redemption_frequency=RedemptionFrequency.NONE,
        ),
        amount=cad("100000.00"),
        rationale=RecommendationRationale(
            text="Placeholder rationale.",
            cited_factors=(a_citation(value="high"),),
        ),
    )
    outcome = refusal_for(determine(profile, recommendation, now=NOW))
    assert set(codes(outcome)) == {
        RefusalCode.RISK_MISMATCH,
        RefusalCode.HORIZON_MISMATCH,
        RefusalCode.LIQUIDITY_CONFLICT,
        RefusalCode.CONCENTRATION_BREACH,
        RefusalCode.UNSUPPORTED_RATIONALE,
        RefusalCode.STALE_PROFILE,
    }


def test_determine_orders_reasons_by_the_rule_order() -> None:
    """Reason order is the declared rule order, not the order refusals were noticed."""
    profile = a_profile(risk_tolerance=RiskLevel.LOW, last_reviewed=dt.date(2023, 1, 1))
    recommendation = a_recommendation(
        product=a_product(risk_rating=RiskLevel.HIGH),
        rationale=RecommendationRationale(
            text="Placeholder rationale.",
            cited_factors=(a_citation(value="low", documented_on=dt.date(2023, 1, 1)),),
        ),
    )
    outcome = refusal_for(determine(profile, recommendation, now=NOW))
    assert codes(outcome) == (RefusalCode.RISK_MISMATCH, RefusalCode.STALE_PROFILE)


def test_determine_tells_a_created_breach_from_an_inherited_one() -> None:
    """Two receipts, one code, two findings — which is the whole of the change.

    A 30,000 purchase into a diversified portfolio and a 15,000 disposal out of a
    concentrated one both refuse under `concentration_breach`. Before the origin existed
    those two receipts said the same thing, so an advisor unwinding a position the client
    arrived with was recorded exactly as one who had built it. Neither is supported now
    and neither was before: what changed is that the receipts differ.
    """
    created = determine(
        a_profile(),
        a_recommendation(amount=cad("30000.00")),
        now=NOW,
    )
    reduced = determine(
        a_profile(holdings=concentrated_holdings()),
        a_recommendation(
            action=TradeAction.SELL,
            product=held_product("A"),
            amount=cad("15000.00"),
        ),
        now=NOW,
    )
    assert codes(refusal_for(created)) == (RefusalCode.CONCENTRATION_BREACH,)
    assert codes(refusal_for(reduced)) == (RefusalCode.CONCENTRATION_BREACH,)
    assert refusal_for(created).reasons[0].breach_origin is BreachOrigin.CREATED
    assert refusal_for(reduced).reasons[0].breach_origin is BreachOrigin.REDUCED


def test_determine_leaves_the_residual_breach_on_the_reduced_receipt() -> None:
    """A reduced breach is still reported as a breach, with what is left of it.

    The guard against the change having quietly become a pass: the receipt is a refusal,
    the code is unchanged, and 29.41% of the portfolio in one name appears on it in
    figures rather than being summarised away as an improvement.
    """
    refusal = refusal_for(
        determine(
            a_profile(holdings=concentrated_holdings()),
            a_recommendation(
                action=TradeAction.SELL,
                product=held_product("A"),
                amount=cad("15000.00"),
            ),
            now=NOW,
        ),
    )
    assert refusal.outcome == "refused"
    assert "29.41%" in refusal.reasons[0].detail


def test_determine_states_an_origin_on_exactly_the_reasons_that_may_carry_one() -> None:
    """Across a refusal carrying several codes, only the portfolio-reading ones say whose."""
    refusal = refusal_for(
        determine(
            a_profile(
                holdings=concentrated_holdings(),
                liquid_net_worth=cad("30000.00"),
                last_reviewed=dt.date(2024, 1, 1),
            ),
            a_recommendation(
                product=a_product(risk_rating=RiskLevel.HIGH),
                amount=cad("30000.00"),
                rationale=RecommendationRationale(
                    text="Placeholder rationale.",
                    cited_factors=(a_citation(documented_on=dt.date(2024, 1, 1)),),
                ),
            ),
            now=NOW,
        ),
    )
    stated = {reason.code for reason in refusal.reasons if reason.breach_origin is not None}
    assert stated == {RefusalCode.RISK_MISMATCH, RefusalCode.CONCENTRATION_BREACH}
    assert len(refusal.reasons) > len(stated)


@pytest.mark.parametrize(
    "profile_overrides",
    [{}, {"risk_tolerance": None}, {"last_reviewed": dt.date(2020, 1, 1)}],
    ids=["supported", "refused for a missing factor", "refused as stale"],
)
def test_determine_is_deterministic(profile_overrides: dict[str, object]) -> None:
    """The same inputs produce a byte-identical outcome, reason ordering included."""
    profile = a_profile(**profile_overrides)
    recommendation = a_recommendation()
    first = determine(profile, recommendation, now=NOW)
    second = determine(profile, recommendation, now=NOW)
    assert first.model_dump_json() == second.model_dump_json()
    assert first.receipt_id == second.receipt_id


def test_determine_binds_the_receipt_to_its_inputs() -> None:
    """A different profile yields a different digest and a different receipt id."""
    recommendation = a_recommendation()
    first = determine(a_profile(), recommendation, now=NOW)
    second = determine(a_profile(annual_income=cad("90001.00")), recommendation, now=NOW)
    assert first.profile_digest != second.profile_digest
    assert first.receipt_id != second.receipt_id
    assert first.recommendation_digest == second.recommendation_digest


def test_determine_reads_no_clock() -> None:
    """The evaluation time is injected, so a later `now` can change the outcome."""
    profile = a_profile()
    recommendation = a_recommendation()
    assert isinstance(determine(profile, recommendation, now=NOW), Determination)
    later = dt.datetime(2030, 1, 1, tzinfo=dt.UTC)
    assert codes(refusal_for(determine(profile, recommendation, now=later))) == (
        RefusalCode.STALE_PROFILE,
    )


@pytest.mark.parametrize(
    ("profile", "recommendation", "now", "message"),
    [
        (
            a_profile(client_id="SYNTHETIC-0002"),
            a_recommendation(),
            NOW,
            "profile is for",
        ),
        (
            a_profile(),
            a_recommendation(amount=Money(amount=Decimal(1), currency=Currency.USD)),
            NOW,
            "no exchange rate",
        ),
        (
            a_profile(),
            a_recommendation(),
            dt.datetime(2025, 7, 1, 12, 0),  # noqa: DTZ001
            "UTC-aware",
        ),
        (
            a_profile(),
            a_recommendation(),
            dt.datetime(2025, 1, 1, tzinfo=dt.UTC),
            "precedes the profile",
        ),
    ],
    ids=["other client", "other currency", "naive datetime", "evaluation before review"],
)
def test_determine_rejects_inputs_it_cannot_determine_over(
    profile: ClientProfile,
    recommendation: Recommendation,
    now: dt.datetime,
    message: str,
) -> None:
    """These are caller errors, not suitability questions, and raise rather than refuse."""
    with pytest.raises(ValueError, match=message):
        determine(profile, recommendation, now=now)
