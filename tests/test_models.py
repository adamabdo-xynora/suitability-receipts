"""Validation tests for the domain models.

All client data here is synthetic and obviously so: identifiers carry a `SYNTHETIC-`
prefix and names are placeholders. No real client data and no credentials appear in
this repository, including in fixtures.
"""

import datetime as dt
import hashlib
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import TypeAdapter, ValidationError

from suitability_receipts import (
    ClientProfile,
    Currency,
    Determination,
    FactorCitation,
    FactorKey,
    Holding,
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
    SuitabilityOutcome,
    TradeAction,
    UpcomingExpense,
)
from suitability_receipts.models import RISK_LEVEL_RANK

REVIEWED_ON = dt.date(2025, 6, 1)
DECIDED_AT = dt.datetime(2025, 7, 1, 12, 0, tzinfo=dt.UTC)
PROFILE_DIGEST = hashlib.sha256(b"SYNTHETIC-PROFILE").hexdigest()
RECOMMENDATION_DIGEST = hashlib.sha256(b"SYNTHETIC-RECOMMENDATION").hexdigest()


def cad(amount: str) -> Money:
    """Build a synthetic CAD amount."""
    return Money(amount=Decimal(amount), currency=Currency.CAD)


def a_citation(key: FactorKey = FactorKey.RISK_TOLERANCE, value: str = "medium") -> FactorCitation:
    """Build a citation against the synthetic profile."""
    return FactorCitation(key=key, value=value, documented_on=REVIEWED_ON)


def a_profile(**overrides: object) -> ClientProfile:
    """Build a fully documented synthetic client profile."""
    fields: dict[str, object] = {
        "client_id": "SYNTHETIC-0001",
        "last_reviewed": REVIEWED_ON,
        "currency": Currency.CAD,
        "risk_tolerance": RiskLevel.MEDIUM,
        "time_horizon_years": Decimal(10),
        "investment_objectives": (InvestmentObjective.GROWTH,),
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
        "net_worth": cad("500000.00"),
        "liquid_net_worth": cad("120000.00"),
        "holdings": (
            Holding(
                instrument_id="SYNTHETIC-FUND-A",
                name="Synthetic Placeholder Fund A",
                issuer="Synthetic Placeholder Issuer",
                sector="diversified",
                market_value=cad("100000.00"),
            ),
        ),
    }
    fields.update(overrides)
    return ClientProfile.model_validate(fields)


def a_product(**overrides: object) -> Product:
    """Build a synthetic product with documented terms."""
    fields: dict[str, object] = {
        "instrument_id": "SYNTHETIC-FUND-B",
        "name": "Synthetic Placeholder Fund B",
        "issuer": "Synthetic Placeholder Issuer",
        "sector": "diversified",
        "risk_rating": RiskLevel.MEDIUM,
        "risk_rating_source": "SYNTHETIC fund facts sheet",
        "redemption_frequency": RedemptionFrequency.DAILY,
    }
    fields.update(overrides)
    return Product.model_validate(fields)


def a_recommendation(**overrides: object) -> Recommendation:
    """Build a synthetic recommendation."""
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


def all_checks() -> tuple[RuleCheck, ...]:
    """Build a passing check for every code in the taxonomy."""
    return tuple(RuleCheck(code=code, basis=(a_citation(),)) for code in RefusalCode)


def a_determination(**overrides: object) -> Determination:
    """Build a supported determination covering the whole taxonomy."""
    fields: dict[str, object] = {
        "receipt_id": "SYNTHETIC-RECEIPT-0001",
        "client_id": "SYNTHETIC-0001",
        "recommendation_id": "SYNTHETIC-REC-0001",
        "profile_digest": PROFILE_DIGEST,
        "recommendation_digest": RECOMMENDATION_DIGEST,
        "profile_last_reviewed": REVIEWED_ON,
        "checks": all_checks(),
        "engine_version": "0.1.0",
        "decided_at": DECIDED_AT,
    }
    fields.update(overrides)
    return Determination.model_validate(fields)


def a_refusal(**overrides: object) -> Refusal:
    """Build a refusal carrying one reason."""
    fields: dict[str, object] = {
        "receipt_id": "SYNTHETIC-RECEIPT-0002",
        "client_id": "SYNTHETIC-0001",
        "recommendation_id": "SYNTHETIC-REC-0001",
        "profile_digest": PROFILE_DIGEST,
        "recommendation_digest": RECOMMENDATION_DIGEST,
        "profile_last_reviewed": REVIEWED_ON,
        "reasons": (
            RefusalReason(
                code=RefusalCode.MISSING_KYC_FACTOR,
                detail="No documented risk tolerance.",
                missing_factors=(FactorKey.RISK_TOLERANCE,),
            ),
        ),
        "engine_version": "0.1.0",
        "decided_at": DECIDED_AT,
    }
    fields.update(overrides)
    return Refusal.model_validate(fields)


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------


