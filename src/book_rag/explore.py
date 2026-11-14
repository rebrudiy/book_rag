"""Evidence-based lexical exploration, with explicit limits and bounded memory.

The map describes vocabulary overlap. It makes no claim about agreement between
books, semantic similarity or a term's meaning. Features are language-separated.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import unicodedata
from collections import Counter, OrderedDict, defaultdict
from copy import deepcopy
from itertools import combinations
from threading import Lock
from typing import Any

from book_rag.library import Library

_STOPWORDS_RAW = {
    "en": """
    a about above across after again against all almost alone along already also although always am among an
    and another any anybody anyone anything are around as ask asked at away back be became because become
    been before being below between both but by call called came can cannot cant chapter come comes could
    couldnt day days did didnt do does doesnt doing done dont down during each either else end enough even
    ever every everybody everyone everything far few first for form found four from full further gave get
    gets give given go goes going gone good got great had hand has have having he heard her here hers
    herself him himself his how however hundred i if in indeed into is isnt it its itself just keep kind
    know known last later least less let like little long look looked made make makes making man many may me
    men might more most mr mrs much must my myself name near necessary need never new next no nobody none
    nor not nothing now of off often old on once one only or other others ought our ours ourselves out over
    own part perhaps place put quite rather said same saw say says second see seem seemed seems seen shall
    she should side since small so some somebody someone something sometimes soon still such take taken than
    that the their theirs them themselves then there therefore these they thing things think third this
    those though thought three through thus till time times to together too took toward two under until up
    upon us use used very was wasnt way we well went were what whatever when where whether which while who
    whom whose why will with within without woman women would wouldnt year years yes yet you your yours
    yourself yourselves thou thee thy thine hath dost doth art ye tis amongst whilst wherein thereof thereby
    hence whomsoever gutenberg ebook ebooks copyright illustration illustrations volume book text produced
    production transcriber transcribers et al pp page pages contents chapter chapters section sections
    edition edited illustrations transcription
    """,
    "ru": """
    а без более больше будет будто бы был была были было быть в вам вами вас весь ведь во вот все всего всех
    всё всю вы где да даже два для до его ее её если есть еще ещё же за зачем здесь и из или им ими их к как
    какая какие какой кам кому когда кого которое которые который кто ли либо лишь мне мной мог может можно
    мои мой мы на над надо нас наш не него нее неё нет ни нибудь них но ну о об один одна одно они оно он
    она опять от перед по под после потому почему при про раз сам себя себе сегодня сейчас сказал сказать
    слова слово со совсем так такая такие такой там те тем теперь то того тоже той только том тот тут ты у
    уже уж через чего чем что чтобы эта эти это этот я ибо сей сего сие ко егою ей негою ними былъ глава
    главы книга книги том страница страницы издание автор примечание примечания
    """,
    "fr": """
    a ai ainsi allait aller alors assez au aucun aucune aussi autre autres aux avec avait avaient avoir
    avons ayant beaucoup bien bon bonne car ce ceci cela celle celles celui ces cet cette ceux chaque chez
    comme comment contre dans de des depuis deux dit doit donc dont du elle elles en encore entre est et eu
    eux fait faire faut fut grand grande ici il ils jamais je jusqu juste la le les leur leurs lui là mais
    me mes moi moins mon même mêmes ne nos notre nous on ont ou où par pas peu peut plus point pour pourquoi
    quand que quel quelle quelles quels qui sa sans se ses si soi soit son sont sous sur ta te tel telle tes
    toi ton tous tout toute toutes très tu un une vers vos votre vous y était étaient été être chapitre
    chapitres livre livres tome édition page pages monsieur madame maintenant puis quelque quelques rien
    toujours
    """,
    "de": """
    aber alle allem allen aller alles als also am an ander andere anderen anderer anderes auch auf aus bei
    beim bin bis bist da dabei dadurch dafür dagegen daher damit dann das dass dasselbe dazu dem den denn
    der des deshalb dessen die dies diese diesem diesen dieser dieses doch dort du durch ein eine einem
    einen einer eines einmal einige einigen einig er es etwas euch euer eure für gegen gewesen gibt habe
    haben hat hatte hatten hier hin hinter ich ihm ihn ihnen ihr ihre ihrem ihren ihrer ihres im immer in
    indem ins ist ja jede jedem jeden jeder jedes jene jenem jenen jener jenes jetzt kann kein keine keinem
    keinen keiner keines können könnte lang man manche manchen mancher mein meine meinem meinen meiner
    meines mich mir mit muss musste müssen nach nicht nichts noch nun nur ob oder ohne sehr sein seine
    seinem seinen seiner seines selbst sich sie sind so soll sollte sondern sonst über um und uns unser
    unsere unter vom von vor war waren warst was weg weil weiter welche welchem welchen welcher welches wenn
    werde werden werdet weshalb wie wieder will wir wird wirst wo wohl würde würden zu zum zur zwar zwischen
    kapitel buch bücher seite seiten band ausgabe herr frau sagte sagte
    """,
    "es": """
    a al algo algunas algunos ante antes como con contra cual cuando de del desde donde dos durante e el
    ella ellas ello ellos en entre era eran es esa esas ese eso esos esta estaba estaban estado estamos
    estan estar estas este esto estos fue fueron ha haber habia han hasta hay la las le les lo los mas me mi
    mis mismo mucho muy nada ni no nos nosotros o otra otras otro otros para pero poco por porque que quien
    se sea ser si sin sobre son su sus tal tambien tan tanto te tiene todo todos tras tu un una unas uno
    unos usted ustedes ya yo capítulo libro página edición
    """,
}
_STOPWORDS_RAW["en"] += (
    " number numbers dear young want wanted certainly large single twenty began thousand whole "
    "require required better proper ten several five six seven eight nine million millions hundred hundreds certain "
    "particularly sir miss lady cried im ive ill youll youre youve theyre theyve weve well hes shes "
    "wasnt werent arent wont "
)
_STOPWORDS_RAW["ru"] += " коей которыя сему сими оный оная оные "
_STOPWORDS_RAW["fr"] += " jeune jeunes cher chère certaine certain "
STOPWORDS = {language: frozenset(text.split()) for language, text in _STOPWORDS_RAW.items()}
WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?", re.UNICODE)
BOILERPLATE = re.compile(
    r"(?:start|end) of (?:the|this) project gutenberg|gutenberg[.](?:org|net)|"
    r"project gutenberg(?:™| trademark| literary archive)|full project gutenberg license",
    re.I,
)
MAX_VOCABULARY = 8192
MAX_FEATURES = 256
MAX_PASSAGES = 2000


def tokens(text: str, language: str) -> list[str]:
    """Return original-language words; no stemming or translation is implied."""
    stopwords = STOPWORDS.get(language.split("-")[0], frozenset())
    normalized = unicodedata.normalize("NFC", text).lower()
    return [
        word
        for word in WORD.findall(normalized)
        if 3 <= len(word) <= 40 and word not in stopwords and word.replace("'", "").replace("’", "") not in stopwords
    ]


def _build_map(library: Library, *, max_books: int = 500, max_themes: int = 60) -> dict[str, Any]:
    """Build a bounded lexical map from indexed passages, without all-pairs matrices.

    Vocabulary is pruned per book; at most 2,000 passages are sampled from each.
    Rare-term inverted postings propose neighbors. The visible map is capped at
    500 books; callers receive counts and limitations instead of silent omission.
    """
    started = time.perf_counter()
    max_books = min(500, max(1, max_books))
    max_themes = min(100, max(1, max_themes))
    counters: dict[str, Counter[str]] = {}
    sampled: set[str] = set()
    vocabulary_pruned = False
    with library.connect() as conn:
        conn.execute("BEGIN")
        total = conn.execute("SELECT COUNT(*) FROM books WHERE status='indexed'").fetchone()[0]
        books = [
            dict(row)
            for row in conn.execute(
                "SELECT id,title,author,language,edition FROM books WHERE status='indexed' ORDER BY title,id LIMIT ?",
                (max_books,),
            )
        ]
        for book in books:
            counter: Counter[str] = Counter()
            rows = conn.execute(
                "SELECT text FROM passages WHERE book_id=? ORDER BY section_id,start LIMIT ?",
                (book["id"], MAX_PASSAGES + 1),
            )
            for ordinal, row in enumerate(rows):
                if ordinal >= MAX_PASSAGES:
                    sampled.add(book["id"])
                    break
                if BOILERPLATE.search(row["text"]):
                    continue
                counter.update(tokens(row["text"], book["language"]))
                if len(counter) > MAX_VOCABULARY:
                    counter = Counter(dict(counter.most_common(MAX_VOCABULARY // 2)))
                    vocabulary_pruned = True
            counters[book["id"]] = Counter(dict(counter.most_common(MAX_FEATURES)))

    language_counts = Counter(book["language"] for book in books)
    document_frequency: Counter[tuple[str, str]] = Counter()
    book_languages = {book["id"]: book["language"] for book in books}
    for book_id, counter in counters.items():
        document_frequency.update((book_languages[book_id], term) for term in counter)
    vectors: dict[str, dict[str, float]] = {}
    postings: dict[tuple[str, str], list[str]] = defaultdict(list)
    for book in books:
        book_id, language = book["id"], book["language"]
        vector = {
            term: (1.0 + math.log(count))
            * (1.0 + math.log((1.0 + language_counts[language]) / (1.0 + document_frequency[(language, term)])))
            for term, count in counters[book_id].items()
        }
        norm = math.sqrt(sum(weight**2 for weight in vector.values())) or 1.0
        vectors[book_id] = {term: weight / norm for term, weight in vector.items()}
        for term in vector:
            postings[(language, term)].append(book_id)
        ranked = sorted(vectors[book_id].items(), key=lambda item: (-item[1], item[0]))
        book["terms"] = [{"term": term, "weight": round(weight, 6)} for term, weight in ranked[:12]]
        book["sampled"] = book_id in sampled

    # Very common terms make weak connections and quadratic posting lists.
    # Bound each posting at 50 books and ignore terms in >65% of a language.
    pair_scores: dict[tuple[str, str], float] = defaultdict(float)
    for (language, term), book_ids in postings.items():
        cutoff = max(2, int(language_counts[language] * 0.65))
        if not 2 <= len(book_ids) <= min(50, cutoff):
            continue
        for source, target in combinations(sorted(book_ids), 2):
            pair_scores[(source, target)] += vectors[source][term] * vectors[target][term]
    neighbors: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for (source, target), _ in pair_scores.items():
        shared = vectors[source].keys() & vectors[target].keys()
        cosine = sum(vectors[source][term] * vectors[target][term] for term in shared)
        if cosine >= 0.025:
            neighbors[source].append((cosine, target))
            neighbors[target].append((cosine, source))
    edge_pairs: set[tuple[str, str]] = set()
    edges: list[dict[str, Any]] = []
    for source in sorted(neighbors):
        for score, target in sorted(neighbors[source], key=lambda item: (-item[0], item[1]))[:3]:
            ordered_pair = sorted((source, target))
            pair = (ordered_pair[0], ordered_pair[1])
            if pair in edge_pairs:
                continue
            edge_pairs.add(pair)
            shared = sorted(
                vectors[source].keys() & vectors[target].keys(),
                key=lambda term: (-vectors[source][term] * vectors[target][term], term),
            )[:5]
            edges.append({"source": pair[0], "target": pair[1], "score": round(score, 6), "shared_terms": shared})

    themes: list[dict[str, Any]] = []
    for (language, term), book_ids in postings.items():
        salience = sum(vectors[book_id][term] for book_id in book_ids) / math.sqrt(len(book_ids))
        themes.append(
            {"term": term, "language": language, "books": sorted(book_ids), "count": len(book_ids), "weight": salience}
        )
    # Shared terms first; single-book entry points remain useful in tiny corpora.
    themes.sort(key=lambda theme: (theme["count"] < 2, -theme["weight"], theme["language"], theme["term"]))
    selected_themes: list[dict[str, Any]] = []
    selected_keys: set[tuple[str, str]] = set()
    # Reserve representation for each language before filling globally.
    for language in sorted(language_counts):
        for theme in [item for item in themes if item["language"] == language][: min(5, max_themes)]:
            if len(selected_themes) >= max_themes:
                break
            selected_themes.append(theme)
            selected_keys.add((language, theme["term"]))
    for theme in themes:
        if len(selected_themes) >= max_themes:
            break
        if (theme["language"], theme["term"]) not in selected_keys:
            selected_themes.append(theme)
            selected_keys.add((theme["language"], theme["term"]))
    selected_themes.sort(key=lambda theme: (theme["count"] < 2, -theme["weight"], theme["language"], theme["term"]))
    for theme in selected_themes:
        theme["weight"] = round(theme["weight"], 6)
    limitations = [
        "Связи показывают сходство словаря, а не согласие авторов или противоположность аргументов.",
        "Темы — характерные слова исходного текста. Перевод и объединение словоформ не выполняются.",
        "Сопоставляются тексты одного языка. Межъязыковые связи не вычисляются.",
        "На книгу сохраняются 256 частотных терминов; частые служебные слова удаляются для en, ru, fr, de, es.",
    ]
    if total > len(books):
        limitations.append(
            f"Показаны первые {len(books)} книг по названию из {total}; карта ограничена {max_books} книгами."
        )
    if sampled:
        limitations.append(f"Для {len(sampled)} книг используются первые {MAX_PASSAGES} фрагментов.")
    if vocabulary_pruned:
        limitations.append("В длинных текстах словарь периодически сокращается до самых частотных слов.")
    return {
        "method": "lexical-tfidf",
        "books": books,
        "themes": selected_themes,
        "edges": sorted(edges, key=lambda edge: (-edge["score"], edge["source"], edge["target"])),
        "corpus_books": total,
        "truncated": total > len(books),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "limitations": limitations,
    }


_MAP_CACHE: OrderedDict[tuple[str, str, int, int], dict[str, Any]] = OrderedDict()
_MAP_CACHE_LOCK = Lock()
_MAX_CACHED_MAPS = 4


def _corpus_identity(library: Library) -> str:
    """Hash index identity and small metadata, never full book or vector contents."""
    identity = hashlib.sha256()
    with library.connect() as conn:
        # A read transaction keeps book/version rows and passage identity coherent.
        conn.execute("BEGIN")
        rows = conn.execute(
            "SELECT b.id,b.title,b.author,b.language,b.edition,b.work_id,b.content_sha256,"
            "b.indexed_at,COALESCE(v.fingerprint,'') FROM books b "
            "LEFT JOIN index_versions v ON v.book_id=b.id WHERE b.status='indexed' ORDER BY b.id"
        )
        for row in rows:
            identity.update(json.dumps(tuple(row), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            identity.update(b"\n")
        # AUTOINCREMENT passage IDs change on reindex even for identical text.
        # Count also catches deletion without reading/hash-checking all passage text.
        row = conn.execute("SELECT COUNT(*),COALESCE(MAX(id),0) FROM passages").fetchone()
        identity.update(json.dumps(tuple(row)).encode("ascii"))
    return identity.hexdigest()


def build_map(library: Library, *, max_books: int = 500, max_themes: int = 60) -> dict[str, Any]:
    """Return a current thematic map, reusing at most four immutable snapshots.

    Concurrent cache misses are serialized to avoid duplicate expensive builds.
    A change during construction is disclosed and the result is not cached. The
    response owns its nested containers, so callers cannot mutate cached data.
    """
    started = time.perf_counter()
    max_books = min(500, max(1, max_books))
    max_themes = min(100, max(1, max_themes))
    with _MAP_CACHE_LOCK:
        before = _corpus_identity(library)
        key = (str(library.db_path.resolve()), before, max_books, max_themes)
        if key in _MAP_CACHE:
            _MAP_CACHE.move_to_end(key)
            result = deepcopy(_MAP_CACHE[key])
            result["cache_hit"] = True
            result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
            return result
        result = _build_map(library, max_books=max_books, max_themes=max_themes)
        result["build_ms"] = result["elapsed_ms"]
        result["cache_hit"] = False
        result["corpus_identity"] = before
        result["snapshot_changed"] = _corpus_identity(library) != before
        if result["snapshot_changed"]:
            result["limitations"].append(
                "Во время построения карты корпус изменился. Показан снимок на начало построения; "
                "обновите карту после завершения индексации. Этот результат не сохранён в кэше."
            )
        else:
            _MAP_CACHE[key] = deepcopy(result)
            _MAP_CACHE.move_to_end(key)
            while len(_MAP_CACHE) > _MAX_CACHED_MAPS:
                _MAP_CACHE.popitem(last=False)
        result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return result
