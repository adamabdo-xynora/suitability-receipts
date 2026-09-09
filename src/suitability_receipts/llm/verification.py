"""Checking a drafted rationale against the documented profile. No model, no network.

The model writes prose and *declares* what it relied on. This module checks the
declarations against the profile and refuses the rationale whole if any of them fails.
It imports nothing from `client`, so a rationale can be verified — and the verification
tested — without a key, a client, or the `anthropic` package.

A failed rationale is never repaired. There is no code path that drops the bad citation
and keeps the sentence, or rewrites the prose to match the profile. A patched rationale
is one nobody reviewed: it would carry the authority of a checked document while saying
whatever survived the patch. The verdict is the whole rationale or nothing.

How far the verification goes
-----------------------------
Two checks, and they are different in kind.

1. **Declarations.** Every declared citation is checked against the profile through
   `engine.citation_matches_profile` — the same function `check_unsupported_rationale`
   uses, so the two cannot drift apart. The key must be documented and the value must
   match what is documented. A declared *absence* ("the profile does not document a risk
   tolerance") is checked the other way: the key must indeed be undocumented. Absences
   exist because the rationale for a `missing_kyc_factor` refusal is an argument about
   what the profile does not say, and without them that argument would be unverifiable.

2. **Numbers in the prose.** Every run of digits in the prose must appear in the
   material the rationale is allowed to rest on: the documented values of the factors it
   declared, the recommendation, the profile's review date, and the determination the
   engine already made. Commas and underscores are stripped first, so "50,000" grounds
   against "50000.00" and "3-year" against "3.0". This is the one check that reads the
   prose rather than the declarations, and it exists because a fabricated number is the
   most damaging thing a rationale can contain and the only kind of fabrication that can
   be caught without judgement.

WHAT THIS DOES NOT CATCH, stated plainly because the gap is real and a reader of a
verified rationale is entitled to know its exact width:

* **A fabricated qualitative claim.** A rationale can declare three correct citations
  and still write "the client has extensive experience with structured products" when
  the profile documents nothing of the kind. Every declaration passes; the sentence is
  invented; this module returns `VerifiedRationale`. Nothing here reads the prose for
  meaning, and nothing here maps a sentence to the citation it is supposed to rest on.
* **A number written in words.** "two hundred thousand" is not digits and is not
  checked. So is any figure the prose approximates ("roughly a fifth").
* **A correct citation used to dress an unrelated claim.** Citing a documented time
  horizon and then asserting something about liquidity passes: the citation is true, and
  the connection between citation and sentence is exactly what is not verified.
* **A softened refusal.** Whether prose describes a refusal as a refusal is a judgement
  about tone and emphasis. There is no mechanical check for it here, and the prompt
  instruction in `rationale.py` is an instruction, not a guarantee.
* **Anything about whether the determination is right.** That was settled by the engine
  before this module ran. Verification checks the explanation against the profile; it
  does not re-open the outcome.

So: a `VerifiedRationale` means *every client fact the model declared is documented as
declared, and every digit it wrote is traceable*. It does not mean the prose is true.
Read it as a checked bibliography, not a checked argument.
"""

import re
from dataclasses import dataclass
from typing import ClassVar

from suitability_receipts.engine import citation_matches_profile, documented_citation
from suitability_receipts.models import (
    ClientProfile,
    Determination,
    FactorCitation,
    FactorKey,
    Recommendation,
    RecommendationRationale,
    RefusalCode,
    SuitabilityOutcome,
)

__all__ = [
    "DraftedRationale",
    "RationaleVerdict",
    "UnsupportedRationale",
    "VerifiedRationale",
    "outcome_evidence",
    "recommendation_evidence",
    "verify_rationale",
]

_DIGIT_RUN = re.compile(r"[0-9]+")
_GROUPING = str.maketrans({",": None, "_": None})
"""Digit-grouping characters, removed before digits are extracted, so that a number
written for a reader grounds against the same number written for a machine."""


