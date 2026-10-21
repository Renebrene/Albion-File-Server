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
  console.log('MOBILE_E2E_OK', JSON.stringify(metrics));
} finally {
  await browser.close();
}
