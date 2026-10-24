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

- `/f/...`, `/api/...`, `/upload...`, `/export_xlsx`, `/api/export_zip` требуют сессию, полученную через `/login`. При отсутствии `auth.json` создаётся стартовая учётная запись `admin` / `альбион`; смените её в `/settings`. Повреждённый `auth.json` не перезаписывается — вход закрыт. Сессия постоянная с максимумом 12 ч (`ALBION_SESSION_HOURS`); при HTTPS обязательно `ALBION_COOKIE_SECURE=1`.
- Скачивание файла, просмотр и публичная страница папки доступны без сессии.
- Авторизованные `POST/PUT/PATCH/DELETE` защищены синхронизируемым CSRF-токеном: браузерная форма `/login` включает hidden token; для JSON API нужен заголовок `X-CSRF-Token` из `<meta name="csrf-token">` на админской странице. Неверный/отсутствующий токен даёт HTTP 403. Изменение логина/пароля аннулирует все ранее выданные admin-сессии; требуется повторный вход.
- Ответы приложения включают anti-framing (`X-Frame-Options: DENY`, CSP `frame-ancestors 'none'`) и `X-Content-Type-Options: nosniff`.
- Автоматика передаёт ключ только заголовком `X-API-Key` (query-параметр намеренно не поддерживается, чтобы секрет не утекал в URL/access-логи). Токены создаются на `/settings/tokens` или командой `python manage_tokens.py add|list|revoke ...`.
- По умолчанию анонимный бюджет — 10 GiB за час/IP; бюджет ключа — 100 GiB за сутки и разделяется по hash-id API-ключа. В памяти используется скользящее окно; с Redis — фиксированное окно часа/суток. Ответ при превышении: HTTP 429, заголовок `Retry-After` в секундах. Значения настраиваются `ALBION_DL_BUDGET_GB` и `ALBION_TOKEN_DL_BUDGET_GB`. Без `ALBION_REDIS_URL` счётчик хранится в памяти и сбрасывается при рестарте; с Redis состояние общее для процессов.
- `GET /export_all` по умолчанию доступен только из локальной сети за явно доверенным proxy, с API-ключом или действующей админ-сессией; в `/settings` это можно переключить. Приложение по умолчанию слушает только `127.0.0.1`; `ALBION_TRUST_PROXY=1` включает доверие одному proxy hop, а `ALBION_TRUSTED_PROXY_IPS` ограничивает адреса proxy peer. Лимит — не чаще раза в час на IP или hash-id API-ключа; размер архива расходует download budget. При настроенном Redis квота общая для процессов.
- Выделенный ZIP (`/api/export_zip`) требует админ-сессию; созданный `/z/<uuid>` публичен 24 часа. Проверка свободного места и запись ZIP сериализованы в рамках одного процесса.
- Один запрос загрузки ограничен `ALBION_MAX_UPLOAD_GB` (по умолчанию 50 ГиБ); общая квота учтённых файлов — `ALBION_STORAGE_QUOTA_GB`; для загрузок и ZIP всегда резервируется `ALBION_MIN_FREE_GB` свободного места. ZIP ограничен `ALBION_MAX_ZIP_GB` и `ALBION_MAX_ZIP_FILES`.
- Ошибки квот: `413` для слишком большого запроса/архива, `507` при нехватке диска/квоты, `411` если для загрузки с активными квотами не передан `Content-Length`.

## Страницы и скачивание

