"""Drafting the prose for a determination the engine has already made.

The model is given the documented factors, the recommendation, and the outcome, and
asked for two things: a paragraph, and a declaration of which factors it relied on. It
is not asked whether the recommendation is suitable — that was settled before this
module ran, by code, and nothing here can change it.

The declaration is the whole point. Prose alone is unverifiable; prose plus a list of
factual claims in a fixed vocabulary can be checked against the profile by
`verification.verify_rationale`, which is where the checking happens and which needs no
model to do it.
"""

from collections.abc import Mapping
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, ValidationError

from suitability_receipts.engine import documented_citation
from suitability_receipts.llm.client import ModelCallError, ModelClient, ToolSpec
from suitability_receipts.llm.verification import (
    DraftedRationale,
    RationaleVerdict,
    outcome_evidence,
    recommendation_evidence,
    verify_rationale,
)
from suitability_receipts.models import (
    ClientProfile,
    FactorCitation,
    FactorKey,
    NonEmptyStr,
    Recommendation,
    SuitabilityOutcome,
)

__all__ = [
    "DRAFTER_VERSION",
    "RATIONALE_SYSTEM_PROMPT",
    "RATIONALE_TOOL",
    "RATIONALE_TOOL_NAME",
    "draft_rationale",
    "explain",
]

DRAFTER_VERSION = "0.1.0"
"""Recorded in `DraftedRationale.drafted_by`. Bump it when the prompt or the tool schema
changes, so that two rationales that read differently can be told apart by what produced
them."""

RATIONALE_TOOL_NAME = "record_rationale"

_FACTOR_KEYS = [key.value for key in FactorKey]

RATIONALE_SYSTEM_PROMPT = """You explain suitability determinations. You do not make them.

The determination in the material below has already been made by deterministic code
against the documented client profile. Your paragraph explains that determination. It
cannot change it, qualify it, or hedge it.

Rules, in order of importance:

1. Never state or imply a conclusion other than the one in the material. If the outcome
   is a refusal, your paragraph says the recommendation was refused and why. Do not
   soften it, do not describe it as a concern or a caution, and do not suggest what would
   make it pass.
2. Never assert a client fact that is not in the documented factors below. "Not
   documented" means the fact does not exist for this purpose. It does not mean unknown,
   and it does not mean probably fine.
3. Declare every documented factor you relied on in `cited_factors`, copying its value
   exactly as it appears in the documented factors. Every declaration is checked against
   the profile by code, and one wrong value refuses the entire rationale.
4. If your explanation rests on a factor being absent — a refusal for a missing KYC
   factor does — declare that factor in `absent_factors` instead. That is checked too.
5. Use a number only if it appears in a factor you declared, in the recommendation, or in
   the determination. Prefer words for counts ("all seven checks"). Do not quote the
   receipt identifier or a digest.
6. Write for the client's advisor: plain, specific, and short. Three or four sentences.
"""

_RATIONALE_INPUT_SCHEMA: Mapping[str, object] = MappingProxyType(
    {
        "type": "object",
        "additionalProperties": False,
        "required": ["prose", "cited_factors", "absent_factors"],
        "properties": {
            "prose": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "The explanation itself. Three or four sentences, explaining the "
                    "determination that was already made."
                ),
            },
            "cited_factors": {
                "type": "array",
                "description": (
                    "Every documented factor the explanation relies on, with its value "
                    "copied exactly from the documented factors."
                ),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["key", "value"],
                    "properties": {
                        "key": {"type": "string", "enum": _FACTOR_KEYS},
                        "value": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Verbatim, as it appears in the documented factors.",
                        },
                    },
                },
            },
            "absent_factors": {
                "type": "array",
                "description": (
                    "Every factor the explanation relies on the profile NOT documenting."
                ),
                "items": {"type": "string", "enum": _FACTOR_KEYS},
            },
        },
    },
)

RATIONALE_TOOL = ToolSpec(
    name=RATIONALE_TOOL_NAME,
    description=(
        "Record an explanation of a determination that has already been made, together "
        "with the documented client factors it relies on. Every declared factor is "
        "checked against the profile by code; a declaration that does not match refuses "
        "the whole explanation."
    ),
    input_schema=_RATIONALE_INPUT_SCHEMA,
)


