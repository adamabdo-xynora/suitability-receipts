"""Domain models for suitability determinations.

These models define the *shape* of the problem and validate data at the boundary.
They contain no determination logic: nothing here decides whether a recommendation
is suitable. That is the job of the (not yet written) deterministic rule engine.

Three invariants are enforced here rather than left to the engine's good intentions,
because a determination that skipped a check — or that reported a finding without saying
what it is a finding about — must be impossible to represent at all:

1. A `Determination` must record a passing `RuleCheck` for every member of
   `RefusalCode`. A supported result is proof that all seven rules ran.
2. Every `RuleCheck` must cite at least one documented profile factor as its basis.
   No rule passes for an unstated reason.
3. A `RefusalReason` whose rule reads the documented portfolio must state a
   `BreachOrigin`. Those two rules can object to exposure the client already held, and a
   refusal that could not say whether the recommendation created that exposure or
   inherited and reduced it would report two different situations identically.

Each `FactorCitation` carries the profile field, the value as documented, and the
date it was documented, so that a verifier holding the profile can recompute the
citation rather than trust it.
"""

import datetime as dt
from collections.abc import Hashable
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

__all__ = [
    "BREACH_ORIGIN_CODES",
    "BREACH_ORIGIN_SEVERITY",
    "REQUIRED_KYC_FACTORS",
    "RISK_LEVEL_RANK",
    "BreachOrigin",
    "ClientProfile",
    "Currency",
    "Determination",
    "FactorCitation",
    "FactorKey",
    "Holding",
    "InvestmentKnowledge",
    "InvestmentObjective",
    "LiquidityRequirement",
    "Money",
    "Product",
    "Recommendation",
    "RecommendationRationale",
    "RedemptionFrequency",
    "Refusal",
    "RefusalCode",
    "RefusalReason",
    "RiskLevel",
    "RuleCheck",
    "SuitabilityOutcome",
    "TradeAction",
    "UpcomingExpense",
]


# ---------------------------------------------------------------------------
# Constrained scalars
# ---------------------------------------------------------------------------


def _reject_float(value: object) -> object:
    """Reject float input for quantities that must be exact.

    Binary floats cannot represent decimal money exactly. Accepting one here would
    silently introduce rounding error into a compliance record.

    Raises `ValueError` rather than `TypeError` so that pydantic folds it into a
    `ValidationError`: a caller validating untrusted input catches one exception type,
    not two.
    """
    if isinstance(value, float):
        msg = "float is not an exact quantity; pass a Decimal, int, or string"
        # TRY004 would prefer TypeError, but pydantic only folds ValueError and
        # AssertionError into ValidationError; a TypeError would escape uncaught.
        raise ValueError(msg)  # noqa: TRY004
    return value


NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
"""A string that is non-empty after surrounding whitespace is stripped."""

Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
"""A lowercase hex SHA-256 digest, used to bind a receipt to its exact inputs."""

ExactDecimal = Annotated[Decimal, BeforeValidator(_reject_float)]
"""A Decimal that refuses float input."""

MoneyAmount = Annotated[ExactDecimal, Field(ge=0, max_digits=18, decimal_places=2)]
"""A non-negative monetary amount with at most two decimal places."""

HorizonYears = Annotated[ExactDecimal, Field(gt=0, le=100, decimal_places=1)]
"""A time horizon in years, to a tenth of a year."""


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class Currency(StrEnum):
    """Currency of a monetary amount."""

    CAD = "CAD"
    USD = "USD"


class RiskLevel(StrEnum):
    """A point on the shared risk scale.

    The same scale carries a client's documented risk tolerance and a product's
    documented risk rating, so that `risk_mismatch` compares like with like.
    """

    LOW = "low"
    LOW_TO_MEDIUM = "low_to_medium"
    MEDIUM = "medium"
    MEDIUM_TO_HIGH = "medium_to_high"
    HIGH = "high"


