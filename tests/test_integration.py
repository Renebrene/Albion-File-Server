"""Tests for machine-token integration endpoints (verify/upload).

The app reads ALBION_DATA_DIR once at import time, so this module points it at
an isolated temporary directory before importing server.  The repository's
runner is unittest (pytest is not installed in the project venv), therefore the
isolated directory is created with tempfile instead of pytest's tmp_path fixture.
"""
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

_DATA = tempfile.mkdtemp(prefix='albion-integration-')
os.environ.setdefault('ALBION_DATA_DIR', _DATA)

import server  # noqa: E402

FOLDER_ID = '11111111-1111-1111-1111-111111111111'
OTHER_FOLDER_ID = '22222222-2222-2222-2222-222222222222'


class IntegrationApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.app.config.update(TESTING=True)

    def setUp(self):
        server.limiter.enabled = False
        server.limiter.reset()
        os.makedirs(server.UPLOAD_FOLDER, exist_ok=True)
        os.makedirs(server.ZIPLINKS_DIR, exist_ok=True)
        for path, value in (
            (server.DB_FILE, {}),
            (server.FOLDERS_DB_FILE, {
                FOLDER_ID: {'name': 'Inbox', 'parent_id': None,
                            'created': '20.10.2026 12:00', 'slug': 'inbox'},
                OTHER_FOLDER_ID: {'name': 'Other', 'parent_id': None,
                                  'created': '20.10.2026 12:00', 'slug': 'other'},
            }),
            (server.TOKENS_FILE, {}),
            (server.STATS_FILE, {'last_upload': None, 'last_export': None}),
        ):
            server._save_json(path, value)
        for entry in os.listdir(server.UPLOAD_FOLDER):
            path = os.path.join(server.UPLOAD_FOLDER, entry)
            if os.path.isfile(path):
                os.remove(path)
            elif os.path.isdir(path):
                shutil.rmtree(path)
        for entry in os.listdir(server.ZIPLINKS_DIR):
            os.remove(os.path.join(server.ZIPLINKS_DIR, entry))
        if os.path.exists(server.SETTINGS_FILE):
            os.remove(server.SETTINGS_FILE)
        server.app.config['MAX_CONTENT_LENGTH'] = (
            int(server.load_quota_settings()['max_upload_gb'] * 1024 ** 3) or None)
        folders = server.load_folders()
        for folder_id in (FOLDER_ID, OTHER_FOLDER_ID):
            os.makedirs(server.get_folder_physical_path(folder_id, folders), exist_ok=True)
        self.client = server.app.test_client()

    def tearDown(self):
        server.limiter.enabled = False
        server.limiter.reset()

    def add_token(self, name='Photobase', scopes=None, folder_id=FOLDER_ID,
                  expires_at=None, include_scopes=True):
        key = 'alb_' + secrets.token_hex(24)
        meta = {'name': name, 'created': '20.10.2026 12:00'}
        if include_scopes:
            meta['scopes'] = ['download'] if scopes is None else scopes
        if folder_id is not None:
            meta['folder_id'] = folder_id
        if expires_at is not None:
            meta['expires_at'] = expires_at
        tokens = server.load_tokens()
        tokens[hashlib.sha256(key.encode()).hexdigest()] = meta
        server._save_json(server.TOKENS_FILE, tokens)
        return key

    def post_file(self, key, filename='photo.jpg', content=b'image-bytes', **fields):
        data = {'file': (io.BytesIO(content), filename)}
        data.update(fields)
        headers = {'X-API-Key': key} if key else {}
        return self.client.post('/api/v1/integration/upload', data=data,
                                headers=headers, content_type='multipart/form-data')

    # 1. Нет ключа / неверный ключ -> 401.
    def test_verify_requires_valid_key(self):
        self.assertEqual(self.client.get('/api/v1/integration/verify').status_code, 401)
        response = self.client.get('/api/v1/integration/verify',
                                   headers={'X-API-Key': 'alb_' + '0' * 48})
        self.assertEqual(response.status_code, 401)
        self.assertIn('X-API-Key', response.get_json()['error'])

    # 2. download-only -> 403; upload -> 200 с корректными scopes.
    def test_verify_scope_rules(self):
        download_key = self.add_token(scopes=['download'])
        response = self.client.get('/api/v1/integration/verify',
                                   headers={'X-API-Key': download_key})
        self.assertEqual(response.status_code, 403)

        upload_key = self.add_token(name='Uploader', scopes=['upload', 'download'])
        response = self.client.get('/api/v1/integration/verify',
                                   headers={'X-API-Key': upload_key})
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['name'], 'Uploader')
        self.assertEqual(payload['scopes'], ['upload', 'download'])
        self.assertEqual(payload['folder_id'], FOLDER_ID)
        self.assertEqual(payload['base_url'], server.BASE_URL)
        self.assertIn('max_upload_gb', payload['limits'])

    def test_old_token_without_scopes_defaults_to_download(self):
        key = self.add_token(include_scopes=False)
        meta = server.check_api_key(key)
        self.assertEqual(meta['scopes'], ['download'])
        self.assertFalse(meta['expired'])
        self.assertEqual(self.client.get('/api/v1/integration/verify',
                                         headers={'X-API-Key': key}).status_code, 403)

    # 3. upload без права upload -> 403.
    def test_upload_requires_upload_scope(self):
        key = self.add_token(scopes=['download'])
        self.assertEqual(self.post_file(key).status_code, 403)

    # 4. Чужая папка -> 403; несуществующая -> 404.
    def test_upload_folder_binding(self):
        bound = self.add_token(scopes=['upload'], folder_id=FOLDER_ID)
        response = self.post_file(bound, folder_id=OTHER_FOLDER_ID)
        self.assertEqual(response.status_code, 403)

        unbound = self.add_token(name='Unbound', scopes=['upload'], folder_id=None)
        response = self.post_file(unbound, folder_id='missing-folder-id')
        self.assertEqual(response.status_code, 404)

        response = self.post_file(unbound)
        self.assertEqual(response.status_code, 403)

    # 5. Успешная загрузка: файл, запись, url, object_key.
    def test_successful_upload(self):
        key = self.add_token(scopes=['upload'], folder_id=FOLDER_ID)
        before = datetime.now(timezone.utc)
        response = self.post_file(key, filename='photo.jpg', content=b'photo-data')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()

        self.assertTrue(payload['url'].startswith(server.BASE_URL + '/'))
        self.assertEqual(payload['url'], payload['short_url'])
        self.assertEqual(payload['object_key'], 'uploads/Inbox/photo.jpg')
        self.assertEqual(payload['folder_id'], FOLDER_ID)
        self.assertEqual(payload['name'], 'photo.jpg')
        self.assertNotIn(key, response.get_data(as_text=True))

        saved = os.path.join(server.UPLOAD_FOLDER, 'Inbox', 'photo.jpg')
        self.assertTrue(os.path.isfile(saved))
        with open(saved, 'rb') as f:
            self.assertEqual(f.read(), b'photo-data')

        db = server.load_db()
        self.assertIn(payload['file_id'], db)
        record = db[payload['file_id']]
        self.assertEqual(record['access_id'], payload['file_id'])
        self.assertEqual(record['current_filename'], 'photo.jpg')
        self.assertEqual(record['physical_path'], 'Inbox/photo.jpg')
        self.assertEqual(record['folder_id'], FOLDER_ID)
        self.assertEqual(record['size'], len(b'photo-data'))
        self.assertTrue(record['slug'])
        self.assertEqual(payload['url'], f"{server.BASE_URL}/{record['slug']}")

        expires = datetime.fromisoformat(payload['expires_at'])
        self.assertIsNotNone(expires.tzinfo)
        delta = expires - before
        self.assertGreaterEqual(delta, timedelta(days=89, hours=23))
        self.assertLessEqual(delta, timedelta(days=90, minutes=1))

    def test_ttl_days_clamped_and_env_default(self):
        key = self.add_token(scopes=['upload'])
        before = datetime.now(timezone.utc)
        short = self.post_file(key, filename='short.txt', ttl_days='0')
        self.assertEqual(short.status_code, 200)
        short_delta = datetime.fromisoformat(short.get_json()['expires_at']) - before
        self.assertGreaterEqual(short_delta, timedelta(hours=23))
        self.assertLessEqual(short_delta, timedelta(days=1, minutes=1))

        long = self.post_file(key, filename='long.txt', ttl_days='99999')
        self.assertEqual(long.status_code, 200)
        long_delta = datetime.fromisoformat(long.get_json()['expires_at']) - before
        self.assertGreaterEqual(long_delta, timedelta(days=3649))
        self.assertLessEqual(long_delta, timedelta(days=3650, minutes=1))

        invalid = self.post_file(key, filename='bad-ttl.txt', ttl_days='soon')
        self.assertEqual(invalid.status_code, 400)

        old = os.environ.get('ALBION_INTEGRATION_TTL_DAYS')
        os.environ['ALBION_INTEGRATION_TTL_DAYS'] = '5'
        try:
            env_default = self.post_file(key, filename='env-ttl.txt')
            self.assertEqual(env_default.status_code, 200)
            delta = datetime.fromisoformat(env_default.get_json()['expires_at']) - before
            self.assertGreaterEqual(delta, timedelta(days=4, hours=23))
            self.assertLessEqual(delta, timedelta(days=5, minutes=1))
        finally:
            if old is None:
                os.environ.pop('ALBION_INTEGRATION_TTL_DAYS', None)
            else:
                os.environ['ALBION_INTEGRATION_TTL_DAYS'] = old

    def test_unsafe_filename_is_rejected(self):
        key = self.add_token(scopes=['upload'])
        for filename in ('../outside.txt', 'dir/file.txt', '..', 'no-extension'):
            response = self.post_file(key, filename=filename)
            self.assertEqual(response.status_code, 400, filename)
        self.assertFalse(os.path.exists(os.path.join(_DATA, 'outside.txt')))

    # 6. Дубликат имени получает суффикс.
    def test_duplicate_name_gets_suffix(self):
        key = self.add_token(scopes=['upload'])
        first = self.post_file(key, filename='report.txt', content=b'one')
        second = self.post_file(key, filename='report.txt', content=b'two')
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.get_json()['name'], 'report.txt')
        self.assertEqual(second.get_json()['name'], 'report_1.txt')
        self.assertTrue(os.path.isfile(os.path.join(server.UPLOAD_FOLDER, 'Inbox', 'report.txt')))
        self.assertTrue(os.path.isfile(os.path.join(server.UPLOAD_FOLDER, 'Inbox', 'report_1.txt')))
        names = {rec['current_filename'] for rec in server.load_db().values()}
        self.assertEqual(names, {'report.txt', 'report_1.txt'})

    # 7. Просроченный токен -> 403.
    def test_expired_token_is_rejected(self):
        expired = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        key = self.add_token(scopes=['upload'], expires_at=expired)
        self.assertEqual(self.client.get('/api/v1/integration/verify',
                                         headers={'X-API-Key': key}).status_code, 403)
        self.assertEqual(self.post_file(key).status_code, 403)
        self.assertFalse(server.load_db())

    # 8. Превышение размера -> 413.
    def test_oversized_upload_returns_413(self):
        key = self.add_token(scopes=['upload'])
        old_limit = server.app.config.get('MAX_CONTENT_LENGTH')
        try:
            server.app.config['MAX_CONTENT_LENGTH'] = 100
            response = self.post_file(key, content=b'x' * 500)
            self.assertEqual(response.status_code, 413)
        finally:
            server.app.config['MAX_CONTENT_LENGTH'] = old_limit

    # 9. Ключ не попадает в логи и ответы.
    def test_key_never_appears_in_logs(self):
        key = self.add_token(scopes=['upload'])
        with self.assertLogs(server.app.logger, level='INFO') as captured:
            response = self.post_file(key, filename='logged.txt')
        self.assertEqual(response.status_code, 200)
        logs = '\n'.join(captured.output)
        self.assertNotIn(key, logs)
        self.assertNotIn('X-API-Key', logs)
        self.assertIn('Интеграционная загрузка', logs)
        self.assertNotIn(key, response.get_data(as_text=True))

    # 10. Отзыв токена -> 401.
    def test_revoked_token_returns_401(self):
        key = self.add_token(scopes=['upload'])
        tokens = server.load_tokens()
        tokens.pop(hashlib.sha256(key.encode()).hexdigest())
        server._save_json(server.TOKENS_FILE, tokens)
        self.assertEqual(self.client.get('/api/v1/integration/verify',
                                         headers={'X-API-Key': key}).status_code, 401)
        self.assertEqual(self.post_file(key).status_code, 401)

    def test_rate_limit_is_per_token(self):
        key = self.add_token(name='RateLimited', scopes=['upload'])
        server.limiter.reset()
        server.limiter.enabled = True
        try:
            statuses = [self.post_file(key, filename='rate.txt').status_code
                        for _ in range(61)]
        finally:
            server.limiter.enabled = False
            server.limiter.reset()
        self.assertEqual(statuses[:60], [200] * 60)
        self.assertEqual(statuses[60], 429)

    # 11. Удаление: только с правом delete и только в своей папке.
    def test_delete_requires_delete_scope(self):
        key = self.add_token(scopes=['upload'])
        uploaded = self.post_file(key, filename='del.txt').get_json()

        response = self.client.delete(
            f"/api/v1/integration/files/{uploaded['file_id']}",
            headers={'X-API-Key': key},
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn(uploaded['file_id'], server.load_db())

    def test_delete_removes_file_and_record(self):
        key = self.add_token(scopes=['upload', 'delete'])
        uploaded = self.post_file(key, filename='del.txt', content=b'x').get_json()
        path = os.path.join(server.UPLOAD_FOLDER, 'Inbox', 'del.txt')
        self.assertTrue(os.path.isfile(path))

        response = self.client.delete(
            f"/api/v1/integration/files/{uploaded['file_id']}",
            headers={'X-API-Key': key},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['deleted'])
        self.assertFalse(os.path.exists(path))
        self.assertNotIn(uploaded['file_id'], server.load_db())

    def test_delete_rejects_another_folder(self):
        uploader = self.add_token(name='Up', scopes=['upload'], folder_id=FOLDER_ID)
        uploaded = self.post_file(uploader, filename='del.txt').get_json()
        deleter = self.add_token(name='Del', scopes=['delete'],
                                 folder_id=OTHER_FOLDER_ID)

        response = self.client.delete(
            f"/api/v1/integration/files/{uploaded['file_id']}",
            headers={'X-API-Key': deleter},
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn(uploaded['file_id'], server.load_db())

    def test_delete_unknown_file_is_404(self):
        key = self.add_token(scopes=['upload', 'delete'])

        response = self.client.delete(
            '/api/v1/integration/files/does-not-exist',
            headers={'X-API-Key': key},
        )

        self.assertEqual(response.status_code, 404)


class ManageTokensCliTests(unittest.TestCase):
    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix='albion-tokens-cli-')
        self.env = os.environ.copy()
        self.env['ALBION_DATA_DIR'] = self.data_dir
        self.script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   'manage_tokens.py')

    def tearDown(self):
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def run_cli(self, *args):
        return subprocess.run([sys.executable, self.script, *args],
                              env=self.env, capture_output=True, text=True)

    def test_add_with_scopes_and_folder_and_list(self):
        result = self.run_cli('add', 'Photobase', '--scopes', 'upload,download',
                              '--folder', FOLDER_ID)
        self.assertEqual(result.returncode, 0, result.stderr)
        match = re.search(r'alb_[0-9a-f]{48}', result.stdout)
        self.assertIsNotNone(match)

        with open(os.path.join(self.data_dir, 'tokens.json'), encoding='utf-8') as f:
            tokens = json.load(f)
        key_hash = hashlib.sha256(match.group(0).encode()).hexdigest()
        self.assertEqual(tokens[key_hash]['scopes'], ['upload', 'download'])
        self.assertEqual(tokens[key_hash]['folder_id'], FOLDER_ID)

        listing = self.run_cli('list')
        self.assertEqual(listing.returncode, 0)
        self.assertIn('upload,download', listing.stdout)
        self.assertIn(FOLDER_ID, listing.stdout)

    def test_add_without_scopes_is_download_only(self):
        result = self.run_cli('add', 'Legacy')
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(os.path.join(self.data_dir, 'tokens.json'), encoding='utf-8') as f:
            tokens = json.load(f)
        self.assertEqual(next(iter(tokens.values()))['scopes'], ['download'])

    def test_invalid_scope_and_missing_folder_are_rejected(self):
        invalid_scope = self.run_cli('add', 'Bad', '--scopes', 'download,admin',
                                     '--folder', FOLDER_ID)
        self.assertNotEqual(invalid_scope.returncode, 0)
        missing_folder = self.run_cli('add', 'NoFolder', '--scopes', 'upload')
        self.assertNotEqual(missing_folder.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.data_dir, 'tokens.json')))


if __name__ == '__main__':
    unittest.main()
