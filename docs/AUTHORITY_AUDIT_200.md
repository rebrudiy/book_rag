# Authority audit: 200 новых публичных изданий

Подготовлены 200 ранее не проверенных indexed Gutenberg editions: **EN 67,
DE 67, FR 66**. Успешный ответ получен для первых **50**: найден один
`authority_linked` кандидат и 49 записей без exact P2034 в ответе. Второй запрос
остановился по read timeout; **150 записей остались authority_unavailable**.
Это не «199 отсутствующих произведений в Wikidata» и не оценка покрытия всех 200.
Рабочие work IDs, манифесты, БД и счётчик подтверждённых произведений не менялись.

Машинный metadata-only отчёт: `experiments/authority-200-v1.json`.
Runtime с исходными запросами, ответами, receipts, предложениями и локальными
frontmatter: `data/identity/wikidata-200-v1/`, игнорируется Git.

## Вход, запросы и остановка

ID взяты только из `data/catalog/expand-text-new.json` и `corpus/manifest.json`.
Для каждого проверены official HTTPS `/ebooks/N`, совпадение `pgN-*`,
`catalog_rights_checked`, public-domain-in-USA metadata, full scope и
`book_or_text`. `books`/`book_metadata` читаются через `mode=ro`, `query_only`:
требуются indexed, тот же Gutenberg source ID и отсутствие curated identity.
Прежние 50 authority-контролей исключены. Никакие saved notes, private imports
или пользовательские тексты не читаются. Из 2 679 подходящих записей выборка
получена по seeded SHA-256 priority внутри языка и round-robin EN/DE/FR.

Новый `authority_audit.batch_query` объединяет exact P2034 binding, до трёх
P629 edges, direct P31 types, P50 authors/labels и P136/P527/P361 scope evidence
в одном VALUES-запросе до 50 Gutenberg ID. Три `P629?` задают конечный максимум
глубины, а не неограниченный обход `P629*`. Дополнительных HTTP-запросов за
авторами или родителями не возникает. Типы работ распознаются консервативным
набором прежнего `work_identity.py`; generic `book` не становится work root.
Multiple items/parents, cycles, unknown types и collection/component признаки
не дают автоматического слияния. `source_scope_verified=false` сохраняется даже
при однозначном authority root.

Первичные основания:

