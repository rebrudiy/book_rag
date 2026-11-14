"""Language-aware lexical query planning and conservative overlap reranking.

The original FTS/BM25 baseline lives outside this module. These pure helpers do
not access a database or infer that a retrieved passage answers a question.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# Function words only: retain negations (not/no/without, не/нет/без,
# nicht/kein/ohne, pas/non/sans) because they matter when comparing arguments.
_LANGUAGE_STOPWORDS = {
    "en": frozenset(
        """
        a an the and or but if then than as at by for from in into of on onto out
    over to up upon with about after again all also am are be been being both
    can could did do does doing down during each few further had has have having
    he her here hers herself him himself his how i im ive id ill is it its itself
    just me more most much my myself now off once only other our ours ourselves
    own same she should so some such that their theirs them themselves there
    these they this those through too under until very was we were what when
    where whether which while who whom whose why will would you your yours
    yourself yourselves don doesn didnt didn wasn weren isn arent aren t s d ll ve m
        """.split()
    ),
    "ru": frozenset(
        """
        и в во на к ко с со у о об обо от до по за из а но или же ли бы чтобы что
    это этот эта эти этого этой этих тому тем той тот та то те как так когда
    где куда откуда кто кого кому кем чем чём чего чей чья чье чьё чьи какой
    какая какие какое который которая которые которое я меня мне мной мы нас
    нам нами ты тебя тебе тобой вы вас вам вами он она оно они его её ее их
    ему ей им ею ними них себе себя свой своя свои своё свое быть был была
    было были будет будут есть ещё еще уже уж лишь только тоже также тут там
    здесь ведь вот даже очень более менее самый самая самое самые сколько
    почему потому пусть словно будто для при через между над под перед после
        """.split()
    ),
    "fr": frozenset(
        """
        du de des le la les un une et ou mais si que qui quoi dont où au aux en
    dans sur sous avec pour par à a ce cet cette ces se sa son ses leur leurs
    lui elle elles il ils on nous vous je j tu te tes toi moi mes ma mon y
    c d l qu n s t est sont était étaient été être ai as avons avez ont avait
    avoir fait font faire peut peuvent plus très bien tout toute tous toutes
    comme aussi alors donc quand quel quelle quels quelles comment pourquoi
        """.split()
    ),
    "de": frozenset(
        """
        der die das den dem des ein eine einer eines einem einen und oder aber als
    an am auf aus bei bis durch für fur gegen hinter im in ins ist mit nach
    ob über uber um unter von vom vor zu zum zur zwischen ich mich mir mein
    meine meiner meines du dich dir dein deine er ihn ihm sein seine sie ihr
    ihre ihnen es sich wir uns unser unsere euch euer eure was wer wen wem
    wie wo wann warum welcher welche welches dass daß da denn doch auch noch
    schon nur so sehr mehr meist alle aller alles diesem dieser dieses diese
    jener jene jenes man kann können konnen konnte könnte wurde würde werden
    wird war waren wäre hatte haben hat habt bin bist seid sind gewesen wäre
        """.split()
    ),
}
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_QUOTED = re.compile(r'"([^"\n]+)"|“([^”\n]+)”|«([^»\n]+)»|„([^“\n]+)“')
_MAX_TERMS = 40
_MAX_PHRASES = 8


def _query_stopwords(tokens: tuple[str, ...]) -> frozenset[str]:
    # A stopword union wrongly removes English content words such as German
    # "war", "die", "hat", and French "son". Use script and function-word
    # evidence; keep English as an explicit fallback for ambiguous short Latin
    # queries. This is a heuristic, not a general language-identification model.
    if any("\u0400" <= char <= "\u04ff" for token in tokens for char in token):
        return _LANGUAGE_STOPWORDS["ru"]
    scores = {
        language: sum(token in stopwords for token in tokens)
        for language, stopwords in _LANGUAGE_STOPWORDS.items()
        if language != "ru"
    }
    language = max(scores, key=lambda key: scores[key])
    return _LANGUAGE_STOPWORDS[language if scores[language] >= 2 else "en"]


def _fold_token(token: str) -> str:
    """Approximate FTS unicode61 Latin accent folding; retain Cyrillic accents."""
    folded: list[str] = []
    for char in unicodedata.normalize("NFKC", token).lower().replace("ς", "σ"):
        decomposition = unicodedata.normalize("NFD", char)
        if decomposition and "LATIN" in unicodedata.name(decomposition[0], ""):
            folded.extend(part for part in decomposition if not unicodedata.combining(part))
        else:
            folded.append(char)
    return "".join(folded)


def tokenize(text: str) -> tuple[str, ...]:
    """Split text into normalized Unicode word tokens, like FTS word matching."""
    return tuple(_fold_token(match.group()) for match in _TOKEN.finditer(unicodedata.normalize("NFKC", text)))


@dataclass(frozen=True, slots=True)
class QueryPlan:
    """An FTS candidate expression and the original query's lexical constraints.

    `terms` contains distinct unquoted meaningful tokens. Each `phrases` entry
    retains all tokens, including function words. Phrases are mandatory and use
    FTS token adjacency, which ignores punctuation and case.
    """

    original: str
    terms: tuple[str, ...]
    phrases: tuple[tuple[str, ...], ...]
    expression: str
    reason: str | None = None


def prepare_query(query: str) -> QueryPlan:
    """Build safe FTS syntax from natural language, without exposing operators.

    Up to eight quoted phrases and forty distinct meaningful unquoted terms are
    supported. Empty/function-word-only queries produce an empty expression.
    A query containing only a negation remains a valid literal word search.
    """
    if len(query) > 4000:
        raise ValueError("Lexical query exceeds 4000 characters")
    phrases: list[tuple[str, ...]] = []
    outside: list[str] = []
    cursor = 0
    for match in _QUOTED.finditer(query):
        outside.append(query[cursor : match.start()])
        phrase = next(group for group in match.groups() if group is not None)
        tokens = tokenize(phrase)
        if tokens:
            phrases.append(tokens)
        cursor = match.end()
    outside.append(query[cursor:])
    unquoted_tokens = tokenize(" ".join(outside))
    stopwords = _query_stopwords(unquoted_tokens)
    terms = tuple(dict.fromkeys(term for term in unquoted_tokens if term not in stopwords))[:_MAX_TERMS]
    if len(phrases) > _MAX_PHRASES:
        raise ValueError("At most eight quoted phrases are supported")
    if phrases:
        expression = " AND ".join('"' + " ".join(phrase) + '"' for phrase in phrases)
    else:
        expression = " OR ".join('"' + term + '"' for term in terms)
    return QueryPlan(query, terms, tuple(phrases), expression, None if expression else "no_meaningful_terms")


def _contains_phrase(tokens: tuple[str, ...], phrase: tuple[str, ...]) -> bool:
    return any(tokens[offset : offset + len(phrase)] == phrase for offset in range(len(tokens) - len(phrase) + 1))


def _minimum_matches(term_count: int, strict: bool = False) -> int:
    if not term_count:
        return 0
    if term_count == 1:
        return 1
    if strict:
        return max(2, math.ceil(term_count * 0.6))
    if term_count == 2:
        return 1
    return max(2, math.ceil(term_count * 0.35))


def _evidence(plan: QueryPlan, result: Mapping[str, Any]) -> tuple[list[str], list[str], bool]:
    text_tokens = tokenize(str(result.get("text", "")))
    # Titles/authors help identify a book; source text separately controls
    # whether the result has enough support for a research evidence board.
    metadata_tokens = tokenize(
        " ".join(str(result.get(key, "")) for key in ("title", "author", "section_author", "section_title"))
    )
    text_set = set(text_tokens)
    all_tokens = text_set | set(metadata_tokens)
    matched = [term for term in plan.terms if term in all_tokens]
    matched_text = [term for term in plan.terms if term in text_set]
    phrases_present = all(_contains_phrase(text_tokens, phrase) for phrase in plan.phrases)
    return matched, matched_text, phrases_present


def has_research_support(plan: QueryPlan, result: Mapping[str, Any]) -> bool:
    """Require source-text overlap for evidence selection, never entailment.

    A bibliographic title match alone is useful for finding books but does not
    support an answer. This threshold does not detect sarcasm, contradiction,
    missing dates, or whether a passage actually answers a question.
    """
    if not plan.expression:
        return False
    _, matched_text, phrases_present = _evidence(plan, result)
    return phrases_present and len(matched_text) >= _minimum_matches(len(plan.terms), strict=True)


def rerank_results(
    plan: QueryPlan, results: Sequence[Mapping[str, Any]], limit: int = 10, strict: bool = False
) -> list[dict[str, Any]]:
    """Filter weak overlaps and rank by meaningful term coverage, stably.

    `score` is a descending lexical coverage score, not a probability or a claim
    that the passage supports an answer. Preserve the input score as
    `baseline_score`. Request a larger candidate pool before reranking so that
    rejected weak matches do not hide a useful result below the original limit.
    """
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if not plan.expression:
        return []
    ranked: list[dict[str, Any]] = []
    minimum = _minimum_matches(len(plan.terms), strict)
    for result in results:
        matched, matched_text, phrases_present = _evidence(plan, result)
        if not phrases_present or len(matched) < minimum:
            continue
        source_support = len(matched_text) >= _minimum_matches(len(plan.terms), strict=True)
        if strict and not source_support:
            continue
        coverage = len(matched) / len(plan.terms) if plan.terms else 1.0
        source_coverage = len(matched_text) / len(plan.terms) if plan.terms else 1.0
        score = coverage + 0.2 * source_coverage
        ranked.append(
            dict(result)
            | {
                "baseline_score": result.get("score"),
                "score": score,
                "matched_terms": matched,
                "term_coverage": round(coverage, 4),
                "research_supported": source_support,
            }
        )
    ranked.sort(key=lambda result: -result["score"])
    return ranked[:limit]
