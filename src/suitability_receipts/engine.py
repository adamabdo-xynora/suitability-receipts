"""The determination engine: seven deterministic rules over documented client facts.

Every function here is pure. The engine does not read the clock, does not read the
environment, does not call a model, and does not repair a recommendation. The
evaluation time is injected by the caller (`determine(..., now=...)`) so that the same
inputs always produce the same receipt.

How a rule reports
------------------
Each rule is a function `(RuleInput) -> RuleCheck | RefusalReason`. Every rule runs on
every call: `determine` never short-circuits, because `Refusal.reasons` is meant to
carry every reason that fired and reporting only the first would understate what is
wrong.

A rule that cannot establish its basis refuses. It does not pass. `RuleCheck.basis` is
required and non-empty precisely so that "passed without consulting anything" is
unrepresentable, and the engine must not route around that by citing a factor it did
not use. When a rule needs a profile factor that is not documented, it returns a
`missing_kyc_factor` reason naming that factor. `REQUIRED_KYC_FACTORS` is the floor
every determination needs; individual rules need more than the floor, and say so.
Several rules can each report a missing factor on the same call, so `determine` merges
reasons that share a code before building the `Refusal` (the model requires codes to be
unique within a refusal).

Citations carry `documented_on=profile.last_reviewed`. That is the only documentation
date a `ClientProfile` carries, and inventing a more precise one would make the receipt
say something the profile does not.

Tension between documented factors
----------------------------------
Some profiles disagree with themselves. Wherever they do, the engine takes the reading
that does *not* produce a supported result. Silently resolving a tension toward
"supported" would be the exact failure this system exists to prevent. Concretely:

* Risk tolerance versus stated objective. A profile documenting a conservative risk
  tolerance and an aggressive objective (or the reverse) is not evidence for the
  aggressive one. `check_risk_mismatch` takes the *most conservative* of the documented
  risk tolerance and the ceiling implied by each documented objective, and compares the
  product against that. Two objectives in tension with each other (`capital_preservation`
  and `speculation` on the same profile) resolve the same way: the lower ceiling wins.
* A `switch` does not say what is being switched out of. It is therefore counted as
  consuming liquidity and as adding concentration, gross, with no netting for the
  disposal it implies.
* `ClientProfile.holdings` defaults to `()`, so an empty tuple cannot be distinguished
  from "the portfolio was never documented". `check_concentration_breach` reads it as
  undocumented and refuses, rather than as an empty portfolio that any purchase would
  fit into.
* A rationale claim that nearly matches the profile is unsupported. The engine does not
  round a claim toward the documented value, and does not treat a claim dated to a
  different documentation event as verified.

Preconditions
-------------
Three things are caller errors rather than suitability questions, and raise `ValueError`
instead of producing a receipt: a recommendation for a different client, an amount in a
currency the profile does not use (the engine has no exchange rate and will not invent
one), and an evaluation time that is not UTC-aware or that precedes the profile's
documented review date.
"""

import datetime as dt
import hashlib
from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType

from pydantic import BaseModel

from suitability_receipts.models import (
    REQUIRED_KYC_FACTORS,
    RISK_LEVEL_RANK,
    ClientProfile,
    Determination,
    FactorCitation,
    FactorKey,
    InvestmentObjective,
    Money,
    Recommendation,
    RedemptionFrequency,
    Refusal,
    RefusalCode,
    RefusalReason,
    RiskLevel,
    RuleCheck,
    SuitabilityOutcome,
    TradeAction,
)

__all__ = [
    "CONCENTRATION_LIMIT_FRACTION",
    "DAYS_PER_YEAR",
    "DEFAULT_CONFIG",
    "ENGINE_VERSION",
    "OBJECTIVE_RISK_CEILING",
    "PROFILE_REVIEW_INTERVAL_DAYS",
    "REDEMPTION_DAYS_TO_LIQUIDITY",
    "RULES",
    "RuleConfig",
    "RuleInput",
    "RuleOutcome",
    "check_concentration_breach",
    "check_horizon_mismatch",
    "check_liquidity_conflict",
    "check_missing_kyc_factor",
    "check_risk_mismatch",
    "check_stale_profile",
    "check_unsupported_rationale",
    "determine",
]

ENGINE_VERSION = "0.1.0"
"""Recorded on every receipt. Bump it whenever rule behaviour changes, so that two
receipts that disagree can be told apart by the rules that produced them."""


