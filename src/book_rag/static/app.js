'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const state = {
    view: 'search', mode: 'hybrid', query: '', books: [], saved: [], results: [], stats: null,
    currentBook: null, currentSection: null, currentPassage: null,
    saveDraft: null, searchSequence: 0, readerSequence: 0, readerSize: 20,
    previousView: 'search', researchQuery: '', toastTimer: null,
    explore: null, exploreLoading: false, mapSelection: null,
    landingTotal: 0, libraryOffset: 0, libraryTotal: 0, libraryHasMore: false, libraryRequest: 0,
    scopeBook: null, scopeBooks: [], scopeTotal: 0, scopeLoaded: false, scopeRequest: 0,
    compareBooks: [], compareTotal: 0, compareLoaded: false, compareRequest: 0, researchSelection: new Map(),
  };
  const languageNames = { ru: 'Русский', en: 'English', fr: 'Français', de: 'Deutsch', es: 'Español', it: 'Italiano', zh: '中文', ja: '日本語', la: 'Latina', el: 'Ελληνικά', pt: 'Português', uk: 'Українська', ar: 'العربية' };
  const statusNames = { discovered: 'Найдены', downloaded: 'Скачаны', processed: 'Обработаны', indexed: 'В поиске', searchable: 'В поиске', failed: 'Требуют внимания', error: 'Требуют внимания', duplicate: 'Дубли' };

  function node(tag, className, value) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (value !== undefined && value !== null) el.textContent = String(value);
    return el;
  }
  function button(label, handler, className = 'text-button') {
    const el = node('button', className, label);
    el.type = 'button';
    el.addEventListener('click', handler);
    return el;
  }
  function announce(message) { $('live-status').textContent = message; }
  function toast(message) {
    clearTimeout(state.toastTimer);
    $('toast').textContent = message;
    $('toast').classList.remove('hidden');
    state.toastTimer = setTimeout(() => $('toast').classList.add('hidden'), 4200);
  }
  function showError(error, target) {
    const message = error instanceof Error ? error.message : String(error);
    if (target) {
      target.replaceChildren(node('div', 'notice', message));
    } else {
      $('global-error').textContent = message;
      $('global-error').classList.remove('hidden');
      setTimeout(() => $('global-error').classList.add('hidden'), 10000);
    }
    announce(message);
  }
  async function api(path, options = {}) {
    let response;
    try { response = await fetch(path, { ...options, headers: { 'Content-Type': 'application/json', ...options.headers } }); }
    catch { throw new Error('Не удалось связаться с библиотекой. Проверьте, запущен ли сервер, и повторите действие.'); }
    let data;
    try { data = await response.json(); }
    catch { throw new Error(`Сервер вернул неожиданный ответ (${response.status}).`); }
    if (!response.ok) {
      const detail = typeof data.detail === 'string' ? data.detail : data.error || `Не удалось выполнить запрос (${response.status}).`;
      throw new Error(detail);
    }
    return data;
  }
  function safeURL(value) {
    if (typeof value !== 'string' || !value.trim()) return null;
    try {
      const url = new URL(value, location.origin);
      return ['https:', 'http:'].includes(url.protocol) ? url.href : null;
    } catch { return null; }
  }
  function loading(target, message) { target.replaceChildren(node('div', 'loading-state', message)); }
  function empty(target, title, description) {
    const box = node('div', 'empty-state');
    box.append(node('h3', '', title), node('p', '', description));
    target.replaceChildren(box);
  }
  function formatNumber(value) { return Number(value || 0).toLocaleString('ru-RU'); }
  function labelLanguage(value) { return languageNames[value] || value || 'Язык не указан'; }
  function asID(value) { return String(value ?? ''); }
  function sectionList(book) { return Array.isArray(book?.sections) ? book.sections : []; }
  function bookSectionsCount(book) { return Array.isArray(book.sections) ? book.sections.length : book.sections || 0; }
  function bookSearchable(book) { return ['indexed', 'searchable'].includes(book.status) || (!book.status && Number(book.passages) > 0); }
  function textExcerpt(text, length = 240) {
    const value = String(text || '');
    return value.length > length ? value.slice(0, length) + '…' : value;
  }

  function updateNav(view) {
    document.querySelectorAll('.site-header [data-view]').forEach((el) => {
      const active = el.dataset.view === view;
      el.classList.toggle('active', active);
      if (active) el.setAttribute('aria-current', 'page'); else el.removeAttribute('aria-current');
    });
  }
  function showView(view, push = true) {
    if (!['search', 'library', 'saved', 'reader', 'explore'].includes(view)) view = 'search';
    state.view = view;
    document.querySelectorAll('.view').forEach((el) => el.classList.toggle('hidden', el.id !== `${view}-view`));
    updateNav(view === 'reader' ? '' : view);
    document.title = `${({ search: 'Поиск и исследование', library: 'Библиотека', saved: 'Сохранённое', reader: 'Читатель', explore: 'Карта тем' })[view]} — Атлас книг`;
    if (push && view !== 'reader') {
      const params = new URLSearchParams();
      if (view !== 'search') params.set('view', view);
      if (view === 'search' && state.query) params.set('q', state.query);
      history.pushState({}, '', params.size ? `/?${params}` : '/');
    }
    if (view === 'library') loadLibrary();
    if (view === 'saved') renderSaved();
    if (view === 'explore') loadExplore();
    window.scrollTo({ top: 0, behavior: 'instant' });
  }
  function navigate(event) {
    const link = event.target.closest('a[data-view]');
    if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    showView(link.dataset.view);
  }
  document.addEventListener('click', navigate);

  function setMode(mode) {
    state.mode = mode;
    $('search-extra-mode').value = mode === 'reranked' ? 'reranked' : '';
    document.querySelectorAll('[data-mode]').forEach((el) => {
      const active = el.dataset.mode === mode;
      el.classList.toggle('active', active);
      el.setAttribute('aria-pressed', String(active));
    });
  }
  function setPanel(panel) {
    const research = panel === 'research';
    $('fragments-panel').classList.toggle('hidden', research);
    $('research-panel').classList.toggle('hidden', !research);
    $('fragments-tab').classList.toggle('active', !research);
    $('research-tab').classList.toggle('active', research);
    $('fragments-tab').setAttribute('aria-pressed', String(!research));
    $('research-tab').setAttribute('aria-pressed', String(research));
  }

  function coverageType(book) { return book?.coverage || book?.source_metadata?.coverage || ''; }
  function createBookCard(book, index) {
    const card = node('article', 'book-card');
    const cover = node('div', `book-cover cover-${index % 6}`);
    cover.setAttribute('aria-hidden', 'true');
    cover.append(node('span', 'cover-title', book.title));
    const info = node('div', 'book-card-info');
    const language = node('div', 'book-language');
    language.append(node('span', '', labelLanguage(book.language)), node('span', 'book-status', bookSearchable(book) ? '● В поиске' : statusNames[book.status] || book.status || 'В библиотеке'));
    info.append(language, node('h3', '', book.title), node('p', 'book-author', book.author));
    const selectedChapters = coverageType(book) === 'selected_chapters';
    const excerpt = coverageType(book) === 'excerpt' || /extract|excerpt|отрывок|фрагмент/i.test(book.edition || '');
    if (selectedChapters) info.append(node('p', 'book-excerpt-label', 'Выбранные главы'));
    else if (excerpt) info.append(node('p', 'book-excerpt-label', 'Отрывок произведения'));
    const footer = node('div', 'book-card-footer');
    footer.append(node('span', '', bookSearchable(book) ? `Разделов: ${formatNumber(bookSectionsCount(book))} · ${labelLanguage(book.language)}` : 'Текст ещё не индексирован'));
    const open = button(selectedChapters ? 'Читать главы ↗' : excerpt ? 'Читать отрывок ↗' : 'Читать ↗', () => openBook(book.id), 'book-open');
    open.disabled = !bookSearchable(book);
    open.setAttribute('aria-label', `Читать: ${book.title}`);
    footer.append(open); info.append(footer); card.append(cover, info);
    return card;
  }
  function populateSelect(select, values, placeholder) {
    const selected = select.value;
    select.replaceChildren();
    const all = node('option', '', placeholder); all.value = ''; select.append(all);
    values.forEach(({ value, label }) => { const option = node('option', '', label); option.value = value; select.append(option); });
    if ([...select.options].some((el) => el.value === selected)) select.value = selected;
  }
  function renderCatalog() {
    const searchable = state.books.filter(bookSearchable);
    $('featured-books').replaceChildren(...searchable.slice(0, 4).map(createBookCard));
    $('discovery-empty').classList.toggle('hidden', searchable.length !== 0);
    if (!state.scopeLoaded) { state.scopeBooks = searchable; state.scopeTotal = state.landingTotal; renderScopeBooks(); }
    if (!state.compareLoaded) { state.compareBooks = searchable; state.compareTotal = state.landingTotal; renderCompareBooks(); }
  }
  function renderScopeBooks() {
    const selected = state.scopeBook;
    const matches = [...state.scopeBooks];
    if (selected && !matches.some((book) => asID(book.id) === asID(selected.id))) matches.unshift(selected);
    populateSelect($('book-filter'), matches.map((book) => ({ value: asID(book.id), label: `${book.title} · ${book.author}` })), 'Вся библиотека');
    $('book-filter').value = selected ? asID(selected.id) : '';
    $('book-scope-status').textContent = state.scopeTotal > state.scopeBooks.length
      ? `Показаны ${formatNumber(state.scopeBooks.length)} из ${formatNumber(state.scopeTotal)} изданий. Уточните название или автора.`
      : `Подходящих изданий: ${formatNumber(state.scopeTotal)}.`;
    const selection = $('book-scope-selection'); selection.replaceChildren();
    selection.classList.toggle('hidden', !selected);
    if (selected) selection.append(node('span', '', `Поиск в книге: ${selected.title} · ${selected.author}`), button('Снять ограничение ×', () => { setScopeBook(null); if (state.query) search(); }));
  }
  function setScopeBook(book) { state.scopeBook = book; renderScopeBooks(); }
  async function loadScopeBooks() {
    const request = ++state.scopeRequest;
    const params = new URLSearchParams({ status: 'indexed', q: $('book-scope-query').value.trim(), limit: '50', offset: '0' });
    if ($('language-filter').value) params.set('language', $('language-filter').value);
    $('book-scope-status').textContent = 'Ищем названия и авторов…';
    try {
      const result = await api(`/api/books?${params}`);
      if (request !== state.scopeRequest) return;
      state.scopeBooks = result.books || []; state.scopeTotal = result.total ?? state.scopeBooks.length; state.scopeLoaded = true;
      renderScopeBooks();
    } catch (error) { if (request === state.scopeRequest) $('book-scope-status').textContent = error.message; }
  }
  function renderCompareBooks() {
    const selected = state.researchSelection;
    $('compare-selected').replaceChildren(...[...selected.values()].map((book) => button(`${book.title} ×`, () => { selected.delete(asID(book.id)); renderCompareBooks(); }, 'selected-book-chip')));
    $('compare-status').textContent = `Выбрано ${selected.size} из 10. Показаны ${state.compareBooks.length} из ${formatNumber(state.compareTotal)} подходящих изданий; уточните поиск, чтобы найти другие.`;
    $('compare-books').replaceChildren(...state.compareBooks.map((book) => {
      const label = node('label'); const check = node('input');
      check.type = 'checkbox'; check.value = asID(book.id); check.name = 'compare-book'; check.checked = selected.has(asID(book.id));
      check.addEventListener('change', () => {
        if (check.checked && selected.size >= 10) { check.checked = false; toast('Для одного исследования можно выбрать до 10 книг.'); return; }
        if (check.checked) selected.set(asID(book.id), book); else selected.delete(asID(book.id));
        renderCompareBooks();
      });
      label.append(check, node('span', '', `${book.title} · ${book.author}`)); return label;
    }));
    if (!state.compareBooks.length) $('compare-books').append(node('p', 'catalog-hint', 'Подходящих названий не найдено. Попробуйте другое имя автора.'));
  }
  async function loadCompareBooks() {
    const request = ++state.compareRequest;
    const params = new URLSearchParams({ status: 'indexed', q: $('compare-query').value.trim(), limit: '50', offset: '0' });
    $('compare-status').textContent = 'Ищем книги для исследования…';
    try {
      const result = await api(`/api/books?${params}`);
      if (request !== state.compareRequest) return;
      state.compareBooks = result.books || []; state.compareTotal = result.total ?? state.compareBooks.length; state.compareLoaded = true;
      renderCompareBooks();
    } catch (error) { if (request === state.compareRequest) $('compare-status').textContent = error.message; }
  }
  function debounce(handler, delay = 250) {
    let timer;
    return (...args) => { clearTimeout(timer); timer = setTimeout(() => handler(...args), delay); };
  }
  function renderStats() {
    const stats = state.stats || {};
    const counts = stats.status_counts || {};
    $('stat-books').textContent = formatNumber(stats.books);
    $('stat-works').textContent = formatNumber(stats.works);
    const provisional = stats.work_count_is_provisional !== false;
    const scope = provisional
      ? `Группировка изданий предварительная: ${formatNumber(stats.works)} групп пока не подтверждают число уникальных произведений. Проверено групп: ${formatNumber(stats.curated_work_groups)}. Периодических выпусков: ${formatNumber(stats.periodical_editions)}.`
      : `Связанные издания объединены в ${formatNumber(stats.works)} групп произведений по проверенным метаданным. Периодических выпусков: ${formatNumber(stats.periodical_editions)}.`;
    for (const id of ['corpus-scope', 'library-scope']) {
      $(id).textContent = scope;
      $(id).classList.remove('hidden');
    }
    $('stat-passages').textContent = formatNumber(stats.passages);
    const languages = stats.languages;
    const codes = Array.isArray(languages) ? languages.map((item) => typeof item === 'string' ? item : item.language) : Object.keys(languages || {});
    const options = codes.filter(Boolean).map((value) => ({ value, label: labelLanguage(value) }));
    populateSelect($('language-filter'), options, 'Все языки');
    populateSelect($('library-language'), options, 'Все языки');
    $('stat-languages').textContent = formatNumber(Array.isArray(languages) ? languages.length : typeof languages === 'object' && languages ? Object.keys(languages).length : languages || 0);
    const items = Object.entries(counts);
    $('pipeline-stats').replaceChildren(...items.map(([status, count]) => {
      const box = node('div', 'pipeline-stat'); box.append(node('b', '', formatNumber(count)), node('span', '', statusNames[status] || status)); return box;
    }));
    if (!items.length) {
      const box = node('div', 'pipeline-stat'); box.append(node('b', '', formatNumber(stats.books)), node('span', '', 'Доступно в поиске')); $('pipeline-stats').append(box);
    }
  }
  async function loadLibrary(offset = state.libraryOffset) {
    const request = ++state.libraryRequest;
    const params = new URLSearchParams({ q: $('library-query').value.trim(), offset: String(offset), limit: '48' });
    if ($('library-language').value) params.set('language', $('library-language').value);
    if ($('library-status').value) params.set('status', $('library-status').value);
    loading($('library-books'), 'Загружаем страницу каталога');
    $('library-prev').disabled = true; $('library-next').disabled = true; $('library-count').textContent = 'Загружаем…';
    try {
      const result = await api(`/api/books?${params}`);
      if (request !== state.libraryRequest) return;
      const books = result.books || []; const total = result.total ?? books.length;
      if (!books.length && total > 0 && offset >= total) { loadLibrary(Math.floor((total - 1) / 48) * 48); return; }
      state.libraryOffset = result.offset ?? offset;
      state.libraryTotal = total;
      state.libraryHasMore = result.has_more ?? state.libraryOffset + books.length < total;
      $('library-count').textContent = books.length ? `Показано ${formatNumber(state.libraryOffset + 1)}–${formatNumber(state.libraryOffset + books.length)} из ${formatNumber(total)} изданий` : 'Подходящих изданий: 0';
      $('library-page').textContent = total ? `Страница ${Math.floor(state.libraryOffset / 48) + 1} из ${Math.ceil(total / 48)}` : '';
      if (books.length) $('library-books').replaceChildren(...books.map(createBookCard));
      else empty($('library-books'), 'Книги не найдены', 'Попробуйте другой запрос, язык или состояние индексации.');
      $('library-prev').disabled = state.libraryOffset === 0;
      $('library-next').disabled = !state.libraryHasMore;
      announce($('library-count').textContent);
    } catch (error) {
      if (request !== state.libraryRequest) return;
      showError(error, $('library-books'));
      $('library-books').append(button('Повторить загрузку', () => loadLibrary(offset), 'button secondary'));
      $('library-count').textContent = 'Не удалось загрузить каталог';
      $('library-page').textContent = '';
    }
  }
  async function refreshLibrary() {
    const results = await Promise.allSettled([api('/api/stats'), api('/api/books?status=indexed&limit=6&offset=0')]);
    if (results[0].status === 'fulfilled') { state.stats = results[0].value; renderStats(); }
    if (results[1].status === 'fulfilled') { state.books = results[1].value.books || []; state.landingTotal = results[1].value.total ?? state.books.length; renderCatalog(); }
    if (results.some((result) => result.status === 'rejected')) showError(results.find((result) => result.status === 'rejected').reason);
  }

  function contributorLabel(section, bookAuthor) {
    const author = section?.section_author || section?.source_author || section?.contributor || '';
    const role = section?.section_role || section?.role || '';
    const labels = [];
    if (author && author !== bookAuthor) labels.push(`Автор раздела: ${author}`);
    if (role === 'editorial') labels.push('Редакционный раздел');
    return labels.join(' · ');
  }
  function passageCard(passage, number) {
    const card = node('article', 'passage-card');
    const top = node('div', 'passage-topline');
    top.append(node('p', 'passage-book', `${number ? String(number).padStart(2, '0') + ' / ' : ''}${passage.author || ''} · ${passage.title || ''}`), node('span', passage.verified ? 'verified-label' : 'unverified-label', passage.verified ? '✓ ЦИТАТА ПРОВЕРЕНА' : 'ПРОВЕРЬТЕ ИСТОЧНИК'));
    const quote = node('blockquote', '', passage.text || '');
    if (passage.language) quote.lang = passage.language;
    const citation = node('p', 'passage-citation', passage.citation || `${passage.section_title || ''} · ${passage.edition || ''}`);
    const actions = node('div', 'passage-actions');
    actions.append(button('Открыть в контексте ↗', () => openBook(passage.book_id, passage.section_id, passage.id)), button('＋ Сохранить', () => openSave({ kind: 'passage', passage_id: passage.id, title: `${passage.title} · ${passage.section_title || 'Фрагмент'}` }, passage.text)), button('Копировать', () => copyText(`${passage.text}\n\n${passage.citation || ''}`)));
    const contributor = contributorLabel(passage, passage.book_author || passage.author);
    card.append(top);
    if (contributor) card.append(node('p', 'contributor-label', contributor));
    card.append(quote, citation, actions); return card;
  }
  async function search(push = true) {
    const query = $('query').value.trim();
    if (!query) { $('query').focus(); return; }
    if (state.query !== query) { state.researchQuery = ''; $('research-output').replaceChildren(); }
    state.query = query;
    const request = ++state.searchSequence;
    $('search-results').classList.remove('hidden');
    $('discovery').classList.add('hidden');
    $('search-warnings').classList.add('hidden');
    $('results-meta').textContent = 'Ищем в библиотеке…';
    setPanel('fragments');
    loading($('fragments-panel'), 'Поиск фрагментов');
    const submit = $('search-form').querySelector('[type="submit"]');
    submit.disabled = true;
    const params = new URLSearchParams({ q: query, mode: state.mode, limit: '20' });
    if ($('language-filter').value) params.set('language', $('language-filter').value);
    if ($('book-filter').value) params.set('book_id', $('book-filter').value);
    if (push) { const route = new URLSearchParams({ q: query }); history.replaceState({}, '', `/?${route}`); }
    try {
      const result = await api(`/api/search?${params}`);
      if (request !== state.searchSequence) return;
      state.results = result.results || [];
      const time = Number(result.elapsed_ms || 0);
      const modeName = { lexical: 'по словам', hybrid: 'гибридный', semantic: 'по смыслу', reranked: 'с переоценкой' }[result.mode || state.mode] || result.mode;
      $('results-meta').textContent = `${formatNumber(state.results.length)} фрагментов · ${Number.isFinite(time) ? time.toFixed(0) : '—'} мс · ${modeName}`;
      if (state.results.length) $('fragments-panel').replaceChildren(...state.results.map((result, index) => passageCard(result, index + 1)));
      else empty($('fragments-panel'), 'Точного следа пока нет', 'В этой библиотеке не найдено подходящих фрагментов. Попробуйте другие слова, снимите фильтры или расширьте корпус. Отсутствие результатов не означает, что ответа нет в книгах.');
      if (result.warnings?.length) { $('search-warnings').textContent = result.warnings.join('\n'); $('search-warnings').classList.remove('hidden'); }
      announce(`Найдено фрагментов: ${state.results.length}.`);
    } catch (error) {
      if (request === state.searchSequence) { showError(error, $('fragments-panel')); $('results-meta').textContent = 'Поиск не завершён'; }
    } finally { if (request === state.searchSequence) submit.disabled = false; }
  }
  async function research() {
    if (!state.query) { toast('Сначала задайте вопрос в строке поиска.'); return; }
    const bookIds = [...state.researchSelection.keys()];
    if (!bookIds.length && $('book-filter').value) bookIds.push($('book-filter').value);
    const run = $('run-research'); run.disabled = true;
    loading($('research-output'), 'Собираем свидетельства из книг');
    const query = state.query; state.researchQuery = query;
    try {
      const data = await api('/api/research', { method: 'POST', body: JSON.stringify({ query, book_ids: bookIds, limit: 8 }) });
      if (state.researchQuery !== query) return;
      const output = $('research-output'); output.replaceChildren();
      const summary = node('div', data.abstained ? 'notice' : 'research-answer', data.answer || (data.abstained ? 'В доступных источниках недостаточно свидетельств для ответа.' : 'Найденные свидетельства приведены ниже.'));
      summary.append(node('p', 'research-method', `Метод: ${data.method || 'поиск по источникам'} · Все фрагменты можно открыть в читателе.`));
      output.append(summary);
      if (data.results?.length) output.append(...data.results.map((passage, index) => passageCard(passage, index + 1)));
      announce(data.abstained ? 'Недостаточно свидетельств для ответа.' : `Исследование готово. Источников: ${data.results?.length || 0}.`);
    } catch (error) { showError(error, $('research-output')); }
    finally { run.disabled = false; }
  }

  async function openBook(bookId, sectionId, passageId, push = true) {
    const request = ++state.readerSequence;
    if (state.view !== 'reader') state.previousView = state.view;
    showView('reader', false);
    loading($('reader-text'), 'Открываем книгу');
    $('reader-section-title').textContent = '';
    $('reader-citation').classList.add('hidden');
    $('reader-contributor').classList.add('hidden');
    try {
      const [book, passage] = await Promise.all([api(`/api/books/${encodeURIComponent(bookId)}`), passageId ? api(`/api/passages/${encodeURIComponent(passageId)}`) : Promise.resolve(null)]);
      if (request !== state.readerSequence) return;
      state.currentBook = book;
      state.currentPassage = passage;
      const sections = sectionList(book);
      state.currentSection = sections.find((section) => asID(section.id) === asID(sectionId)) || sections.find((section) => asID(section.id) === asID(passage?.section_id)) || sections[0] || null;
      renderReader();
      if (push) updateReaderURL();
    } catch (error) { if (request === state.readerSequence) showError(error, $('reader-text')); }
  }
  function updateReaderURL() {
    const params = new URLSearchParams({ book: asID(state.currentBook.id) });
    if (state.currentSection) params.set('section', asID(state.currentSection.id));
    if (state.currentPassage && asID(state.currentPassage.section_id) === asID(state.currentSection?.id)) params.set('passage', asID(state.currentPassage.id));
    history.pushState({}, '', `/?${params}`);
  }
  function selectChapter(sectionId) {
    const section = sectionList(state.currentBook).find((item) => asID(item.id) === asID(sectionId));
    if (!section) return;
    state.currentSection = section;
    renderReader(); updateReaderURL(); window.scrollTo({ top: 0, behavior: 'instant' });
  }
  function renderReader() {
    const book = state.currentBook; const section = state.currentSection; const passage = state.currentPassage;
    const sections = sectionList(book);
    $('reader-sidebar-title').textContent = book.title || '';
    $('reader-book-meta').textContent = `${book.author || ''} · ${book.title || ''} · ${book.edition || ''}`;
    const coverage = coverageType(book);
    $('reader-coverage').textContent = coverage === 'selected_chapters' ? 'Выбранные главы · частичный текст произведения' : coverage === 'excerpt' || /extract|excerpt|отрывок/i.test(book.edition || '') ? 'Отрывок произведения · частичный текст' : '';
    $('reader-coverage').classList.toggle('hidden', !$('reader-coverage').textContent);
    document.title = `${book.title} — Атлас книг`;
    $('reader-toc').replaceChildren(...sections.map((item) => {
      const btn = button(item.title || `Глава ${Number(item.ordinal || 0) + 1}`, () => selectChapter(item.id), asID(item.id) === asID(section?.id) ? 'active' : '');
      if (asID(item.id) === asID(section?.id)) btn.setAttribute('aria-current', 'true');
      return btn;
    }));
    const source = $('reader-source'); source.replaceChildren();
    source.append(node('p', '', book.edition || 'Издание не указано'), node('p', '', book.rights || 'Условия использования не указаны'));
    const sourceURL = safeURL(book.source_url);
    if (sourceURL) { const link = node('a', '', 'Открыть первоисточник ↗'); link.href = sourceURL; link.target = '_blank'; link.rel = 'noopener noreferrer'; const p = node('p'); p.append(link); source.append(p); }
    if (!section) { empty($('reader-text'), 'Нет доступных глав', 'Эта книга ещё не обработана. Проверьте состояние корпуса в библиотеке.'); return; }
    $('reader-section-title').textContent = section.title || 'Текст книги';
    const contributor = contributorLabel(section, book.author);
    $('reader-contributor').textContent = contributor;
    $('reader-contributor').classList.toggle('hidden', !contributor);
    $('reader-text').lang = book.language || 'und';
    const fullText = String(section.text || '');
    const highlight = passage && asID(passage.section_id) === asID(section.id);
    const start = Number(passage?.start); const end = Number(passage?.end);
    const exact = highlight && Number.isInteger(start) && Number.isInteger(end) && start >= 0 && end > start && end <= [...fullText].length;
    // API offsets count Unicode code points; JavaScript slice counts UTF-16 code units.
    const characters = exact ? Array.from(fullText) : null;
    if (exact && characters.slice(start, end).join('') === passage.text) {
      const mark = node('mark', '', characters.slice(start, end).join('')); mark.id = 'found-passage'; mark.tabIndex = -1;
      $('reader-text').replaceChildren(document.createTextNode(characters.slice(0, start).join('')), mark, document.createTextNode(characters.slice(end).join('')));
      const citation = $('reader-citation'); citation.replaceChildren(node('p', '', passage.citation || 'Найденный фрагмент выделен в тексте.'));
      citation.append(button('＋ Сохранить фрагмент', () => openSave({ kind: 'passage', passage_id: passage.id, title: `${book.title} · ${section.title || 'Фрагмент'}` }, passage.text)), button('Копировать цитату', () => copyText(`${passage.text}\n\n${passage.citation || ''}`)));
      citation.classList.remove('hidden');
      requestAnimationFrame(() => mark.scrollIntoView({ block: 'center', behavior: 'instant' }));
    } else {
      $('reader-text').textContent = fullText;
      $('reader-citation').classList.toggle('hidden', !highlight);
      if (highlight) $('reader-citation').textContent = 'Не удалось подтвердить положение фрагмента в этой версии главы. Прочитайте источник и проверьте цитату.';
    }
    const index = sections.findIndex((item) => asID(item.id) === asID(section.id));
    $('prev-chapter').disabled = index <= 0;
    $('next-chapter').disabled = index < 0 || index >= sections.length - 1;
    announce(`Открыта книга ${book.title}. ${section.title || 'Текст книги'}.`);
  }

  async function loadExplore(force = false) {
    if (state.exploreLoading) return;
    if (state.explore && !force) { renderExplore(); return; }
    state.exploreLoading = true;
    $('refresh-map').disabled = true;
    $('map-meta').textContent = 'Ищем связи между текстами…';
    $('theme-cloud').replaceChildren(node('div', 'loading-state', 'Составляем карту слов'));
    try {
      state.explore = await api('/api/explore');
      const languages = [...new Set(state.explore.books.map((book) => book.language))].sort();
      populateSelect($('map-language'), languages.map((value) => ({ value, label: labelLanguage(value) })), 'Все языки');
      populateMapBooks();
      if (!state.mapSelection) {
        const shared = [...state.explore.themes].sort((a, b) => b.count - a.count || b.weight - a.weight)[0];
        if (shared) state.mapSelection = { kind: 'theme', term: shared.term, language: shared.language };
      }
      if (state.explore.snapshot_changed) toast('Во время построения карты корпус обновился. Проверьте пояснение и обновите карту после индексации.');
      $('map-limitations').replaceChildren(...(state.explore.limitations || []).map((text) => node('li', '', text)));
      renderExplore();
      announce(`Карта готова. Книг: ${state.explore.books.length}, слов: ${state.explore.themes.length}.`);
    } catch (error) {
      showError(error, $('theme-cloud'));
      $('map-meta').textContent = 'Не удалось построить карту. Повторите обновление.';
    } finally { state.exploreLoading = false; $('refresh-map').disabled = false; }
  }
  function populateMapBooks() {
    if (!state.explore) return;
    const language = $('map-language').value;
    populateSelect($('map-book'), state.explore.books.filter((book) => !language || book.language === language).map((book) => ({ value: asID(book.id), label: `${book.title} · ${book.author}` })), 'Выберите книгу');
  }
  async function termSearch(term, language, bookId) {
    showView('search');
    setMode('lexical');
    $('query').value = term;
    $('language-filter').value = language || '';
    if (bookId) {
      const known = state.explore?.books.find((book) => asID(book.id) === asID(bookId));
      try { setScopeBook(known || await api(`/api/books/${encodeURIComponent(bookId)}`)); }
      catch (error) { showError(error); return; }
    } else setScopeBook(null);
    search();
  }
  function focusMapBook(bookId) {
    const book = state.explore?.books.find((item) => asID(item.id) === asID(bookId));
    if (!book) return;
    state.mapSelection = { kind: 'book', book_id: asID(book.id) };
    $('map-book').value = asID(book.id);
    renderExplore();
    announce(`Связи книги ${book.title}.`);
  }
  function mapBookRow(book) {
    const row = node('div', 'map-book-row');
    const title = button(book.title, () => focusMapBook(book.id), 'map-book-title');
    title.setAttribute('aria-label', `Исследовать связи: ${book.title}`);
    row.append(title, node('p', '', book.author), button('Читать ↗', () => openBook(book.id)));
    return row;
  }
  function renderExplore() {
    const map = state.explore;
    if (!map) return;
    const language = $('map-language').value;
    const books = map.books.filter((book) => !language || book.language === language);
    const themes = map.themes.filter((theme) => !language || theme.language === language);
    if (state.mapSelection?.kind !== 'book' && !themes.some((theme) => theme.term === state.mapSelection?.term && theme.language === state.mapSelection?.language)) {
      const first = [...themes].sort((a, b) => b.count - a.count || b.weight - a.weight)[0];
      state.mapSelection = first ? { kind: 'theme', term: first.term, language: first.language } : null;
    }
    const visibleIds = new Set(books.map((book) => asID(book.id)));
    const edges = map.edges.filter((edge) => visibleIds.has(asID(edge.source)));
    $('map-meta').textContent = `${formatNumber(books.length)} изданий на карте · ${formatNumber(themes.length)} характерных слов · ${formatNumber(edges.length)} связей · Сходство словаря${map.truncated ? ' · Выборка из ' + formatNumber(map.corpus_books) + ' книг' : ''}`;
    const cloud = $('theme-cloud');
    if (!themes.length) empty(cloud, 'Для карты пока мало текста', 'Добавьте книги или выберите другой язык. Карта строится только по реально индексированным фрагментам.');
    else {
      const maxCount = Math.max(...themes.map((theme) => theme.count), 1);
      cloud.replaceChildren(...themes.map((theme) => {
        const active = state.mapSelection?.kind === 'theme' && state.mapSelection.term === theme.term && state.mapSelection.language === theme.language;
        const chip = button('', () => { state.mapSelection = { kind: 'theme', term: theme.term, language: theme.language }; $('map-book').value = ''; renderExplore(); announce(`Слово ${theme.term}: ${theme.count} изданий.`); }, `theme-chip${active ? ' active' : ''}`);
        chip.style.setProperty('--theme-size', `${13 + 8 * theme.count / maxCount}px`);
        chip.setAttribute('aria-pressed', String(active));
        chip.setAttribute('aria-label', `${theme.term}, ${labelLanguage(theme.language)}, изданий: ${theme.count}`);
        chip.append(node('span', '', theme.term), node('small', '', String(theme.count)));
        chip.lang = theme.language;
        if (!language) chip.title = labelLanguage(theme.language);
        return chip;
      }));
    }
    const evidence = $('map-evidence'); evidence.replaceChildren();
    if (!map.books.length) { empty(evidence, 'Начните с книги', 'Импортируйте демонстрационный корпус, чтобы исследовать связи.'); return; }
    if (state.mapSelection?.kind === 'book') {
      const book = map.books.find((item) => asID(item.id) === state.mapSelection.book_id);
      if (book) renderBookEvidence(evidence, book);
      return;
    }
    const selected = themes.find((theme) => theme.term === state.mapSelection?.term && theme.language === state.mapSelection?.language) || themes[0];
    if (!selected) return;
    state.mapSelection = { kind: 'theme', term: selected.term, language: selected.language };
    evidence.append(node('p', 'eyebrow', 'СЛОВО В ИСТОЧНИКАХ'), node('h2', '', selected.term), node('p', 'map-evidence-description', `${labelLanguage(selected.language)} · ${formatNumber(selected.count)} изданий в карте`));
    evidence.append(button('Найти фрагменты ↗', () => termSearch(selected.term, selected.language), 'button primary'));
    const list = node('div', 'map-book-list');
    selected.books.forEach((id) => { const book = map.books.find((item) => asID(item.id) === asID(id)); if (book) list.append(mapBookRow(book)); });
    evidence.append(list);
  }
  function renderBookEvidence(target, book) {
    target.append(node('p', 'eyebrow', 'МАРШРУТ ОТ КНИГИ'), node('h2', '', book.title), node('p', 'map-evidence-description', `${book.author} · ${labelLanguage(book.language)}`));
    target.append(button('Открыть книгу ↗', () => openBook(book.id), 'button primary'));
    const terms = node('div', 'map-book-terms');
    (book.terms || []).slice(0, 8).forEach(({ term }) => terms.append(button(term, () => termSearch(term, book.language, book.id), 'map-term-link')));
    target.append(terms, node('h3', 'map-related-title', 'Связанные книги'));
    const related = state.explore.edges.filter((edge) => asID(edge.source) === asID(book.id) || asID(edge.target) === asID(book.id)).sort((a, b) => b.score - a.score).slice(0, 3);
    if (!related.length) { target.append(node('p', 'map-evidence-description', 'Достаточного совпадения словаря с другими книгами этого языка пока нет.')); return; }
    related.forEach((edge) => {
      const otherId = asID(edge.source) === asID(book.id) ? edge.target : edge.source;
      const other = state.explore.books.find((item) => asID(item.id) === asID(otherId));
      if (!other) return;
      const relation = node('div', 'map-relation');
      relation.append(mapBookRow(other), node('p', 'map-score', `Сходство словаря: ${Number(edge.score).toFixed(3)}`));
      const shared = node('div', 'map-shared-terms');
      shared.append(node('span', '', 'Общие слова: '));
      edge.shared_terms.forEach((term) => shared.append(button(term, () => termSearch(term, book.language), 'map-term-link')));
      relation.append(shared); target.append(relation);
    });
  }

  function collectionNames() { return [...new Set(state.saved.map((item) => item.collection).filter(Boolean))].sort(); }
  async function refreshSaved() {
    const data = await api('/api/saved'); state.saved = data.items || [];
    $('saved-count').textContent = formatNumber(state.saved.length);
    const names = collectionNames();
    populateSelect($('collection-filter'), names.map((value) => ({ value, label: value })), 'Все коллекции');
    $('collection-options').replaceChildren(...names.map((name) => { const option = node('option'); option.value = name; return option; }));
    if (state.view === 'saved') renderSaved();
  }
  function openSave(draft, preview) {
    state.saveDraft = draft;
    $('save-title').value = draft.title || '';
    $('save-collection').value = '';
    $('save-note').value = '';
    $('save-preview').textContent = textExcerpt(preview || draft.query || '', 350);
    $('save-error').classList.add('hidden');
    $('save-dialog').showModal();
    $('save-title').focus();
  }
  async function saveItem(event) {
    event.preventDefault();
    if (!state.saveDraft) return;
    const submit = $('save-form').querySelector('[type="submit"]'); submit.disabled = true;
    try {
      await api('/api/saved', { method: 'POST', body: JSON.stringify({ ...state.saveDraft, title: $('save-title').value.trim(), collection: $('save-collection').value.trim(), note: $('save-note').value.trim() }) });
      $('save-dialog').close(); state.saveDraft = null;
      await refreshSaved(); toast('Сохранено в исследовательский блокнот.');
    } catch (error) { $('save-error').textContent = error.message; $('save-error').classList.remove('hidden'); }
    finally { submit.disabled = false; }
  }
  function renderSaved() {
    const collection = $('collection-filter').value;
    const items = state.saved.filter((item) => !collection || item.collection === collection);
    if (!items.length) { empty($('saved-items'), collection ? 'В коллекции пока пусто' : 'Дайте мысли продолжение', 'Сохраните фрагмент из поисковой выдачи или читателя. Добавьте заметку и объедините находки в тематическую коллекцию.'); return; }
    $('saved-items').replaceChildren(...items.map((item) => {
      const card = node('article', 'saved-item');
      const date = item.created_at ? new Date(item.created_at).toLocaleDateString('ru-RU') : '';
      card.append(node('p', 'saved-kind', `${item.kind === 'query' ? 'ЗАПРОС' : 'ФРАГМЕНТ'}${item.collection ? ' / ' + item.collection : ''}${date && date !== 'Invalid Date' ? ' · ' + date : ''}`), node('h2', '', item.title));
      if (item.text) card.append(node('blockquote', '', item.text));
      else if (item.query) card.append(node('p', '', item.query));
      if (item.citation) card.append(node('p', 'passage-citation', item.citation));
      if (item.note) card.append(node('p', 'saved-note', item.note));
      const actions = node('div', 'saved-actions');
      if (item.kind === 'query') actions.append(button('Продолжить поиск ↗', () => { showView('search'); $('query').value = item.query || item.title; search(); }));
      else if (item.passage_id) actions.append(button('Открыть в контексте ↗', async () => {
        try { const passage = await api(`/api/passages/${encodeURIComponent(item.passage_id)}`); await openBook(passage.book_id, passage.section_id, passage.id); }
        catch (error) { showError(error); }
      }));
      actions.append(button('Копировать', () => copyText([item.text || item.query || item.title, item.citation, item.note].filter(Boolean).join('\n\n'))), button('Удалить', async (event) => {
        const target = event.currentTarget;
        if (target.dataset.confirm !== 'true') { target.dataset.confirm = 'true'; target.textContent = 'Подтвердить удаление'; setTimeout(() => { if (target.isConnected) { target.dataset.confirm = ''; target.textContent = 'Удалить'; } }, 4000); return; }
        target.disabled = true;
        try { await api(`/api/saved/${encodeURIComponent(item.id)}`, { method: 'DELETE' }); await refreshSaved(); toast('Запись удалена.'); }
        catch (error) { target.disabled = false; showError(error); }
      }, 'text-button delete-item'));
      card.append(actions); return card;
    }));
  }
  async function copyText(value) {
    try { await navigator.clipboard.writeText(value); toast('Скопировано в буфер обмена.'); }
    catch {
      const textarea = node('textarea'); textarea.value = value;
      textarea.style.position = 'fixed'; textarea.style.opacity = '0'; document.body.append(textarea); textarea.select();
      const copied = document.execCommand('copy'); textarea.remove();
      toast(copied ? 'Текст скопирован.' : 'Браузер не разрешил копирование. Выделите текст и скопируйте вручную.');
    }
  }
  function exportSaved() {
    const collection = $('collection-filter').value;
    const data = { app: 'Book Atlas', exported_at: new Date().toISOString(), collection: collection || null, items: state.saved.filter((item) => !collection || item.collection === collection) };
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }));
    const link = node('a'); link.href = url; link.download = `book-atlas-notes-${new Date().toISOString().slice(0, 10)}.json`; document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function exportMarkdown() {
    const collection = $('collection-filter').value;
    const params = new URLSearchParams();
    if (collection) params.set('collection', collection);
    const link = node('a');
    link.href = `/api/saved/export${params.size ? '?' + params : ''}`;
    link.download = 'book-atlas-collection.md';
    document.body.append(link); link.click(); link.remove();
  }
  async function route() {
    const params = new URLSearchParams(location.search);
    if (params.get('book')) { await openBook(params.get('book'), params.get('section'), params.get('passage'), false); return; }
    showView(params.get('view') || 'search', false);
    if (params.has('q')) { $('query').value = params.get('q'); await search(false); }
  }

  $('search-form').addEventListener('submit', (event) => { event.preventDefault(); search(); });
  document.querySelectorAll('[data-mode]').forEach((el) => el.addEventListener('click', () => { setMode(el.dataset.mode); if (state.query) search(); }));
  document.querySelectorAll('[data-query]').forEach((el) => el.addEventListener('click', () => { $('query').value = el.dataset.query; search(); }));
  $('refresh-map').addEventListener('click', () => loadExplore(true));
  $('map-language').addEventListener('change', () => { state.mapSelection = null; populateMapBooks(); renderExplore(); });
  $('map-book').addEventListener('change', () => { if ($('map-book').value) focusMapBook($('map-book').value); else { state.mapSelection = null; renderExplore(); } });
  $('search-extra-mode').addEventListener('change', () => { setMode($('search-extra-mode').value || 'hybrid'); if (state.query) search(); });
  $('fragments-tab').addEventListener('click', () => setPanel('fragments'));
  $('research-tab').addEventListener('click', () => setPanel('research'));
  $('run-research').addEventListener('click', research);
  $('language-filter').addEventListener('change', () => { loadScopeBooks(); if (state.query) search(); });
  $('book-filter').addEventListener('change', () => { const id = $('book-filter').value; setScopeBook(id ? state.scopeBooks.find((book) => asID(book.id) === id) || state.scopeBook : null); if (state.query) search(); });
  $('book-scope-query').addEventListener('input', debounce(loadScopeBooks));
  $('book-scope-query').addEventListener('focus', () => { if (!state.scopeLoaded) loadScopeBooks(); });
  $('book-filter').addEventListener('focus', () => { if (!state.scopeLoaded) loadScopeBooks(); });
  $('compare-query').addEventListener('input', debounce(loadCompareBooks));
  $('compare-query').addEventListener('focus', () => { if (!state.compareLoaded) loadCompareBooks(); });
  $('clear-filters').addEventListener('click', () => { $('language-filter').value = ''; $('book-scope-query').value = ''; setScopeBook(null); loadScopeBooks(); if (state.query) search(); });
  $('save-query').addEventListener('click', () => { if (state.query) openSave({ kind: 'query', title: state.query, query: state.query }, state.query); });
  $('save-form').addEventListener('submit', saveItem);
  $('close-save').addEventListener('click', () => $('save-dialog').close());
  $('save-dialog').addEventListener('click', (event) => { if (event.target === $('save-dialog')) { const bounds = $('save-dialog').getBoundingClientRect(); if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) $('save-dialog').close(); } });
  $('collection-filter').addEventListener('change', renderSaved);
  $('export-saved').addEventListener('click', exportSaved);
  $('export-markdown').addEventListener('click', exportMarkdown);
  $('library-query').addEventListener('input', debounce(() => loadLibrary(0)));
  $('library-language').addEventListener('change', () => loadLibrary(0));
  $('library-status').addEventListener('change', () => loadLibrary(0));
  $('library-prev').addEventListener('click', () => loadLibrary(Math.max(0, state.libraryOffset - 48)));
  $('library-next').addEventListener('click', () => loadLibrary(state.libraryOffset + 48));
  $('reload-library').addEventListener('click', refreshLibrary);
  $('reader-back').addEventListener('click', () => showView(state.previousView));
  $('prev-chapter').addEventListener('click', () => { const sections = sectionList(state.currentBook); const index = sections.findIndex((item) => asID(item.id) === asID(state.currentSection?.id)); if (index > 0) selectChapter(sections[index - 1].id); });
  $('next-chapter').addEventListener('click', () => { const sections = sectionList(state.currentBook); const index = sections.findIndex((item) => asID(item.id) === asID(state.currentSection?.id)); if (index >= 0 && index < sections.length - 1) selectChapter(sections[index + 1].id); });
  $('font-smaller').addEventListener('click', () => { state.readerSize = Math.max(15, state.readerSize - 1); document.documentElement.style.setProperty('--reader-size', `${state.readerSize}px`); });
  $('font-larger').addEventListener('click', () => { state.readerSize = Math.min(30, state.readerSize + 1); document.documentElement.style.setProperty('--reader-size', `${state.readerSize}px`); });
  window.addEventListener('popstate', route);
  document.addEventListener('keydown', (event) => {
    if (event.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName) && !$('save-dialog').open && !event.ctrlKey && !event.metaKey) { event.preventDefault(); if (state.view !== 'search') showView('search'); $('query').focus(); }
  });

  async function initialize() {
    await Promise.allSettled([refreshLibrary(), refreshSaved().catch(showError)]);
    await route();
  }
  initialize();
})();
