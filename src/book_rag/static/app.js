'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const state = {
    view: 'search', mode: 'hybrid', query: '', books: [], saved: [], results: [], stats: null,
    currentBook: null, currentSection: null, currentPassage: null,
    saveDraft: null, saveItemID: null, saveOriginal: null, savePending: false, saveReturnFocus: null,
    searchSequence: 0, readerSequence: 0, readerSize: 20,
    previousView: 'search', researchQuery: '', toastTimer: null,
    explore: null, exploreLoading: false, mapSelection: null,
    landingTotal: 0, libraryOffset: 0, libraryTotal: 0, libraryHasMore: false, libraryRequest: 0,
    scopeBook: null, scopeBooks: [], scopeTotal: 0, scopeLoaded: false, scopeRequest: 0,
    compareBooks: [], compareTotal: 0, compareLoaded: false, compareRequest: 0, researchSelection: new Map(),
    starterLanguage: '', starterCache: new Map(), starterPending: new Map(), starterVersion: 0,
    panel: 'fragments', answerSequence: 0, answerController: null, answerResponse: null,
    answerRouteSequence: 0, answerScopeBlocked: null, answerScopeLoading: false,
  };
  const languageNames = { ru: 'Русский', en: 'English', fr: 'Français', de: 'Deutsch', es: 'Español', it: 'Italiano', nl: 'Nederlands', fi: 'Suomi', sv: 'Svenska', da: 'Dansk', zh: '中文', ja: '日本語', la: 'Latina', el: 'Ελληνικά', pt: 'Português', uk: 'Українська', ar: 'العربية' };
  const statusNames = { discovered: 'Найдены', downloaded: 'Скачаны', processed: 'Обработаны', indexed: 'В поиске', searchable: 'В поиске', failed: 'Пока недоступны · ошибка', deferred: 'Пока недоступны · отложены', error: 'Требуют внимания', duplicate: 'Дубли' };
  const ingestionNames = { discovered: 'Ожидают загрузки', downloaded: 'Файл получен', processed: 'Текст извлечён', indexed: 'Завершены', failed: 'Не удались', deferred: 'Отложены', duplicate: 'Дубли' };
  // These are catalogue lookup hints, never fabricated book cards. Every match
  // must have the requested language, title, author and searchable status.
  const starterSeeds = {
    ru: [
      { q: 'Записки из подполья', title: ['записки из подполья'], author: ['достоевский'] },
      { q: 'Отцы и дети', title: ['отцы и дети'], author: ['тургенев'] },
      { q: 'Смерть Ивана Ильича', title: ['смерть ивана ильича'], author: ['толстой'] },
      { q: 'Палата', title: ['палата'], author: ['чехов'] },
      { q: 'Шинель', title: ['шинель'], author: ['гоголь'] },
      { q: 'Пушкин', titles: [['пиковая дама'], ['красавице']], author: ['пушкин'] },
    ],
    en: [
      { q: 'Wonderland', title: ['alice', 'wonderland'], author: ['carroll'] },
      { q: 'Pride and Prejudice', title: ['pride and prejudice'], author: ['austen'] },
      { q: 'Origin of Species', title: ['origin of species'], author: ['darwin'] },
      { q: 'Republic', title: ['republic'], author: ['plato'] },
      { q: 'Meditations', title: ['meditations'], author: ['aurelius'] },
      { q: 'Wealth of Nations', title: ['wealth of nations'], author: ['smith'] },
    ],
    fr: [
      { q: 'Candide', title: ['candide'], author: ['voltaire'] },
      { q: 'Bovary', title: ['madame bovary'], author: ['flaubert'] },
      { q: 'Fables', title: ['fables'], author: ['la fontaine'] },
      { q: 'Discours de la méthode', title: ['discours de la méthode'], author: ['descartes'] },
    ],
    de: [
      { q: 'Verwandlung', title: ['verwandlung'], author: ['kafka'] },
      { q: 'Faust', title: ['faust'], author: ['goethe'] },
      { q: 'Zarathustra', title: ['zarathustra'], author: ['nietzsche'] },
      { q: 'reinen Vernunft', title: ['kritik', 'reinen vernunft'], author: ['kant'] },
    ],
  };

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
    catch (error) { if (error.name === 'AbortError') throw error; throw new Error('Не удалось связаться с библиотекой. Проверьте, запущен ли сервер, и повторите действие.'); }
    let data;
    try { data = await response.json(); }
    catch { throw new Error(`Сервер вернул неожиданный ответ (${response.status}).`); }
    if (!response.ok) {
      const detail = typeof data.detail === 'string' ? data.detail : data.error || `Не удалось выполнить запрос (${response.status}).`;
      const error = new Error(detail); error.status = response.status; throw error;
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
  function artifactDownload(value) {
    if (typeof value !== 'string' || !value.trim()) return null;
    try {
      const url = new URL(value, location.origin);
      if (url.origin !== location.origin || url.username || url.password || url.search || url.hash || !/^\/api\/artifacts\/[a-f0-9]{64}$/.test(url.pathname)) return null;
      const link = node('a', 'text-button artifact-download', 'Скачать зафиксированный файл');
      link.href = url.pathname;
      // The verified attachment response supplies the digest and real format filename.
      link.download = '';
      return link;
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
    if (view !== 'reader') state.readerSequence++;
    if (push || view !== 'search') stopScopeRestore();
    if (view !== 'search') cancelAnswer('Ожидание остановлено. Отправьте вопрос, чтобы повторить.');
    state.view = view;
    document.querySelectorAll('.view').forEach((el) => el.classList.toggle('hidden', el.id !== `${view}-view`));
    updateNav(view === 'reader' ? '' : view);
    document.title = `${({ search: 'Поиск и исследование', library: 'Библиотека', saved: 'Сохранённое', reader: 'Читатель', explore: 'Карта тем' })[view]} — Атлас книг`;
    if (push && view !== 'reader') {
      const params = new URLSearchParams();
      if (view !== 'search') params.set('view', view);
      if (view === 'library') {
        if ($('library-search-mode').value === 'description') params.set('book_mode', 'description');
        if ($('library-query').value.trim()) params.set('book_q', $('library-query').value.trim());
        if ($('library-language').value) params.set('language', $('library-language').value);
      }
      if (view === 'search' && state.query) params.set('q', state.query);
      if (view === 'search' && state.panel === 'answer') params.set('panel', 'answer');
      if (view === 'search' && state.panel === 'answer' && $('book-filter').value) params.set('scope', $('book-filter').value);
      if (view === 'search' && state.panel === 'answer' && $('language-filter').value) params.set('language', $('language-filter').value);
      history.pushState({}, '', params.size ? `/?${params}` : '/');
    }
    if (view === 'library') loadLibrary();
    if (view === 'saved') renderSaved();
    if (view === 'explore') loadExplore();
    if (view === 'search' && !$('discovery').classList.contains('hidden') && !new URLSearchParams(location.search).has('q')) loadStarterShelf();
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
  function setPanel(panel, updateURL = true) {
    if (!['fragments', 'research', 'answer'].includes(panel)) panel = 'fragments';
    if (updateURL) stopScopeRestore();
    if (panel !== 'answer') cancelAnswer('Ожидание остановлено. Отправьте вопрос, чтобы повторить.');
    state.panel = panel;
    for (const name of ['fragments', 'research', 'answer']) {
      $(`${name}-panel`).classList.toggle('hidden', panel !== name);
      $(`${name}-tab`).classList.toggle('active', panel === name);
      $(`${name}-tab`).setAttribute('aria-pressed', String(panel === name));
    }
    $('results-meta').classList.toggle('hidden', panel === 'answer');
    $('search-warnings').classList.toggle('hidden', panel === 'answer' || !$('search-warnings').textContent.trim());
    const coverage = $('semantic-coverage');
    if (coverage) coverage.classList.toggle('hidden', panel !== 'fragments' || coverage.dataset.active !== 'true');
    if (panel === 'answer') {
      $('search-results').classList.remove('hidden'); $('discovery').classList.add('hidden');
      if (!$('answer-query').value.trim()) $('answer-query').value = $('query').value.trim();
      updateAnswerScope();
    }
    if (updateURL && state.view === 'search') {
      const params = new URLSearchParams(location.search);
      params.delete('book'); params.delete('section'); params.delete('passage'); params.delete('view');
      if (panel === 'answer') { updateAnswerURL(); return; }
      params.delete('panel'); params.delete('scope'); params.delete('language');
      history.replaceState({}, '', params.size ? `/?${params}` : '/');
    }
  }

  function coverageType(book) { return book?.coverage || book?.source_metadata?.coverage || ''; }
  function withoutIllustrations(book) {
    const metadata = book?.source_metadata || {};
    return book?.illustrations_included === false || metadata.illustrations_included === false ||
      book?.source_variant?.illustrations_included === false || metadata.source_variant?.illustrations_included === false;
  }
  function ingestionNotice(item, compact = false) {
    const status = item?.ingestion_status;
    const available = bookSearchable(item) || item?.verified === true;
    let message = '';
    if (status === 'failed') message = available ? 'Обновление не удалось · прежнее издание доступно' : 'Загрузка не удалась · текст пока недоступен';
    else if (status === 'deferred') message = available ? 'Обновление отложено · прежнее издание доступно' : 'Загрузка отложена · текст пока недоступен';
    else if (available && ['discovered', 'downloaded', 'processed'].includes(status)) message = 'Источник обновляется · прежнее издание доступно';
    else if (typeof item?.ingestion_warning === 'string') message = item.ingestion_warning;
    if (!message) return null;
    const notice = node('div', compact ? 'ingestion-notice ingestion-compact' : 'ingestion-notice');
    notice.dataset.stage = status || '';
    notice.append(node('p', 'ingestion-message', message));
    if (!compact) {
      if (available) notice.append(node('p', 'ingestion-explanation', 'Текст и цитаты относятся к последнему успешно проиндексированному изданию.'));
      if (item.ingestion_updated_at) {
        const value = String(item.ingestion_updated_at);
        const date = new Date(/(?:Z|[+-]\d\d:\d\d)$/.test(value) ? value : value.replace(' ', 'T') + 'Z');
        if (!Number.isNaN(date.getTime())) notice.append(node('p', 'ingestion-date', `Последняя попытка: ${date.toLocaleString('ru-RU')}`));
      }
      if (item.ingestion_error) { const details = node('details', 'ingestion-error'); details.append(node('summary', '', 'Причина последней попытки'), node('p', '', item.ingestion_error)); notice.append(details); }
    }
    return notice;
  }
  function createBookCard(book, index) {
    const card = node('article', 'book-card');
    card.dataset.bookId = asID(book.id);
    const cover = node('div', `book-cover cover-${index % 6}`);
    cover.setAttribute('aria-hidden', 'true');
    cover.append(node('span', 'cover-title', book.title));
    const info = node('div', 'book-card-info');
    const language = node('div', 'book-language');
    language.append(node('span', '', labelLanguage(book.language)), node('span', 'book-status', bookSearchable(book) ? '● В поиске' : statusNames[book.status] || book.status || 'В библиотеке'));
    const title = node('h3', '', book.title); title.title = book.title;
    info.append(language, title, node('p', 'book-author', book.author));
    const selectedChapters = coverageType(book) === 'selected_chapters';
    const excerpt = coverageType(book) === 'excerpt' || /extract|excerpt|отрывок|фрагмент/i.test(book.edition || '');
    if (selectedChapters) info.append(node('p', 'book-excerpt-label', 'Выбранные главы'));
    else if (excerpt) info.append(node('p', 'book-excerpt-label', 'Отрывок произведения'));
    else if (coverageType(book) === 'partial') info.append(node('p', 'book-excerpt-label', 'Полнота произведения не подтверждена'));
    if (withoutIllustrations(book)) info.append(node('p', 'book-excerpt-label', 'Без иллюстраций'));
    const attempt = ingestionNotice(book, true);
    if (attempt) info.append(attempt);
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
  function starterLanguages() {
    const languages = state.stats?.languages;
    const codes = Array.isArray(languages) ? languages.map((item) => typeof item === 'string' ? item : item.language) : Object.keys(languages || {});
    return Object.keys(starterSeeds).filter((code) => codes.includes(code));
  }
  function starterText(value) { return String(value || '').normalize('NFKC').toLocaleLowerCase().replaceAll('ё', 'е'); }
  function starterMatch(book, seed, language) {
    const titles = seed.titles || [seed.title];
    return bookSearchable(book) && book.language === language && titles.some((terms) => terms.every((term) => starterText(book.title).includes(term))) && seed.author.every((term) => starterText(book.author).includes(term));
  }
  function starterPreference(book) {
    const full = ['full', 'complete poem'].includes(coverageType(book));
    return (full ? 3 : coverageType(book) ? 0 : 1) + (book.work_identity_status === 'curated' ? 0.1 : 0);
  }
  function scopeStarterBook(book) {
    // Opening a scope never starts an embedding request or reuses an old query.
    state.searchSequence++; state.scopeRequest++;
    state.query = ''; $('query').value = ''; $('book-scope-query').value = '';
    $('language-filter').value = book.language;
    state.scopeBooks = [book]; state.scopeTotal = 1; state.scopeLoaded = false; setScopeBook(book);
    showView('search'); setPanel('fragments');
    $('discovery').classList.add('hidden'); $('search-results').classList.remove('hidden');
    $('search-warnings').classList.add('hidden'); renderSemanticCoverage(null);
    $('results-meta').textContent = `Поиск в книге: ${book.title}`;
    empty($('fragments-panel'), 'Что найти в этой книге?', 'Введите цитату, ситуацию или мысль. Поиск будет ограничен выбранным изданием.');
    loadScopeBooks(); $('query').focus();
  }
  function starterCard(book, index) {
    const card = createBookCard(book, index); card.classList.add('starter-card'); card.dataset.bookId = asID(book.id);
    const cover = card.querySelector('.book-cover'); cover.replaceChildren(node('span', '', String(index + 1).padStart(2, '0')));
    const title = card.querySelector('h3'); title.lang = book.language;
    const edition = node('p', 'starter-edition', book.edition || 'Издание из текущего каталога'); title.nextElementSibling.after(edition);
    const footer = card.querySelector('.book-card-footer'); footer.replaceChildren();
    const read = node('a', 'book-open', 'Читать ↗'); read.href = '/?' + new URLSearchParams({ book: book.id }); read.setAttribute('aria-label', `Читать: ${book.title}`);
    read.addEventListener('click', (event) => { if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return; event.preventDefault(); openBook(book.id); });
    const searchBook = button('Искать в книге', () => scopeStarterBook(book), 'starter-search'); searchBook.setAttribute('aria-label', `Искать в книге: ${book.title}`);
    footer.append(read, searchBook); return card;
  }
  function renderStarterShelf() {
    const language = state.starterLanguage; const data = state.starterCache.get(language);
    const available = starterLanguages();
    $('starter-shelf').classList.toggle('hidden', !available.length);
    document.querySelectorAll('[data-shelf-language]').forEach((el) => { el.disabled = !available.includes(el.dataset.shelfLanguage); el.setAttribute('aria-pressed', String(el.dataset.shelfLanguage === language)); });
    $('starter-catalog').href = '/?' + new URLSearchParams({ view: 'library', language });
    $('starter-context').textContent = language === 'ru' ? 'Русские произведения на русском языке. На полке — лишь часть каталога.' : 'Издания на выбранном языке, включая переводы. На полке — лишь часть каталога.';
    const target = $('starter-books'); const status = $('starter-status'); status.replaceChildren();
    target.setAttribute('aria-busy', String(!data));
    if (!available.length) return;
    if (!data) { status.textContent = 'Проверяем, какие книги доступны…'; loading(target, 'Собираем полку из каталога'); return; }
    if (data.books.length) { target.replaceChildren(...data.books.map(starterCard)); status.textContent = `На полке: ${formatNumber(data.books.length)} изданий.`; }
    else empty(target, data.errors ? 'Полку пока не удалось загрузить' : 'На этой полке пока нет знакомых названий', 'Все доступные книги на этом языке можно посмотреть в каталоге.');
    if (data.errors) {
      status.append(node('span', '', ' Часть запросов к каталогу не выполнена. '), button('Повторить', () => { state.starterCache.delete(language); loadStarterShelf(language); }));
    }
  }
  async function loadStarterShelf(language = state.starterLanguage) {
    const available = starterLanguages();
    state.starterLanguage = available.includes(language) ? language : available[0] || '';
    language = state.starterLanguage; renderStarterShelf();
    if (!language || state.starterCache.has(language) || state.starterPending.has(language)) return;
    const version = state.starterVersion;
    const pending = Promise.allSettled(starterSeeds[language].map(async (seed) => {
      const params = new URLSearchParams({ status: 'indexed', language, q: seed.q, limit: '8', offset: '0' });
      const result = await api(`/api/books?${params}`);
      return (result.books || []).filter((book) => starterMatch(book, seed, language)).sort((a, b) => starterPreference(b) - starterPreference(a))[0] || null;
    }));
    state.starterPending.set(language, pending);
    const results = await pending;
    if (version !== state.starterVersion) return;
    state.starterPending.delete(language);
    const books = results.filter((result) => result.status === 'fulfilled' && result.value).map((result) => result.value);
    state.starterCache.set(language, { books: books.filter((book, index) => books.findIndex((other) => asID(other.id) === asID(book.id)) === index), errors: results.some((result) => result.status === 'rejected') });
    if (state.starterLanguage === language) renderStarterShelf();
  }
  function shelfCatalogFilters(language) {
    $('library-query').value = ''; $('library-language').value = language;
    $('library-status').value = 'indexed'; $('library-ingestion-status').value = ''; state.libraryOffset = 0;
  }
  function renderScopeBooks() {
    const selected = state.scopeBook;
    const matches = [...state.scopeBooks];
    if (selected && !matches.some((book) => asID(book.id) === asID(selected.id))) matches.unshift(selected);
    populateSelect($('book-filter'), matches.map((book) => ({ value: asID(book.id), label: `${book.title} · ${book.author}` })), 'Вся библиотека');
    if (state.answerScopeBlocked) { const unresolved = node('option', '', state.answerScopeLoading ? 'Проверяем книгу из ссылки…' : 'Книга из ссылки не подтверждена'); unresolved.value = state.answerScopeBlocked; unresolved.disabled = true; $('book-filter').prepend(unresolved); }
    $('book-filter').value = state.answerScopeBlocked || (selected ? asID(selected.id) : '');
    $('book-scope-status').textContent = state.scopeTotal > state.scopeBooks.length
      ? `Показаны ${formatNumber(state.scopeBooks.length)} из ${formatNumber(state.scopeTotal)} изданий. Уточните название или автора.`
      : `Подходящих изданий: ${formatNumber(state.scopeTotal)}.`;
    const selection = $('book-scope-selection'); selection.replaceChildren();
    selection.classList.toggle('hidden', !selected && !state.answerScopeBlocked);
    if (state.answerScopeBlocked) selection.append(node('span', '', 'Ограничение по книге пока не подтверждено. Выберите книгу или явно снимите ограничение.'), button('Снять ограничение ×', () => { setScopeBook(null); scopeChanged(); }));
    if (selected) selection.append(node('span', '', `Поиск в книге: ${selected.title} · ${selected.author}`), button('Снять ограничение ×', () => { setScopeBook(null); scopeChanged(); }));
  }
  function setScopeBook(book, invalidateRoute = true) {
    if (invalidateRoute) stopScopeRestore();
    state.answerScopeBlocked = null; state.answerScopeLoading = false;
    state.scopeBook = book; renderScopeBooks(); answerBusy(Boolean(state.answerController));
  }
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
    $('stat-books').textContent = formatNumber(stats.searchable_books ?? stats.books);
    $('stat-works').textContent = formatNumber(stats.works);
    const provisional = stats.work_count_is_provisional !== false;
    let scope = provisional
      ? `Группировка изданий предварительная: ${formatNumber(stats.works)} групп пока не подтверждают число уникальных произведений. Проверено групп: ${formatNumber(stats.curated_work_groups)}. Периодических выпусков: ${formatNumber(stats.periodical_editions)}.`
      : `Связанные издания объединены в ${formatNumber(stats.works)} групп произведений по проверенным метаданным. Периодических выпусков: ${formatNumber(stats.periodical_editions)}.`;
    if (Number.isInteger(stats.confirmed_full_works_lower_bound)) scope += ` Подтверждённых полных произведений — не менее ${formatNumber(stats.confirmed_full_works_lower_bound)}.`;
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
    const availability = node('section', 'pipeline-group'); availability.setAttribute('aria-label', 'Доступность изданий');
    availability.append(node('h2', '', 'Доступность изданий'));
    const availabilityCounts = node('div', 'pipeline-counts');
    const available = node('div', 'pipeline-stat pipeline-available'); available.dataset.status = 'indexed';
    available.append(node('b', '', formatNumber(stats.searchable_books ?? stats.books)), node('span', '', 'Доступны в поиске'));
    availabilityCounts.append(available);
    Object.entries(counts).filter(([status]) => !['indexed', 'searchable'].includes(status)).forEach(([status, count]) => {
      const box = node('div', 'pipeline-stat'); box.dataset.status = status;
      box.append(node('b', '', formatNumber(count)), node('span', '', statusNames[status] || status)); availabilityCounts.append(box);
    });
    availability.append(availabilityCounts);
    const attempts = node('section', 'pipeline-group'); attempts.setAttribute('aria-label', 'Последние попытки получения источников');
    attempts.append(node('h2', '', 'Загрузка и обработка · последняя попытка'));
    if (stats.ingestion_status_counts && typeof stats.ingestion_status_counts === 'object') {
      const attemptCounts = node('div', 'pipeline-counts');
      Object.entries(stats.ingestion_status_counts).forEach(([status, count]) => {
        const box = node('div', 'pipeline-stat'); box.dataset.ingestionStatus = status;
        box.append(node('b', '', formatNumber(count)), node('span', '', ingestionNames[status] || status)); attemptCounts.append(box);
      });
      if (!attemptCounts.childElementCount) { const box = node('div', 'pipeline-stat'); box.append(node('b', '', '0'), node('span', '', 'Записей о попытках')); attemptCounts.append(box); }
      attempts.append(attemptCounts, node('p', 'pipeline-explanation', 'Учтена одна последняя попытка на издание. Ошибка обновления сохраняет доступность прежнего издания.'));
    } else attempts.append(node('p', 'pipeline-explanation', 'Сведения о последних попытках пока недоступны.'));
    const refresh = node('div', 'pipeline-refresh');
    refresh.append(node('span', '', `Состояние на ${new Date().toLocaleTimeString('ru-RU')}`), button('Обновить счётчики', async (event) => {
      const trigger = event.currentTarget; trigger.disabled = true;
      try { await refreshLibrary(); if (state.view === 'library') await loadLibrary(); }
      finally { trigger.disabled = false; }
    }));
    $('pipeline-stats').replaceChildren(availability, attempts, refresh);
    $('library-ingestion-status').classList.toggle('hidden', !stats.ingestion_status_counts);
  }
  async function loadLibrary(offset = state.libraryOffset) {
    const request = ++state.libraryRequest;
    const description = $('library-search-mode').value === 'description';
    const query = $('library-query').value.trim();
    if (state.view === 'library') {
      const route = new URLSearchParams({ view: 'library' });
      if (description) route.set('book_mode', 'description');
      if (query) route.set('book_q', query);
      if ($('library-language').value) route.set('language', $('library-language').value);
      history.replaceState({}, '', '/?' + route);
    }
    const params = new URLSearchParams({ q: query, offset: String(offset), limit: description ? '12' : '48' });
    $('library-status').disabled = description; $('library-ingestion-status').disabled = description;
    $('library-query').placeholder = description ? 'Опишите тему, идею или сюжет книги' : 'Название или автор';
    $('book-discovery-context').classList.toggle('hidden', !description);
    $('book-discovery-context').textContent = description ? 'Ищем по названию, автору и темам каталога. Это поиск книг по метаданным; поиск фрагментов внутри текста доступен отдельно.' : '';
    $('book-discovery-warnings').classList.add('hidden'); $('book-discovery-warnings').textContent = '';
    $('library-books').nextElementSibling.classList.toggle('hidden', description);
    if (description && !query) {
      empty($('library-books'), 'Какую книгу вы хотите найти?', 'Опишите тему, идею или сюжет. Название и язык запроса могут отличаться от языка издания.');
      $('library-count').textContent = ''; $('library-page').textContent = ''; state.libraryOffset = 0; return;
    }
    if ($('library-language').value) params.set('language', $('library-language').value);
    if (!description && $('library-status').value) params.set('status', $('library-status').value);
    if (!description && $('library-ingestion-status').value) params.set('ingestion_status', $('library-ingestion-status').value);
    loading($('library-books'), description ? 'Ищем книги по описанию' : 'Загружаем страницу каталога');
    $('library-prev').disabled = true; $('library-next').disabled = true; $('library-count').textContent = 'Загружаем…';
    try {
      const result = await api(`${description ? '/api/discover/books' : '/api/books'}?${params}`);
      if (request !== state.libraryRequest) return;
      const books = result.books || []; const total = result.total ?? books.length;
      if (description) {
        state.libraryOffset = 0; state.libraryHasMore = false;
        const coverage = result.coverage || {};
        $('library-count').textContent = `Ближайшие совпадения: ${formatNumber(books.length)}`;
        if (Number.isFinite(coverage.indexed_editions) && Number.isFinite(coverage.searchable_editions)) $('book-discovery-context').textContent += ` В поиске по описанию: ${formatNumber(coverage.indexed_editions)} из ${formatNumber(coverage.searchable_editions)} доступных изданий.`;
        const warnings = Array.isArray(result.warnings) ? result.warnings.filter((value) => typeof value === 'string') : [];
        $('book-discovery-warnings').textContent = warnings.join('\n'); $('book-discovery-warnings').classList.toggle('hidden', !warnings.length);
        if (books.length) $('library-books').replaceChildren(...books.map((book, index) => {
          const card = createBookCard(book, index);
          const evidence = book.metadata_evidence || {};
          const subjects = Array.isArray(evidence.subjects) ? evidence.subjects.filter((value) => typeof value === 'string') : [];
          card.querySelector('.book-card-info').insertBefore(node('p', 'book-discovery-evidence', subjects.length ? `Темы каталога: ${subjects.slice(0, 4).join(' · ')}` : 'Подобрано по метаданным издания'), card.querySelector('.book-card-footer'));
          return card;
        }));
        else if (result.status === 'unavailable') empty($('library-books'), 'Поиск по описанию пока недоступен', 'Доступен обычный каталог по названию и автору.');
        else empty($('library-books'), 'Совпадения пока не найдены', 'Попробуйте уточнить описание или изменить язык источников.');
        announce($('library-count').textContent); return;
      }
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
    state.starterVersion++; state.starterCache.clear(); state.starterPending.clear();
    if (state.starterLanguage && state.view === 'search' && results[0].status === 'fulfilled') loadStarterShelf();
    if (results.some((result) => result.status === 'rejected')) showError(results.find((result) => result.status === 'rejected').reason);
  }

  function contributorLabel(section, bookAuthor) {
    const author = section?.section_author || section?.source_author || section?.contributor || '';
    const role = section?.section_role || section?.role || '';
    const labels = [];
    if (author && author !== bookAuthor) labels.push(`Автор раздела: ${author}`);
    if (section?.section_translator) labels.push(`Перевод фрагмента: ${section.section_translator}`);
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
    const attempt = ingestionNotice(passage, true);
    if (attempt) card.append(attempt);
    card.append(quote, citation, actions); return card;
  }
  function renderSemanticCoverage(coverage, target = $('semantic-coverage')) {
    target.replaceChildren(); target.classList.add('hidden'); target.dataset.active = 'false';
    if (!coverage || coverage.active !== true || !Number.isSafeInteger(coverage.indexed_books) || !Number.isSafeInteger(coverage.searchable_books) || coverage.indexed_books < 1 || coverage.searchable_books < coverage.indexed_books) return;
    const russianScope = coverage.scope === 'ru21-scoped-overlay' && coverage.scope_language === 'ru' && Number.isSafeInteger(coverage.scope_searchable_books) && coverage.scope_searchable_books >= coverage.indexed_books;
    const title = russianScope
      ? `Поиск по смыслу на русском: ${formatNumber(coverage.indexed_books)} из ${formatNumber(coverage.scope_searchable_books)} изданий`
      : `Поиск по смыслу: ${formatNumber(coverage.indexed_books)} из ${formatNumber(coverage.searchable_books)} изданий`;
    target.append(node('p', 'semantic-coverage-title', title));
    if (russianScope) target.append(node('p', 'semantic-coverage-scope', 'Подключён отдельный индекс русских текстов. Порядок фрагментов рассчитан приближённо; откройте источник, чтобы проверить содержание.'));
    if (coverage.requested_book_covered === false) target.append(node('p', 'semantic-coverage-scope', 'Для выбранной книги доступен поиск по словам.'));
    target.append(node('p', 'semantic-coverage-note', 'Поиск по словам доступен по всей библиотеке.'));
    target.dataset.active = 'true';
    target.classList.toggle('hidden', state.panel !== (target.id === 'answer-coverage' ? 'answer' : 'fragments'));
  }
  function updateAnswerScope() {
    if (state.answerScopeBlocked) { $('answer-scope').textContent = state.answerScopeLoading ? 'Проверяем выбранную книгу из ссылки…' : 'Книга из ссылки не подтверждена. Выберите другую книгу или явно снимите ограничение.'; return; }
    const book = state.scopeBook;
    const scope = $('book-filter').value ? `Книга: ${book?.title || 'выбранное издание'}${book?.author ? ' · ' + book.author : ''}` : 'Вся библиотека';
    $('answer-scope').textContent = `${scope}. Язык источников: ${$('language-filter').value ? labelLanguage($('language-filter').value) : 'все языки'}.`;
  }
  function updateAnswerURL() {
    if (state.view !== 'search' || state.panel !== 'answer') return;
    const params = new URLSearchParams({ panel: 'answer' });
    if (state.query) params.set('q', state.query);
    if ($('book-filter').value) params.set('scope', $('book-filter').value);
    if ($('language-filter').value) params.set('language', $('language-filter').value);
    history.replaceState({}, '', `/?${params}`);
  }
  function answerBusy(busy) {
    const restoreFocus = !busy && document.activeElement === $('cancel-answer');
    $('answer-panel').setAttribute('aria-busy', String(busy));
    $('run-answer').disabled = busy || Boolean(state.answerScopeBlocked);
    $('cancel-answer').classList.toggle('hidden', !busy || !state.answerController);
    if (restoreFocus && state.view === 'search' && state.panel === 'answer') $('answer-query').focus();
  }
  function stopScopeRestore() {
    state.answerRouteSequence++;
    if (state.answerScopeLoading) { state.answerScopeLoading = false; answerBusy(Boolean(state.answerController)); renderScopeBooks(); updateAnswerScope(); }
  }
  function cancelAnswer(message) {
    if (!state.answerController) return;
    state.answerSequence++;
    state.answerController.abort(); state.answerController = null;
    answerBusy(false);
    $('answer-status').textContent = message || 'Ожидание остановлено.';
  }
  function invalidateAnswer(message = 'Вопрос или область поиска изменились. Отправьте вопрос снова.') {
    const hadAnswer = state.answerResponse || state.answerController;
    cancelAnswer(message); state.answerResponse = null;
    $('answer-output').replaceChildren(); $('answer-warnings').replaceChildren(); $('answer-warnings').classList.add('hidden');
    renderSemanticCoverage(null, $('answer-coverage'));
    $('answer-status').textContent = hadAnswer ? message : '';
    if (hadAnswer) announce(message);
  }
  function scopeChanged() {
    stopScopeRestore();
    invalidateAnswer(); updateAnswerScope();
    if (state.panel === 'answer') {
      clearFragmentContext('Область поиска изменилась'); updateAnswerURL();
    }
    else if (state.query) search();
  }
  function clearFragmentContext(title) {
    state.searchSequence++; state.results = [];
    $('search-form').querySelector('[type="submit"]').disabled = false;
    $('search-warnings').textContent = ''; $('search-warnings').classList.add('hidden');
    $('results-meta').textContent = '';
    renderSemanticCoverage(null);
    empty($('fragments-panel'), title, 'Нажмите «Найти» в строке поиска, чтобы получить отдельную выдачу для текущего запроса и области.');
  }
  function answerSourceLink(id, sourceMap) {
    const source = sourceMap.get(id);
    const link = node('a', 'answer-source-chip', id);
    link.href = '/?' + new URLSearchParams({ book: asID(source.book_id), section: asID(source.section_id), passage: asID(source.id) });
    link.setAttribute('aria-label', `Источник ${id}: ${source.title}`);
    link.addEventListener('click', (event) => { if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return; event.preventDefault(); const target = $('answer-source-' + id); target.scrollIntoView({ behavior: 'smooth', block: 'start' }); target.focus({ preventScroll: true }); });
    return link;
  }
  function renderAnswer(data) {
    const output = $('answer-output'); output.replaceChildren();
    const sourceMap = new Map();
    for (const source of Array.isArray(data.sources) ? data.sources : []) {
      if (source && /^S[1-9]\d{0,2}$/.test(source.source_id) && source.verified === true && typeof source.text === 'string' && source.book_id && !sourceMap.has(source.source_id)) sourceMap.set(source.source_id, source);
    }
    const claims = Array.isArray(data.claims) ? data.claims : [];
    let status = data.status;
    const validClaim = (claim) => claim && typeof claim.text === 'string' && claim.text.trim() &&
      Array.isArray(claim.source_ids) && claim.source_ids.length && claim.source_ids.every((id) => sourceMap.has(id)) &&
      Array.isArray(claim.supporting_quotes) && claim.supporting_quotes.length &&
      claim.supporting_quotes.every((support) => support && claim.source_ids.includes(support.source_id) && typeof support.quote === 'string' && support.quote.trim() && sourceMap.get(support.source_id)?.text.includes(support.quote)) &&
      claim.source_ids.every((id) => claim.supporting_quotes.some((support) => support.source_id === id));
    if (status === 'answered' && (!claims.length || !claims.every(validClaim))) status = 'invalid_output';
    const messages = {
      abstained: ['Недостаточно оснований для ответа', 'В найденных источниках недостаточно свидетельств. Попробуйте уточнить вопрос или изменить область поиска.'],
      unavailable: ['Ответ пока недоступен', 'Сейчас можно искать и читать точные фрагменты. Попробуйте получить ответ позже.'],
      invalid_output: ['Ответ не прошёл проверку', 'Показаны доступные фрагменты. Проверьте их в контексте книги или повторите вопрос.'],
      sources_changed: ['Источники изменились во время подготовки ответа', 'Ответ не показан. Отправьте вопрос снова, чтобы использовать актуальные фрагменты.'],
    };
    if (status === 'answered') {
      const answer = node('section', 'answer-claims'); answer.setAttribute('aria-label', 'Ответ с указанием источников');
      answer.append(node('p', 'eyebrow', 'ОТВЕТ ПО ИСТОЧНИКАМ'), node('p', 'answer-verification-note', 'Ответ пересказывает источники. Метка проверки ниже относится к точному тексту фрагмента.'));
      for (const claim of claims) {
        const group = node('article', 'answer-claim-group');
        const paragraph = node('p', 'answer-claim'); paragraph.append(node('span', '', claim.text));
        const citations = node('span', 'answer-citations');
        [...new Set(claim.source_ids)].forEach((id) => citations.append(answerSourceLink(id, sourceMap)));
        paragraph.append(citations); group.append(paragraph);
        const supports = node('div', 'answer-supports');
        for (const support of claim.supporting_quotes) {
          const figure = node('figure', 'answer-support');
          const quote = node('blockquote', 'answer-support-quote', support.quote);
          if (sourceMap.get(support.source_id).language) quote.lang = sourceMap.get(support.source_id).language;
          const caption = node('figcaption'); caption.append(node('span', 'verified-label', '✓ ЦИТАТА ПРОВЕРЕНА'), answerSourceLink(support.source_id, sourceMap));
          figure.append(quote, caption); supports.append(figure);
        }
        group.append(supports); answer.append(group);
      }
      output.append(answer);
      $('answer-status').textContent = `Ответ готов. Проверяемых источников: ${sourceMap.size}.`;
    } else {
      const message = messages[status] || messages.invalid_output;
      empty(output, message[0], message[1]); $('answer-status').textContent = message[0];
    }
    if (status === 'sources_changed') clearFragmentContext('Источники изменились');
    if (sourceMap.size && status !== 'sources_changed') {
      const sources = node('section', 'answer-sources'); sources.setAttribute('aria-label', 'Проверяемые фрагменты ответа');
      sources.append(node('h3', '', status === 'answered' ? 'Фрагменты, на которые опирается ответ' : 'Найденные фрагменты'));
      for (const [id, source] of sourceMap) { const card = passageCard(source); card.id = 'answer-source-' + id; card.tabIndex = -1; card.prepend(node('p', 'answer-source-id', `ИСТОЧНИК ${id}`)); sources.append(card); }
      output.append(sources);
    }
    const warnings = Array.isArray(data.warnings) ? data.warnings.filter((value) => typeof value === 'string') : [];
    $('answer-warnings').textContent = warnings.join('\n'); $('answer-warnings').classList.toggle('hidden', !warnings.length);
    renderSemanticCoverage(status === 'sources_changed' ? null : data.retrieval?.semantic_coverage, $('answer-coverage'));
    announce($('answer-status').textContent);
  }
  async function ask(event) {
    event.preventDefault();
    if (state.answerScopeBlocked) { $('book-filter').focus(); announce('Подтвердите книгу или снимите ограничение перед отправкой вопроса.'); return; }
    const query = $('answer-query').value.trim();
    if (!query) { $('answer-query').focus(); return; }
    cancelAnswer();
    const request = ++state.answerSequence; const controller = new AbortController(); state.answerController = controller;
    state.answerResponse = null; state.searchSequence++; $('search-form').querySelector('[type="submit"]').disabled = false;
    if (state.query !== query) { state.researchQuery = ''; $('research-output').replaceChildren(); clearFragmentContext('Этот вопрос можно исследовать по фрагментам'); }
    state.query = query; $('query').value = query; updateAnswerURL();
    $('answer-output').replaceChildren(); $('answer-warnings').replaceChildren(); $('answer-warnings').classList.add('hidden'); renderSemanticCoverage(null, $('answer-coverage'));
    answerBusy(true); $('answer-status').textContent = 'Ищем фрагменты и готовим ответ с источниками…';
    announce($('answer-status').textContent);
    try {
      const data = await api('/api/ask', { method: 'POST', signal: controller.signal, body: JSON.stringify({ query, book_ids: $('book-filter').value ? [$('book-filter').value] : [], language: $('language-filter').value || null, mode: 'hybrid', limit: 8 }) });
      if (request !== state.answerSequence || state.view !== 'search' || state.panel !== 'answer') return;
      state.answerResponse = data; renderAnswer(data);
    } catch (error) {
      if (request !== state.answerSequence || error.name === 'AbortError') return;
      showError(error, $('answer-output')); $('answer-status').textContent = 'Не удалось получить ответ. Вопрос сохранён в форме; попробуйте снова.';
    } finally {
      if (request === state.answerSequence) { state.answerController = null; answerBusy(false); }
    }
  }
  async function search(push = true) {
    const query = $('query').value.trim();
    if (!query) { $('query').focus(); return; }
    invalidateAnswer();
    if (state.query !== query) { state.researchQuery = ''; $('research-output').replaceChildren(); $('answer-query').value = query; }
    state.query = query;
    const request = ++state.searchSequence;
    $('search-results').classList.remove('hidden');
    $('discovery').classList.add('hidden');
    $('search-warnings').textContent = ''; $('search-warnings').classList.add('hidden');
    renderSemanticCoverage(null);
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
      renderSemanticCoverage(result.semantic_coverage);
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

  function prepareReader(message) {
    showView('reader', false);
    state.currentBook = null; state.currentSection = null; state.currentPassage = null;
    loading($('reader-text'), message);
    $('reader-sidebar-title').textContent = ''; $('reader-book-meta').textContent = '';
    $('reader-section-title').textContent = '';
    $('reader-toc').replaceChildren(); $('reader-source').replaceChildren();
    $('reader-source').closest('details').classList.add('hidden');
    for (const id of ['reader-citation', 'reader-contributor', 'reader-coverage', 'reader-historical-label']) { $(id).replaceChildren(); $(id).classList.add('hidden'); }
    $('reader-ingestion-warning').classList.add('hidden');
    $('reader-related-editions').classList.add('hidden');
    $('prev-chapter').disabled = true; $('next-chapter').disabled = true;
    $('reader-back').textContent = state.previousView === 'saved' ? '← К сохранённому' : state.previousView === 'library' ? '← К библиотеке' : '← К результатам';
  }
  async function openBook(bookId, sectionId, passageId, push = true) {
    const request = ++state.readerSequence;
    if (state.view !== 'reader') state.previousView = state.view;
    prepareReader('Открываем книгу');
    try {
      const [book, passage] = await Promise.all([api(`/api/books/${encodeURIComponent(bookId)}`), passageId ? api(`/api/passages/${encodeURIComponent(passageId)}`) : Promise.resolve(null)]);
      if (request !== state.readerSequence) return;
      state.currentBook = book;
      state.currentPassage = passage;
      const sections = sectionList(book);
      const opening = !sectionId && !passageId ? sections.find((section) => !(section.section_role === 'editorial' && String(section.section_author || '').replaceAll(' ', '').toLowerCase() === 'bookrag')) : sections[0];
      state.currentSection = sections.find((section) => asID(section.id) === asID(sectionId)) || sections.find((section) => asID(section.id) === asID(passage?.section_id)) || opening || sections[0] || null;
      renderReader();
      if (push) updateReaderURL();
    } catch (error) { if (request === state.readerSequence) showError(error, $('reader-text')); }
  }
  async function openSaved(itemID, ordinal = null, push = true) {
    const request = ++state.readerSequence;
    state.previousView = 'saved';
    prepareReader('Открываем сохранённый контекст');
    try {
      const id = asID(itemID);
      if (!/^[1-9]\d*$/.test(id) || !Number.isSafeInteger(Number(id))) throw new Error('Некорректная ссылка на сохранённый фрагмент.');
      const hasChapter = ordinal !== null && ordinal !== undefined;
      if (hasChapter && (!/^\d+$/.test(String(ordinal)) || !Number.isSafeInteger(Number(ordinal)))) throw new Error('Некорректный номер сохранённой главы.');
      const params = new URLSearchParams();
      if (hasChapter) params.set('ordinal', String(Number(ordinal)));
      if (push) { const url = new URLSearchParams({ saved: id }); if (hasChapter) url.set('chapter', String(Number(ordinal))); history.pushState({}, '', `/?${url}`); }
      const context = await api(`/api/saved/${encodeURIComponent(id)}/context${params.size ? '?' + params : ''}`);
      if (request !== state.readerSequence) return;
      if (!context.book || !context.section || typeof context.section.text !== 'string' || asID(context.book.historical_saved_id) !== id) throw new Error('Сервер вернул неполный сохранённый контекст.');
      state.currentBook = context.book; state.currentSection = context.section; state.currentPassage = context.passage || null;
      renderReader();
      if (push) updateReaderURL(true);
    } catch (error) {
      if (request !== state.readerSequence) return;
      showError(error, $('reader-text'));
      $('reader-text').append(button('К сохранённым фрагментам', () => { showView('saved'); refreshSaved().catch(showError); }, 'button secondary'));
      document.title = 'Сохранённый контекст недоступен — Атлас книг';
    }
  }
  function updateReaderURL(replace = false) {
    if (state.currentBook?.historical_saved_id) {
      const params = new URLSearchParams({ saved: asID(state.currentBook.historical_saved_id), chapter: String(state.currentSection.ordinal) });
      history[replace ? 'replaceState' : 'pushState']({}, '', `/?${params}`); return;
    }
    const params = new URLSearchParams({ book: asID(state.currentBook.id) });
    if (state.currentSection) params.set('section', asID(state.currentSection.id));
    if (state.currentPassage && asID(state.currentPassage.section_id) === asID(state.currentSection?.id)) params.set('passage', asID(state.currentPassage.id));
    history.pushState({}, '', `/?${params}`);
  }
  function selectChapter(sectionId) {
    const section = sectionList(state.currentBook).find((item) => asID(item.id) === asID(sectionId));
    if (!section) return;
    if (state.currentBook.historical_saved_id) { openSaved(state.currentBook.historical_saved_id, section.ordinal); return; }
    state.currentSection = section;
    renderReader(); updateReaderURL(); window.scrollTo({ top: 0, behavior: 'instant' });
  }
  function renderReader() {
    const book = state.currentBook; const section = state.currentSection; const passage = state.currentPassage;
    const sections = sectionList(book);
    $('reader-sidebar-title').textContent = book.title || '';
    $('reader-book-meta').textContent = `${book.author || ''} · ${book.title || ''} · ${book.edition || ''}`;
    const historical = Boolean(book.historical_saved_id);
    const savedAt = String(book.historical_saved_at || '');
    const savedDate = new Date(/(?:Z|[+-]\d\d:\d\d)$/.test(savedAt) ? savedAt : savedAt.replace(' ', 'T') + 'Z');
    $('reader-historical-label').replaceChildren(node('strong', '', `Сохранённое издание${savedAt && !Number.isNaN(savedDate.getTime()) ? ' · сохранено ' + savedDate.toLocaleString('ru-RU') : ''}`), node('span', '', 'Контекст зафиксирован вместе с сохранённым фрагментом.'));
    $('reader-historical-label').classList.toggle('hidden', !historical);
    const attempt = ingestionNotice(book);
    $('reader-ingestion-warning').replaceChildren(...(attempt ? [attempt] : []));
    $('reader-ingestion-warning').classList.toggle('hidden', !attempt);
    const coverage = coverageType(book);
    $('reader-coverage').textContent = coverage === 'selected_chapters' ? 'Выбранные главы · частичный текст произведения' : coverage === 'partial' ? 'Снимок текста · полнота произведения не подтверждена' : coverage === 'excerpt' || /extract|excerpt|отрывок/i.test(book.edition || '') ? 'Отрывок произведения · частичный текст' : book.source_metadata?.coverage_scope === 'original_work_only' ? 'Оригинальное произведение · без позднего редакционного аппарата' : '';
    $('reader-coverage').classList.toggle('hidden', !$('reader-coverage').textContent);
    document.title = `${book.title}${historical ? ' · сохранённое издание' : ''} — Атлас книг`;
    $('reader-toc').replaceChildren(...sections.map((item) => {
      const btn = button(item.title || `Глава ${Number(item.ordinal || 0) + 1}`, () => selectChapter(item.id), asID(item.id) === asID(section?.id) ? 'active' : '');
      if (asID(item.id) === asID(section?.id)) btn.setAttribute('aria-current', 'true');
      return btn;
    }));
    const source = $('reader-source'); source.replaceChildren();
    source.closest('details').classList.remove('hidden');
    source.append(node('p', '', book.edition || 'Издание не указано'), node('p', '', book.rights || 'Условия использования не указаны'));
    const sourceURL = safeURL(book.source_url);
    if (sourceURL) { const link = node('a', '', 'Открыть первоисточник ↗'); link.href = sourceURL; link.target = '_blank'; link.rel = 'noopener noreferrer'; const p = node('p'); p.append(link); source.append(p); }
    const archivedOriginal = artifactDownload(book.artifact_url);
    if (archivedOriginal) { const paragraph = node('p', 'source-artifact'); paragraph.append(archivedOriginal, node('span', 'artifact-description', 'Файл этой версии текста.')); source.append(paragraph); }
    if (withoutIllustrations(book)) source.append(node('p', 'source-variant-note', 'В этой копии нет иллюстраций; оригинал доступен по ссылке источника'));
    const rightsURL = safeURL(book.rights_url || book.source_metadata?.rights_url);
    if (rightsURL) { const link = node('a', '', 'Условия использования источника ↗'); link.href = rightsURL; link.target = '_blank'; link.rel = 'noopener noreferrer'; const paragraph = node('p'); paragraph.append(link); source.append(paragraph); }
    const bundle = book.source_metadata?.source_bundle_sha256;
    if (typeof bundle === 'string' && /^[0-9a-f]{64}$/i.test(bundle)) source.append(node('p', 'source-revision-bundle', `Снимок версий источника: ${bundle.slice(0, 16)}…`));
    const credits = book.source_metadata?.upstream_attribution;
    if (Array.isArray(credits) && credits.length) {
      const list = node('ul', 'source-upstream-credits');
      credits.slice(0, 50).forEach((credit) => {
        if (!credit || typeof credit !== 'object') return;
        const item = node('li'); item.append(node('span', '', credit.author || 'Участники источника'));
        for (const [value, label] of [[credit.source_url, 'Текст источника'], [credit.history_url, 'История участников']]) {
          const url = safeURL(value); if (!url) continue;
          const link = node('a', '', label); link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer'; item.append(document.createTextNode(' · '), link);
        }
        list.append(item);
      });
      source.append(list);
    }
    if (typeof book.source_metadata?.attribution === 'string' && book.source_metadata.attribution.trim()) {
      const details = node('details', 'source-attribution'); details.append(node('summary', '', 'Происхождение и преобразования'), node('p', '', book.source_metadata.attribution)); source.append(details);
    }
    const revisions = book.source_metadata?.chapter_sources;
    if (Array.isArray(revisions) && revisions.length) {
      const details = node('details', 'source-revisions');
      details.append(node('summary', '', `Зафиксированные версии страниц (${revisions.length})`));
      const list = node('ol');
      revisions.forEach((revision) => {
        if (!revision || typeof revision !== 'object') return;
        const url = safeURL(revision.permanent_url);
        if (!url) return;
        const item = node('li'); const link = node('a', '', revision.title || `Версия ${revision.revision_id || ''}`);
        link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer';
        item.append(link);
        if (revision.revision_id) item.append(node('span', '', `Версия ${revision.revision_id}${revision.revision_timestamp ? ' · ' + String(revision.revision_timestamp).slice(0, 10) : ''}`));
        list.append(item);
      });
      details.append(list); source.append(details);
    }
    const related = Array.isArray(book.related_editions) ? book.related_editions.filter((edition) => edition && edition.id && asID(edition.id) !== asID(book.id)).slice(0, 20) : [];
    $('reader-related-editions').replaceChildren();
    $('reader-related-editions').classList.toggle('hidden', !related.length);
    if (related.length) {
      const summary = node('summary'); summary.append(node('h3', '', 'Другие издания и переводы'));
      $('reader-related-editions').append(summary, node('p', 'related-edition-hint', 'Откроется начало выбранного издания.'));
      const editions = node('ul');
      related.forEach((edition) => {
        const item = node('li'); const link = node('a', '', edition.title || 'Открыть издание');
        link.href = '/?' + new URLSearchParams({ book: edition.id });
        link.addEventListener('click', (event) => { if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return; event.preventDefault(); openBook(edition.id); });
        item.append(link, node('p', '', [labelLanguage(edition.language), edition.edition].filter(Boolean).join(' · ')));
        if (typeof edition.translator === 'string' && edition.translator.trim()) item.append(node('p', '', `Перевод: ${edition.translator}`));
        if (coverageType(edition) === 'selected_chapters') item.append(node('p', '', 'Выбранные главы'));
        else if (coverageType(edition) === 'excerpt') item.append(node('p', '', 'Отрывок произведения'));
        else if (coverageType(edition) === 'partial') item.append(node('p', '', 'Полнота произведения не подтверждена'));
        editions.append(item);
      });
      $('reader-related-editions').append(editions);
      if (Number(book.related_editions_total) > related.length) $('reader-related-editions').append(node('p', 'related-edition-hint', `Показано ${related.length} из ${formatNumber(book.related_editions_total)} связанных изданий.`));
    }

    if (!section) { empty($('reader-text'), 'Нет доступных глав', 'Эта книга ещё не обработана. Проверьте состояние корпуса в библиотеке.'); return; }
    $('reader-section-title').textContent = section.title || 'Текст книги';
    const contributor = contributorLabel(section, book.author);
    $('reader-contributor').textContent = contributor;
    $('reader-contributor').classList.toggle('hidden', !contributor);
    $('reader-text').lang = book.language || 'und';
    const fullText = String(section.text || '');
    const highlight = passage && asID(passage.section_id) === asID(section.id);
    const start = Number(passage?.start); const end = Number(passage?.end);
    const exact = highlight && passage.verified === true && Number.isInteger(start) && Number.isInteger(end) && start >= 0 && end > start && end <= [...fullText].length;
    // API offsets count Unicode code points; JavaScript slice counts UTF-16 code units.
    const characters = exact ? Array.from(fullText) : null;
    if (exact && characters.slice(start, end).join('') === passage.text) {
      const mark = node('mark', '', characters.slice(start, end).join('')); mark.id = 'found-passage'; mark.tabIndex = -1;
      $('reader-text').replaceChildren(document.createTextNode(characters.slice(0, start).join('')), mark, document.createTextNode(characters.slice(end).join('')));
      const citation = $('reader-citation'); citation.replaceChildren(node('p', '', passage.citation || 'Найденный фрагмент выделен в тексте.'));
      if (!historical && !passage.historical && Number.isSafeInteger(Number(passage.id)) && Number(passage.id) > 0) citation.append(button('＋ Сохранить фрагмент', () => openSave({ kind: 'passage', passage_id: passage.id, title: `${book.title} · ${section.title || 'Фрагмент'}` }, passage.text)));
      citation.append(button('Копировать цитату', () => copyText(`${passage.text}\n\n${passage.citation || ''}`)));
      citation.classList.remove('hidden');
      requestAnimationFrame(() => mark.scrollIntoView({ block: 'center', behavior: 'instant' }));
    } else {
      $('reader-text').textContent = fullText;
      $('reader-citation').replaceChildren();
      $('reader-citation').classList.toggle('hidden', !highlight);
      if (highlight) $('reader-citation').textContent = 'Не удалось подтвердить положение фрагмента в этой версии главы. Прочитайте источник и проверьте цитату.';
    }
    const index = sections.findIndex((item) => asID(item.id) === asID(section.id));
    $('prev-chapter').disabled = index <= 0;
    $('next-chapter').disabled = index < 0 || index >= sections.length - 1;
    announce(`${historical ? 'Открыто сохранённое издание' : 'Открыта книга'} ${book.title}. ${section.title || 'Текст книги'}.`);
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
  function syncSavedUI(preferredCollection = $('collection-filter').value) {
    $('saved-count').textContent = formatNumber(state.saved.length);
    const names = collectionNames();
    populateSelect($('collection-filter'), names.map((value) => ({ value, label: value })), 'Все коллекции');
    $('collection-filter').value = names.includes(preferredCollection) ? preferredCollection : '';
    $('collection-options').replaceChildren(...names.map((name) => { const option = node('option'); option.value = name; return option; }));
    if (state.view === 'saved') renderSaved();
  }
  async function refreshSaved() {
    const data = await api('/api/saved'); state.saved = data.items || []; syncSavedUI();
  }
  function setSaveBusy(busy) {
    state.savePending = busy;
    $('save-dialog').setAttribute('aria-busy', String(busy));
    for (const id of ['save-title', 'save-collection', 'save-note', 'close-save', 'cancel-save']) $(id).disabled = busy;
    $('save-form').querySelector('[type="submit"]').disabled = busy;
    $('save-status').textContent = busy ? 'Сохраняем изменения…' : '';
  }
  function configureSaveDialog(editing, preview, description) {
    $('save-dialog-title').textContent = editing ? 'Редактировать запись' : 'Сохранить в коллекцию';
    $('save-mode-description').textContent = description;
    $('save-preview').textContent = preview;
    $('save-title').required = !editing;
    $('save-form').querySelector('[type="submit"]').textContent = editing ? 'Сохранить изменения' : 'Сохранить';
    $('save-error').textContent = ''; $('save-error').classList.add('hidden');
    setSaveBusy(false);
    $('save-dialog').showModal(); $('save-title').focus();
  }
  function openSave(draft, preview) {
    if (state.savePending) return;
    state.saveDraft = draft; state.saveItemID = null; state.saveOriginal = null; state.saveReturnFocus = document.activeElement;
    $('save-title').value = draft.title || '';
    $('save-collection').value = '';
    $('save-note').value = '';
    configureSaveDialog(false, textExcerpt(preview || draft.query || '', 350), 'Добавьте название, заметку и коллекцию.');
  }
  function editSaved(item, trigger) {
    if (state.savePending) return;
    state.saveDraft = null; state.saveItemID = item.id; state.saveReturnFocus = trigger;
    state.saveOriginal = { title: item.title || '', note: item.note || '', collection: item.collection || '' };
    $('save-title').value = state.saveOriginal.title; $('save-note').value = state.saveOriginal.note; $('save-collection').value = state.saveOriginal.collection;
    configureSaveDialog(true, item.text || item.query || '', item.kind === 'query' ? 'Название, заметка и коллекция. Исходный сохранённый запрос:' : 'Название, заметка и коллекция. Цитата и источник зафиксированы.');
  }
  function closeSaveDialog() { if (!state.savePending) $('save-dialog').close(); }
  function saveDialogClosed() {
    const trigger = state.saveReturnFocus; const id = state.saveItemID;
    state.saveDraft = null; state.saveItemID = null; state.saveOriginal = null; state.saveReturnFocus = null;
    requestAnimationFrame(() => {
      if (trigger?.isConnected && typeof trigger.focus === 'function') trigger.focus();
      else if (id !== null) { const card = [...$('saved-items').querySelectorAll('.saved-item')].find((item) => item.dataset.savedId === asID(id)); (card?.querySelector('.edit-saved') || $('collection-filter')).focus(); }
    });
  }
  async function saveItem(event) {
    event.preventDefault();
    if (state.savePending || (!state.saveDraft && state.saveItemID === null)) return;
    const editing = state.saveItemID !== null;
    const values = { title: $('save-title').value, collection: $('save-collection').value, note: $('save-note').value };
    const changed = editing ? Object.fromEntries(Object.entries(values).filter(([key, value]) => value !== state.saveOriginal[key])) : null;
    if (editing && !Object.keys(changed).length) { closeSaveDialog(); toast('Изменений нет.'); return; }
    setSaveBusy(true); $('save-error').classList.add('hidden');
    try {
      const updated = editing
        ? await api(`/api/saved/${encodeURIComponent(state.saveItemID)}`, { method: 'PATCH', body: JSON.stringify(changed) })
        : await api('/api/saved', { method: 'POST', body: JSON.stringify({ ...state.saveDraft, title: values.title.trim(), collection: values.collection.trim(), note: values.note.trim() }) });
      const selected = editing && updated.collection !== state.saveOriginal.collection ? updated.collection : $('collection-filter').value;
      if (editing) state.saved = state.saved.map((item) => asID(item.id) === asID(updated.id) ? updated : item);
      else state.saved.unshift(updated);
      syncSavedUI(selected); setSaveBusy(false); closeSaveDialog();
      toast(editing ? 'Изменения сохранены.' : 'Сохранено в исследовательский блокнот.');
    } catch (error) { $('save-error').textContent = editing && error.status === 404 ? 'Запись не найдена. Введённый текст остался в диалоге.' : error.message; $('save-error').classList.remove('hidden'); }
    finally { setSaveBusy(false); }
  }
  function renderSaved() {
    const collection = $('collection-filter').value;
    const items = state.saved.filter((item) => !collection || item.collection === collection);
    if (!items.length) { empty($('saved-items'), collection ? 'В коллекции пока пусто' : 'Дайте мысли продолжение', 'Сохраните фрагмент из поисковой выдачи или читателя. Добавьте заметку и объедините находки в тематическую коллекцию.'); return; }
    $('saved-items').replaceChildren(...items.map((item) => {
      const card = node('article', 'saved-item');
      card.dataset.savedId = asID(item.id);
      const date = item.created_at ? new Date(item.created_at).toLocaleDateString('ru-RU') : '';
      card.append(node('p', 'saved-kind', `${item.kind === 'query' ? 'ЗАПРОС' : 'ФРАГМЕНТ'}${item.collection ? ' / ' + item.collection : ''}${date && date !== 'Invalid Date' ? ' · ' + date : ''}`), node('h2', '', item.title));
      if (item.text) card.append(node('blockquote', '', item.text));
      else if (item.query) card.append(node('p', '', item.query));
      if (item.citation) card.append(node('p', 'passage-citation', item.citation));
      if (item.note) card.append(node('p', 'saved-note', item.note));
      const actions = node('div', 'saved-actions');
      if (item.historical_reader_url === '/?saved=' + asID(item.id) && /^[1-9]\d*$/.test(asID(item.id))) {
        const link = node('a', 'text-button historical-reader-link', 'Читать сохранённое издание');
        link.href = item.historical_reader_url;
        link.addEventListener('click', (event) => { if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return; event.preventDefault(); openSaved(item.id); });
        actions.append(link);
      } else if (item.provenance?.historical_context_error) card.append(node('p', 'saved-context-note', 'Цитата и заметка сохранены. Контекст издания сохранить не удалось.'));
      const archivedOriginal = artifactDownload(item.artifact_url);
      if (archivedOriginal) actions.append(archivedOriginal);
      if (item.kind === 'query') actions.append(button('Продолжить поиск ↗', () => { showView('search'); $('query').value = item.query || item.title; search(); }));
      else if (item.passage_id && item.reader_url) actions.append(button('Открыть текущее издание ↗', async () => {
        try { const passage = await api(`/api/passages/${encodeURIComponent(item.passage_id)}`); await openBook(passage.book_id, passage.section_id, passage.id); }
        catch (error) { showError(error); }
      }));
      actions.append(button('Редактировать', (event) => editSaved(item, event.currentTarget), 'text-button edit-saved'), button('Копировать', () => copyText([item.text || item.query || item.title, item.citation, item.note].filter(Boolean).join('\n\n'))), button('Удалить', async (event) => {
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
    const navigation = ++state.answerRouteSequence;
    const routeKey = location.search;
    const currentRoute = () => navigation === state.answerRouteSequence && location.search === routeKey && state.view === 'search';
    const params = new URLSearchParams(location.search);
    if (params.has('saved')) { await openSaved(params.get('saved'), params.has('chapter') ? params.get('chapter') : null, false); return; }
    if (params.get('book')) { await openBook(params.get('book'), params.get('section'), params.get('passage'), false); return; }
    if (params.get('view') === 'library') {
      if ([...$('library-language').options].some((option) => option.value === params.get('language'))) shelfCatalogFilters(params.get('language'));
      $('library-search-mode').value = params.get('book_mode') === 'description' ? 'description' : 'title';
      if (params.has('book_q')) $('library-query').value = params.get('book_q');
    }
    showView(params.get('view') || 'search', false);
    if (params.get('panel') === 'answer' && state.view === 'search') {
      const query = params.get('q') || '';
      if (state.query !== query) { invalidateAnswer(); state.query = query; $('answer-query').value = query; }
      $('query').value = query;
      const scope = params.get('scope') || '';
      const language = params.get('language') || '';
      setPanel('answer', false);
      if ($('language-filter').value !== language) { invalidateAnswer(); $('language-filter').value = language; }
      if (scope !== $('book-filter').value || (scope && state.answerScopeBlocked === scope)) {
        invalidateAnswer();
        if (scope) {
          state.answerScopeBlocked = scope; state.answerScopeLoading = true; state.scopeBook = null;
          renderScopeBooks(); updateAnswerScope();
          answerBusy(true);
          try { const book = await api(`/api/books/${encodeURIComponent(scope)}`); if (!currentRoute()) return; if (!bookSearchable(book)) throw new Error('Выбранное издание сейчас недоступно в поиске.'); setScopeBook(book, false); }
          catch (error) { if (currentRoute()) { state.answerScopeLoading = false; renderScopeBooks(); updateAnswerScope(); showError(error, $('answer-output')); } }
          finally { if (currentRoute()) { state.answerScopeLoading = false; answerBusy(false); } }
        } else setScopeBook(null, false);
      }
      if (currentRoute()) setPanel('answer', false); return;
    }
    if (params.has('q')) { $('query').value = params.get('q'); await search(false); }
  }

  // Keep availability and the latest source attempt independently selectable.
  const attemptFilter = node('select', 'hidden'); attemptFilter.id = 'library-ingestion-status';
  attemptFilter.setAttribute('aria-label', 'Последняя попытка получения источника');
  populateSelect(attemptFilter, Object.entries(ingestionNames).map(([value, label]) => ({ value, label: `Попытка: ${label.toLocaleLowerCase('ru-RU')}` })), 'Все последние попытки');
  $('library-count').before(attemptFilter);
  $('library-status').setAttribute('aria-label', 'Доступность издания');
  $('library-status').querySelector('option[value="failed"]').textContent = 'Недоступны · ошибка загрузки';
  if (!$('library-status').querySelector('option[value="deferred"]')) { const option = node('option', '', 'Недоступны · отложены'); option.value = 'deferred'; $('library-status').append(option); }
  const readerAttempt = node('div', 'hidden'); readerAttempt.id = 'reader-ingestion-warning'; readerAttempt.setAttribute('role', 'status');
  $('reader-citation').before(readerAttempt);
  const historicalLabel = node('div', 'reader-historical-label hidden'); historicalLabel.id = 'reader-historical-label';
  $('reader-book-meta').after(historicalLabel);
  const readerRelated = node('details', 'reader-related-editions hidden'); readerRelated.id = 'reader-related-editions'; readerRelated.setAttribute('aria-label', 'Другие издания и переводы');
  const wideReader = matchMedia('(min-width: 761px)'); readerRelated.open = wideReader.matches;
  wideReader.addEventListener('change', (event) => { readerRelated.open = event.matches; });
  $('reader-source').closest('details').after(readerRelated);
  $('save-dialog').querySelector('.dialog-heading h2').id = 'save-dialog-title';
  $('save-dialog').setAttribute('aria-labelledby', 'save-dialog-title');
  $('save-dialog').setAttribute('aria-describedby', 'save-mode-description');
  $('save-title').maxLength = 500; $('save-collection').maxLength = 200; $('save-note').maxLength = 10000;
  const saveDescription = node('p'); saveDescription.id = 'save-mode-description'; $('save-preview').before(saveDescription);
  const saveStatus = node('p'); saveStatus.id = 'save-status'; saveStatus.setAttribute('role', 'status'); $('save-error').after(saveStatus);
  const saveSubmit = $('save-form').querySelector('[type="submit"]');
  const saveActions = node('div', 'save-dialog-actions'); saveSubmit.before(saveActions);
  const cancelSave = button('Отмена', closeSaveDialog); cancelSave.id = 'cancel-save'; saveActions.append(cancelSave, saveSubmit);

  const semanticCoverage = node('aside', 'semantic-coverage hidden'); semanticCoverage.id = 'semantic-coverage'; semanticCoverage.setAttribute('role', 'status'); semanticCoverage.setAttribute('aria-label', 'Покрытие поиска по смыслу');
  $('search-warnings').before(semanticCoverage);

  $('search-form').addEventListener('submit', (event) => { event.preventDefault(); search(); });
  document.querySelectorAll('[data-mode]').forEach((el) => el.addEventListener('click', () => { setMode(el.dataset.mode); if (state.query) search(); }));
  document.querySelectorAll('[data-query]').forEach((el) => el.addEventListener('click', () => { $('query').value = el.dataset.query; search(); }));
  $('refresh-map').addEventListener('click', () => loadExplore(true));
  document.querySelectorAll('[data-shelf-language]').forEach((el) => el.addEventListener('click', () => loadStarterShelf(el.dataset.shelfLanguage)));
  $('starter-catalog').addEventListener('click', (event) => { if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return; event.preventDefault(); shelfCatalogFilters(state.starterLanguage); showView('library'); history.replaceState({}, '', $('starter-catalog').href); });
  $('map-language').addEventListener('change', () => { state.mapSelection = null; populateMapBooks(); renderExplore(); });
  $('map-book').addEventListener('change', () => { if ($('map-book').value) focusMapBook($('map-book').value); else { state.mapSelection = null; renderExplore(); } });
  $('search-extra-mode').addEventListener('change', () => { setMode($('search-extra-mode').value || 'hybrid'); if (state.query) search(); });
  $('fragments-tab').addEventListener('click', () => setPanel('fragments'));
  $('research-tab').addEventListener('click', () => setPanel('research'));
  $('answer-tab').addEventListener('click', () => setPanel('answer'));
  $('open-answer').addEventListener('click', () => { setPanel('answer'); $('answer-query').focus(); $('search-results').scrollIntoView({ block: 'start' }); });
  $('answer-form').addEventListener('submit', ask);
  $('answer-query').addEventListener('input', () => invalidateAnswer('Вопрос изменён. Отправьте его снова.'));
  $('cancel-answer').addEventListener('click', () => cancelAnswer('Ожидание остановлено. Вопрос остался в форме.'));
  $('run-research').addEventListener('click', research);
  $('language-filter').addEventListener('change', () => { loadScopeBooks(); scopeChanged(); });
  $('book-filter').addEventListener('change', () => { const id = $('book-filter').value; setScopeBook(id ? state.scopeBooks.find((book) => asID(book.id) === id) || state.scopeBook : null); scopeChanged(); });
  $('book-scope-query').addEventListener('input', debounce(loadScopeBooks));
  $('book-scope-query').addEventListener('focus', () => { if (!state.scopeLoaded) loadScopeBooks(); });
  $('book-filter').addEventListener('focus', () => { if (!state.scopeLoaded) loadScopeBooks(); });
  $('compare-query').addEventListener('input', debounce(loadCompareBooks));
  $('compare-query').addEventListener('focus', () => { if (!state.compareLoaded) loadCompareBooks(); });
  $('clear-filters').addEventListener('click', () => { $('language-filter').value = ''; $('book-scope-query').value = ''; setScopeBook(null); loadScopeBooks(); scopeChanged(); });
  $('save-query').addEventListener('click', () => { const query = state.panel === 'answer' ? $('answer-query').value.trim() : state.query; if (query) openSave({ kind: 'query', title: query, query }, query); });
  $('save-form').addEventListener('submit', saveItem);
  $('close-save').addEventListener('click', closeSaveDialog);
  $('save-dialog').addEventListener('cancel', (event) => { if (state.savePending) event.preventDefault(); });
  $('save-dialog').addEventListener('close', saveDialogClosed);
  $('save-dialog').addEventListener('click', (event) => { if (event.target === $('save-dialog')) { const bounds = $('save-dialog').getBoundingClientRect(); if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) closeSaveDialog(); } });
  $('collection-filter').addEventListener('change', renderSaved);
  $('export-saved').addEventListener('click', exportSaved);
  $('export-markdown').addEventListener('click', exportMarkdown);
  $('library-query').addEventListener('input', debounce(() => loadLibrary(0)));
  $('library-search-mode').addEventListener('change', () => loadLibrary(0));
  $('open-book-discovery').addEventListener('click', () => { $('library-search-mode').value = 'description'; $('library-query').value = $('query').value.trim(); showView('library'); $('library-query').focus(); });
  $('library-language').addEventListener('change', () => loadLibrary(0));
  $('library-status').addEventListener('change', () => loadLibrary(0));
  $('library-ingestion-status').addEventListener('change', () => loadLibrary(0));
  $('library-prev').addEventListener('click', () => loadLibrary(Math.max(0, state.libraryOffset - 48)));
  $('library-next').addEventListener('click', () => loadLibrary(state.libraryOffset + 48));
  $('reload-library').addEventListener('click', async () => { await refreshLibrary(); loadStarterShelf(); });
  $('reader-back').addEventListener('click', () => { const previous = state.previousView; showView(previous); if (previous === 'saved') refreshSaved().catch(showError); });
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