# ---------------------------------------------------------------------------
# Configured thresholds
#
# Both numbers below are decisions made in this repository, not figures read out of a
# rulebook. They are named, defined in one place, and injectable through `RuleConfig`
# so that changing one is a one-line edit and so that the tests can pin the boundary
# rather than the value.
# ---------------------------------------------------------------------------

CONCENTRATION_LIMIT_FRACTION = Decimal("0.20")
"""Maximum share of the post-recommendation portfolio permitted in any one instrument,
issuer, or sector.

DERIVATION: none. This is a repository-specific placeholder, not an industry standard
and not a regulatory figure, and it is stated here as a placeholder so that nobody later
mistakes it for one. It was chosen only so that the rule has a definite, testable
boundary: 20% is the point at which five equal positions would be the most concentrated
portfolio that still passes. Nothing about the number is derived from data or sourced
from a citable requirement.

The same limit is applied to all three dimensions (instrument, issuer, sector), which is
itself a simplification — a defensible policy would very likely set them differently.

TO DO: calibrate against the eval set once it exists, and either replace this with a
figure traceable to a primary source or record the measured basis for keeping it. Until
then, treat every `concentration_breach` refusal as "exceeded the configured limit",
never as "exceeded a required limit"."""

PROFILE_REVIEW_INTERVAL_DAYS = 365
"""Maximum age, in days, of a profile's documented review date before the profile is
treated as stale.

DERIVATION: none from a citable source. This is a repository choice. It encodes an
annual review cadence, expressed in exact days (365) rather than as "one calendar year"
so that the arithmetic is exact and does not shift with leap years — a profile reviewed
on 2024-02-29 gets the same allowance as one reviewed on 2024-03-01.

Deliberately NOT stated here: what any regulator requires. The review obligations this
project intends to model have not yet been verified against primary sources (see the
placeholder section in README.md), and a confident wrong citation in a compliance tool
is worse than an absent one. This number is an engineering default.

TO DO: replace with a sourced interval once the regulatory section is written, or record
the measured basis for keeping it after the eval set exists."""

OBJECTIVE_RISK_CEILING: MappingProxyType[InvestmentObjective, RiskLevel] = MappingProxyType(
    {
        InvestmentObjective.CAPITAL_PRESERVATION: RiskLevel.LOW,
        InvestmentObjective.INCOME: RiskLevel.LOW_TO_MEDIUM,
        InvestmentObjective.BALANCED: RiskLevel.MEDIUM,
        InvestmentObjective.GROWTH: RiskLevel.MEDIUM_TO_HIGH,
        InvestmentObjective.SPECULATION: RiskLevel.HIGH,
    },
)
"""The highest product risk each documented objective is read as supporting.

Also a repository decision rather than a sourced mapping: it exists so that a stated
objective constrains the determination instead of decorating it. It is used only to
lower the effective ceiling, never to raise it above the documented risk tolerance."""

REDEMPTION_DAYS_TO_LIQUIDITY: MappingProxyType[RedemptionFrequency, int | None] = MappingProxyType(
    {
        RedemptionFrequency.DAILY: 1,
        RedemptionFrequency.MONTHLY: 31,
        RedemptionFrequency.QUARTERLY: 92,
        RedemptionFrequency.ANNUAL: 366,
        RedemptionFrequency.NONE: None,
    },
)
"""Worst-case wait, in days, for the next redemption window.

These are upper bounds on the calendar, not policy choices: the longest month is 31
days, the longest quarter 92, the longest year 366. `NONE` maps to `None` — there is no
window at all, which is a different fact from a long wait."""

DAYS_PER_YEAR = Decimal(365)
"""Used only to compare a documented horizon in years against product terms in days.
Fixed at 365 so the comparison is exact; see `PROFILE_REVIEW_INTERVAL_DAYS`."""

_ZERO = Decimal(0)
_HUNDRED = Decimal(100)
_PERCENT_QUANTUM = Decimal("0.01")
_RECEIPT_ID_HEX_LENGTH = 32

_REQUIRED_KYC_ORDER: tuple[FactorKey, ...] = tuple(
    key for key in FactorKey if key in REQUIRED_KYC_FACTORS
)
"""`REQUIRED_KYC_FACTORS` in `FactorKey` declaration order, so that refusals list
missing factors in a fixed order rather than a set's."""


@dataclass(frozen=True, slots=True)
class RuleConfig:
    """The configured thresholds a determination was made against."""

    concentration_limit_fraction: Decimal = CONCENTRATION_LIMIT_FRACTION
    profile_review_interval_days: int = PROFILE_REVIEW_INTERVAL_DAYS


