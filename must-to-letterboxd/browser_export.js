// Must -> Letterboxd export. Paste into the browser console on https://mustapp.com
// (any page) and press Enter. Change USERNAME below to export another public profile.
// Produces the same files as must_to_letterboxd.py; see README.md.
(async () => {
  const USERNAME = 'vladimirsalov';
  const DATES = 'smart';      // smart | all | none — see README.md
  const WINDOW_DAYS = 30;     // smart: drop dates in the first N days on Must
  const BULK_PER_DAY = 5;     // smart: drop dates of days with N+ films
  const TAG = '';             // Letterboxd tag for diary entries, e.g. 'must-import'
  const INCLUDE_REVIEWS = true;
  const LANG = 'en';          // English titles match Letterboxd best

  const API = location.hostname.endsWith('mustapp.com') ? '/api' : 'https://mustapp.com/api';
  const MUST_HEADERS = {
    accept: '*/*',
    bearer: '3a77331c-943f-44e8-b636-5deebcbe33b9',
    'content-type': 'application/json;v=1873',
    'x-client-version': 'frontend_site/2.24.2-390.390',
    'x-requested-with': 'XMLHttpRequest',
  };
  const MUST_BATCH = 100;
  const TV_TYPES = new Set(['show', 'season', 'episode']);
  const MAX_CSV_BYTES = 900 * 1024;
  const WATCHED_COLUMNS = ['tmdbID', 'imdbID', 'Title', 'Year', 'Rating10', 'WatchedDate', 'Tags', 'Review'];
  const WATCHLIST_COLUMNS = ['tmdbID', 'imdbID', 'Title', 'Year'];
  const TV_COLUMNS = ['List', 'MustID', 'Type', 'Title', 'Year', 'Status', 'Rating10', 'Date', 'Review'];

  const normalizeUsername = value => String(value || '').trim()
    .replace(/^(?:https?:\/\/)?(?:www\.)?mustapp\.com\/@?/i, '').replace(/^@+/, '').split(/[/?#]/)[0].trim();
  const user = normalizeUsername(USERNAME);
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const log = (...args) => console.log('%c[Must→Letterboxd]', 'color:#00c030;font-weight:bold', ...args);

  async function fetchJson(url, options, label) {
    for (let attempt = 0; ; attempt++) {
      let response, text;
      try {
        // No cookies: the public API answers the same as for a logged-out visitor.
        const signal = typeof AbortSignal.timeout === 'function' ? AbortSignal.timeout(60000) : undefined;
        response = await fetch(url, { credentials: 'omit', signal, ...options });
        if (response.ok) text = await response.text();
      } catch (error) {
        // Timeouts, dropped connections and cut-off replies are worth another try.
        if (attempt >= 5) throw new Error(`${label}: ${error.message}`);
        await sleep(1000 * 2 ** attempt);
        continue;
      }
      if (response.ok) {
        try { return JSON.parse(text); } catch { throw new Error(`${label}: the server did not return JSON`); }
      }
      const retryable = response.status === 408 || response.status === 429 || response.status >= 500;
      if (!retryable || attempt >= 5) throw new Error(`${label}: HTTP ${response.status}`);
      const retryAfter = Math.min(Math.max(Number(response.headers.get('retry-after')) || 0, 0), 300);
      await sleep((retryAfter || 2 ** attempt) * 1000);
    }
  }

  const isObject = value => !!value && typeof value === 'object' && !Array.isArray(value);
  const asObject = value => (isObject(value) ? value : {});

  async function fetchList(url, ids, headers, label) {
    const result = await fetchJson(url, { method: 'POST', headers, body: JSON.stringify({ ids }) }, label);
    if (!Array.isArray(result)) {
      const message = result && result.error && typeof result.error === 'object' ? result.error.message : JSON.stringify(result);
      throw new Error(`${label}: unexpected reply: ${String(message).slice(0, 200)}`);
    }
    return result.filter(item => item && typeof item === 'object' && !Array.isArray(item));
  }

  async function fetchMustBackup(username) {
    const profile = await fetchJson(`${API}/users/uri/${encodeURIComponent(username)}`,
      { headers: { 'accept-language': LANG } }, 'Must profile');
    if (!isObject(profile) || profile.error) throw new Error((isObject(profile) && asObject(profile.error).message) || `Must user "${username}" not found`);
    if (profile.is_private || !isObject(profile.lists) || !Object.keys(profile.lists).length) {
      throw new Error('This Must profile is private. Make it public in Must settings and try again.');
    }
    if (profile.id == null) throw new Error('Must profile: unexpected reply without a user id');

    const lists = profile.lists;
    const ids = [...new Set([...(lists.watched || []), ...(lists.want || []), ...(lists.shows || [])])];
    const headers = { ...MUST_HEADERS, 'accept-language': LANG };
    const products = [], reviews = [], reviewsFailed = [];
    for (let start = 0; start < ids.length; start += MUST_BATCH) {
      const batch = ids.slice(start, start + MUST_BATCH);
      const url = `${API}/users/id/${profile.id}/products?embed=`;
      products.push(...await fetchList(url + 'product', batch, headers, 'Must products'));
      try {
        reviews.push(...await fetchList(url + 'review', batch, headers, 'Must reviews'));
      } catch (error) {
        reviewsFailed.push(...batch);
        log(`warning: reviews for ${batch.length} titles unavailable (${error.message})`);
      }
      log(`Must: ${Math.min(start + MUST_BATCH, ids.length)}/${ids.length}`);
    }
    return {
      format: 'must-backup/1',
      username,
      fetched_at: new Date().toISOString().replace(/\.\d+Z$/, '+00:00'),
      lang: LANG,
      profile,
      products,
      reviews,
      reviews_failed: reviewsFailed,
    };
  }

  // ------------------------------------------------------------ conversion
  // Mirrors build_entries / apply_date_policy in must_to_letterboxd.py.

  const productId = item => asObject(item.product).id || asObject(item.user_product_info).product_id;
  const clean = value => String(value || '').trim();
  const datePart = value => { const m = String(value || '').match(/^\d{4}-\d{2}-\d{2}/); return m ? m[0] : ''; };
  // Must stores UTC times; the day a film was marked is the one in this browser's time zone.
  function localDate(value) {
    const text = String(value || '');
    const m = text.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2})?)(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})/);
    if (!m) return datePart(text);
    const clock = m[2].length === 5 ? m[2] + ':00' : m[2];
    const zone = m[4] === 'Z' ? 'Z' : m[4].includes(':') ? m[4] : m[4].slice(0, 3) + ':' + m[4].slice(3);
    const d = new Date(`${m[1]}T${clock}${zone}`);
    if (isNaN(d)) return m[1];
    const pad = n => String(n).padStart(2, '0');
    return `${String(d.getFullYear()).padStart(4, '0')}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  }
  function reviewText(info) {
    let review = (info || {}).review;
    if (review && typeof review === 'object') review = review.body;
    return clean(review);
  }
  function rating10(value) {
    let rate = null;
    if (typeof value === 'number' && Number.isInteger(value)) rate = value;
    else if (typeof value === 'string' && /^\s*[+-]?\d+\s*$/.test(value)) rate = parseInt(value, 10);
    return rate !== null && rate >= 1 && rate <= 10 ? String(rate) : '';
  }

  function buildEntries(backup) {
    const lists = backup.profile.lists || {};
    const products = backup.products || [];
    const byId = new Map();
    for (const item of products) {
      const pid = productId(item);
      if (pid != null) byId.set(pid, { product: { ...asObject(item.product) }, info: { ...asObject(item.user_product_info) } });
    }
    const reviews = backup.reviews || [];
    reviews.forEach((item, index) => {
      let pid = productId(item);
      if (pid == null && reviews.length === products.length) pid = productId(products[index]);
      if (!byId.has(pid)) return;
      const target = byId.get(pid).info;
      for (const [key, value] of Object.entries(asObject(item.user_product_info))) {
        if (value != null && target[key] == null) target[key] = value;
      }
      const text = reviewText(item.user_product_info);
      if (text) target.review = { body: text };
    });

    const films = [], tv = [], missing = [], seen = new Set();
    for (const [listName, ids] of [['watched', lists.watched || []], ['want', lists.want || []], ['shows', lists.shows || []]]) {
      for (const pid of ids) {
        if (seen.has(pid)) continue;
        seen.add(pid);
        if (!byId.has(pid)) { missing.push(pid); continue; }
        const { product, info } = byId.get(pid);
        const kind = product.type || 'movie';
        const entry = {
          list: listName,
          must_id: pid,
          type: kind,
          title: clean(product.title),
          year: datePart(product.release_date).slice(0, 4),
          status: info.status || '',
          rating: rating10(info.rate),
          date: localDate(info.watched_at || info.modified_at),
          review: reviewText(info),
          tmdb_id: '',
          imdb_id: '',
        };
        (TV_TYPES.has(kind) || listName === 'shows' ? tv : films).push(entry);
      }
    }
    return { films, tv, missing };
  }

  function applyDatePolicy(watched, mode, windowDays, bulkPerDay) {
    const stats = { kept: 0, window: 0, bulk: 0, missing: 0 };
    const dated = watched.map(e => e.date).filter(Boolean);
    const first = dated.length ? dated.slice().sort()[0] : '';
    let windowEnd = '';
    if (first) {
      const d = new Date(first + 'T00:00:00Z');
      d.setUTCDate(d.getUTCDate() + windowDays);
      windowEnd = d.toISOString().slice(0, 10);
    }
    const perDay = new Map();
    for (const date of dated) perDay.set(date, (perDay.get(date) || 0) + 1);

    for (const entry of watched) {
      const date = entry.date;
      if (!date) { stats.missing++; entry.watched_date = ''; }
      else if (mode === 'none') entry.watched_date = '';
      else if (mode === 'smart' && windowDays > 0 && date < windowEnd) { stats.window++; entry.watched_date = ''; }
      else if (mode === 'smart' && bulkPerDay > 0 && perDay.get(date) >= bulkPerDay) { stats.bulk++; entry.watched_date = ''; }
      else { stats.kept++; entry.watched_date = date; }
    }
    stats.first_date = first;
    stats.window_end = windowEnd;
    return stats;
  }

  const compare = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
  // Oldest first, so Letterboxd creates diary entries in the order they happened.
  const sortWatched = watched => watched.slice().sort((a, b) =>
    compare(a.watched_date || '0000', b.watched_date || '0000') || compare(a.date || '0000', b.date || '0000'));
  // Oldest first: Letterboxd adds rows in file order, so recent Must additions stay recent.
  const sortWatchlist = want => want.slice().sort((a, b) => compare(a.date || '0000', b.date || '0000'));

  // ------------------------------------------------------------ CSV

  // Letterboxd escapes quotes inside quoted text with a backslash, not by doubling them
  // (https://letterboxd.com/about/importing-data/).
  function lbField(value) {
    let s = String(value ?? '');
    if (s.endsWith('\\')) s += ' ';  // a trailing backslash would escape the closing quote
    if (!/[",\r\n]|^[ \t]|[ \t]$/.test(s)) return s;
    return `"${s.replace(/\\"/g, '\\ "').replace(/"/g, '\\"')}"`;
  }
  const lbLine = values => values.map(lbField).join(',') + '\n';
  // RFC 4180, for the TV file that is read by people and spreadsheets, not Letterboxd.
  const csvField = value => {
    const s = String(value ?? '');
    return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const csvLine = values => values.map(csvField).join(',') + '\n';
  const byteLength = text => new TextEncoder().encode(text).length;

  function csvParts(columns, rows) {
    const header = lbLine(columns);
    const parts = [];
    let current = [], size = byteLength(header);
    for (const row of rows) {
      const line = lbLine(row);
      const length = byteLength(line);
      if (current.length && size + length > MAX_CSV_BYTES) {
        parts.push(header + current.join(''));
        current = []; size = byteLength(header);
      }
      current.push(line); size += length;
    }
    if (current.length || !parts.length) parts.push(header + current.join(''));
    return parts;
  }

  const watchedRow = e => [e.tmdb_id, e.imdb_id, e.title, e.year, e.rating, e.watched_date,
    e.watched_date ? clean(TAG) : '', e.review.replace(/\r\n|\r|\n/g, '<br>')];
  const watchlistRow = e => [e.tmdb_id, e.imdb_id, e.title, e.year];
  const tvRow = e => [e.list, e.must_id, e.type, e.title, e.year, e.status, e.rating, e.date, e.review];

  // ------------------------------------------------------------ UI

  function showPanel(lines, files) {
    document.getElementById('must-lb-panel')?.remove();
    const panel = document.createElement('div');
    panel.id = 'must-lb-panel';
    panel.style.cssText = 'box-sizing:border-box;position:fixed;top:16px;right:16px;z-index:2147483647;width:min(420px,calc(100vw - 32px));' +
      'max-height:calc(100vh - 32px);overflow:auto;background:#14181c;color:#def;border:2px solid #00c030;' +
      'border-radius:12px;padding:16px;font:14px/1.45 -apple-system,system-ui,sans-serif;box-shadow:0 8px 32px #0008';
    const title = document.createElement('div');
    title.textContent = 'Must → Letterboxd';
    title.style.cssText = 'font-weight:700;font-size:17px;margin-bottom:8px;color:#fff';
    panel.append(title);
    for (const line of lines) {
      const p = document.createElement('div');
      p.textContent = line;
      panel.append(p);
    }
    for (const file of files) {
      const url = URL.createObjectURL(new Blob([file.text], { type: file.type + ';charset=utf-8' }));
      const a = document.createElement('a');
      a.href = url; a.download = file.name;
      a.textContent = '⬇ ' + file.name;
      a.style.cssText = 'display:block;margin-top:8px;padding:8px 10px;border-radius:8px;background:#00c030;color:#fff;' +
        'text-decoration:none;font-weight:600;word-break:break-all';
      panel.append(a);
    }
    const close = document.createElement('button');
    close.textContent = '✕';
    close.style.cssText = 'position:absolute;top:8px;right:10px;background:none;border:0;color:#9ab;font-size:18px;cursor:pointer';
    close.onclick = () => panel.remove();
    panel.append(close);
    document.body.append(panel);
  }

  // ------------------------------------------------------------ main

  try {
    log(`Downloading Must profile @${user}...`);
    const backup = await fetchMustBackup(user);
    const { films, tv, missing } = buildEntries(backup);
    const watched = films.filter(e => e.list === 'watched');
    const want = sortWatchlist(films.filter(e => e.list === 'want'));
    if (!INCLUDE_REVIEWS) watched.forEach(e => { e.review = ''; });
    const stats = applyDatePolicy(watched, DATES, WINDOW_DAYS, BULK_PER_DAY);

    const files = [];
    const addParts = (stem, parts) => parts.forEach((text, i) =>
      files.push({ name: `${stem}${parts.length > 1 ? `_part${i + 1}` : ''}.csv`, text, type: 'text/csv' }));
    addParts(`${user}_letterboxd_watched`, csvParts(WATCHED_COLUMNS, sortWatched(watched).map(watchedRow)));
    addParts(`${user}_letterboxd_watchlist`, csvParts(WATCHLIST_COLUMNS, want.map(watchlistRow)));
    addParts(`${user}_must_tv`, [csvLine(TV_COLUMNS) + tv.map(tvRow).map(csvLine).join('')]);
    files.push({ name: `${user}_must_backup.json`, text: JSON.stringify(backup, null, 1), type: 'application/json' });

    const rated = watched.filter(e => e.rating).length;
    const reviewed = watched.filter(e => e.review).length;
    const lines = [
      `@${user}. Просмотренные фильмы: ${watched.length} (с оценкой: ${rated}, с рецензией: ${reviewed}). Хочу посмотреть: ${want.length}. Сериалы: ${tv.length} (в Letterboxd не переносятся).`,
      DATES === 'smart'
        ? `Даты для дневника: оставлено ${stats.kept}; убрано ${stats.window} из первых ${WINDOW_DAYS} дней в Must и ${stats.bulk} из дней с ${BULK_PER_DAY}+ фильмами; без даты ${stats.missing}.`
        : `Даты для дневника (${DATES}): оставлено ${stats.kept}, без даты ${stats.missing}.`,
      ...(backup.reviews_failed.length ? [`⚠ Рецензии для ${backup.reviews_failed.length} позиций не скачались — запусти скрипт ещё раз перед импортом.`] : []),
      ...(missing.length ? [`⚠ Must не вернул данные для ${missing.length} позиций (id: ${missing.join(', ')}) — они не попали в файлы.`] : []),
      'Скачай файлы ниже. *_letterboxd_watched.csv → letterboxd.com/import, *_letterboxd_watchlist.csv → страница Watchlist → «Import films to watchlist…».',
    ];
    lines.forEach(line => log(line));
    showPanel(lines, files);
    window.mustLetterboxdExport = { backup, files, stats };
  } catch (error) {
    const hint = /Must profile: HTTP 404/.test(error.message) ? ` — профиль @${user} не найден, проверь USERNAME в начале скрипта` : '';
    log('Ошибка:', error.message + hint);
    showPanel([`Ошибка: ${error.message}${hint}`], []);
  }
})();
