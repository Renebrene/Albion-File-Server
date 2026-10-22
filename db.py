"""SQLite-хранилище метаданных АЛЬБИОН.ДИСК.

Заменяет JSON-файлы (files_db.json, folders_db.json, tokens.json, stats.json,
settings.json) одной локальной базой в режиме WAL. База лежит рядом с JSON и
никогда не выносится на сетевые диски.

Совместимость: наборы данных читаются и пишутся как обычные словари Python
(``{id: record}``) — как это делал JSON-слой, поэтому вызывающий код в server.py
не меняется, переключается только backend.

Переключение — переменная окружения ``ALBION_METADATA_BACKEND``:

    json   (по умолчанию) — прежнее поведение, JSON-файлы;
    sqlite                — чтение и запись через SQLite.

Откат: вернуть флаг ``json``. JSON-файлы при миграции не удаляются.

CLI:
    python db.py init                — создать схему
    python db.py migrate             — перенести данные JSON → SQLite со сверкой
    python db.py verify              — сверить SQLite с JSON
    python db.py stats               — показать содержимое базы
    python db.py backup --dest DIR   — согласованная копия базы
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import threading
from datetime import datetime, timezone

DB_FILE_NAME = 'albion.db'
BACKEND_ENV = 'ALBION_METADATA_BACKEND'
DEFAULT_BACKEND = 'json'
SQLITE_TIMEOUT = 5.0

JSON_FILES = {
    'files': 'files_db.json',
    'folders': 'folders_db.json',
    'tokens': 'tokens.json',
    'stats': 'stats.json',
    'settings': 'settings.json',
}

FILE_COLUMNS = (
    'folder_id', 'slug', 'physical_path', 'original_filename', 'size', 'ext',
    'is_image', 'is_video', 'download_count', 'view_count',
)
FOLDER_COLUMNS = ('parent_id', 'name', 'slug', 'physical_path')
TOKEN_COLUMNS = ('name', 'scopes', 'folder_id', 'created_at', 'expires_at', 'revoked_at')

_local = threading.local()


class DbError(RuntimeError):
    """Ошибка слоя метаданных с понятным сообщением для лога и CLI."""


# ===== BACKEND =====

def backend():
    value = (os.environ.get(BACKEND_ENV) or DEFAULT_BACKEND).strip().lower()
    return 'sqlite' if value == 'sqlite' else 'json'


def sqlite_enabled():
    return backend() == 'sqlite'


def data_dir():
    return os.environ.get('ALBION_DATA_DIR', '.')


def db_path(directory=None):
    explicit = os.environ.get('ALBION_DB_FILE')
    if explicit:
        return explicit
    return os.path.join(directory or data_dir(), DB_FILE_NAME)


def _now():
    return datetime.now(timezone.utc).isoformat()


# ===== СОЕДИНЕНИЕ =====

def connect(path=None):
    """Соединение текущего потока: WAL, busy_timeout, внешние ключи.

    Если схема ещё не создана (пустой каталог данных, первый запуск),
    она создаётся сразу: чтение настроек и статистики происходит раньше,
    чем любой явный вызов init_schema().
    """
    path = path or db_path()
    conn = getattr(_local, 'conn', None)
    if conn is not None and getattr(_local, 'path', None) == path:
        return conn
    if conn is not None:
        conn.close()
    directory = os.path.dirname(path) or '.'
    os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, timeout=SQLITE_TIMEOUT, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute(f'PRAGMA busy_timeout={int(SQLITE_TIMEOUT * 1000)}')
    conn.execute('PRAGMA foreign_keys=ON')
    _local.conn = conn
    _local.path = path
    if not _has_schema(conn):
        _apply_schema(conn, path)
    return conn


def _has_schema(conn):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone()
    return row is not None


def _apply_schema(conn, path=None):
    conn.executescript(SCHEMA)
    conn.execute('INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (?, ?)',
                 (SCHEMA_VERSION, _now()))
    try:
        os.chmod(path or db_path(), 0o600)
    except OSError:
        pass
    return conn


def close():
    conn = getattr(_local, 'conn', None)
    if conn is not None:
        conn.close()
        _local.conn = None
        _local.path = None


class _Transaction:
    """Транзакция записи. BEGIN IMMEDIATE — писатель один, ожидание снимает busy_timeout."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.execute('BEGIN IMMEDIATE')
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute('ROLLBACK' if exc_type else 'COMMIT')
        return False