@dataclass(frozen=True, slots=True)
class DraftedRationale:
    """What the model produced: prose, plus the declarations it is to be checked against.

    `cited_factors` carry `documented_on` stamped by code from the profile in play, not
    supplied by the model. A documentation date is not something the model can know from
    the material it was given, and a field whose check can never fail is theatre.
    """

    prose: str
    cited_factors: tuple[FactorCitation, ...]
    absent_factors: tuple[FactorKey, ...]
    drafted_by: str


@dataclass(frozen=True, slots=True)
class VerifiedRationale:
    """A rationale whose every declaration was checked and held.

    Read `verification.py`'s module docstring for what that does and does not mean.
    """

    rationale: RecommendationRationale
    absent_factors: tuple[FactorKey, ...]
    drafted_by: str


@dataclass(frozen=True, slots=True)
class UnsupportedRationale:
    """A rationale that failed verification, with every reason it failed.

    Every check runs before the verdict is returned, for the reason the engine never
    short-circuits either: naming one of several problems understates what is wrong.

    This is not a `Refusal`. Receipts are the engine's to issue, and a refused rationale
    simply never reaches a recommendation — leaving the engine's own
    `unsupported_rationale` and `missing_kyc_factor` rules to produce the receipt from
    inputs they can see. `code` names the taxonomy entry this belongs to and nothing more.
    """

    detail: str
    unsupported_citations: tuple[FactorCitation, ...] = ()
    contradicted_absences: tuple[FactorKey, ...] = ()
    duplicate_declarations: tuple[FactorKey, ...] = ()
    ungrounded_numbers: tuple[str, ...] = ()
    code: ClassVar[RefusalCode] = RefusalCode.UNSUPPORTED_RATIONALE


type RationaleVerdict = VerifiedRationale | UnsupportedRationale


def recommendation_evidence(recommendation: Recommendation) -> str:
    """Render the recommendation's terms.

    Shown to the model when it drafts, and read for digits when the draft is checked.
    One function for both so that the numbers a rationale may use are exactly the
    numbers it was shown.
    """
    product = recommendation.product
    holding_period = product.recommended_holding_period_years
    return "\n".join(
        (
            f"action: {recommendation.action.value}",
            f"amount: {recommendation.amount.amount} {recommendation.amount.currency.value}",
            f"proposed on: {recommendation.proposed_on.isoformat()}",
            f"product: {product.name} ({product.instrument_id})",
            f"product issuer: {product.issuer}",
            f"product sector: {product.sector if product.sector is not None else 'not documented'}",
            (
                f"product risk rating: {product.risk_rating.value} "
                f"(source: {product.risk_rating_source})"
            ),
            f"product lock-up: {product.lock_up_days} days",
            f"product redemption frequency: {product.redemption_frequency.value}",
            "product recommended holding period: "
            + (f"{holding_period} years" if holding_period is not None else "not documented"),
        ),
    )


def _determination_evidence(outcome: Determination) -> tuple[str, ...]:
    """Render a supported determination as the checks that produced it."""
    lines = [
        "outcome: supported",
        f"profile last reviewed: {outcome.profile_last_reviewed.isoformat()}",
    ]
    lines.extend(
        f"check {check.code.value} passed, on: "
        + "; ".join(f"{citation.key.value} = {citation.value}" for citation in check.basis)
        for check in outcome.checks
    )
    return tuple(lines)


def _refusal_evidence(outcome: SuitabilityOutcome) -> tuple[str, ...]:
    """Render a refusal as every reason that fired, in full."""
    if isinstance(outcome, Determination):  # pragma: no cover - guarded by the caller
        msg = "not a refusal"
        raise TypeError(msg)
    lines = [
        "outcome: refused",
        f"profile last reviewed: {outcome.profile_last_reviewed.isoformat()}",
    ]
    for reason in outcome.reasons:
        lines.append(f"reason {reason.code.value}: {reason.detail}")
        if reason.missing_factors:
            lines.append(
                "  factors not documented: "
                + ", ".join(key.value for key in reason.missing_factors),
            )
        lines.extend(
            f"  conflicting fact: {citation.key.value} = {citation.value}"
            for citation in reason.conflicting_factors
        )
        lines.extend(
            f"  unsupported claim: {citation.key.value} = {citation.value}"
            for citation in reason.unsupported_claims
        )
    return tuple(lines)


