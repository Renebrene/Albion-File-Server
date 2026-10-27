import { chromium } from 'playwright';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';

const BASE = process.env.ALBION_E2E_BASE || 'http://127.0.0.1:18081';
const FOLDER_ID = '11111111-1111-1111-1111-111111111111';
const FOLDER_URL = `${BASE}/f/${FOLDER_ID}`;
const USER = 'e2e';
const PASSWORD = 'E2E-Password-2026!';

const browserErrors = [];
const stepResults = [];
const allSteps = [
  '1 login and navigate folder',
  '2 lazy load 50 -> 60, no pagination',
  '3 table mini thumbs -> grid medium thumbs',
  '4 search photo_059 -> 1 row, reset -> lazy page',
  '5 select all/deselect one, count + indeterminate',
  '6 edit modal role/active, Escape closes, focus returns',
  '7 create token: secret once, hash prefix, revoke',
  '8 upload txt via UI and verify it appears',
  '9 short link icon sits between checkbox and preview',
  '10 select all found across lazy pages, selection survives filter',
  '11 upload queue guard and filter auto-apply',
  '12 delete and move update rows without reload',
  '13 keyboard focus and user chip',
];

async function runStep(name, fn) {
  try {
    await fn();
    stepResults.push({ name, status: 'PASS' });
    console.log(`PASS ${name}`);
  } catch (error) {
    stepResults.push({ name, status: 'FAIL', error: error?.message || String(error) });
    console.log(`FAIL ${name}: ${error?.message || error}`);
    throw error;
  }
}

function rowNames(page) {
  return page.locator('#view-table .file-row td:nth-child(3) a').allTextContents();
}

const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH || '/usr/bin/google-chrome',
  headless: true,
  args: ['--no-sandbox'],
});
const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
const page = await context.newPage();
page.on('pageerror', (error) => browserErrors.push(`pageerror: ${error}`));
page.on('console', (message) => {
  if (message.type() === 'error') browserErrors.push(`console.error: ${message.text()}`);
});

