"""Tests for parsing free text into a validated `Recommendation`.

The centre of gravity is the risk rating: where it is allowed to come from, and what
happens when the source text does not state one. Everything runs against a stub client,
so no test needs a key or a network.
"""

from collections.abc import Mapping

import pytest

from suitability_receipts import Product, Recommendation, RiskLevel, TradeAction, determine
from suitability_receipts.llm import ModelCallError, ParseRequest, parse_recommendation
from suitability_receipts.llm.parsing import (
    PARSE_TOOL_NAME,
    PARSER_VERSION,
    UnparseableRecommendationError,
)
from synthetic import (
    NOW,
    REVIEWED_ON,
    SOURCE_TEXT,
    STUB_MODEL_ID,
    StubModelClient,
    a_product,
    a_profile,
    parse_arguments,
    product_arguments,
)

REQUEST = ParseRequest(
    source_text=SOURCE_TEXT,
    recommendation_id="SYNTHETIC-REC-0002",
    client_id="SYNTHETIC-0001",
    proposed_on=REVIEWED_ON,
    profile_last_reviewed=REVIEWED_ON,
)

CATALOGUE: dict[str, Product] = {"SYNTHETIC-FUND-Z": a_product()}


def parse(
    arguments: dict[str, object],
    *,
    products: Mapping[str, Product] | None = None,
) -> Recommendation:
    """Parse `arguments` as though a model had returned them."""
    return parse_recommendation(StubModelClient(arguments), REQUEST, products=products)


# ---------------------------------------------------------------------------
# The happy paths
# ---------------------------------------------------------------------------


def test_a_quoted_rating_is_accepted_and_its_source_named() -> None:
    """With no product record, the rating comes from the text and says so."""
    product = parse(parse_arguments()).product
    assert product.risk_rating is RiskLevel.MEDIUM
    assert product.risk_rating_source == 'quoted from the source text: "rates the fund medium risk"'


def test_a_product_record_is_authoritative() -> None:
    """A catalogued instrument takes its terms from the record, not from the model."""
    arguments = parse_arguments(
        product=product_arguments(risk_rating="high", risk_rating_quote="high risk"),
    )
    product = parse(arguments, products=CATALOGUE).product
    assert product.risk_rating is RiskLevel.MEDIUM
    assert product.risk_rating_source == "SYNTHETIC fund facts sheet"


def test_source_text_and_parsed_by_are_set_together() -> None:
    """The model requires both or neither; a parsed recommendation carries both."""
    recommendation = parse(parse_arguments())
    assert recommendation.source_text == SOURCE_TEXT
    assert recommendation.parsed_by == (f"{PARSE_TOOL_NAME}@{PARSER_VERSION} model={STUB_MODEL_ID}")


def test_the_parse_carries_the_request_not_the_model_s_opinion_of_it() -> None:
    """Identity and dates come from the caller; the model supplies neither."""
    recommendation = parse(parse_arguments())
    assert recommendation.recommendation_id == "SYNTHETIC-REC-0002"
    assert recommendation.client_id == "SYNTHETIC-0001"
    assert recommendation.proposed_on == REVIEWED_ON
    assert recommendation.action is TradeAction.BUY


def test_claims_about_the_client_are_dated_by_code() -> None:
    """A documentation date is a property of the profile, not of the text."""
    recommendation = parse(parse_arguments())
    citations = recommendation.rationale.cited_factors
    assert [citation.key.value for citation in citations] == [
        "risk_tolerance",
        "time_horizon_years",
    ]
    assert all(citation.documented_on == REVIEWED_ON for citation in citations)


def test_the_profile_is_never_sent_to_the_model() -> None:
    """The parser is given the text and, at most, product identifiers. Nothing else."""
    client = StubModelClient(parse_arguments())
    parse_recommendation(client, REQUEST, products=CATALOGUE)
    _, user_text, _ = client.calls[0]
    assert "SYNTHETIC-0001" not in user_text
    assert "120000" not in user_text
    assert "SYNTHETIC fund facts sheet" not in user_text


def test_a_parsed_recommendation_goes_straight_to_the_engine() -> None:
    """What parsing produces is what determination consumes; no adapter in between."""
    recommendation = parse(parse_arguments())
    outcome = determine(a_profile(), recommendation, now=NOW)
    assert outcome.outcome == "supported"


# ---------------------------------------------------------------------------
# Refusing to invent a risk rating
# ---------------------------------------------------------------------------


def test_no_stated_rating_refuses_the_parse() -> None:
    """The required case: the text states no rating, so there is no recommendation."""
    arguments = parse_arguments(
        product=product_arguments(risk_rating=None, risk_rating_quote=None),
    )
    with pytest.raises(UnparseableRecommendationError) as raised:
        parse(arguments)
    assert "states no risk rating" in str(raised.value)


def test_a_rating_without_a_quotation_refuses_the_parse() -> None:
    """A rating with nothing behind it is the model's judgement, which is not a source."""
    arguments = parse_arguments(
        product=product_arguments(risk_rating="medium", risk_rating_quote=None),
    )
    with pytest.raises(UnparseableRecommendationError):
        parse(arguments)


