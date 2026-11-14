# План opt-in span-only storage: только read-only аудит

**Реализация ещё не начата. Базы, frozen snapshots, индексы и код приложения не
менялись.** Аудит выполнен после эксперимента −41,03% на owned copy. `library`,
`semantic`, `index_coverage` и `index_installation` остаются замороженными до
координации с root; live semantic validation / installer имеют приоритет.

Предлагаемый минимальный путь: общий канонический reader + **явный постоянный
`storage_mode`**, сначала совместимость inline/span во всех consumers, затем
отдельная явная миграция. По отсутствию текста режим не угадывать.

## Точный SQL inventory

Состояние рабочего дерева на 15 ноября, HEAD
`01e72c59fad1f7a1e4d80e993ff54af0d0a85423`. Строки относятся к этому аудиту;
ни одна приведённая запись SQL сейчас не изменена.

| Файл / строка / функция | Прямое чтение или запись | Что требуется до span migration |
| --- | --- | --- |
| `library.py:324`, `import_book` | `INSERT INTO passages(...text,sha256)`; рядом `:328` FTS insert получает исходный `text` | В inline сохранять прежний текст; в span сохранять sentinel, но SHA и FTS payload брать из того же исходного chunk. Parser/chunker не менять |
| `library.py:351`, `_remove_index` | `SELECT p.*, b.title,b.author,s.title,a.section_author`; `:358–362` передаёт `row['text']` в FTS5 OLD delete payload | **Обязательная hydration до удаления sections**, с прежними headers/contributor. Иначе reimport/delete/subset snapshot оставят неправильные FTS postings |
| `library.py:569`, `passages` | `SELECT p.*,... substr(s.text,start+1,end-start) section_quote`; после закрытия connection вызывает `_result` | Hydrate внутри того же connection/read snapshot, до его закрытия; сохранить input order и лимит 500 |
| `library.py:590`, `_passage` | `SELECT p.*,... s.text section_text`; используется также внутри `save` transaction | Mode-aware text в caller transaction; сохранить context, offsets, citation, SHA verification |
| `library.py:658`, `search` | FTS join `SELECT p.*,... section_quote,bm25(...)`; затем `_result` и lexical rerank | Восстановить canonical text кандидатов до проверки/rerank; не превращать corrupt evidence в verified fallback |
| `library.py:526`, `_result` | Не SQL; сравнивает `r['text']` со span и SHA | Получать гидратированный текст + явную representation-validation; inline сохраняет строгую независимую проверку stored text |
| `semantic.py:102`, `_SQL` | `SELECT p.id,p.text,b.title,b.author,s.title section_title` | Shared canonical input iterator; добавить IDs/book/bounds/SHA для проверки, сохранить точный header/text |
| `semantic.py:193`, `_count_windows`; `:526`, encoding/resume loop | Обе итерации используют `_SQL` | Одинаковая hydration в count и encode/resume; те же windows, IDs, cursor и exact-input keys |
| `index_installation.py:226`, `_input_proof` | Отдельная копия `SELECT p.id,p.text,b.title,b.author,s.title...` | Использовать тот же canonical iterator: installer не должен проверять пустые окна или отличающийся encoder input |
| `index_coverage.py:256`, `capture_coverage` | `SELECT p.id,p.sha256,p.book_id,p.section_id,p.start,p.end,p.text`; `:260` SHA raw text; `:291` сравнение с chapter slice | Проверять representation по mode, затем canonical slice/SHA; логические fingerprints должны остаться одинаковыми |
| `index_coverage.py:47–83`, `_trigger_sql` | Epoch triggers наблюдают `passages.text` наряду с IDs/bounds/SHA | Очистка текста штатно повысит epochs. **Не сбрасывать epochs и не отключать triggers** ради старого receipt |
| `integrity.py:96`, `audit_library` | `SELECT * FROM passages WHERE section_id=?`; хеширует `passage['text']` и сравнивает со section | Mode-aware verification, использовать уже загруженный section; прежний inline corruption audit сохраняется |
| `explore.py:147`, `_build_map` | `SELECT text FROM passages ... LIMIT ?` | Восстанавливать sampled canonical text до tokens/boilerplate filtering; иначе получится пустая тематическая карта |
| `scripts/benchmark_fts_maintenance.py:106` | Explicit rebuild: `SELECT p.id,p.text,...` в FTS insert | Canonical input или явный отказ для span DB; нельзя молча построить пустой FTS |
| `scripts/benchmark_span_storage.py:114,151,241,318` | Специально читает raw/slice text и пишет sentinel только в owned experiment copy | Оставить исследовательским инструментом с отдельной семантикой; текущая **непомеченная** sentinel copy не становится production DB |

