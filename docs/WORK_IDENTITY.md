# Evidence-based work and edition proposals

`book_rag.work_identity.propose_work_links(entries, cache_dir)` is a bounded,
read-only prototype. It connects a local Gutenberg edition to Wikidata through
its **exact Gutenberg number**, then follows explicit edition/work relations.
It writes a review report and metadata caches, never SQLite, corpus manifests,
manual work IDs, or a new count of verified unique works.

## Primary-source permissions and semantics

Checked 2026-11-14 before the first larger query:

- [P2034](https://www.wikidata.org/wiki/Property:P2034) is the Project Gutenberg
  ebook identifier, with the official `/ebooks/$1` formatter. An identifier
  match binds an authority assertion to the same catalog record. It does not
  prove that the ebook contains exactly one original work.
- [P629](https://www.wikidata.org/wiki/Property:P629) means version, edition, or
  translation of another entity. It is broader than a translation assertion;
  the report retains that exact property instead of inventing a relationship.
- [Wikidata licensing](https://www.wikidata.org/wiki/Wikidata:Licensing) places
  structured entity data under CC0. That permission covers these metadata
  assertions, not book text or general documentation prose. Existing text
  rights and regional public-domain restrictions remain separate.
- The [WDQS manual](https://www.mediawiki.org/wiki/Wikidata_Query_Service/User_Manual#Query_limits)
  documents a 60-second query deadline, client processing/error budgets,
  throttling with HTTP 429 and `Retry-After`, and parallel-request limits.
  The helper makes serial requests, no automatic retries, and respects longer
  server deferrals. The [User-Agent policy](https://foundation.wikimedia.org/wiki/Policy:User-Agent_policy)
  requires an identifiable client. Our header includes `BookRAG/0.1` and its
  public repository URL; no account or paid service is used.
- The [WDQS graph split](https://www.wikidata.org/wiki/Wikidata:SPARQL_query_service/WDQS_graph_split)
  separates scholarly articles from the main graph. This prototype queries only
  the main public endpoint, `https://query.wikidata.org/sparql`. Absence there is
  not proof that an authority entity does not exist.

No public book pages or Gutenberg downloads are requested by this helper.

## Decisions and output

Inputs require a stable local ID and a strict official HTTPS Gutenberg ebook
source URL. A `pgN-*` ID must agree with `/ebooks/N`. Query values come only from
validated numbers and QIDs. Invalid providers stay unresolved without requests.
Up to 50 distinct Gutenberg IDs are accepted per invocation; duplicates share
one authority query but every original entry remains in the report.

A proposal retains `id`, `work_id`, `work_identity_status`, local author/title,
`candidate_qid` where singular, P2034 evidence, and candidate paths. Each path
contains authority labels, P31 types, P50 author assertions, and every P629 edge
with its query hash. Missing labels are null; local metadata is never presented
as an authority label. Author assertions describe the authority item and do not
replace edition contributor attribution.

| Confidence | Required evidence |
| --- | --- |
| `authority_linked` | One exact P2034 item, a single acyclic P629 path of at most three edges, and a terminal item with a recognized direct P31 work type |
| `ambiguous` | Multiple P2034 items, multiple parents, cycles, depth limit, missing metadata, unknown root type, or an edition root without a work parent |
| `unresolved` | Invalid provider identity, no exact P2034 match, or unavailable mapping query |

The recognized work types are deliberately small:
[literary work Q7725634](https://www.wikidata.org/wiki/Q7725634),
[written work Q47461344](https://www.wikidata.org/wiki/Q47461344), and
[novel Q8261](https://www.wikidata.org/wiki/Q8261).
[Edition Q3331189](https://www.wikidata.org/wiki/Q3331189) is classified separately.
Generic `book` Q571 and unknown types are not promoted by title similarity.
This under-links some valid work types and is preferable to inventing identity.
A direct work item may have a zero-edge path; P629 is never fabricated.

`authority_groups` lists common proposed roots for review and flags multiple
existing manual groups. Its action is always `review_only_no_merge`.
`identity_count_promotion_allowed` is always false. Even one authoritative exact
identifier may describe a component story inside a collection or contain an
editorial mistake. `authority_linked` is an evidence class, not verified literary
identity or an instruction to replace a curated group.

## Cache, bounds, and failure behavior

GET queries use JSON/gzip, a descriptive User-Agent, 10-second connection and
25-second operation timeouts, two-second request spacing, at most 50 metadata
nodes per batch, 300 nodes total, three P629 edges, 2 MiB per response and a
5,000-row completeness bound. Reaching a bound leaves the record uncertain.
No arbitrary SPARQL supplied by a user is executed.

`cache_dir/queries/<query-sha256>.json` stores exact query text, normalized
SPARQL JSON, a canonical JSON hash, original HTTP body SHA256, retrieval time,
endpoint, and CC0 license. The original HTTP whitespace bytes are not retained
separately; structured payload integrity is reproducible from the canonical hash.
Successful snapshots are immutable until the operator explicitly removes them.
Reruns need no network for the same query set. Corrupt or incomplete cache files
are refetched rather than silently trusted. Queries are cached by exact batch,
so a different batch can require a new query.

HTTP errors, transport failures, invalid JSON, oversized responses and HTML
error pages stop subsequent requests. `queries/deferred.json` persists a host
cooldown: the full valid `Retry-After` is respected with a 60-second minimum;
missing or invalid values defer for one hour. No sleep/retry loop challenges a
blocked endpoint. Existing successful snapshots are usable by later runs during
cooldown. Semantically invalid cached bindings are rejected, never partly used.

`proposals.json` is replaced atomically with the latest run. It includes request
and cache counts, failures, query evidence, ambiguity reasons and the original
manual identities. Runtime inputs/reports belong under ignored `data/identity`.

## Measured prototype

A three-ID permission/access probe ran before the larger experiment and returned
HTTP 200 JSON in 0.439 seconds. It found:

| Gutenberg ID | Edition item | Explicit P629 root |
| --- | --- | --- |
| 11 | Q112864267 | Q92640 (Alice) |
| 1228 | Q20968204 | Q20124 (Origin of Species) |
| 2009 | Q21948575 | Q20124 (Origin of Species) |

The Darwin result corroborates the existing manual edition grouping. It does not
create a new group or change the library's verified-work count.

The subsequent 50-edition sample used all 20 curated controls plus the first 30
distinct source IDs in the existing catalog queue. It is English-heavy (47 EN,
1 FR, 1 DE, 1 RU), and does not measure representative multilingual coverage.

| Slice | Authority linked | Ambiguous | Unresolved |
| --- | ---: | ---: | ---: |
| Curated controls, 20 | 14 | 1 | 5 |
| Additional catalog editions, 30 | 2 | 2 | 26 |
| Total, 50 | 16 | 3 | 31 |

The initial run took 6.343 seconds and four serial metadata requests with no
errors. A repeat took 0.0287 seconds, four cache hits and zero HTTP requests.
Ten linked editions had one P629 edge; six matched a directly typed work item.
The sixteen linked edition proposals have fifteen candidate roots, including
the already curated Darwin pair. These are **candidate authority roots**.
Measurements, exact input IDs, complete assertions, query hashes and snapshots:
`data/identity/wikidata-50/{input,proposals,measurements}.json` and `queries/`.

Three useful ambiguity cases demonstrate why exact identifiers cannot simply
replace work curation:

- Gutenberg 1661, *The Adventures of Sherlock Holmes*, has exact P2034 claims on
  [the collection Q392147](https://www.wikidata.org/wiki/Q392147) and individual
  stories [Q1151748](https://www.wikidata.org/wiki/Q1151748) and
  [Q265546](https://www.wikidata.org/wiki/Q265546). All are preserved as competing
  assertions. The local whole-book entry stays ambiguous.
- Gutenberg 10000 links to [Magna Carta Q12519](https://www.wikidata.org/wiki/Q12519),
  whose direct type Q727002 is outside the explicitly recognized work types.
  It stays ambiguous rather than acquiring a type from its familiar title.
- Gutenberg 10011 links to [Q19019778](https://www.wikidata.org/wiki/Q19019778),
  *365 foreign dishes*, typed as an edition without an explicit P629 work parent.
  It remains an edition candidate, not a newly verified unique work.

## Reproduction and next task

```python
import json
from pathlib import Path
from book_rag.work_identity import propose_work_links

entries = json.loads(Path("data/identity/wikidata-50/input.json").read_text())
report = propose_work_links(entries, Path("data/identity/wikidata-50"))
print(report["counts"], report["network_requests"], report["cache_hits"])
```

Offline verification:

```sh
.venv/bin/pytest -q tests/test_work_identity.py
.venv/bin/ruff check src/book_rag/work_identity.py tests/test_work_identity.py
.venv/bin/ty check src/book_rag/work_identity.py tests/test_work_identity.py
```

34 mocked tests cover exact binding, immutable manual IDs, duplicate input IDs,
shared authority/manual-group conflicts, multiple QIDs/parents, cycles, unknown
labels/types, depth/row/byte limits, provider validation, corrupt cache,
timeouts, HTML/JSON errors, HTTP 429/503 and persistent deferral.

The next useful step is a review surface for proposals and component/collection
relationships, with an explicit accepted/rejected decision and provenance.
Automatic count promotion, broad fuzzy matching, or a large endpoint crawl are
outside this prototype. Any later integration should retain manual precedence,
handle split/collected works, and add independently checked multilingual controls
before expanding authority-derived linking.
