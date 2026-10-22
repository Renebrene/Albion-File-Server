"""Проверка работы приложения с ALBION_METADATA_BACKEND=sqlite.

Тесты гоняют реальные сценарии (вход, загрузка, публичная ссылка, удаление,
папки, токен) через тот же код, что и production, но метаданные лежат в SQLite.

Запуск: ALBION_DATA_DIR=/tmp/x python -m unittest tests.test_sqlite_backend -v
"""
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from werkzeug.security import generate_password_hash

# Изоляция: если сервер ещё не импортирован (запуск модуля отдельно),
# работаем в собственном временном каталоге и никогда — в каталоге проекта.
if 'server' not in sys.modules:
    os.environ['ALBION_DATA_DIR'] = tempfile.mkdtemp(prefix='albion-sqlite-run-')

import db  # noqa: E402
import server  # noqa: E402

# Режим sqlite включается только внутри setUp: на уровне модуля флаг протёк бы
# в остальные тесты и в дочерние процессы (CLI-тесты запускают manage_tokens.py).
assert os.path.abspath(server.DATA_DIR) != '/home/alexice/projects/albion-files', \
    'тесты не должны работать с каталогом проекта'


class SqliteBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.app.config.update(TESTING=True)
        server.limiter.enabled = False
        cls.client = server.app.test_client()

    def setUp(self):
        # Другие тестовые модули меняют окружение: фиксируем его здесь заново,
        # чтобы тесты sqlite-режима всегда работали в своём каталоге.
        self._saved_backend = os.environ.get('ALBION_METADATA_BACKEND')
        self._saved_data_dir = os.environ.get('ALBION_DATA_DIR')
        os.environ['ALBION_METADATA_BACKEND'] = 'sqlite'
        os.environ['ALBION_DATA_DIR'] = server.DATA_DIR
        db.close()
        self.assertEqual(db.backend(), 'sqlite', 'тест должен идти в режиме sqlite')
        os.makedirs(server.UPLOAD_FOLDER, exist_ok=True)
        os.makedirs(server.ZIPLINKS_DIR, exist_ok=True)
        for name, value in (('files', {}), ('folders', {}), ('tokens', {})):
            getattr(db, f'save_{name}')(value)
        db.save_document('stats', {'last_upload': None, 'last_export': None})
        db.save_document('settings', server._quota_defaults())
        for entry in os.listdir(server.UPLOAD_FOLDER):
            path = os.path.join(server.UPLOAD_FOLDER, entry)
            if os.path.isfile(path):
                os.remove(path)
            elif os.path.isdir(path):
                shutil.rmtree(path)
        with open(server.AUTH_FILE, 'w', encoding='utf-8') as handle:
            generate = generate_password_hash('Test-Password-2026!')
            json.dump({'username': 'test', 'password_hash': generate}, handle)
        self.client = server.app.test_client()
        server._download_windows.clear()
        server._export_all_scopes.clear()
        self.login()

    def tearDown(self):
        for name, value in (('ALBION_METADATA_BACKEND', self._saved_backend),
                            ('ALBION_DATA_DIR', self._saved_data_dir)):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        db.close()

    # --- вспомогательное ---

    def login(self):
        page = self.client.get('/login')
        token = re.search(rb'name="csrf_token" value="([^"]+)"', page.data).group(1).decode()
        self.client.post('/login', data={
            'username': 'test', 'password': 'Test-Password-2026!', 'csrf_token': token,
        })
        page = self.client.get('/f/')
        match = re.search(rb'<meta name="csrf-token" content="([^"]+)"', page.data)
        if match:
            self.csrf = match.group(1).decode()
            self.client.environ_base['HTTP_X_CSRF_TOKEN'] = self.csrf

    def upload(self, name='sqlite-case.jpg', payload=b'image-bytes'):
        return self.client.post('/upload', data={
            'files': (io.BytesIO(payload), name),
        }, content_type='multipart/form-data')

    # --- сценарии ---

    def test_upload_writes_to_sqlite_not_json(self):
        response = self.upload()
        self.assertIn(response.status_code, (200, 201, 302))
        files = db.load_files()
        self.assertEqual(len(files), 1)
        record = next(iter(files.values()))
        self.assertEqual(record['original_filename'], 'sqlite-case.jpg')
        # JSON-база не должна обновляться в режиме sqlite
        json_path = server.DB_FILE
        if os.path.exists(json_path):
            with open(json_path, encoding='utf-8') as handle:
                self.assertEqual(json.load(handle), {})

    def test_uploaded_file_is_listed_and_served_publicly(self):
        self.upload()
        page = self.client.get('/f/')
        self.assertEqual(page.status_code, 200)
        self.assertIn('sqlite-case.jpg', page.data.decode('utf-8'))

        record = next(iter(db.load_files().values()))
        public = self.client.get(f"/{record['slug']}")
        self.assertEqual(public.status_code, 200)
        self.assertEqual(public.data, b'image-bytes')

    def test_public_route_works_for_anonymous_client(self):
        self.upload()
        record = next(iter(db.load_files().values()))
        anon = server.app.test_client()
        self.assertEqual(anon.get(f"/{record['slug']}").status_code, 200)

    def test_download_counter_persists_in_sqlite(self):
        self.upload()
        record = next(iter(db.load_files().values()))
        self.client.get(f"/{record['slug']}")
        stored = db.load_files()[record['access_id']]
        self.assertEqual(stored.get('download_count'), 1)

    def test_folder_and_file_deletion_remove_rows(self):
        folder = self.client.post('/api/create_folder', json={'name': 'Раздел SQLite'})
        self.assertEqual(folder.status_code, 200)
        folder_id = folder.get_json().get('folder_id') or folder.get_json().get('id')
        self.assertIsNotNone(folder_id)

        self.upload('в-папке.jpg')
        record = next(iter(db.load_files().values()))
        self.assertIn(folder_id, db.load_folders())

        removed = self.client.post('/api/delete_files_batch',
                                   json={'file_ids': [record['access_id']]})
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(removed.get_json()['count'], 1)
        self.assertEqual(db.load_files(), {})

    def test_machine_token_is_read_from_sqlite(self):
        folder = self.client.post('/api/create_folder', json={'name': 'Для токена'})
        folder_id = folder.get_json().get('folder_id') or folder.get_json().get('id')
        secret = 'alb_' + 'a' * 48
        key_hash = __import__('hashlib').sha256(secret.encode()).hexdigest()
        db.save_tokens({key_hash: {
            'name': 'Сайт', 'created': '22.10.2026 12:00',
            'created_at': '2026-10-22T12:00:00+03:00',
            'scopes': ['upload'], 'folder_id': folder_id, 'expires_at': None,
        }})
        response = self.client.get('/api/v1/integration/verify',
                                   headers={'X-API-Key': secret})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['name'], 'Сайт')

    def test_stats_and_settings_live_in_sqlite(self):
        server.save_stats({'last_upload': 'сегодня', 'last_export': None})
        self.assertEqual(db.load_document('stats')['last_upload'], 'сегодня')
        server.save_quota_settings({**server.load_quota_settings(), 'min_free_gb': 7.0})
        self.assertEqual(db.load_document('settings')['min_free_gb'], 7.0)
        self.assertEqual(server.load_quota_settings()['min_free_gb'], 7.0)

    def test_fifty_parallel_uploads_keep_every_record(self):
        """Критерий этапа 1: параллельные загрузки не теряют записи."""
        import threading

        errors = []
        count = 50
        cookie_name = server.app.config['SESSION_COOKIE_NAME']
        session_cookie = self.client.get_cookie(cookie_name)

        def worker(index):
            client = server.app.test_client()
            # каждый поток — свой HTTP-клиент, но сессия та же, что у setUp
            if session_cookie is not None:
                client.set_cookie(cookie_name, session_cookie.value, domain='localhost')
            client.environ_base['HTTP_X_CSRF_TOKEN'] = self.csrf
            try:
                response = client.post('/upload', data={
                    'files': (io.BytesIO(f'payload-{index}'.encode()), f'parallel-{index}.jpg'),
                }, content_type='multipart/form-data')
                if response.status_code != 200:
                    errors.append((index, response.status_code, response.data[:120]))
            except Exception as exc:  # noqa: BLE001
                errors.append((index, repr(exc)))
            finally:
                db.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        files = db.load_files()
        self.assertEqual(len(files), count)
        self.assertEqual(len({rec['slug'] for rec in files.values()}), count)
        self.assertEqual(len({rec['physical_path'] for rec in files.values()}), count)
        for record in files.values():
            self.assertTrue(os.path.isfile(server.safe_upload_path(record['physical_path'])))

    def test_cli_issues_token_into_sqlite(self):
        """manage_tokens.py в режиме sqlite пишет токен в базу, а не в файл."""
        import subprocess
        folder = self.client.post('/api/create_folder', json={'name': 'CLI папка'})
        folder_id = folder.get_json().get('folder_id') or folder.get_json().get('id')

        env = os.environ.copy()
        env['ALBION_DATA_DIR'] = server.DATA_DIR
        env['ALBION_METADATA_BACKEND'] = 'sqlite'
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              'manage_tokens.py')
        result = subprocess.run([sys.executable, script, 'add', 'Мониторинг',
                                 '--scopes', 'upload', '--folder', folder_id],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

        import hashlib
        import re
        secret = re.search(r'alb_[0-9a-f]{48}', result.stdout).group(0)
        key_hash = hashlib.sha256(secret.encode()).hexdigest()
        tokens = db.load_tokens()
        self.assertIn(key_hash, tokens)
        self.assertEqual(tokens[key_hash]['scopes'], ['upload'])
        # файл tokens.json не должен получить новый токен (в общем прогоне он
        # может существовать от json-режимных тестов)
        token_file = os.path.join(server.DATA_DIR, 'tokens.json')
        if os.path.exists(token_file):
            with open(token_file, encoding='utf-8') as handle:
                self.assertNotIn(key_hash, json.load(handle))

        listing = subprocess.run([sys.executable, script, 'list'],
                                 env=env, capture_output=True, text=True)
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertIn('Мониторинг', listing.stdout)

    def test_switching_back_to_json_keeps_files_intact(self):
        """Откат флага не трогает JSON-файлы и SQLite-базу."""
        db.save_files({'keep': {'access_id': 'keep', 'slug': 'keep-slug',
                                'physical_path': 'x.jpg', 'size': 1, 'is_image': True}})
        os.environ['ALBION_METADATA_BACKEND'] = 'json'
        try:
            self.assertEqual(server.load_db(), {})
        finally:
            os.environ['ALBION_METADATA_BACKEND'] = 'sqlite'
        self.assertEqual(list(db.load_files()), ['keep'])


if __name__ == '__main__':
    unittest.main()
