# Agent review of reference misses and actual source support

The review found both incomplete reference evidence and real retrieval failures. Five of the nine original MiniLM top-five reference misses already contain sufficient source support under the wording of their queries. Other results need adjacent context, retrieve the wrong relationship or narrative moment, supply only one side of a comparison, or confuse editorial commentary with an author's position. These are **agent-reviewed judgments**, not human ratings, independent expert adjudication, or replacement retrieval scores.

`experiments/relevance-review-v1.json` contains the complete case and passage reviews, source citations and exact proposed reference anchors. No labels, score files, algorithms, manifest or snapshot were changed. No historical held-out file or newly generated fixed-pool comparison report was used.

## Scope and method

The selection includes all nine answerable cases in the original `expanded-hybrid-v1.json` whose top five contain a declared relevant edition but no required reference phrase. It also includes all 19 reference-missing reranker top-one results on answerable queries, rather than only the six with logits at least three. The union is 20 queries and 58 distinct returned passages. Raw reranker scores come from `calibration-reranker-v1.json`; every reviewed top-one passage ID was checked against the original `expanded-reranked-v1.json` and matched.

The actual returned text and bounded same-section neighboring context were read from the frozen 20-edition snapshot. Text/offset/hash integrity was checked independently. The database digest remains `ea1feab7c1d1c58acd7457529ef70885f2767ceeab6050ec12db0fa4f74bb4b8`, and all original input digests are recorded in the review JSON.

A correctly named book is not enough. A quoted query requires its literal phrase; an explanation needs an actual mechanism or rationale; a question about a particular letter must preserve author, recipient and relationship. Editorial and character voices cannot be replaced by the catalog author. A source that supports one author's side does not complete a multi-author comparison.

The classifications are `sufficient_support`, `partial_related`, `irrelevant` and `ambiguous`. Passage-alone support is distinguished from support supplied by a separately cited neighboring span. The review did not search arbitrarily far through a book until any useful text appeared.

| Selected outputs | Sufficient | Partial | Irrelevant |
| --- | ---: | ---: | ---: |
| Nine MiniLM top-five sets | 5 | 4 | 0 |
| Nineteen reranker top-one excerpts | 7 | 9 | 3 |

Of the seven sufficient reranker excerpts, six support the question in the declared edition and one supplies an exact literal quote in another edition of the same work. The latter needs an explicit query-scope decision. Across 58 individual passages, judgments are 11 sufficient, 32 partial, 14 irrelevant and one ambiguous. These are selected-case review counts, not model accuracy estimates; no benchmark was rescored.

## Direct support missed by one reference phrase

The following examples supply explicit evidence, rather than merely plausible associations. The JSON records exact alternatives and citations; each proposed anchor was checked in its actual source passage.

