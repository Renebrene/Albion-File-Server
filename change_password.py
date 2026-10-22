import getpass
import json
import os
import tempfile
from werkzeug.security import generate_password_hash

DATA_DIR = os.environ.get('ALBION_DATA_DIR', '.')
AUTH_FILE = os.path.join(DATA_DIR, 'auth.json')

if os.environ.get('ALBION_METADATA_BACKEND', 'json').strip().lower() == 'sqlite':
    print("Внимание: включён режим SQLite — учётные данные здесь не используются.")
    print("Создание и смена паролей: python manage_users.py (см. manage_users.py --help).")
    raise SystemExit(1)

print("=== Смена учетных данных АЛЬБИОН ===")
username = input("Введите новый логин: ").strip()
password = getpass.getpass("Введите новый пароль: ")

if not username or not password:
    print("❌ Ошибка: Логин и пароль не могут быть пустыми!")
else:
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.auth-', dir=DATA_DIR, text=True)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump({
                "username": username,
                "password_hash": generate_password_hash(password)
            }, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, AUTH_FILE)
        print(f"✅ Учётные данные для «{username}» сохранены в {AUTH_FILE}.")
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
