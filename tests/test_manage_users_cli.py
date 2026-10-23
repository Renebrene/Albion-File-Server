# -*- coding: utf-8 -*-
"""Тесты CLI manage_users.py (запуск отдельным процессом)."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, 'manage_users.py')


class ManageUsersCliTests(unittest.TestCase):
    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix='albion-users-cli-')
        self.env = os.environ.copy()
        self.env['ALBION_DATA_DIR'] = self.data_dir
        self.env['ALBION_METADATA_BACKEND'] = 'sqlite'

    def tearDown(self):
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def run_cli(self, *args, **kwargs):
        env = kwargs.pop('env', self.env)
        return subprocess.run([sys.executable, SCRIPT, *args],
                              env=env, capture_output=True, text=True, cwd=REPO_ROOT)

    def run_cli_ok(self, *args):
        result = self.run_cli(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def sql(self, query, params=()):
        import sqlite3
        conn = sqlite3.connect(os.path.join(self.data_dir, 'albion.db'))
        try:
            return conn.execute(query, params).fetchall()
        finally:
            conn.close()

    # --- создание и список ---

    def test_add_admin_with_cyrillic_login(self):
        result = self.run_cli_ok('add-admin', 'админ', '--password', 'Админ-Пароль-2026')
        self.assertIn('Создана учётная запись: админ', result.stdout)
        rows = self.sql('SELECT login, role, must_change_password FROM users')
        self.assertEqual(rows, [('админ', 'admin', 0)])

    def test_add_user_generates_password_once(self):
        result = self.run_cli_ok('add', 'ivan', '--name', 'Иван Петров')
        self.assertIn('Временный пароль:', result.stdout)
        rows = self.sql('SELECT login, role, must_change_password, display_name FROM users')
        self.assertEqual(rows, [('ivan', 'user', 1, 'Иван Петров')])

    def test_duplicate_login_is_rejected(self):
        self.run_cli_ok('add', 'ivan', '--password', 'Пароль-Ивана-2026')
        result = self.run_cli('add', 'ivan', '--password', 'Другой-Пароль-2026')
        self.assertEqual(result.returncode, 1)
        self.assertIn('занят', result.stderr)

    def test_list_shows_state(self):
        self.run_cli_ok('add-admin', 'admin1', '--password', 'Админ-Пароль-2026')
        self.run_cli_ok('add', 'ivan', '--password', 'Иван-Пароль-2026')
        self.run_cli_ok('deactivate', 'ivan')
        listing = self.run_cli_ok('list')
        self.assertIn('Администратор', listing.stdout)
        self.assertIn('отключена', listing.stdout)

    # --- права и пароли ---

    def test_role_change_and_must_change_flag(self):
        self.run_cli_ok('add-admin', 'admin1', '--password', 'Админ-Пароль-2026')
        self.run_cli_ok('add', 'ivan', '--password', 'Иван-Пароль-2026')
        result = self.run_cli_ok('role', 'ivan', 'admin')
        self.assertIn('Администратор', result.stdout)
        self.assertEqual(self.sql('SELECT role FROM users WHERE login=?', ('ivan',))[0][0], 'admin')

    def test_last_admin_cannot_be_demoted(self):
        self.run_cli_ok('add-admin', 'admin1', '--password', 'Админ-Пароль-2026')
        result = self.run_cli('role', 'admin1', 'user')
        self.assertEqual(result.returncode, 1)
        self.assertIn('последний активный администратор', result.stderr)

    def test_passwd_with_explicit_password_does_not_force_change(self):
        self.run_cli_ok('add', 'ivan', '--password', 'Иван-Пароль-2026')
        before = self.sql('SELECT password_hash FROM users WHERE login=?', ('ivan',))[0][0]
        result = self.run_cli_ok('passwd', 'ivan', '--password', 'Новый-Пароль-2026')
        self.assertIn('Пароль изменён', result.stdout)
        after, must_change = self.sql(
            'SELECT password_hash, must_change_password FROM users WHERE login=?', ('ivan',))[0]
        self.assertNotEqual(before, after)
        self.assertEqual(must_change, 0, 'явно заданный пароль окончательный')

    def test_passwd_generated_password_requires_change(self):
        self.run_cli_ok('add', 'ivan', '--password', 'Иван-Пароль-2026')
        result = self.run_cli_ok('passwd', 'ivan')
        self.assertIn('Временный пароль:', result.stdout)
        must_change = self.sql(
            'SELECT must_change_password FROM users WHERE login=?', ('ivan',))[0][0]
        self.assertEqual(must_change, 1)

    def test_passwd_flags_override_behaviour(self):
        self.run_cli_ok('add', 'ivan', '--password', 'Иван-Пароль-2026')
        self.run_cli_ok('passwd', 'ivan', '--password', 'Новый-Пароль-2026', '--must-change')
        self.assertEqual(self.sql('SELECT must_change_password FROM users WHERE login=?',
                                  ('ivan',))[0][0], 1)
        self.run_cli_ok('passwd', 'ivan', '--no-must-change')
        self.assertEqual(self.sql('SELECT must_change_password FROM users WHERE login=?',
                                  ('ivan',))[0][0], 0)

    def test_password_policy_is_enforced(self):
        self.run_cli_ok('add', 'ivan', '--password', 'Иван-Пароль-2026')
        result = self.run_cli('passwd', 'ivan', '--password', 'коротко')
        self.assertEqual(result.returncode, 1)
        self.assertIn('не короче', result.stderr)

    # --- сессии и внешний вход ---

    def test_sessions_revoke_reports_count(self):
        self.run_cli_ok('add', 'ivan', '--password', 'Иван-Пароль-2026')
        result = self.run_cli_ok('sessions', '--revoke')
        self.assertIn('Завершено сессий: 0', result.stdout)
        empty = self.run_cli_ok('sessions')
        self.assertIn('Активных сессий нет', empty.stdout)

    def test_remote_access_toggle(self):
        self.run_cli_ok('add', 'ivan', '--password', 'Иван-Пароль-2026')
        allowed = self.run_cli_ok('allow-remote', 'ivan')
        self.assertIn('разрешён', allowed.stdout)
        self.assertEqual(self.sql('SELECT allow_remote FROM users WHERE login=?', ('ivan',))[0][0], 1)
        denied = self.run_cli_ok('deny-remote', 'ivan')
        self.assertIn('запрещён', denied.stdout)

    # --- импорт и миграция ---

    def test_import_csv_reports_created_and_rejected(self):
        path = os.path.join(self.data_dir, 'users.csv')
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write('login;display_name;role\nanna;Анна;user\nbad login;Ошибка;user\n')
        result = self.run_cli_ok('import', path)
        self.assertIn('Создано учётных записей: 1', result.stdout)
        self.assertIn('Отклонено строк: 1', result.stdout)
        self.assertEqual(len(self.sql('SELECT id FROM users')), 1)

    def test_migrate_from_auth_json(self):
        with open(os.path.join(self.data_dir, 'auth.json'), 'w', encoding='utf-8') as handle:
            json.dump({'username': 'админ',
                       'password_hash': 'scrypt:32768:8:1$abc$def'}, handle)
        result = self.run_cli_ok('migrate')
        self.assertIn('Перенесена учётная запись: админ', result.stdout)
        self.assertEqual(self.sql("SELECT role FROM users WHERE login='админ'")[0][0], 'admin')
        self.assertIn('пароль перенесён', result.stdout.lower())

    def test_rejects_json_backend(self):
        env = dict(self.env, ALBION_METADATA_BACKEND='json')
        result = self.run_cli('list', env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn('SQLite', result.stderr)


if __name__ == '__main__':
    unittest.main()