DEFAULT_CONFIG = RuleConfig()
"""The thresholds used when a caller does not supply its own."""


@dataclass(frozen=True, slots=True)
class RuleInput:
    """Everything a rule is allowed to look at.

    `as_of` is the injected evaluation date. No rule reads a clock.
    """

    profile: ClientProfile
    recommendation: Recommendation
    as_of: dt.date
    config: RuleConfig = DEFAULT_CONFIG


type RuleOutcome = RuleCheck | RefusalReason
type Rule = Callable[[RuleInput], RuleOutcome]


# ---------------------------------------------------------------------------
# Reading the profile
# ---------------------------------------------------------------------------


def _unique[T: Hashable](items: Iterable[T]) -> tuple[T, ...]:
    """Return `items` with duplicates dropped, keeping first-appearance order."""
    return tuple(dict.fromkeys(items))


def _money(amount: Money) -> str:
    """Render a monetary amount exactly as documented."""
    return f"{amount.amount} {amount.currency.value}"


def _render_risk_tolerance(profile: ClientProfile) -> str | None:
    """Render the documented risk tolerance."""
    return None if profile.risk_tolerance is None else profile.risk_tolerance.value


def _render_time_horizon(profile: ClientProfile) -> str | None:
    """Render the documented time horizon in years."""
    return None if profile.time_horizon_years is None else str(profile.time_horizon_years)


def _render_objectives(profile: ClientProfile) -> str | None:
    """Render the documented objectives, in the order the profile documents them."""
    if not profile.investment_objectives:
        return None
    return ", ".join(objective.value for objective in profile.investment_objectives)


def _render_knowledge(profile: ClientProfile) -> str | None:
    """Render the documented level of investment knowledge."""
    return None if profile.investment_knowledge is None else profile.investment_knowledge.value


def _render_liquidity_requirement(profile: ClientProfile) -> str | None:
    """Render the documented liquidity requirement as reserve plus upcoming expenses."""
    requirement = profile.liquidity_requirement
    if requirement is None:
        return None
    expenses = requirement.upcoming_expenses
    total = sum((expense.amount.amount for expense in expenses), _ZERO)
    return (
        f"emergency reserve {_money(requirement.emergency_reserve)}; "
        f"upcoming expenses: {len(expenses)} totalling {total} {profile.currency.value}"
    )


def _render_annual_income(profile: ClientProfile) -> str | None:
    """Render the documented annual income."""
    return None if profile.annual_income is None else _money(profile.annual_income)


def _render_net_worth(profile: ClientProfile) -> str | None:
    """Render the documented net worth."""
    return None if profile.net_worth is None else _money(profile.net_worth)


def _render_liquid_net_worth(profile: ClientProfile) -> str | None:
    """Render the documented liquid net worth."""
    return None if profile.liquid_net_worth is None else _money(profile.liquid_net_worth)


def _render_holdings(profile: ClientProfile) -> str | None:
    """Render the documented portfolio as a count and a total.

    An empty tuple renders as `None`: `ClientProfile.holdings` defaults to `()`, so an
    empty portfolio and an undocumented one are the same value, and the engine reads it
    as undocumented rather than as the reading that lets a purchase through.
    """
    if not profile.holdings:
        return None
    total = sum((holding.market_value.amount for holding in profile.holdings), _ZERO)
    return f"{len(profile.holdings)} holdings totalling {total} {profile.currency.value}"


def _render_last_reviewed(profile: ClientProfile) -> str | None:
    """Render the documented review date."""
    return profile.last_reviewed.isoformat()


_RENDERERS: MappingProxyType[FactorKey, Callable[[ClientProfile], str | None]] = MappingProxyType(
    {
        FactorKey.RISK_TOLERANCE: _render_risk_tolerance,
        FactorKey.TIME_HORIZON_YEARS: _render_time_horizon,
        FactorKey.INVESTMENT_OBJECTIVES: _render_objectives,
        FactorKey.INVESTMENT_KNOWLEDGE: _render_knowledge,
        FactorKey.LIQUIDITY_REQUIREMENT: _render_liquidity_requirement,
        FactorKey.ANNUAL_INCOME: _render_annual_income,
        FactorKey.NET_WORTH: _render_net_worth,
        FactorKey.LIQUID_NET_WORTH: _render_liquid_net_worth,
        FactorKey.HOLDINGS: _render_holdings,
        FactorKey.LAST_REVIEWED: _render_last_reviewed,
    },
)
"""How each citable factor is rendered for citation. One entry per `FactorKey`."""

