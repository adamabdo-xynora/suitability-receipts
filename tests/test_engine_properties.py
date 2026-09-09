"""Property-based tests for the determination engine.

`test_engine.py` pins each rule at its threshold and one unit either side. This file
states what the engine promises across the whole space the models admit, and generates
that space rather than choosing points in it.

Two rules govern everything below.

*The expected answer is restated independently.* The `_SPEC_*` tables and
`documented_value` are a second, deliberately separate statement of the specification.
Nothing here imports `OBJECTIVE_RISK_CEILING`, `RISK_LEVEL_RANK`,
`REDEMPTION_DAYS_TO_LIQUIDITY`, `CONCENTRATION_LIMIT_FRACTION` or the engine's
renderers, because a property that asks the engine what the answer should be agrees
with the engine by construction and tests nothing. The cost is duplication that has to
be kept in step by hand; the benefit is that changing a number in `engine.py` fails
here instead of silently redefining the promise.

*Every property is two-sided wherever the specification is.* "Refuses when X" passes on
an engine that refuses everything, and an engine that refuses everything is a failure
this project exists to catch. So each rule property asserts both that the code appears
when it must and that it does not appear when it must not. Each property records that
reasoning in a `Tightening:` note.

The strategies are as much the deliverable as the properties. `_supportable_cases`
exists because a uniformly generated case is almost never supported, and properties
about a `Determination` that never see one pass vacuously;
`test_the_generated_space_reaches_*` fails loudly if any boundary stops being reachable.

All client data here is synthetic: identifiers carry a `SYNTHETIC-` prefix and names are
placeholders. No real client data and no credentials appear in this repository.
"""

import datetime as dt
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

import pytest
from hypothesis import event, example, find, given, settings
from hypothesis import strategies as st

from suitability_receipts import (
    BREACH_ORIGIN_CODES,
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
    SuitabilityOutcome,
    TradeAction,
    UpcomingExpense,
    determine,
)
from suitability_receipts.engine import RULES, _exposures, check_unsupported_rationale

# ---------------------------------------------------------------------------
# The specification, restated independently of the engine
# ---------------------------------------------------------------------------

SPEC_RISK_ORDER: tuple[RiskLevel, ...] = (
    RiskLevel.LOW,
    RiskLevel.LOW_TO_MEDIUM,
    RiskLevel.MEDIUM,
    RiskLevel.MEDIUM_TO_HIGH,
    RiskLevel.HIGH,
)
"""The risk scale, least to most. Restated so a mutant to `RISK_LEVEL_RANK` fails."""

SPEC_OBJECTIVE_CEILING: dict[InvestmentObjective, RiskLevel] = {
    InvestmentObjective.CAPITAL_PRESERVATION: RiskLevel.LOW,
    InvestmentObjective.INCOME: RiskLevel.LOW_TO_MEDIUM,
    InvestmentObjective.BALANCED: RiskLevel.MEDIUM,
    InvestmentObjective.GROWTH: RiskLevel.MEDIUM_TO_HIGH,
    InvestmentObjective.SPECULATION: RiskLevel.HIGH,
}
"""The highest product risk each objective supports. Restated, not imported."""

SPEC_REDEMPTION_WAIT_DAYS: dict[RedemptionFrequency, int | None] = {
    RedemptionFrequency.DAILY: 1,
    RedemptionFrequency.MONTHLY: 31,
    RedemptionFrequency.QUARTERLY: 92,
    RedemptionFrequency.ANNUAL: 366,
    RedemptionFrequency.NONE: None,
}
"""Worst-case wait for a redemption window, in days. Restated, not imported."""

SPEC_REQUIRED_KYC: tuple[FactorKey, ...] = (
    FactorKey.RISK_TOLERANCE,
    FactorKey.TIME_HORIZON_YEARS,
    FactorKey.INVESTMENT_OBJECTIVES,
)
"""The KYC floor, in the order refusals report it."""

SPEC_CONCENTRATION_LIMIT = Decimal("0.20")
SPEC_REVIEW_INTERVAL_DAYS = 365
SPEC_DAYS_PER_YEAR = Decimal(365)
SPEC_ENGINE_VERSION = "0.1.0"
SPEC_RECEIPT_ID_LENGTH = len("RCPT-") + 32

ZERO = Decimal(0)
CENT = Decimal("0.01")
MAX_AMOUNT = Decimal("9999999999999999.99")
"""The largest `Money.amount` the models accept: 18 digits, two decimal places."""


def _rank(level: RiskLevel) -> int:
    """Return the position of `level` on the risk scale."""
    return SPEC_RISK_ORDER.index(level)


def effective_ceiling(profile: ClientProfile) -> int | None:
    """Return the risk rank the profile supports, or `None` if it cannot be computed.

    The engine's promise, restated: the ceiling is the *more conservative* of the
    documented risk tolerance and the ceiling implied by each documented objective. A
    property written against the tolerance alone would call a refusal a false positive
    every time a conservative objective sits on an aggressive profile.
    """
    if profile.risk_tolerance is None or not profile.investment_objectives:
        return None
    return min(
        [
            _rank(profile.risk_tolerance),
            *(_rank(SPEC_OBJECTIVE_CEILING[o]) for o in profile.investment_objectives),
        ],
    )


# --- the documented value of each citable factor, re-derived from the profile ---


def _spec_money(amount: Money) -> str:
    """Render a monetary amount as documented."""
    return f"{amount.amount} {amount.currency.value}"


def _spec_liquidity(profile: ClientProfile) -> str | None:
    """Render the documented liquidity requirement."""
    requirement = profile.liquidity_requirement
    if requirement is None:
        return None
    expenses = requirement.upcoming_expenses
    total = sum((expense.amount.amount for expense in expenses), ZERO)
    return (
        f"emergency reserve {_spec_money(requirement.emergency_reserve)}; "
        f"upcoming expenses: {len(expenses)} totalling {total} {profile.currency.value}"
    )


def _spec_holdings(profile: ClientProfile) -> str | None:
    """Render the documented portfolio, or `None` when it is undocumented.

    An empty tuple is undocumented rather than empty: it is the value a profile that
    never recorded a portfolio carries, and reading it as "an empty portfolio" is the
    reading that lets any purchase through.
    """
    if not profile.holdings:
        return None
    total = sum((holding.market_value.amount for holding in profile.holdings), ZERO)
    return f"{len(profile.holdings)} holdings totalling {total} {profile.currency.value}"


_SPEC_RENDERERS: dict[FactorKey, Callable[[ClientProfile], str | None]] = {
    FactorKey.RISK_TOLERANCE: lambda p: (
        None if p.risk_tolerance is None else p.risk_tolerance.value
    ),
    FactorKey.TIME_HORIZON_YEARS: lambda p: (
        None if p.time_horizon_years is None else str(p.time_horizon_years)
    ),
    FactorKey.INVESTMENT_OBJECTIVES: lambda p: (
        ", ".join(o.value for o in p.investment_objectives) if p.investment_objectives else None
    ),
    FactorKey.INVESTMENT_KNOWLEDGE: lambda p: (
        None if p.investment_knowledge is None else p.investment_knowledge.value
    ),
    FactorKey.LIQUIDITY_REQUIREMENT: _spec_liquidity,
    FactorKey.ANNUAL_INCOME: lambda p: (
        None if p.annual_income is None else _spec_money(p.annual_income)
    ),
    FactorKey.NET_WORTH: lambda p: None if p.net_worth is None else _spec_money(p.net_worth),
    FactorKey.LIQUID_NET_WORTH: lambda p: (
        None if p.liquid_net_worth is None else _spec_money(p.liquid_net_worth)
    ),
    FactorKey.HOLDINGS: _spec_holdings,
    FactorKey.LAST_REVIEWED: lambda p: p.last_reviewed.isoformat(),
}

if set(_SPEC_RENDERERS) != set(FactorKey):  # pragma: no cover - a guard, not a branch
    _uncovered = sorted(set(FactorKey) - set(_SPEC_RENDERERS))
    _message = f"_SPEC_RENDERERS does not cover {_uncovered}"
    raise RuntimeError(_message)


def documented_value(profile: ClientProfile, key: FactorKey) -> str | None:
    """Return what `profile` documents for `key`, or `None` if it documents nothing.

    Derived from the profile alone, so that a citation can be checked against the
    profile rather than believed.
    """
    return _SPEC_RENDERERS[key](profile)


def _spec_holdings_exposure(profile: ClientProfile) -> dict[tuple[str, str], Decimal]:
    """Return exposure per (dimension, value) for the documented portfolio alone.

    Used to aim generated amounts at the concentration boundary; the engine's own
    post-recommendation exposure is a separate calculation.
    """
    exposure: dict[tuple[str, str], Decimal] = {}
    for holding in profile.holdings:
        value = holding.market_value.amount
        for bucket in (
            ("instrument", holding.instrument_id),
            ("issuer", holding.issuer),
            *((("sector", holding.sector),) if holding.sector is not None else ()),
        ):
            exposure[bucket] = exposure.get(bucket, ZERO) + value
    return exposure


