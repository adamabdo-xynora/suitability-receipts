"""Turning free text into a validated `Recommendation`, or refusing to.

The model transcribes. It reads a sentence an advisor wrote and returns the same facts
in the shape the domain models require. It does not assess the product, does not decide
whether the recommendation is suitable, and does not fill a term the text leaves out.

The risk rating
---------------
This is the field the boundary is tested on, because it is the one a model is most
tempted to supply from its own knowledge of the product. There are exactly two places a
risk rating may come from, and no third:

* **A supplied product record.** When the instrument is in the catalogue passed to
  `parse_recommendation`, that record is used whole — rating, source, terms and all —
  and anything the model said about the product is ignored. The catalogue is authority;
  the model is not.
* **A verbatim quotation from the source text.** With no catalogue entry, the model must
  return both a rating and `risk_rating_quote`: the exact words of the source that state
  it. Code then checks three things. The quote must appear in the source text. The quote
  must itself state a rating on the documented scale, decided by `RISK_RATING_SPELLINGS`
  and longest match, by code — so the rating is *derived from the quotation*, not taken
  on the model's word. And the derived rating must equal the one the model recorded.

If the source states no rating and no record is supplied, parsing fails. It does not
guess, and it does not fall back to a middling default, which would be a guess wearing a
neutral face.

The same "state it or fail" rule applies to the lock-up and the redemption frequency:
both are nullable in the tool schema so that "the text does not say" is expressible, and
a null refuses the parse rather than being read as zero days or as daily liquidity.

What the model may claim about the client
-----------------------------------------
`claimed_factors` are claims, not findings. They land in
`RecommendationRationale.cited_factors` and the engine's `unsupported_rationale` rule
checks each one against the profile. The model never sees the profile here; the
documentation date on each claim is stamped by code from `ParseRequest`, because a date
is a property of which profile is in play rather than anything the text can assert.
"""

import datetime as dt
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from suitability_receipts.llm.client import ModelCallError, ModelClient, ToolSpec
from suitability_receipts.models import (
    Currency,
    FactorCitation,
    FactorKey,
    HorizonYears,
    Money,
    MoneyAmount,
    NonEmptyStr,
    Product,
    Recommendation,
    RecommendationRationale,
    RedemptionFrequency,
    RiskLevel,
    TradeAction,
)

__all__ = [
    "PARSER_VERSION",
    "PARSE_SYSTEM_PROMPT",
    "PARSE_TOOL",
    "PARSE_TOOL_NAME",
    "RISK_RATING_SPELLINGS",
    "ParseRequest",
    "UnparseableRecommendationError",
    "parse_recommendation",
]

PARSER_VERSION = "0.1.0"
"""Recorded in `Recommendation.parsed_by`. Bump it when the prompt or the tool schema
changes, so that two recommendations parsed differently can be told apart."""

PARSE_TOOL_NAME = "record_recommendation"

RISK_RATING_SPELLINGS: MappingProxyType[RiskLevel, tuple[str, ...]] = MappingProxyType(
    {
        RiskLevel.LOW: ("low",),
        RiskLevel.LOW_TO_MEDIUM: (
            "low to medium",
            "low-to-medium",
            "low-medium",
            "low/medium",
            "low to moderate",
        ),
        RiskLevel.MEDIUM: ("medium", "moderate"),
        RiskLevel.MEDIUM_TO_HIGH: (
            "medium to high",
            "medium-to-high",
            "medium-high",
            "medium/high",
            "moderate to high",
        ),
        RiskLevel.HIGH: ("high",),
    },
)
"""How each point on the risk scale may be written in a source document.

DERIVATION: none. This is a repository choice, like the thresholds in `engine.py`, and
it is a deliberately small one. It exists so that a quoted risk rating is read by code
rather than accepted on the model's word, which means the reading has to be written
down somewhere it can be argued with.

Its consequence is a narrow refusal: a source that states a rating in words not on this
list is refused, not interpreted. That is the intended direction of failure. Widening
the list is a decision to accept more phrasings as equivalent to a scale point, and it
belongs in this constant where it can be reviewed, not in a model's judgement where it
cannot.

TO DO: calibrate against the eval set once it exists, and record what phrasings real
documents actually use."""

_SPELLING_PATTERNS: MappingProxyType[RiskLevel, tuple[re.Pattern[str], ...]] = MappingProxyType(
    {
        level: tuple(re.compile(rf"\b{re.escape(spelling)}\b") for spelling in spellings)
        for level, spellings in RISK_RATING_SPELLINGS.items()
    },
)

