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
* A profile that documents no portfolio cannot establish that a recommendation reduces
  an existing exposure, so `check_risk_mismatch` records the origin it can defend rather
  than the one that reads best: a purchase creates the exposure, and a disposal it cannot
  see leaves it unchanged. `check_concentration_breach` does not face the question, since
  it refuses outright when the portfolio is undocumented.
* `ClientProfile.holdings` defaults to `()`, so an empty tuple cannot be distinguished
  from "the portfolio was never documented". `check_concentration_breach` reads it as
  undocumented and refuses, rather than as an empty portfolio that any purchase would
  fit into.
* A rationale claim that nearly matches the profile is unsupported. The engine does not
  round a claim toward the documented value, and does not treat a claim dated to a
  different documentation event as verified.
* A reason covering several breaches at once reports the most severe `BreachOrigin`
  among them, so that reducing one breach while creating another does not read as a
  reduction.

Breaches the recommendation did not create
------------------------------------------
`risk_mismatch` and `concentration_breach` are the two rules that read the documented
portfolio, so they are the two that can object to exposure the client already held. Both
used to ignore the recommendation's action entirely, which meant a `sell` that strictly
reduced the exposure being objected to was refused in the same terms as a `buy` that
created it. The outcome was right and the receipt was not: it reported the client's
history and the advisor's proposal as one finding, and an advisor de-risking a position
in stages got the same answer at every stage but the last.

Both rules still refuse. What changed is that each now states a `BreachOrigin` on its
reason, and each says in `detail` what the exposure was before the recommendation and
what it would be after. Nothing passes that did not pass before: a reduced breach is
still a breach and the residual is still on the receipt. The two rules share the enum,
the field, and the principle, and differ in what "the exposure being objected to" means —
for concentration it is a bucket over the configured limit, for risk it is any holding of
a product rated above the effective ceiling — so each classifies its own before-and-after
and they do not share a classifier. See `_concentration_origin` and `_risk_origin`.

A limit worth stating plainly: selling a position *entirely* still refuses under
`risk_mismatch`, with origin `reduced`. That rule objects to the product's rating, which
no disposal changes. Whether the objection should lift once the exposure reaches zero is
a separate question from this one, and it is not answered here.

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
from collections.abc import Callable, Hashable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType

from pydantic import BaseModel