| Query | Actual source support | Reference limitation |
| --- | --- | --- |
| `expanded-10`, snake/rope/ventilator story | Doyle, [Sherlock Holmes](https://www.gutenberg.org/ebooks/1661), Speckled Band: passage 222264 explains the rope as a snake bridge; 222265 explains its route through the ventilator to the bed. | The frozen anchor names the snake species later; it is not the only passage identifying the remembered mechanism. |
| `expanded-cross-04`, why common people need public education | Smith, [Wealth of Nations](https://www.gutenberg.org/ebooks/3300), public institutions: 220635 explains limited parental means, early work, exhausting simple trades and public basic education. | A preceding general statement was the sole labeled reference. This passage provides the rationale more directly. |
| `expanded-cross-05`, why the creature wants a companion | Shelley, [Frankenstein](https://www.gutenberg.org/ebooks/84), chapter 17: 222644 gives the creature's request, isolation, need for sympathy and proposed harmless life together. | The initial request sentence is not the only acceptable evidence. The reranker's different top-one excerpt is still inadequate. |
| `expanded-cross-12`, using matches against Morlocks | Wells, [The Time Machine](https://www.gutenberg.org/ebooks/35), IX: 229221 describes lighting a match and retreating under its protective glare. | The next paragraph's second defensive light was the sole anchor, excluding this enacted escape. |
| `expanded-role-01`, Jowett's comparison of dialogues | Jowett's editorial introduction to [The Republic](https://www.gutenberg.org/ebooks/1497): 226954 explicitly compares Republic/Laws; 226958 positions Statesman relative to both. | The opening length/ranking comparison is far from exhaustive evidence for this broad question. Correct Jowett attribution must be retained. |
| `expanded-cross-03`, disuse and inheritance | Darwin, [1859 Origin](https://www.gutenberg.org/ebooks/1228), XIII: 218533 explains inherited reduction at corresponding ages and rudimentary organs. | It answers the mechanism with different wording from the single earlier anchor. |
| `expanded-cross-10`, lightning and moving observers | Einstein, [Relativity](https://www.gutenberg.org/ebooks/5001), IX: 228957 explains moving toward one beam and away from the other, receiving B earlier. | The frozen anchor is the preceding question, while the model returned the actual explanation. |
| `expanded-cross-11`, over-production crises | Marx and Engels, [Manifesto](https://www.gutenberg.org/ebooks/61), I: 229082 describes recurring crises and destruction of products/productive forces, explicitly identifying over-production. | The labeled next clause is not necessary for answering how the text describes the crises. |

These are proposals for a future explicitly versioned relevance set, not silent fixes to current scores. Most are additional reference passages; some questions may need a passage-relevance judgment set instead of one required substring.

## Literal phrase, edition scope and attribution

For `expanded-03`, reranker passage 221898 contains the requested literal phrase exactly in Darwin's [sixth edition](https://www.gutenberg.org/ebooks/2009), chapter XV. The visible query does not restrict the edition, but the frozen relevance target accepts only the 1859 edition. This is sufficient for an unscoped literal lookup with a truthful sixth-edition citation. It is **not** evidence for the 1859 wording, and adjacent wording differs between editions. A future annotation decision should either state the edition constraint in the query or explicitly accept the linked work's applicable editions.

Jowett's reviewed excerpts have an explicit editorial contributor label. Passage 226956 additionally quotes Aristotle inside that introduction; a summary must preserve this nested source credit. By contrast, Meditations passages 218631 and 218627 are later commentary still unclassified in the frozen snapshot. They cannot establish Marcus's own position simply because the ebook catalog author is Marcus. The former discusses a voyage/death metaphor rather than the three-act play; the latter compares Meditations with The Imitation of Christ, not Nietzsche.

Darwin passage 221920 is a glossary definition block, including retina. The glossary elsewhere credits Dallas. It does not explain evolution of a complex eye or compare editions, so neither matching the topic “eye” nor the book author makes it supporting evidence.

## Three bounded context opportunities

The review found three concrete cases where an immediately adjacent passage supplies missing support:

- `expanded-18`: returned Einstein passage 228940 concerns time coordinates in the stone example. Previous passage 228939 gives the body-relative straight-line/parabola explanation.
- `expanded-13`: returned Tolstoy passage 224347 locates wounded Andrew on Pratzen Heights and reprises the lofty sky. Next passage 224348 explicitly contrasts Napoleon's insignificance with the infinite sky, completing the changed valuation. The returned scene-location evidence is useful, but its full explanatory premise needs that continuation.
- `expanded-cross-06`: returned Melville passage 223672 gives the missing-boy backstory. Next passage 223673 contains Ahab's refusal and stated urgency; that source is already in the frozen labels.

This supports offering adjacent evidence with its own source offsets and citation. The product must not merge unseen neighboring content into the allegedly scored quote or claim it was already present in the returned excerpt. A fixed number of same-section neighbor passages gives a reviewable boundary; opening an arbitrary whole chapter does not by itself establish support.

## Real failures remain

The French Darcy query retrieves several different letters and inheritance episodes. Reranker passage 217855 is Lydia's request for money/employment after Elizabeth's marriage, not Darcy revealing Wickham's pursuit of Georgiana's fortune. Its high logit does not repair that relation error. Other top-five passages concern Jane/Bingley, Miss King, Lady Catherine's daughter, or Mrs Gardiner's account of Lydia.

For the companion question, reranker 222760 supplies Victor's retrospective justification for refusing, not the creature's reason for asking. This reverses the relevant speaker and decision. The same original MiniLM candidate set contains a genuinely sufficient creature speech, so both label incompleteness and ranking error coexist.

For the Morlock defense question, reranker 229140 demonstrates the time-machine model's levers, not light used against Morlocks. For the coffin-survival question, 223685 merely foreshadows the coffin life-buoy before the Pequod sinks; it does not describe the surviving narrator's flotation. Neither result should receive a new acceptable anchor.

Cave passage 227331 mainly applies education to rulers' obligation to return to the prisoners. It is related to the allegory but does not itself explain the initial shadows-to-knowledge transition. Nearby interpretation can be useful context, but no complete-answer alternative is proposed solely from the political application.

## Comparisons need separate source coverage

Smith passage 219233 directly says lifelong simple operations improve dexterity. The Smith/Marx question is broadly worded about changes to workers, so this is a valid Smith-side component, even though the frozen Smith anchor concerns intellectual harm. The original reviewed top-five set contains no Marx source; it therefore cannot complete the comparison. Broadening the Smith references must not hide the missing second author.

Hobbes passage 227735 gives an explicit argument that covenants need coercive common power and fear. It is a valid alternative Hobbes-side component for the Machiavelli/Hobbes question. A single Hobbes excerpt is still not a complete comparison.

The Marcus/Nietzsche case is worse: the original set lacks Marcus and mostly contains Nietzsche passages about other subjects. One passage discusses apparent political opponents sharing values, but treating this as advice about becoming like opponents requires interpretation; it is marked ambiguous and receives no new reference anchor. The reranker instead selects unrelated editorial comparison with The Imitation of Christ.

## Implications and limits

The next useful work has three distinct targets: collect additional independently adjudicated evidence for genuinely supported answers; expose precisely cited adjacent context; and improve relation/speaker/location/multi-source retrieval where support is actually absent. Source-role controls are also necessary for author research. Raising a universal cosine or logit floor cannot resolve these distinctions.

The original reported scores remain untouched. In particular, this audit does not declare MiniLM or E5 the winner, and it does not convert provisional agent judgments into new official precision or recall. Any future benchmark revision should freeze query intent first, pool candidate evidence across methods without favoring one model, preserve disagreements and source roles, and use a newly untouched final-test set for later generalization claims.
