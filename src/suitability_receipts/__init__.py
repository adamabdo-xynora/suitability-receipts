"""Deterministic investment suitability determinations, or an explicit refusal.

The design principle this package exists to enforce: **the language model parses and
explains; it never determines suitability.** Determination is made by deterministic
code against the documented client profile.

Credentials
-----------
`ANTHROPIC_API_KEY` is read in exactly one place, and that place does not exist yet.
When the LLM layer is added it will live at `suitability_receipts.llm.client`, which
will be the only module permitted to read the environment for credentials; every other
module will receive an already-constructed client by injection. Nothing in this package
reads it today, and the determination rules never will — they do not call a model.
"""

from suitability_receipts.models import (
    REQUIRED_KYC_FACTORS,
    RISK_LEVEL_RANK,
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
    SuitabilityOutcome,
    TradeAction,
    UpcomingExpense,
)

__all__ = [
    "REQUIRED_KYC_FACTORS",
    "RISK_LEVEL_RANK",
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