RISK_LEVEL_RANK: MappingProxyType[RiskLevel, int] = MappingProxyType(
    {
        RiskLevel.LOW: 0,
        RiskLevel.LOW_TO_MEDIUM: 1,
        RiskLevel.MEDIUM: 2,
        RiskLevel.MEDIUM_TO_HIGH: 3,
        RiskLevel.HIGH: 4,
    },
)
"""Ordering of `RiskLevel`, stated explicitly rather than left to declaration order."""


class InvestmentObjective(StrEnum):
    """A documented investment objective."""

    CAPITAL_PRESERVATION = "capital_preservation"
    INCOME = "income"
    BALANCED = "balanced"
    GROWTH = "growth"
    SPECULATION = "speculation"


class InvestmentKnowledge(StrEnum):
    """A documented level of investment knowledge."""

    NONE = "none"
    LIMITED = "limited"
    GOOD = "good"
    SOPHISTICATED = "sophisticated"


class RedemptionFrequency(StrEnum):
    """How often a product may be redeemed."""

    DAILY = "daily"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"
    NONE = "none"


class TradeAction(StrEnum):
    """The action a recommendation proposes."""

    BUY = "buy"
    SELL = "sell"
    SWITCH = "switch"
    HOLD = "hold"


class FactorKey(StrEnum):
    """A citable field of a `ClientProfile`.

    Citations name a key from this enumeration rather than free text, so that a
    verifier can look the value up in the profile instead of interpreting prose.
    """

    RISK_TOLERANCE = "risk_tolerance"
    TIME_HORIZON_YEARS = "time_horizon_years"
    INVESTMENT_OBJECTIVES = "investment_objectives"
    INVESTMENT_KNOWLEDGE = "investment_knowledge"
    LIQUIDITY_REQUIREMENT = "liquidity_requirement"
    ANNUAL_INCOME = "annual_income"
    NET_WORTH = "net_worth"
    LIQUID_NET_WORTH = "liquid_net_worth"
    HOLDINGS = "holdings"
    LAST_REVIEWED = "last_reviewed"


REQUIRED_KYC_FACTORS: frozenset[FactorKey] = frozenset(
    {
        FactorKey.RISK_TOLERANCE,
        FactorKey.TIME_HORIZON_YEARS,
        FactorKey.INVESTMENT_OBJECTIVES,
    },
)
"""Factors whose absence is a `missing_kyc_factor` refusal."""


class RefusalCode(StrEnum):
    """The refusal taxonomy.

    Each member is a separately checked rule. A `Determination` must record a
    passing check for every one of them.
    """

    MISSING_KYC_FACTOR = "missing_kyc_factor"
    RISK_MISMATCH = "risk_mismatch"
    HORIZON_MISMATCH = "horizon_mismatch"
    LIQUIDITY_CONFLICT = "liquidity_conflict"
    CONCENTRATION_BREACH = "concentration_breach"
    UNSUPPORTED_RATIONALE = "unsupported_rationale"
    STALE_PROFILE = "stale_profile"


class BreachOrigin(StrEnum):
    """What a recommendation does to the exposure a rule objects to.

    Two rules read the documented portfolio, and so can object to something that was
    already in it. Without this, their refusals conflate two different situations: a
    recommendation that brings the objectionable exposure into being, and one that
    inherits it from the client's history and reduces it. Both still refuse. A receipt
    that could not tell them apart reports the advisor's proposal and the client's file
    as the same finding, and an advisor de-risking a position in stages gets the same
    answer at every stage but the last.

    `CREATED` is the only member that holds the recommendation responsible for the
    objection. The other three all say the objection predates it, and differ in what the
    recommendation does about it. `UNCHANGED` also covers having no exposure to move at
    all — a sell of an instrument the profile does not document holding — because such a
    recommendation likewise neither creates nor reduces one.

    This is not a softer outcome and does not license one. A `REDUCED` breach is still a
    breach, the refusal still fires with the same force, and the residual is still stated
    in `RefusalReason.detail`. What the field adds is that the receipt says which of the
    two situations it is, rather than leaving them indistinguishable.
    """

    CREATED = "created"
    INCREASED = "increased"
    UNCHANGED = "unchanged"
    REDUCED = "reduced"