- [P2034](https://www.wikidata.org/wiki/Property:P2034) идентифицирует Gutenberg
  ebook, но сам по себе не доказывает границы литературного произведения.
- [P629](https://www.wikidata.org/wiki/Property:P629) означает version, edition
  или translation of; более узкий вид отношения не угадывается.
- [Wikidata licensing](https://www.wikidata.org/wiki/Wikidata:Licensing)
  разрешает reuse структурированных данных как CC0. Это не лицензия книжного
  текста, который по-прежнему имеет отдельное происхождение и условия.
- [WDQS manual](https://www.mediawiki.org/wiki/Wikidata_Query_Service/User_Manual#Query_limits)
  и [Data access](https://www.wikidata.org/wiki/Wikidata:Data_access)
  описывают endpoint, ограничения и выбор экономного способа доступа.

Границы: 4 requests максимум, 2 МиБ на response, максимум 8 МиБ суммарно,
120 секунд всего, 25 секунд на HTTP read, serial spacing 2 секунды,
описательный User-Agent, JSON/gzip, без redirects/retry loops. HTTP 429,
ошибки/HTML, неправильные bindings, byte/row/deadline limits прекращают работу.
Полный valid Retry-After сохраняется, не сокращается. Query text, query SHA,
исходные HTTP entity bytes, response SHA, время, endpoint и CC0 receipt
сохраняются; успешные snapshots проверяются повторно по SHA.

Фактическая попытка заняла **27,84 с** и отправила **2 запроса**. Первый вернул
8 378 байт JSON. Второй не успел получить ответ за 25 секунд: HTTP status и
Retry-After не получены. Клиент поставил консервативную часовую отсрочку до
`2026-11-14T23:06:28.869486Z` и не отправлял запросы 3–4. Это локальный
transport timeout, а не подтверждённый HTTP access denial. Endpoint не менялся,
блокировка не обходилась. Первый ответ охватывает EN 17 / DE 17 / FR 16.

## Один новый кандидат

Для [Gutenberg 56156, Venus im Pelz](https://www.gutenberg.org/ebooks/56156):

`P2034 "56156" → Q140772079 → P629 → Q677592`.

Последний элемент — [Venus in Furs / Venus im Pelz](https://www.wikidata.org/wiki/Q677592),
direct type literary work; P50 указывает на
[Leopold von Sacher-Masoch, Q78474](https://www.wikidata.org/wiki/Q78474).
Немецкий authority title и автор согласуются с OPF и печатным frontmatter.
Метка edition item отсутствует и остаётся null, вместо подстановки каталожного
названия. Подтверждена цепочка authority assertions, **не новое verified unique
work**: издание 1920 года также содержит позднее предисловие. Его атрибуция и
границы основного текста требуют отдельной проверки перед применением правила.

Исходный EPUB проверен по SHA:
`c0d1deca89679bd6780d66c81377ce47475b991f7b16c9db20da2b6643bfc5ee`.
Ни одно предложение не изменило `work_id` и не увеличило confirmed-work count.

## 20 проверок cached frontmatter

Отдельно просмотрены 20 новых записей из отвеченного пакета: EN 7 / DE 7 / FR 6.
Для каждой проверен SHA локального публичного артефакта, извлечены OPF и
ограниченные префиксы первых четырёх spine members. Ни файл не распаковывался
целиком в память, ни текст не скачивался. Во всех 20 OPF title соответствует
каталогу, фамилия/имя каталожного creator присутствует. **Это не подтверждение
правильной авторской роли**: ручная проверка выявила редакторов, переводы,
сборники и части многотомников. Для независимого authority comparison данные
имеются только у одного нового кандидата; у остальных 19 exact claim в первом
ответе не найден.

| PG ID | Проверенное наблюдение / граница work |
| --- | --- |
| 21789 | Два французских оригинала Mornay и Garnier, оба переведены Countess of Pembroke; не один оригинальный work |
| 56156 | Sacher-Masoch, 1920 edition; одна novella плюс отдельный поздний Vorwort |
| 33388 | Austen, Tome premier 1815; явно свободный перевод Isabelle de Montolieu |
| 34771 | G. Gore, Scientific Basis, Williams and Norgate 1882; authority root не получен |
| 79789 | Kriegsschiffbau, Berlin 1919, 34 оригинальных иллюстрации; text-only полнота визуального материала не доказана |
| 64144 | Femmes nouvelles, Paul и Victor Margueritte; совместное авторство и список других книг не смешивать |
| 27106 | Ferguson, Museum of History and Technology, PAPER 27; статья/публикация в серии, не вся серия |
| 53344 | Hantschel, guide 1905 с картой и открытками; визуальные материалы отдельно |
| 66995 | Huysmans, Trois Eglises, Paris 1920; несколько очерков и гравюры Ch. Jouas |
| 7923 | Smith/Watson, Part One, Plate 1–69; преимущественно репродукции, text-only coverage требует проверки |
| 6503 | Schiller, Der Neffe als Onkel, явно из французского Picard; creator не равен автоматически автору оригинала |
| 26336 | Talleyrand, report 1791; цифровое оглавление добавлено позднее, есть рамка архивного переиздания |
| 36523 | Titus, Last Straw, second printing June 1920; OPF согласован, authority identity не установлена |
| 60845 | Schaeffer, Helianth: третий из трёх томов, начинается с седьмой книги |
| 36260 | Thierry, Attila: Tome second 1856, начинается третьей частью |
| 9591 | Whittier, Part 1 from Volume VI; несколько групп очерков и статей |
| 13805 | Heiberg, Todsünden, Roman 1891; вводный текст об авторе требует отдельной атрибуции |
| 64721 | Chroniques, том шестой 1838: Paulin Paris указан как издатель/редактор («publiées par»), а не автор средневековой хроники |
| 6476 | Hooker, Himalayan Journals, Volume I 1854; посвящение Darwin не является авторством |
| 50187 | Gerstäcker, 1854: первый том собрания рассказов, collection/volume/component не разрешены автоматически |

Ручные заметки привязаны к artifact SHA в
`frontmatter-manual-review.json`. Публичный summary содержит эти заметки и
библиографические поля; исходные excerpts остаются только под `data/`.

## Офлайн fallback и повторение

Без HTTP заново проверены **4** query-cache snapshots прежнего 50-control
эксперимента: CC0 provenance и canonical hashes совпали. Прежние результаты
16 authority_linked / 3 ambiguous / 31 unresolved сохранены отдельно, не прибавлены
к новым 200. Это подтверждает доступность офлайн-референса, а не новые authority
утверждения после тайм-аута.

```sh
.venv/bin/python scripts/audit_work_identity.py
.venv/bin/pytest -q tests/test_authority_audit.py
.venv/bin/ruff check src/book_rag/authority_audit.py scripts/audit_work_identity.py tests/test_authority_audit.py
.venv/bin/ty check src/book_rag/authority_audit.py scripts/audit_work_identity.py tests/test_authority_audit.py
```

Повторный CLI использует существующий исходный proposals report, проверяет raw
query-cache SHA и повторяет локальный frontmatter audit, **0 HTTP**. Он не
повторяет deferred attempt даже после истечения записанной отсрочки.
Проверки: **22 pytest passed**, Ruff/ty passed. Тесты покрывают точные provider
IDs, private-source mismatch, баланс, guarded caches, multiple items/parents,
циклы, unknown types, collection/component scope, 50 answered против 150
unavailable, максимум 4 batches, deadline, HTTP 403/429/503 и malformed/large HTML/JSON.

Следующее действие — отдельно проверить атрибуцию позднего Vorwort и полноту
основной novella PG56156, затем рассмотреть authority proposal. Для оставшихся
150 сохраняется незавершённая metadata-очередь. Текущая итерация заканчивается
без новых сетевых попыток и без автоматического применения authority links.
