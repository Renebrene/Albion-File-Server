# -*- coding: utf-8 -*-
"""Управление API-ключами для автоматического скачивания и интеграций.

Использование:
    python3 manage_tokens.py add "Отчёты 1С"     # download, срок 90 дней
    python3 manage_tokens.py add "Photobase" --scopes upload,download --folder <uuid>
    python3 manage_tokens.py add "Демо" --scopes upload,delete --folder <uuid> --expires-days 30
    python3 manage_tokens.py add "Сервис" --scopes download --no-expiry
    python3 manage_tokens.py list                # список ключей, прав, папок и срока
    python3 manage_tokens.py revoke "Отчёты 1С"  # отозвать по имени или уникальному префиксу hash

Права: download, upload, delete или * (все). Без --scopes создаётся ключ
только на скачивание. Для upload, delete и * обязательна привязка --folder.
Новые токены по умолчанию действуют 90 дней; --no-expiry создаёт бессрочный.
Ключи хранятся только в виде sha256-хешей в tokens.json.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

import token_store
# Тот же переключатель хранилища, что и у сервера: json | sqlite
import db as metadata_store

DATA_DIR = os.environ.get('ALBION_DATA_DIR', '.')
TOKENS_FILE = os.path.join(DATA_DIR, 'tokens.json')
FOLDERS_FILE = os.path.join(DATA_DIR, 'folders_db.json')


def load_folders():
    if metadata_store.sqlite_enabled():
        return metadata_store.load_folders()
    if not os.path.exists(FOLDERS_FILE):
        return {}
    try:
        with open(FOLDERS_FILE, 'r', encoding='utf-8') as handle:
            folders = json.load(handle)
    except (OSError, ValueError) as exc:
        raise token_store.TokenStoreError(f'Не удалось прочитать {FOLDERS_FILE}: {exc}') from exc
    return folders if isinstance(folders, dict) else {}


def parse_scopes(raw):
    if raw is None:
        return ['download']
    return [scope.strip().lower() for scope in raw.split(',') if scope.strip()]


def format_expiry(value):
    if not value:
        return 'бессрочно'
    try:
        expires = datetime.fromisoformat(str(value))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires.astimezone(timezone.utc).strftime('%d.%m.%Y %H:%M UTC')
    except (TypeError, ValueError):
        return 'некорректно'


def folder_label(folder_id, folders):
    if not folder_id:
        return '—'
    folder = folders.get(folder_id)
    if not isinstance(folder, dict):
        return f'{folder_id} (удалена)'
    return folder.get('name') or folder_id


def command_add(args):
    scopes = parse_scopes(args.scopes)
    folders = load_folders()
    raw_key, key_hash = token_store.generate_token()
    created = {}

    def add_token(tokens):
        name, normalized_scopes, folder_id, expires_at = token_store.validate_issuance(
            args.name, scopes, args.folder, folders, args.expires_days,
            no_expiry=args.no_expiry,
            existing_names=[
                meta.get('name', '') for meta in tokens.values()
                if isinstance(meta, dict)
            ],
        )
        record = token_store.build_record(name, normalized_scopes, folder_id, expires_at)
        tokens[key_hash] = record
        created['record'] = record

    token_store.mutate_tokens(TOKENS_FILE, add_token)
    record = created['record']
    print('✅ Ключ создан. Показывается ОДИН раз — сохраните его:')
    print(f'   {raw_key}')
    print(f'   Права: {", ".join(record["scopes"])}; папка: {folder_label(record["folder_id"], folders)}')
    print(f'   Срок: {format_expiry(record.get("expires_at"))}')
    print('   Использование: curl -H "X-API-Key: <ключ>" '
          'https://ваш-домен/api/v1/integration/verify')


def command_list(_args):
    tokens = token_store.load_tokens_file(TOKENS_FILE)
    if not tokens:
        print('Ключей нет. Создайте: manage_tokens.py add "Имя"')
        return
    folders = load_folders()
    print(f'{"Имя":<22} {"Права":<18} {"Папка":<24} {"Срок":<22} {"Статус":<12} Hash')
    for key_hash, meta in sorted(tokens.items(), key=lambda item: item[1].get('name', '')):
        if not isinstance(meta, dict):
            continue
        scopes = token_store.scopes_of(meta)
        expired = token_store.token_expired(meta.get('expires_at'))
        unbound_write = any(scope in token_store.FOLDER_REQUIRED_SCOPES for scope in scopes) \
            and not meta.get('folder_id')
        if unbound_write:
            status = 'нет папки'
        elif expired is None:
            status = 'срок ошибка'
        elif expired:
            status = 'истёк'
        elif not meta.get('expires_at'):
            status = 'бессрочный'
        else:
            status = 'активен'
        print(f'{meta.get("name", "?"):<22} {",".join(scopes):<18} '
              f'{folder_label(meta.get("folder_id"), folders):<24} '
              f'{format_expiry(meta.get("expires_at")):<22} {status:<12} {key_hash[:12]}…')


def command_revoke(args):
    target = args.target.strip()
    if not target:
        raise ValueError('Укажите имя или префикс хеша токена.')
    removed = {}

    def remove_token(tokens):
        exact = [key_hash for key_hash, meta in tokens.items()
                 if isinstance(meta, dict) and meta.get('name') == target]
        if len(exact) > 1:
            raise ValueError('Найдено несколько токенов с таким именем.')
        if exact:
            key_hash = exact[0]
        else:
            matches = [candidate for candidate in tokens if candidate.startswith(target)]
            if not matches:
                raise ValueError(f'Ключ «{target}» не найден.')
            if len(matches) > 1:
                raise ValueError('Префикс соответствует нескольким токенам; укажите больше символов hash.')
            key_hash = matches[0]
        removed['meta'] = tokens.pop(key_hash)

    token_store.mutate_tokens(TOKENS_FILE, remove_token)
    print(f'✅ Ключ «{target}» отозван.')


def build_parser():
    parser = argparse.ArgumentParser(description='Управление API-токенами АЛЬБИОН')
    subparsers = parser.add_subparsers(dest='command', required=True)

    add = subparsers.add_parser('add', help='создать токен')
    add.add_argument('name', help='имя интеграции')
    add.add_argument('--scopes', help='download,upload,delete или *; по умолчанию download')
    add.add_argument('--folder', help='UUID папки; обязательна для upload/delete/*')
    add.add_argument('--expires-days', type=int, default=None,
                     help='срок действия в днях (по умолчанию 90)')
    add.add_argument('--no-expiry', action='store_true', help='создать бессрочный токен')
    add.set_defaults(func=command_add)

    listing = subparsers.add_parser('list', help='показать токены')
    listing.set_defaults(func=command_list)

    revoke = subparsers.add_parser('revoke', help='отозвать токен')
    revoke.add_argument('target', help='имя или уникальный префикс hash')
    revoke.set_defaults(func=command_revoke)
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    if args.command == 'add' and args.no_expiry and args.expires_days is not None:
        parser.error('--expires-days и --no-expiry нельзя использовать вместе')
    try:
        args.func(args)
    except (ValueError, token_store.TokenStoreError) as exc:
        print(f'❌ {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