| Метод/путь | Доступ | Описание |
|---|---|---|
| `GET /` | публичный | Перенаправляет на `/f/` |
| `GET /login` | публичный | Форма входа. В режиме SQLite без учётных записей отдаёт 503 с подсказкой про `manage_users.py add-admin` |
| `POST /login` | публичный | Параметры формы: `username`, `password`; безопасный относительный `next`. При 10 неудачных попытках учётка блокируется на 15 минут (429), ответ «неверный логин или пароль» одинаков для несуществующего логина и неверного пароля |
| `GET /logout` | сессия | Отзывает серверную сессию и перенаправляет на логин |
| `GET /password` | сессия | Смена собственного пароля; при обязательной смене текущий пароль не запрашивается |
| `POST /password` | сессия + CSRF | Новый пароль: `current_password`, `new_password`, `new_password_confirm`. Прочие сессии учётки отзываются |
| `GET /settings/users` | админ | Управление учётными записями: список, роли, состояние, счётчик сессий |
| `POST /settings/users/create` | админ + CSRF | Создать учётку (`login`, `display_name`, `role`, `password`, `allow_remote`); пустой пароль — сгенерировать временный и показать один раз |
| `POST /settings/users/update` | админ + CSRF | Действия `activate`/`deactivate`, `to_admin`/`to_user`, `allow_remote`/`deny_remote`; понижение прав и запрет внешнего входа завершают сессии учётки |
| `POST /settings/users/reset` | админ + CSRF | Сбросить пароль (временный показывается один раз, смена обязательна, сессии отозваны) |
| `POST /settings/users/revoke` | админ + CSRF | Завершить все сессии указанной учётки |
| `POST /settings/users/delete` | админ + CSRF | Удалить учётку и её сессии; последнего администратора и себя удалить нельзя |
| `POST /settings/users/import` | админ + CSRF | Импорт CSV (`csv_text` или файл `csv_file`); обязательна колонка `login`, отчёт об отклонённых строках |
| `GET /f/` | сессия | Корень проводника; query: `q`, `ext`, `sort`, `per_page` (25/50/100), `page`, `view` (table/grid/list) |
| `GET /f/<folder_id>` | сессия | Содержимое папки в админке; те же фильтр/сортировка и активное представление; `page` используется lazy-загрузкой |
| `GET /settings` | админ | Форма квот; роль `user` получает 403 |
| `POST /settings` | админ + CSRF | Сохранить `max_upload_gb`, `storage_quota_gb`, `min_free_gb`, `max_zip_gb`, `max_zip_files`. В режиме SQLite учётные данные здесь не меняются — ими управляет раздел пользователей |
| `GET /settings/tokens` | сессия | Админская страница API-токенов и их срока действия |
| `POST /settings/tokens/create` | сессия + CSRF | Создать токен; секрет показывается один раз |
| `POST /settings/tokens/revoke` | сессия + CSRF | Отозвать токен по полному hash-id |
| `GET /d/<slug-or-uuid>` | публичный | Публичный список только файлов непосредственно в указанной папке; `page` используется lazy-загрузкой по 50 файлов, без перехода к родителю/подпапкам |
| `GET /<physical-path-or-file-slug-or-alias>` | публичный | Скачивание файла как вложения; `Content-Disposition` задаёт имя файла и увеличивается счётчик |
| `HEAD /<physical-path-or-file-slug-or-alias>` | публичный | Заголовки скачивания без тела; счётчики/бюджет не расходуются |
| `GET /thumb/<mini\|medium>/<file_id>` | публичный | Кэшированные WebP-превью без увеличения `view_count`; анонимный запрос учитывается в download budget |
| `GET /v/<physical-path>` | публичный | Inline-просмотр (например, изображение); увеличивает `view_count` и учитывает бюджет |
| `HEAD /v/<physical-path>` | публичный | Проверка заголовков без увеличения счётчика/бюджета |
| `POST /api/select_by_filter` | сессия + CSRF | Идентификаторы файлов и папок по текущему фильтру проводника (`folder_id`, `q`, `ext`) — для кнопки «Выбрать все найденные»; в ответе `file_ids`, `folder_ids`, `total`, `truncated` и `limit` (20 000). Только папка, указанная в запросе, без обхода вложенных |
| `GET /export_xlsx` | сессия | Скачать XLSX со списком физических и коротких ссылок |
| `GET /export_all` | LAN/API-ключ `download`/сессия | Скачать ZIP всего хранилища; раз в час на IP/ключ, расходует byte budget |
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

