# -*- coding: utf-8 -*-
"""Тесты учётных записей и серверных сессий (users.py, sessions.py)."""
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from werkzeug.security import generate_password_hash

_DATA = tempfile.mkdtemp(prefix='albion-users-test-')

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _isolation import assert_isolated, ensure_temp_data_dir  # noqa: E402  # noqa: E402

ensure_temp_data_dir('albion-users-run-')

import db  # noqa: E402
import sessions  # noqa: E402
import users  # noqa: E402


class BaseUsersTest(unittest.TestCase):
    def setUp(self):
        # Режим sqlite включается только здесь: на уровне модуля флаг протёк бы
        # в остальные тесты (и в дочерние процессы CLI-тестов).
        self._saved = {name: os.environ.get(name) for name in
                       ('ALBION_DATA_DIR', 'ALBION_METADATA_BACKEND')}
        os.environ['ALBION_DATA_DIR'] = _DATA
        os.environ['ALBION_METADATA_BACKEND'] = 'sqlite'
        db.close()
        assert_isolated(os.environ.get('ALBION_DATA_DIR'), db.db_path())
        db.init_schema()
        conn = db.connect()
        conn.execute('DELETE FROM sessions')
        conn.execute('DELETE FROM users')
        self.auth_file = os.path.join(_DATA, 'auth.json')
        if os.path.exists(self.auth_file):
            os.remove(self.auth_file)

    def tearDown(self):
        db.close()
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class PasswordTests(BaseUsersTest):
    def test_hash_and_verify_roundtrip(self):
        stored = users.hash_password('Очень-Длинный-Пароль-2026')
        self.assertTrue(stored.startswith('$argon2id$'))
        ok, rehash = users.verify_password(stored, 'Очень-Длинный-Пароль-2026')
        self.assertTrue(ok)
        self.assertFalse(rehash)
        self.assertFalse(users.verify_password(stored, 'другой-пароль-2026')[0])

    def test_legacy_werkzeug_hash_is_accepted_and_marked_for_rehash(self):
        legacy = generate_password_hash('Старый-Пароль-2026')
        ok, rehash = users.verify_password(legacy, 'Старый-Пароль-2026')
        self.assertTrue(ok)
        self.assertTrue(rehash, 'прежний формат должен требовать перехода на argon2id')
        self.assertFalse(users.verify_password(legacy, 'не тот пароль')[0])

    def test_password_policy(self):
        self.assertIn('не короче', users.password_error('коротко'))
        self.assertIsNone(users.password_error('Достаточно-Длинный-2026'))
        self.assertIn('пробел', users.password_error(' Пароль-С-Пробелом-2026'))
        self.assertIn('пустым', users.password_error(''))

    def test_generated_password_fits_policy(self):
        for _ in range(5):
            password = users.generate_password()
            self.assertIsNone(users.password_error(password))


class UserRecordTests(BaseUsersTest):
    def test_create_and_fetch(self):
        user = users.create_user('ivan', 'Пароль-Ивана-2026', role='user')
        self.assertEqual(user['login'], 'ivan')
        self.assertEqual(user['role'], 'user')
        self.assertTrue(user['active'])
        self.assertFalse(user['allow_remote'])
        self.assertEqual(users.get_user('ivan')['id'], user['id'])

    def test_duplicate_login_rejected(self):
        users.create_user('ivan', 'Пароль-Ивана-2026')
        with self.assertRaises(users.UserError):
            users.create_user('ivan', 'Другой-Пароль-2026')

    def test_invalid_login_rejected(self):
        for login in ('', 'a b', 'x' * 65, 'иван!', '<script>'):
            with self.assertRaises(users.UserError):
                users.create_user(login, 'Пароль-Хороший-2026')

    def test_cyrillic_login_is_allowed(self):
        user = users.create_user('админ', 'Пароль-Админа-2026', role='admin')
        self.assertEqual(user['login'], 'админ')
        self.assertEqual(users.get_user('админ')['id'], user['id'])

    def test_invalid_role_rejected(self):
        with self.assertRaises(users.UserError):
            users.create_user('petr', 'Пароль-Петра-2026', role='editor')

    def test_last_admin_cannot_be_demoted_or_deleted(self):
        admin = users.create_user('admin1', 'Пароль-Админа-2026', role='admin')
        with self.assertRaises(users.UserError):
            users.update_user(admin['id'], role='user')
        with self.assertRaises(users.UserError):
            users.update_user(admin['id'], active=False)
        with self.assertRaises(users.UserError):
            users.delete_user(admin['id'])

        second = users.create_user('admin2', 'Пароль-Второго-2026', role='admin')
        users.update_user(admin['id'], role='user')      # теперь можно: админ не последний
        self.assertEqual(users.get_user_by_id(admin['id'])['role'], 'user')
        users.delete_user(admin['id'])                   # бывший админ удаляется
        self.assertIsNone(users.get_user_by_id(admin['id']))
        with self.assertRaises(users.UserError):
            users.delete_user(second['id'])               # а этот остался последним

    def test_update_flags_and_password(self):
        user = users.create_user('olga', 'Пароль-Ольги-2026')
        updated = users.update_user(user['id'], allow_remote=True, display_name='Ольга П.')
        self.assertTrue(updated['allow_remote'])
        self.assertEqual(updated['display_name'], 'Ольга П.')

        users.set_password(user['id'], 'Новый-Пароль-Ольги-2026', must_change=True)
        stored = users.get_user_by_id(user['id'])
        self.assertTrue(stored['must_change_password'])
        self.assertTrue(users.verify_password(stored['password_hash'], 'Новый-Пароль-Ольги-2026')[0])
        self.assertFalse(users.verify_password(stored['password_hash'], 'Пароль-Ольги-2026')[0])


