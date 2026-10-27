import { chromium, devices } from 'playwright';
import assert from 'node:assert/strict';

const base = process.env.ALBION_E2E_BASE || 'http://127.0.0.1:18081';
const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH || '/usr/bin/google-chrome',
  headless: true,
  args: ['--no-sandbox'],
});
const context = await browser.newContext({ ...devices['iPhone 13'] });
const page = await context.newPage();
const errors = [];
page.on('pageerror', error => errors.push(String(error)));
page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
try {
  await page.goto(`${base}/d/public`);
  const metrics = await page.evaluate(() => ({
    innerWidth,
    scrollWidth: document.documentElement.scrollWidth,
    viewport: document.querySelector('meta[name="viewport"]')?.getAttribute('content') || '',
  }));
  assert.match(metrics.viewport, /width=device-width/);
  assert.equal(metrics.innerWidth, 390, 'mobile layout viewport');
  assert.equal(metrics.scrollWidth, 390, 'no horizontal overflow');
  assert.equal(await page.locator('#publicItems .item').count(), 50);
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  await page.waitForFunction(
    () => document.querySelectorAll('#publicItems .item').length === 60,
    null,
    { timeout: 15000 },
  );
  assert.deepEqual(errors, [], 'browser errors: ' + errors.join(' | '));

  // Админ-страница на телефоне: таблица становится карточками без горизонтального скролла.
  await page.goto(`${base}/login`);
  await page.fill('input[name="username"]', 'e2e');
  await page.fill('input[name="password"]', 'E2E-Password-2026!');
  await Promise.all([
    page.waitForURL((url) => url.pathname.startsWith('/f/'), { timeout: 15000 }),
    page.click('button[type="submit"]'),
  ]);
  await page.goto(`${base}/f/11111111-1111-1111-1111-111111111111`);
  await page.waitForSelector('#view-table .file-row');
  const adminMetrics = await page.evaluate(() => ({
    innerWidth,
    scrollWidth: document.documentElement.scrollWidth,
    theadDisplay: getComputedStyle(document.querySelector('.files-table thead')).display,
    rowDisplay: getComputedStyle(document.querySelector('#view-table .file-row')).display,
  }));
  assert.equal(adminMetrics.theadDisplay, 'none', 'thead скрыт на мобильном');
  assert.equal(adminMetrics.rowDisplay, 'block', 'строка таблицы — карточка');
  assert.ok(adminMetrics.scrollWidth <= adminMetrics.innerWidth + 1,
    `нет горизонтального скролла (${adminMetrics.scrollWidth} > ${adminMetrics.innerWidth})`);
  assert.deepEqual(errors, [], 'browser errors: ' + errors.join(' | '));
  console.log('MOBILE_E2E_OK', JSON.stringify(metrics), JSON.stringify(adminMetrics));
} finally {
  await browser.close();
}
