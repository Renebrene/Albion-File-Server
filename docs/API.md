# АЛЬБИОН — функции и HTTP API

## Основные возможности

- Веб-проводник: три режима отображения (таблица, плитка, список), хлебные крошки, создание/удаление папок, drag-and-drop перемещение файлов.
- Загрузка одиночных файлов, наборов файлов и папок. Папочная загрузка разбивается браузером на батчи по 25 файлов, до 4 параллельных запросов.
- Имена сохраняются с кириллицей; при загрузке, переименовании, скачивании и записи в ZIP `+` заменяется на `_`.
- Для файлов генерируются короткие транслитерированные ссылки; для папок — публичные страницы `/d/<slug>`. UUID-ссылки на папки остаются совместимыми.
- Учёт скачиваний и просмотров, статистика папки (суммарный размер, файлов и подкаталогов), XLSX-экспорт.
- Мультивыбор файлов/папок с ZIP-архивом и временной публичной ссылкой.
- API-ключи для автоматического скачивания; анонимные и ключевые лимиты по объёму; ZIP всего хранилища раз в час.

## Аутентификация и ограничения

- `/f/...`, `/api/...`, `/upload...`, `/export_xlsx`, `/api/export_zip` требуют сессию, полученную через `/login`. Если `auth.json` отсутствует/повреждён, вход закрыт (HTTP 503), без встроенных паролей. Сессия постоянная с максимумом 12 ч (`ALBION_SESSION_HOURS`); при HTTPS обязательно `ALBION_COOKIE_SECURE=1`.
- Скачивание файла, просмотр и публичная страница папки доступны без сессии.
- Авторизованные `POST/PUT/PATCH/DELETE` защищены синхронизируемым CSRF-токеном: браузерная форма `/login` включает hidden token; для JSON API нужен заголовок `X-CSRF-Token` из `<meta name="csrf-token">` на админской странице. Неверный/отсутствующий токен даёт HTTP 403.
- Автоматика передаёт ключ только заголовком `X-API-Key` (query-параметр намеренно не поддерживается, чтобы секрет не утекал в URL/access-логи). Скрипт создания/отзыва: `python manage_tokens.py add|list|revoke ...`.
- По умолчанию анонимный бюджет — 10 GiB за скользящий час/IP; бюджет ключа — 100 GiB за сутки и разделяется по hash-id API-ключа. Ответ при превышении: HTTP 429, заголовок `Retry-After` в секундах. Значения настраиваются `ALBION_DL_BUDGET_GB` и `ALBION_TOKEN_DL_BUDGET_GB`. Счётчик хранится в памяти и сбрасывается при рестарте.
- `GET /export_all` разрешён анонимно и с ключом, не чаще раза в час на IP или hash-id API-ключа; размер архива также расходует download budget.
- Выделенный ZIP (`/api/export_zip`) требует админ-сессию; созданный `/z/<uuid>` публичен 24 часа.

## Страницы и скачивание

| Метод/путь | Доступ | Описание |
|---|---|---|
| `GET /` | публичный | Перенаправляет на `/f/` |
| `GET /login` | публичный | Форма входа |
| `POST /login` | публичный | Параметры формы: `username`, `password`; безопасный относительный `next` |
| `GET /logout` | сессия | Удаляет флаг входа и перенаправляет на логин |
| `GET /f/` | сессия | Корень файлового проводника |
| `GET /f/<folder_id>` | сессия | Содержимое папки в админке (идентификатор — UUID) |
| `GET /d/<slug-or-uuid>` | публичный | Публичный список файлов/подпапок указанной папки |
| `GET /<physical-path-or-file-slug-or-alias>` | публичный | Скачивание файла как вложения; `Content-Disposition` задаёт имя файла и увеличивается счётчик |
| `HEAD /<physical-path-or-file-slug-or-alias>` | публичный | Заголовки скачивания без тела; счётчики/бюджет не расходуются |
| `GET /v/<physical-path>` | публичный | Inline-просмотр (например, изображение); увеличивает `view_count` и учитывает бюджет |
| `HEAD /v/<physical-path>` | публичный | Проверка заголовков без увеличения счётчика/бюджета |
| `GET /export_xlsx` | сессия | Скачать XLSX со списком физических и коротких ссылок |
| `GET /export_all` | публичный/API-ключ | Скачать ZIP всего хранилища; раз в час на IP/ключ, расходует byte budget |
| `GET /z/<uuid>` | публичный | Скачивание ZIP, созданного для выбранных элементов; архив удаляется через сутки |
| `GET /favicon.ico` | публичный | Иконка сайта (logo.png) |
| `GET /robots.txt` | публичный | Правила обхода для роботов |

