"""Deterministic investment suitability determinations, or an explicit refusal.

The design principle this package exists to enforce: **the language model parses and
explains; it never determines suitability.** Determination is made by deterministic
code against the documented client profile.

Credentials
-----------
`ANTHROPIC_API_KEY` is read in exactly one place: `suitability_receipts.llm.client`.
That is the only module permitted to read the environment for credentials, and every
other module receives an already-constructed client by injection. It is enforced rather
than promised — `tests/test_llm_boundary.py` parses every module under `src/` and fails
if any other one imports `os` or `anthropic`. The determination rules never touch it;
they do not call a model.

The model layer lives in `suitability_receipts.llm` and is imported explicitly. It is
deliberately not re-exported here, so that importing this package imports no SDK.
"""

from suitability_receipts.engine import (
    CONCENTRATION_LIMIT_FRACTION,
    ENGINE_VERSION,
    PROFILE_REVIEW_INTERVAL_DAYS,
    RuleConfig,
    RuleInput,
    determine,
)
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
    "CONCENTRATION_LIMIT_FRACTION",
    "ENGINE_VERSION",
    "PROFILE_REVIEW_INTERVAL_DAYS",
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
    "RuleConfig",
    "RuleInput",
    "SuitabilityOutcome",
    "TradeAction",
    "UpcomingExpense",
    "determine",
]