_AMOUNT_PATTERN = r"^[0-9]{1,16}(\.[0-9]{1,2})?$"
_HORIZON_PATTERN = r"^[0-9]{1,3}(\.[0-9])?$"

_ACTIONS = [action.value for action in TradeAction]
_CURRENCIES = [currency.value for currency in Currency]
_FACTOR_KEYS = [key.value for key in FactorKey]
_REDEMPTION_FREQUENCIES = [frequency.value for frequency in RedemptionFrequency]
_RISK_LEVELS = [level.value for level in RiskLevel]

PARSE_SYSTEM_PROMPT = """You transcribe investment recommendations. You do not evaluate them.

You are given the text of a proposed recommendation. Return the same facts in the
structure the tool requires. You are a transcriber with a fixed vocabulary, not an
analyst.

Rules, in order of importance:

1. Never supply a fact the text does not state. Every field that can be null is null
   when the text does not state it. A null is a correct answer; a plausible value is not.
2. Never assign a risk rating. If the text states one, record it and copy the exact
   words that state it into `risk_rating_quote` — that quotation is checked against the
   text and the rating is re-derived from it by code. If the text states no rating, set
   both `risk_rating` and `risk_rating_quote` to null. Your own knowledge of the product,
   its asset class, or products like it is not a source.
3. The same applies to `lock_up_days` and `redemption_frequency`. If the text does not
   state the lock-up or the redemption terms, they are null. Do not read "no lock-up
   mentioned" as zero, and do not read a fund as daily-redeemable because most are.
4. `claimed_factors` are the client facts the text asserts, in the text's own terms. They
   are claims to be checked against the documented profile later, not findings. Record
   what the text says; if it says nothing about the client, return an empty list.
5. `amount` is digits only — no currency symbol, no thousands separators.
6. `rationale_text` is the reasoning the text gives, not reasoning you supply.
"""

_PRODUCT_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "name",
        "issuer",
        "sector",
        "risk_rating",
        "risk_rating_quote",
        "lock_up_days",
        "redemption_frequency",
        "recommended_holding_period_years",
    ],
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "issuer": {"type": "string", "minLength": 1},
        "sector": {"anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}]},
        "risk_rating": {
            "anyOf": [{"type": "string", "enum": _RISK_LEVELS}, {"type": "null"}],
            "description": (
                "The risk rating the source text states, mapped onto this scale. null if "
                "the source text states none. Never your own assessment of the product."
            ),
        },
        "risk_rating_quote": {
            "anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}],
            "description": (
                "The exact words of the source text that state the risk rating, copied "
                "verbatim. Checked against the source text; the rating is re-derived "
                "from it. null if the source text states no rating."
            ),
        },
        "lock_up_days": {
            "anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}],
            "description": "null if the source text does not state a lock-up.",
        },
        "redemption_frequency": {
            "anyOf": [{"type": "string", "enum": _REDEMPTION_FREQUENCIES}, {"type": "null"}],
            "description": "null if the source text does not state redemption terms.",
        },
        "recommended_holding_period_years": {
            "anyOf": [{"type": "string", "pattern": _HORIZON_PATTERN}, {"type": "null"}],
            "description": "Digits only, to one decimal place. null if not stated.",
        },
    },
}

_PARSE_INPUT_SCHEMA: Mapping[str, object] = MappingProxyType(
    {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "instrument_id",
            "action",
            "amount",
            "currency",
            "rationale_text",
            "claimed_factors",
            "product",
        ],
        "properties": {
            "instrument_id": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "The instrument identifier the text names. If a product record list "
                    "is supplied below, use the identifier from that list."
                ),
            },
            "action": {"type": "string", "enum": _ACTIONS},
            "amount": {
                "type": "string",
                "pattern": _AMOUNT_PATTERN,
                "description": "Digits only, at most two decimal places.",
            },
            "currency": {"type": "string", "enum": _CURRENCIES},
            "rationale_text": {
                "type": "string",
                "minLength": 1,
                "description": "The reasoning the source text gives, in its own terms.",
            },
            "claimed_factors": {
                "type": "array",
                "description": (
                    "Client facts the source text asserts. Claims to be checked against "
                    "the documented profile, not findings. Empty if the text asserts none."
                ),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["key", "value"],
                    "properties": {
                        "key": {"type": "string", "enum": _FACTOR_KEYS},
                        "value": {"type": "string", "minLength": 1},
                    },
                },
            },
            "product": {
                "description": (
                    "The product as the source text describes it. null when the "
                    "instrument appears in the supplied product records — those are "
                    "authoritative and are used instead."
                ),
                "anyOf": [_PRODUCT_SCHEMA, {"type": "null"}],
            },
        },
    },
)

