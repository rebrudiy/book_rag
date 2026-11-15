"""Scope selection must avoid unrelated matrix reads without changing evidence."""

import numpy as np
import pytest

from book_rag.semantic import _candidate_passages, _score_vectors


class ScopedMatrix:
    def __init__(self, values: np.ndarray, allowed: set[int], maximum: int) -> None:
        self.values = values
        self.dtype = values.dtype
        self.allowed = allowed
        self.maximum = maximum
        self.visited: list[int] = []

    def __matmul__(self, vector: np.ndarray) -> np.ndarray:
        pytest.fail("A scoped request must not read the entire embedding matrix")

    def __getitem__(self, positions: np.ndarray) -> np.ndarray:
        selected = positions.tolist()
        assert len(selected) <= self.maximum
        assert set(selected) <= self.allowed
        self.visited.extend(selected)
        return self.values[positions]


@pytest.mark.parametrize("ordered", [False, True])
def test_scoped_scores_rankings_and_duplicate_windows_match_dense_mask(ordered: bool) -> None:
    rng = np.random.default_rng(92)
    values = rng.normal(size=(300, 4)).astype(np.float32)
    ids = np.repeat(np.arange(1, 101, dtype=np.int64), 3)
    if not ordered:
        permutation = rng.permutation(len(ids))
        values, ids = values[permutation], ids[permutation]
    query = np.array([0.1, 0.3, -0.2, 0.4], dtype=np.float32)
    eligible = np.array([34, 38, 37], dtype=np.int64)
    keep = np.isin(ids, eligible)
    dense = values @ query
    dense[~keep] = -np.inf
    matrix = ScopedMatrix(values, set(np.flatnonzero(keep).tolist()), maximum=5)
    actual = _score_vectors(matrix, ids, query, eligible, ordered_ids=ordered, block_rows=5)
    # OpenBLAS may choose a different reduction kernel for the final small
    # gathered block (observed one float32 ULP on Linux). Scope/masks and actual
    # candidate ordering remain exact; allow only rounding-level score drift.
    np.testing.assert_array_equal(np.isfinite(actual), np.isfinite(dense))
    np.testing.assert_allclose(actual[keep], dense[keep], rtol=2e-7, atol=2e-7)
    assert _candidate_passages(actual, ids, -1)[0] == _candidate_passages(dense, ids, -1)[0]
    assert sorted(matrix.visited) == np.flatnonzero(keep).tolist()


@pytest.mark.parametrize("eligible", [[], [999]])
def test_empty_scope_never_reads_matrix(eligible: list[int]) -> None:
    values = np.array([[1, 0], [0, 1]], dtype=np.float32)
    matrix = ScopedMatrix(values, set(), maximum=1)
    actual = _score_vectors(
        matrix,
        np.array([1, 2], dtype=np.int64),
        np.array([1, 0], dtype=np.float32),
        np.array(eligible, dtype=np.int64),
        ordered_ids=True,
    )
    assert np.isneginf(actual).all() and not matrix.visited


def test_query_result_precision_is_preserved_for_legacy_float64_queries() -> None:
    values = np.array([[0.1, 0.2], [0.3, 0.4]], dtype=np.float32)
    query = np.array([0.123456789, 0.23456789], dtype=np.float64)
    ids = np.array([1, 2], dtype=np.int64)
    actual = _score_vectors(values, ids, query, np.array([2]))
    assert actual.dtype == np.float64
    assert actual[1] == (values @ query)[1] and np.isneginf(actual[0])


def test_nonfinite_selected_scores_fail_closed_and_unfiltered_path_is_unchanged() -> None:
    values = np.array([[1, 0], [np.nan, 1]], dtype=np.float32)
    ids = np.array([1, 2], dtype=np.int64)
    query = np.array([1, 0], dtype=np.float32)
    with pytest.raises(ValueError, match="Nonfinite"):
        _score_vectors(values, ids, query)
    with pytest.raises(ValueError, match="Nonfinite"):
        _score_vectors(values, ids, query, np.array([2]))
    assert _score_vectors(values, ids, query, np.array([1]))[0] == 1
    clean = np.nan_to_num(values)
    np.testing.assert_array_equal(_score_vectors(clean, ids, query), clean @ query)