def _spec_required_liquidity(profile: ClientProfile) -> Decimal | None:
    """Return the documented liquidity the recommendation must leave intact."""
    requirement = profile.liquidity_requirement
    if requirement is None:
        return None
    return requirement.emergency_reserve.amount + sum(
        (expense.amount.amount for expense in requirement.upcoming_expenses),
        ZERO,
    )


# ---------------------------------------------------------------------------
# The generated space
# ---------------------------------------------------------------------------

CLIENT_ID = "SYNTHETIC-0001"
RECOMMENDATION_ID = "SYNTHETIC-REC-0001"

# Small pools, on purpose. Random identifiers would never collide, and a portfolio whose
# instruments, issuers and sectors never coincide never exercises the aggregation that
# `concentration_breach` is made of.
#
# The names also interleave when sorted: an issuer that sorts before every instrument id,
# and a sector that sorts after both. An earlier draft named them so that instruments,
# issuers and sectors happened to sort in that same order, which made "sorted by
# dimension, then by value" and "sorted by value alone" indistinguishable — a refusal
# that listed its evidence in the wrong order would have read as correct.
INSTRUMENTS = tuple(f"SYNTHETIC-FUND-{letter}" for letter in "ABCDEFGHIJ")
ISSUERS = ("Acme SYNTHETIC Trust", "SYNTHETIC Holdings", "zenith SYNTHETIC Group")
SECTORS = ("Aggregates (SYNTHETIC)", "SYNTHETIC energy", "zinc (SYNTHETIC)")

# `ClientProfile` rejects a `last_reviewed` after today, checked against the real clock.
# Bounding generation in the settled past keeps the space stable on any day it is run.
EARLIEST_REVIEW = dt.date(2015, 1, 1)
LATEST_REVIEW = dt.date(2025, 1, 1)

# Ages that straddle the configured review interval, plus a spread of ordinary ones.
REVIEW_AGES = (0, 1, 364, 365, 366, 367, 1000)


def amounts(
    min_value: Decimal = ZERO,
    max_value: Decimal = MAX_AMOUNT,
) -> st.SearchStrategy[Decimal]:
    """Return a strategy for exact amounts across the range `Money` permits."""
    return st.one_of(
        st.decimals(min_value=min_value, max_value=max_value, places=2),
        st.sampled_from(
            [
                value
                for exponent in range(18)
                if min_value <= (value := Decimal(10) ** exponent / Decimal(100)) <= max_value
            ],
        ),
    )


def money(currency: Currency, min_value: Decimal = ZERO) -> st.SearchStrategy[Money]:
    """Return a strategy for `Money` in `currency`."""
    return amounts(min_value=min_value).map(lambda a: Money(amount=a, currency=currency))


def horizons() -> st.SearchStrategy[Decimal]:
    """Return a strategy for documented time horizons in years."""
    return st.decimals(min_value=Decimal("0.1"), max_value=Decimal(100), places=1)


def objectives() -> st.SearchStrategy[tuple[InvestmentObjective, ...]]:
    """Return a strategy for documented objectives, including sets in tension.

    The tension pairs are sampled explicitly rather than left to chance: a profile
    documenting both capital preservation and speculation is the case the engine's
    "resolve against the recommendation" promise is about, and it is rare in a uniform
    draw over subsets.
    """
    tension = (
        (InvestmentObjective.CAPITAL_PRESERVATION, InvestmentObjective.SPECULATION),
        (InvestmentObjective.SPECULATION, InvestmentObjective.CAPITAL_PRESERVATION),
        (InvestmentObjective.GROWTH, InvestmentObjective.CAPITAL_PRESERVATION),
        (InvestmentObjective.INCOME, InvestmentObjective.SPECULATION),
    )
    return st.one_of(
        st.sampled_from(tension),
        st.lists(
            st.sampled_from(list(InvestmentObjective)),
            max_size=len(InvestmentObjective),
            unique=True,
        ).map(tuple),
    )


@st.composite
def liquidity_requirements(draw: st.DrawFn, currency: Currency) -> LiquidityRequirement:
    """Return a strategy for documented liquidity needs in `currency`."""
    expenses = draw(
        st.lists(
            st.builds(
                UpcomingExpense,
                description=st.just("SYNTHETIC placeholder expense"),
                amount=money(currency),
                expected_on=st.dates(min_value=EARLIEST_REVIEW, max_value=dt.date(2035, 12, 31)),
            ),
            max_size=3,
        ),
    )
    return LiquidityRequirement(
        emergency_reserve=draw(money(currency)),
        upcoming_expenses=tuple(expenses),
    )


@st.composite
def holdings(draw: st.DrawFn, currency: Currency) -> tuple[Holding, ...]:
    """Return a strategy for portfolios from empty to many positions."""
    instruments = draw(
        st.lists(st.sampled_from(INSTRUMENTS), max_size=len(INSTRUMENTS), unique=True),
    )
    return tuple(
        Holding(
            instrument_id=instrument,
            name=f"Synthetic Placeholder {instrument}",
            issuer=draw(st.sampled_from(ISSUERS)),
            sector=draw(st.none() | st.sampled_from(SECTORS)),
            market_value=Money(amount=draw(amounts()), currency=currency),
        )
        for instrument in instruments
    )


@st.composite
def profiles(draw: st.DrawFn) -> ClientProfile:
    """Return a strategy for client profiles across the documented space.

    Every optional factor is drawn as "documented or not", so each one is absent on some
    fraction of examples, and `risk_tolerance` is drawn independently of
    `investment_objectives` so that profiles in tension arise without being asked for.
    """
    currency = draw(st.sampled_from(list(Currency)))
    net_worth = draw(st.none() | money(currency))
    liquid = draw(st.none() | money(currency))
    if net_worth is not None and liquid is not None and liquid.amount > net_worth.amount:
        liquid = Money(amount=net_worth.amount, currency=currency)
    return ClientProfile(
        client_id=CLIENT_ID,
        last_reviewed=draw(st.dates(min_value=EARLIEST_REVIEW, max_value=LATEST_REVIEW)),
        currency=currency,
        risk_tolerance=draw(st.none() | st.sampled_from(list(RiskLevel))),
        time_horizon_years=draw(st.none() | horizons()),
        investment_objectives=draw(objectives()),
        investment_knowledge=draw(st.none() | st.sampled_from(list(InvestmentKnowledge))),
        liquidity_requirement=draw(st.none() | liquidity_requirements(currency)),
        annual_income=draw(st.none() | money(currency)),
        net_worth=net_worth,
        liquid_net_worth=liquid,
        holdings=draw(holdings(currency)),
    )


@st.composite
def products(draw: st.DrawFn, profile: ClientProfile) -> Product:
    """Return a strategy for products, with terms aimed at the horizon boundary.

    A uniformly drawn `lock_up_days` almost never lands on the documented horizon, so
    the values that straddle it are offered explicitly.
    """
    horizon = profile.time_horizon_years
    lock_ups = [0, 1, 365, 366, 3650]
    periods: list[Decimal | None] = [None, Decimal(1), Decimal(100)]
    if horizon is not None:
        horizon_days = int(horizon * SPEC_DAYS_PER_YEAR)
        lock_ups += [horizon_days - 1, horizon_days, horizon_days + 1]
        periods += [
            period
            for period in (horizon - Decimal("0.1"), horizon, horizon + Decimal("0.1"))
            if ZERO < period <= Decimal(100)
        ]
    return Product(
        instrument_id=draw(st.sampled_from(INSTRUMENTS)),
        name="Synthetic Placeholder Product",
        issuer=draw(st.sampled_from(ISSUERS)),
        sector=draw(st.none() | st.sampled_from(SECTORS)),
        risk_rating=draw(st.sampled_from(list(RiskLevel))),
        risk_rating_source="SYNTHETIC placeholder fund facts",
        lock_up_days=draw(st.sampled_from(sorted({days for days in lock_ups if days >= 0}))),
        redemption_frequency=draw(st.sampled_from(list(RedemptionFrequency))),
        recommended_holding_period_years=draw(st.sampled_from(periods)),
    )


def _straddle(value: Decimal) -> list[Decimal]:
    """Return amounts either side of `value`, in cents."""
    base = value.quantize(CENT, rounding=ROUND_DOWN)
    return [base + step * CENT for step in (-1, 0, 1, 2)]


def boundary_amounts(
    profile: ClientProfile,
    product: Product,
    action: TradeAction,
) -> list[Decimal]:
    """Return amounts that sit on a rule's boundary for this profile and product.

    Without this, an amount drawn uniformly from eighteen digits of range lands on the
    concentration or liquidity threshold with probability zero, and the properties over
    those rules only ever see the easy side of them.
    """
    candidates: list[Decimal] = []
    total = sum((holding.market_value.amount for holding in profile.holdings), ZERO)
    if action in {TradeAction.BUY, TradeAction.SWITCH} and total > ZERO:
        exposure = _spec_holdings_exposure(profile)
        buckets = [("instrument", product.instrument_id), ("issuer", product.issuer)]
        if product.sector is not None:
            buckets.append(("sector", product.sector))
        for bucket in buckets:
            head_room = SPEC_CONCENTRATION_LIMIT * total - exposure.get(bucket, ZERO)
            candidates += _straddle(head_room / (1 - SPEC_CONCENTRATION_LIMIT))
    required = _spec_required_liquidity(profile)
    if required is not None and profile.liquid_net_worth is not None:
        candidates += _straddle(profile.liquid_net_worth.amount - required)
    held = _spec_holdings_exposure(profile).get(("instrument", product.instrument_id))
    if held is not None:
        candidates += _straddle(held)
    return [value for value in candidates if CENT <= value <= MAX_AMOUNT]