PARSE_TOOL = ToolSpec(
    name=PARSE_TOOL_NAME,
    description=(
        "Record a proposed recommendation exactly as the source text states it. Fields "
        "the text does not state are null. Risk ratings are transcribed with the "
        "quotation that states them, never assigned."
    ),
    input_schema=_PARSE_INPUT_SCHEMA,
)


class UnparseableRecommendationError(ValueError):
    """The source text does not state what a `Recommendation` requires.

    A refusal to parse, not a failed call: the model did its job and the text does not
    contain the facts. Carries every problem found, not the first, for the same reason
    the engine reports every refusal reason.
    """

    def __init__(self, problems: tuple[str, ...]) -> None:
        """Record every problem that prevented a parse."""
        self.problems = problems
        super().__init__(
            "the source text cannot be parsed into a recommendation: " + "; ".join(problems),
        )


@dataclass(frozen=True, slots=True)
class ParseRequest:
    """Everything a parse needs that the source text does not supply.

    None of this reaches the model. `client_id` and `profile_last_reviewed` are used by
    code only — the first to address the recommendation, the second to stamp the
    documentation date on the claims the text makes about the client.
    """

    source_text: str
    recommendation_id: str
    client_id: str
    proposed_on: dt.date
    profile_last_reviewed: dt.date


class _ClaimedFactorPayload(BaseModel):
    """One claim the source text makes about the client."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: FactorKey
    value: NonEmptyStr


class _ProductPayload(BaseModel):
    """The product as the source text describes it, before any of it is believed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: NonEmptyStr
    issuer: NonEmptyStr
    sector: NonEmptyStr | None
    risk_rating: RiskLevel | None
    risk_rating_quote: NonEmptyStr | None
    lock_up_days: Annotated[int, Field(ge=0)] | None
    redemption_frequency: RedemptionFrequency | None
    recommended_holding_period_years: HorizonYears | None