if set(_RENDERERS) != set(FactorKey):
    _unrendered = sorted(set(FactorKey) - set(_RENDERERS))
    _renderer_message = f"_RENDERERS does not cover {_unrendered}"
    raise RuntimeError(_renderer_message)


def _documented_value(profile: ClientProfile, key: FactorKey) -> str | None:
    """Return the profile's documented value for `key`, or `None` if undocumented.

    The rendering is the citable form of the fact: a verifier holding the profile
    recomputes this string and compares, so it must depend on nothing but the profile.
    """
    return _RENDERERS[key](profile)


def _cite(profile: ClientProfile, key: FactorKey) -> FactorCitation | None:
    """Cite the profile's documented value for `key`, or `None` if undocumented."""
    value = _documented_value(profile, key)
    if value is None:
        return None
    return FactorCitation(key=key, value=value, documented_on=profile.last_reviewed)


def _basis(profile: ClientProfile, *keys: FactorKey) -> tuple[FactorCitation, ...]:
    """Cite each documented factor in `keys`.

    Callers check for absence themselves before building a basis; an undocumented key
    is dropped here rather than faked, which leaves the basis empty and makes the
    `RuleCheck` unconstructible. That is the intended failure.
    """
    cited = (_cite(profile, key) for key in keys)
    return tuple(citation for citation in cited if citation is not None)


def _undocumented(profile: ClientProfile, *keys: FactorKey) -> tuple[FactorKey, ...]:
    """Return those of `keys` the profile does not document, in the order given."""
    return tuple(key for key in keys if _documented_value(profile, key) is None)


def _missing_factors(code: RefusalCode, keys: tuple[FactorKey, ...]) -> RefusalReason:
    """Refuse because `code` needs profile factors that are not documented."""
    names = ", ".join(key.value for key in keys)
    return RefusalReason(
        code=RefusalCode.MISSING_KYC_FACTOR,
        detail=f"{code.value} cannot be evaluated: {names} not documented.",
        missing_factors=keys,
    )


# ---------------------------------------------------------------------------
# The seven rules
# ---------------------------------------------------------------------------


def check_missing_kyc_factor(rule_input: RuleInput) -> RuleOutcome:
    """Refuse when a factor every determination needs is undocumented."""
    profile = rule_input.profile
    missing = _undocumented(profile, *_REQUIRED_KYC_ORDER)
    if missing:
        names = ", ".join(key.value for key in missing)
        return RefusalReason(
            code=RefusalCode.MISSING_KYC_FACTOR,
            detail=f"Required KYC factors not documented: {names}.",
            missing_factors=missing,
        )
    return RuleCheck(
        code=RefusalCode.MISSING_KYC_FACTOR,
        basis=_basis(profile, *_REQUIRED_KYC_ORDER),
    )


def check_risk_mismatch(rule_input: RuleInput) -> RuleOutcome:
    """Refuse when the product's risk rating exceeds what the profile supports.

    The ceiling is the most conservative of the documented risk tolerance and the
    ceiling implied by each documented objective. A profile in tension with itself does
    not license the riskier reading of itself.
    """
    profile = rule_input.profile
    missing = _undocumented(profile, FactorKey.RISK_TOLERANCE, FactorKey.INVESTMENT_OBJECTIVES)
    if missing:
        return _missing_factors(RefusalCode.RISK_MISMATCH, missing)
    tolerance = profile.risk_tolerance
    if tolerance is None:
        return _missing_factors(RefusalCode.RISK_MISMATCH, (FactorKey.RISK_TOLERANCE,))

    basis = _basis(profile, FactorKey.RISK_TOLERANCE, FactorKey.INVESTMENT_OBJECTIVES)
    ranks = [RISK_LEVEL_RANK[tolerance]]
    ranks.extend(
        RISK_LEVEL_RANK[OBJECTIVE_RISK_CEILING[objective]]
        for objective in profile.investment_objectives
    )
    ceiling = min(ranks)
    product = rule_input.recommendation.product
    if RISK_LEVEL_RANK[product.risk_rating] > ceiling:
        return RefusalReason(
            code=RefusalCode.RISK_MISMATCH,
            detail=(
                f"Product risk rating {product.risk_rating.value} "
                f"(source: {product.risk_rating_source}) exceeds what the profile supports: "
                f"documented risk tolerance {tolerance.value}, "
                f"objectives {_documented_value(profile, FactorKey.INVESTMENT_OBJECTIVES)}."
            ),
            conflicting_factors=basis,
        )
    return RuleCheck(code=RefusalCode.RISK_MISMATCH, basis=basis)


