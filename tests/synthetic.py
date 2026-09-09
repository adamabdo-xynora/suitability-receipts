"""Synthetic fixtures shared by the LLM-layer tests.

All client data here is synthetic and obviously so: identifiers carry a `SYNTHETIC-`
prefix and names are placeholders. No real client data and no credentials appear in this
repository, including in fixtures — the stub client below needs neither a key nor a
network, which is what lets every test in this layer run offline.
"""

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from suitability_receipts import (
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
    RiskLevel,
    SuitabilityOutcome,
    TradeAction,
    UpcomingExpense,
    determine,
)
from suitability_receipts.llm import ToolSpec

REVIEWED_ON = dt.date(2025, 6, 1)
NOW = dt.datetime(2025, 7, 1, 12, 0, tzinfo=dt.UTC)
STUB_MODEL_ID = "SYNTHETIC-stub-model"


@dataclass
class StubModelClient:
    """A `ModelClient` that answers with canned tool arguments.

    It satisfies the protocol structurally, so every test that exercises parsing or
    drafting runs with no key, no SDK client, and no network.
    """

    arguments: Mapping[str, object]
    model_id: str = STUB_MODEL_ID
    calls: list[tuple[str, str, ToolSpec]] = field(default_factory=list)

    def call_tool(
        self,
        *,
        system: str,
        user_text: str,
        tool: ToolSpec,
    ) -> Mapping[str, object]:
        """Record the call and return the canned arguments."""
        self.calls.append((system, user_text, tool))
        return self.arguments


def cad(amount: str) -> Money:
    """Build a synthetic CAD amount."""
    return Money(amount=Decimal(amount), currency=Currency.CAD)


def diversified_holdings() -> tuple[Holding, ...]:
    """Six equal positions, each in its own instrument, issuer, and sector."""
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
            cited_factors=(
                FactorCitation(
                    key=FactorKey.RISK_TOLERANCE,
                    value="medium",
                    documented_on=REVIEWED_ON,
                ),
            ),
        ),
    }
    fields.update(overrides)
    return Recommendation.model_validate(fields)


def an_outcome(
    profile: ClientProfile | None = None,
    recommendation: Recommendation | None = None,
) -> SuitabilityOutcome:
    """Run the engine, so that tests explain a determination the engine really made."""
    return determine(
        profile if profile is not None else a_profile(),
        recommendation if recommendation is not None else a_recommendation(),
        now=NOW,
    )


SOURCE_TEXT = (
    "Recommend a purchase of 10000 CAD of Synthetic Placeholder Fund Z "
    "(SYNTHETIC-FUND-Z), issued by Synthetic Placeholder Issuer Z. The fund facts sheet "
    "rates the fund medium risk. Redeemable daily with no lock-up. The client has told "
    "us their risk tolerance is medium and their time horizon is 10 years."
)
"""A synthetic free-text recommendation. Deliberately states a risk rating in words the
`RISK_RATING_SPELLINGS` table recognises, so that the parse can succeed at all."""


def parse_arguments(**overrides: object) -> dict[str, object]:
    """Build the tool arguments a well-behaved parse would return for `SOURCE_TEXT`."""
    arguments: dict[str, object] = {
        "instrument_id": "SYNTHETIC-FUND-Z",
        "action": "buy",
        "amount": "10000.00",
        "currency": "CAD",
        "rationale_text": "Medium-risk fund matching a medium risk tolerance.",
        "claimed_factors": [
            {"key": "risk_tolerance", "value": "medium"},
            {"key": "time_horizon_years", "value": "10"},
        ],
        "product": {
            "name": "Synthetic Placeholder Fund Z",
            "issuer": "Synthetic Placeholder Issuer Z",
            "sector": "synthetic-sector-z",
            "risk_rating": "medium",
            "risk_rating_quote": "rates the fund medium risk",
            "lock_up_days": 0,
            "redemption_frequency": "daily",
            "recommended_holding_period_years": None,
        },
    }
    arguments.update(overrides)
    return arguments


def product_arguments(**overrides: object) -> dict[str, object]:
    """Build the `product` sub-object of `parse_arguments`, with overrides applied."""
    product = parse_arguments()["product"]
    assert isinstance(product, dict)
    product.update(overrides)
    return product
