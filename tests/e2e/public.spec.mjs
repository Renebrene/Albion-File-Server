import { chromium } from 'playwright';
import assert from 'node:assert/strict';

const base = process.env.ALBION_E2E_BASE || 'http://127.0.0.1:18081';
const fileId = '00000000-0000-0000-0000-000000000001';
const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH || '/usr/bin/google-chrome',
  headless: true,
  args: ['--no-sandbox'],
});
const page = await browser.newPage();
const errors = [];
page.on('pageerror', error => errors.push(String(error)));
page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
try {
  for (const size of ['mini', 'medium']) {
    const thumb = await page.request.get(`${base}/thumb/${size}/${fileId}`);
    assert.equal(thumb.status(), 200, `${size} thumbnail status`);
    assert.equal(thumb.headers()['content-type'], 'image/webp', `${size} content type`);
    const body = await thumb.body();
    assert.equal(body.subarray(0, 4).toString('ascii'), 'RIFF');
    assert.equal(body.subarray(8, 12).toString('ascii'), 'WEBP');
  }

  await page.goto(`${base}/d/public`);
  await page.waitForSelector('#publicItems .item');
  assert.equal(await page.locator('#publicItems .item').count(), 50, 'initial public page');
  assert.equal(await page.locator('.pagination a, .page-link').count(), 0, 'no pagination controls');
  assert.match(await page.locator('.thumb').first().getAttribute('src'), /^\/thumb\/mini\//);
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  await page.waitForFunction(
    () => document.querySelectorAll('#publicItems .item').length === 60,
    null,
    { timeout: 15000 },
  );
  assert.equal(await page.locator('#publicItems .item').count(), 60, 'lazy public page');
  assert.equal(await page.locator('#publicLazyLoader').evaluate(el => el.hidden), true);
  assert.deepEqual(errors, [], 'browser errors: ' + errors.join(' | '));
  console.log('PUBLIC_E2E_OK');
} finally {
  await browser.close();
}