BREACH_ORIGIN_SEVERITY: MappingProxyType[BreachOrigin, int] = MappingProxyType(
    {
        BreachOrigin.REDUCED: 0,
        BreachOrigin.UNCHANGED: 1,
        BreachOrigin.INCREASED: 2,
        BreachOrigin.CREATED: 3,
    },
)
"""Ordering of `BreachOrigin`, stated explicitly rather than left to declaration order.

One reason can cover several breaches at once — an instrument, its issuer and its sector,
or unrelated buckets that breach for unrelated causes. Such a reason reports the most
severe origin among them, so that a recommendation reducing one breach while creating
another cannot read as a reduction."""


BREACH_ORIGIN_CODES: frozenset[RefusalCode] = frozenset(
    {
        RefusalCode.RISK_MISMATCH,
        RefusalCode.CONCENTRATION_BREACH,
    },
)
"""The codes whose refusal must state a `BreachOrigin`, and the only ones that may.

These are the two rules that read the documented portfolio, so they are the two whose
objection can predate the recommendation. The others cannot: `missing_kyc_factor`,
`unsupported_rationale` and `stale_profile` are findings about the file rather than about
exposure; `horizon_mismatch` is a finding about the product's terms; and
`liquidity_conflict` already counts a disposal as consuming nothing, so it never objects
to a recommendation that reduces what it measures."""


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


class _Record(BaseModel):
    """Base for every model here: immutable, closed, and validated on construction."""

    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)


def _require_unique(items: tuple[Hashable, ...], label: str) -> None:
    """Raise if `items` contains a duplicate."""
    if len(set(items)) != len(items):
        msg = f"duplicate {label}"
        raise ValueError(msg)


def _require_not_future(value: dt.date, label: str) -> None:
    """Raise if `value` is a future date; a fact cannot have been documented tomorrow."""
    if value > dt.datetime.now(tz=dt.UTC).date():
        msg = f"{label} is in the future"
        raise ValueError(msg)


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


class Money(_Record):
    """An exact monetary amount in a stated currency."""

    amount: MoneyAmount
    currency: Currency


class UpcomingExpense(_Record):
    """A known upcoming expense documented against a client's liquidity needs."""

    description: NonEmptyStr
    amount: Money
    expected_on: dt.date


class LiquidityRequirement(_Record):
    """A client's documented liquidity needs."""

    emergency_reserve: Money
    upcoming_expenses: tuple[UpcomingExpense, ...] = ()


class Holding(_Record):
    """A position in the client's current portfolio."""

    instrument_id: NonEmptyStr
    name: NonEmptyStr
    issuer: NonEmptyStr
    sector: NonEmptyStr | None = None
    market_value: Money


class FactorCitation(_Record):
    """A claim that the profile documents `value` for `key`, as of `documented_on`.

    A citation is a checkable assertion, not a note. Later code re-reads the profile
    and compares, rather than trusting the citation's contents.
    """

    key: FactorKey
    value: NonEmptyStr
    documented_on: dt.date

    @model_validator(mode="after")
    def _check_documented_on(self) -> Self:
        _require_not_future(self.documented_on, "documented_on")
        return self


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