class LockoutTests(BaseUsersTest):
    def test_lockout_after_ten_failures_and_reset_on_success(self):
        user = users.create_user('sergey', 'Пароль-Сергея-2026')
        for _ in range(users.MAX_FAILED_LOGINS - 1):
            self.assertIsNone(users.register_failure(user['id']))
        locked_until = users.register_failure(user['id'])
        self.assertIsNotNone(locked_until, 'на десятом промахе учётка блокируется')

        stored = users.get_user_by_id(user['id'])
        self.assertTrue(users.is_locked(stored))
        self.assertGreater(users.lock_remaining_minutes(stored), 0)

        users.register_success(user['id'])
        stored = users.get_user_by_id(user['id'])
        self.assertFalse(users.is_locked(stored))
        self.assertEqual(stored['failed_logins'], 0)

    def test_expired_lock_is_not_locked(self):
        user = users.create_user('anna', 'Пароль-Анны-2026')
        db.connect().execute('UPDATE users SET locked_until=? WHERE id=?',
                             ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                              user['id']))
        self.assertFalse(users.is_locked(users.get_user_by_id(user['id'])))


class MigrationTests(BaseUsersTest):
    def _write_auth(self, username='админ', password='Прежний-Пароль-2026'):
        with open(self.auth_file, 'w', encoding='utf-8') as handle:
            json.dump({'username': username,
                       'password_hash': generate_password_hash(password)}, handle)

    def test_migrates_existing_admin_with_same_password(self):
        self._write_auth()
        user, note = users.migrate_from_auth_json(self.auth_file)
        self.assertIsNotNone(user)
        self.assertEqual(user['role'], 'admin')
        self.assertEqual(user['login'], 'админ')
        self.assertIn('пароль перенесён', note)
        ok, rehash = users.verify_password(user['password_hash'], 'Прежний-Пароль-2026')
        self.assertTrue(ok)
        self.assertTrue(rehash)

    def test_migration_warns_about_login_that_violates_new_rules(self):
        self._write_auth(username='иван петрович')
        user, note = users.migrate_from_auth_json(self.auth_file)
        self.assertIsNotNone(user, 'доступ не должен теряться из-за правил нового логина')
        self.assertIn('логин', note)

    def test_migration_is_skipped_when_users_exist(self):
        users.create_user('admin1', 'Пароль-Админа-2026', role='admin')
        self._write_auth()
        user, note = users.migrate_from_auth_json(self.auth_file)
        self.assertIsNone(user)
        self.assertIn('уже есть', note)
        self.assertEqual(len(users.list_users()), 1)

    def test_migration_without_auth_file(self):
        user, note = users.migrate_from_auth_json(self.auth_file)
        self.assertIsNone(user)
        self.assertIn('не найден', note)


