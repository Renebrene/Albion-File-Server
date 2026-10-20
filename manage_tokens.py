# -*- coding: utf-8 -*-
"""Управление API-ключами для автоматического скачивания и интеграций.

Использование:
    python3 manage_tokens.py add "Отчёты 1С"     # создать ключ (показывается ОДИН раз)
    python3 manage_tokens.py add "Photobase" --scopes upload,download --folder <uuid>
    python3 manage_tokens.py list                # список ключей, прав и папок
    python3 manage_tokens.py revoke "Отчёты 1С"  # отозвать по имени или префиксу хеша

Права: download, upload, delete или * (все). Без --scopes создаётся ключ
только на скачивание (прежнее поведение). Для права upload обязательна
привязка --folder. Папка — UUID из folders_db.json.

Ключи хранятся только в виде sha256-хешей в tokens.json.
"""
import hashlib
import json
import os
import secrets
import sys
from datetime import datetime

TOKENS_FILE = os.path.join(os.environ.get('ALBION_DATA_DIR', '.'), 'tokens.json')

ALLOWED_SCOPES = {'download', 'upload', 'delete', '*'}


def load_tokens():
    if os.path.exists(TOKENS_FILE):
        with open(TOKENS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_tokens(tokens):
    os.makedirs(os.path.dirname(TOKENS_FILE) or '.', exist_ok=True)
    tmp = TOKENS_FILE + '.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(tokens, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, TOKENS_FILE)


def token_scopes(meta):
    """Старые записи без scopes работают только на скачивание."""
    scopes = meta.get('scopes')
    if not isinstance(scopes, list) or not scopes:
        return ['download']
    return scopes


def parse_add_args(args):
    """Разбирает: <имя> [--scopes a,b] [--folder uuid]."""
    if not args:
        print('Укажите имя: manage_tokens.py add "Имя интеграции"')
        sys.exit(1)
    name = args[0].strip()
    if not name:
        print('Имя не может быть пустым.')
        sys.exit(1)

    scopes_arg = None
    folder = None
    i = 1
    while i < len(args):
        option = args[i]
        if option in ('--scopes', '--folder'):
            if i + 1 >= len(args):
                print(f'Для {option} требуется значение.')
                sys.exit(1)
            value = args[i + 1].strip()
            if option == '--scopes':
                scopes_arg = value
            else:
                folder = value or None
            i += 2
        else:
            print(f'Неизвестный аргумент: {option}')
            sys.exit(1)

    if scopes_arg is None:
        scopes = ['download']
    else:
        scopes = []
        for scope in scopes_arg.split(','):
            scope = scope.strip().lower()
            if not scope:
                continue
            if scope not in ALLOWED_SCOPES:
                print(f'❌ Недопустимое право "{scope}". Разрешены: '
                      f'{", ".join(sorted(ALLOWED_SCOPES))}')
                sys.exit(1)
            if scope not in scopes:
                scopes.append(scope)
        if not scopes:
            print('❌ Список прав пуст. Укажите download, upload, delete или *.')
            sys.exit(1)

    if ('upload' in scopes or '*' in scopes) and not folder:
        print('❌ Для права upload (включая *) обязателен --folder <uuid>.')
        sys.exit(1)

    return name, scopes, folder


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    tokens = load_tokens()

    if cmd == 'add':
        name, scopes, folder = parse_add_args(sys.argv[2:])
        if any(meta.get('name') == name for meta in tokens.values()):
            print(f'❌ Ключ с именем "{name}" уже существует. Сначала отзовите его.')
            sys.exit(1)
        key = 'alb_' + secrets.token_hex(24)
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        tokens[key_hash] = {
            'name': name,
            'created': datetime.now().strftime('%d.%m.%Y %H:%M'),
            'scopes': scopes,
            'folder_id': folder,
        }
        save_tokens(tokens)
        print('✅ Ключ создан. Показывается ОДИН раз — сохраните его:')
        print(f'   {key}')
        print(f'   Права: {", ".join(scopes)}; папка: {folder or "—"}')
        print('   Использование: curl -H "X-API-Key: <ключ>" '
              'https://ваш-домен/api/v1/integration/verify')

    elif cmd == 'list':
        if not tokens:
            print('Ключей нет. Создайте: manage_tokens.py add "Имя"')
            return
        print(f'{"Имя":<25} {"Создан":<17} {"Права":<20} {"Папка":<38} Хеш (префикс)')
        for key_hash, meta in sorted(tokens.items(), key=lambda x: x[1].get('name', '')):
            scopes = ','.join(token_scopes(meta))
            folder = meta.get('folder_id') or '—'
            print(f'{meta.get("name", "?"):<25} {meta.get("created", "?"):<17} '
                  f'{scopes:<20} {folder:<38} {key_hash[:12]}…')

    elif cmd == 'revoke':
        if len(sys.argv) < 3:
            print('Укажите имя или префикс хеша: manage_tokens.py revoke "Имя"')
            sys.exit(1)
        target = sys.argv[2].strip()
        found = None
        for key_hash, meta in list(tokens.items()):
            if meta.get('name') == target or key_hash.startswith(target):
                found = key_hash
                break
        if not found:
            print(f'❌ Ключ "{target}" не найден.')
            sys.exit(1)
        del tokens[found]
        save_tokens(tokens)
        print(f'✅ Ключ "{target}" отозван.')

    else:
        print(__doc__)
        sys.exit(1)


if __name__ == '__main__':
    main()
