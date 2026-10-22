// Замер реальных размеров превью на странице: сколько пикселей занимают
// миниатюры в списке, плитки грида и публичная страница при разной ширине окна
// и разном DPR. Нужен, чтобы выбирать размеры кэша по фактической вёрстке.
// Запуск: ALBION_E2E_BASE=http://127.0.0.1:PORT bun measure_thumbs.mjs
import { chromium } from 'playwright';

const BASE = process.env.ALBION_E2E_BASE || 'http://127.0.0.1:18081';
const FOLDER_ID = '11111111-1111-1111-1111-111111111111';
const FOLDER_URL = `${BASE}/f/${FOLDER_ID}`;

const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH || '/usr/bin/google-chrome',
});

async function login(page) {
  await page.goto(`${BASE}/login`);
  await page.fill('input[name="username"]', 'e2e');
  await page.fill('input[name="password"]', 'E2E-Password-2026!');
  await page.click('button[type="submit"]');
  await page.waitForLoadState('networkidle');
}

async function measure(viewport, deviceScaleFactor, label) {
  const context = await browser.newContext({ viewport, deviceScaleFactor });
  const page = await context.newPage();
  await login(page);
  await page.goto(FOLDER_URL);
  await page.waitForSelector('.tile, .list-item, tr');

  // таблица
  const list = await page.evaluate(() => {
    const row = document.querySelector('.thumb-sm, .thumb-xs');
    if (!row) return null;
    const r = row.getBoundingClientRect();
    return { w: Math.round(r.width), h: Math.round(r.height), cls: row.className };
  });

  // грид
  await page.click('.view-btn[data-view="grid"]').catch(() => {});
  await page.waitForTimeout(400);
  const grid = await page.evaluate(() => {
    const img = document.querySelector('.tile-preview');
    const tile = document.querySelector('.tile');
    if (!img) return null;
    const r = img.getBoundingClientRect();
    return {
      imgW: Math.round(r.width), imgH: Math.round(r.height),
      tileW: tile ? Math.round(tile.getBoundingClientRect().width) : null,
      natural: `${img.naturalWidth}x${img.naturalHeight}`,
      rendered: `${Math.round(r.width)}x${Math.round(r.height)}`,
    };
  });

  // публичная страница
  const pub = await context.newPage();
  await pub.goto(`${BASE}/d/${FOLDER_ID}`).catch(() => pub.goto(`${BASE}/f/${FOLDER_ID}`));
  await pub.waitForTimeout(500);
  const publicThumb = await pub.evaluate(() => {
    const img = document.querySelector('.item img.thumb');
    if (!img) return null;
    const r = img.getBoundingClientRect();
    return { w: Math.round(r.width), h: Math.round(r.height),
             natural: `${img.naturalWidth}x${img.naturalHeight}` };
  });

  console.log(`${label}: список ${list ? list.w + 'x' + list.h + ' (' + list.cls + ')' : '—'}` +
              ` | плитка ${grid ? grid.rendered + ' в карточке ' + grid.tileW + ', файл ' + grid.natural : '—'}` +
              ` | публичная ${publicThumb ? publicThumb.w + 'x' + publicThumb.h + ', файл ' + publicThumb.natural : '—'}`);
  await context.close();
}

await measure({ width: 1440, height: 900 }, 1, '1440px DPR1');
await measure({ width: 1440, height: 900 }, 2, '1440px DPR2');
await measure({ width: 1920, height: 1080 }, 2, '1920px DPR2');
await measure({ width: 390, height: 844 }, 3, 'мобильный DPR3');

await browser.close();