def claims(profile: ClientProfile) -> st.SearchStrategy[FactorCitation]:
    """Return a strategy for the claims a rationale might make about `profile`.

    Three kinds, deliberately: claims the profile supports exactly, near misses that a
    charitable reader would accept (right value, wrong documentation date; right shape,
    wrong content), and claims about factors the profile does not document at all.
    """
    supported = [
        FactorCitation(key=key, value=value, documented_on=profile.last_reviewed)
        for key in FactorKey
        if (value := documented_value(profile, key)) is not None
    ]
    near_misses = [
        FactorCitation(
            key=claim.key,
            value=f"{claim.value} (not what the profile documents)",
            documented_on=claim.documented_on,
        )
        for claim in supported
    ] + [
        FactorCitation(
            key=claim.key,
            value=claim.value,
            documented_on=claim.documented_on - dt.timedelta(days=1),
        )
        for claim in supported
    ]
    undocumented = [
        FactorCitation(
            key=key,
            value="asserted, not documented",
            documented_on=profile.last_reviewed,
        )
        for key in FactorKey
        if documented_value(profile, key) is None
    ]
    return st.sampled_from(supported + near_misses + undocumented)


@st.composite
def rationales(draw: st.DrawFn, profile: ClientProfile) -> RecommendationRationale:
    """Return a strategy for rationales, from citing nothing to citing three claims."""
    cited = draw(st.lists(claims(profile), max_size=3, unique=True))
    return RecommendationRationale(
        text="Synthetic placeholder rationale.",
        cited_factors=tuple(cited),
    )


@st.composite
def recommendations(draw: st.DrawFn, profile: ClientProfile) -> Recommendation:
    """Return a strategy for recommendations the engine will determine over."""
    product = draw(products(profile))
    action = draw(st.sampled_from(list(TradeAction)))
    boundaries = boundary_amounts(profile, product, action)
    amount = draw(
        st.one_of(st.sampled_from(boundaries), amounts(min_value=CENT))
        if boundaries
        else amounts(min_value=CENT),
    )
    return Recommendation(
        recommendation_id=RECOMMENDATION_ID,
        client_id=profile.client_id,
        action=action,
        product=product,
        amount=Money(amount=amount, currency=profile.currency),
        proposed_on=profile.last_reviewed,
        rationale=draw(rationales(profile)),
    )


def evaluation_times(last_reviewed: dt.date) -> st.SearchStrategy[dt.datetime]:
    """Return a strategy for injected evaluation times the engine accepts.

    UTC-aware and never before the review date, because anything else is a documented
    `ValueError` and out of scope for these properties.
    """
    ages = st.one_of(st.sampled_from(REVIEW_AGES), st.integers(min_value=0, max_value=4000))
    return st.builds(
        lambda age, hour: dt.datetime.combine(
            last_reviewed + dt.timedelta(days=age),
            dt.time(hour),
            tzinfo=dt.UTC,
        ),
        ages,
        st.sampled_from((0, 12, 23)),
    )


@dataclass(frozen=True, slots=True)
class Case:
    """A profile, a recommendation for it, and an evaluation time the engine accepts."""

    profile: ClientProfile
    recommendation: Recommendation
    now: dt.datetime


@st.composite
def arbitrary_cases(draw: st.DrawFn) -> Case:
    """Return a strategy over the whole space, favouring nothing."""
    profile = draw(profiles())
    return Case(
        profile=profile,
        recommendation=draw(recommendations(profile)),
        now=draw(evaluation_times(profile.last_reviewed)),
    )


@st.composite
def _supportable_profile(draw: st.DrawFn, currency: Currency, amount: Decimal) -> ClientProfile:
    """Return a profile documenting everything, sized to fund `amount`."""
    tolerance = draw(st.sampled_from(list(RiskLevel)))
    documented_objectives = draw(
        st.lists(st.sampled_from(list(InvestmentObjective)), min_size=1, max_size=3, unique=True),
    )
    reserve = Decimal(draw(st.integers(min_value=0, max_value=50_000)))
    expenses = tuple(
        UpcomingExpense(
            description="SYNTHETIC placeholder expense",
            amount=Money(amount=Decimal(value), currency=currency),
            expected_on=dt.date(2030, 1, 15),
        )
        for value in draw(st.lists(st.integers(min_value=0, max_value=10_000), max_size=2))
    )
    required = reserve + sum((expense.amount.amount for expense in expenses), ZERO)
    # Leaves exactly the documented requirement when the slack is zero, which is the
    # liquidity rule's boundary rather than a comfortable distance from it.
    slack = Decimal(draw(st.integers(min_value=0, max_value=1000)))
    liquid = required + amount + slack
    return ClientProfile(
        client_id=CLIENT_ID,
        last_reviewed=draw(st.dates(min_value=EARLIEST_REVIEW, max_value=LATEST_REVIEW)),
        currency=currency,
        risk_tolerance=tolerance,
        time_horizon_years=Decimal(draw(st.integers(min_value=2, max_value=100))),
        investment_objectives=tuple(documented_objectives),
        investment_knowledge=draw(st.sampled_from(list(InvestmentKnowledge))),
        liquidity_requirement=LiquidityRequirement(
            emergency_reserve=Money(amount=reserve, currency=currency),
            upcoming_expenses=expenses,
        ),
        annual_income=Money(amount=Decimal(90_000), currency=currency),
        net_worth=Money(
            amount=liquid + Decimal(draw(st.integers(min_value=0, max_value=100_000))),
            currency=currency,
        ),
        liquid_net_worth=Money(amount=liquid, currency=currency),
        holdings=(),
    )


@st.composite
def supportable_cases(draw: st.DrawFn) -> Case:
    """Return a strategy for cases built to be supported, or to just miss.

    A uniformly generated case trips at least one of seven rules almost always, so the
    properties about a `Determination` — that it invents no evidence, that its checks
    cite what they consulted — would pass without ever seeing one. These cases document
    every factor, hold a diversified portfolio, and size the recommendation at the
    concentration boundary, so that the supported side of every rule is reached and the
    boundary itself is landed on exactly rather than approached.
    """
    currency = draw(st.sampled_from(list(Currency)))
    count = draw(st.integers(min_value=5, max_value=8))
    unit = Decimal(draw(st.integers(min_value=1, max_value=10_000))) * 100
    total = unit * count
    # A buy of a quarter of the portfolio is exactly 20% of the portfolio it creates,
    # and five or more equal positions each sit under the limit whatever is added.
    boundary = total / 4
    amount = draw(st.sampled_from((CENT, unit, boundary - CENT, boundary, boundary + CENT)))
    profile = draw(_supportable_profile(currency, amount))
    horizon = profile.time_horizon_years
    assert horizon is not None  # documented by _supportable_profile
    horizon_days = int(horizon * SPEC_DAYS_PER_YEAR)
    ceiling = effective_ceiling(profile)
    assert ceiling is not None  # documented by _supportable_profile
    profile = profile.model_copy(
        update={
            "holdings": tuple(
                Holding(
                    instrument_id=f"SYNTHETIC-DIVERSIFIED-{index}",
                    name=f"Synthetic Placeholder Fund {index}",
                    issuer=f"Synthetic Diversified Issuer {index}",
                    sector=f"synthetic-diversified-sector-{index}",
                    market_value=Money(amount=unit, currency=currency),
                )
                for index in range(count)
            ),
        },
    )
    product = Product(
        instrument_id="SYNTHETIC-FUND-NEW",
        name="Synthetic Placeholder Fund New",
        issuer="Synthetic New Issuer",
        sector=draw(st.none() | st.just("synthetic-new-sector")),
        risk_rating=draw(st.sampled_from(SPEC_RISK_ORDER[: ceiling + 1])),
        risk_rating_source="SYNTHETIC placeholder fund facts",
        lock_up_days=draw(st.sampled_from((0, 1, horizon_days - 1, horizon_days))),
        redemption_frequency=draw(
            st.sampled_from(
                (
                    RedemptionFrequency.DAILY,
                    RedemptionFrequency.MONTHLY,
                    RedemptionFrequency.QUARTERLY,
                    RedemptionFrequency.ANNUAL,
                ),
            ),
        ),
        recommended_holding_period_years=draw(st.sampled_from((None, Decimal(1), horizon))),
    )
    supported_claims = [
        FactorCitation(key=key, value=value, documented_on=profile.last_reviewed)
        for key in FactorKey
        if (value := documented_value(profile, key)) is not None
    ]
    cited = draw(st.lists(st.sampled_from(supported_claims), min_size=1, max_size=3, unique=True))
    recommendation = Recommendation(
        recommendation_id=RECOMMENDATION_ID,
        client_id=profile.client_id,
        action=draw(st.sampled_from(list(TradeAction))),
        product=product,
        amount=Money(amount=amount, currency=currency),
        proposed_on=profile.last_reviewed,
        rationale=RecommendationRationale(
            text="Synthetic placeholder rationale.",
            cited_factors=tuple(cited),
        ),
    )
    age = draw(st.sampled_from((0, 1, 364, 365, 366)))
    now = dt.datetime.combine(
        profile.last_reviewed + dt.timedelta(days=age),
        dt.time(12),
        tzinfo=dt.UTC,
    )
    return Case(profile=profile, recommendation=recommendation, now=now)