class ClientProfile(_Record):
    """The documented client facts a determination may rely on.

    Every KYC field is optional, and that is deliberate: absence is the input to the
    `missing_kyc_factor` rule, so it must be representable. A `None` here means "not
    documented", never "unknown but probably fine".
    """

    client_id: NonEmptyStr
    last_reviewed: dt.date
    currency: Currency
    risk_tolerance: RiskLevel | None = None
    time_horizon_years: HorizonYears | None = None
    investment_objectives: tuple[InvestmentObjective, ...] = ()
    investment_knowledge: InvestmentKnowledge | None = None
    liquidity_requirement: LiquidityRequirement | None = None
    annual_income: Money | None = None
    net_worth: Money | None = None
    liquid_net_worth: Money | None = None
    holdings: tuple[Holding, ...] = ()

    @model_validator(mode="after")
    def _check_profile(self) -> Self:
        _require_not_future(self.last_reviewed, "last_reviewed")
        _require_unique(self.investment_objectives, "investment objective")
        _require_unique(tuple(h.instrument_id for h in self.holdings), "holding instrument_id")

        amounts: list[Money] = [
            money
            for money in (self.annual_income, self.net_worth, self.liquid_net_worth)
            if money is not None
        ]
        if self.liquidity_requirement is not None:
            amounts.append(self.liquidity_requirement.emergency_reserve)
            amounts.extend(e.amount for e in self.liquidity_requirement.upcoming_expenses)
        amounts.extend(h.market_value for h in self.holdings)
        foreign = sorted({m.currency for m in amounts} - {self.currency})
        if foreign:
            msg = f"profile currency is {self.currency}, but it also contains {foreign}"
            raise ValueError(msg)

        if (
            self.net_worth is not None
            and self.liquid_net_worth is not None
            and self.liquid_net_worth.amount > self.net_worth.amount
        ):
            msg = "liquid_net_worth exceeds net_worth"
            raise ValueError(msg)
        return self


class Product(_Record):
    """The product a recommendation concerns, with its documented terms."""

    instrument_id: NonEmptyStr
    name: NonEmptyStr
    issuer: NonEmptyStr
    sector: NonEmptyStr | None = None
    risk_rating: RiskLevel
    risk_rating_source: NonEmptyStr
    lock_up_days: Annotated[int, Field(ge=0)] = 0
    redemption_frequency: RedemptionFrequency
    recommended_holding_period_years: HorizonYears | None = None


class RecommendationRationale(_Record):
    """The stated reasoning for a recommendation, and the client facts it claims.

    `cited_factors` holds claims to be checked, not findings. If the model parsing a
    free-text recommendation asserts something about the client, it lands here and the
    `unsupported_rationale` rule compares it against the profile.
    """

    text: NonEmptyStr
    cited_factors: tuple[FactorCitation, ...] = ()

    @model_validator(mode="after")
    def _check_cited_factors(self) -> Self:
        _require_unique(self.cited_factors, "cited factor")
        return self


class Recommendation(_Record):
    """A proposed investment recommendation awaiting determination."""

    recommendation_id: NonEmptyStr
    client_id: NonEmptyStr
    action: TradeAction
    product: Product
    amount: Money
    proposed_on: dt.date
    rationale: RecommendationRationale
    source_text: NonEmptyStr | None = None
    parsed_by: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _check_recommendation(self) -> Self:
        if self.amount.amount <= 0:
            msg = "recommendation amount must be positive"
            raise ValueError(msg)
        if (self.source_text is None) != (self.parsed_by is None):
            msg = "source_text and parsed_by must be provided together"
            raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