def test_money_rejects_float() -> None:
    """A float amount is refused outright rather than rounded."""
    with pytest.raises(ValidationError, match="float is not an exact quantity"):
        Money.model_validate({"amount": 10000.10, "currency": Currency.CAD})


def test_money_rejects_sub_cent_precision() -> None:
    """More than two decimal places is a data error, not something to round."""
    with pytest.raises(ValidationError):
        Money(amount=Decimal("100.005"), currency=Currency.CAD)


def test_money_rejects_negative() -> None:
    """Amounts are non-negative."""
    with pytest.raises(ValidationError):
        Money(amount=Decimal("-1.00"), currency=Currency.CAD)


def test_money_is_frozen() -> None:
    """Receipts and the values inside them are immutable.

    The direct form (`money.amount = ...`) is rejected statically by mypy, which is
    the stronger guarantee; this covers the runtime half of it.
    """
    money = cad("1.00")
    assert money.model_config["frozen"] is True
    field = "amount"
    with pytest.raises(ValidationError):
        setattr(money, field, Decimal("2.00"))


# ---------------------------------------------------------------------------
# ClientProfile
# ---------------------------------------------------------------------------


def test_profile_builds() -> None:
    """The synthetic profile is valid."""
    profile = a_profile()
    assert profile.risk_tolerance is RiskLevel.MEDIUM
    assert profile.time_horizon_years == Decimal(10)


def test_profile_allows_every_kyc_factor_to_be_absent() -> None:
    """Absence must be representable: it is the input to `missing_kyc_factor`."""
    profile = ClientProfile(
        client_id="SYNTHETIC-0002",
        last_reviewed=REVIEWED_ON,
        currency=Currency.CAD,
    )
    assert profile.risk_tolerance is None
    assert profile.time_horizon_years is None
    assert profile.investment_objectives == ()


def test_profile_forbids_unknown_fields() -> None:
    """An undeclared field is refused rather than silently ignored."""
    with pytest.raises(ValidationError):
        a_profile(inferred_risk_tolerance="high")


def test_profile_rejects_future_review_date() -> None:
    """A fact cannot have been documented tomorrow."""
    tomorrow = dt.datetime.now(tz=dt.UTC).date() + dt.timedelta(days=1)
    with pytest.raises(ValidationError, match="last_reviewed is in the future"):
        a_profile(last_reviewed=tomorrow)


def test_profile_accepts_a_review_dated_today() -> None:
    """Today is not the future: a profile reviewed this morning is documented, not stale.

    The boundary matters in both directions. Rejecting today would refuse the freshest
    profile there is, which is the opposite of what the check is for.
    """
    today = dt.datetime.now(tz=dt.UTC).date()
    assert a_profile(last_reviewed=today).last_reviewed == today


def test_profile_rejects_mixed_currency() -> None:
    """Every amount in a profile is in the profile's currency."""
    with pytest.raises(ValidationError, match="profile currency is CAD"):
        a_profile(net_worth=Money(amount=Decimal("1.00"), currency=Currency.USD))


def test_profile_rejects_liquid_exceeding_net_worth() -> None:
    """Liquid net worth cannot exceed net worth."""
    with pytest.raises(ValidationError, match="liquid_net_worth exceeds net_worth"):
        a_profile(net_worth=cad("100.00"), liquid_net_worth=cad("101.00"))


def test_profile_rejects_duplicate_objectives() -> None:
    """A duplicated objective is a data error."""
    with pytest.raises(ValidationError, match="duplicate investment objective"):
        a_profile(
            investment_objectives=(InvestmentObjective.GROWTH, InvestmentObjective.GROWTH),
        )


def test_profile_rejects_duplicate_holdings() -> None:
    """The same instrument cannot appear twice in a portfolio."""
    holding = Holding(
        instrument_id="SYNTHETIC-FUND-A",
        name="Synthetic Placeholder Fund A",
        issuer="Synthetic Placeholder Issuer",
        market_value=cad("1.00"),
    )
    with pytest.raises(ValidationError, match="duplicate holding instrument_id"):
        a_profile(holdings=(holding, holding))


# ---------------------------------------------------------------------------
# Recommendation
# ---------------------------------------------------------------------------


def test_recommendation_builds() -> None:
    """The synthetic recommendation is valid."""
    assert a_recommendation().product.risk_rating is RiskLevel.MEDIUM