CASES = st.one_of(arbitrary_cases(), supportable_cases())
"""The generated space: unbiased cases, and cases built to reach the supported side."""


# ---------------------------------------------------------------------------
# Reading an outcome
# ---------------------------------------------------------------------------


def codes(outcome: SuitabilityOutcome) -> tuple[RefusalCode, ...]:
    """Return the refusal codes an outcome carries; a determination carries none."""
    if isinstance(outcome, Determination):
        return ()
    return tuple(reason.code for reason in outcome.reasons)


def decide(case: Case) -> SuitabilityOutcome:
    """Determine `case`, recording for the run's statistics which way it went."""
    outcome = determine(case.profile, case.recommendation, now=case.now)
    event(f"outcome: {'supported' if isinstance(outcome, Determination) else 'refused'}")
    return outcome


# ---------------------------------------------------------------------------
# 1. Soundness of the risk rule
# ---------------------------------------------------------------------------


@given(CASES)
def test_a_product_above_the_effective_ceiling_is_always_refused(case: Case) -> None:
    """The risk rule fires exactly when the product exceeds the *effective* ceiling.

    The ceiling is the more conservative of the documented tolerance and the ceiling
    each documented objective implies, computed here by `effective_ceiling` from the
    profile alone. A property written against the documented tolerance by itself would
    be wrong in both directions: it would call a refusal spurious whenever a
    conservative objective sits on an aggressive tolerance, and it would demand a
    refusal the engine correctly does not make.

    Tightening: "a product above the ceiling is refused" alone passes on an engine that
    refuses everything, and on one whose ceiling is always `LOW`. So the property is an
    equivalence — at or below the ceiling, `risk_mismatch` must be absent — and it
    checks that the refusal carries the factors it weighed, so an engine cannot satisfy
    it by refusing with empty evidence. Since the refusal states what it does to the
    client's position in the product, the portfolio is among those factors — but only
    when the profile documents one, because a rule may not cite a fact it could not read.
    """
    ceiling = effective_ceiling(case.profile)
    outcome = decide(case)
    if ceiling is None:
        event("risk: ceiling undeterminable")
        assert RefusalCode.RISK_MISMATCH not in codes(outcome)
        return
    exceeds = _rank(case.recommendation.product.risk_rating) > ceiling
    event(f"risk: product {'above' if exceeds else 'within'} the effective ceiling")
    assert (RefusalCode.RISK_MISMATCH in codes(outcome)) is exceeds
    if exceeds:
        assert isinstance(outcome, Refusal)
        reason = next(r for r in outcome.reasons if r.code == RefusalCode.RISK_MISMATCH)
        weighed = (FactorKey.RISK_TOLERANCE, FactorKey.INVESTMENT_OBJECTIVES)
        portfolio = (FactorKey.HOLDINGS,) if case.profile.holdings else ()
        assert tuple(citation.key for citation in reason.conflicting_factors) == (
            *weighed,
            *portfolio,
        )


@given(CASES)
def test_a_documented_objective_never_raises_the_ceiling(case: Case) -> None:
    """No objective licenses risk above the documented tolerance.

    Tightening: this is implied by the equivalence above, but stated separately because
    it is the direction a plausible bug takes — reading the objective as the ceiling
    instead of as one candidate for it. Stated on its own it fails on `max` where the
    engine uses `min`, without depending on the rest of the equivalence holding.
    """
    tolerance = case.profile.risk_tolerance
    if tolerance is None or not case.profile.investment_objectives:
        return
    ceiling = effective_ceiling(case.profile)
    assert ceiling is not None
    assert ceiling <= _rank(tolerance)
    if _rank(case.recommendation.product.risk_rating) > _rank(tolerance):
        assert RefusalCode.RISK_MISMATCH in codes(decide(case))


# ---------------------------------------------------------------------------
# 2. Missing KYC dominates
# ---------------------------------------------------------------------------


def expected_missing_factors(case: Case) -> frozenset[FactorKey]:
    """Return every factor the engine should report as undocumented.

    The floor is `REQUIRED_KYC_FACTORS`, but individual rules need more than the floor
    and say so, and a rationale that cites nothing reports the whole floor as missing
    because nothing anchors it to the profile.
    """
    profile = case.profile
    missing = {key for key in SPEC_REQUIRED_KYC if documented_value(profile, key) is None}
    missing |= {
        key
        for key in (
            FactorKey.LIQUID_NET_WORTH,
            FactorKey.LIQUIDITY_REQUIREMENT,
            FactorKey.HOLDINGS,
        )
        if documented_value(profile, key) is None
    }
    if not case.recommendation.rationale.cited_factors:
        missing |= set(SPEC_REQUIRED_KYC)
    return frozenset(missing)


@given(CASES)
def test_an_undocumented_factor_is_never_determined_around(case: Case) -> None:
    """A profile missing anything a rule needs is refused, however favourable the rest.

    Tightening: "missing means refused" holds on an engine that never determines
    anything, so the property is an equivalence on the *code*: `missing_kyc_factor`
    appears if and only if some rule could not establish a basis, and the set of factors
    it names is exactly the set computed here from the profile. That fails on an engine
    that refuses for the wrong factor, that reports only the first rule's, or that
    reports a factor the profile documents — none of which a one-sided property sees.
    """
    expected = expected_missing_factors(case)
    outcome = decide(case)
    event(f"missing: {len(expected)} undocumented factor(s)")
    if not expected:
        assert RefusalCode.MISSING_KYC_FACTOR not in codes(outcome)
        return
    assert isinstance(outcome, Refusal)
    assert RefusalCode.MISSING_KYC_FACTOR in codes(outcome)
    reason = next(r for r in outcome.reasons if r.code == RefusalCode.MISSING_KYC_FACTOR)
    assert set(reason.missing_factors) == expected
    assert len(reason.missing_factors) == len(set(reason.missing_factors))


# ---------------------------------------------------------------------------
# 3. No invented evidence
# ---------------------------------------------------------------------------


def expected_basis_keys(case: Case, code: RefusalCode) -> tuple[FactorKey, ...]:
    """Return the factors the rule for `code` is entitled to cite, in order."""
    if code is RefusalCode.UNSUPPORTED_RATIONALE:
        cited = case.recommendation.rationale.cited_factors
        return tuple(dict.fromkeys(claim.key for claim in cited))
    return {
        RefusalCode.MISSING_KYC_FACTOR: SPEC_REQUIRED_KYC,
        RefusalCode.RISK_MISMATCH: (FactorKey.RISK_TOLERANCE, FactorKey.INVESTMENT_OBJECTIVES),
        RefusalCode.HORIZON_MISMATCH: (FactorKey.TIME_HORIZON_YEARS,),
        RefusalCode.LIQUIDITY_CONFLICT: (
            FactorKey.LIQUID_NET_WORTH,
            FactorKey.LIQUIDITY_REQUIREMENT,
        ),
        RefusalCode.CONCENTRATION_BREACH: (FactorKey.HOLDINGS,),
        RefusalCode.STALE_PROFILE: (FactorKey.LAST_REVIEWED,),
    }[code]


@given(CASES)
def test_a_determination_cites_only_what_the_profile_documents(case: Case) -> None:
    """Every citation on a determination is checkable against the profile, and checks.

    The citation is not taken at its word: the value is recomputed from the profile by
    `documented_value` and compared, which is what a verifier holding the profile would
    do.

    Tightening: comparing a citation against the engine's own renderer would agree with
    any renderer, correct or not, so the comparison is against this file's independent
    restatement. And "every citation names a documented key" is satisfied by a rule that
    cites a factor it never consulted — `annual_income` documents *something*, so citing
    it passes a naive check — so the property also pins the exact keys and their order
    per rule. An engine that quietly cited a convenient factor to satisfy the non-empty
    `basis` requirement fails here.
    """
    outcome = decide(case)
    if not isinstance(outcome, Determination):
        return
    event("determination: examined")
    for check in outcome.checks:
        assert check.basis, f"{check.code} passed citing nothing"
        assert tuple(citation.key for citation in check.basis) == expected_basis_keys(
            case,
            check.code,
        )
        for citation in check.basis:
            documented = documented_value(case.profile, citation.key)
            assert documented is not None, f"{check.code} cited undocumented {citation.key}"
            assert citation.value == documented
            assert citation.documented_on == case.profile.last_reviewed


