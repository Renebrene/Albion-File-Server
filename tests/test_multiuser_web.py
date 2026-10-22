# -*- coding: utf-8 -*-
"""Веб-сценарии этапа 2: персональные учётки, роли, отзыв сессий.

Проверяются только в режиме SQLite: в режиме json сохраняется прежний
единственный администратор из auth.json (путь отката).
"""
import os
import re
import shutil
import tempfile
import unittest

import db as metadata_store

# Режим sqlite включается только в setUp: на уровне модуля флаг протёк бы в
# остальные тесты и в дочерние процессы.
import server  # noqa: E402
import sessions as session_store  # noqa: E402
import users as user_store  # noqa: E402

ADMIN_PASSWORD = 'Админ-Пароль-2026'
USER_PASSWORD = 'Юзер-Пароль-2026'


class MultiUserWebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.app.config.update(TESTING=True)
        server.limiter.enabled = False
        cls.data_dir = tempfile.mkdtemp(prefix='albion-multiuser-')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    def setUp(self):
        # Собственный каталог данных: тесты учёток не должны трогать базу и
        # auth.json других тестовых модулей.
        self._saved = {name: os.environ.get(name) for name in
                       ('ALBION_DATA_DIR', 'ALBION_METADATA_BACKEND')}
        os.environ['ALBION_DATA_DIR'] = self.data_dir
        os.environ['ALBION_METADATA_BACKEND'] = 'sqlite'
        if not metadata_store.sqlite_enabled():
            self.skipTest('сценарии этапа 2 работают в режиме SQLite')
        self._saved_lan = os.environ.get('ALBION_LAN_HOST')
        server.LAN_HOST = ''           # по умолчанию правило внешнего входа выключено
        metadata_store.close()
        conn = metadata_store.init_schema()
        conn.execute('DELETE FROM sessions')
        conn.execute('DELETE FROM users')
        self.admin = user_store.create_user('admin1', ADMIN_PASSWORD, role='admin',
                                            allow_remote=True)
        self.user = user_store.create_user('user1', USER_PASSWORD, role='user')

    def tearDown(self):
        server.LAN_HOST = ''
        metadata_store.close()
        for name, value in list(self._saved.items()) + [('ALBION_LAN_HOST', self._saved_lan)]:
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    # --- вспомогательное ---

    def login(self, login_name, password, follow=True):
        client = server.app.test_client()
        page = client.get('/login')
        token = re.search(rb'name="csrf_token" value="([^"]+)"', page.data).group(1).decode()
        response = client.post('/login', data={
            'username': login_name, 'password': password, 'csrf_token': token,
        })
        if follow and response.status_code == 302:
            page = client.get('/f/')
            match = re.search(rb'<meta name="csrf-token" content="([^"]+)"', page.data)
            if match:
                client.environ_base['HTTP_X_CSRF_TOKEN'] = match.group(1).decode()
        return client, response

    def csrf_of(self, client):
        page = client.get('/settings/users')
        match = re.search(rb'name="csrf_token" value="([^"]+)"', page.data)
        return match.group(1).decode() if match else client.environ_base.get('HTTP_X_CSRF_TOKEN', '')

    # --- вход и роли ---

    def test_user_logs_in_and_gets_files_but_not_settings(self):
        client, response = self.login('user1', USER_PASSWORD)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(client.get('/f/').status_code, 200)
        self.assertEqual(client.get('/settings').status_code, 403)
        self.assertEqual(client.get('/settings/users').status_code, 403)
        self.assertEqual(client.get('/settings/tokens').status_code, 403)

    def test_admin_enters_settings(self):
        client, _ = self.login('admin1', ADMIN_PASSWORD)
        self.assertEqual(client.get('/settings').status_code, 200)
        self.assertEqual(client.get('/settings/users').status_code, 200)

    def test_wrong_password_is_rejected_generically(self):
        _, response = self.login('user1', 'неверный-пароль-2026')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Неверный логин или пароль', response.get_data(as_text=True))
        _, unknown = self.login('нет-такого', 'любой-пароль-2026')
        self.assertIn('Неверный логин или пароль', unknown.get_data(as_text=True))

    def test_disabled_account_cannot_log_in(self):
        user_store.update_user(self.user['id'], active=False)
        _, response = self.login('user1', USER_PASSWORD)
        self.assertIn('отключена', response.get_data(as_text=True))

    def test_lockout_after_repeated_failures(self):
        for _ in range(user_store.MAX_FAILED_LOGINS):
            self.login('user1', 'неверный-пароль-2026')
        _, response = self.login('user1', USER_PASSWORD)
        self.assertEqual(response.status_code, 429)
        self.assertIn('заблокирована', response.get_data(as_text=True))

    # --- сессии ---

    def test_logout_revokes_session(self):
        client, _ = self.login('user1', USER_PASSWORD)
        self.assertEqual(client.get('/f/').status_code, 200)
        client.get('/logout')
        self.assertEqual(client.get('/f/').status_code, 302)

    def test_revoked_session_stops_working_immediately(self):
        client, _ = self.login('user1', USER_PASSWORD)
        self.assertEqual(client.get('/f/').status_code, 200)
        session_store.revoke_all_for_user(self.user['id'])
        self.assertEqual(client.get('/f/').status_code, 302)

    def test_password_change_keeps_current_session_and_kills_others(self):
        other, _ = self.login('user1', USER_PASSWORD)
        client, _ = self.login('user1', USER_PASSWORD)
        page = client.get('/password')
        token = re.search(rb'name="csrf_token" value="([^"]+)"', page.data).group(1).decode()
        changed = client.post('/password', data={
            'current_password': USER_PASSWORD,
            'new_password': 'Совсем-Новый-Пароль-2026',
            'new_password_confirm': 'Совсем-Новый-Пароль-2026',
            'csrf_token': token,
        })
        self.assertEqual(changed.status_code, 302)
        self.assertEqual(client.get('/f/').status_code, 200, 'текущая сессия остаётся рабочей')
        self.assertEqual(other.get('/f/').status_code, 302, 'прочие сессии отозваны')
        _, old_password = self.login('user1', USER_PASSWORD)
        self.assertIn('Неверный логин или пароль', old_password.get_data(as_text=True))

    def test_password_change_requires_correct_current_password(self):
        client, _ = self.login('user1', USER_PASSWORD)
        page = client.get('/password')
        token = re.search(rb'name="csrf_token" value="([^"]+)"', page.data).group(1).decode()
        response = client.post('/password', data={
            'current_password': 'не тот пароль',
            'new_password': 'Совсем-Новый-Пароль-2026',
            'new_password_confirm': 'Совсем-Новый-Пароль-2026',
            'csrf_token': token,
        })
        self.assertIn('Текущий пароль указан неверно', response.get_data(as_text=True))

    # --- обязательная смена пароля и внешний вход ---

    def test_must_change_password_redirects_to_password_page(self):
        user_store.set_password(self.user['id'], 'Временный-Пароль-2026', must_change=True)
        client, response = self.login('user1', 'Временный-Пароль-2026')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith('/password'))
        self.assertEqual(client.get('/f/').status_code, 302)

        page = client.get('/password')
        self.assertNotIn(b'current_password', page.data, 'текущий пароль не спрашиваем')
        token = re.search(rb'name="csrf_token" value="([^"]+)"', page.data).group(1).decode()
        client.post('/password', data={
            'new_password': 'Постоянный-Пароль-2026',
            'new_password_confirm': 'Постоянный-Пароль-2026',
            'csrf_token': token,
        })
        self.assertEqual(client.get('/f/').status_code, 200)

    def test_remote_login_rule_applies_to_users_only(self):
        server.LAN_HOST = '192.168.1.7'   # правило включено
        # Тестовый клиент приходит с loopback, то есть считается внешним.
        _, response = self.login('user1', USER_PASSWORD)
        self.assertIn('разрешён только администратору', response.get_data(as_text=True))
        _, admin_response = self.login('admin1', ADMIN_PASSWORD)
        self.assertEqual(admin_response.status_code, 302)

        user_store.update_user(self.user['id'], allow_remote=True)
        _, allowed = self.login('user1', USER_PASSWORD)
        self.assertEqual(allowed.status_code, 302, 'флаг allow_remote — исключение для сотрудника')

    def test_admin_enters_from_outside_without_extra_flag(self):
        """Администратору внешний вход разрешён ролью, отдельный флаг не нужен."""
        server.LAN_HOST = '192.168.1.7'
        user_store.update_user(self.admin['id'], allow_remote=False)
        _, response = self.login('admin1', ADMIN_PASSWORD)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(server.remote_access_allowed({'role': 'admin', 'allow_remote': False}), True)
        self.assertEqual(server.remote_access_allowed({'role': 'user', 'allow_remote': False}), False)

    def test_rule_is_inactive_until_lan_listener_is_configured(self):
        server.LAN_HOST = ''
        _, response = self.login('user1', USER_PASSWORD)
        self.assertEqual(response.status_code, 302, 'без LAN-листенера правило не применяется')

    # --- раздел пользователей ---

    def test_admin_creates_user_and_sees_password_once(self):
        client, _ = self.login('admin1', ADMIN_PASSWORD)
        token = self.csrf_of(client)
        created = client.post('/settings/users/create', data={
            'login': 'novy', 'display_name': 'Новый Сотрудник', 'role': 'user',
            'password': '', 'allow_remote': '1', 'csrf_token': token,
        })
        self.assertEqual(created.status_code, 302)
        user = user_store.get_user('novy')
        self.assertIsNotNone(user)
        self.assertTrue(user['must_change_password'])
        self.assertTrue(user['allow_remote'])

        page = client.get('/settings/users')
        body = page.get_data(as_text=True)
        self.assertIn('Пароль для «novy»', body)
        self.assertIn('novy', body)
        # повторный заход — пароль больше не показывается
        self.assertNotIn('Пароль для «novy»', client.get('/settings/users').get_data(as_text=True))

    def test_admin_resets_password_and_revokes_sessions(self):
        victim, _ = self.login('user1', USER_PASSWORD)
        client, _ = self.login('admin1', ADMIN_PASSWORD)
        token = self.csrf_of(client)
        reset = client.post('/settings/users/reset', data={
            'user_id': self.user['id'], 'csrf_token': token,
        })
        self.assertEqual(reset.status_code, 302)
        self.assertEqual(victim.get('/f/').status_code, 302, 'сессии сброшенного пароля отозваны')
        stored = user_store.get_user_by_id(self.user['id'])
        self.assertTrue(stored['must_change_password'])

    def test_admin_cannot_delete_own_account_or_last_admin(self):
        client, _ = self.login('admin1', ADMIN_PASSWORD)
        token = self.csrf_of(client)
        response = client.post('/settings/users/delete', data={
            'user_id': self.admin['id'], 'csrf_token': token,
        })
        body = response.get_data(as_text=True)
        self.assertIn('Нельзя удалить учётную запись, под которой вы работаете', body)
        self.assertIsNotNone(user_store.get_user_by_id(self.admin['id']))

    def test_role_downgrade_revokes_sessions(self):
        admin_session, _ = self.login('admin1', ADMIN_PASSWORD)
        second = user_store.create_user('admin2', 'Второй-Админ-2026', role='admin')
        client, _ = self.login('admin2', 'Второй-Админ-2026')
        token = self.csrf_of(client)
        client.post('/settings/users/update', data={
            'user_id': second['id'], 'action': 'to_user', 'csrf_token': token,
        })
        self.assertEqual(user_store.get_user_by_id(second['id'])['role'], 'user')
        self.assertEqual(client.get('/f/').status_code, 302, 'сессии понижённого админа отозваны')
        self.assertEqual(admin_session.get('/settings').status_code, 200)

    def test_csv_import_through_web_form(self):
        client, _ = self.login('admin1', ADMIN_PASSWORD)
        token = self.csrf_of(client)
        response = client.post('/settings/users/import', data={
            'csv_text': 'login;display_name;role\nanna;Анна;user\nborya;Борис;user\nborya;Дубль;\n',
            'csrf_token': token,
        })
        body = response.get_data(as_text=True)
        self.assertIn('Создано учётных записей: <b>2</b>', body)
        self.assertIn('Отклонённые строки', body)
        self.assertIsNotNone(user_store.get_user('anna'))
        self.assertIsNotNone(user_store.get_user('borya'))

    def test_users_page_requires_admin(self):
        client, _ = self.login('user1', USER_PASSWORD)
        self.assertEqual(client.get('/settings/users').status_code, 403)
        anon = server.app.test_client()
        self.assertEqual(anon.get('/settings/users').status_code, 302)


if __name__ == '__main__':
    unittest.main()