class CsvImportTests(BaseUsersTest):
    def test_import_with_headers_and_generated_passwords(self):
        text = ('login;display_name;role;allow_remote\n'
                'pavel;Павел;user;да\n'
                'marina;Марина;admin;\n')
        created, errors = users.import_csv(text)
        self.assertEqual(errors, [])
        self.assertEqual([row['login'] for row in created], ['pavel', 'marina'])
        self.assertTrue(all(row['password'] for row in created), 'пароли сгенерированы')
        self.assertTrue(users.get_user('pavel')['allow_remote'])
        self.assertEqual(users.get_user('marina')['role'], 'admin')

    def test_import_reports_bad_rows_and_keeps_good_ones(self):
        text = ('login,password\n'
                'good,Пароль-Хороший-2026\n'
                'bad,коротко\n'
                'good,Пароль-Дубль-2026\n')
        created, errors = users.import_csv(text)
        self.assertEqual([row['login'] for row in created], ['good'])
        self.assertIsNone(created[0]['password'], 'явный пароль в отчёт не попадает')
        self.assertEqual(len(errors), 2)
        self.assertIn('строка 3', errors[0])
        self.assertIn('занят', errors[1])

    def test_import_requires_login_column(self):
        with self.assertRaises(users.UserError):
            users.import_csv('name,role\nИван,user\n')

    def test_import_rejects_empty_file(self):
        with self.assertRaises(users.UserError):
            users.import_csv('   \n')

    def test_imported_users_must_change_password(self):
        created, _ = users.import_csv('login\nnina\n')
        self.assertTrue(users.get_user(created[0]['login'])['must_change_password'])


class SessionTests(BaseUsersTest):
    def test_create_and_resolve(self):
        user = users.create_user('kirill', 'Пароль-Кирилла-2026')
        token = sessions.create(user['id'], role='user', ip='127.0.0.1', user_agent='test')
        record = sessions.resolve(token)
        self.assertIsNotNone(record)
        self.assertEqual(record['user_id'], user['id'])

    def test_token_is_not_stored_in_plain_text(self):
        user = users.create_user('lida', 'Пароль-Лиды-2026')
        token = sessions.create(user['id'], role='user')
        stored = db.connect().execute('SELECT token_hash FROM sessions').fetchone()[0]
        self.assertNotEqual(stored, token)
        self.assertNotIn(token, stored)

    def test_ttl_depends_on_role(self):
        admin = users.create_user('boss', 'Пароль-Босса-2026', role='admin')
        plain = users.create_user('user1', 'Пароль-Юзера-2026')
        admin_token = sessions.create(admin['id'], role='admin')
        user_token = sessions.create(plain['id'], role='user')
        admin_expiry = sessions.resolve(admin_token)['expires_at']
        user_expiry = sessions.resolve(user_token)['expires_at']
        admin_dt = datetime.fromisoformat(admin_expiry)
        user_dt = datetime.fromisoformat(user_expiry)
        self.assertLess(admin_dt - datetime.now(timezone.utc), timedelta(hours=13))
        self.assertGreater(user_dt - datetime.now(timezone.utc), timedelta(days=29))

    def test_revoked_session_is_rejected(self):
        user = users.create_user('timur', 'Пароль-Тимура-2026')
        token = sessions.create(user['id'])
        self.assertTrue(sessions.revoke(token))
        self.assertIsNone(sessions.resolve(token))
        self.assertFalse(sessions.revoke(token), 'повторный отзыв ничего не делает')

    def test_revoke_all_for_user(self):
        user = users.create_user('vera', 'Пароль-Веры-2026')
        first = sessions.create(user['id'])
        second = sessions.create(user['id'])
        self.assertEqual(sessions.revoke_all_for_user(user['id']), 2)
        self.assertIsNone(sessions.resolve(first))
        self.assertIsNone(sessions.resolve(second))

    def test_expired_session_is_rejected(self):
        user = users.create_user('grigory', 'Пароль-Григория-2026')
        token = sessions.create(user['id'], ttl=timedelta(seconds=-1))
        self.assertIsNone(sessions.resolve(token))

    def test_purge_removes_only_old_records(self):
        user = users.create_user('nina2', 'Пароль-Нины-2026')
        old = sessions.create(user['id'])
        sessions.revoke(old)
        db.connect().execute(
            'UPDATE sessions SET revoked_at=? WHERE token_hash IS NOT NULL',
            ((datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),))
        fresh = sessions.create(user['id'])
        self.assertEqual(sessions.purge_expired(days=7), 1)
        self.assertIsNotNone(sessions.resolve(fresh))


if __name__ == '__main__':
    unittest.main()