from suitability_receipts.models import (
    BREACH_ORIGIN_SEVERITY,
    REQUIRED_KYC_FACTORS,
    RISK_LEVEL_RANK,
    BreachOrigin,
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
    "breach_origin_of",
    "check_concentration_breach",
    "check_horizon_mismatch",
    "check_liquidity_conflict",
    "check_missing_kyc_factor",
    "check_risk_mismatch",
    "check_stale_profile",
    "check_unsupported_rationale",
    "citation_matches_profile",
    "determine",
    "documented_citation",
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

MEASURED, and only this far. The eval set (`eval/cases.py`) pins the boundary at
19.999993% supported, 20.000000% supported, 20.000007% refused — so the limit is
enforced exactly where this docstring says it is, and the maximum is itself permitted
rather than being the first value refused. That is the semantics measured and kept.

STILL A GUESS: the level. No case in the eval set is evidence that 20% is better than
15% or 25%, because every case that turns on the level was constructed from the level.
Measuring it needs a source of truth outside this repository — a stated policy, a
regulator's figure, or outcome data — and there is none yet. Until there is, treat every
`concentration_breach` refusal as "exceeded the configured limit", never as "exceeded a
required limit"."""

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

MEASURED, and only this far. The eval set pins the boundary at 364 days supported, 365
supported, 366 refused: the interval is the age a profile is allowed to reach and not
the age at which it fails, so a profile reviewed exactly a year ago still determines.
That inclusive reading is a decision, it is now tested, and it is kept.

STILL A GUESS: the level. Nothing in the eval set distinguishes 365 from 300 or from
400 — the cases that sit on the boundary were derived from the constant, so they move
with it. Replacing it with a sourced interval remains work for the regulatory section."""

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
lower the effective ceiling, never to raise it above the documented risk tolerance.

MEASURED: the mapping is load-bearing and each entry means the level it names. The eval
set holds a case at each side of the growth entry — a medium product supported, a
medium-to-high product supported, a high product refused, all against a documented *high*
tolerance, so the objective is doing the constraining on its own.

STILL A GUESS, and with a consequence worth stating: no case is evidence that growth
belongs at medium-to-high rather than at high, or income at low-to-medium. The rows are
unsourced, and because the effective ceiling is the minimum over every documented
objective, an unsourced row is not a local decision. A profile documenting both income
and growth — an ordinary pair on a real KYC form — caps at low-to-medium, so a
medium-rated product is refused for a client whose documented tolerance is medium. That
is the mapping working as specified. Whether it is calibrated is a different question,
and this set does not answer it."""

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
# Exposure, and whose breach it is
#
# Shared by the two rules that read the documented portfolio, and therefore by the two
# that can object to something the client already held.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Exposures:
    """Bucket exposure and portfolio total, on both sides of the recommendation.

    Both sides are kept because a rule that objects to an exposure has to be able to say
    whether the recommendation is responsible for it. Computing only the "after" side is
    what made a disposal indistinguishable from a purchase.
    """

    before: Mapping[tuple[str, str], Decimal]
    before_total: Decimal
    after: Mapping[tuple[str, str], Decimal]
    after_total: Decimal

    def amounts(self, bucket: tuple[str, str]) -> tuple[Decimal, Decimal]:
        """Return the exposure in `bucket` before and after the recommendation."""
        return self.before.get(bucket, _ZERO), self.after.get(bucket, _ZERO)


def _portfolio_exposure(profile: ClientProfile) -> tuple[dict[tuple[str, str], Decimal], Decimal]:
    """Return documented exposure per (dimension, value), and the portfolio total."""
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
    total = sum((holding.market_value.amount for holding in profile.holdings), _ZERO)
    return exposure, total


def _exposures(rule_input: RuleInput) -> _Exposures:
    """Return exposure per (dimension, value) before and after the recommendation.

    A buy or a switch adds the full amount to the product's instrument, issuer, and
    sector buckets, gross. A sell removes what is actually held of that instrument, and
    no more. A hold changes nothing.
    """
    product = rule_input.recommendation.product
    amount = rule_input.recommendation.amount.amount
    before, before_total = _portfolio_exposure(rule_input.profile)

    action = rule_input.recommendation.action
    if action in {TradeAction.BUY, TradeAction.SWITCH}:
        delta = amount
    elif action is TradeAction.SELL:
        held = before.get(("instrument", product.instrument_id), _ZERO)
        delta = -min(amount, held)
    else:
        delta = _ZERO

    after = dict(before)
    if delta != _ZERO:
        buckets = [("instrument", product.instrument_id), ("issuer", product.issuer)]
        if product.sector is not None:
            buckets.append(("sector", product.sector))
        for bucket in buckets:
            after[bucket] = after.get(bucket, _ZERO) + delta

    return _Exposures(
        before=before,
        before_total=before_total,
        after=after,
        after_total=before_total + delta,
    )


def _percent(part: Decimal, whole: Decimal) -> str:
    """Render `part` as a percentage of `whole`, to two decimal places."""
    return f"{(part / whole * _HUNDRED).quantize(_PERCENT_QUANTUM)}%"


# ---------------------------------------------------------------------------
# Whose breach it is
#
# One enum, one field, one principle, and two classifiers — because the two rules that
# can inherit a breach do not agree on what "the exposure being objected to" is. For
# `concentration_breach` it is a bucket over the configured limit, so the created case
# is "this bucket was inside the limit before". For `risk_mismatch` there is no limit to
# be over: the objection is to holding a product rated above the ceiling at all, so the
# created case is "the profile documents no position at all". Forcing one function to
# serve both would mean pretending those are the same question.
# ---------------------------------------------------------------------------


def breach_origin_of(origins: Iterable[BreachOrigin]) -> BreachOrigin:
    """Return the most severe of `origins`, ordered by `BREACH_ORIGIN_SEVERITY`.

    One reason can cover several breaches at once. Reporting the mildest of them, or
    whichever came first, would let a recommendation that creates one breach while
    reducing another read as a reduction.

    Raises:
        ValueError: If `origins` is empty. There is no neutral origin to fall back to,
            and inventing one would be the understatement this exists to prevent.
    """
    ordered = sorted(origins, key=lambda origin: BREACH_ORIGIN_SEVERITY[origin])
    if not ordered:
        msg = "no breach origins to summarise"
        raise ValueError(msg)
    return ordered[-1]


def _concentration_origin(
    before: Decimal,
    after: Decimal,
    *,
    breached_before: bool,
) -> BreachOrigin:
    """Classify a breaching bucket by what the recommendation did to it.

    `breached_before` carries the whole created-versus-inherited distinction: a bucket
    inside the limit before and outside it now was put there by this recommendation,
    whether by adding to the bucket or by shrinking the portfolio it is measured against.

    Direction is then read from the *amount*, never from the fraction. A purchase of
    something else lowers an over-concentrated position's share by enlarging the
    denominator, and diluting an exposure is not disposing of one — the client's holding
    of the concentrated name is the same size it was.
    """
    if not breached_before:
        return BreachOrigin.CREATED
    if after > before:
        return BreachOrigin.INCREASED
    if after < before:
        return BreachOrigin.REDUCED
    return BreachOrigin.UNCHANGED


def _risk_origin(before: Decimal, after: Decimal) -> BreachOrigin:
    """Classify a risk objection by what the recommendation does to the position.

    This rule objects to holding a product rated above the effective ceiling, so the
    exposure it measures is the position in that instrument and there is no limit for it
    to be over: any position at all is the thing objected to. `CREATED` therefore means
    the profile documents no position and the recommendation would open one — which is
    also how an undocumented portfolio reads, deliberately, because a profile that
    documents no holdings cannot establish that a position already existed.

    Zero before and zero after is a breach nobody created: a sell or a hold of an
    instrument the profile does not document holding moves no exposure, and the refusal
    stands on the product's rating alone.
    """
    if after > before:
        return BreachOrigin.CREATED if before <= _ZERO else BreachOrigin.INCREASED
    if after < before:
        return BreachOrigin.REDUCED
    return BreachOrigin.UNCHANGED


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


def _risk_origin_detail(
    rule_input: RuleInput,
    origin: BreachOrigin,
    before: Decimal,
    after: Decimal,
) -> str:
    """Say what the recommendation does to the client's position in the rated product.

    The sentence that keeps a `reduced` refusal from reading like a `created` one. It
    states both sides of the position in figures rather than characterising the trade,
    so that a reader can check it against `profile.holdings` instead of taking it.
    """
    profile = rule_input.profile
    currency = profile.currency.value
    instrument = rule_input.recommendation.product.instrument_id
    action = rule_input.recommendation.action.value
    if not profile.holdings:
        held = (
            f"The profile documents no portfolio, so no existing position in {instrument} "
            f"can be established"
        )
    elif before <= _ZERO:
        held = f"The profile documents no position in {instrument}"
    else:
        held = f"The profile documents {before} {currency} in {instrument}"
    if origin is BreachOrigin.CREATED:
        return f"{held}; this {action} would open one of {after} {currency}."
    if origin is BreachOrigin.INCREASED:
        return f"{held}; this {action} would raise it to {after} {currency}."
    if origin is BreachOrigin.REDUCED:
        return (
            f"{held}; this {action} would reduce it to {after} {currency}. The refusal "
            f"is about the product's rating, which a disposal does not change."
        )
    if before <= _ZERO:
        return f"{held}, and this {action} would not open one."
    return f"{held}, which this {action} does not change."


def check_risk_mismatch(rule_input: RuleInput) -> RuleOutcome:
    """Refuse when the product's risk rating exceeds what the profile supports.

    The ceiling is the most conservative of the documented risk tolerance and the
    ceiling implied by each documented objective. A profile in tension with itself does
    not license the riskier reading of itself.

    The refusal states a `BreachOrigin`: a purchase of a product rated above the ceiling
    creates the exposure, a disposal of one already held reduces it, and the two are not
    the same finding even though both refuse. Reducing a position does not lift the
    objection — the rating is a fact about the product, not about the size of the
    position — so a `reduced` refusal is a refusal in full. What it is not is a claim
    that the advisor proposed the unsuitable exposure.

    Holdings are read only to classify the origin, never to decide the outcome, so the
    rule does not require them: a client whose portfolio is undocumented still gets told
    that the product is too risky, rather than being told only that their portfolio is
    undocumented. `HOLDINGS` is cited on the refusal exactly when the profile documents
    it, because that is when the rule actually consulted it.
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
        before, after = _exposures(rule_input).amounts(("instrument", product.instrument_id))
        origin = _risk_origin(before, after)
        return RefusalReason(
            code=RefusalCode.RISK_MISMATCH,
            detail=(
                f"Product risk rating {product.risk_rating.value} "
                f"(source: {product.risk_rating_source}) exceeds what the profile supports: "
                f"documented risk tolerance {tolerance.value}, "
                f"objectives {_documented_value(profile, FactorKey.INVESTMENT_OBJECTIVES)}. "
                f"{_risk_origin_detail(rule_input, origin, before, after)}"
            ),
            conflicting_factors=(*basis, *_basis(profile, FactorKey.HOLDINGS)),
            breach_origin=origin,
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


_CONCENTRATION_ORIGIN_PHRASE: MappingProxyType[BreachOrigin, str] = MappingProxyType(
    {
        BreachOrigin.CREATED: "created by this recommendation",
        BreachOrigin.INCREASED: "already over the limit, and increased by this recommendation",
        BreachOrigin.UNCHANGED: "already over the limit, and not reduced by this recommendation",
        BreachOrigin.REDUCED: (
            "already over the limit, and reduced by this recommendation without curing it"
        ),
    },
)
"""How each origin is described against a single breaching bucket."""

_CONCENTRATION_ORIGIN_SUMMARY: MappingProxyType[BreachOrigin, str] = MappingProxyType(
    {
        BreachOrigin.CREATED: "This recommendation creates a breach that did not exist before it.",
        BreachOrigin.INCREASED: "The breach predates this recommendation, which increases it.",
        BreachOrigin.UNCHANGED: (
            "The breach predates this recommendation, which does not reduce it."
        ),
        BreachOrigin.REDUCED: (
            "The breach predates this recommendation, which reduces it without bringing it "
            "inside the limit."
        ),
    },
)
"""How the reason's overall origin is stated, once, at the end of the detail."""


def _breach_share(amount: Decimal, total: Decimal, currency: str) -> str:
    """Render a bucket's exposure as a share of the portfolio, or as an amount.

    A portfolio with nothing in it has no share to express, and dividing by it would
    raise rather than report. The amount is the honest fallback.
    """
    return _percent(amount, total) if total > _ZERO else f"{amount} {currency}"


def check_concentration_breach(rule_input: RuleInput) -> RuleOutcome:
    """Refuse when post-recommendation exposure exceeds the configured limit.

    The limit is `RuleConfig.concentration_limit_fraction`, a repository-specific
    placeholder — see `CONCENTRATION_LIMIT_FRACTION`. Exposure is checked in every
    instrument, issuer, and sector of the post-recommendation portfolio, not only the
    ones the recommendation touches. A consequence worth stating: a sell that reduces an
    over-concentrated portfolio without curing it is still refused. The engine reports
    that the portfolio breaches the limit; it does not decide that an improvement is
    good enough.

    What it now also reports is which of those situations it is. Each breaching bucket is
    compared against the same bucket in the documented portfolio, and the reason carries
    the most severe `BreachOrigin` among them. The distinction is drawn on the exposure
    *amount*, not on its share: a purchase of something else lowers an over-concentrated
    position's percentage by enlarging the portfolio, and that is dilution rather than
    remediation — the client's holding of the concentrated name has not moved.

    A consequence of checking every bucket rather than only the ones touched: a disposal
    can *create* a breach in a bucket it never touches. Selling one position shrinks the
    portfolio every other bucket is measured against, so a holding sitting exactly at the
    limit can be over it afterwards without having moved a cent. `created` is the right
    word for that — the recommendation is what put the bucket over the line — and it is
    why the aggregate is the most severe origin rather than the one belonging to the
    instrument being traded.
    """
    profile = rule_input.profile
    if not profile.holdings:
        return _missing_factors(RefusalCode.CONCENTRATION_BREACH, (FactorKey.HOLDINGS,))

    basis = _basis(profile, FactorKey.HOLDINGS)
    exposures = _exposures(rule_input)
    total = exposures.after_total
    if total <= _ZERO:
        return RuleCheck(code=RefusalCode.CONCENTRATION_BREACH, basis=basis)

    limit = rule_input.config.concentration_limit_fraction
    dimensions = ("instrument", "issuer", "sector")
    breaches = sorted(
        (
            (dimensions.index(dimension), value, amount)
            for (dimension, value), amount in exposures.after.items()
            if amount > total * limit
        ),
        key=lambda breach: (breach[0], breach[1]),
    )
    if breaches:
        currency = profile.currency.value
        origins: list[BreachOrigin] = []
        described: list[str] = []
        for index, value, amount in breaches:
            before, _ = exposures.amounts((dimensions[index], value))
            origin = _concentration_origin(
                before,
                amount,
                breached_before=before > exposures.before_total * limit,
            )
            origins.append(origin)
            described.append(
                f"{dimensions[index]} {value} at {_percent(amount, total)} "
                f"(was {_breach_share(before, exposures.before_total, currency)}, "
                f"{_CONCENTRATION_ORIGIN_PHRASE[origin]})"
            )
        overall = breach_origin_of(origins)
        return RefusalReason(
            code=RefusalCode.CONCENTRATION_BREACH,
            detail=(
                f"Post-recommendation exposure exceeds the configured limit of "
                f"{_percent(limit, Decimal(1))}: {'; '.join(described)}. "
                f"{_CONCENTRATION_ORIGIN_SUMMARY[overall]}"
            ),
            conflicting_factors=basis,
            breach_origin=overall,
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


# ---------------------------------------------------------------------------
# The profile reading, exposed
#
# Code outside the engine has to check a citation against the profile: the rationale
# verifier in `suitability_receipts.llm` does exactly that, on prose the model wrote.
# It reads the profile through these two functions rather than through its own copy of
# the rendering, because a second implementation would be a second definition of what
# the profile says, and the two would eventually disagree. When they disagreed, a
# rationale could pass verification and then be refused by `check_unsupported_rationale`
# on the same inputs — or, worse, the other way round.
# ---------------------------------------------------------------------------


def documented_citation(profile: ClientProfile, key: FactorKey) -> FactorCitation | None:
    """Cite the profile's documented value for `key`, or `None` if it documents none."""
    return _cite(profile, key)


def citation_matches_profile(profile: ClientProfile, citation: FactorCitation) -> bool:
    """Return whether the profile documents exactly what `citation` asserts."""
    return _claim_is_supported(profile, citation)


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

    A merged reason takes the most severe `BreachOrigin` of the group, for the same
    reason a single reason covering several buckets does: the merge must not be a way for
    a created breach to be reported as a reduced one.
    """
    grouped: dict[RefusalCode, list[RefusalReason]] = {}
    for reason in reasons:
        grouped.setdefault(reason.code, []).append(reason)

    merged: list[RefusalReason] = []
    for code, group in grouped.items():
        if len(group) == 1:
            merged.append(group[0])
            continue
        origins = [reason.breach_origin for reason in group if reason.breach_origin is not None]
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
                breach_origin=breach_origin_of(origins) if origins else None,
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
