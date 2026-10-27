# -*- coding: utf-8 -*-
"""Shared rules for machine-token issuance and authorization.

No Flask imports live here: both the web admin page and manage_tokens.py use
these helpers so scopes, expiry and folder binding cannot drift apart.
"""
import hashlib
import json
import os
import re
import secrets
import unicodedata
import urllib.parse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

try:
    import fcntl
except ImportError:  # pragma: no cover - the project targets Linux
    fcntl = None

# Хранилище метаданных: в режиме ALBION_METADATA_BACKEND=sqlite токены живут
# в таблице tokens (albion.db), иначе — в tokens.json рядом с остальными файлами.
import db as metadata_store

ALLOWED_SCOPES = {'download', 'upload', 'delete', '*'}
# Scopes that allow changing data must be bound to one concrete folder.
FOLDER_REQUIRED_SCOPES = {'upload', 'delete', '*'}
DEFAULT_TTL_DAYS = 90
MAX_TTL_DAYS = 3650


class TokenStoreError(RuntimeError):
    """tokens.json cannot be read or safely updated."""


def scopes_of(meta):
    """Old records without scopes keep their original download-only meaning."""
    scopes = meta.get('scopes') if isinstance(meta, dict) else None
    if not isinstance(scopes, list) or not scopes:
        return ['download']
    return scopes


def has_scope(meta, scope):
    scopes = scopes_of(meta)
    return '*' in scopes or scope in scopes


def token_expired(value):
    """Return True/False, or None when expires_at is malformed (fail closed)."""
    if not value:
        return False
    try:
        expires = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if expires.tzinfo is None:
        # New records are UTC. Treat legacy naïve values as UTC too instead
        # of silently depending on the server's local timezone.
        expires = expires.replace(tzinfo=timezone.utc)
    return expires < datetime.now(timezone.utc)


def normalize_scopes(raw_scopes):
    scopes = []
    for value in raw_scopes or []:
        scope = str(value).strip().lower()
        if not scope:
            continue
        if scope not in ALLOWED_SCOPES:
            raise ValueError(f'Недопустимое право «{scope}». Разрешены: download, upload, delete, *.')
        if scope not in scopes:
            scopes.append(scope)
    if not scopes:
        raise ValueError('Укажите хотя бы одно право: download, upload или delete.')
    if '*' in scopes and len(scopes) > 1:
        raise ValueError('Право * нельзя сочетать с другими правами.')
    return scopes


def expiry_from_days(days):
    """Return an aware UTC ISO timestamp. None means explicitly unlimited."""
    if days is None:
        return None
    try:
        number = int(days)
    except (TypeError, ValueError):
        raise ValueError('Срок действия должен быть целым числом дней.')
    if number < 1 or number > MAX_TTL_DAYS:
        raise ValueError(f'Срок действия должен быть от 1 до {MAX_TTL_DAYS} дней.')
    return (datetime.now(timezone.utc) + timedelta(days=number)).isoformat()


def validate_issuance(name, raw_scopes, folder_id, folders, expires_days,
                      no_expiry=False, existing_names=()):
    """Validate and normalize a token registration.

    Returns (name, scopes, folder_id, expires_at). Raises ValueError with a
    user-facing Russian message.
    """
    clean_name = str(name or '').strip()
    if not clean_name:
        raise ValueError('Имя токена не может быть пустым.')
    if len(clean_name) > 64:
        raise ValueError('Имя токена не должно превышать 64 символа.')
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in clean_name):
        raise ValueError('Имя токена содержит недопустимые управляющие символы.')
    if clean_name.casefold() in {str(value).casefold() for value in existing_names}:
        raise ValueError('Токен с таким именем уже существует.')

    scopes = normalize_scopes(raw_scopes)
    normalized_folder = (str(folder_id).strip() or None) if folder_id else None
    if any(scope in FOLDER_REQUIRED_SCOPES for scope in scopes):
        if not normalized_folder:
            raise ValueError('Для прав upload/delete/* требуется привязка к папке.')
        if normalized_folder not in folders:
            raise ValueError('Указанная папка не существует.')
    elif normalized_folder and normalized_folder not in folders:
        raise ValueError('Указанная папка не существует.')

    if no_expiry:
        expires_at = None
    else:
        expires_at = expiry_from_days(DEFAULT_TTL_DAYS if expires_days in (None, '') else expires_days)
    return clean_name, scopes, normalized_folder, expires_at


def generate_token():
    """Return (raw secret, sha256 id). The raw value is never persisted."""
    raw = 'alb_' + secrets.token_hex(24)
    return raw, hashlib.sha256(raw.encode('utf-8')).hexdigest()


