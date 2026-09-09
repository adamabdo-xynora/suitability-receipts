"""Tests for drafting a rationale, and for the shape of what the model is asked for.

Drafting runs against a stub client returning canned tool arguments, so nothing here
needs a key or a network. What is worth testing about the drafting step is narrow: that
the material handed to the model is the documented material and nothing else, that the
documentation date on every citation is stamped by code, and that the tool the model
must call has no field through which a judgement could arrive.
"""

from collections.abc import Mapping

import pytest

from suitability_receipts import FactorKey
from suitability_receipts.llm import (
    DRAFTER_VERSION,
    DraftedRationale,
    ModelCallError,
    ToolSpec,
    UnsupportedRationale,
    VerifiedRationale,
    draft_rationale,
    explain,
)
from suitability_receipts.llm.parsing import PARSE_TOOL
from suitability_receipts.llm.rationale import (
    RATIONALE_SYSTEM_PROMPT,
    RATIONALE_TOOL,
    RATIONALE_TOOL_NAME,
    factor_sheet,
)
from synthetic import (
    REVIEWED_ON,
    STUB_MODEL_ID,
    StubModelClient,
    a_profile,
    a_recommendation,
    an_outcome,
)

PROFILE = a_profile()
RECOMMENDATION = a_recommendation()
OUTCOME = an_outcome()

GOOD_ARGUMENTS: dict[str, object] = {
    "prose": (
        "The recommendation is supported. The documented risk tolerance is medium and "
        "the fund is rated medium risk."
    ),
    "cited_factors": [{"key": "risk_tolerance", "value": "medium"}],
    "absent_factors": [],
}


def draft(arguments: Mapping[str, object]) -> DraftedRationale:
    """Draft a rationale as though a model had returned `arguments`."""
    return draft_rationale(
        StubModelClient(arguments),
        profile=PROFILE,
        outcome=OUTCOME,
        recommendation=RECOMMENDATION,
    )


# ---------------------------------------------------------------------------
# What the model is asked
# ---------------------------------------------------------------------------


def test_the_prompt_carries_the_documented_factors_and_the_outcome() -> None:
    """The model draws on the documented material, assembled by code."""
    client = StubModelClient(GOOD_ARGUMENTS)
    draft_rationale(
        client,
        profile=PROFILE,
        outcome=OUTCOME,
        recommendation=RECOMMENDATION,
    )
    system, user_text, tool = client.calls[0]
    assert system == RATIONALE_SYSTEM_PROMPT
    assert tool is RATIONALE_TOOL
    assert factor_sheet(PROFILE) in user_text
    assert "outcome: supported" in user_text


def test_the_factor_sheet_names_absences_explicitly() -> None:
    """An explicit absence is a value the model must be able to see and rely on."""
    sheet = factor_sheet(a_profile(investment_knowledge=None))
    assert "- investment_knowledge: NOT DOCUMENTED" in sheet
    assert "- risk_tolerance: medium" in sheet


def test_the_system_prompt_forbids_deciding_and_softening() -> None:
    """The boundary is stated to the model as well as enforced around it."""
    assert "You do not make them." in RATIONALE_SYSTEM_PROMPT
    assert "soften it" in RATIONALE_SYSTEM_PROMPT


def test_the_rationale_tool_has_no_field_for_a_judgement() -> None:
    """There is no channel through which a suitability opinion could arrive."""
    properties = RATIONALE_TOOL.input_schema["properties"]
    assert isinstance(properties, dict)
    assert set(properties) == {"prose", "cited_factors", "absent_factors"}


@pytest.mark.parametrize("tool", [RATIONALE_TOOL, PARSE_TOOL], ids=["rationale", "parse"])
def test_both_tool_schemas_are_closed_and_fully_required(tool: ToolSpec) -> None:
    """A strict schema is only strict if it forbids extras and requires every field."""
    schema = tool.input_schema
    required = schema["required"]
    properties = schema["properties"]
    assert schema["additionalProperties"] is False
    assert isinstance(required, list)
    assert isinstance(properties, dict)
    assert set(required) == set(properties)


# ---------------------------------------------------------------------------
# What comes back
# ---------------------------------------------------------------------------


def test_the_documentation_date_is_stamped_by_code() -> None:
    """A date is a property of the profile in play, not something the model can know."""
    drafted = draft(GOOD_ARGUMENTS)
    assert drafted.cited_factors[0].documented_on == REVIEWED_ON


def test_the_draft_records_what_produced_it() -> None:
    """A rationale names the model and the tool version behind it."""
    drafted = draft(GOOD_ARGUMENTS)
    expected = f"{RATIONALE_TOOL_NAME}@{DRAFTER_VERSION} model={STUB_MODEL_ID}"
    assert drafted.drafted_by == expected


def test_declared_absences_survive_the_draft() -> None:
    """The absence declaration is the verifiable half of a missing-factor explanation."""
    profile = a_profile(risk_tolerance=None)
    client = StubModelClient(
        {
            "prose": "The profile documents no risk tolerance, so it was refused.",
            "cited_factors": [{"key": "time_horizon_years", "value": "10"}],
            "absent_factors": ["risk_tolerance"],
        },
    )
    drafted = draft_rationale(
        client,
        profile=profile,
        outcome=an_outcome(profile),
        recommendation=RECOMMENDATION,
    )
    assert drafted.absent_factors == (FactorKey.RISK_TOLERANCE,)


def test_arguments_off_the_schema_are_a_call_failure() -> None:
    """A broken call is never mistaken for an unsupported rationale."""
    with pytest.raises(ModelCallError, match="do not match its schema"):
        draft({"prose": "Fine by me."})


def test_an_invented_field_is_rejected() -> None:
    """The payload is closed, so a judgement cannot be smuggled in beside the prose."""
    with pytest.raises(ModelCallError):
        draft({**GOOD_ARGUMENTS, "outcome": "supported"})


# ---------------------------------------------------------------------------
# Draft and verify, in that order and with nothing between
# ---------------------------------------------------------------------------


def test_explain_verifies_what_it_drafted() -> None:
    """The happy path: a correct rationale comes back verified, unedited."""
    verdict = explain(
        StubModelClient(GOOD_ARGUMENTS),
        profile=PROFILE,
        outcome=OUTCOME,
        recommendation=RECOMMENDATION,
    )
    assert isinstance(verdict, VerifiedRationale)
    assert verdict.rationale.text == GOOD_ARGUMENTS["prose"]


def test_explain_refuses_a_rationale_it_cannot_check() -> None:
    """A wrong declaration refuses the whole rationale; nothing is repaired."""
    arguments = {
        "prose": "The client's documented risk tolerance is high.",
        "cited_factors": [{"key": "risk_tolerance", "value": "high"}],
        "absent_factors": [],
    }
    verdict = explain(
        StubModelClient(arguments),
        profile=PROFILE,
        outcome=OUTCOME,
        recommendation=RECOMMENDATION,
    )
    assert isinstance(verdict, UnsupportedRationale)
    assert "but the profile documents 'medium'" in verdict.detail