def check_horizon_mismatch(rule_input: RuleInput) -> RuleOutcome:
    """Refuse when the product's terms outlast the documented time horizon.

    Three terms are compared against the horizon: the recommended holding period, the
    lock-up, and the worst-case wait for a redemption window. A product with no
    redemption window at all conflicts with any documented horizon, because a documented
    horizon is a statement that the client expects to exit.
    """
    profile = rule_input.profile
    horizon = profile.time_horizon_years
    if horizon is None:
        return _missing_factors(RefusalCode.HORIZON_MISMATCH, (FactorKey.TIME_HORIZON_YEARS,))

    basis = _basis(profile, FactorKey.TIME_HORIZON_YEARS)
    product = rule_input.recommendation.product
    horizon_days = horizon * DAYS_PER_YEAR
    conflicts: list[str] = []

    holding_period = product.recommended_holding_period_years
    if holding_period is not None and holding_period > horizon:
        conflicts.append(
            f"recommended holding period {holding_period} years exceeds documented "
            f"horizon {horizon} years"
        )
    if Decimal(product.lock_up_days) > horizon_days:
        conflicts.append(
            f"lock-up of {product.lock_up_days} days exceeds documented horizon "
            f"{horizon} years ({horizon_days} days)"
        )
    wait_days = REDEMPTION_DAYS_TO_LIQUIDITY[product.redemption_frequency]
    if wait_days is None:
        conflicts.append(
            f"redemption frequency {product.redemption_frequency.value} offers no "
            f"redemption window, but the profile documents a horizon of {horizon} years"
        )
    elif Decimal(wait_days) > horizon_days:
        conflicts.append(
            f"redemption frequency {product.redemption_frequency.value} can require a "
            f"wait of {wait_days} days, exceeding documented horizon {horizon} years "
            f"({horizon_days} days)"
        )

    if conflicts:
        return RefusalReason(
            code=RefusalCode.HORIZON_MISMATCH,
            detail="; ".join(conflicts) + ".",
            conflicting_factors=basis,
        )
    return RuleCheck(code=RefusalCode.HORIZON_MISMATCH, basis=basis)


def _liquidity_consumed(rule_input: RuleInput) -> Decimal:
    """Return the liquid amount the recommendation consumes.

    A `switch` is counted as consuming the full amount: the recommendation does not
    document what is being switched out of, so netting the disposal would be an
    assumption, and it would be the assumption that produces a supported result.
    """
    action = rule_input.recommendation.action
    if action in {TradeAction.BUY, TradeAction.SWITCH}:
        return rule_input.recommendation.amount.amount
    return _ZERO


def check_liquidity_conflict(rule_input: RuleInput) -> RuleOutcome:
    """Refuse when the recommendation would eat into documented liquidity needs."""
    profile = rule_input.profile
    missing = _undocumented(
        profile,
        FactorKey.LIQUID_NET_WORTH,
        FactorKey.LIQUIDITY_REQUIREMENT,
    )
    if missing:
        return _missing_factors(RefusalCode.LIQUIDITY_CONFLICT, missing)
    liquid_net_worth = profile.liquid_net_worth
    requirement = profile.liquidity_requirement
    if liquid_net_worth is None or requirement is None:
        return _missing_factors(
            RefusalCode.LIQUIDITY_CONFLICT,
            (FactorKey.LIQUID_NET_WORTH, FactorKey.LIQUIDITY_REQUIREMENT),
        )

    basis = _basis(profile, FactorKey.LIQUID_NET_WORTH, FactorKey.LIQUIDITY_REQUIREMENT)
    required = requirement.emergency_reserve.amount + sum(
        (expense.amount.amount for expense in requirement.upcoming_expenses),
        _ZERO,
    )
    remaining = liquid_net_worth.amount - _liquidity_consumed(rule_input)
    if remaining < required:
        return RefusalReason(
            code=RefusalCode.LIQUIDITY_CONFLICT,
            detail=(
                f"After a {rule_input.recommendation.action.value} of "
                f"{_money(rule_input.recommendation.amount)}, liquid net worth would be "
                f"{remaining} {profile.currency.value}, below the documented requirement "
                f"of {required} {profile.currency.value}."
            ),
            conflicting_factors=basis,
        )
    return RuleCheck(code=RefusalCode.LIQUIDITY_CONFLICT, basis=basis)


