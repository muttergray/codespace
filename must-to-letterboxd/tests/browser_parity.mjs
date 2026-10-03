// End-to-end test of browser_export.js: runs it in Chromium on a mocked mustapp.com
// and checks every file it offers is byte-identical to must_to_letterboxd.py's output.
//
//   node tests/browser_parity.mjs        (needs Playwright + Chromium, Python 3)
import { execFileSync } from 'node:child_process';
import { createRequire } from 'node:module';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const require = createRequire(import.meta.url);
let chromium;
try {
  ({ chromium } = require('playwright'));
} catch {
  const globalRoot = execFileSync('npm', ['root', '-g'], { encoding: 'utf8' }).trim();
  ({ chromium } = require(path.join(globalRoot, 'playwright')));
}

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.dirname(here);
const fixture = JSON.parse(fs.readFileSync(path.join(here, 'fixtures', 'must_backup.json'), 'utf8'));
const script = fs.readFileSync(path.join(root, 'browser_export.js'), 'utf8');
const BEARER = '3a77331c-943f-44e8-b636-5deebcbe33b9';

function bigFixture() {
  const backup = structuredClone(fixture);
  const base = backup.products[0];
  for (let i = 0; i < 5000; i++) {
    const item = structuredClone(base);
    item.product.id = item.user_product_info.product_id = 10000 + i;
    item.product.title = [`Film ${i}, "quoted"`, `Film ${i} \\`, `Film ${i} \\"x\\"`, ` Film ${i}\u00a0`][i % 4];
    item.user_product_info.modified_at = `20${10 + (i % 15)}-0${1 + (i % 9)}-1${i % 10}T${String(i % 24).padStart(2, '0')}:30:00.000Z`;
    item.user_product_info.rate = (i % 10) + 1;
    backup.products.push(item);
    const review = structuredClone(item);
    review.user_product_info.review = { body: 'Длинная рецензия, с запятыми.\nИ переносами. 🎬 '.repeat(5) };
    delete review.product;
    backup.reviews.push(review);
    backup.profile.lists.watched.push(10000 + i);
  }
  return backup;
}

function pythonFiles(backup, extraArgs, tz) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'must-py-'));
  const input = path.join(dir, 'input.json');
  fs.writeFileSync(input, JSON.stringify(backup));
  const out = path.join(dir, 'out');
  execFileSync('python3', [path.join(root, 'must_to_letterboxd.py'), '--from-json', input, '--out-dir', out, ...extraArgs],
    { stdio: 'pipe', env: { ...process.env, TZ: tz } });
  const files = {};
  for (const name of fs.readdirSync(out)) if (name.endsWith('.csv')) files[name] = fs.readFileSync(path.join(out, name), 'utf8');
  return files;
}

async function runCase(browser, name, backup, settings = {}, pyArgs = [], tz = 'Europe/Moscow', options = {}) {
  const username = backup.profile.uri;
  const requests = [];
  const context = await browser.newContext({ acceptDownloads: true, timezoneId: tz });
  const page = await context.newPage();
  page.on('pageerror', error => { throw error; });
  await page.route('https://mustapp.com/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    requests.push({ method: request.method(), path: url.pathname + url.search, headers: request.headers(),
      body: request.postData() });
    const json = body => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
    if (url.pathname === `/api/users/uri/${username}`) return json(backup.profile);
    const match = url.pathname.match(/^\/api\/users\/id\/(\d+)\/products$/);
    if (match && request.method() === 'POST') {
      assert.equal(request.headers().bearer, BEARER);
      assert.equal(request.headers()['content-type'], 'application/json;v=1873');
      const ids = JSON.parse(request.postData()).ids;
      assert.ok(ids.length <= 100, 'batch over 100 ids');
      if (url.searchParams.get('embed') === 'review' && options.reviewStatus) {
        return route.fulfill({ status: options.reviewStatus, contentType: 'application/json', body: '{"error":{"message":"nope"}}' });
      }
      const source = url.searchParams.get('embed') === 'review' ? backup.reviews : backup.products;
      const byId = new Map(source.map(i => [(i.product || {}).id || i.user_product_info.product_id, i]));
      return json(ids.filter(id => byId.has(id)).map(id => byId.get(id)));
    }
    if (url.pathname.startsWith('/api/')) return route.fulfill({ status: 404, body: '{}' });
    return route.fulfill({ status: 200, contentType: 'text/html', body: '<!doctype html><title>Must</title><body>Must</body>' });
  });
  await page.goto(`https://mustapp.com/@${username}`);

  let source = script.replace(/const USERNAME = '[^']*';/, `const USERNAME = '${username}';`);
  for (const [key, value] of Object.entries(settings)) {
    source = source.replace(new RegExp(`const ${key} = [^;]*;`), `const ${key} = ${JSON.stringify(value)};`);
  }
  await page.evaluate(source);
  const result = await page.evaluate(() => window.mustLetterboxdExport &&
    { backup: window.mustLetterboxdExport.backup, files: window.mustLetterboxdExport.files.map(f => ({ name: f.name, text: f.text })) });
  assert.ok(result, `${name}: export did not finish: ` + await page.locator('#must-lb-panel').innerText().catch(() => '?'));

  // Same-origin, English titles requested.
  assert.ok(requests.filter(r => r.path.startsWith('/api/')).every(r => r.headers['accept-language'] === 'en'));

  const py = pythonFiles(result.backup, pyArgs, tz);
  const browserCsv = Object.fromEntries(result.files.filter(f => f.name.endsWith('.csv')).map(f => [f.name, f.text]));
  assert.deepEqual(Object.keys(browserCsv).sort(), Object.keys(py).sort(), `${name}: file names differ`);
  for (const file of Object.keys(py)) assert.equal(browserCsv[file], py[file], `${name}: ${file} differs`);
  for (const text of Object.values(browserCsv)) assert.ok(Buffer.byteLength(text) < 1024 * 1024, 'CSV over 1 MB');

  const panelText = await page.locator('#must-lb-panel').innerText();
  for (const expected of options.panel || []) assert.match(panelText, expected, `${name}: panel text`);

  // The panel's links really download the files.
  const links = page.locator('#must-lb-panel a[download]');
  assert.equal(await links.count(), result.files.length);
  const [download] = await Promise.all([page.waitForEvent('download'), links.first().click()]);
  assert.equal(download.suggestedFilename(), result.files[0].name);
  assert.equal(fs.readFileSync(await download.path(), 'utf8'), result.files[0].text);

  await context.close();
  console.log(`ok - ${name} [${tz}]: ${Object.keys(py).length} CSV files identical (${requests.length} requests)`);
  return { result, py };
}

