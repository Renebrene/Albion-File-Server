import json
import os
from werkzeug.security import generate_password_hash

AUTH_FILE = 'auth.json'

print("=== Смена учетных данных АЛЬБИОН ===")
username = input("Введите новый логин: ").strip()
password = input("Введите новый пароль: ").strip()

if not username or not password:
    print("❌ Ошибка: Логин и пароль не могут быть пустыми!")
else:
    with open(AUTH_FILE, 'w', encoding='utf-8') as f:
        json.dump({
            "username": username, 
            "password_hash": generate_password_hash(password)
        }, f, indent=2)
    print(f"✅ Успешно! Теперь для входа используйте:\n   Логин: {username}\n   Пароль: {password}")
