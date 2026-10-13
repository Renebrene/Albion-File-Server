# -*- coding: utf-8 -*-
"""Управление API-ключами для автоматического скачивания.

Использование:
    python3 manage_tokens.py add "Отчёты 1С"     # создать ключ (показывается ОДИН раз)
    python3 manage_tokens.py list                # список ключей
    python3 manage_tokens.py revoke "Отчёты 1С"  # отозвать по имени или префиксу хеша

Ключи хранятся только в виде sha256-хешей в tokens.json.
"""
import hashlib
import json
import os
import secrets
import sys
from datetime import datetime

TOKENS_FILE = os.path.join(os.environ.get('ALBION_DATA_DIR', '.'), 'tokens.json')


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


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    tokens = load_tokens()

    if cmd == 'add':
        if len(sys.argv) < 3:
            print('Укажите имя: manage_tokens.py add "Имя интеграции"')
            sys.exit(1)
        name = sys.argv[2].strip()
        if any(meta.get('name') == name for meta in tokens.values()):
            print(f'❌ Ключ с именем "{name}" уже существует. Сначала отзовите его.')
            sys.exit(1)
        key = 'alb_' + secrets.token_hex(24)
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        tokens[key_hash] = {
            'name': name,
            'created': datetime.now().strftime('%d.%m.%Y %H:%M'),
        }
        save_tokens(tokens)
        print('✅ Ключ создан. Показывается ОДИН раз — сохраните его:')
        print(f'   {key}')
        print('   Использование: curl -H "X-API-Key: <ключ>" -O https://ваш-домен/<слаг-файла>')

    elif cmd == 'list':
        if not tokens:
            print('Ключей нет. Создайте: manage_tokens.py add "Имя"')
            return
        print(f'{"Имя":<25} {"Создан":<17} Хеш (префикс)')
        for key_hash, meta in sorted(tokens.items(), key=lambda x: x[1].get('name', '')):
            print(f'{meta.get("name", "?"):<25} {meta.get("created", "?"):<17} {key_hash[:12]}…')

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
