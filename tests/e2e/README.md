# Playwright E2E

Изолированные пользовательские сценарии для Albion. Тесты запускаются только
против одноразового сервера на `127.0.0.1` с временным `ALBION_DATA_DIR`; они не
касаются production и не используют реальные файлы.

## Требования

- Python-окружение проекта (`venv`) и зависимости из `requirements.txt`.
- Bun: <https://bun.sh/>.
- Chromium/Google Chrome. По умолчанию используется `/usr/bin/google-chrome`;
  другой путь задаётся через `CHROME_PATH`.

## Запуск

```bash
cd tests/e2e
bun install
./run_e2e.sh
```

Runner сам:

- создаёт временный каталог данных и тестового пользователя `e2e`;
- поднимает сервер на `127.0.0.1:18081` (можно изменить `ALBION_E2E_PORT`);
- запускает `public.spec.mjs`, `mobile.spec.mjs`, затем `admin.spec.mjs`;
- останавливает сервер и удаляет временные данные через `trap`.

Отдельные сценарии:

```bash
ALBION_E2E_BASE=http://127.0.0.1:18081 bun public.spec.mjs
ALBION_E2E_BASE=http://127.0.0.1:18081 bun mobile.spec.mjs
ALBION_E2E_BASE=http://127.0.0.1:18081 bun admin.spec.mjs
```

`admin.spec.mjs` выполняет вход `e2e / E2E-Password-2026!`, ленивую загрузку,
проверку mini/medium, фильтры, выбор, модальное окно, выдачу/отзыв токена и
загрузку тестового файла. `public.spec.mjs` проверяет публичную папку без входа,
WebP-превью и `view_count`. `mobile.spec.mjs` проверяет viewport meta и мобильную
ленивую загрузку.

## Что важно помнить

- Не направлять `ALBION_E2E_BASE` на боевой домен.
- Не запускать тесты параллельно на одном порту.
- `bun.lock` фиксирует версию Playwright; `node_modules` в репозиторий не входит.
