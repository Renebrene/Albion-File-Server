#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Управление учётными записями АЛЬБИОН.ДИСК из командной строки.

Требуется режим метаданных SQLite (ALBION_METADATA_BACKEND=sqlite): учётные
записи и сессии живут в таблице users базы albion.db.

Примеры:
    python manage_users.py add-admin админ            # создать администратора
    python manage_users.py add ivan --name "Иван Петров"
    python manage_users.py list
    python manage_users.py passwd ivan                # сбросить пароль
    python manage_users.py role ivan admin
    python manage_users.py deactivate ivan
    python manage_users.py sessions ivan --revoke
    python manage_users.py import users.csv
    python manage_users.py migrate                    # перенести админа из auth.json
"""
from __future__ import annotations

import argparse
import os
import sys

import db as metadata_store
import sessions as session_store
import users as user_store

DEFAULT_CSV_COLUMNS = 'login,display_name,role,allow_remote,password'


def _require_sqlite():
    if not metadata_store.sqlite_enabled():
        print('ОШИБКА: учётные записи доступны в режиме SQLite. '
              'Установите ALBION_METADATA_BACKEND=sqlite и запустите db.py migrate.',
              file=sys.stderr)
        return False
    return True


def _print_secret(login, password, generated):
    if generated:
        print('   Временный пароль:', password)
        print('   (показывается один раз; пользователь сменит его при входе)')
    else:
        print('   Пароль задан вручную.')


def _fmt(value):
    return value if value else '—'


def cmd_add(args, role):
    password = args.password or user_store.generate_password()
    try:
        user = user_store.create_user(
            args.login, password, role=role, display_name=args.name,
            email=args.email, allow_remote=args.allow_remote,
            must_change_password=not args.password)
    except user_store.UserError as exc:
        print('ОШИБКА:', exc, file=sys.stderr)
        return 1
    print(f'Создана учётная запись: {user["login"]} ({user_store.ROLE_TITLES[user["role"]]})')
    _print_secret(user['login'], password, not args.password)
    return 0


def cmd_list(args):
    users = user_store.list_users()
    if not users:
        print('Учётных записей нет. Создайте администратора: manage_users.py add-admin ЛОГИН')
        return 0
    sessions = {}
    for record in session_store.list_active():
        sessions[record['user_id']] = sessions.get(record['user_id'], 0) + 1
    print(f'{"ЛОГИН":<20}{"РОЛЬ":<16}{"СОСТОЯНИЕ":<22}{"СНАРУЖИ":<9}{"СЕССИИ":<7}ПОСЛЕДНИЙ ВХОД')
    for user in users:
        state = 'активна' if user['active'] else 'отключена'
        if user_store.is_locked(user):
            state = f'заблокирована {user_store.lock_remaining_minutes(user)}м'
        elif user['must_change_password']:
            state = 'сменит пароль'
        print(f'{user["login"]:<20}{user_store.ROLE_TITLES[user["role"]]:<16}{state:<22}'
              f'{"да" if user["allow_remote"] else "нет":<9}'
              f'{sessions.get(user["id"], 0):<7}{_fmt(user["last_login_at"])}')
    return 0


def cmd_passwd(args):
    user = user_store.get_user(args.login)
    if not user:
        print('ОШИБКА: учётная запись не найдена', file=sys.stderr)
        return 1
    password = args.password or user_store.generate_password()
    try:
        user_store.set_password(user['id'], password, must_change=not args.no_must_change)
    except user_store.UserError as exc:
        print('ОШИБКА:', exc, file=sys.stderr)
        return 1
    revoked = session_store.revoke_all_for_user(user['id'])
    print(f'Пароль изменён: {user["login"]} (завершено сессий: {revoked})')
    _print_secret(user['login'], password, not args.password)
    return 0


def cmd_role(args):
    user = user_store.get_user(args.login)
    if not user:
        print('ОШИБКА: учётная запись не найдена', file=sys.stderr)
        return 1
    try:
        user_store.update_user(user['id'], role=args.role)
    except user_store.UserError as exc:
        print('ОШИБКА:', exc, file=sys.stderr)
        return 1
    revoked = session_store.revoke_all_for_user(user['id']) if args.role != user['role'] else 0
    print(f'Роль «{user["login"]}»: {user_store.ROLE_TITLES[args.role]} '
          f'(завершено сессий: {revoked})')
    return 0


def cmd_state(args, active):
    user = user_store.get_user(args.login)
    if not user:
        print('ОШИБКА: учётная запись не найдена', file=sys.stderr)
        return 1
    try:
        user_store.update_user(user['id'], active=active)
    except user_store.UserError as exc:
        print('ОШИБКА:', exc, file=sys.stderr)
        return 1
    if not active:
        print(f'Завершено сессий: {session_store.revoke_all_for_user(user["id"])}')
    print(f'Учётная запись «{user["login"]}»: {"включена" if active else "отключена"}')
    return 0


def cmd_remote(args, allow):
    user = user_store.get_user(args.login)
    if not user:
        print('ОШИБКА: учётная запись не найдена', file=sys.stderr)
        return 1
    user_store.update_user(user['id'], allow_remote=allow)
    if not allow:
        session_store.revoke_all_for_user(user['id'])
    print(f'Вход из интернета для «{user["login"]}»: {"разрешён" if allow else "запрещён"}')
    return 0


def cmd_delete(args):
    user = user_store.get_user(args.login)
    if not user:
        print('ОШИБКА: учётная запись не найдена', file=sys.stderr)
        return 1
    if not args.yes:
        answer = input(f'Удалить учётную запись «{user["login"]}» и её сессии? [y/N] ').strip().lower()
        if answer not in {'y', 'yes', 'д', 'да'}:
            print('Отменено.')
            return 0
    try:
        user_store.delete_user(user['id'])
    except user_store.UserError as exc:
        print('ОШИБКА:', exc, file=sys.stderr)
        return 1
    session_store.revoke_all_for_user(user['id'])
    print(f'Учётная запись «{user["login"]}» удалена.')
    return 0


def cmd_sessions(args):
    user = user_store.get_user(args.login) if args.login else None
    if args.login and not user:
        print('ОШИБКА: учётная запись не найдена', file=sys.stderr)
        return 1
    if args.revoke:
        if user:
            count = session_store.revoke_all_for_user(user['id'])
        else:
            count = sum(session_store.revoke_all_for_user(item['id'])
                        for item in user_store.list_users())
        print(f'Завершено сессий: {count}')
        return 0
    records = session_store.list_active(user['id'] if user else None)
    if not records:
        print('Активных сессий нет.')
        return 0
    names = {item['id']: item['login'] for item in user_store.list_users()}
    print(f'{"ЛОГИН":<20}{"СОЗДАНА":<28}{"ПОСЛЕДНЯЯ АКТИВНОСТЬ":<28}IP')
    for record in records:
        print(f'{names.get(record["user_id"], "?"):<20}{_fmt(record["created_at"]):<28}'
              f'{_fmt(record["last_seen"]):<28}{_fmt(record["ip"])}')
    return 0


def cmd_import(args):
    try:
        with open(args.file, 'r', encoding='utf-8-sig') as handle:
            text = handle.read()
    except OSError as exc:
        print('ОШИБКА: файл не прочитан:', exc, file=sys.stderr)
        return 1
    try:
        created, errors = user_store.import_csv(text, default_role=args.role)
    except user_store.UserError as exc:
        print('ОШИБКА:', exc, file=sys.stderr)
        print(f'Ожидаемые колонки: {DEFAULT_CSV_COLUMNS}', file=sys.stderr)
        return 1
    print(f'Создано учётных записей: {len(created)}')
    for row in created:
        line = f'   {row["login"]} ({user_store.ROLE_TITLES[row["role"]]})'
        if row['password']:
            line += f' — временный пароль: {row["password"]}'
        print(line)
    if errors:
        print(f'Отклонено строк: {len(errors)}')
        for line in errors:
            print('   ', line)
    return 0 if created or not errors else 1


def cmd_migrate(args):
    auth_file = os.path.join(metadata_store.data_dir(), 'auth.json')
    user, note = user_store.migrate_from_auth_json(auth_file)
    if user:
        print(f'Перенесена учётная запись: {user["login"]} ({user_store.ROLE_TITLES[user["role"]]})')
        print('  ', note)
    else:
        print('Перенос не выполнен:', note)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description='Учётные записи АЛЬБИОН.ДИСК')
    sub = parser.add_subparsers(dest='command', required=True)

    add_admin = sub.add_parser('add-admin', help='создать администратора')
    add_admin.add_argument('login')
    add_admin.add_argument('--password')
    add_admin.add_argument('--name')
    add_admin.add_argument('--email')
    add_admin.add_argument('--allow-remote', action='store_true')
    add_admin.set_defaults(func=lambda args: cmd_add(args, 'admin'))

    add_user = sub.add_parser('add', help='создать пользователя')
    add_user.add_argument('login')
    add_user.add_argument('--password')
    add_user.add_argument('--name')
    add_user.add_argument('--email')
    add_user.add_argument('--allow-remote', action='store_true')
    add_user.set_defaults(func=lambda args: cmd_add(args, 'user'))

    sub.add_parser('list', help='показать учётные записи').set_defaults(func=cmd_list)

    passwd = sub.add_parser('passwd', help='сменить пароль')
    passwd.add_argument('login')
    passwd.add_argument('--password')
    passwd.add_argument('--no-must-change', action='store_true',
                        help='не требовать смены пароля при следующем входе')
    passwd.set_defaults(func=cmd_passwd)

    role = sub.add_parser('role', help='сменить роль')
    role.add_argument('login')
    role.add_argument('role', choices=user_store.ROLES)
    role.set_defaults(func=cmd_role)

    activate = sub.add_parser('activate', help='включить учётную запись')
    activate.add_argument('login')
    activate.set_defaults(func=lambda args: cmd_state(args, True))

    deactivate = sub.add_parser('deactivate', help='отключить учётную запись')
    deactivate.add_argument('login')
    deactivate.set_defaults(func=lambda args: cmd_state(args, False))

    allow_remote = sub.add_parser('allow-remote', help='разрешить вход из интернета')
    allow_remote.add_argument('login')
    allow_remote.set_defaults(func=lambda args: cmd_remote(args, True))

    deny_remote = sub.add_parser('deny-remote', help='запретить вход из интернета')
    deny_remote.add_argument('login')
    deny_remote.set_defaults(func=lambda args: cmd_remote(args, False))

    delete = sub.add_parser('delete', help='удалить учётную запись')
    delete.add_argument('login')
    delete.add_argument('--yes', action='store_true', help='без подтверждения')
    delete.set_defaults(func=cmd_delete)

    sess = sub.add_parser('sessions', help='показать или завершить сессии')
    sess.add_argument('login', nargs='?')
    sess.add_argument('--revoke', action='store_true', help='завершить сессии')
    sess.set_defaults(func=cmd_sessions)

    imp = sub.add_parser('import', help='импорт учётных записей из CSV')
    imp.add_argument('file')
    imp.add_argument('--role', default='user', choices=user_store.ROLES)
    imp.set_defaults(func=cmd_import)

    sub.add_parser('migrate', help='перенести администратора из auth.json').set_defaults(
        func=cmd_migrate)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not _require_sqlite():
        return 1
    try:
        metadata_store.init_schema()
        return args.func(args)
    except Exception as exc:  # noqa: BLE001 — CLI обязан показать причину, а не трассировку
        print('ОШИБКА:', exc, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