## Интеграция (машинные токены)

Токены выпускаются администратором для доверенных сервисов (Photobase, демо-сайт
и т. п.). Аутентификация — только заголовок `X-API-Key`; браузерная сессия и CSRF
для этих endpoint'ов не нужны и не принимаются. Секрет хранится в `tokens.json`
только как SHA-256, показывается один раз при создании и никогда не логируется.

### Выдача токена

Администратор открывает **⚙️ Настройки → 🔑 API-токены** (`/settings/tokens`) или
использует CLI:

```bash
python manage_tokens.py add "Photobase" --scopes upload,download --folder <uuid>
python manage_tokens.py add "Демо-сайт" --scopes upload,delete --folder <uuid> --expires-days 30
python manage_tokens.py add "Скрипт отчётов"          # только download, срок 90 дней
python manage_tokens.py add "Сервис" --scopes download --no-expiry
python manage_tokens.py list                          # права, папка, срок, статус
python manage_tokens.py revoke "Photobase"
```

По умолчанию новый токен действует **90 дней**; `--expires-days N` задаёт срок
1…3650 дней, `--no-expiry` создаёт бессрочный токен. Веб-форма показывает секрет
один раз и требует явно отметить бессрочность.

### Права и привязка к папке

| Поле `tokens.json` | Смысл |
|---|---|
| `scopes` | `download`, `upload`, `delete` или `*`. Нет поля — трактуется как `["download"]` (совместимость со старыми ключами) |
| `folder_id` | UUID папки; **обязателен** для `upload`, `delete` и `*`; токен может изменять только эту папку |
| `expires_at` | ISO-дата в UTC; после неё токен отклоняется (`403`). Отсутствие поля/`null` — бессрочный legacy-токен |

`*` нельзя сочетать с другими scopes. `download` разрешает использовать ключ для
`/export_all` и машинного бюджета скачивания; прямые публичные ссылки на файлы
по-прежнему открываются без ключа. Токен без привязки к папке не может загружать
или удалять файлы, даже если клиент передал `folder_id`.

### `GET /api/v1/integration/verify`

Проверяет токен, его права и привязку. Ничего не меняет. Требуется право `upload`
(токен только на скачивание получает `403`).

```bash
curl -H 'X-API-Key: alb_...' https://files.example.ru/api/v1/integration/verify
```

Успех `200`:

```json
{
  "ok": true,
  "name": "Photobase",
  "scopes": ["upload", "download"],
  "folder_id": "3f0c...",
  "token_expires_at": "2027-01-18T12:00:00+00:00",
  "base_url": "https://files.example.ru",
  "limits": {"max_upload_gb": 50}
}
```

### `POST /api/v1/integration/upload`

`multipart/form-data`, один файл в поле `file`.

| Поле | Обяз. | Смысл |
|---|---|---|
| `file` | да | один файл; расширение — из белого списка Albion |
| `folder_id` | нет | можно не передавать; если передан, должен совпадать с папкой токена |
| `display_name` | нет | имя в интерфейсе; по умолчанию имя файла |
| `ttl_days` | нет | только для расчёта `expires_at` (1…3650) |

```bash
curl -H 'X-API-Key: alb_...' \
     -F 'file=@photo.jpg' \
     -F 'display_name=Фото объекта' \
     -F 'ttl_days=90' \
     https://files.example.ru/api/v1/integration/upload
```

Успех `200`:

```json
{
  "file_id": "uuid",
  "url": "https://files.example.ru/korotkaya-ssylka",
  "short_url": "https://files.example.ru/korotkaya-ssylka",
  "object_key": "uploads/<folder>/photo.jpg",
  "name": "photo.jpg",
  "folder_id": "3f0c...",
  "expires_at": "2027-01-18T12:00:00+00:00"
}
```