# ---------------------------------------------------------------------------
# 4. Concentration monotonicity
# ---------------------------------------------------------------------------


@given(CASES, st.data())
def test_growing_a_position_never_lowers_its_exposure(case: Case, data: st.DataObject) -> None:
    """Adding to a holding cannot reduce exposure to its instrument, issuer or sector.

    Tightening: monotonicity alone is satisfied by an exposure that ignores the holding
    entirely — a constant is monotone — so the property is two-sided: growing a position
    by `delta` raises each of its three buckets by at least nothing and at most `delta`.
    The upper bound is what fails on an engine that counts a holding twice, or that
    counts the recommendation's amount into a bucket the recommendation does not touch;
    the lower bound is what fails on one that nets a sale against an unrelated position.
    """
    positions = case.profile.holdings
    if not positions:
        event("exposure: no documented portfolio")
        return
    index = data.draw(st.integers(min_value=0, max_value=len(positions) - 1))
    grown = positions[index]
    head_room = MAX_AMOUNT - grown.market_value.amount
    delta = data.draw(amounts(max_value=head_room)) if head_room > ZERO else ZERO
    larger = case.profile.model_copy(
        update={
            "holdings": (
                *positions[:index],
                grown.model_copy(
                    update={
                        "market_value": Money(
                            amount=grown.market_value.amount + delta,
                            currency=case.profile.currency,
                        ),
                    },
                ),
                *positions[index + 1 :],
            ),
        },
    )
    as_of = case.now.date()
    smaller_portfolio = _exposures(RuleInput(case.profile, case.recommendation, as_of)).after
    larger_portfolio = _exposures(RuleInput(larger, case.recommendation, as_of)).after
    buckets = [("instrument", grown.instrument_id), ("issuer", grown.issuer)]
    if grown.sector is not None:
        buckets.append(("sector", grown.sector))
    event(f"exposure: grew a position by {'nothing' if delta == ZERO else 'something'}")
    for bucket in buckets:
        increase = larger_portfolio[bucket] - smaller_portfolio[bucket]
        assert increase >= ZERO, f"{bucket} fell when the position grew"
        assert increase <= delta, f"{bucket} rose by more than the position did"


# ---------------------------------------------------------------------------
# 5. Horizon
# ---------------------------------------------------------------------------


@given(CASES)
def test_terms_outlasting_the_horizon_are_always_refused(case: Case) -> None:
    """The horizon rule fires exactly when a product term outlasts the documented wait.

    Three terms count, and a product with no redemption window at all counts as an
    unbounded wait rather than as no wait.

    Tightening: "a long lock-up is refused" would pass on an engine that refuses every
    lock-up, so the property is an equivalence over all three terms at once, with the
    day counts restated here rather than imported. That fails on an engine that compares
    a quarterly window against 90 days instead of 92, on one that reads a missing
    redemption window as no constraint, and on one that refuses a term equal to the
    horizon — none of which the one-sided form catches.
    """
    horizon = case.profile.time_horizon_years
    outcome = decide(case)
    if horizon is None:
        event("horizon: undocumented")
        assert RefusalCode.HORIZON_MISMATCH not in codes(outcome)
        return
    product = case.recommendation.product
    horizon_days = horizon * SPEC_DAYS_PER_YEAR
    wait_days = SPEC_REDEMPTION_WAIT_DAYS[product.redemption_frequency]
    period = product.recommended_holding_period_years
    outlasts = (
        (period is not None and period > horizon)
        or Decimal(product.lock_up_days) > horizon_days
        or wait_days is None
        or Decimal(wait_days) > horizon_days
    )
    event(f"horizon: terms {'outlast' if outlasts else 'fit within'} it")
    assert (RefusalCode.HORIZON_MISMATCH in codes(outcome)) is outlasts


# ---------------------------------------------------------------------------
# 6. Determinism
# ---------------------------------------------------------------------------


@given(CASES)
def test_the_same_inputs_produce_a_byte_identical_receipt(case: Case) -> None:
    """Two calls, and a round trip through JSON, produce the identical receipt.

    Tightening: comparing two calls in one process passes on an engine that caches by
    object identity, and passes on one that reads the clock only once. So the third
    determination is made from models rebuilt out of their own serialised form — equal
    in value, sharing nothing — and the comparison is on serialised output, so a
    difference in reason ordering or citation ordering fails rather than being absorbed
    by set equality.
    """
    first = determine(case.profile, case.recommendation, now=case.now)
    second = determine(case.profile, case.recommendation, now=case.now)
    rebuilt = determine(
        ClientProfile.model_validate_json(case.profile.model_dump_json()),
        Recommendation.model_validate_json(case.recommendation.model_dump_json()),
        now=case.now,
    )
    assert first.model_dump_json() == second.model_dump_json()
    assert first.model_dump_json() == rebuilt.model_dump_json()


@given(CASES, CASES)
def test_a_receipt_id_separates_the_inputs_it_attests_to(first: Case, second: Case) -> None:
    """Receipt ids agree exactly when the profile, recommendation and time agree.

    Tightening: determinism on its own is satisfied by a constant receipt id. This is
    the other half — the id distinguishes inputs — and it fails on an engine that leaves
    the evaluation time, the profile or the recommendation out of the material it
    derives the id from.
    """
    left = determine(first.profile, first.recommendation, now=first.now)
    right = determine(second.profile, second.recommendation, now=second.now)
    same_inputs = (
        first.profile.model_dump_json() == second.profile.model_dump_json()
        and first.recommendation.model_dump_json() == second.recommendation.model_dump_json()
        and first.now == second.now
    )
    event(f"receipt: inputs {'agree' if same_inputs else 'differ'}")
    assert (left.receipt_id == right.receipt_id) is same_inputs


@given(CASES, st.data())
def test_a_receipt_id_changes_when_any_single_input_changes(
    case: Case,
    data: st.DataObject,
) -> None:
    """Vary one of the three inputs and the receipt id moves with it.

    Tightening: comparing two independently drawn cases almost always varies all three
    inputs at once, so it cannot tell which of them the id actually depends on — an
    engine that left the evaluation time out of the material would pass it, because the
    profiles differed anyway. Here the other two are held fixed while one changes, which
    is what makes the property name the input it is about.
    """
    base = determine(case.profile, case.recommendation, now=case.now)

    other_now = data.draw(evaluation_times(case.profile.last_reviewed))
    retimed = determine(case.profile, case.recommendation, now=other_now)
    assert (retimed.receipt_id == base.receipt_id) is (other_now == case.now)

    other_recommendation = data.draw(recommendations(case.profile))
    reproposed = determine(case.profile, other_recommendation, now=case.now)
    assert (reproposed.receipt_id == base.receipt_id) is (
        other_recommendation.model_dump_json() == case.recommendation.model_dump_json()
    )

    # Varied through a field no precondition depends on, so the engine still determines.
    other_profile = case.profile.model_copy(
        update={"annual_income": data.draw(st.none() | money(case.profile.currency))},
    )
    redocumented = determine(other_profile, case.recommendation, now=case.now)
    assert (redocumented.receipt_id == base.receipt_id) is (
        other_profile.model_dump_json() == case.profile.model_dump_json()
    )


# ---------------------------------------------------------------------------
# 7. Totality
# ---------------------------------------------------------------------------


@given(CASES)
def test_every_accepted_input_yields_a_receipt(case: Case) -> None:
    """There is no third outcome, and no input the models accept that has none.

    Tightening: `isinstance(outcome, Determination | Refusal)` is nearly free — the type
    says so — so the property also re-derives the digests the receipt binds itself to,
    from the inputs, with `hashlib`. That is what makes the receipt checkable rather
    than believed, and it fails on an engine that swaps the two digests, that digests
    the wrong record, or that stamps a version it did not run.
    """
    outcome = decide(case)
    assert isinstance(outcome, Determination | Refusal)
    assert outcome.client_id == case.profile.client_id
    assert outcome.recommendation_id == case.recommendation.recommendation_id
    assert outcome.profile_last_reviewed == case.profile.last_reviewed
    assert outcome.decided_at == case.now
    assert outcome.engine_version == SPEC_ENGINE_VERSION
    assert (
        outcome.profile_digest
        == hashlib.sha256(
            case.profile.model_dump_json().encode(),
        ).hexdigest()
    )
    assert (
        outcome.recommendation_digest
        == hashlib.sha256(
            case.recommendation.model_dump_json().encode(),
        ).hexdigest()
    )
    assert outcome.receipt_id.startswith("RCPT-")
    assert len(outcome.receipt_id) == SPEC_RECEIPT_ID_LENGTH


def rule_configs() -> st.SearchStrategy[RuleConfig]:
    """Return a strategy for the thresholds a caller may determine against."""
    return st.builds(
        RuleConfig,
        concentration_limit_fraction=st.sampled_from(
            (Decimal(0), Decimal("0.05"), Decimal("0.20"), Decimal("0.50"), Decimal(1)),
        ),
        profile_review_interval_days=st.sampled_from((0, 30, 365, 100_000)),
    )