def test_recommendation_rejects_zero_amount() -> None:
    """A recommendation concerns a positive amount."""
    with pytest.raises(ValidationError, match="amount must be positive"):
        a_recommendation(amount=cad("0.00"))


def test_recommendation_requires_parser_provenance() -> None:
    """Free text that was parsed must name what parsed it."""
    with pytest.raises(ValidationError, match="must be provided together"):
        a_recommendation(source_text="Put 10k of the client's cash into Fund B.")


def test_recommendation_accepts_parser_provenance() -> None:
    """Source text and parser identity travel together."""
    recommendation = a_recommendation(
        source_text="Put 10k of the client's cash into Fund B.",
        parsed_by="claude-opus-5",
    )
    assert recommendation.parsed_by == "claude-opus-5"


def test_rationale_citations_are_claims_not_findings() -> None:
    """Cited factors are recorded as-stated; nothing here checks them against a profile."""
    rationale = RecommendationRationale(
        text="Client is a sophisticated investor.",
        cited_factors=(a_citation(FactorKey.INVESTMENT_KNOWLEDGE, "sophisticated"),),
    )
    assert rationale.cited_factors[0].value == "sophisticated"


# ---------------------------------------------------------------------------
# Determination
# ---------------------------------------------------------------------------


def test_determination_builds_with_full_coverage() -> None:
    """A determination covering every rule is valid."""
    determination = a_determination()
    assert {check.code for check in determination.checks} == set(RefusalCode)
    assert determination.outcome == "supported"


@pytest.mark.parametrize("omitted", list(RefusalCode))
def test_determination_requires_every_rule(omitted: RefusalCode) -> None:
    """Omitting any single rule makes the determination unrepresentable."""
    partial = tuple(check for check in all_checks() if check.code is not omitted)
    with pytest.raises(ValidationError, match="missing checks for"):
        a_determination(checks=partial)


def test_determination_rejects_duplicate_checks() -> None:
    """A rule is recorded once."""
    duplicated = (*all_checks(), RuleCheck(code=RefusalCode.RISK_MISMATCH, basis=(a_citation(),)))
    with pytest.raises(ValidationError, match="duplicate rule check"):
        a_determination(checks=duplicated)


def test_rule_check_requires_a_basis() -> None:
    """No rule passes for an unstated reason."""
    with pytest.raises(ValidationError):
        RuleCheck(code=RefusalCode.RISK_MISMATCH, basis=())


def test_determination_rejects_naive_timestamp() -> None:
    """A receipt timestamp must be unambiguous."""
    with pytest.raises(ValidationError):
        a_determination(decided_at=dt.datetime(2025, 7, 1, 12, 0))  # noqa: DTZ001


def test_determination_rejects_non_utc_timestamp() -> None:
    """Receipt timestamps are UTC."""
    eastern = dt.timezone(dt.timedelta(hours=-5))
    with pytest.raises(ValidationError, match="decided_at must be UTC"):
        a_determination(decided_at=dt.datetime(2025, 7, 1, 12, 0, tzinfo=eastern))


def test_determination_rejects_malformed_digest() -> None:
    """The digest binding a receipt to its inputs must be a SHA-256 hex string."""
    with pytest.raises(ValidationError):
        a_determination(profile_digest="not-a-digest")


# ---------------------------------------------------------------------------
# Refusal
# ---------------------------------------------------------------------------


def test_refusal_builds() -> None:
    """The synthetic refusal is valid."""
    refusal = a_refusal()
    assert refusal.outcome == "refused"
    assert refusal.reasons[0].missing_factors == (FactorKey.RISK_TOLERANCE,)


def test_refusal_requires_at_least_one_reason() -> None:
    """A refusal that names nothing is not a refusal."""
    with pytest.raises(ValidationError):
        a_refusal(reasons=())


def test_refusal_rejects_duplicate_codes() -> None:
    """Each rule refuses at most once."""
    reason = RefusalReason(
        code=RefusalCode.MISSING_KYC_FACTOR,
        detail="No documented risk tolerance.",
        missing_factors=(FactorKey.RISK_TOLERANCE,),
    )
    with pytest.raises(ValidationError, match="duplicate refusal code"):
        a_refusal(reasons=(reason, reason))


def test_refusal_carries_every_reason_that_fired() -> None:
    """More than one reason may be reported; naming only the first would understate it."""
    refusal = a_refusal(
        reasons=(
            RefusalReason(
                code=RefusalCode.MISSING_KYC_FACTOR,
                detail="No documented time horizon.",
                missing_factors=(FactorKey.TIME_HORIZON_YEARS,),
            ),
            RefusalReason(
                code=RefusalCode.STALE_PROFILE,
                detail="Profile last reviewed beyond the review interval.",
                conflicting_factors=(a_citation(FactorKey.LAST_REVIEWED, "2025-06-01"),),
            ),
        ),
    )
    assert len(refusal.reasons) == 2


