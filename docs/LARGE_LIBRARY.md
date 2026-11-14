# Local large-library MVP

Measured on 14 November 2026. These are runtime results from the maintained
development machine, not a corpus bundled with a fresh clone.

The full interface is **http://127.0.0.1:8001**. It contains 3,991 searchable
editions and 1,401,120 passages. Languages: English 1,940, French 1,031,
German 1,009 and Russian 11. These are editions, not a verified unique-work total:
the catalogue has 60 periodical editions and mostly provisional grouping.
The manually confirmed full-work lower bound is 24.

The home page's language shelves show real available titles. Russian originals
include Dostoevsky's *Записки из подполья*, Turgenev's *Отцы и дети*, Tolstoy's
*Смерть Ивана Ильича*, Chekhov's *Палата №6* and Gogol's *Шинель*. English,
French and German shelves include, when present in the actual catalogue,
Carroll, Austen, Darwin, Plato, Voltaire, Flaubert, Kafka and others. A shelf is
a small entry point; **Все книги на этом языке** opens the complete paginated
catalogue for that language.

## Semantic availability

A complete pinned MiniLM index is installed over **1,308 editions**,
445,466 passages and 1,865,985 encoder windows. Every window/input/ID mapping
was verified before activation. All 3,928 duplicate-input vector rows were
bitwise equal to their verified first rows; immutable files could be hardlinked
without another matrix or new encoding. The frozen source database SHA remained
`2d77675ba2a68f63e25f507355272e3206db8f9abbe959f326cea96f774939f0`.

Select **По смыслу** or **Гибридный**. The interface shows the exact edition
coverage. Hybrid also searches words across all 3,991 editions. An uncovered
selected book/language uses explicit lexical fallback. **По словам** works
without any model. [Installation, checks and invalidation](INDEX_INSTALLATION.md)

Three exploratory scenarios that were actually checked:

1. Search **По смыслу** across the library for
   `Девочка следует за белым кроликом и падает в его нору`.
   The first result belongs to *Alice's Adventures in Wonderland* in English.
2. Choose Kafka's *Die Verwandlung* using **Искать в книге**, then search
   **По смыслу** for
   `Человек просыпается и обнаруживает, что превратился в насекомое`.
   The first result is the German opening about Gregor Samsa.
3. Choose *Смерть Ивана Ильича* and search **По смыслу** for
   `Человек перед смертью думает, что прожил жизнь неправильно`.
   The first result is from chapter IX, reflecting on his earlier life.

Open a result to inspect its exact chapter/highlight, edition and source links.
Full digital editions are stored locally where the source provides them;
search returns passages rather than placing an entire book into each query.
Explicit excerpt/partial imports remain labeled as such. A captured generated
FB2 is a conversion of its credited source, not a scan of the printed original.

**Known failure:** the same Kafka description across the whole library did
not return Kafka in its first five results. Scoped success does not establish
global approximate-description quality. The first cold query took 15.03s;
the exploratory warm Alice/Kafka/Tolstoy queries took 3.14s/0.47s/0.52s.
These different queries are not an isolated latency benchmark. Independent
checks confirmed all 20 returned passages, metadata and reader links across
the four probes. [Exact availability receipt and failed case](../experiments/semantic1308-availability-v1.json)

## Reproduce evaluation

On a stable local library with the completed subset already installed:

```sh
BOOKRAG_MODEL_CACHE=./data/models uv run python scripts/evaluate_semantic_subset.py \
  data/full --model-cache data/models --output experiments/large-subset-dev-v1
```

This reuses the 54 development cases over their known targets with additional
distractors, including ten explicitly book-scoped negatives. Alternate relevant
editions can be unlabeled. It runs baseline, enhanced lexical and hybrid against
one owned SQLite copy, guards the unchanged original and stores one shared
metadata snapshot. It requires copy space plus a four-GiB reserve, accepts only
development fixtures and never checkpoints the original database. Final held-out
labels remain separate. Sequential RSS/cache measurements are not isolated
mode-to-mode resource comparisons. No general relevance score is claimed here
before those runs complete.

Restart the existing full library with:

```sh
BOOKRAG_DATA_DIR=./data/full BOOKRAG_MODEL_CACHE=./data/models \
  uv run bookrag serve --port 8001
```

A fresh clone starts with the licensed demo and acquires its own corpus using
the [source instructions](SOURCES.md);
runtime texts, models, indexes and private notes are not committed.