@given(CASES, rule_configs())
def test_the_receipt_reports_exactly_the_rules_that_fired(case: Case, config: RuleConfig) -> None:
    """`determine` reports every rule that refused, once each, in the declared order.

    The configured thresholds are drawn rather than defaulted, so the property also says
    that `determine` determines against the configuration it was handed: the rules are
    run here with the same `RuleConfig` the entry point is given, and the two must agree.

    Tightening: the models already require a determination to carry all seven checks, so
    asserting that tests pydantic rather than the engine. This compares the two public
    ways of asking the same question — running each rule, and calling `determine` — so
    it fails on an engine that short-circuits on the first refusal (the refusal would
    carry fewer codes than the rules produced), that reports a reason no rule gave, that
    quietly evaluates against its own default thresholds instead of the caller's, or
    whose merge of same-code reasons loses one or reorders them.
    """
    rule_input = RuleInput(case.profile, case.recommendation, case.now.date(), config)
    rule_outcomes = [rule(rule_input) for _, rule in RULES]
    reasons = [result for result in rule_outcomes if isinstance(result, RefusalReason)]
    expected = tuple(dict.fromkeys(reason.code for reason in reasons))
    outcome = determine(case.profile, case.recommendation, now=case.now, config=config)
    event(f"rules: {len(expected)} refusing")
    if not expected:
        assert isinstance(outcome, Determination)
        assert tuple(check.code for check in outcome.checks) == tuple(code for code, _ in RULES)
        return
    assert isinstance(outcome, Refusal)
    assert codes(outcome) == expected


RULE_NEEDS: dict[RefusalCode, tuple[FactorKey, ...]] = {
    RefusalCode.MISSING_KYC_FACTOR: SPEC_REQUIRED_KYC,
    RefusalCode.RISK_MISMATCH: (FactorKey.RISK_TOLERANCE, FactorKey.INVESTMENT_OBJECTIVES),
    RefusalCode.HORIZON_MISMATCH: (FactorKey.TIME_HORIZON_YEARS,),
    RefusalCode.LIQUIDITY_CONFLICT: (FactorKey.LIQUID_NET_WORTH, FactorKey.LIQUIDITY_REQUIREMENT),
    RefusalCode.CONCENTRATION_BREACH: (FactorKey.HOLDINGS,),
    RefusalCode.UNSUPPORTED_RATIONALE: (),
    RefusalCode.STALE_PROFILE: (),
}
"""What each rule needs documented before it can establish a basis at all."""


@given(CASES)
def test_each_rule_names_every_factor_it_needed_and_lacked(case: Case) -> None:
    """A rule that cannot establish its basis names all of what it lacked, not some.

    Tightening: the merged refusal cannot see this. `determine` unions the missing
    factors across rules, so a rule that reports only one of the two factors it needed
    is invisible at the entry point — the KYC rule reports the rest and the union comes
    out right. Asking each rule directly is what makes the understatement observable,
    and the detail is checked too because it is the half of the reason a person reads.
    """
    rule_input = RuleInput(case.profile, case.recommendation, case.now.date())
    for code, rule in RULES:
        needed = tuple(
            key for key in RULE_NEEDS[code] if documented_value(case.profile, key) is None
        )
        outcome = rule(rule_input)
        lacking = isinstance(outcome, RefusalReason) and outcome.code is (
            RefusalCode.MISSING_KYC_FACTOR
        )
        if code is RefusalCode.UNSUPPORTED_RATIONALE:
            # The rationale's own case: it needs a citation, not a profile factor.
            assert lacking is (not case.recommendation.rationale.cited_factors)
            continue
        assert lacking is bool(needed), f"{code} disagreed about whether it had a basis"
        if not needed:
            continue
        assert isinstance(outcome, RefusalReason)
        assert outcome.missing_factors == needed
        assert ", ".join(key.value for key in needed) in outcome.detail


@given(CASES)
def test_an_unsupported_rationale_names_the_claims_it_rejected(case: Case) -> None:
    """The claims a refusal rejects are exactly those the profile does not document.

    Tightening: `unsupported_claims` being non-empty is already required by the models,
    so it proves nothing. This recomputes which claims the profile supports — value and
    documentation date both — and requires the refusal to name that set exactly, in the
    order the rationale made them. An engine that rounded a near miss into a match, or
    that rejected a claim it could have verified, fails here in one direction or the
    other.
    """
    claimed = case.recommendation.rationale.cited_factors
    outcome = check_unsupported_rationale(
        RuleInput(case.profile, case.recommendation, case.now.date())
    )
    if not claimed:
        assert isinstance(outcome, RefusalReason)
        assert outcome.code is RefusalCode.MISSING_KYC_FACTOR
        return
    unsupported = tuple(
        claim
        for claim in claimed
        if documented_value(case.profile, claim.key) != claim.value
        or claim.documented_on != case.profile.last_reviewed
    )
    event(f"rationale: {len(unsupported)} of {len(claimed)} claims unsupported")
    if not unsupported:
        assert isinstance(outcome, RuleCheck)
        return
    assert isinstance(outcome, RefusalReason)
    assert outcome.code is RefusalCode.UNSUPPORTED_RATIONALE
    assert outcome.unsupported_claims == unsupported
    assert ", ".join(claim.key.value for claim in unsupported) in outcome.detail


# ---------------------------------------------------------------------------
# Concentration, stated as an equivalence
# ---------------------------------------------------------------------------

DIMENSIONS = ("instrument", "issuer", "sector")


def spec_exposures(case: Case) -> tuple[dict[tuple[str, str], Decimal], Decimal]:
    """Return post-recommendation exposure per bucket, and the portfolio total.

    The engine's promise, restated from its documentation rather than its code: a buy or
    a switch adds the full amount to the product's instrument, issuer and sector, gross
    and with no netting for the disposal a switch implies; a sell removes what is
    actually held of that instrument and no more; a hold changes nothing.
    """
    exposure = _spec_holdings_exposure(case.profile)
    product = case.recommendation.product
    action = case.recommendation.action
    if action in {TradeAction.BUY, TradeAction.SWITCH}:
        delta = case.recommendation.amount.amount
    elif action is TradeAction.SELL:
        held = exposure.get(("instrument", product.instrument_id), ZERO)
        delta = -min(case.recommendation.amount.amount, held)
    else:
        delta = ZERO
    if delta != ZERO:
        buckets = [("instrument", product.instrument_id), ("issuer", product.issuer)]
        if product.sector is not None:
            buckets.append(("sector", product.sector))
        for bucket in buckets:
            exposure[bucket] = exposure.get(bucket, ZERO) + delta
    total = sum((holding.market_value.amount for holding in case.profile.holdings), ZERO) + delta
    return exposure, total


def spec_percent(part: Decimal, whole: Decimal) -> str:
    """Render `part` as a percentage of `whole`, as a receipt states it."""
    return f"{(part / whole * Decimal(100)).quantize(Decimal('0.01'))}%"


# A portfolio sold down to nothing, where the product shares the sold instrument but not
# its issuer or sector, so the disposal is subtracted from buckets the holding never
# contributed to: the total reaches exactly zero while two buckets are still positive.
# Nothing else in the space reaches it, and the engine's `total <= 0` guard is the only
# thing standing between that state and a division by zero.
_EMPTIED_PORTFOLIO = Case(
    profile=ClientProfile(
        client_id=CLIENT_ID,
        last_reviewed=LATEST_REVIEW,
        currency=Currency.CAD,
        risk_tolerance=RiskLevel.MEDIUM,
        time_horizon_years=Decimal(10),
        investment_objectives=(InvestmentObjective.BALANCED,),
        holdings=(
            Holding(
                instrument_id="SYNTHETIC-FUND-A",
                name="Synthetic Placeholder Fund A",
                issuer="Synthetic Placeholder Issuer X",
                sector="synthetic-sector-p",
                market_value=Money(amount=Decimal("100.00"), currency=Currency.CAD),
            ),
            Holding(
                instrument_id="SYNTHETIC-FUND-B",
                name="Synthetic Placeholder Fund B",
                issuer="Synthetic Placeholder Issuer Y",
                sector="synthetic-sector-q",
                market_value=Money(amount=Decimal("0.00"), currency=Currency.CAD),
            ),
        ),
    ),
    recommendation=Recommendation(
        recommendation_id=RECOMMENDATION_ID,
        client_id=CLIENT_ID,
        action=TradeAction.SELL,
        product=Product(
            instrument_id="SYNTHETIC-FUND-A",
            name="Synthetic Placeholder Fund A",
            issuer="Synthetic Placeholder Issuer Y",
            sector="synthetic-sector-q",
            risk_rating=RiskLevel.MEDIUM,
            risk_rating_source="SYNTHETIC placeholder fund facts",
            redemption_frequency=RedemptionFrequency.DAILY,
        ),
        amount=Money(amount=Decimal("100.00"), currency=Currency.CAD),
        proposed_on=LATEST_REVIEW,
        rationale=RecommendationRationale(
            text="Synthetic placeholder rationale.",
            cited_factors=(
                FactorCitation(
                    key=FactorKey.RISK_TOLERANCE,
                    value="medium",
                    documented_on=LATEST_REVIEW,
                ),
            ),
        ),
    ),
    now=dt.datetime.combine(LATEST_REVIEW, dt.time(12), tzinfo=dt.UTC),
)