let currentStep = 'startup';
try {
  await runStep(allSteps[0], async () => {
    currentStep = allSteps[0];
    await page.goto(`${BASE}/login`);
    await page.fill('input[name="username"]', USER);
    await page.fill('input[name="password"]', PASSWORD);
    await Promise.all([
      page.waitForURL((url) => url.pathname.startsWith('/f/'), { timeout: 15000 }),
      page.click('button[type="submit"]'),
    ]);
    await page.goto(FOLDER_URL);
    await page.waitForSelector('#view-table .file-row');
    assert.match(page.url(), new RegExp(`/f/${FOLDER_ID}`), 'folder URL');
    const title = await page.locator('.card-files h2').innerText();
    assert.match(title, /Public/, 'folder heading');
  });

  await runStep(allSteps[1], async () => {
    currentStep = allSteps[1];
    assert.equal(await page.locator('#view-table .file-row').count(), 50, 'initial lazy page rows');
    const paginationLinks = await page.locator('.pagination a, a.page-link, .page-link').count();
    assert.equal(paginationLinks, 0, 'no pagination links on initial page');

    await page.locator('.files-content').evaluate((el) => {
      el.scrollTop = el.scrollHeight;
    });
    await page.waitForFunction(
      () => document.querySelectorAll('#view-table .file-row').length === 60,
      null,
      { timeout: 15000 },
    );
    assert.equal(await page.locator('#view-table .file-row').count(), 60, 'all rows after lazy load');
    const loaderHidden = await page.locator('#lazyLoader').evaluate((el) => el.hidden);
    assert.equal(loaderHidden, true, 'lazy loader hidden after last page');
    const lazyStatus = await page.locator('#lazyStatus').innerText();
    assert.match(lazyStatus, /Все файлы загружены/);
    assert.equal(await page.locator('.pagination a, a.page-link, .page-link').count(), 0, 'no pagination after lazy load');
  });

  await runStep(allSteps[2], async () => {
    currentStep = allSteps[2];
    const miniSrc = await page.locator('#view-table .file-row img.thumb-sm').first().getAttribute('src');
    assert.ok(miniSrc && miniSrc.startsWith('/thumb/mini/'), `table thumb is mini: ${miniSrc}`);
    const miniCount = await page.locator('#view-table .file-row img.thumb-sm[src^="/thumb/mini/"]').count();
    assert.equal(miniCount, 60, 'all table thumbs use /thumb/mini/');

    await Promise.all([
      page.waitForURL((url) => url.searchParams.get('view') === 'grid', { timeout: 15000 }),
      page.click('.view-btn[data-view="grid"]'),
    ]);
    await page.waitForSelector('#view-grid .tile[data-file-id]');
    assert.equal(await page.locator('#view-grid .tile[data-file-id]').count(), 50, 'grid initial lazy page');
    const mediumSrc = await page.locator('#view-grid .tile img.tile-preview').first().getAttribute('src');
    assert.ok(mediumSrc && mediumSrc.startsWith('/thumb/medium/'), `grid thumb is medium: ${mediumSrc}`);
    const mediumCount = await page.locator('#view-grid .tile img.tile-preview[src^="/thumb/medium/"]').count();
    assert.equal(mediumCount, 50, 'grid thumbs use /thumb/medium/');
  });

  await runStep(allSteps[3], async () => {
    currentStep = allSteps[3];
    await Promise.all([
      page.waitForURL((url) => url.searchParams.get('view') === 'table', { timeout: 15000 }),
      page.click('.view-btn[data-view="table"]'),
    ]);
    await page.waitForSelector('#view-table .file-row');

    await page.fill('.file-filters input[name="q"]', 'photo_059');
    await Promise.all([
      page.waitForURL((url) => url.searchParams.get('q') === 'photo_059', { timeout: 15000 }),
      page.click('.file-filters button[type="submit"]'),
    ]);
    await page.waitForSelector('#view-table .file-row');
    assert.equal(await page.locator('#view-table .file-row').count(), 1, 'search result rows');
    assert.match(await page.locator('#view-table .file-row').innerText(), /photo_059\.png/);
    assert.equal(await page.locator('#lazyLoader').evaluate((el) => el.hidden), true, 'no more pages in search');

    await Promise.all([
      page.waitForURL((url) => url.searchParams.get('q') === null && url.searchParams.get('view') === 'table', { timeout: 15000 }),
      page.click('.file-filters a:has-text("Сбросить")'),
    ]);
    await page.waitForSelector('#view-table .file-row');
    assert.equal(await page.locator('#view-table .file-row').count(), 50, 'reset returns initial lazy page');
    const nextUrl = await page.locator('#lazyLoader').getAttribute('data-next-url');
    assert.ok(nextUrl && /page=2/.test(nextUrl), `lazy loader has next page: ${nextUrl}`);
  });

  await runStep(allSteps[4], async () => {
    currentStep = allSteps[4];
    await page.check('#selectAll');
    const selectedAfterAll = (await page.locator('#selectedCount').innerText()).trim();
    assert.equal(selectedAfterAll, '50', 'selected count after select all');
    assert.equal(await page.locator('#selectAll').isChecked(), true);
    assert.equal(await page.locator('#selectAll').evaluate((el) => el.indeterminate), false);

    const firstCheckbox = page.locator('#view-table .file-row .file-checkbox').first();
    const deselectedName = await page.locator('#view-table .file-row').first().innerText();
    await firstCheckbox.uncheck();
    const selectedAfterOne = (await page.locator('#selectedCount').innerText()).trim();
    assert.equal(selectedAfterOne, '49', `selected count after deselect (${deselectedName})`);
    assert.equal(await page.locator('#selectAll').isChecked(), false, 'selectAll unchecked at 49/50');
    assert.equal(await page.locator('#selectAll').evaluate((el) => el.indeterminate), true, 'selectAll is indeterminate');
    // Leave a clean slate for the modal step.
    await page.click('#batchActions button:has-text("Снять выделение")');
    assert.equal(
      await page.locator('#batchActions').evaluate((el) => el.classList.contains('active')),
      false,
      'batch actions hidden after clearing selection',
    );
    assert.equal(await page.locator('#selectAll').evaluate((el) => el.indeterminate), false);
  });

  await runStep(allSteps[5], async () => {
    currentStep = allSteps[5];
    const editButton = page.locator('#view-table .file-row .actions-cell button[title="Редактировать"]').first();
    await Promise.all([
      page.waitForFunction(() => {
        const modal = document.getElementById('editModal');
        return modal && modal.classList.contains('active') && modal.getAttribute('aria-hidden') === 'false';
      }, null, { timeout: 15000 }),
      editButton.click(),
    ]);
    assert.equal(await page.locator('#editModal').getAttribute('role'), 'dialog');
    assert.equal(await page.locator('#editModal').getAttribute('aria-modal'), 'true');
    assert.equal(await page.locator('#editModal').evaluate((el) => el.classList.contains('active')), true);

    await page.keyboard.press('Escape');
    await page.waitForFunction(() => {
      const modal = document.getElementById('editModal');
      return modal && !modal.classList.contains('active') && modal.getAttribute('aria-hidden') === 'true';
    }, null, { timeout: 5000 });
    const focus = await page.evaluate(() => ({
      title: document.activeElement?.getAttribute?.('title') || '',
      inActions: !!document.activeElement?.closest?.('.actions-cell'),
      inFileRow: !!document.activeElement?.closest?.('.file-row'),
    }));
    assert.equal(focus.title, 'Редактировать', `focus returned to edit button (got ${JSON.stringify(focus)})`);
    assert.equal(focus.inActions, true, 'focused element is in row actions');
    assert.equal(focus.inFileRow, true, 'focused element is in a file row');
  });

  await runStep(allSteps[6], async () => {
    currentStep = allSteps[6];
    const tokenName = `e2e-upload-download-${Date.now()}`;
    await page.goto(`${BASE}/settings/tokens`);
    await page.waitForSelector('#tokenName');
    await page.fill('#tokenName', tokenName);
    await page.check('input[name="scopes"][value="download"]');
    await page.check('input[name="scopes"][value="upload"]');
    await page.selectOption('#tokenFolder', FOLDER_ID);
    await page.fill('#tokenExpires', '90');
    await Promise.all([
      page.waitForSelector('#newToken', { timeout: 15000 }),
      page.click('button:has-text("Создать токен")'),
    ]);
    const secret = (await page.locator('#newToken').innerText()).trim();
    assert.match(secret, /^alb_[0-9a-f]{48}$/, `token secret format (${secret})`);

    const bodyText = await page.locator('body').innerText();
    const occurrences = bodyText.split(secret).length - 1;
    assert.equal(occurrences, 1, 'secret appears exactly once on creation page');
    assert.equal(await page.locator('#newToken').count(), 1, 'one #newToken block');

    const tokenRow = page.locator('table tbody tr', { hasText: tokenName });
    assert.equal(await tokenRow.count(), 1, 'new token row in list');
    const rowText = await tokenRow.innerText();
    assert.equal(rowText.includes(secret), false, 'list row does not expose secret');
    const hashPrefix = (await tokenRow.locator('td code').innerText()).replace('…', '').trim();
    assert.match(hashPrefix, /^[0-9a-f]{12}$/, `hash prefix format (${hashPrefix})`);
    const expectedPrefix = createHash('sha256').update(secret).digest('hex').slice(0, 12);
    assert.equal(hashPrefix, expectedPrefix, 'hash prefix matches sha256(secret)[:12]');
    assert.match(rowText, /upload, download|download, upload/, 'scopes shown as upload+download');
    assert.match(rowText, /активен/, 'token status active');

    await page.goto(`${BASE}/settings/tokens`);
    await page.waitForSelector('#tokenName');
    const reloadedText = await page.locator('body').innerText();
    assert.equal(reloadedText.includes(secret), false, 'secret not shown after reload');
    assert.equal(await page.locator('#newToken').count(), 0, 'secret block gone after reload');
    const reloadedRow = page.locator('table tbody tr', { hasText: tokenName });
    assert.equal(await reloadedRow.count(), 1, 'token persists in list after reload');
    assert.ok((await reloadedRow.innerText()).includes(hashPrefix), 'hash prefix persists, secret does not');

    page.once('dialog', (dialog) => dialog.accept());
    await Promise.all([
      page.waitForURL((url) => url.searchParams.get('revoked') === '1', { timeout: 15000 }),
      reloadedRow.locator('button:has-text("Отозвать")').click(),
    ]);
    await page.waitForSelector('#tokenName');
    assert.equal(await page.locator('table tbody tr', { hasText: tokenName }).count(), 0, 'revoked token disappears');
    assert.equal(await page.locator('body').innerText().then((t) => t.includes(tokenName)), false, 'token name gone from page');
  });

  await runStep(allSteps[7], async () => {
    currentStep = allSteps[7];
    const uploadName = 'agent-upload.txt';
    await page.goto(FOLDER_URL);
    await page.waitForSelector('#view-table .file-row');
    await page.setInputFiles('#fileInput', {
      name: uploadName,
      mimeType: 'text/plain',
      buffer: Buffer.from('isolated e2e upload\n'),
    });
    await page.waitForFunction(() => !document.getElementById('uploadBtn').disabled, null, { timeout: 5000 });
    assert.match(await page.locator('#fileList').innerText(), new RegExp(uploadName));

    const navigation = page.waitForNavigation({ waitUntil: 'load', timeout: 20000 });
    await page.click('#uploadBtn');
    const toastText = await page.waitForSelector('#toast.show', { timeout: 5000 })
      .then((el) => el.innerText()).catch(() => '');
    await navigation;
    await page.waitForLoadState('load');
    assert.match(toastText, /Загружено/, `success toast (${toastText})`);

    const uploadedRow = page.locator('#view-table .file-row', { hasText: uploadName });
    await uploadedRow.waitFor({ timeout: 10000 });
    assert.equal(await uploadedRow.count(), 1, 'uploaded txt appears in file table');
    const footer = await page.locator('.status-bar').innerText();
    assert.match(footer, /Файлов:\s*61/, 'file counter includes upload');
    assert.match(await uploadedRow.innerText(), /TXT/i, 'uploaded row type is TXT');
  });
await runStep('9 short link icon sits between checkbox and preview', async () => {
  // Таблица: чекбокс, затем кнопка «ссылка», затем превью
  await page.click('.view-btn[data-view="table"]');
  await page.waitForTimeout(300);
  const table = await page.evaluate(() => {
    const head = document.querySelectorAll('#view-table thead th').length;
    const row = document.querySelector('#view-table .file-row');
    const cells = [...row.children];
    return {
      head,
      cells: cells.length,
      checkIndex: cells.findIndex((td) => td.querySelector('input[type=checkbox]')),
      linkIndex: cells.findIndex((td) => td.querySelector('button[title*="ссылк"]')),
      previewIndex: cells.findIndex((td) => td.querySelector('img.thumb-sm, .emoji-icon')),
      linkInActions: !!row.querySelector('.actions-cell button[title*="ссылк"]'),
    };
  });
  assert.equal(table.head, table.cells, 'число столбцов шапки и строки совпадает');
  assert.equal(table.checkIndex, 0, 'первый столбец — чекбокс');
  assert.equal(table.linkIndex, 1, 'ссылка идёт сразу после чекбокса');
  assert.equal(table.previewIndex, 2, 'превью идёт после ссылки');
  assert.equal(table.linkInActions, false, 'в действиях ссылки больше нет');

  // Список: тот же порядок
  await page.click('.view-btn[data-view="list"]');
  await page.waitForTimeout(300);
  const list = await page.evaluate(() => {
    const row = document.querySelector('#view-list .list-item[data-file-id]');
    const kind = (el) => {
      if (el.matches('input[type=checkbox]')) return 'checkbox';
      if (el.matches('button[title*="ссылк"]')) return 'link';
      if (el.matches('img.thumb-xs, .emoji-icon-sm')) return 'thumb';
      return 'other';
    };
    return [...row.children].slice(0, 3).map(kind);
  });
  assert.deepEqual(list, ['checkbox', 'link', 'thumb'], `порядок в списке: ${list.join(', ')}`);
  await page.click('.view-btn[data-view="table"]');
  await page.waitForTimeout(200);
});

await runStep('10 select all found across lazy pages, selection survives filter', async () => {
  await page.goto(FOLDER_URL);
  await page.waitForSelector('#view-table .file-row');
  const total = parseInt((await page.locator('.card-files h2 span').first().innerText()).match(/(\d+)/)[1], 10);
  const loaded = await page.locator('#view-table .file-row').count();
  assert.ok(total > loaded, `файлов ${total}, подгружено ${loaded} — нужен случай с несколькими страницами`);

  const foundBtn = page.locator('#selectAllFoundBtn');
  assert.equal(await foundBtn.count(), 1, 'кнопка «Выбрать все найденные» показана');
  await foundBtn.click();
  await page.waitForFunction((expected) => document.getElementById('selectedCount').textContent === String(expected), total, { timeout: 15000 });
  assert.equal(await page.locator('#selectedCount').innerText(), String(total),
    'выделены все найденные, а не только подгруженная страница');

  // выделение переживает применение фильтра (страница перезагружается)
  await page.fill('.file-filters input[name="q"]', 'photo_059');
  await Promise.all([
    page.waitForURL((url) => url.searchParams.get('q') === 'photo_059', { timeout: 15000 }),
    page.click('.file-filters button[type="submit"]'),
  ]);
  await page.waitForSelector('.file-row');
  assert.equal(await page.locator('#selectedCount').innerText(), String(total),
    'выделение восстановлено после перезагрузки');

  await page.click('#batchActions .btn-secondary');
  assert.equal(await page.locator('#selectedCount').innerText(), '0', 'снятие выделения работает');
  await Promise.all([
    page.waitForURL((url) => url.searchParams.get('q') === null, { timeout: 15000 }),
    page.click('.file-filters a:has-text("Сбросить")'),
  ]);
  await page.waitForSelector('#view-table .file-row');
});

await runStep('11 upload queue guard and filter auto-apply', async () => {
  await page.goto(FOLDER_URL);
  await page.waitForSelector('#view-table .file-row');

  // Очередь загрузки не должна теряться молча. Проверяем на действии, которое
  // всё ещё перезагружает страницу — создание папки (удаление больше не перезагружает).
  await page.setInputFiles('#fileInput', { name: 'guard.txt', mimeType: 'text/plain', buffer: Buffer.from('guard') });
  assert.equal(await page.locator('#fileList .file-item').count(), 1, 'файл в очереди');

  const messages = [];
  const dismiss = async (dialog) => { messages.push(dialog.message()); await dialog.dismiss(); };
  page.on('dialog', dismiss);
  try {
    await page.click('button:has-text("➕ Новая папка")');
    await page.fill('#newFolderName', 'GuardCheck');
    await page.click('#createFolderBtn');
    await page.waitForTimeout(500);
    assert.ok(messages.some((text) => /очереди загрузки/.test(text)), 'показано предупреждение об очереди');
    assert.equal(await page.locator('#fileList .file-item').count(), 1, 'очередь сохранена после отказа');
    assert.equal(await page.locator('#view-table .file-row').count(), 50, 'папка не создана: страница не перезагрузилась');
  } finally {
    page.off('dialog', dismiss);
    await page.locator('#createFolderModal .btn-secondary').click().catch(() => {});
  }

  // Селект фильтра применяется сразу, без кнопки «Применить»
  await Promise.all([
    page.waitForURL((url) => url.searchParams.get('sort') === 'name_asc', { timeout: 15000 }),
    page.selectOption('.file-filters select[name="sort"]', 'name_asc'),
  ]);
  await page.waitForSelector('#view-table .file-row');
});

await runStep('12 delete and move update rows without reload', async () => {
  await page.goto(FOLDER_URL);
  await page.waitForSelector('#view-table .file-row');

  // Папку-приёмник создаём заранее: само создание папки пока перезагружает страницу.
  await page.click('button:has-text("➕ Новая папка")');
  await page.fill('#newFolderName', 'MoveTarget');
  await Promise.all([
    page.waitForNavigation({ timeout: 15000 }),
    page.click('#createFolderBtn'),
  ]);
  await page.waitForSelector('#view-table .file-row');

  // Маркер документа: переживает правки DOM, но исчезает при перезагрузке.
  await page.evaluate(() => { window.__noReload = 'kept'; });

  // --- Удаление одного файла через модалку: строка исчезает без перезагрузки ---
  const rowsBeforeDelete = await page.locator('#view-table .file-row').count();
  const totalBeforeDelete = Number(await page.locator('#foundCount').innerText());
  await page.locator('#view-table .file-row .actions-cell button[title="Удалить"]').first().click();
  await page.locator('#deleteModal .btn-danger').click();
  await page.waitForFunction(
    (n) => document.querySelectorAll('#view-table .file-row').length === n - 1,
    rowsBeforeDelete, { timeout: 10000 });
  assert.equal(await page.evaluate(() => window.__noReload), 'kept', 'удаление не перезагружает страницу');
  assert.equal(await page.locator('#view-table .file-row').count(), rowsBeforeDelete - 1, 'строка удалена');
  assert.equal(Number(await page.locator('#foundCount').innerText()), totalBeforeDelete - 1,
    'счётчик «Найдено» уменьшился');

  // --- Перемещение выбранного файла в созданную папку: тоже без перезагрузки ---
  const rowsBeforeMove = await page.locator('#view-table .file-row').count();
  await page.locator('#view-table .file-row .checkbox-cell input').first().check();
  await page.locator('#batchActions button', { hasText: 'Переместить' }).click();
  await page.waitForFunction(() => {
    const select = document.getElementById('batchMoveFolderId');
    return !!select && Array.from(select.options).some((option) => option.textContent.includes('MoveTarget'));
  }, null, { timeout: 10000 });
  const targetFolderValue = await page.evaluate(() => {
    const select = document.getElementById('batchMoveFolderId');
    const option = Array.from(select.options).find((item) => item.textContent.includes('MoveTarget'));
    return option ? option.value : '';
  });
  assert.ok(targetFolderValue, 'папка MoveTarget есть в списке перемещения');
  await page.selectOption('#batchMoveFolderId', targetFolderValue);
  await page.click('#batchMoveBtn');
  await page.waitForFunction(
    (n) => document.querySelectorAll('#view-table .file-row').length === n - 1,
    rowsBeforeMove, { timeout: 10000 });
  assert.equal(await page.evaluate(() => window.__noReload), 'kept', 'перемещение не перезагружает страницу');
  assert.equal(await page.locator('#view-table .file-row').count(), rowsBeforeMove - 1,
    'перемещённая строка исчезла из текущей папки');
});

await runStep('13 keyboard focus and user chip', async () => {
  await page.goto(FOLDER_URL);
  await page.waitForSelector('#view-table .file-row');

  // Шапка: индикатор вошедшего пользователя и роль.
  const chip = page.locator('.user-chip');
  assert.equal(await chip.count(), 1, 'чип пользователя в шапке');
  const chipText = await chip.innerText();
  assert.match(chipText, /e2e/, 'логин в чипе');
  assert.match(chipText, /админ/i, 'бейдж роли');

  // Строка таблицы фокусируема с клавиатуры, действия имеют aria-label.
  const row = page.locator('#view-table .file-row').first();
  assert.equal(await row.getAttribute('tabindex'), '0', 'строка фокусируема');
  await row.focus();
  const active = await page.evaluate(() => ({
    inRow: !!document.activeElement?.closest?.('.file-row'),
  }));
  assert.ok(active.inRow, 'фокус остаётся на строке файла');

  const editButton = row.locator('button[title="Редактировать"]');
  const editLabel = await editButton.getAttribute('aria-label');
  assert.ok(editLabel && editLabel.startsWith('Редактировать'), 'aria-label у кнопки редактирования');
});

} catch (error) {
  console.log(`ABORT at step: ${currentStep}`);
} finally {
  await browser.close();
}

for (const name of allSteps) {
  if (!stepResults.some((item) => item.name === name)) {
    stepResults.push({ name, status: 'NOT RUN' });
  }
}
console.log('\n===== STEP RESULTS =====');
for (const result of stepResults) {
  console.log(`${result.status}\t${result.name}${result.error ? ` :: ${result.error}` : ''}`);
}
console.log('\n===== BROWSER ERRORS =====');
if (browserErrors.length === 0) console.log('none');
else browserErrors.forEach((entry) => console.log(entry));

const failed = stepResults.some((item) => item.status !== 'PASS');
console.log(`\nRESULT: ${failed ? 'FAIL' : 'PASS'}`);
process.exitCode = failed ? 1 : 0;