Имя файла очищается (`+` → `_`), небезопасные имена (`..`, `/`, `\`, `\0`,
абсолютные пути) отклоняются. Совпадение имени с существующим файлом даёт
суффикс `_1`, `_2`, … — как у веб-загрузки. Запись создаётся в `files_db.json`
с теми же полями, что `/upload`; физическая запись атомарная (временный файл
рядом с целевым + `os.replace`).

`expires_at` — **метка для клиента**, а не политика удаления: Albion не удаляет
файлы по TTL. По умолчанию TTL — `ALBION_INTEGRATION_TTL_DAYS` (90 дней),
значение ограничивается диапазоном 1…3650. Это срок метаданных файла, а не срок
токена. Лимит частоты — 60 загрузок в минуту на токен (fallback — IP), ответ при
превышении `429`.

Коды ошибок:

| Код | Когда |
|---|---|
| `400` | нет файла, пустое/недопустимое имя, недопустимое расширение, некорректный `ttl_days`/форма |
| `401` | токена нет или он неверен (в том числе после отзыва) |
| `403` | нет права `upload`, токен не привязан к папке, чужая папка, токен просрочен |
| `404` | папки нет |
| `413` | превышен размер запроса/файла |
| `429` | превышен лимит запросов токена |
| `507` | нет свободного места/квоты |

Квоты и резерв `ALBION_MIN_FREE_GB` — те же, что у веб-загрузки. Токен не даёт
доступа к админским JSON-API и не создаёт браузерную сессию.

### `DELETE /api/v1/integration/files/<file_id>`

Удаляет файл и его запись из `files_db.json`. Требуется право `delete` и
обязательная привязка токена к папке; удалять можно только файлы этой папки
(`403` для чужой или для токена без папки).

```bash
curl -X DELETE -H 'X-API-Key: alb_...' \
     https://files.example.ru/api/v1/integration/files/<file_id>
```

Успех `200`: `{"deleted": true, "file_id": "..."}`. Ошибки: `401` — токен
неверен/отозван, `403` — нет права `delete`, нет привязки к папке или файл в
чужой папке, `404` — файл не найден, `400` — небезопасный путь в записи.

### Обратная совместимость токенов

Старые токены без поля `scopes` работают как `download`; записи без `expires_at`
остаются бессрочными. Старые токены `upload`/`delete` без `folder_id` после
обновления отклоняются — их нужно перевыпустить с привязкой к папке. Неверный
непустой `X-API-Key` на `/export_all` возвращает `401`, просроченный или без
права `download` — `403`; анонимный LAN-доступ проверяется отдельно.

## Состояние хранения и эксплуатация

- Данные находятся в `ALBION_DATA_DIR` (по умолчанию `.`): `uploads/`, JSON-БД, `auth.json`, `tokens.json`, `.secret_key`, логи. Секретные JSON-файлы и ключ сессии создаются с правами 0600.
- В Docker новый bind-mounted `./data` должен быть доступен контейнерному UID 10001. Перед запуском: `mkdir -p data && sudo chown -R 10001:10001 data`; затем `docker compose exec app python change_password.py`.
- `/export_all` и выделенные ZIP используют дисковое место. ZIP по выделению хранится в `ziplinks/` до 24 часов; `/export_all` оставляет временный архив в `uploads/` до последующей очистки старше двух часов.
- JSON-БД пишутся атомарно через временный файл и `os.replace`; повреждённая БД сохраняется как `.corrupt-*`.
- Для домена офиса задайте `ALBION_BASE_URL=https://files.example.ru`; без этого ссылки в интерфейсе используют cloudpub origin по умолчанию. За reverse proxy задайте `ALBION_TRUST_PROXY=1`, ограничьте `ALBION_TRUSTED_PROXY_IPS` адресом proxy и перезаписывайте `X-Forwarded-For`/`X-Forwarded-Proto`; пример nginx — `deploy/nginx.conf.example`.