# ===== ПРОСТЫЕ 8-БУКВЕННЫЕ КИРИЛЛИЧЕСКИЕ ТОКЕНЫ =====
# Для бригад используется короткий токен из 8 кириллических букв. Регистр не
# важен: «Пролонг1» и «ПРОЛОНГ1» — один и тот же токен (цифры не допускаются).
# В БД по-прежнему хранится только SHA-256, но с отдельным пространством имён,
# чтобы хеш простого токена не совпал с хешем legacy-ключа alb_....
SIMPLE_TOKEN_NAMESPACE = 'albion-simple-v1:'
SIMPLE_TOKEN_LENGTH = 8
# Для генерации исключаем редко используемые/похожие буквы; при проверке
# принимаются все буквы а-я и ё.
SIMPLE_TOKEN_ALPHABET = 'абвгдежзиклмнопрстуфхцчшщэюя'
_SIMPLE_TOKEN_RE = re.compile(r'^[а-яё]{8}$')


def normalize_simple_token(value):
    """NFC + trim + casefold. Возвращает '' для не-строк."""
    if not isinstance(value, str):
        return ''
    return unicodedata.normalize('NFC', value).strip().casefold()


def is_simple_token(value):
    return bool(_SIMPLE_TOKEN_RE.fullmatch(normalize_simple_token(value)))


def simple_token_hash(value):
    normalized = normalize_simple_token(value)
    if not _SIMPLE_TOKEN_RE.fullmatch(normalized):
        raise ValueError('Простой токен должен состоять ровно из 8 кириллических букв.')
    return hashlib.sha256((SIMPLE_TOKEN_NAMESPACE + normalized).encode('utf-8')).hexdigest()


def generate_simple_token():
    return ''.join(secrets.choice(SIMPLE_TOKEN_ALPHABET) for _ in range(SIMPLE_TOKEN_LENGTH))


def decode_simple_token(value):
    """Декодирует значение заголовка в нормализованный простой токен.

    Клиенты передают токен в percent-encoding (UTF-8), потому что HTTP-заголовки
    не допускают не-ASCII. На случай прямого UTF-8 в WSGI-строке восстанавливаем
    «кракозябры» latin-1 → UTF-8.
    """
    if not isinstance(value, str):
        return ''
    text = value.strip()

    if text and all(ord(char) <= 255 for char in text):
        try:
            repaired = text.encode('latin-1').decode('utf-8')
            if '�' not in repaired:
                text = repaired
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass

    if '%' in text:
        try:
            text = urllib.parse.unquote(text, errors='strict')
        except (UnicodeDecodeError, ValueError):
            pass

    return normalize_simple_token(text)


def api_key_hash_candidates(provided):
    """Все варианты хеша, по которым ищем ключ: legacy и простой токен."""
    if not isinstance(provided, str) or not provided:
        return []

    candidates = []

    def add(value):
        if not value:
            return
        digest = hashlib.sha256(value.encode('utf-8')).hexdigest()
        if digest not in candidates:
            candidates.append(digest)

    add(provided)

    decoded = decode_simple_token(provided)
    if decoded:
        add(decoded)
        if is_simple_token(decoded):
            digest = hashlib.sha256((SIMPLE_TOKEN_NAMESPACE + decoded).encode('utf-8')).hexdigest()
            if digest not in candidates:
                candidates.append(digest)

    return candidates


def build_record(name, scopes, folder_id, expires_at, now=None, kind=None):
    now = now or datetime.now(timezone.utc)
    record = {
        'name': name,
        'created': now.astimezone().strftime('%d.%m.%Y %H:%M'),
        'created_at': now.isoformat(),
        'scopes': scopes,
        'folder_id': folder_id or None,
        'expires_at': expires_at,
    }
    if kind:
        record['kind'] = kind
    return record


def load_tokens_file(path):
    if metadata_store.sqlite_enabled():
        return metadata_store.load_tokens()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise TokenStoreError(f'Не удалось прочитать {path}: {exc}') from exc
    if not isinstance(data, dict):
        raise TokenStoreError(f'{path} должен содержать JSON-объект.')
    return data


def save_tokens_file(path, data):
    if metadata_store.sqlite_enabled():
        metadata_store.save_tokens(data)
        return
    directory = os.path.dirname(path) or '.'
    os.makedirs(directory, exist_ok=True)
    tmp = f'{path}.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


@contextmanager
def locked_tokens_file(path):
    """Cross-process lock for read-modify-write of tokens.json."""
    directory = os.path.dirname(path) or '.'
    os.makedirs(directory, exist_ok=True)
    lock_path = path + '.lock'
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(lock_fd)


def mutate_tokens(path, mutation):
    """Reload under an exclusive lock, apply mutation(tokens), then save."""
    if metadata_store.sqlite_enabled():
        # В SQLite блокировку держит транзакция BEGIN IMMEDIATE (в том числе
        # между процессами: сервер и CLI), поэтому flock не нужен.
        return metadata_store.mutate_tokens(mutation)
    with locked_tokens_file(path):
        tokens = load_tokens_file(path)
        result = mutation(tokens)
        save_tokens_file(path, tokens)
        return result