def transaction(conn=None):
    return _Transaction(conn or connect())


# ===== СХЕМА =====

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sections (
    id TEXT PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    root_path TEXT NOT NULL,
    policy TEXT NOT NULL DEFAULT '{}',
    public_links INTEGER NOT NULL DEFAULT 0,
    allow_upload INTEGER NOT NULL DEFAULT 1,
    owner_edit_only INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    login TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    display_name TEXT,
    email TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    allow_remote INTEGER NOT NULL DEFAULT 0,
    totp_secret TEXT,
    created_at TEXT NOT NULL,
    last_login_at TEXT,
    failed_logins INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    data TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    last_seen TEXT,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    ip TEXT,
    user_agent TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS folders (
    id TEXT PRIMARY KEY,
    section_id TEXT,
    parent_id TEXT,
    name TEXT NOT NULL DEFAULT '',
    owner_id TEXT,
    slug TEXT,
    physical_path TEXT,
    created_at TEXT,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_folders_parent ON folders(parent_id);
CREATE INDEX IF NOT EXISTS idx_folders_section ON folders(section_id);

CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,
    section_id TEXT,
    folder_id TEXT,
    owner_id TEXT,
    slug TEXT,
    physical_path TEXT,
    original_filename TEXT,
    size INTEGER NOT NULL DEFAULT 0,
    ext TEXT,
    is_image INTEGER NOT NULL DEFAULT 0,
    is_video INTEGER NOT NULL DEFAULT 0,
    download_count INTEGER NOT NULL DEFAULT 0,
    view_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT,
    updated_at TEXT,
    deleted_at TEXT,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_slug ON files(slug);
CREATE INDEX IF NOT EXISTS idx_files_physical ON files(physical_path);
CREATE INDEX IF NOT EXISTS idx_files_folder ON files(folder_id);
CREATE INDEX IF NOT EXISTS idx_files_owner ON files(owner_id);
CREATE INDEX IF NOT EXISTS idx_files_section ON files(section_id);

CREATE TABLE IF NOT EXISTS links (
    id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    token_hash TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    revoked_at TEXT,
    hits INTEGER NOT NULL DEFAULT 0,
    last_hit_at TEXT,
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_links_target ON links(target_type, target_id);

CREATE TABLE IF NOT EXISTS tokens (
    key_hash TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    scopes TEXT NOT NULL DEFAULT '[]',
    section_id TEXT,
    folder_id TEXT,
    created_by TEXT,
    created_at TEXT,
    expires_at TEXT,
    revoked_at TEXT,
    last_used_at TEXT,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tokens_folder ON tokens(folder_id);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor_id TEXT,
    actor_label TEXT,
    action TEXT NOT NULL,
    object_type TEXT,
    object_id TEXT,
    before_json TEXT,
    after_json TEXT,
    ip TEXT,
    user_agent TEXT,
    request_id TEXT,
    result TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit(actor_id, ts);

CREATE TABLE IF NOT EXISTS documents (
    name TEXT PRIMARY KEY,
    body TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quota_usage (
    user_id TEXT PRIMARY KEY,
    bytes INTEGER NOT NULL DEFAULT 0,
    files INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
"""


def init_schema(path=None):
    conn = connect(path)
    return _apply_schema(conn, path)


# ===== ПРЕОБРАЗОВАНИЕ ЗАПИСЕЙ В КОЛОНКИ =====

def _as_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _bool_int(value):
    return 1 if value else 0


def _file_values(record):
    return (
        record.get('folder_id'), record.get('slug'), record.get('physical_path'),
        record.get('original_filename'), _as_int(record.get('size')), record.get('ext'),
        _bool_int(record.get('is_image')), _bool_int(record.get('is_video')),
        _as_int(record.get('download_count')), _as_int(record.get('view_count')),
    )


def _folder_values(record):
    return (
        record.get('parent_id'), record.get('name') or '',
        record.get('slug'), record.get('physical_path'),
    )


def _token_values(record):
    scopes = record.get('scopes')
    if isinstance(scopes, (list, tuple)):
        scopes = json.dumps(list(scopes), ensure_ascii=False)
    elif scopes is None:
        scopes = '[]'
    return (
        record.get('name') or '', scopes, record.get('folder_id'),
        record.get('created_at') or record.get('created'),
        record.get('expires_at'), record.get('revoked_at'),
    )


def _values_for(table, record):
    if table == 'files':
        return _file_values(record)
    if table == 'folders':
        return _folder_values(record)
    raise DbError(f'Неизвестная таблица: {table}')


def _columns_for(table):
    return FILE_COLUMNS if table == 'files' else FOLDER_COLUMNS


# ===== ЧТЕНИЕ И ЗАПИСЬ НАБОРОВ =====

def _load_collection(table, conn=None):
    conn = conn or connect()
    result = {}
    for row in conn.execute(f'SELECT id, data FROM {table}').fetchall():
        try:
            result[row['id']] = json.loads(row['data'])
        except ValueError as exc:
            raise DbError(f'Повреждена запись {table}/{row["id"]}: {exc}') from exc
    return result


def _write_collection(conn, table, data):
    """Записать набор целиком. Обновляются только изменившиеся записи."""
    if not isinstance(data, dict):
        raise DbError(f'{table}: ожидался словарь, получено {type(data).__name__}')

    columns = _columns_for(table)
    existing = {}
    for row in conn.execute(f'SELECT id, data FROM {table}').fetchall():
        existing[row['id']] = row['data']

    rows = []
    for key, record in data.items():
        if not isinstance(record, dict):
            raise DbError(f'{table}/{key}: ожидался словарь записи')
        body = json.dumps(record, ensure_ascii=False, sort_keys=True)
        if existing.get(key) == body:
            continue
        rows.append(tuple([key, *_values_for(table, record), body]))

    removed = [key for key in existing if key not in data]

    if rows:
        column_list = ', '.join(('id', *columns, 'data'))
        placeholders = ', '.join('?' * (len(columns) + 2))
        updates = ', '.join(f'{column}=excluded.{column}' for column in columns)
        conn.executemany(
            f'INSERT INTO {table} ({column_list}) VALUES ({placeholders}) '
            f'ON CONFLICT(id) DO UPDATE SET {updates}, data=excluded.data',
            rows)
    if removed:
        conn.executemany(f'DELETE FROM {table} WHERE id=?', [(key,) for key in removed])
    return len(rows), len(removed)


def load_files():
    return _load_collection('files')


def save_files(data, conn=None):
    with transaction(conn) as tx:
        return _write_collection(tx, 'files', data)


def mutate_files(mutation, conn=None):
    """Атомарно: прочитать, применить mutation(данные), записать."""
    with transaction(conn) as tx:
        data = _load_collection('files', tx)
        result = mutation(data)
        _write_collection(tx, 'files', data)
        return result


def load_folders():
    return _load_collection('folders')


def save_folders(data, conn=None):
    with transaction(conn) as tx:
        return _write_collection(tx, 'folders', data)


def mutate_folders(mutation, conn=None):
    with transaction(conn) as tx:
        data = _load_collection('folders', tx)
        result = mutation(data)
        _write_collection(tx, 'folders', data)
        return result


def load_tokens():
    conn = connect()
    result = {}
    for row in conn.execute('SELECT key_hash, data FROM tokens').fetchall():
        try:
            result[row['key_hash']] = json.loads(row['data'])
        except ValueError as exc:
            raise DbError(f'Повреждён токен {row["key_hash"]}: {exc}') from exc
    return result


def _write_tokens(conn, data):
    if not isinstance(data, dict):
        raise DbError(f'tokens: ожидался словарь, получено {type(data).__name__}')
    existing = {row['key_hash']: row['data']
                for row in conn.execute('SELECT key_hash, data FROM tokens').fetchall()}
    rows = []
    for key, record in data.items():
        if not isinstance(record, dict):
            raise DbError(f'tokens/{key}: ожидался словарь записи')
        body = json.dumps(record, ensure_ascii=False, sort_keys=True)
        if existing.get(key) == body:
            continue
        rows.append(tuple([key, *_token_values(record), body]))
    removed = [key for key in existing if key not in data]
    if rows:
        column_list = ', '.join(('key_hash', *TOKEN_COLUMNS, 'data'))
        placeholders = ', '.join('?' * (len(TOKEN_COLUMNS) + 2))
        updates = ', '.join(f'{column}=excluded.{column}' for column in TOKEN_COLUMNS)
        conn.executemany(
            f'INSERT INTO tokens ({column_list}) VALUES ({placeholders}) '
            f'ON CONFLICT(key_hash) DO UPDATE SET {updates}, data=excluded.data',
            rows)
    if removed:
        conn.executemany('DELETE FROM tokens WHERE key_hash=?', [(key,) for key in removed])
    return len(rows), len(removed)


def save_tokens(data, conn=None):
    with transaction(conn) as tx:
        return _write_tokens(tx, data)


def mutate_tokens(mutation, conn=None):
    with transaction(conn) as tx:
        data = load_tokens_from(tx)
        result = mutation(data)
        _write_tokens(tx, data)
        return result


def load_tokens_from(conn):
    result = {}
    for row in conn.execute('SELECT key_hash, data FROM tokens').fetchall():
        result[row['key_hash']] = json.loads(row['data'])
    return result


# ===== ДОКУМЕНТЫ (stats, settings) =====

def load_document(name, default=None):
    conn = connect()
    row = conn.execute('SELECT body FROM documents WHERE name=?', (name,)).fetchone()
    if row is None:
        return {} if default is None else default
    try:
        return json.loads(row['body'])
    except ValueError as exc:
        raise DbError(f'Повреждён документ {name}: {exc}') from exc


def save_document(name, body, conn=None):
    with transaction(conn) as tx:
        tx.execute(
            'INSERT INTO documents(name, body, updated_at) VALUES (?, ?, ?) '
            'ON CONFLICT(name) DO UPDATE SET body=excluded.body, updated_at=excluded.updated_at',
            (name, json.dumps(body, ensure_ascii=False, sort_keys=True), _now()))
    return body


# ===== ПЕРВИЧНАЯ ЗАПИСЬ ИЗ JSON (миграция) =====

def _read_json_file(path):
    if not os.path.exists(path):
        return None
    with open(path, 'r', encoding='utf-8') as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise DbError(f'{path}: ожидался JSON-объект')
    return data


def read_json_dataset(directory):
    """Прочитать все JSON-наборы из каталога данных."""
    dataset = {}
    for name, filename in JSON_FILES.items():
        dataset[name] = _read_json_file(os.path.join(directory, filename))
    return dataset


def _dataset_summary(dataset):
    files = dataset.get('files') or {}
    folders = dataset.get('folders') or {}
    tokens = dataset.get('tokens') or {}
    return {
        'files': len(files),
        'folders': len(folders),
        'tokens': len(tokens),
        'size': sum(_as_int(rec.get('size')) for rec in files.values() if isinstance(rec, dict)),
        'slugs': len({rec.get('slug') for rec in files.values()
                      if isinstance(rec, dict) and rec.get('slug')}),
        'paths': len({rec.get('physical_path') for rec in files.values()
                      if isinstance(rec, dict) and rec.get('physical_path')}),
    }


def migrate_from_json(directory=None, path=None, force=False):
    """Перенести JSON в SQLite одной транзакцией, затем сверить результат."""
    directory = directory or data_dir()
    path = path or db_path(directory)
    dataset = read_json_dataset(directory)

    conn = init_schema(path)
    already = {name: conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
               for name, table in (('files', 'files'), ('folders', 'folders'), ('tokens', 'tokens'))}
    if any(already.values()) and not force:
        raise DbError(
            f'База {path} уже содержит данные ({already}). Повторный перенос — только с --force.')

    with transaction(conn) as tx:
        _write_collection(tx, 'files', dataset.get('files') or {})
        _write_collection(tx, 'folders', dataset.get('folders') or {})
        _write_tokens(tx, dataset.get('tokens') or {})
        for name in ('stats', 'settings'):
            body = dataset.get(name)
            if body is not None:
                tx.execute(
                    'INSERT INTO documents(name, body, updated_at) VALUES (?, ?, ?) '
                    'ON CONFLICT(name) DO UPDATE SET body=excluded.body, updated_at=excluded.updated_at',
                    (name, json.dumps(body, ensure_ascii=False, sort_keys=True), _now()))

    report = verify_against_json(directory, path)
    report['source'] = _dataset_summary(dataset)
    report['target'] = _database_summary(path)
    report['ok'] = bool(report['source']['files'] == report['target']['files']
                        and report['source']['folders'] == report['target']['folders']
                        and report['source']['tokens'] == report['target']['tokens']
                        and report['source']['size'] == report['target']['size']
                        and not report['mismatches'])
    return report


def _database_summary(path=None):
    conn = connect(path or db_path())
    files = _load_collection('files', conn)
    folders = _load_collection('folders', conn)
    tokens = load_tokens_from(conn)
    return {
        'files': len(files),
        'folders': len(folders),
        'tokens': len(tokens),
        'size': sum(_as_int(rec.get('size')) for rec in files.values() if isinstance(rec, dict)),
        'slugs': len({rec.get('slug') for rec in files.values()
                      if isinstance(rec, dict) and rec.get('slug')}),
        'paths': len({rec.get('physical_path') for rec in files.values()
                      if isinstance(rec, dict) and rec.get('physical_path')}),
    }


def verify_against_json(directory=None, path=None, sample=25):
    """Построчная сверка записей SQLite и JSON с выборочной проверкой полей."""
    directory = directory or data_dir()
    path = path or db_path(directory)
    conn = init_schema(path)
    dataset = read_json_dataset(directory)
    mismatches = []

    for name, table in (('files', 'files'), ('folders', 'folders')):
        source = dataset.get(name) or {}
        target = _load_collection(table, conn)
        missing = set(source) - set(target)
        extra = set(target) - set(source)
        if missing:
            mismatches.append(f'{table}: нет в базе {len(missing)} записей, например {sorted(missing)[:3]}')
        if extra:
            mismatches.append(f'{table}: лишние в базе {len(extra)} записей, например {sorted(extra)[:3]}')
        changed = [key for key in list(source)[:sample]
                   if key in target and target[key] != source[key]]
        if changed:
            mismatches.append(f'{table}: различаются поля у {len(changed)} записей, например {changed[:3]}')

    source_tokens = dataset.get('tokens') or {}
    target_tokens = load_tokens_from(conn)
    if set(source_tokens) != set(target_tokens):
        mismatches.append('tokens: набор ключей не совпадает')

    return {'mismatches': mismatches}


def backup(destination, path=None):
    """Согласованная копия базы через sqlite3 backup API (без остановки сервиса)."""
    path = path or db_path()
    os.makedirs(destination, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    target = os.path.join(destination, f'albion-{stamp}.db')
    source = connect(path)
    copy = sqlite3.connect(target)
    try:
        source.backup(copy)
        copy.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    finally:
        copy.close()
    os.chmod(target, 0o600)
    return target


# ===== CLI =====

def _print_report(report):
    source, target = report.get('source'), report.get('target')
    if source and target:
        print(f"JSON:    файлов {source['files']}, папок {source['folders']}, "
              f"токенов {source['tokens']}, байт {source['size']}")
        print(f"SQLite:  файлов {target['files']}, папок {target['folders']}, "
              f"токенов {target['tokens']}, байт {target['size']}")
    if report['mismatches']:
        print('Расхождения:')
        for line in report['mismatches']:
            print('  -', line)
    print('РЕЗУЛЬТАТ:', 'OK' if report.get('ok', not report['mismatches']) else 'FAIL')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Метаданные АЛЬБИОН.ДИСК в SQLite')
    parser.add_argument('command', choices=('init', 'migrate', 'verify', 'stats', 'backup'))
    parser.add_argument('--data-dir', default=None, help='каталог данных (по умолчанию ALBION_DATA_DIR)')
    parser.add_argument('--db', default=None, help='путь к файлу базы')
    parser.add_argument('--force', action='store_true', help='перезаписать существующие данные')
    parser.add_argument('--dest', default=None, help='каталог для backup')
    args = parser.parse_args(argv)

    if args.data_dir:
        os.environ['ALBION_DATA_DIR'] = args.data_dir
    path = args.db or db_path(args.data_dir)

    try:
        if args.command == 'init':
            init_schema(path)
            print('Схема создана:', path)
        elif args.command == 'migrate':
            _print_report(migrate_from_json(args.data_dir, path, force=args.force))
        elif args.command == 'verify':
            report = verify_against_json(args.data_dir, path)
            report['source'] = _dataset_summary(read_json_dataset(args.data_dir or data_dir()))
            report['target'] = _database_summary(path)
            report['ok'] = not report['mismatches']
            _print_report(report)
        elif args.command == 'stats':
            for key, value in sorted(_database_summary(path).items()):
                print(f'{key}: {value}')
            print('файл базы:', path)
        elif args.command == 'backup':
            if not args.dest:
                raise DbError('Укажите --dest')
            print('Копия:', backup(args.dest, path))
    except (DbError, sqlite3.Error, OSError, ValueError) as exc:
        print('ОШИБКА:', exc, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
