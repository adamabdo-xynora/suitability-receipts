"""The model layer: parsing free text, and explaining a determination already made.

The boundary this package exists to hold, restated where it is easiest to break:

* The model **parses**. Free text in, a validated `Recommendation` out. Every field it
  returns is either transcribed from the text with a quotation that code checks, or
  taken from a supplied product record, or null. See `parsing`.
* The model **explains**. It writes prose for a `Determination` or `Refusal` the engine
  has already produced, and declares the documented factors it relied on. Code checks
  every declaration against the profile and refuses the rationale whole if any fails.
  See `rationale` for the drafting and `verification` for the checking.

It never decides suitability, never assigns a risk rating absent from its source, never
infers an undocumented client fact, and never softens a refusal.

`client` is the only module in this repository that reads a credential from the
environment; everything else here takes an already-constructed `ModelClient` as its
first argument. `verification` imports neither, so a rationale can be checked with no
client at all.
"""

from suitability_receipts.llm.client import (
    API_KEY_VARIABLE,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    AnthropicModelClient,
    MissingApiKeyError,
    ModelCallError,
    ModelClient,
    ToolSpec,
    client_from_environment,
)
from suitability_receipts.llm.parsing import (
    PARSE_TOOL,
    PARSER_VERSION,
    RISK_RATING_SPELLINGS,
    ParseRequest,
    UnparseableRecommendationError,
    parse_recommendation,
)
from suitability_receipts.llm.rationale import (
    DRAFTER_VERSION,
    RATIONALE_TOOL,
    draft_rationale,
    explain,
)
from suitability_receipts.llm.verification import (
    DraftedRationale,
    RationaleVerdict,
    UnsupportedRationale,
    VerifiedRationale,
    verify_rationale,
)

__all__ = [
    "API_KEY_VARIABLE",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_MODEL",
    "DRAFTER_VERSION",
    "PARSER_VERSION",
    "PARSE_TOOL",
    "RATIONALE_TOOL",
    "RISK_RATING_SPELLINGS",
    "AnthropicModelClient",
    "DraftedRationale",
    "MissingApiKeyError",
    "ModelCallError",
    "ModelClient",
    "ParseRequest",
    "RationaleVerdict",
    "ToolSpec",
    "UnparseableRecommendationError",
    "UnsupportedRationale",
    "VerifiedRationale",
    "client_from_environment",
    "draft_rationale",
    "explain",
    "parse_recommendation",
    "verify_rationale",
]
