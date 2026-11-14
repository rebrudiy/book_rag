'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const state = {
    view: 'search', mode: 'lexical', query: '', books: [], saved: [], results: [], stats: null,
    currentBook: null, currentSection: null, currentPassage: null,
    saveDraft: null, searchSequence: 0, readerSequence: 0, readerSize: 20,
    previousView: 'search', researchQuery: '', toastTimer: null,
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
    if (!['search', 'library', 'saved', 'reader'].includes(view)) view = 'search';
    state.view = view;
    document.querySelectorAll('.view').forEach((el) => el.classList.toggle('hidden', el.id !== `${view}-view`));
    updateNav(view === 'reader' ? '' : view);
    document.title = `${({ search: 'Поиск и исследование', library: 'Библиотека', saved: 'Сохранённое', reader: 'Читатель' })[view]} — Атлас книг`;
    if (push && view !== 'reader') {
      const params = new URLSearchParams();
      if (view !== 'search') params.set('view', view);
      if (view === 'search' && state.query) params.set('q', state.query);
      history.pushState({}, '', params.size ? `/?${params}` : '/');
    }
    if (view === 'library') renderLibrary();
    if (view === 'saved') renderSaved();
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

  function createBookCard(book, index) {
    const card = node('article', 'book-card');
    const cover = node('div', `book-cover cover-${index % 6}`);
    cover.setAttribute('aria-hidden', 'true');
    cover.append(node('span', 'cover-title', book.title));
    const info = node('div', 'book-card-info');
    const language = node('div', 'book-language');
    language.append(node('span', '', labelLanguage(book.language)), node('span', 'book-status', bookSearchable(book) ? '● В поиске' : statusNames[book.status] || book.status || 'В библиотеке'));
    info.append(language, node('h3', '', book.title), node('p', 'book-author', book.author));
    const excerpt = /extract|excerpt|отрывок|фрагмент/i.test(book.edition || '');
    if (excerpt) info.append(node('p', 'book-excerpt-label', 'Демонстрационный отрывок')); 
    const footer = node('div', 'book-card-footer');
    footer.append(node('span', '', bookSearchable(book) ? `${formatNumber(bookSectionsCount(book))} глав · ${labelLanguage(book.language)}` : 'Текст ещё не индексирован'));
    const open = button(excerpt ? 'Читать отрывок ↗' : 'Читать ↗', () => openBook(book.id), 'book-open');
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
    const languages = [...new Set(state.books.map((book) => book.language).filter(Boolean))].sort();
    const languageOptions = languages.map((value) => ({ value, label: labelLanguage(value) }));
    populateSelect($('language-filter'), languageOptions, 'Все языки');
    populateSelect($('library-language'), languageOptions, 'Все языки');
    populateSelect($('book-filter'), searchable.map((book) => ({ value: asID(book.id), label: `${book.author} — ${book.title}` })), 'Вся библиотека');
    $('compare-books').replaceChildren(...searchable.map((book) => {
      const label = node('label'); const check = node('input');
      check.type = 'checkbox'; check.value = asID(book.id); check.name = 'compare-book';
      label.append(check, node('span', '', `${book.title} · ${book.author}`));
      return label;
    }));
    renderLibrary();
  }
  function renderStats() {
    const stats = state.stats || {};
    const counts = stats.status_counts || {};
    $('stat-books').textContent = formatNumber(stats.books);
    $('stat-passages').textContent = formatNumber(stats.passages);
    const languages = stats.languages;
    $('stat-languages').textContent = formatNumber(Array.isArray(languages) ? languages.length : typeof languages === 'object' && languages ? Object.keys(languages).length : languages || 0);
    const items = Object.entries(counts);
    $('pipeline-stats').replaceChildren(...items.map(([status, count]) => {
      const box = node('div', 'pipeline-stat'); box.append(node('b', '', formatNumber(count)), node('span', '', statusNames[status] || status)); return box;
    }));
    if (!items.length) {
      const box = node('div', 'pipeline-stat'); box.append(node('b', '', formatNumber(stats.books)), node('span', '', 'Доступно в поиске')); $('pipeline-stats').append(box);
    }
  }
  function renderLibrary() {
    const q = $('library-query').value.trim().toLocaleLowerCase();
    const language = $('library-language').value;
    const books = state.books.filter((book) => (!language || book.language === language) && (!q || `${book.title} ${book.author}`.toLocaleLowerCase().includes(q)));
    $('library-count').textContent = `${formatNumber(books.length)} из ${formatNumber(state.books.length)} книг`;
    if (books.length) $('library-books').replaceChildren(...books.map(createBookCard));
    else empty($('library-books'), 'Книги не найдены', state.books.length ? 'Попробуйте другой фильтр или имя автора.' : 'Добавьте демонстрационный корпус командой bookrag demo.');
  }
  async function refreshLibrary() {
    const results = await Promise.allSettled([api('/api/stats'), api('/api/books')]);
    if (results[0].status === 'fulfilled') { state.stats = results[0].value; renderStats(); }
    if (results[1].status === 'fulfilled') { state.books = results[1].value.books || []; renderCatalog(); }
    if (results.some((result) => result.status === 'rejected')) {
      const failure = results.find((result) => result.status === 'rejected');
      showError(failure.reason);
    }
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
    card.append(top, quote, citation, actions); return card;
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
      const modeName = { lexical: 'по словам', hybrid: 'гибридный', semantic: 'по смыслу' }[result.mode || state.mode] || result.mode;
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
    const bookIds = [...document.querySelectorAll('input[name="compare-book"]:checked')].map((el) => el.value);
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
    try { await navigator.clipboard.writeText(value); toast('Текст и ссылка на источник скопированы.'); }
    catch {
      const textarea = node('textarea'); textarea.value = value;
      textarea.style.position = 'fixed'; textarea.style.opacity = '0'; document.body.append(textarea); textarea.select();
      const copied = document.execCommand('copy'); textarea.remove();
      toast(copied ? 'Текст скопирован.' : 'Браузер не разрешил копирование. Выделите текст и скопируйте вручную.');
    }
  }
  function exportSaved() {
    const data = { app: 'Book Atlas', exported_at: new Date().toISOString(), items: state.saved };
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }));
    const link = node('a'); link.href = url; link.download = `book-atlas-notes-${new Date().toISOString().slice(0, 10)}.json`; document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
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
  $('fragments-tab').addEventListener('click', () => setPanel('fragments'));
  $('research-tab').addEventListener('click', () => setPanel('research'));
  $('run-research').addEventListener('click', research);
  $('language-filter').addEventListener('change', () => { if (state.query) search(); });
  $('book-filter').addEventListener('change', () => { if (state.query) search(); });
  $('clear-filters').addEventListener('click', () => { $('language-filter').value = ''; $('book-filter').value = ''; if (state.query) search(); });
  $('save-query').addEventListener('click', () => { if (state.query) openSave({ kind: 'query', title: state.query, query: state.query }, state.query); });
  $('save-form').addEventListener('submit', saveItem);
  $('close-save').addEventListener('click', () => $('save-dialog').close());
  $('save-dialog').addEventListener('click', (event) => { if (event.target === $('save-dialog')) { const bounds = $('save-dialog').getBoundingClientRect(); if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) $('save-dialog').close(); } });
  $('collection-filter').addEventListener('change', renderSaved);
  $('export-saved').addEventListener('click', exportSaved);
  $('library-query').addEventListener('input', renderLibrary);
  $('library-language').addEventListener('change', renderLibrary);
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