@example(_EMPTIED_PORTFOLIO)
@given(CASES)
def test_concentration_fires_exactly_when_a_bucket_exceeds_the_limit(case: Case) -> None:
    """Every instrument, issuer and sector is checked, and the receipt says by how much.

    Tightening: monotonicity says exposure moves the right way; it says nothing about
    whether the engine aggregates two positions in the same sector, or whether it adds a
    purchase to the position it already holds — a mutant that overwrites instead of
    accumulating is monotone. So exposure is recomputed here bucket by bucket and the
    rule is stated as an equivalence over the result, including the percentages the
    refusal reports, because "instrument X at 0.25%" and "instrument X at 25.00%" are
    different statements to the person reading the receipt and only one of them is true.
    """
    outcome = decide(case)
    if not case.profile.holdings:
        assert RefusalCode.CONCENTRATION_BREACH not in codes(outcome)
        return
    exposure, total = spec_exposures(case)
    breaches = (
        sorted(
            (
                (DIMENSIONS.index(dimension), value, amount)
                for (dimension, value), amount in exposure.items()
                if amount > total * SPEC_CONCENTRATION_LIMIT
            ),
            key=lambda breach: (breach[0], breach[1]),
        )
        if total > ZERO
        else []
    )
    event(f"concentration: {len(breaches)} bucket(s) over the limit")
    assert (RefusalCode.CONCENTRATION_BREACH in codes(outcome)) is bool(breaches)
    if not breaches:
        return
    assert isinstance(outcome, Refusal)
    reason = next(r for r in outcome.reasons if r.code is RefusalCode.CONCENTRATION_BREACH)
    # Each bucket is named with its post-recommendation share, and they appear in the
    # declared dimension order. Matched fragment by fragment rather than as one joined
    # string, because each entry now carries what the bucket was before as well.
    positions = []
    for index, value, amount in breaches:
        fragment = f"{DIMENSIONS[index]} {value} at {spec_percent(amount, total)}"
        assert fragment in reason.detail, fragment
        positions.append(reason.detail.index(fragment))
    assert positions == sorted(positions)
    assert spec_percent(SPEC_CONCENTRATION_LIMIT, Decimal(1)) in reason.detail


# ---------------------------------------------------------------------------
# Whose breach it is, stated as an equivalence
#
# The two rules that read the portfolio can object to exposure the client already held.
# Each states a `BreachOrigin` saying what the recommendation does to that exposure, and
# the outcome does not depend on it: a `reduced` breach refuses exactly as a `created`
# one does. What follows recomputes the origin from the documented rules rather than
# from the engine, so that a mutant which reports every breach as `created` — or which
# quietly turns `reduced` into a pass — fails here.
# ---------------------------------------------------------------------------

SPEC_ORIGIN_SEVERITY: dict[BreachOrigin, int] = {
    BreachOrigin.REDUCED: 0,
    BreachOrigin.UNCHANGED: 1,
    BreachOrigin.INCREASED: 2,
    BreachOrigin.CREATED: 3,
}
"""Least to most severe. Restated so a mutant to `BREACH_ORIGIN_SEVERITY` fails."""


def spec_before_exposures(case: Case) -> tuple[dict[tuple[str, str], Decimal], Decimal]:
    """Return the documented portfolio's exposure per bucket, and its total.

    The "before" half of `spec_exposures`: what the client held when the recommendation
    was made, with nothing about the recommendation applied to it.
    """
    exposure = _spec_holdings_exposure(case.profile)
    total = sum((holding.market_value.amount for holding in case.profile.holdings), ZERO)
    return exposure, total


def spec_concentration_origin(
    before: Decimal,
    after: Decimal,
    *,
    breached_before: bool,
) -> BreachOrigin:
    """Classify a breaching bucket, as the concentration rule documents it.

    Created is decided by whether the bucket was over the limit before, and direction by
    the exposure amount — never by the share, so that a purchase which dilutes an
    over-concentrated position by enlarging the portfolio counts as `UNCHANGED`.
    """
    if not breached_before:
        return BreachOrigin.CREATED
    if after > before:
        return BreachOrigin.INCREASED
    if after < before:
        return BreachOrigin.REDUCED
    return BreachOrigin.UNCHANGED


def spec_risk_origin(before: Decimal, after: Decimal) -> BreachOrigin:
    """Classify a risk objection, as the risk rule documents it.

    The objection is to holding the product at all, so `CREATED` means the profile
    documents no position and the recommendation would open one. No position on either
    side is `UNCHANGED`: nothing was created and nothing was reduced.
    """
    if after > before:
        return BreachOrigin.CREATED if before <= ZERO else BreachOrigin.INCREASED
    if after < before:
        return BreachOrigin.REDUCED
    return BreachOrigin.UNCHANGED


def spec_most_severe(origins: list[BreachOrigin]) -> BreachOrigin:
    """Return the most severe origin, as a reason covering several breaches reports it."""
    return max(origins, key=lambda origin: SPEC_ORIGIN_SEVERITY[origin])


def origin_for(outcome: SuitabilityOutcome, code: RefusalCode) -> BreachOrigin | None:
    """Return the origin the outcome states for `code`, or `None` if it did not fire."""
    if isinstance(outcome, Determination):
        return None
    return next((r.breach_origin for r in outcome.reasons if r.code is code), None)


@given(CASES)
def test_every_reason_states_an_origin_exactly_when_its_rule_reads_the_portfolio(
    case: Case,
) -> None:
    """Only the two portfolio-reading rules state an origin, and they always state one.

    Tightening: the models enforce the same invariant, so on its own this would test
    pydantic. What it adds is that the engine reaches the invariant through every route
    into a refusal — including the merge of same-code reasons, which builds a
    `RefusalReason` of its own and could drop the field on the way through.
    """
    outcome = decide(case)
    if isinstance(outcome, Determination):
        return
    for reason in outcome.reasons:
        assert (reason.breach_origin is not None) is (reason.code in BREACH_ORIGIN_CODES), (
            f"{reason.code} disagreed about whether it states an origin"
        )


@given(CASES)
def test_a_concentration_refusal_reports_the_most_severe_origin_among_its_breaches(
    case: Case,
) -> None:
    """The origin is recomputed bucket by bucket and must match what the receipt says.

    Tightening: an engine that reported `CREATED` for everything would still refuse the
    right cases, so the code alone cannot catch it, and neither can a property that only
    asks whether *an* origin is present. This restates the classification from the
    documented rule and requires equality — including the aggregation, so that a
    recommendation reducing one breach while creating another cannot report as a
    reduction.
    """
    reported = origin_for(decide(case), RefusalCode.CONCENTRATION_BREACH)
    if reported is None:
        return
    after, after_total = spec_exposures(case)
    before, before_total = spec_before_exposures(case)
    limit = SPEC_CONCENTRATION_LIMIT
    origins = [
        spec_concentration_origin(
            before.get(bucket, ZERO),
            amount,
            breached_before=before.get(bucket, ZERO) > before_total * limit,
        )
        for bucket, amount in after.items()
        if amount > after_total * limit
    ]
    assert origins
    event(f"origin: concentration {spec_most_severe(origins).value}")
    assert reported is spec_most_severe(origins)


@given(CASES)
def test_a_risk_refusal_reports_what_it_does_to_the_position(case: Case) -> None:
    """The risk origin is the documented position before against the position after.

    Tightening: the risk rule reads the portfolio only to classify, never to decide, so
    a mutant that ignored the holdings entirely would still refuse exactly the same
    cases. This is the property that sees the difference.
    """
    reported = origin_for(decide(case), RefusalCode.RISK_MISMATCH)
    if reported is None:
        return
    bucket = ("instrument", case.recommendation.product.instrument_id)
    after, _ = spec_exposures(case)
    before, _ = spec_before_exposures(case)
    expected = spec_risk_origin(before.get(bucket, ZERO), after.get(bucket, ZERO))
    event(f"origin: risk {expected.value}")
    assert reported is expected


@given(CASES)
def test_a_disposal_is_never_reported_as_creating_the_position_it_reduces(case: Case) -> None:
    """A sell of a documented position never reads as `CREATED` under `risk_mismatch`.

    That rule measures one bucket — the client's position in the product — so a disposal
    of a documented position can only ever move it downwards.

    Tightening: the equivalence above is exact, so this is implied by it, but it is the
    direction the original defect took and it fails on its own without depending on the
    whole recomputation being right. It is also what would fail first if the
    classification were rewired to read shares instead of amounts, since a disposal
    shrinks the portfolio a share is measured against.
    """
    if case.recommendation.action is not TradeAction.SELL:
        return
    bucket = ("instrument", case.recommendation.product.instrument_id)
    before, _ = spec_before_exposures(case)
    if before.get(bucket, ZERO) <= ZERO:
        event("disposal: nothing documented to dispose of")
        return
    event("disposal: a documented position sold down")
    assert origin_for(decide(case), RefusalCode.RISK_MISMATCH) is not BreachOrigin.CREATED