def _exposures(rule_input: RuleInput) -> tuple[dict[tuple[str, str], Decimal], Decimal]:
    """Return post-recommendation exposure per (dimension, value), and the portfolio total.

    A buy or a switch adds the full amount to the product's instrument, issuer, and
    sector buckets, gross. A sell removes what is actually held of that instrument, and
    no more. A hold changes nothing.
    """
    profile = rule_input.profile
    product = rule_input.recommendation.product
    amount = rule_input.recommendation.amount.amount

    exposure: dict[tuple[str, str], Decimal] = {}
    for holding in profile.holdings:
        value = holding.market_value.amount
        exposure[("instrument", holding.instrument_id)] = (
            exposure.get(("instrument", holding.instrument_id), _ZERO) + value
        )
        exposure[("issuer", holding.issuer)] = (
            exposure.get(("issuer", holding.issuer), _ZERO) + value
        )
        if holding.sector is not None:
            exposure[("sector", holding.sector)] = (
                exposure.get(("sector", holding.sector), _ZERO) + value
            )

    action = rule_input.recommendation.action
    if action in {TradeAction.BUY, TradeAction.SWITCH}:
        delta = amount
    elif action is TradeAction.SELL:
        held = exposure.get(("instrument", product.instrument_id), _ZERO)
        delta = -min(amount, held)
    else:
        delta = _ZERO

    if delta != _ZERO:
        buckets = [("instrument", product.instrument_id), ("issuer", product.issuer)]
        if product.sector is not None:
            buckets.append(("sector", product.sector))
        for bucket in buckets:
            exposure[bucket] = exposure.get(bucket, _ZERO) + delta

    total = sum((holding.market_value.amount for holding in profile.holdings), _ZERO) + delta
    return exposure, total


def _percent(part: Decimal, whole: Decimal) -> str:
    """Render `part` as a percentage of `whole`, to two decimal places."""
    return f"{(part / whole * _HUNDRED).quantize(_PERCENT_QUANTUM)}%"


def check_concentration_breach(rule_input: RuleInput) -> RuleOutcome:
    """Refuse when post-recommendation exposure exceeds the configured limit.

    The limit is `RuleConfig.concentration_limit_fraction`, a repository-specific
    placeholder — see `CONCENTRATION_LIMIT_FRACTION`. Exposure is checked in every
    instrument, issuer, and sector of the post-recommendation portfolio, not only the
    ones the recommendation touches. A consequence worth stating: a sell that reduces an
    over-concentrated portfolio without curing it is still refused. The engine reports
    that the portfolio breaches the limit; it does not decide that an improvement is
    good enough.
    """
    profile = rule_input.profile
    if not profile.holdings:
        return _missing_factors(RefusalCode.CONCENTRATION_BREACH, (FactorKey.HOLDINGS,))

    basis = _basis(profile, FactorKey.HOLDINGS)
    exposure, total = _exposures(rule_input)
    if total <= _ZERO:
        return RuleCheck(code=RefusalCode.CONCENTRATION_BREACH, basis=basis)

    limit = rule_input.config.concentration_limit_fraction
    dimensions = ("instrument", "issuer", "sector")
    breaches = sorted(
        (
            (dimensions.index(dimension), value, amount)
            for (dimension, value), amount in exposure.items()
            if amount > total * limit
        ),
        key=lambda breach: (breach[0], breach[1]),
    )
    if breaches:
        described = "; ".join(
            f"{dimensions[index]} {value} at {_percent(amount, total)}"
            for index, value, amount in breaches
        )
        return RefusalReason(
            code=RefusalCode.CONCENTRATION_BREACH,
            detail=(
                f"Post-recommendation exposure exceeds the configured limit of "
                f"{_percent(limit, Decimal(1))}: {described}."
            ),
            conflicting_factors=basis,
        )
    return RuleCheck(code=RefusalCode.CONCENTRATION_BREACH, basis=basis)


def _normalise(text: str) -> str:
    """Collapse whitespace and case, so that a claim is compared on its content."""
    return " ".join(text.split()).casefold()


def _claim_is_supported(profile: ClientProfile, claim: FactorCitation) -> bool:
    """Return whether the profile documents exactly what `claim` asserts.

    Values are compared case-insensitively with whitespace collapsed, and the
    documentation date must match. Anything else is unsupported: the engine does not
    reinterpret a claim to make it fit.
    """
    documented = _cite(profile, claim.key)
    if documented is None:
        return False
    return (
        _normalise(documented.value) == _normalise(claim.value)
        and documented.documented_on == claim.documented_on
    )