def outcome_evidence(outcome: SuitabilityOutcome) -> str:
    """Render the determination the engine already made.

    Receipt identifiers and digests are left out. They are on the receipt, they are the
    only fields whose digits mean nothing in prose, and including them would whitelist
    dozens of arbitrary digit runs for the grounding check.
    """
    lines = (
        _determination_evidence(outcome)
        if isinstance(outcome, Determination)
        else _refusal_evidence(outcome)
    )
    return "\n".join(lines)


def _digits(text: str) -> frozenset[str]:
    """Return every run of digits in `text`, with grouping characters removed first."""
    return frozenset(_DIGIT_RUN.findall(text.translate(_GROUPING)))


def _grounded_digits(
    profile: ClientProfile,
    drafted: DraftedRationale,
    outcome: SuitabilityOutcome,
    recommendation: Recommendation,
) -> frozenset[str]:
    """Return the digit runs the prose is allowed to use.

    Built from the *documented* value of each declared factor, never from the value the
    model claimed, so that a wrong number cannot ground itself.
    """
    parts = [
        outcome_evidence(outcome),
        recommendation_evidence(recommendation),
        profile.last_reviewed.isoformat(),
    ]
    for citation in drafted.cited_factors:
        documented = documented_citation(profile, citation.key)
        if documented is not None:
            parts.append(documented.value)
            parts.append(documented.documented_on.isoformat())
    return _digits("\n".join(parts))


def _citation_problem(profile: ClientProfile, citation: FactorCitation) -> str:
    """Describe why a citation is not supported by the profile."""
    documented = documented_citation(profile, citation.key)
    if documented is None:
        return f"cited {citation.key.value}, which the profile does not document at all"
    return (
        f"cited {citation.key.value} as {citation.value!r}, but the profile documents "
        f"{documented.value!r} as of {documented.documented_on.isoformat()}"
    )


def verify_rationale(
    profile: ClientProfile,
    drafted: DraftedRationale,
    *,
    outcome: SuitabilityOutcome,
    recommendation: Recommendation,
) -> RationaleVerdict:
    """Check a drafted rationale against the documented profile.

    Args:
        profile: The documented client facts. The only thing a citation may match.
        drafted: The prose and the declarations to check it against.
        outcome: The determination the rationale explains. Already made; not re-opened.
        recommendation: The recommendation that determination was made on.

    Returns:
        A `VerifiedRationale` when every declaration holds, otherwise an
        `UnsupportedRationale` naming every declaration that did not. Never a repaired
        rationale: verification does not edit.
    """
    declared = [citation.key for citation in drafted.cited_factors]
    declared.extend(drafted.absent_factors)
    duplicates = tuple(key for key in FactorKey if declared.count(key) > 1)
    unsupported = tuple(
        citation
        for citation in drafted.cited_factors
        if not citation_matches_profile(profile, citation)
    )
    contradicted = tuple(
        key for key in drafted.absent_factors if documented_citation(profile, key) is not None
    )
    ungrounded = tuple(
        sorted(
            _digits(drafted.prose) - _grounded_digits(profile, drafted, outcome, recommendation),
        ),
    )

    problems: list[str] = []
    if not drafted.prose.strip():
        problems.append("the rationale is empty")
    if not declared:
        problems.append(
            "the rationale declares no factor at all, so nothing anchors it to the profile",
        )
    problems.extend(_citation_problem(profile, citation) for citation in unsupported)
    problems.extend(
        f"declared {key.value} undocumented, but the profile documents it" for key in contradicted
    )
    problems.extend(f"declared {key.value} more than once" for key in duplicates)
    if ungrounded:
        problems.append(
            "used numbers the declared material does not contain: " + ", ".join(ungrounded),
        )

    if problems:
        return UnsupportedRationale(
            detail="The rationale is not supported: " + "; ".join(problems) + ".",
            unsupported_citations=unsupported,
            contradicted_absences=contradicted,
            duplicate_declarations=duplicates,
            ungrounded_numbers=ungrounded,
        )
    return VerifiedRationale(
        rationale=RecommendationRationale(
            text=drafted.prose,
            cited_factors=drafted.cited_factors,
        ),
        absent_factors=drafted.absent_factors,
        drafted_by=drafted.drafted_by,
    )