@given(CASES)
def test_a_concentration_breach_reads_as_created_only_when_a_bucket_newly_breaches(
    case: Case,
) -> None:
    """`CREATED` is reported exactly when some breaching bucket was inside the limit before.

    Deliberately stated over the aggregate rather than per bucket, because a disposal can
    create a breach in a bucket it never touches: selling one position shrinks the
    portfolio every other bucket is measured against, so a holding that sat at exactly
    the limit can be over it afterwards without having moved. The rule reports that as
    created, and it is created — the recommendation is what put the bucket over the line.
    A property saying "a sell is never `CREATED`" would be false, and would have to be
    weakened by exempting the very case worth knowing about.

    Tightening: this pins the aggregation from the outside. An engine that reported the
    first bucket's origin instead of the most severe passes the per-bucket equivalence on
    every single-breach case and fails here as soon as two buckets disagree.
    """
    reported = origin_for(decide(case), RefusalCode.CONCENTRATION_BREACH)
    if reported is None:
        return
    after, after_total = spec_exposures(case)
    before, before_total = spec_before_exposures(case)
    limit = SPEC_CONCENTRATION_LIMIT
    newly = [
        bucket
        for bucket, amount in after.items()
        if amount > after_total * limit and before.get(bucket, ZERO) <= before_total * limit
    ]
    event(f"concentration: {len(newly)} bucket(s) newly over the limit")
    assert (reported is BreachOrigin.CREATED) is bool(newly)


# ---------------------------------------------------------------------------
# The generated space actually reaches the boundaries
#
# A property that never sees a case near a threshold passes without testing it. These
# assert that each interesting region is reachable, and fail loudly — rather than
# quietly weakening every property above — if a change to a strategy stops reaching one.
# They characterise the strategies, not the engine, so a mutation run skips them.
# ---------------------------------------------------------------------------


def _concentration_margin(case: Case) -> Decimal | None:
    """Return how far the most concentrated bucket sits above the configured limit."""
    exposures = _exposures(RuleInput(case.profile, case.recommendation, case.now.date()))
    if exposures.after_total <= ZERO:
        return None
    limit = exposures.after_total * SPEC_CONCENTRATION_LIMIT
    return max((amount - limit for amount in exposures.after.values()), default=None)


def _liquidity_margin(case: Case) -> Decimal | None:
    """Return what the recommendation leaves above the documented liquidity need."""
    required = _spec_required_liquidity(case.profile)
    if required is None or case.profile.liquid_net_worth is None:
        return None
    consumed = (
        case.recommendation.amount.amount
        if case.recommendation.action in {TradeAction.BUY, TradeAction.SWITCH}
        else ZERO
    )
    return case.profile.liquid_net_worth.amount - consumed - required


def _at_the_risk_ceiling(case: Case) -> bool:
    """Return whether the product is rated exactly at the effective ceiling."""
    ceiling = effective_ceiling(case.profile)
    return ceiling is not None and _rank(case.recommendation.product.risk_rating) == ceiling


def _one_step_above_the_risk_ceiling(case: Case) -> bool:
    """Return whether the product is rated one step above the effective ceiling."""
    ceiling = effective_ceiling(case.profile)
    return ceiling is not None and _rank(case.recommendation.product.risk_rating) == ceiling + 1


def _objectives_in_tension_with_the_tolerance(case: Case) -> bool:
    """Return whether an objective pulls the ceiling below the documented tolerance."""
    tolerance = case.profile.risk_tolerance
    ceiling = effective_ceiling(case.profile)
    return tolerance is not None and ceiling is not None and ceiling < _rank(tolerance)


def _objectives_in_tension_with_each_other(case: Case) -> bool:
    """Return whether two documented objectives imply different ceilings."""
    implied = {_rank(SPEC_OBJECTIVE_CEILING[o]) for o in case.profile.investment_objectives}
    return len(implied) > 1


def _lock_up_exactly_at_the_horizon(case: Case) -> bool:
    """Return whether the lock-up equals the documented horizon to the day."""
    horizon = case.profile.time_horizon_years
    if horizon is None:
        return False
    return Decimal(case.recommendation.product.lock_up_days) == horizon * SPEC_DAYS_PER_YEAR


def _breaches_sorting_against_the_dimension_order(case: Case) -> bool:
    """Return whether two breached buckets sort differently by value than by dimension.

    Without such a case, a refusal ordered by dimension and one ordered by value alone
    read identically, and the promise that the evidence is grouped by dimension is
    untested however many examples are drawn.
    """
    exposure, total = spec_exposures(case)
    if total <= ZERO:
        return False
    breached = [
        (DIMENSIONS.index(dimension), value)
        for (dimension, value), amount in exposure.items()
        if amount > total * SPEC_CONCENTRATION_LIMIT
    ]
    return sorted(breached) != sorted(breached, key=lambda breach: breach[1])


def _shares_an_issuer(case: Case) -> bool:
    """Return whether two documented positions are with the same issuer."""
    issuers = [holding.issuer for holding in case.profile.holdings]
    return len(issuers) != len(set(issuers))


REACHABLE: list[tuple[str, Callable[[Case], bool]]] = [
    ("an undocumented portfolio", lambda c: not c.profile.holdings),
    ("a portfolio of five or more positions", lambda c: len(c.profile.holdings) >= 5),
    ("two positions with the same issuer", _shares_an_issuer),
    (
        "breaches that sort against the dimension order",
        _breaches_sorting_against_the_dimension_order,
    ),
    (
        "a risk tolerance and objectives both undocumented",
        lambda c: c.profile.risk_tolerance is None and not c.profile.investment_objectives,
    ),
    ("an undocumented risk tolerance", lambda c: c.profile.risk_tolerance is None),
    ("an undocumented horizon", lambda c: c.profile.time_horizon_years is None),
    ("undocumented objectives", lambda c: not c.profile.investment_objectives),
    ("undocumented knowledge", lambda c: c.profile.investment_knowledge is None),
    ("an undocumented liquidity requirement", lambda c: c.profile.liquidity_requirement is None),
    ("an undocumented income", lambda c: c.profile.annual_income is None),
    ("an undocumented net worth", lambda c: c.profile.net_worth is None),
    ("an undocumented liquid net worth", lambda c: c.profile.liquid_net_worth is None),
    ("objectives in tension with the tolerance", _objectives_in_tension_with_the_tolerance),
    ("objectives in tension with each other", _objectives_in_tension_with_each_other),
    ("a product rated exactly at the ceiling", _at_the_risk_ceiling),
    ("a product rated one step above the ceiling", _one_step_above_the_risk_ceiling),
    ("a lock-up exactly at the horizon", _lock_up_exactly_at_the_horizon),
    (
        "a product with no redemption window",
        lambda c: c.recommendation.product.redemption_frequency is RedemptionFrequency.NONE,
    ),
    ("concentration exactly at the limit", lambda c: _concentration_margin(c) == ZERO),
    (
        "concentration one cent over the limit",
        lambda c: (margin := _concentration_margin(c)) is not None and ZERO < margin <= CENT,
    ),
    (
        "concentration comfortably under the limit",
        lambda c: (margin := _concentration_margin(c)) is not None and margin < -CENT,
    ),
    ("liquidity exactly at the documented requirement", lambda c: _liquidity_margin(c) == ZERO),
    (
        "liquidity one cent short",
        lambda c: (margin := _liquidity_margin(c)) is not None and -CENT <= margin < ZERO,
    ),
    (
        "a profile exactly at the review interval",
        lambda c: (c.now.date() - c.profile.last_reviewed).days == SPEC_REVIEW_INTERVAL_DAYS,
    ),
    (
        "a profile one day past the review interval",
        lambda c: (c.now.date() - c.profile.last_reviewed).days == SPEC_REVIEW_INTERVAL_DAYS + 1,
    ),
    ("a rationale citing nothing", lambda c: not c.recommendation.rationale.cited_factors),
    (
        "a rationale citing three claims",
        lambda c: len(c.recommendation.rationale.cited_factors) == 3,
    ),
    ("an amount above a trillion", lambda c: c.recommendation.amount.amount > Decimal(10) ** 12),
    (
        "a supported determination",
        lambda c: isinstance(
            determine(c.profile, c.recommendation, now=c.now),
            Determination,
        ),
    ),
    (
        "a refusal carrying four or more reasons",
        lambda c: len(codes(determine(c.profile, c.recommendation, now=c.now))) >= 4,
    ),
]


@pytest.mark.strategy_coverage
@pytest.mark.parametrize(("description", "predicate"), REACHABLE, ids=[d for d, _ in REACHABLE])
def test_the_generated_space_reaches(
    description: str,
    predicate: Callable[[Case], bool],
) -> None:
    """The strategies reach every region the properties above need to be worth running.

    `find` raises rather than returns when the region is unreachable, so a strategy that
    stops generating (say) a concentration near the limit fails here instead of leaving
    the concentration property passing on cases that never approach it.
    """
    assert predicate(
        find(CASES, predicate, settings=settings(max_examples=2000, deadline=None, database=None)),
    ), description
