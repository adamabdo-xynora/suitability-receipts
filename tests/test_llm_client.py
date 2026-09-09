"""Tests for the one module that reads a credential, and for how it calls a model.

Nothing here reaches the network. The SDK client is constructed with an obviously
synthetic key — construction performs no request — and its `messages.create` is replaced
with a function that records what it was sent and returns a canned message.
"""

import pytest
from anthropic import Anthropic
from anthropic.types import Message, StopReason, ToolUseBlock, Usage

from suitability_receipts.llm.client import (
    API_KEY_VARIABLE,
    DEFAULT_MODEL,
    AnthropicModelClient,
    MissingApiKeyError,
    ModelCallError,
    ModelClient,
    ToolSpec,
    client_from_environment,
)

SYNTHETIC_KEY = "SYNTHETIC-not-a-real-credential"

A_TOOL = ToolSpec(
    name="record_something",
    description="Record something.",
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["value"],
        "properties": {"value": {"type": "string"}},
    },
)


def a_message(*blocks: ToolUseBlock, stop_reason: StopReason = "tool_use") -> Message:
    """Build a response without touching the network."""
    return Message(
        id="msg_synthetic",
        content=list(blocks),
        model="SYNTHETIC-stub-model",
        role="assistant",
        stop_reason=stop_reason,
        stop_sequence=None,
        type="message",
        usage=Usage(input_tokens=0, output_tokens=0),
    )


def a_tool_use(name: str = "record_something", **arguments: object) -> ToolUseBlock:
    """Build a tool-use block the way the API would return one."""
    return ToolUseBlock(id="toolu_synthetic", input=arguments, name=name, type="tool_use")


class _Recorder:
    """Stands in for `client.messages.create`, capturing what it was called with."""

    def __init__(self, message: Message) -> None:
        self.message = message
        self.kwargs: dict[str, object] = {}

    def __call__(self, **kwargs: object) -> Message:
        self.kwargs = kwargs
        return self.message


def a_client(
    monkeypatch: pytest.MonkeyPatch,
    message: Message,
) -> tuple[AnthropicModelClient, _Recorder]:
    """Build a wrapped SDK client whose `messages.create` returns `message`."""
    sdk = Anthropic(api_key=SYNTHETIC_KEY)
    recorder = _Recorder(message)
    monkeypatch.setattr(sdk.messages, "create", recorder)
    return AnthropicModelClient(sdk), recorder


# ---------------------------------------------------------------------------
# Reading the credential
# ---------------------------------------------------------------------------


def test_a_missing_key_is_an_error_not_a_silent_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unset variable must not resolve to some other credential source."""
    monkeypatch.delenv(API_KEY_VARIABLE, raising=False)
    with pytest.raises(MissingApiKeyError, match=API_KEY_VARIABLE):
        client_from_environment()


def test_a_blank_key_is_treated_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whitespace is not a credential."""
    monkeypatch.setenv(API_KEY_VARIABLE, "   ")
    with pytest.raises(MissingApiKeyError):
        client_from_environment()


def test_a_present_key_builds_a_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """The happy path builds a client and records which model it will call."""
    monkeypatch.setenv(API_KEY_VARIABLE, SYNTHETIC_KEY)
    client = client_from_environment()
    assert client.model_id == DEFAULT_MODEL


def test_the_model_is_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Callers pin a model; nothing downstream hard-codes one."""
    monkeypatch.setenv(API_KEY_VARIABLE, SYNTHETIC_KEY)
    client = client_from_environment(model="SYNTHETIC-other-model")
    assert client.model_id == "SYNTHETIC-other-model"


# ---------------------------------------------------------------------------
# The call itself
# ---------------------------------------------------------------------------


def test_the_call_forces_the_tool_and_marks_it_strict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Structure is required of the model, not hoped for and then parsed out of prose."""
    client, recorder = a_client(monkeypatch, a_message(a_tool_use(value="x")))
    client.call_tool(system="rules", user_text="material", tool=A_TOOL)

    assert recorder.kwargs["tool_choice"] == {
        "type": "tool",
        "name": A_TOOL.name,
        "disable_parallel_tool_use": True,
    }
    tools = recorder.kwargs["tools"]
    assert isinstance(tools, list)
    assert tools[0]["strict"] is True
    assert tools[0]["name"] == A_TOOL.name
    assert tools[0]["input_schema"] == dict(A_TOOL.input_schema)
    assert recorder.kwargs["system"] == "rules"
    assert recorder.kwargs["messages"] == [{"role": "user", "content": "material"}]


def test_the_call_returns_the_tool_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    """The model's whole output is the arguments of one tool call."""
    client, _ = a_client(monkeypatch, a_message(a_tool_use(value="recorded")))
    arguments = client.call_tool(system="rules", user_text="material", tool=A_TOOL)
    assert arguments == {"value": "recorded"}


def test_a_response_without_the_tool_call_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No tool call means nothing structured to validate; that is not a determination."""
    client, _ = a_client(monkeypatch, a_message(stop_reason="end_turn"))
    with pytest.raises(ModelCallError, match="no call to record_something"):
        client.call_tool(system="rules", user_text="material", tool=A_TOOL)


def test_a_call_to_a_different_tool_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Arguments are only read from the tool that was asked for."""
    client, _ = a_client(monkeypatch, a_message(a_tool_use(name="something_else")))
    with pytest.raises(ModelCallError, match="no call to record_something"):
        client.call_tool(system="rules", user_text="material", tool=A_TOOL)


def test_a_refusal_is_reported_as_a_broken_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """A model that declines is a failed call, never an empty or hedged result."""
    client, _ = a_client(monkeypatch, a_message(stop_reason="refusal"))
    with pytest.raises(ModelCallError, match="declined"):
        client.call_tool(system="rules", user_text="material", tool=A_TOOL)


def test_the_wrapper_satisfies_the_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SDK-backed client is usable everywhere a `ModelClient` is asked for."""
    client, _ = a_client(monkeypatch, a_message(a_tool_use(value="x")))
    injected: ModelClient = client
    assert injected.call_tool(system="rules", user_text="material", tool=A_TOOL) == {
        "value": "x",
    }
