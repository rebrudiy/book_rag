"""General lexical query behavior, independent of the demo/held-out corpus."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from book_rag.retrieval import has_research_support, prepare_query, rerank_results, tokenize


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("What is the purpose of public education?", ("purpose", "public", "education")),
        ("Как и почему работают тепловые насосы?", ("работают", "тепловые", "насосы")),
        ("Pourquoi les oiseaux migrent-ils en hiver?", ("oiseaux", "migrent", "hiver")),
        ("Warum fliegen die Vögel im Winter?", ("fliegen", "vogel", "winter")),
    ],
)
def test_multilingual_function_words(query: str, expected: tuple[str, ...]) -> None:
    assert prepare_query(query).terms == expected


@pytest.mark.parametrize("query", ["the and of", "и в на", "le et de", "der und die", "... * () :", ""])
def test_no_meaningful_terms(query: str) -> None:
    plan = prepare_query(query)
    assert plan.reason == "no_meaningful_terms"
    assert not plan.expression
    assert not rerank_results(plan, [{"text": "Some unrelated material"}])


@pytest.mark.parametrize(
    "query", ['"to be or not to be"', "“to be or not to be”", "«to be or not to be»", "„to be or not to be“"]
)
def test_quoted_phrase_preserves_function_words(query: str) -> None:
    plan = prepare_query(query)
    assert plan.terms == ()
    assert plan.expression == '"to be or not to be"'
    assert has_research_support(plan, {"text": "To be, or not to be: that is the question."})
    assert not has_research_support(plan, {"text": "To be sure, or not to be sure."})


def test_unquoted_terms_with_quotes_are_optional_candidates_but_rank_constraints() -> None:
    plan = prepare_query('"solar wind" charged particles')
    assert plan.expression == '"solar wind"'
    assert plan.terms == ("charged", "particles")
    ranked = rerank_results(
        plan,
        [
            {"id": 1, "text": "A wind farm has charged particles."},
            {"id": 2, "text": "Solar wind is a stream of charged particles."},
            {"id": 3, "text": "Solar wind."},
        ],
    )
    assert [item["id"] for item in ranked] == [2]


def test_negations_remain_meaningful() -> None:
    for query, negation in [
        ("not rational", "not"),
        ("не рационально", "не"),
        ("nicht rational", "nicht"),
        ("pas rationnel", "pas"),
    ]:
        assert negation in prepare_query(query).terms


def test_case_accent_and_unicode_equivalence() -> None:
    assert tokenize("CAFÉ café Cafe\u0301") == ("cafe", "cafe", "cafe")
    assert tokenize("ёлка йод") == ("ёлка", "йод")
    assert tokenize("Straße großen") == ("straße", "großen")
    assert prepare_query("教育 科学").terms == ("教育", "科学")
    assert prepare_query("０１２３ science").terms == ("0123", "science")


def test_fts_expression_cannot_inject_operators() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE documents USING fts5(text)")
    conn.execute("INSERT INTO documents VALUES ('solar wind carries plasma')")
    for query in ['solar OR NOT * ) ":', '"solar wind" OR NEAR(plasma)', 'solar"; DROP TABLE documents; --']:
        plan = prepare_query(query)
        conn.execute("SELECT rowid FROM documents WHERE documents MATCH ?", (plan.expression,)).fetchall()
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1


def test_rejects_weak_overlap_and_prioritizes_coverage() -> None:
    plan = prepare_query("ocean currents transport tropical heat")
    results = [
        {"id": 1, "text": "The heat of a desert", "score": -100},
        {"id": 2, "text": "Ocean currents transport tropical heat.", "score": -1},
        {"id": 3, "text": "Ocean currents affect weather.", "score": -10},
    ]
    ranked = rerank_results(plan, results)
    assert [item["id"] for item in ranked] == [2, 3]
    assert ranked[0]["baseline_score"] == -1
    assert ranked[0]["term_coverage"] == 1
    assert ranked[0]["research_supported"]
    assert not ranked[1]["research_supported"]
    assert results[0]["score"] == -100


def test_book_metadata_match_does_not_support_an_answer() -> None:
    plan = prepare_query("ocean currents")
    result: dict[str, Any] = {"title": "Ocean Currents", "text": "A preface thanking colleagues."}
    assert rerank_results(plan, [result])
    assert not has_research_support(plan, result)
    assert not rerank_results(plan, [result], strict=True)


def test_strict_evidence_threshold_rejects_a_single_keyword() -> None:
    plan = prepare_query("ocean currents")
    result = {"text": "The ocean is vast."}
    assert rerank_results(plan, [result])
    assert not has_research_support(plan, result)
    assert not rerank_results(plan, [result], strict=True)


def test_phrase_constraint_is_never_satisfied_by_metadata() -> None:
    plan = prepare_query('"ocean currents"')
    result = {"title": "Ocean Currents", "text": "Different words."}
    assert not rerank_results(plan, [result])


def test_long_input_is_bounded_and_ranking_is_stable() -> None:
    with pytest.raises(ValueError):
        prepare_query("x" * 4001)
    with pytest.raises(ValueError):
        prepare_query('"ocean" ' * 9)
    assert len(prepare_query(" ".join(f"term{index}" for index in range(100))).terms) == 40
    plan = prepare_query("plasma")
    assert [
        item["id"] for item in rerank_results(plan, [{"id": 2, "text": "plasma"}, {"id": 1, "text": "plasma"}])
    ] == [2, 1]


def test_german_sharp_s_is_not_expanded_in_fts_queries() -> None:
    # SQLite unicode61 does not expand ß into ss. A Python casefold here would
    # silently prevent otherwise exact German words from finding candidates.
    conn = sqlite3.connect(":memory:")
    conn.execute('CREATE VIRTUAL TABLE documents USING fts5(text, tokenize="unicode61 remove_diacritics 2")')
    conn.execute("INSERT INTO documents VALUES ('Eine große Straße mit weißen Häusern')")
    plan = prepare_query("große Straße")
    rows = conn.execute("SELECT text FROM documents WHERE documents MATCH ?", (plan.expression,)).fetchall()
    assert rows
    assert rerank_results(plan, [{"text": rows[0][0]}])


@pytest.mark.parametrize(
    ("query", "term"),
    [
        ("Why do flowers die in winter?", "die"),
        ("The hat on the table", "hat"),
        ("A son and his father", "son"),
        ("War and peace", "war"),
        ("A man and a child", "man"),
    ],
)
def test_function_words_of_other_languages_do_not_erase_english_content(query: str, term: str) -> None:
    assert term in prepare_query(query).terms


def test_equal_coverage_preserves_bm25_candidate_order() -> None:
    # The caller has already ranked by term rarity/frequency. Do not reverse
    # equal-coverage passages on the basis of a tiny uncalibrated span bonus.
    plan = prepare_query("ocean currents heat")
    rows = [
        {"id": 1, "text": "Ocean warm warm currents", "score": -4},
        {"id": 2, "text": "Ocean currents", "score": -1},
    ]
    assert [row["id"] for row in rerank_results(plan, rows)] == [1, 2]
