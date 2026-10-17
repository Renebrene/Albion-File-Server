# Архитектура и модель данных

## Компоненты

```text
Browser / curl / integration
          │ HTTPS public origin
          ▼
 nginx (TLS termination, bandwidth/connection controls)
          │ private network or loopback :5000
          ▼
 Flask app ─ Waitress (8 threads)
    ├── authenticated admin UI (Jinja template string + inline JS)
    ├── public file/view/folder/archive routes
    ├── Flask-Limiter (memory://)
    └── JSON files + filesystem under ALBION_DATA_DIR
```

`server.py` содержит Flask-приложение, HTML/CSS/JS админки, маршруты и запуск Waitress. Это небольшой одно-процессный файловый сервер, а не multi-worker application. Для production за пределами текущего масштаба рекомендуется вынести метаданные в SQLite/PostgreSQL и rate-limit state в Redis.

## Каталог данных

`ALBION_DATA_DIR` по умолчанию `.`. При значении `/srv/albion/data`:

```text
/srv/albion/data/
├── uploads/          физические файлы, структура каталогов совпадает с folders_db
├── ziplinks/         ZIP по выделению; публичные UUID-ссылки, срок жизни 24 ч
├── files_db.json     метаданные файлов
├── folders_db.json   метаданные папок и slug
├── stats.json        время последней загрузки/XLSX-выгрузки
├── settings.json     квоты, изменяемые администратором в /settings (0600)
├── auth.json         единственный admin username + werkzeug password hash
├── tokens.json       SHA-256 ключей автоматики → name/created
├── .secret_key       Flask signing key, нужен для сохранения сессий
├── albion.log        лог приложения, ротация 5 MiB × 3
└── .corrupt-*        резерв повреждённого JSON при ошибке чтения
```

`files_db.json` — объект, ключом является внутренний UUID. Поля записи: `access_id`, `current_filename`, `physical_path` (относительный путь внутри uploads), `original_filename`, `display_name`, `folder_id` (UUID либо null), `custom_alias`, `slug`, `link_rotated`, `download_count`, `view_count`, `size`, `mtime`, `ext`, `is_image`.

`folders_db.json` — объект UUID → `name`, `parent_id`, `created`, `slug`. Публичный `/d/<slug>` ищет короткий slug; ссылки `/d/<uuid>` также поддержаны.

## Путь масштабирования на SQLite/PostgreSQL

Сначала вынести `files` и `folders` в таблицы с полями из JSON-записей, добавив индексы по `folder_id`, `slug`, `custom_alias` и `physical_path`. Счётчики обновлять атомарным `UPDATE ... SET download_count = download_count + 1` в транзакции, а не read-modify-write JSON. Затем заменить `load_db/save_db` на repository-функции. SQLite подходит для нескольких процессов на одной машине при WAL + `busy_timeout`; PostgreSQL — для нескольких хостов/реплик. Redis оставить для rate-limit/budget/export quota. `uploads/` должно быть общим сетевым/filesystem volume, а ZIP/temp-файлы — локальными для процесса либо в объектном хранилище.

`tokens.json` имеет вид `{sha256(raw_key): {"name": ..., "created": ...}}`. Открытый API-ключ показывается только один раз командой `manage_tokens.py add`.

## Согласованность и конкуренция

- Запись JSON выполняется во временный файл и `os.replace`; повреждённый JSON сохраняется в `.corrupt-*` вместо тихого затирания пустым объектом.
- `DB_LOCK` защищает одиночные read-modify-write счётчиков и атомарные JSON-записи.
- `UPLOAD_LOCK` сериализует загрузки и административные DB-мутации в рамках одного процесса. Сетевой приём multipart-запросов остаётся параллельным.
- `ensure_folder_path()` блокирует создание одинаковой цепочки папок разными параллельными батчами.
- Эти блокировки **не межпроцессные**. Не запускайте несколько процессов/реплик, разделяющих один `ALBION_DATA_DIR`, без миграции на БД с транзакциями/межпроцессными lock.

## Ссылки и доступ

- Физический путь — публичная ссылка скачивания; `slug` и `custom_alias` — альтернативные публичные ссылки.
- Файловый slug стабилен при переименовании физического файла; прямая ссылка по физическому имени меняется.
- Публичная папка показывает список её непосредственных файлов и подпапок; дочерние папки продолжают навигацию.
- Публичность URL означает, что секретность ссылки — не ACL. Любой получатель ссылки может скачать содержимое.
- `/api/export_zip` требует админ-сессию + CSRF, создаёт архив в `ziplinks/`; ссылка `/z/<uuid>` доступна без входа до суток.
- `/export_all` по умолчанию разрешён только private/LAN-клиентам, API-ключу или админ-сессии; ограничен одним созданием ZIP в час на IP/API-key scope и учитывается в byte budget. Режим переключается в `/settings`.

## Защита и ограничения

- Сессия подписана `.secret_key`; cookie имеет `SameSite=Lax`; `Secure` включается `ALBION_COOKIE_SECURE=1`.
- Админские мутации проверяют session + synchronizer CSRF token. UI передаёт его hidden field login form и `X-CSRF-Token` для fetch/XHR.
- API-key передаётся только `X-API-Key`, не query string.
- Анонимный byte budget по умолчанию 10 GiB/час/IP; API key — 100 GiB/сутки/hash-id ключа. Без `ALBION_REDIS_URL` состояние в памяти сбрасывается при рестарте; с Redis бюджеты и часовая квота `/export_all` общие для процессов.
- `Flask-Limiter` использует `memory://` по умолчанию; `ALBION_RATE_LIMIT_STORAGE_URI` можно указать `redis://...` для общего состояния. Приватные IPv4/loopback исключены из request-rate limit; byte budget остаётся.
- Загрузка ограничена `ALBION_MAX_UPLOAD_GB` (50 ГиБ по умолчанию); Flask получает `MAX_CONTENT_LENGTH`, Waitress — тот же предел. `MAX_FORM_PARTS=50000`. `ALBION_MIN_FREE_GB` резервирует свободное место, `ALBION_STORAGE_QUOTA_GB` ограничивает учтённый объём, а ZIP — `ALBION_MAX_ZIP_GB`/`ALBION_MAX_ZIP_FILES`.
- JSON-БД не транзакционны между процессами. При нескольких workers/репликах нужна общая SQLite/PostgreSQL-БД; Redis не заменяет транзакции файловых метаданных.
- Короткие пароли, отсутствие резервных копий, публичный `/export_all` и доступные извне ссылки — операционные риски, которые должен оценить владелец данных.
