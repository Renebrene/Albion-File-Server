# -*- coding: utf-8 -*-
"""Серверные сессии АЛЬБИОН.ДИСК.

В cookie уходит случайный токен, в базе лежит только его SHA-256: дамп базы не
даёт возможности войти. Запись сессии позволяет отозвать доступ немедленно —
при выходе, смене пароля, деактивации учётки или вручную из раздела пользователей.

Срок жизни зависит от роли (администратор — короче). Срок продлевается по
активности, но не чаще, чем раз в минуту: иначе каждая картинка страницы
превращалась бы в запись в базу.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import db as metadata_store
import users as user_store

TOUCH_INTERVAL = timedelta(seconds=60)


def _now():
    return datetime.now(timezone.utc)


def _iso(moment):
    return moment.astimezone(timezone.utc).isoformat()


def _hash(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def ttl_for(role):
    return user_store.SESSION_TTL.get(role, user_store.SESSION_TTL['user'])


def create(user_id, role=None, ip=None, user_agent=None, ttl=None):
    """Создать сессию. Возвращает токен — он показывается клиенту один раз."""
    if role is None:
        user = user_store.get_user_by_id(user_id)
        role = user['role'] if user else 'user'
    token = secrets.token_urlsafe(32)
    now = _now()
    metadata_store.connect().execute(
        '''INSERT INTO sessions(id, user_id, token_hash, created_at, last_seen, expires_at, ip, user_agent)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
        (str(uuid.uuid4()), user_id, _hash(token), _iso(now), _iso(now),
         _iso(now + (ttl or ttl_for(role))), ip, (user_agent or '')[:255]))
    return token


def resolve(token, touch=True):
    """Найти живую сессию по токену. Просроченные и отозванные не возвращаются."""
    if not token:
        return None
    conn = metadata_store.connect()
    row = conn.execute('SELECT * FROM sessions WHERE token_hash=?', (_hash(token),)).fetchone()
    if row is None or row['revoked_at']:
        return None
    now = _now()
    expires = user_store.parse_moment(row['expires_at'])
    if expires and expires <= now:
        return None

    record = {'id': row['id'], 'user_id': row['user_id'], 'expires_at': row['expires_at'],
              'created_at': row['created_at'], 'ip': row['ip'], 'user_agent': row['user_agent']}
    if touch:
        last_seen = user_store.parse_moment(row['last_seen'])
        if last_seen is None or now - last_seen >= TOUCH_INTERVAL:
            user = user_store.get_user_by_id(row['user_id'])
            role = user['role'] if user else 'user'
            new_expiry = _iso(now + ttl_for(role))
            conn.execute('UPDATE sessions SET last_seen=?, expires_at=? WHERE id=?',
                         (_iso(now), new_expiry, row['id']))
            record['expires_at'] = new_expiry
    return record


def revoke(token):
    if not token:
        return False
    cursor = metadata_store.connect().execute(
        'UPDATE sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL',
        (_iso(_now()), _hash(token)))
    return cursor.rowcount > 0


def revoke_by_id(session_id):
    cursor = metadata_store.connect().execute(
        'UPDATE sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL',
        (_iso(_now()), session_id))
    return cursor.rowcount > 0


def revoke_all_for_user(user_id, keep_session_id=None):
    conn = metadata_store.connect()
    if keep_session_id:
        cursor = conn.execute(
            'UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL AND id<>?',
            (_iso(_now()), user_id, keep_session_id))
    else:
        cursor = conn.execute(
            'UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL',
            (_iso(_now()), user_id))
    return cursor.rowcount


def list_active(user_id=None):
    conn = metadata_store.connect()
    query = ('SELECT * FROM sessions WHERE revoked_at IS NULL AND expires_at > ?')
    params = [_iso(_now())]
    if user_id:
        query += ' AND user_id=?'
        params.append(user_id)
    query += ' ORDER BY last_seen DESC'
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def purge_expired(days=7):
    """Удалить старые записи: отозванные и просроченные более days назад."""
    limit = _iso(_now() - timedelta(days=days))
    cursor = metadata_store.connect().execute(
        'DELETE FROM sessions WHERE (revoked_at IS NOT NULL AND revoked_at < ?) '
        'OR (expires_at < ?)', (limit, limit))
    return cursor.rowcount