Метаданные-only SQL не требует hydration: `semantic.corpus_fingerprint` (`id,sha256`),
semantic eligibility query (`p.id`), counts/max IDs в stats/explore, ranking-only
SQL FTS benchmark. Не нужно читать весь section для этих операций.

## Косвенные consumers и API

- `GET /api/search`, `/api/passages/{id}`, research и reranking получают text из
  `Library.search/passages/passage`; response schema и browser quotes не меняются.
- `Library.save` вызывает `_passage` в `BEGIN IMMEDIATE`; hydration должна
  использовать **тот же** transaction. `saved.snapshot_text`, citation,
  `saved_provenance` и `canonical_versions` не компактировать и не переписывать.
  `_saved_item` сравнивает frozen snapshot с текущим passage; historical reader
  продолжает работать после reimport независимо от current IDs.
- `get_book` и основной reader читают `sections.text` и сами не требуют span
  adaptation. Проверки целых разделов и оригинальных артефактов сохраняются.
- `snapshots.create_snapshot` переносит формат через SQLite backup. При выборе
  subset вызывает `_remove_index`, поэтому зависит от OLD FTS payload hydration.
  Source snapshot никогда не передавать writable `Library.__init__`.
- `evaluation` клонирует источник и пользуется Library/reference adapters;
  проверить оба storage modes, immutable source hashes и partial-index binding.
- `vector_reuse`, tokenizer windows, reranker и exporting работают с переданными
  строками, а не raw SQL. Исправлять формат их текста не нужно; требуются проверки
  идентичности inputs/results после смены storage representation.

## Рекомендуемый формат и reader contract

Предложение для последующей реализации, не существующий сейчас интерфейс:

1. Отдельный DB descriptor `storage_mode = inline-v1 | span-v1` и version.
   Отсутствие descriptor означает **строгий legacy inline**. Неизвестный,
   повреждённый или противоречивый descriptor — fail closed.
2. В `inline-v1` `p.text` обязателен и должен совпасть с section slice/SHA.
   `text=''` при непустом span — повреждение. Запрещён heuristic fallback вроде
   `COALESCE(NULLIF(p.text,''),substr(...))`: он скрывает inline corruption.
3. В `span-v1` пустой sentinel обязателен; непустой `p.text` — format/integrity
   error. Текст вычисляется по canonical section и проверяется по `p.sha256`,
   book/section binding, типам и границам. Никакой дополнительной нормализации
   Unicode, пробелов, табов или LF при hydration.
4. Общий модуль без импорта Library: resolve одной строки и bounded iterator/batch
   hydration. Python codepoint slicing — reference semantics. Для bulk paths
   допускается ограниченный LRU sections, принадлежащий **одному read snapshot**;
   process-global cache только по section ID небезопасен при reimport/reuse IDs.
5. Mode, passage rows и section fetches читаются в одном SQL snapshot. Сохранить
   уже открытые caller transactions; не делать второй connection при save или
   proof capture. Для новых многошаговых read paths нужен owned read transaction.
6. Для API повреждённые строки подавляются с нынешним предупреждением/verified
   semantics; embedding, installer, integrity и FTS deletion должны остановиться
   на corrupt canonical input. Не подменять ошибки пустым «успешным» текстом.

Начальный формат может быть глобальным для всей БД: atomic conversion всех rows
и descriptor в одной transaction; mixed row formats пока не нужны. Runtime env
или CLI flag означает запрос на opt-in/creation, **не автоматическое очищение
существующей БД при startup**. После конверсии persisted mode обязателен при
чтении даже с выключенным runtime opt-in. Старый binary напрямую читать такую
БД не сможет; downgrade требует отдельной materialization/restore.

View/rename table как скрытую замену `passages` не предлагаю: это затрагивает
`lastrowid`, FK и epoch trigger definitions, а также может маскировать corruption.

## FTS и semantic proof guards

**FTS:** storage conversion не меняет tokenizer, postings, docsize, row IDs или
headers, не вызывает optimize/rebuild. Перед реальным reimport/delete нужно
восстановить и проверить старый канонический текст, затем передать его в
contentless FTS delete вместе с прежними title/author/section headers и только
после этого удалять sections. Если старый canonical source повреждён и payload
невозможно проверить, не продолжать частичное удаление: требуется отдельный
repair path с оригинальным источником/индексом.

**Coverage:** `capture_coverage` должен хешировать фактический canonical text,
проверяя representation отдельно. Структура source proofs и encoder inputs
может оставаться логически прежней: source sections, headers, IDs, spans и SHA
не меняются. Но UPDATE p.text повысит epochs, DDL descriptor изменит schema
cookie, а replacement DB изменит inode. Старый target binding после этого
правильно invalidates; cached «valid» не переносится.