class RuleCheck(_Record):
    """A record that one taxonomy rule was evaluated and did not refuse.

    `basis` is what the rule looked at. It is required and non-empty: a rule that
    passed without consulting a documented fact has not established anything.
    """

    code: RefusalCode
    basis: Annotated[tuple[FactorCitation, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _check_basis(self) -> Self:
        _require_unique(self.basis, "basis citation")
        return self


class Determination(_Record):
    """A supported determination: the receipt for a recommendation that passed.

    Carries the digests of the exact profile and recommendation it was computed from,
    so the receipt can be re-verified against its inputs rather than believed.
    """

    outcome: Literal["supported"] = "supported"
    receipt_id: NonEmptyStr
    client_id: NonEmptyStr
    recommendation_id: NonEmptyStr
    profile_digest: Sha256Hex
    recommendation_digest: Sha256Hex
    profile_last_reviewed: dt.date
    checks: tuple[RuleCheck, ...]
    engine_version: NonEmptyStr
    decided_at: AwareDatetime

    @model_validator(mode="after")
    def _check_full_coverage(self) -> Self:
        codes = tuple(check.code for check in self.checks)
        _require_unique(codes, "rule check")
        unchecked = sorted(set(RefusalCode) - set(codes))
        if unchecked:
            msg = f"determination is missing checks for {unchecked}"
            raise ValueError(msg)
        if self.decided_at.utcoffset() != dt.timedelta(0):
            msg = "decided_at must be UTC"
            raise ValueError(msg)
        return self


_EVIDENCE_FOR_CODE: MappingProxyType[RefusalCode, str] = MappingProxyType(
    {
        RefusalCode.MISSING_KYC_FACTOR: "missing_factors",
        RefusalCode.UNSUPPORTED_RATIONALE: "unsupported_claims",
        RefusalCode.RISK_MISMATCH: "conflicting_factors",
        RefusalCode.HORIZON_MISMATCH: "conflicting_factors",
        RefusalCode.LIQUIDITY_CONFLICT: "conflicting_factors",
        RefusalCode.CONCENTRATION_BREACH: "conflicting_factors",
        RefusalCode.STALE_PROFILE: "conflicting_factors",
    },
)
"""Which evidence field each refusal code must populate, and only that one."""


class RefusalReason(_Record):
    """One reason a recommendation was refused, with the evidence for it.

    Exactly one evidence field is populated, determined by `code`: a missing-factor
    refusal names missing factors, an unsupported-rationale refusal names the claims
    the profile does not support, and the rest name the documented facts that conflict.

    `breach_origin` is present on exactly the codes in `BREACH_ORIGIN_CODES` and absent
    on every other, both enforced here. A rule that reads the documented portfolio cannot
    report a breach without saying whether this recommendation created it or inherited it
    — the two are different findings, and the model makes the undifferentiated one
    unrepresentable rather than leaving it to the engine's good intentions.
    """

    code: RefusalCode
    detail: NonEmptyStr
    missing_factors: tuple[FactorKey, ...] = ()
    conflicting_factors: tuple[FactorCitation, ...] = ()
    unsupported_claims: tuple[FactorCitation, ...] = ()
    breach_origin: BreachOrigin | None = None

    @model_validator(mode="after")
    def _check_evidence(self) -> Self:
        _require_unique(self.missing_factors, "missing factor")
        _require_unique(self.conflicting_factors, "conflicting factor")
        _require_unique(self.unsupported_claims, "unsupported claim")

        states_origin = self.code in BREACH_ORIGIN_CODES
        if states_origin and self.breach_origin is None:
            msg = f"{self.code} requires a breach_origin"
            raise ValueError(msg)
        if not states_origin and self.breach_origin is not None:
            msg = f"{self.code} must not carry a breach_origin"
            raise ValueError(msg)

        evidence: dict[str, tuple[object, ...]] = {
            "missing_factors": self.missing_factors,
            "conflicting_factors": self.conflicting_factors,
            "unsupported_claims": self.unsupported_claims,
        }
        expected = _EVIDENCE_FOR_CODE[self.code]
        for name, value in evidence.items():
            if name == expected and not value:
                msg = f"{self.code} requires non-empty {name}"
                raise ValueError(msg)
            if name != expected and value:
                msg = f"{self.code} must not carry {name}"
                raise ValueError(msg)
        return self


class Refusal(_Record):
    """A refusal: the receipt for a recommendation that could not be supported.

    Carries every reason that fired, not just the first. Reporting one of several
    problems would understate what is wrong, which is its own kind of hedge.
    """

    outcome: Literal["refused"] = "refused"
    receipt_id: NonEmptyStr
    client_id: NonEmptyStr
    recommendation_id: NonEmptyStr
    profile_digest: Sha256Hex
    recommendation_digest: Sha256Hex
    profile_last_reviewed: dt.date
    reasons: Annotated[tuple[RefusalReason, ...], Field(min_length=1)]
    engine_version: NonEmptyStr
    decided_at: AwareDatetime

    @model_validator(mode="after")
    def _check_reasons(self) -> Self:
        _require_unique(tuple(reason.code for reason in self.reasons), "refusal code")
        if self.decided_at.utcoffset() != dt.timedelta(0):
            msg = "decided_at must be UTC"
            raise ValueError(msg)
        return self


SuitabilityOutcome = Annotated[Determination | Refusal, Field(discriminator="outcome")]
"""The only two things the system may produce. There is no third case."""
