# -*- coding: utf-8 -*-
"""Учётные записи АЛЬБИОН.ДИСК: пароли, роли, блокировки, импорт CSV.

Модуль не зависит от Flask: и веб-интерфейс, и CLI `manage_users.py` используют
одни и те же правила, чтобы политика пароля и роли не расходились.

Пароли хранятся в виде argon2id. Записи из прежнего `auth.json` содержат
werkzeug-scrypt хеш — такие пароли проверяются по-старому и молча переносятся
в argon2id при первом успешном входе.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from werkzeug.security import check_password_hash

import db as metadata_store

try:
    from argon2 import PasswordHasher
    from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
except ImportError:  # pragma: no cover - зависимость обязательна, но импорт не должен ронять сервер
    PasswordHasher = None
    InvalidHashError = VerificationError = VerifyMismatchError = Exception

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 200
MAX_FAILED_LOGINS = 10
LOCK_MINUTES = 15
ROLES = ('admin', 'user')
ROLE_TITLES = {'admin': 'Администратор', 'user': 'Пользователь'}
# Срок жизни сессии по роли: у администратора короче — у него больше прав.
SESSION_TTL = {'admin': timedelta(hours=12), 'user': timedelta(days=30)}
# Логины: латиница и кириллица, цифры и . _ @ + - — привычные имена вроде
# «админ» продолжают работать, а не только латинские.
LOGIN_RE = re.compile(r'^[A-Za-z0-9А-Яа-яЁё._@+-]{3,64}$')

_hasher = PasswordHasher() if PasswordHasher else None


class UserError(ValueError):
    """Ошибка с понятным текстом для формы или CLI."""


def _now():
    return datetime.now(timezone.utc)


def _iso(moment):
    return moment.astimezone(timezone.utc).isoformat()


def parse_moment(value):
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


# ===== ПАРОЛИ =====

def hash_password(password):
    if _hasher is None:
        raise UserError('Не установлен argon2-cffi: pip install -r requirements.txt')
    return _hasher.hash(password)


def verify_password(stored_hash, password):
    """Проверить пароль. Возвращает (подходит, нужно_перехешировать)."""
    if not stored_hash or not password:
        return False, False
    if stored_hash.startswith('$argon2'):
        if _hasher is None:
            return False, False
        try:
            _hasher.verify(stored_hash, password)
            return True, _hasher.check_needs_rehash(stored_hash)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False, False
    # Прежний формат auth.json (werkzeug scrypt/pbkdf2)
    try:
        if check_password_hash(stored_hash, password):
            return True, True
    except (ValueError, TypeError):
        return False, False
    return False, False


def password_error(password):
    """None — пароль допустим, иначе текст ошибки."""
    if not password:
        return 'Пароль не может быть пустым'
    if len(password) < MIN_PASSWORD_LENGTH:
        return f'Пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов'
    if len(password) > MAX_PASSWORD_LENGTH:
        return f'Пароль длиннее {MAX_PASSWORD_LENGTH} символов'
    if password.strip() != password:
        return 'Пароль не должен начинаться или заканчиваться пробелом'
    return None


def login_error(login):
    if not login or not LOGIN_RE.match(login):
        return 'Логин: 3–64 символа — буквы, цифры и . _ @ + -'
    return None


def generate_password(length=16):
    """Временный пароль для новой учётки: без похожих символов."""
    alphabet = 'abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    return '-'.join(''.join(secrets.choice(alphabet) for _ in range(5)) for _ in range(3))


# ===== ЗАПИСИ ПОЛЬЗОВАТЕЛЕЙ =====

def _row_to_user(row):
    return {
        'id': row['id'],
        'login': row['login'],
        'password_hash': row['password_hash'],
        'role': row['role'],
        'display_name': row['display_name'] or row['login'],
        'email': row['email'],
        'active': bool(row['active']),
        'must_change_password': bool(row['must_change_password']),
        'allow_remote': bool(row['allow_remote']),
        'created_at': row['created_at'],
        'last_login_at': row['last_login_at'],
        'failed_logins': row['failed_logins'] or 0,
        'locked_until': row['locked_until'],
    }


def get_user(login):
    if not login:
        return None
    conn = metadata_store.connect()
    row = conn.execute('SELECT * FROM users WHERE login=?', (login,)).fetchone()
    return _row_to_user(row) if row else None


def get_user_by_id(user_id):
    conn = metadata_store.connect()
    row = conn.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
    return _row_to_user(row) if row else None


def list_users():
    conn = metadata_store.connect()
    rows = conn.execute('SELECT * FROM users ORDER BY role, login').fetchall()
    return [_row_to_user(row) for row in rows]


def count_active_admins(exclude_id=None):
    conn = metadata_store.connect()
    if exclude_id:
        row = conn.execute(
            "SELECT COUNT(*) FROM users WHERE role='admin' AND active=1 AND id<>?",
            (exclude_id,)).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) FROM users WHERE role='admin' AND active=1").fetchone()
    return row[0]


def create_user(login, password, role='user', display_name=None, email=None,
                allow_remote=False, must_change_password=False, password_hash=None):
    """Создать учётку. password_hash задаётся только при переносе из auth.json.

    При переносе логин не проверяется по новым правилам: в старом auth.json он
    может быть кириллическим, и терять из-за этого доступ нельзя — администратор
    сменит логин в разделе пользователей.
    """
    if password_hash is None:
        problem = login_error(login)
        if problem:
            raise UserError(problem)
    elif not login or len(login) > 64:
        raise UserError('Некорректный логин в auth.json')
    if role not in ROLES:
        raise UserError(f'Неизвестная роль: {role}')
    if password_hash is None:
        problem = password_error(password)
        if problem:
            raise UserError(problem)
        password_hash = hash_password(password)

    conn = metadata_store.connect()
    if conn.execute('SELECT 1 FROM users WHERE login=?', (login,)).fetchone():
        raise UserError(f'Логин «{login}» уже занят')

    user_id = str(uuid.uuid4())
    conn.execute(
        '''INSERT INTO users(id, login, password_hash, role, display_name, email, active,
                             must_change_password, allow_remote, created_at, failed_logins)
           VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 0)''',
        (user_id, login, password_hash, role, display_name or login, email,
         1 if must_change_password else 0, 1 if allow_remote else 0, _iso(_now())))
    return get_user_by_id(user_id)


def update_user(user_id, *, role=None, display_name=None, email=None,
                active=None, allow_remote=None, must_change_password=None):
    user = get_user_by_id(user_id)
    if not user:
        raise UserError('Учётная запись не найдена')
    if role is not None and role not in ROLES:
        raise UserError(f'Неизвестная роль: {role}')
    if user['role'] == 'admin' and (role == 'user' or active is False):
        if count_active_admins(exclude_id=user_id) == 0:
            raise UserError('Это последний активный администратор: сначала назначьте другого')

    fields, values = [], []
    for column, value in (('role', role), ('display_name', display_name), ('email', email)):
        if value is not None:
            fields.append(f'{column}=?')
            values.append(value)
    for column, value in (('active', active), ('allow_remote', allow_remote),
                          ('must_change_password', must_change_password)):
        if value is not None:
            fields.append(f'{column}=?')
            values.append(1 if value else 0)
    if not fields:
        return user
    values.append(user_id)
    metadata_store.connect().execute(
        f'UPDATE users SET {", ".join(fields)} WHERE id=?', values)
    return get_user_by_id(user_id)


def set_password(user_id, password, must_change=False):
    problem = password_error(password)
    if problem:
        raise UserError(problem)
    metadata_store.connect().execute(
        'UPDATE users SET password_hash=?, must_change_password=?, failed_logins=0, locked_until=NULL '
        'WHERE id=?',
        (hash_password(password), 1 if must_change else 0, user_id))
    return get_user_by_id(user_id)


def delete_user(user_id):
    user = get_user_by_id(user_id)
    if not user:
        raise UserError('Учётная запись не найдена')
    if user['role'] == 'admin' and count_active_admins(exclude_id=user_id) == 0:
        raise UserError('Это последний активный администратор: удаление запрещено')
    metadata_store.connect().execute('DELETE FROM users WHERE id=?', (user_id,))


# ===== БЛОКИРОВКА ПОСЛЕ НЕУДАЧНЫХ ВХОДОВ =====

def is_locked(user):
    locked_until = parse_moment(user.get('locked_until'))
    return bool(locked_until and locked_until > _now())


def lock_remaining_minutes(user):
    locked_until = parse_moment(user.get('locked_until'))
    if not locked_until:
        return 0
    delta = locked_until - _now()
    return max(0, int(delta.total_seconds() // 60) + 1) if delta.total_seconds() > 0 else 0


def register_failure(user_id):
    """Увеличить счётчик неудач; на пороге — заблокировать учётку."""
    conn = metadata_store.connect()
    row = conn.execute('SELECT failed_logins FROM users WHERE id=?', (user_id,)).fetchone()
    if not row:
        return None
    failed = (row['failed_logins'] or 0) + 1
    if failed >= MAX_FAILED_LOGINS:
        until = _iso(_now() + timedelta(minutes=LOCK_MINUTES))
        conn.execute('UPDATE users SET failed_logins=?, locked_until=? WHERE id=?',
                     (failed, until, user_id))
        return until
    conn.execute('UPDATE users SET failed_logins=? WHERE id=?', (failed, user_id))
    return None


def register_success(user_id, rehash=None):
    fields = 'failed_logins=0, locked_until=NULL, last_login_at=?'
    values = [_iso(_now())]
    if rehash:
        fields += ', password_hash=?'
        values.append(rehash)
    values.append(user_id)
    metadata_store.connect().execute(f'UPDATE users SET {fields} WHERE id=?', values)


# ===== ПЕРЕНОС ИЗ auth.json =====

def migrate_from_auth_json(path, create_if_missing=False):
    """Перенести единственную учётку из auth.json в таблицу users.

    Возвращает (созданная учётка или None, сообщение). Пароль остаётся прежним:
    хеш переносится как есть и станет argon2id при первой удачной смене пароля.
    """
    if list_users():
        return None, 'учётные записи уже есть — перенос не требуется'
    if not os.path.exists(path):
        return None, 'auth.json не найден'
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        return None, f'auth.json не прочитан: {exc}'
    login = (data.get('username') or '').strip()
    stored_hash = data.get('password_hash') or ''
    if not login or not stored_hash:
        return None, 'в auth.json нет имени пользователя или хеша пароля'
    if login_error(login):
        # Логин из старого файла может не проходить новую проверку (например, кириллица):
        # переносим как есть, чтобы не потерять доступ, но сообщаем об этом.
        note = f'логин «{login}» не соответствует новым правилам — смените его в разделе пользователей'
    else:
        note = 'пароль перенесён как есть, хеш станет argon2id при следующей смене'
    user = create_user(login, None, role='admin', password_hash=stored_hash)
    return user, note


# ===== ИМПОРТ CSV =====

CSV_COLUMNS = ('login', 'display_name', 'role', 'allow_remote', 'password')
_TRUE_VALUES = {'1', 'да', 'yes', 'true', 'истина', '+'}


def _parse_bool(value, default=False):
    if value is None or value == '':
        return default
    return str(value).strip().lower() in _TRUE_VALUES


def import_csv(text, default_role='user'):
    """Разобрать CSV со учётками.

    Обязательна только колонка login. Пароль можно не указывать — тогда
    сгенерируется временный и попадёт в отчёт. Возвращает (созданные, ошибки).
    """
    if not text or not text.strip():
        raise UserError('Файл пуст')
    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=',;\t')
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise UserError('Не найдена строка заголовков (нужна колонка login)')
    headers = {(name or '').strip().lower() for name in reader.fieldnames}
    if 'login' not in headers:
        raise UserError('Нужна колонка login; допустимы display_name, role, allow_remote, password')

    created, errors = [], []
    for number, row in enumerate(reader, start=2):
        row = {(key or '').strip().lower(): (value or '').strip()
               for key, value in row.items() if key}
        if not any(row.values()):
            continue
        login = row.get('login', '')
        password = row.get('password') or generate_password()
        generated = not row.get('password')
        try:
            user = create_user(
                login, password,
                role=(row.get('role') or default_role).lower(),
                display_name=row.get('display_name') or None,
                allow_remote=_parse_bool(row.get('allow_remote')),
                must_change_password=generated,
            )
        except UserError as exc:
            errors.append(f'строка {number}: {exc}')
            continue
        created.append({'login': user['login'], 'role': user['role'],
                        'password': password if generated else None})
    return created, errors