class _CitedFactorPayload(BaseModel):
    """One declared citation as the tool returned it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: FactorKey
    value: NonEmptyStr


class _RationalePayload(BaseModel):
    """The tool's arguments, validated before anything downstream sees them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prose: NonEmptyStr
    cited_factors: tuple[_CitedFactorPayload, ...]
    absent_factors: tuple[FactorKey, ...]


def factor_sheet(profile: ClientProfile) -> str:
    """Render every citable factor, documented value or explicit absence.

    Rendered through `engine.documented_citation`, so the values the model is asked to
    copy are byte-for-byte the values its declarations will be checked against.
    """
    lines: list[str] = []
    for key in FactorKey:
        citation = documented_citation(profile, key)
        if citation is None:
            lines.append(f"- {key.value}: NOT DOCUMENTED")
        else:
            lines.append(f"- {key.value}: {citation.value}")
    return "\n".join(lines)


def _user_text(
    profile: ClientProfile,
    outcome: SuitabilityOutcome,
    recommendation: Recommendation,
) -> str:
    """Assemble the material the model may draw on, and nothing else."""
    return "\n\n".join(
        (
            (
                "DOCUMENTED CLIENT FACTORS — the only client facts that exist for "
                "this determination:"
            ),
            factor_sheet(profile),
            "THE RECOMMENDATION:",
            recommendation_evidence(recommendation),
            "THE DETERMINATION, ALREADY MADE:",
            outcome_evidence(outcome),
        ),
    )


def draft_rationale(
    client: ModelClient,
    *,
    profile: ClientProfile,
    outcome: SuitabilityOutcome,
    recommendation: Recommendation,
) -> DraftedRationale:
    """Ask the model for prose and the declarations to check it against.

    Nothing is verified here. The draft is a claim; `verify_rationale` is the check.

    Args:
        client: An already-constructed client. This module reads no credential.
        profile: The documented client facts, rendered for the model as a factor sheet.
        outcome: The determination the prose must explain, unchanged.
        recommendation: The recommendation that determination was made on.

    Returns:
        The prose and its declarations, with `documented_on` stamped from the profile.

    Raises:
        ModelCallError: If the tool's arguments do not match its schema.
    """
    arguments = client.call_tool(
        system=RATIONALE_SYSTEM_PROMPT,
        user_text=_user_text(profile, outcome, recommendation),
        tool=RATIONALE_TOOL,
    )
    try:
        payload = _RationalePayload.model_validate(arguments)
    except ValidationError as error:
        msg = f"{RATIONALE_TOOL_NAME} returned arguments that do not match its schema: {error}"
        raise ModelCallError(msg) from error

    citations = tuple(
        FactorCitation(
            key=cited.key,
            value=cited.value,
            documented_on=profile.last_reviewed,
        )
        for cited in payload.cited_factors
    )
    return DraftedRationale(
        prose=payload.prose,
        cited_factors=citations,
        absent_factors=payload.absent_factors,
        drafted_by=f"{RATIONALE_TOOL_NAME}@{DRAFTER_VERSION} model={client.model_id}",
    )


def explain(
    client: ModelClient,
    *,
    profile: ClientProfile,
    outcome: SuitabilityOutcome,
    recommendation: Recommendation,
) -> RationaleVerdict:
    """Draft a rationale and verify it, in that order and with no step between.

    Args:
        client: An already-constructed client.
        profile: The documented client facts.
        outcome: The determination to explain.
        recommendation: The recommendation that determination was made on.

    Returns:
        A `VerifiedRationale`, or an `UnsupportedRationale` naming what failed.

    Raises:
        ModelCallError: If the call itself returns nothing usable.
    """
    drafted = draft_rationale(
        client,
        profile=profile,
        outcome=outcome,
        recommendation=recommendation,
    )
    return verify_rationale(
        profile,
        drafted,
        outcome=outcome,
        recommendation=recommendation,
    )
