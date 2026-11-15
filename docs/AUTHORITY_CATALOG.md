# Compact authority metadata and review proposals

`book_rag.authority_catalog` matches exact public Gutenberg ebook identifiers
against a compact Wikidata snapshot. It produces proposals, not database updates
or a count of confirmed unique literary works. Local titles, authors, artifact
hashes, existing work IDs and manual curation remain unchanged.

## Primary permissions and interpretation

Checked 2026-11-15:

- [P2034](https://www.wikidata.org/wiki/Property:P2034) identifies a Project
  Gutenberg ebook catalog record. It does not guarantee that the file contains
  one original work. Several entities can assert the same identifier.
- [P629](https://www.wikidata.org/wiki/Property:P629) relates a version, edition,
  or translation to another item. A path alone does not establish that an
  edition is specifically a translation, or that every anthology component is
  equivalent to its entire container.
- [Wikidata licensing](https://www.wikidata.org/wiki/Wikidata:Licensing) permits
  reuse of structured entity assertions under CC0. This permission does not
  change the separate licenses or regional copyright status of book texts.
- The [WDQS manual](https://www.mediawiki.org/wiki/Wikidata_Query_Service/User_Manual#Query_limits)
  describes query deadlines, processing/error budgets and HTTP429 deferrals.
  [Client identification](https://foundation.wikimedia.org/wiki/Policy:User-Agent_policy)
  is provided by our descriptive project User-Agent. The helper uses serial
  GET requests with gzip, two-second spacing, strict byte/time bounds and no
  automatic retries or endpoint switching.
- The [current backend migration notice](https://www.wikidata.org/wiki/Wikidata:SPARQL_query_service/WDQS_backend_update)
  describes a changing WDQS backend. This implementation uses standard SPARQL
  on the main public endpoint; it does not assume Blazegraph-specific hints or
  use a new endpoint to bypass a timeout/defer.

The identifier query reads both normal and preferred non-deprecated statements,
including statement GUIDs and ranks. A truthy-property query could hide normal
assertions when preferred ones exist. Competing objects are retained. A separate
COUNT query must agree with the complete, unbounded-by-LIMIT compact TSV before
the mapping can support proposals. Invalid rows are explicit rather than guessed.

## Run and reproduce

Default execution is offline. It validates raw cached response hashes and keeps
original retrieval times. A cache miss yields unresolved records without a
network call or a new provider deferral:

```sh
.venv/bin/python scripts/propose_authority_catalog.py \
  --input-editions data/identity/wikidata-catalog-v1/input-editions.json \
  --frontmatter 20
```

Without `--input-editions`, the script makes a fresh streaming selection from the
existing official RDF archive and read-only indexed public PG rows. Reusing a
selection verifies its RDF snapshot SHA and each current indexed source URL,
artifact hash, title, author, language, work ID, coverage and curation flag. It
never opens notes or personal-import records. Both selection modes open SQLite
with `mode=ro`; no production or frozen database is written.

`--fetch-metadata` explicitly allows the compact count/map requests. Adding
`--graph-pilot` enables up to40 deterministically selected exact-ID seed entities
and at most two further requests for bounded work graphs/author labels. The
initial global map takes two requests rather than one request per local edition.
Global snapshot responses are capped at6MiB, requests at4 per client, client wall time
at120s and operation timeouts at25s. The graph pilot separately caps metadata at
2MiB plus1MiB for author labels (9MiB combined total) and follows at most three P629 edges. It does not
fetch full Wikidata dumps or book text.

Failures persist a shared cooldown at
`data/identity/wikidata-access-state.json`, together with query/time/error evidence
in the cache. The existing50/200-edition provider deferrals are also checked.
Changing a destination cannot bypass a live cooldown. Valid cached responses
remain usable offline. Respect the recorded retry time before requesting more
metadata; do not repeatedly run the fetch command while blocked.

The runtime directory contains exact query text, raw JSON/TSV bytes, SHA256,
endpoint, CC0 links and actual receipt times. `authority-proposals.json` records
every input edition, exact P2034 assertions and ambiguity reasons. Work graphs
preserve statement ranks and GUIDs. Cycles, multiple parents, unknown direct work
types, edition roots without parents, depth limits, collections and components
remain ambiguous. Only a small explicit direct-P31 work-type set is recognized;
no fuzzy title match promotes identity. `authority_linked` remains a review class
with `source_scope_verified=false`, never permission to merge or change counts.

## Measured snapshot and actual blocker

The 2026-11-15 snapshot acquired 4,125 non-deprecated P2034 statements covering
4,072 distinct PG identifiers;47 identifiers had competing authority items.
The count and TSV agreed. Two successful requests retained334,129 decoded response
bytes. Raw caches and the original capture report are under
`data/identity/wikidata-catalog-v1/` (ignored runtime metadata).

Of10,984 indexed public PG editions verified against our existing official RDF
snapshot,555 have an exact identifier claim (5.05%). Seven of those have competing
items. The remaining10,429 lack an exact claim in this observed snapshot; this
means unresolved, not proof that their work has no Wikidata entity. This measured
coverage makes automatic authority resolution of all11,001 editions unrealistic.

The40-seed graph pilot made one actual request and timed out after25 seconds.
It stopped with no retries or endpoint changes and persisted a cooldown until
**2026-11-15T03:42:58.294058+00:00**. No graph result was accepted. Thus the current
proposal report has555 ambiguous and10,429 unresolved editions, zero promoted
work roots. The raw map remains valid and an offline full-catalog rerun completed
with zero HTTP requests. The earlier50-edition cached prototype reported16
linked/3ambiguous/31unresolved; those historical results are separate evidence,
not silently reclassified under this stricter statement-rank/collection audit.

Twenty bounded local-source previews passed artifact SHA verification. Their
OPF titles exactly match local titles and catalog author surnames occur in the
source metadata/previews. This checks source binding, not semantic identity or
complete-work scope. All seven competing-ID examples were included. In particular,
PG108 and PG1661 contain several Sherlock Holmes stories; PG6645 is an explicitly
selected-writings collection. Those containers must not become single original
story identities merely because a P2034 claim points at an individual component.
Runtime previews are retained for manual review without distributing their text
in the repository. The small public measurement is
`experiments/authority-catalog-v1.json`.

## Next decision

After cooldown, first simplify the work-graph query to a bounded flat statement
batch or use already cached item metadata, measuring completeness before any
wider expansion. Retain all competing P2034 statements and verify actual edition
scope against source frontmatter and contents before proposing a curated link.
Keep graph proposals separate from protected retrieval controls, semantic receipts
and published counts. A local content-overlap audit can proceed while the public
provider is deferred.