def check_unsupported_rationale(rule_input: RuleInput) -> RuleOutcome:
    """Refuse when the stated reasoning claims a client fact the profile does not document.

    A rationale that cites nothing is refused too, under `missing_kyc_factor`: it is the
    only code whose evidence can express "a required input was not supplied". An
    uncited rationale anchors the recommendation to nothing, and a recommendation
    resting on an unstated assumption is exactly what this system refuses.
    """
    profile = rule_input.profile
    claims = rule_input.recommendation.rationale.cited_factors
    if not claims:
        return RefusalReason(
            code=RefusalCode.MISSING_KYC_FACTOR,
            detail=(
                "unsupported_rationale cannot be evaluated: the rationale cites no "
                "documented factor, so nothing anchors it to the profile."
            ),
            missing_factors=_REQUIRED_KYC_ORDER,
        )

    unsupported = tuple(claim for claim in claims if not _claim_is_supported(profile, claim))
    if unsupported:
        named = ", ".join(claim.key.value for claim in unsupported)
        return RefusalReason(
            code=RefusalCode.UNSUPPORTED_RATIONALE,
            detail=f"The rationale claims facts the profile does not document: {named}.",
            unsupported_claims=unsupported,
        )
    return RuleCheck(
        code=RefusalCode.UNSUPPORTED_RATIONALE,
        basis=_basis(profile, *_unique(claim.key for claim in claims)),
    )


def check_stale_profile(rule_input: RuleInput) -> RuleOutcome:
    """Refuse when the profile's review date is older than the configured interval.

    The interval is `RuleConfig.profile_review_interval_days`, a repository choice — see
    `PROFILE_REVIEW_INTERVAL_DAYS`.
    """
    profile = rule_input.profile
    basis = _basis(profile, FactorKey.LAST_REVIEWED)
    age_days = (rule_input.as_of - profile.last_reviewed).days
    interval = rule_input.config.profile_review_interval_days
    if age_days > interval:
        return RefusalReason(
            code=RefusalCode.STALE_PROFILE,
            detail=(
                f"Profile last reviewed {profile.last_reviewed.isoformat()}, "
                f"{age_days} days before the evaluation date "
                f"{rule_input.as_of.isoformat()}, exceeding the configured review "
                f"interval of {interval} days."
            ),
            conflicting_factors=basis,
        )
    return RuleCheck(code=RefusalCode.STALE_PROFILE, basis=basis)


RULES: tuple[tuple[RefusalCode, Rule], ...] = (
    (RefusalCode.MISSING_KYC_FACTOR, check_missing_kyc_factor),
    (RefusalCode.RISK_MISMATCH, check_risk_mismatch),
    (RefusalCode.HORIZON_MISMATCH, check_horizon_mismatch),
    (RefusalCode.LIQUIDITY_CONFLICT, check_liquidity_conflict),
    (RefusalCode.CONCENTRATION_BREACH, check_concentration_breach),
    (RefusalCode.UNSUPPORTED_RATIONALE, check_unsupported_rationale),
    (RefusalCode.STALE_PROFILE, check_stale_profile),
)
"""Every rule, in the order they run and in which their reasons are reported."""

if {code for code, _ in RULES} != set(RefusalCode):
    _uncovered = sorted(set(RefusalCode) - {code for code, _ in RULES})
    _message = f"RULES does not cover {_uncovered}"
    raise RuntimeError(_message)


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def _digest(record: BaseModel) -> str:
    """Return the SHA-256 of a record's canonical JSON form."""
    return hashlib.sha256(record.model_dump_json().encode()).hexdigest()


def _receipt_id(profile_digest: str, recommendation_digest: str, decided_at: dt.datetime) -> str:
    """Derive a receipt identifier from the exact inputs it will attest to.

    Derived rather than random so that the same inputs and the same injected evaluation
    time produce a byte-identical receipt.
    """
    decided = decided_at.isoformat()
    material = f"{ENGINE_VERSION}\n{profile_digest}\n{recommendation_digest}\n{decided}"
    return f"RCPT-{hashlib.sha256(material.encode()).hexdigest()[:_RECEIPT_ID_HEX_LENGTH]}"


