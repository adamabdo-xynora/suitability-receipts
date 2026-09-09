"""The one module in this repository permitted to read a credential from the environment.

`ANTHROPIC_API_KEY` is read here and nowhere else. Every other module — including the
two that actually use a model, `parsing` and `rationale` — receives an already
constructed client through its first argument. That is not a convention documented in a
docstring and then quietly broken: `tests/test_llm_boundary.py` parses every module
under `src/` and fails if any file other than this one imports `os` or `anthropic`.

What a client is allowed to do
------------------------------
`ModelClient` has exactly one method, and it makes exactly one kind of call: a single
forced tool use against a strict schema. There is no method that returns free prose for
somebody else to parse, because the moment such a method exists it becomes the easy way
to do things and structure becomes optional. The model's whole output is the arguments
of one tool call, validated against a schema before anything downstream sees it.

`model_id` is on the protocol so that what the model produces can record which model
produced it: `Recommendation.parsed_by` names a real model rather than the string
"an LLM".

Nothing here decides anything. This module knows nothing about suitability, profiles, or
refusals; it moves a prompt and a schema out and a dict of arguments back.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

import anthropic
from anthropic.types import Message

__all__ = [
    "API_KEY_VARIABLE",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_MODEL",
    "AnthropicModelClient",
    "MissingApiKeyError",
    "ModelCallError",
    "ModelClient",
    "ToolSpec",
    "client_from_environment",
]

API_KEY_VARIABLE = "ANTHROPIC_API_KEY"
"""The only environment variable this repository reads, in the only module that reads it."""

DEFAULT_MODEL = "claude-opus-5"
"""The model used when a caller does not name one."""

DEFAULT_MAX_TOKENS = 4096
"""Output ceiling per call. Both tools in this package return a small structured record;
a rationale is a paragraph, not a document."""


class MissingApiKeyError(RuntimeError):
    """Raised when a client is asked for and no credential is available."""


class ModelCallError(RuntimeError):
    """Raised when a call returns nothing usable.

    This is a broken call, not a determination: the model refused, or returned no call
    to the tool it was required to call, or returned arguments that do not match the
    tool's schema. It is deliberately distinct from the domain outcomes in `parsing` and
    `verification`, so that "the API misbehaved" is never mistaken for "the model said
    something unsupported".
    """


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A tool the model will be forced to call, and the schema its arguments must match."""

    name: str
    description: str
    input_schema: Mapping[str, object]


class ModelClient(Protocol):
    """The seam every module that uses a model depends on.

    A protocol rather than a class, so that a test can supply canned arguments without
    a key, a network, or a subclass of anything in the SDK.
    """

    @property
    def model_id(self) -> str:
        """Identifier of the model behind this client, recorded on what it produces."""

    def call_tool(
        self,
        *,
        system: str,
        user_text: str,
        tool: ToolSpec,
    ) -> Mapping[str, object]:
        """Call `tool`, forced, and return its arguments.

        Args:
            system: The rules the model works under.
            user_text: The material the model works on.
            tool: The tool it must call, and the schema its arguments must satisfy.

        Returns:
            The tool call's arguments, unvalidated beyond the schema the API enforced.

        Raises:
            ModelCallError: If the call returns no usable arguments for `tool`.
        """


def _tool_arguments(message: Message, tool_name: str) -> Mapping[str, object]:
    """Extract the forced tool call's arguments, or explain why there are none."""
    if message.stop_reason == "refusal":
        msg = f"the model declined the request (stop_reason={message.stop_reason})"
        raise ModelCallError(msg)
    for block in message.content:
        if block.type == "tool_use" and block.name == tool_name:
            return block.input
    msg = (
        f"the model returned no call to {tool_name} "
        f"(stop_reason={message.stop_reason}); nothing structured to validate"
    )
    raise ModelCallError(msg)


class AnthropicModelClient:
    """A `ModelClient` backed by the Anthropic Messages API.

    Every call sets `tool_choice` to the one tool and marks that tool `strict`, so the
    model returns arguments the API has already validated against the schema rather than
    prose for this repository to guess at.
    """

    def __init__(
        self,
        client: anthropic.Anthropic,
        *,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        """Wrap an already-constructed SDK client.

        Args:
            client: The SDK client. Constructing it — and therefore reading a
                credential — is the caller's business; `client_from_environment` is the
                only place in this repository that does it from the environment.
            model: The model to call.
            max_tokens: Output ceiling per call.
        """
        self._client = client
        self._model = model
        self._max_tokens = max_tokens

    @property
    def model_id(self) -> str:
        """Identifier of the model behind this client."""
        return self._model

    def call_tool(
        self,
        *,
        system: str,
        user_text: str,
        tool: ToolSpec,
    ) -> Mapping[str, object]:
        """Call `tool`, forced and strict, and return its arguments.

        Args:
            system: The rules the model works under.
            user_text: The material the model works on.
            tool: The tool it must call, and the schema its arguments must satisfy.

        Returns:
            The tool call's arguments as the API returned them.

        Raises:
            ModelCallError: If the model declines, or returns no call to `tool`.
        """
        message = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_text}],
            tools=[
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": dict(tool.input_schema),
                    "strict": True,
                },
            ],
            tool_choice={
                "type": "tool",
                "name": tool.name,
                "disable_parallel_tool_use": True,
            },
        )
        return _tool_arguments(message, tool.name)


def client_from_environment(
    *,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> AnthropicModelClient:
    """Build a client from `ANTHROPIC_API_KEY`.

    The key is read here and passed to the SDK explicitly rather than left to the SDK's
    own credential resolution. That is the point: with an explicit key there is exactly
    one way a credential enters this process, and it is this function.

    Args:
        model: The model to call.
        max_tokens: Output ceiling per call.

    Returns:
        A client ready to inject into `parsing` and `rationale`.

    Raises:
        MissingApiKeyError: If the variable is unset or empty.
    """
    api_key = os.environ.get(API_KEY_VARIABLE, "").strip()
    if not api_key:
        msg = (
            f"{API_KEY_VARIABLE} is not set. It is read in exactly one place "
            f"({__name__}); every other module takes a client by injection. No "
            f"determination needs it: the rules are pure functions over documented data."
        )
        raise MissingApiKeyError(msg)
    return AnthropicModelClient(
        anthropic.Anthropic(api_key=api_key),
        model=model,
        max_tokens=max_tokens,
    )