@pytest.mark.parametrize(
    ("code", "expected_field"),
    [
        (RefusalCode.MISSING_KYC_FACTOR, "missing_factors"),
        (RefusalCode.UNSUPPORTED_RATIONALE, "unsupported_claims"),
        (RefusalCode.RISK_MISMATCH, "conflicting_factors"),
        (RefusalCode.HORIZON_MISMATCH, "conflicting_factors"),
        (RefusalCode.LIQUIDITY_CONFLICT, "conflicting_factors"),
        (RefusalCode.CONCENTRATION_BREACH, "conflicting_factors"),
        (RefusalCode.STALE_PROFILE, "conflicting_factors"),
    ],
)
def test_refusal_reason_requires_its_own_evidence(code: RefusalCode, expected_field: str) -> None:
    """Each code populates exactly its own evidence field, and no other."""
    evidence: dict[str, object] = {
        "missing_factors": (FactorKey.RISK_TOLERANCE,),
        "conflicting_factors": (a_citation(),),
        "unsupported_claims": (a_citation(),),
    }
    reason = RefusalReason.model_validate(
        {"code": code, "detail": "Synthetic detail.", expected_field: evidence[expected_field]},
    )
    assert reason.code is code

    with pytest.raises(ValidationError, match="requires non-empty"):
        RefusalReason.model_validate({"code": code, "detail": "Synthetic detail."})

    for wrong_field, wrong_value in evidence.items():
        if wrong_field == expected_field:
            continue
        with pytest.raises(ValidationError, match="must not carry"):
            RefusalReason.model_validate(
                {
                    "code": code,
                    "detail": "Synthetic detail.",
                    expected_field: evidence[expected_field],
                    wrong_field: wrong_value,
                },
            )


# ---------------------------------------------------------------------------
# The outcome union
# ---------------------------------------------------------------------------


def test_outcome_union_discriminates_on_outcome() -> None:
    """There are exactly two outcomes, told apart by their tag."""
    adapter: TypeAdapter[Determination | Refusal] = TypeAdapter(SuitabilityOutcome)
    supported = adapter.validate_json(a_determination().model_dump_json())
    refused = adapter.validate_json(a_refusal().model_dump_json())
    assert isinstance(supported, Determination)
    assert isinstance(refused, Refusal)


def test_receipt_round_trips_through_json() -> None:
    """A receipt survives serialisation unchanged, digests and citations included."""
    determination = a_determination()
    assert Determination.model_validate_json(determination.model_dump_json()) == determination


# ---------------------------------------------------------------------------
# Property-based
# ---------------------------------------------------------------------------


def test_risk_level_rank_is_a_total_order() -> None:
    """Every risk level is ranked, and no two share a rank."""
    assert set(RISK_LEVEL_RANK) == set(RiskLevel)
    assert len(set(RISK_LEVEL_RANK.values())) == len(RiskLevel)


@given(
    amount=st.decimals(
        min_value=Decimal("0.00"),
        max_value=Decimal("9999999999999999.99"),
        places=2,
        allow_nan=False,
        allow_infinity=False,
    ),
    currency=st.sampled_from(Currency),
)
def test_money_round_trips_exactly(amount: Decimal, currency: Currency) -> None:
    """No monetary amount loses precision through JSON."""
    money = Money(amount=amount, currency=currency)
    assert Money.model_validate_json(money.model_dump_json()) == money


def a_reason_with_detail(detail: str) -> RefusalReason:
    """Build a refusal reason carrying an arbitrary detail string."""
    return RefusalReason(
        code=RefusalCode.RISK_MISMATCH,
        detail=detail,
        conflicting_factors=(a_citation(),),
    )


@pytest.mark.parametrize("blank", ["", " ", "\t\n", "   \r\n  "])
def test_refusal_detail_rejects_blank(blank: str) -> None:
    """A refusal must actually say something; blank detail is refused."""
    with pytest.raises(ValidationError):
        a_reason_with_detail(blank)


@given(text=st.text())
def test_refusal_detail_validation_is_idempotent(text: str) -> None:
    r"""Any detail that validates is non-empty and validates again unchanged.

    The oracle here is pydantic itself rather than `str.strip()`: Python treats the
    C0 separators (\x1c-\x1f) as whitespace and Unicode does not, so using `str.strip()`
    to predict the stored value would test the wrong thing.
    """
    try:
        reason = a_reason_with_detail(text)
    except ValidationError:
        return
    assert reason.detail
    assert a_reason_with_detail(reason.detail).detail == reason.detail
