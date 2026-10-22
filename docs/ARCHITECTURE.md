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
├── thumbnails/       кэш WebP-превью mini/medium; можно безопасно удалить для перегенерации
├── files_db.json     метаданные файлов
├── folders_db.json   метаданные папок и slug
├── stats.json        время последней загрузки/XLSX-выгрузки
├── settings.json     квоты, изменяемые администратором в /settings (0600)
├── auth.json         единственный admin username + werkzeug password hash
├── tokens.json       SHA-256 ключей автоматики → name/created
├── albion.db         метаданные в SQLite (режим ALBION_METADATA_BACKEND=sqlite, 0600)
├── backups/          ежедневные копии метаданных (db + JSON + MANIFEST.json)
├── .secret_key       Flask signing key, нужен для сохранения сессий
├── albion.log        лог приложения, ротация 5 MiB × 3
└── .corrupt-*        резерв повреждённого JSON при ошибке чтения
```

`files_db.json` — объект, ключом является внутренний UUID. Поля записи: `access_id`, `current_filename`, `physical_path` (относительный путь внутри uploads), `original_filename`, `display_name`, `folder_id` (UUID либо null), `custom_alias`, `slug`, `link_rotated`, `download_count`, `view_count`, `size`, `mtime`, `ext`, `is_image`.

`folders_db.json` — объект UUID → `name`, `parent_id`, `created`, `slug`. Публичный `/d/<slug>` ищет короткий slug; ссылки `/d/<uuid>` также поддержаны.

## Хранилище метаданных: JSON и SQLite

Режим выбирает `ALBION_METADATA_BACKEND`:

- `json` (по умолчанию) — прежние файлы `files_db.json`, `folders_db.json`, `tokens.json`, `stats.json`, `settings.json`;
- `sqlite` — одна база `albion.db` (WAL, `busy_timeout=5000`, внешние ключи) в каталоге данных.

Слой `db.py` держит **ту же модель данных**, что и JSON: наборы читаются и пишутся словарями
`{id: record}`, поэтому весь код маршрутов работает без изменений, а переключение режима
обратимо. Наборы маршрутизируются в одном месте (`_sqlite_target` в `server.py`), так что ни
один вызов `_load_json`/`_save_json` не может пройти мимо активного хранилища.

Схема подготовлена сразу под следующие этапы: `sections`, `users`, `sessions`, `links`,
`audit`, `quota_usage` создаются вместе с `files`, `folders`, `tokens`, `documents`.
Колонки дублируют поля записей (для индексов и запросов), а полная запись хранится в
JSON-поле `data` — это даёт сверку «байт в байт» при миграции и сохранность полей, которые
пока не вынесены в колонки.

Запись набора — одна транзакция `BEGIN IMMEDIATE`: обновляются только реально изменившиеся
записи (сравнение сериализованного вида), отсутствующие в словаре удаляются. Отсюда два
следствия: параллельные загрузки не теряют записи (проверено тестом на 50 потоков), а
`mutate_files/mutate_folders/mutate_tokens` дают атомарный read-modify-write, в том числе
между процессами — именно так CLI `manage_tokens.py` и веб-выдача не конфликтуют.

Ограничение: SQLite-файл **нельзя** размещать на NFS/SMB — WAL требует локальной ФС.
`uploads/` при этом может жить на сетевом хранилище.

## Путь масштабирования на PostgreSQL

Дальше: перевести маршруты с «прочитать весь набор» на запросы по записям (репозитории),
заменить общий `DB_LOCK` на транзакции БД, вынести счётчики в `UPDATE ... SET x = x + 1`.
PostgreSQL нужен при нескольких хостах/репликах; на одной машине SQLite WAL достаточен.
Redis оставить для rate-limit/budget/export quota. ZIP/temp-файлы — локальными для процесса
либо в объектном хранилище.

`tokens.json` имеет вид `{sha256(raw_key): {"name", "created", "created_at", "scopes", "folder_id", "expires_at"}}`. Открытый API-ключ показывается только один раз при создании в `/settings/tokens` или командой `manage_tokens.py add`; новые токены по умолчанию действуют 90 дней. Записи старых версий без `scopes` трактуются как `download`, без `expires_at` — как бессрочные. `upload`, `delete` и `*` обязаны иметь `folder_id`; `/export_all` принимает API-ключ только со scope `download` или `*`. Запись `tokens.json` сериализуется блокировкой `tokens.json.lock`, общей для CLI и веб-выдачи.

## Согласованность и конкуренция

- Запись JSON выполняется во временный файл и `os.replace`; повреждённый JSON сохраняется в `.corrupt-*` вместо тихого затирания пустым объектом.
- `DB_LOCK` защищает одиночные read-modify-write счётчиков и атомарные JSON-записи.
- `UPLOAD_LOCK` сериализует загрузки и административные DB-мутации в рамках одного процесса. Сетевой приём multipart-запросов остаётся параллельным.
- `ZIP_BUILD_LOCK` сериализует проверку дискового резерва и создание ZIP по выборке/полного экспорта; блокировка действует только внутри одного процесса.
- `ensure_folder_path()` блокирует создание одинаковой цепочки папок разными параллельными батчами.
- Эти блокировки **не межпроцессные**. В частности, `ZIP_BUILD_LOCK` защищает свободное место лишь внутри одного Waitress-процесса. Не запускайте несколько процессов/реплик, разделяющих один `ALBION_DATA_DIR`, без миграции на БД с транзакциями и межпроцессной координацией.

## Ссылки и доступ

- Физический путь — публичная ссылка скачивания; `slug` и `custom_alias` — альтернативные публичные ссылки.
- Файловый slug стабилен при переименовании физического файла; прямая ссылка по физическому имени меняется.
- Публичная папка показывает только файлы непосредственно в ней; ссылки на родительские и дочерние папки не выводятся. Каждую дочернюю папку нужно публиковать отдельной ссылкой.
- Админский проводник рендерит только активное представление (`table`/`grid`/`list`); фильтры, сортировка и view хранятся в query string. Файлы подгружаются лениво по 50, публичные папки используют тот же механизм.
- Превью `/thumb/<mini|medium>/<file_id>` не увеличивает `view_count`; счётчик меняется только при явном открытии `/v/<path>`. Анонимные превью учитываются в download budget, чтобы не обходить ограничение массового скачивания. Изображения используют `loading="lazy"` и `decoding="async"`.
- Публичность URL означает, что секретность ссылки — не ACL. Любой получатель ссылки может скачать содержимое.
- `/api/export_zip` требует админ-сессию + CSRF, создаёт архив в `ziplinks/`; ссылка `/z/<uuid>` доступна без входа до суток.
- `/export_all` по умолчанию разрешён private/LAN-клиентам за настроенным доверенным proxy, API-ключу или действующей админ-сессии; forwarded IP учитывается только если peer входит в `ALBION_TRUSTED_PROXY_IPS`. Маршрут ограничен одним созданием ZIP в час на IP/API-key scope и учитывается в byte budget. Режим переключается в `/settings`.

## Защита и ограничения

- Если `auth.json` отсутствует при первом запуске, создаётся bootstrap-учётка `admin / альбион`; пароль хранится только как scrypt-хеш и меняется в `/settings`. Это удобно для первого входа, но пароль необходимо сменить до публикации в интернет.
- Сессия подписана `.secret_key`; cookie имеет `SameSite=Lax`; `Secure` включается `ALBION_COOKIE_SECURE=1`. Сессионный fingerprint меняется вместе с логином/password hash, поэтому смена credentials через UI или `change_password.py` отзывает старые админ-сессии.
- Ответы приложения содержат `X-Frame-Options: DENY`, CSP `frame-ancestors 'none'`, `X-Content-Type-Options: nosniff` и `Referrer-Policy`.
- Админские мутации проверяют session + synchronizer CSRF token. UI передаёт его hidden field login form и `X-CSRF-Token` для fetch/XHR.
- API-key передаётся только `X-API-Key`, не query string.
- Анонимный byte budget по умолчанию 10 GiB/час/IP; API key — 100 GiB/сутки/hash-id ключа. Без `ALBION_REDIS_URL` состояние в памяти сбрасывается при рестарте; с Redis бюджеты и часовая квота `/export_all` общие для процессов.
- `Flask-Limiter` использует `memory://` по умолчанию; `ALBION_RATE_LIMIT_STORAGE_URI` можно указать `redis://...` для общего состояния. Приватные IPv4/loopback исключены из request-rate limit; byte budget остаётся.
- Загрузка ограничена `ALBION_MAX_UPLOAD_GB` (50 ГиБ по умолчанию); Flask получает `MAX_CONTENT_LENGTH`, Waitress — тот же предел. `MAX_FORM_PARTS=50000`. `ALBION_MIN_FREE_GB` резервирует свободное место, `ALBION_STORAGE_QUOTA_GB` ограничивает учтённый объём, а ZIP — `ALBION_MAX_ZIP_GB`/`ALBION_MAX_ZIP_FILES`.
- JSON-БД не транзакционны между процессами. При нескольких workers/репликах нужна общая SQLite/PostgreSQL-БД; Redis не заменяет транзакции файловых метаданных.
- Короткие пароли, отсутствие резервных копий, публичный `/export_all` и доступные извне ссылки — операционные риски, которые должен оценить владелец данных.