def test_a_quotation_absent_from_the_source_refuses_the_parse() -> None:
    """The quotation is checked against the text, not taken on trust."""
    arguments = parse_arguments(
        product=product_arguments(risk_rating_quote="rated medium risk by the manager"),
    )
    with pytest.raises(UnparseableRecommendationError, match="does not appear in the source text"):
        parse(arguments)


def test_a_quotation_that_states_no_rating_refuses_the_parse() -> None:
    """A verbatim quotation of something that is not a rating is still not a rating."""
    arguments = parse_arguments(
        product=product_arguments(risk_rating_quote="issued by Synthetic Placeholder Issuer Z"),
    )
    with pytest.raises(UnparseableRecommendationError, match="does not state a risk rating"):
        parse(arguments)


def test_a_quotation_that_states_a_different_rating_refuses_the_parse() -> None:
    """The rating is derived from the quotation by code; the model's label must agree."""
    arguments = parse_arguments(
        product=product_arguments(
            risk_rating="high", risk_rating_quote="rates the fund medium risk"
        ),
    )
    with pytest.raises(UnparseableRecommendationError, match="states medium"):
        parse(arguments)


@pytest.mark.parametrize(
    ("quote", "expected"),
    [
        ("rates the fund medium risk", RiskLevel.MEDIUM),
        ("rated low risk", RiskLevel.LOW),
        ("a low to medium risk rating", RiskLevel.LOW_TO_MEDIUM),
        ("rated medium-to-high risk", RiskLevel.MEDIUM_TO_HIGH),
        ("rated high risk", RiskLevel.HIGH),
    ],
)
def test_compound_ratings_read_as_the_compound_not_a_component(
    quote: str,
    expected: RiskLevel,
) -> None:
    """Longest match wins, so "low to medium" is not read as "low" or as "medium"."""
    source = f"Recommend the fund. The fact sheet {quote}. Redeemable daily."
    request = ParseRequest(
        source_text=source,
        recommendation_id="SYNTHETIC-REC-0003",
        client_id="SYNTHETIC-0001",
        proposed_on=REVIEWED_ON,
        profile_last_reviewed=REVIEWED_ON,
    )
    arguments = parse_arguments(
        product=product_arguments(risk_rating=expected.value, risk_rating_quote=quote),
    )
    recommendation = parse_recommendation(StubModelClient(arguments), request)
    assert recommendation.product.risk_rating is expected


def test_a_quotation_naming_two_points_on_the_scale_is_ambiguous() -> None:
    """A quotation naming two points on the scale states no single rating."""
    source = "The fund's risk ranges from low to high depending on the sleeve."
    request = ParseRequest(
        source_text=source,
        recommendation_id="SYNTHETIC-REC-0004",
        client_id="SYNTHETIC-0001",
        proposed_on=REVIEWED_ON,
        profile_last_reviewed=REVIEWED_ON,
    )
    arguments = parse_arguments(
        product=product_arguments(
            risk_rating="high",
            risk_rating_quote="risk ranges from low to high",
        ),
    )
    with pytest.raises(UnparseableRecommendationError, match="does not state a risk rating"):
        parse_recommendation(StubModelClient(arguments), request)


# ---------------------------------------------------------------------------
# Refusing to invent the other product terms
# ---------------------------------------------------------------------------


def test_an_unstated_lock_up_refuses_the_parse() -> None:
    """A null lock-up is not read as zero days."""
    arguments = parse_arguments(product=product_arguments(lock_up_days=None))
    with pytest.raises(UnparseableRecommendationError, match="lock-up"):
        parse(arguments)


def test_unstated_redemption_terms_refuse_the_parse() -> None:
    """A null redemption frequency is not read as daily liquidity."""
    arguments = parse_arguments(product=product_arguments(redemption_frequency=None))
    with pytest.raises(UnparseableRecommendationError, match="redemption frequency"):
        parse(arguments)


def test_every_problem_is_reported_not_only_the_first() -> None:
    """Reporting one of several problems would understate what the text does not say."""
    arguments = parse_arguments(
        product=product_arguments(
            lock_up_days=None,
            redemption_frequency=None,
            risk_rating=None,
            risk_rating_quote=None,
        ),
    )
    with pytest.raises(UnparseableRecommendationError) as raised:
        parse(arguments)
    assert len(raised.value.problems) == 3


def test_an_unknown_product_with_no_description_refuses_the_parse() -> None:
    """Neither of the two permitted sources supplied anything."""
    arguments = parse_arguments(product=None)
    with pytest.raises(UnparseableRecommendationError, match="no product record was supplied"):
        parse(arguments)


# ---------------------------------------------------------------------------
# A broken call is not a domain outcome
# ---------------------------------------------------------------------------


def test_arguments_off_the_schema_are_a_call_failure() -> None:
    """The API misbehaving is never mistaken for the text saying nothing."""
    with pytest.raises(ModelCallError, match="do not match its schema"):
        parse({"instrument_id": "SYNTHETIC-FUND-Z"})


def test_an_invented_field_is_rejected() -> None:
    """The payload is closed: the model may not smuggle a field past the schema."""
    with pytest.raises(ModelCallError):
        parse(parse_arguments(suitability="supported"))