class _ParsePayload(BaseModel):
    """The tool's arguments, validated before anything downstream sees them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    instrument_id: NonEmptyStr
    action: TradeAction
    amount: MoneyAmount
    currency: Currency
    rationale_text: NonEmptyStr
    claimed_factors: tuple[_ClaimedFactorPayload, ...]
    product: _ProductPayload | None


def _normalise(text: str) -> str:
    """Collapse whitespace and case, so that a quotation is compared on its content."""
    return " ".join(text.split()).casefold()


def _risk_level_of(quote: str) -> RiskLevel | None:
    """Return the risk level `quote` states, or `None` if it states none unambiguously.

    Longest match wins, so "low to medium" reads as `low_to_medium` rather than as `low`
    or `medium`. After the winning phrase is removed, any other level still named in what
    remains makes the quotation ambiguous — "ranges from low to high" names two points on
    the scale and is not a rating — and an ambiguous quotation states nothing.
    """
    text = _normalise(quote)
    matches = [
        (len(pattern.pattern), level, pattern)
        for level, patterns in _SPELLING_PATTERNS.items()
        for pattern in patterns
        if pattern.search(text)
    ]
    if not matches:
        return None
    longest = max(length for length, _, _ in matches)
    winners = {level for length, level, _ in matches if length == longest}
    if len(winners) != 1:
        return None
    level = next(iter(winners))

    remainder = text
    for length, matched_level, pattern in matches:
        if matched_level is level and length == longest:
            remainder = pattern.sub(" ", remainder)
    others = {
        other
        for other, patterns in _SPELLING_PATTERNS.items()
        if other is not level and any(pattern.search(remainder) for pattern in patterns)
    }
    if others:
        return None
    return level


def _verified_risk_rating(
    parsed: _ProductPayload,
    source_text: str,
    problems: list[str],
) -> RiskLevel | None:
    """Return the risk rating the source text states, or `None`, recording why not."""
    quote = parsed.risk_rating_quote
    claimed = parsed.risk_rating
    if quote is None or claimed is None:
        problems.append(
            "the source text states no risk rating for the product, and none was "
            "supplied as a product record; a risk rating is never assigned by the model",
        )
        return None
    if _normalise(quote) not in _normalise(source_text):
        problems.append(f"the quoted risk rating {quote!r} does not appear in the source text")
        return None
    derived = _risk_level_of(quote)
    if derived is None:
        problems.append(
            f"the quoted text {quote!r} does not state a risk rating on the documented scale",
        )
        return None
    if derived is not claimed:
        problems.append(
            f"the quoted text {quote!r} states {derived.value}, but {claimed.value} was recorded",
        )
        return None
    return derived


def _resolve_product(
    payload: _ParsePayload,
    source_text: str,
    catalogue: Mapping[str, Product],
    problems: list[str],
) -> Product | None:
    """Return the product record, from the catalogue or from the text, or `None`."""
    record = catalogue.get(payload.instrument_id)
    if record is not None:
        return record

    parsed = payload.product
    if parsed is None:
        problems.append(
            f"no product record was supplied for {payload.instrument_id} and the source "
            f"text does not describe it",
        )
        return None

    lock_up_days = parsed.lock_up_days
    redemption_frequency = parsed.redemption_frequency
    if lock_up_days is None:
        problems.append("the source text does not state the product's lock-up period")
    if redemption_frequency is None:
        problems.append("the source text does not state the product's redemption frequency")
    risk_rating = _verified_risk_rating(parsed, source_text, problems)
    if lock_up_days is None or redemption_frequency is None or risk_rating is None:
        return None

    return Product(
        instrument_id=payload.instrument_id,
        name=parsed.name,
        issuer=parsed.issuer,
        sector=parsed.sector,
        risk_rating=risk_rating,
        risk_rating_source=(f'quoted from the source text: "{parsed.risk_rating_quote}"'),
        lock_up_days=lock_up_days,
        redemption_frequency=redemption_frequency,
        recommended_holding_period_years=parsed.recommended_holding_period_years,
    )


def parse_recommendation(
    client: ModelClient,
    request: ParseRequest,
    *,
    products: Mapping[str, Product] | None = None,
) -> Recommendation:
    """Parse free text into a validated `Recommendation`.

    Args:
        client: An already-constructed client. This module reads no credential.
        request: The source text and the facts about the request the text cannot state.
        products: Product records by instrument identifier. A record here is
            authoritative: it supplies the risk rating and every other term, and
            anything the model said about that product is ignored.

    Returns:
        A `Recommendation` carrying `source_text` and `parsed_by` together, as the model
        requires, with the model and tool that produced it named in `parsed_by`.

    Raises:
        ModelCallError: If the tool's arguments do not match its schema.
        UnparseableRecommendationError: If the source text does not state what a
            recommendation requires — most often a risk rating.
    """
    arguments = client.call_tool(
        system=PARSE_SYSTEM_PROMPT,
        user_text=_user_text(request.source_text, products),
        tool=PARSE_TOOL,
    )
    try:
        payload = _ParsePayload.model_validate(arguments)
    except ValidationError as error:
        msg = f"{PARSE_TOOL_NAME} returned arguments that do not match its schema: {error}"
        raise ModelCallError(msg) from error

    problems: list[str] = []
    product = _resolve_product(payload, request.source_text, products or {}, problems)
    if product is None:
        raise UnparseableRecommendationError(tuple(problems))

    citations = tuple(
        FactorCitation(
            key=claim.key,
            value=claim.value,
            documented_on=request.profile_last_reviewed,
        )
        for claim in payload.claimed_factors
    )
    try:
        return Recommendation(
            recommendation_id=request.recommendation_id,
            client_id=request.client_id,
            action=payload.action,
            product=product,
            amount=Money(amount=payload.amount, currency=payload.currency),
            proposed_on=request.proposed_on,
            rationale=RecommendationRationale(
                text=payload.rationale_text,
                cited_factors=citations,
            ),
            source_text=request.source_text,
            parsed_by=f"{PARSE_TOOL_NAME}@{PARSER_VERSION} model={client.model_id}",
        )
    except ValidationError as error:
        raise UnparseableRecommendationError((str(error),)) from error


def _user_text(source_text: str, products: Mapping[str, Product] | None) -> str:
    """Assemble the source text and, if supplied, the identifiers of known products.

    Only identifier, name and issuer are shown. The catalogue's risk ratings are
    deliberately withheld: the model has no use for a rating it is forbidden to record,
    and showing it would invite the text and the record to be blurred together.
    """
    sections = ["SOURCE TEXT:", source_text]
    if products:
        sections.append("PRODUCT RECORDS ON FILE — use these identifiers where they match:")
        sections.append(
            "\n".join(
                f"- {instrument_id}: {product.name}, issued by {product.issuer}"
                for instrument_id, product in sorted(products.items())
            ),
        )
        sections.append(
            "For an instrument in that list, set `product` to null: the record on file "
            "is used, not your description of it.",
        )
    return "\n\n".join(sections)