async function errorCase(browser, name, profile, expected) {
  const page = await browser.newPage();
  await page.route('https://mustapp.com/**', route => {
    const url = new URL(route.request().url());
    if (url.pathname.startsWith('/api/users/uri/')) {
      return profile ? route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(profile) })
        : route.fulfill({ status: 404, contentType: 'application/json', body: '{"error":{"message":"not found"}}' });
    }
    return route.fulfill({ status: 200, contentType: 'text/html', body: '<!doctype html><body></body>' });
  });
  await page.goto('https://mustapp.com/');
  await page.evaluate(script);
  const text = await page.locator('#must-lb-panel').innerText();
  assert.match(text, expected, `${name}: ${text}`);
  await page.close();
  console.log(`ok - ${name}`);
}

const browser = await chromium.launch();
try {
  const { py } = await runCase(browser, 'fixture, defaults', fixture);
  assert.match(py['testuser_letterboxd_watched.csv'], /^tmdbID,imdbID,Title,Year,Rating10,WatchedDate,Tags,Review\n/);
  assert.match(py['testuser_letterboxd_watched.csv'], /,Perfect Days,2023,10,2025-01-06,,/);  // 22:00 UTC is next day in Moscow
  assert.match(py['testuser_letterboxd_watchlist.csv'], /^tmdbID,imdbID,Title,Year\n,,Mickey 17,2025\n,,"\\"Weird\\" Title, With Comma",2026\n$/);
  const ny = await runCase(browser, 'fixture, defaults', fixture, {}, [], 'America/New_York');
  assert.match(ny.py['testuser_letterboxd_watched.csv'], /,Perfect Days,2023,10,2025-01-05,,/);
  await runCase(browser, 'fixture, all dates + tag', fixture, { DATES: 'all', TAG: 'must-import' }, ['--dates', 'all', '--tag', 'must-import']);
  await runCase(browser, 'fixture, no dates, no reviews', fixture, { DATES: 'none', INCLUDE_REVIEWS: false }, ['--dates', 'none', '--no-reviews']);
  const big = await runCase(browser, '5000 extra films (batching + 1 MB split)', bigFixture());
  assert.ok(Object.keys(big.py).some(n => n.includes('_part2')), 'big export was not split');
  await runCase(browser, 'profile URL as USERNAME', fixture, { USERNAME: 'https://mustapp.com/@testuser/watched' });
  const gappy = structuredClone(fixture);
  gappy.profile.lists.want.push(999);
  await runCase(browser, 'reviews fail, an id is missing', gappy, {}, [], 'Europe/Moscow',
    { reviewStatus: 400, panel: [/Рецензии для 17 позиций не скачались/, /Must не вернул данные для 1 позиций \(id: 999\)/] });
  await errorCase(browser, 'unknown user shows an error', null, /Ошибка: Must profile: HTTP 404 — профиль @vladimirsalov не найден/);
  await errorCase(browser, 'private profile shows an error', { id: 1, is_private: true }, /private/);
  console.log('all browser tests passed');
} finally {
  await browser.close();
}