## API (JSON/multipart)

Во всех операциях записи требуется админ-сессия и заголовок `X-CSRF-Token`; при отсутствии сессии API перенаправляет на `/login`, при ошибке токена возвращает 403. Токен доступен в meta-теге на странице `/f/`.

| Метод/путь | Тело/параметры | Успешный ответ |
|---|---|---|
| `POST /upload` | `multipart/form-data`: `count`, `file_0...file_N`, `folder_id` (необязательный) | `{"count": N}` |
| `POST /upload_folder` | multipart: `count`, `file_i`, `path_i` (относительный путь), `folder_id` | `{"count": N, "folders_created": N}` |
| `POST /api/create_folder` | JSON: `{"name": str, "parent_id": UUID|null}` | `{"success": true, "folder_id": UUID}` |
| `POST /api/update_file` | JSON: `internal_id`, `display_name`, `physical_filename`, `custom_alias`, `folder_id` | `{"success": true}` |
| `POST /api/move_file` | JSON: `{"file_id": UUID, "target_folder_id": UUID|null}` | `{"success": true}` |
| `POST /api/move_files_batch` | JSON: `{"file_ids": [UUID], "target_folder_id": UUID|null}` | `{"success": true, "count": N}` |
| `POST /api/delete_file` | JSON: `{"internal_id": UUID}` | `{"success": true}` |
| `POST /api/delete_files_batch` | JSON: `{"file_ids": [UUID]}` | `{"success": true, "count": N}` |
| `POST /api/delete_folder` | JSON: `{"folder_id": UUID}` | `{"success": true}` |
| `GET /api/get_all_folders` | — | `[{"id": UUID, "name": str, "depth": N}]` |
| `POST /api/export_zip` | JSON: `{"file_ids": [UUID], "folder_ids": [UUID]}` | `{"url": "https://.../z/<uuid>", "count": N, "size": str}` |

Ошибки API возвращаются JSON вида `{"error": "..."}` (валидация — 400, не найдено — 404, rate/budget — 429). `Retry-After` задаёт задержку перед повтором.

## API-ключи и автоматическое скачивание

Создание ключа (секрет выводится один раз):

```bash
python manage_tokens.py add "Скрипт отчётов"
python manage_tokens.py list
python manage_tokens.py revoke "Скрипт отчётов"
```

Скачать файл:

```bash
curl -fL -H 'X-API-Key: alb_...' -O 'https://files.example.ru/<slug>'
```

Скачать весь каталог одним архивом (не чаще раза в час):

```bash
curl -fL -H 'X-API-Key: alb_...' -o albion-files.zip 'https://files.example.ru/export_all'
```

Не передавайте ключ через query string: URL попадает в access-логи и историю клиента.

## Состояние хранения и эксплуатация

- Данные находятся в `ALBION_DATA_DIR` (по умолчанию `.`): `uploads/`, JSON-БД, `auth.json`, `tokens.json`, `.secret_key`, логи. Секретные JSON-файлы и ключ сессии создаются с правами 0600.
- В Docker новый bind-mounted `./data` должен быть доступен контейнерному UID 10001. Перед запуском: `mkdir -p data && sudo chown -R 10001:10001 data`; затем `docker compose exec app python change_password.py`.
- `/export_all` и выделенные ZIP используют дисковое место. ZIP по выделению хранится в `ziplinks/` до 24 часов; `/export_all` оставляет временный архив в `uploads/` до последующей очистки старше двух часов.
- JSON-БД пишутся атомарно через временный файл и `os.replace`; повреждённая БД сохраняется как `.corrupt-*`.
- Для домена офиса задайте `ALBION_BASE_URL=https://files.example.ru`; без этого ссылки в интерфейсе используют cloudpub origin по умолчанию. Внешний reverse proxy должен передавать `X-Forwarded-For`/`X-Forwarded-Proto`; пример nginx — `deploy/nginx.conf.example`.