def _merge_reasons(reasons: Iterable[RefusalReason]) -> tuple[RefusalReason, ...]:
    """Combine reasons sharing a code into one, keeping first-appearance order.

    Several rules can independently report the same missing factor, and a `Refusal` may
    carry each code only once. Merging keeps every piece of evidence rather than
    discarding whichever reason came second.
    """
    grouped: dict[RefusalCode, list[RefusalReason]] = {}
    for reason in reasons:
        grouped.setdefault(reason.code, []).append(reason)

    merged: list[RefusalReason] = []
    for code, group in grouped.items():
        if len(group) == 1:
            merged.append(group[0])
            continue
        merged.append(
            RefusalReason(
                code=code,
                detail=" ".join(_unique(reason.detail for reason in group)),
                missing_factors=_unique(key for reason in group for key in reason.missing_factors),
                conflicting_factors=_unique(
                    citation for reason in group for citation in reason.conflicting_factors
                ),
                unsupported_claims=_unique(
                    citation for reason in group for citation in reason.unsupported_claims
                ),
            ),
        )
    return tuple(merged)


def _check_preconditions(
    profile: ClientProfile,
    recommendation: Recommendation,
    now: dt.datetime,
) -> None:
    """Raise `ValueError` for inputs that are caller errors, not suitability questions."""
    if recommendation.client_id != profile.client_id:
        msg = (
            f"recommendation is for client {recommendation.client_id}, "
            f"profile is for {profile.client_id}"
        )
        raise ValueError(msg)
    if recommendation.amount.currency != profile.currency:
        msg = (
            f"recommendation amount is in {recommendation.amount.currency}, "
            f"profile is in {profile.currency}; the engine has no exchange rate"
        )
        raise ValueError(msg)
    if now.utcoffset() != dt.timedelta(0):
        msg = "now must be a UTC-aware datetime"
        raise ValueError(msg)
    if now.date() < profile.last_reviewed:
        msg = (
            f"evaluation time {now.date().isoformat()} precedes the profile's "
            f"documented review date {profile.last_reviewed.isoformat()}"
        )
        raise ValueError(msg)


def determine(
    profile: ClientProfile,
    recommendation: Recommendation,
    *,
    now: dt.datetime,
    config: RuleConfig = DEFAULT_CONFIG,
) -> SuitabilityOutcome:
    """Determine whether `recommendation` is supported by `profile`.

    Every rule runs. If any refuses, the result is a `Refusal` carrying all of them; if
    none does, the result is a `Determination` carrying one check per rule, each citing
    the documented factors that rule actually consulted.

    Args:
        profile: The documented client facts. Absence in it is an input, not a gap.
        recommendation: The proposal to determine, with the claims its rationale makes.
        now: The injected evaluation time. Must be UTC-aware; the engine reads no clock.
        config: The thresholds to determine against.

    Returns:
        A `Determination` or a `Refusal`. There is no third case.

    Raises:
        ValueError: If the recommendation is for another client, is denominated in a
            currency the profile does not use, or `now` is not UTC-aware or precedes the
            profile's documented review date.
    """
    _check_preconditions(profile, recommendation, now)

    rule_input = RuleInput(
        profile=profile,
        recommendation=recommendation,
        as_of=now.date(),
        config=config,
    )
    checks: list[RuleCheck] = []
    reasons: list[RefusalReason] = []
    for code, rule in RULES:
        outcome = rule(rule_input)
        if isinstance(outcome, RuleCheck):
            if outcome.code != code:
                msg = f"rule for {code} returned a check for {outcome.code}"
                raise RuntimeError(msg)
            checks.append(outcome)
        else:
            reasons.append(outcome)

    profile_digest = _digest(profile)
    recommendation_digest = _digest(recommendation)
    receipt_id = _receipt_id(profile_digest, recommendation_digest, now)
    if reasons:
        return Refusal(
            receipt_id=receipt_id,
            client_id=profile.client_id,
            recommendation_id=recommendation.recommendation_id,
            profile_digest=profile_digest,
            recommendation_digest=recommendation_digest,
            profile_last_reviewed=profile.last_reviewed,
            reasons=_merge_reasons(reasons),
            engine_version=ENGINE_VERSION,
            decided_at=now,
        )
    return Determination(
        receipt_id=receipt_id,
        client_id=profile.client_id,
        recommendation_id=recommendation.recommendation_id,
        profile_digest=profile_digest,
        recommendation_digest=recommendation_digest,
        profile_last_reviewed=profile.last_reviewed,
        checks=tuple(checks),
        engine_version=ENGINE_VERSION,
        decided_at=now,
    )