Storage descriptor должен участвовать в проверяемом state/cache guard до cache
hit; смена значения mode без source UPDATE не должна обойти fail-closed checks.
Совместимость старых inline receipt/state tokens следует сохранить на inline
ветке; для span выдаётся явный новый binding. Это место согласовать с владельцем
`index_coverage`, не менять протокол между live semantic validations.

Не сбрасывать epochs, не переписывать старый immutable receipt и не отключать
triggers. После migration — **явный reinstall/rebind** через проверенный
installer: full source coverage, готовые vector/ID/key-map receipts и exact
window input proof. Существующий `install_partial_index` умеет повторно
использовать уже проверенную target generation и создать новый installation
receipt; понадобится лишь storage-aware source/target input reading.
Vectors/model-space/window keys не должны меняться только из-за хранения текста.
Для первоначальных тестов достаточно fake encoder и cached tokenizer — новые
загрузки моделей и MPS не нужны.

## Минимальные этапы после координации

1. **Reader compatibility, inline default.** Новый общий resolver + тесты, затем
   перечисленные consumers. Пустой legacy inline остаётся corruption. Пока ни
   один persisted mode/row существующей библиотеки не меняется. Старые APIs,
   lexical/semantic receipts и source hashes должны проходить прежние проверки.
2. **Owned fixtures/copy.** Явная format migration только собственной копии;
   сравнить все spans, metadata и FTS fingerprints, ranking/citations, model
   windows/key checksums, resume, receipts, map, save/export/historical reader,
   subset snapshots и FTS deletion/reimport. Frozen sources остаются immutable.
3. **Explicit migration command, opt-in.** Dry-run выдаёт source identity,
   storage mode, полный integrity/proof результат, активные build/installers и
   measured disk budget. Maintenance window: quiesce writers/old readers,
   независимый проверенный backup. В одной transaction проверить source и
   переключить mode + sentinel rows, сохранив IDs/SHA/metadata. VACUUM — отдельная
   проверяемая post-commit стадия. После crash до commit старый inline цел;
   после commit span reader работает, даже если reclaim места ещё не завершён.
4. **Live rollout только после успешной semantic validation.** Повторная
   integrity/proof проверка, явный receipt reinstall/rebind, API/browser smoke и
   сравнение canonical results. Активный frozen embedding source не мигрировать;
   миграция target не должна модифицировать staging/ready generations.

Этапы 1–4 здесь не выполнены и не разрешены к началу без координации с root.

## Обязательная regression matrix

| Область | Проверки для inline и span |
| --- | --- |
| Corruption | inline `text=''`/tampered unchanged SHA остаётся invalid; span unexpected text, bad bounds/book/section/SHA, marker tampering/missing/unknown version не становятся valid |
| Exact text | RU/FR/DE, astral Unicode, accents, NBSP, tabs/LF, code/regex; все existing passage IDs и source SHA сохраняются |
| FTS lifecycle | Delete/reimport одного book, source failure rollback, subset snapshot; прежние строки исчезают, новые queries возвращают точные citations; storage migration не меняет FTS shadows |
| Semantic inputs | Count/encode/resume/install используют один input iterator; headers, windows, IDs, exact-input keys и checksums идентичны; fake/cached tokenizer без MPS |
| Coverage | Epochs повышаются; old bindings fail closed; explicit reinstall проходит; covered source mutation по-прежнему invalidates; inline no-op UPDATE не invalidates |
| User evidence | Save/clipboard/export, frozen snapshot, historical reader после reimport; snapshot_text/citation и contributor attribution не меняются |
| Reader/map/API | Section DOM/quote marks и payloads прежние; explore themes не пустеют; error handling не выдаёт пустые verified citations |
| Migration | New DB default inline, opt-in explicit, atomic crash states, idempotent resume/reclaim, unknown mode rejection, documented reverse materialization |

## Диск: пока hard blocker для полного live migration

Read-only stat при аудите:

- live `data/full/library.sqlite3`: **5 367 955 456 байт**;
- свободно: **15 806 234 624 байта**;
- консервативный preflight **4 ГиБ + 3×DB = 20 398 833 664 байта** не проходит.

Этот предел не ослаблять. Измеренный эксперимент был на snapshot без нынешних
live epoch guards; его UPDATE/VACUUM timing не является timing production
миграции с trigger bumps. Диск/время/backup для live пути ещё не измерены.
Альтернативный `VACUUM INTO`/owned backup алгоритм возможен только отдельным
измеренным экспериментом с собственным space bound и rollback proof; здесь он
не реализован. Не считать APFS clone автоматически бесплатным запасом.

Следующий согласуемый шаг — reader-only compatibility слой и изолированные
fixtures после завершения текущей live semantic validation. Production и frozen
DB до этого остаются без изменений.
