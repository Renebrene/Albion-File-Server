"""Isolated smoke/regression tests for the Flask app (stdlib unittest only).

Run: python -m unittest discover -s tests -v
"""
import io
import json
import os
import re
import tempfile
import unittest
from werkzeug.security import generate_password_hash

_DATA = tempfile.mkdtemp(prefix='albion-unittest-')
os.environ['ALBION_DATA_DIR'] = _DATA

import server  # noqa: E402


class ServerSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.app.config.update(TESTING=True)
        server.limiter.enabled = False
        cls.client = server.app.test_client()
        cls.anon = server.app.test_client()

    def setUp(self):
        os.makedirs(server.UPLOAD_FOLDER, exist_ok=True)
        os.makedirs(server.ZIPLINKS_DIR, exist_ok=True)
        for name, value in (
            (server.DB_FILE, {}),
            (server.FOLDERS_DB_FILE, {}),
            (server.TOKENS_FILE, {}),
            (server.STATS_FILE, {'last_upload': None, 'last_export': None}),
        ):
            server._save_json(name, value)
        for entry in os.listdir(server.UPLOAD_FOLDER):
            path = os.path.join(server.UPLOAD_FOLDER, entry)
            if os.path.isfile(path):
                os.remove(path)
            elif os.path.isdir(path):
                import shutil
                shutil.rmtree(path)
        for entry in os.listdir(server.ZIPLINKS_DIR):
            os.remove(os.path.join(server.ZIPLINKS_DIR, entry))
        with open(server.AUTH_FILE, 'w', encoding='utf-8') as f:
            json.dump({'username': 'test', 'password_hash': generate_password_hash('Test-Password-2026!')}, f)
        self.client = server.app.test_client()
        self.anon = server.app.test_client()
        server._download_windows.clear()
        server._export_all_scopes.clear()

    def login(self):
        page = self.client.get('/login')
        match = re.search(rb'name="csrf_token" value="([^"]+)"', page.data)
        if not match:
            raise AssertionError('Login form has no CSRF token')
        token = match.group(1).decode()
        response = self.client.post('/login', data={
            'username': 'test', 'password': 'Test-Password-2026!', 'csrf_token': token,
        })
        self.client.environ_base['HTTP_X_CSRF_TOKEN'] = token
        # Successful login rotates token; read the protected page's rendered token.
        page = self.client.get('/f/')
        match = re.search(rb'<meta name="csrf-token" content="([^"]+)"', page.data)
        if match:
            self.client.environ_base['HTTP_X_CSRF_TOKEN'] = match.group(1).decode()
        return response

    def test_static_branding_routes(self):
        self.assertEqual(self.anon.get('/favicon.ico').status_code, 200)
        self.assertEqual(self.anon.get('/static/logo.png').status_code, 200)
        self.assertEqual(self.anon.get('/robots.txt').status_code, 200)

    def test_auth_and_open_redirect(self):
        self.assertEqual(self.anon.get('/f/').status_code, 302)
        page = self.client.get('/login')
        token = re.search(rb'name="csrf_token" value="([^"]+)"', page.data).group(1).decode()
        r = self.client.post('/login?next=https://evil.invalid', data={
            'username': 'test', 'password': 'Test-Password-2026!', 'csrf_token': token,
        })
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.location.endswith('/f/'))

    def test_api_rejects_non_object_json_without_500(self):
        self.login()
        for raw in ('null', '[]', '"text"'):
            response = self.client.post('/api/create_folder', data=raw, content_type='application/json')
            self.assertEqual(response.status_code, 400)

    def test_authenticated_mutations_require_csrf_header(self):
        self.login()
        csrf = self.client.environ_base.pop('HTTP_X_CSRF_TOKEN')
        response = self.client.post('/api/create_folder', json={'name':'No CSRF'})
        self.assertEqual(response.status_code, 403)
        self.client.environ_base['HTTP_X_CSRF_TOKEN'] = csrf
        response = self.client.post('/api/create_folder', json={'name':'With CSRF'})
        self.assertEqual(response.status_code, 200)

    def test_missing_auth_fails_closed(self):
        self.login()
        os.remove(server.AUTH_FILE)
        self.assertEqual(self.anon.get('/login').status_code, 503)
        self.assertNotEqual(self.anon.post('/login', data={'username': 'admin', 'password': 'admin'}).status_code, 302)
        # Existing signed session must not retain administrator access without auth.json.
        self.assertEqual(self.client.get('/f/').status_code, 302)

    def test_upload_and_plus_sanitization(self):
        self.login()
        r = self.client.post('/upload', data={
            'count': '1',
            'file_0': (io.BytesIO(b'test'), 'report+final.txt'),
        }, content_type='multipart/form-data')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(os.path.isfile(os.path.join(server.UPLOAD_FOLDER, 'report_final.txt')))

    def test_file_url_path_is_encoded(self):
        self.login()
        response = self.client.post('/upload', data={
            'count':'1', 'file_0':(io.BytesIO(b'hash-name'), 'hash#name.txt')
        }, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 200)
        files = server.get_files_in_folder(None)
        item = next(f for f in files if f['current_filename'] == 'hash#name.txt')
        self.assertIn('%23', item['download_url'])
        download = self.anon.get('/hash%23name.txt')
        self.assertEqual(download.status_code, 200)
        download.close()

    def test_invalid_upload_count_is_400(self):
        self.login()
        r = self.client.post('/upload', data={'count': 'NaN', 'file_0': (io.BytesIO(b'x'), 'x.txt')}, content_type='multipart/form-data')
        self.assertEqual(r.status_code, 400)

    def test_folder_upload_preserves_same_basename_in_distinct_directories(self):
        self.login()
        r = self.client.post('/upload_folder', data={
            'count': '2',
            'path_0': 'A/same.txt', 'file_0': (io.BytesIO(b'a'), 'same.txt'),
            'path_1': 'B/same.txt', 'file_1': (io.BytesIO(b'b'), 'same.txt'),
        }, content_type='multipart/form-data')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['count'], 2)
        self.assertTrue(os.path.isfile(os.path.join(server.UPLOAD_FOLDER, 'A', 'same.txt')))
        self.assertTrue(os.path.isfile(os.path.join(server.UPLOAD_FOLDER, 'B', 'same.txt')))

    def test_traversal_rejected(self):
        self.login()
        r = self.client.post('/upload_folder', data={
            'count': '1', 'path_0': '../../outside.txt',
            'file_0': (io.BytesIO(b'x'), 'outside.txt'),
        }, content_type='multipart/form-data')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(os.path.exists(os.path.join(_DATA, 'outside.txt')))

    def test_head_requests_do_not_count_download_or_consume_budget(self):
        self.login()
        self.client.post('/upload', data={'count':'1','file_0':(io.BytesIO(b'head-test'),'head.txt')}, content_type='multipart/form-data')
        fid, rec = next(iter(server.load_db().items()))
        self.assertEqual(self.anon.head('/' + rec['slug']).status_code, 200)
        self.assertEqual(self.anon.head('/v/' + rec['physical_path']).status_code, 200)
        saved = server.load_db()[fid]
        self.assertEqual(saved.get('download_count', 0), 0)
        self.assertEqual(saved.get('view_count', 0), 0)
        self.assertFalse(server._download_windows)

    def test_corrupt_database_path_cannot_escape_upload_root(self):
        outside = os.path.join(_DATA, 'outside-secret.txt')
        with open(outside, 'w', encoding='utf-8') as f:
            f.write('secret')
        db = {'bad-id': {
            'access_id':'bad-id', 'current_filename':'outside-secret.txt',
            'physical_path':'../outside-secret.txt', 'original_filename':'outside-secret.txt',
            'display_name':'outside-secret.txt', 'folder_id':None, 'custom_alias':'',
            'slug':'outside-secret', 'download_count':0, 'size':6, 'mtime':'now',
            'ext':'TXT', 'is_image':False,
        }}
        server._save_json(server.DB_FILE, db)
        r = self.anon.get('/outside-secret')
        self.assertEqual(r.status_code, 404)
        self.assertNotIn(b'secret', r.data)

    def test_public_download_view_and_counter(self):
        self.login()
        self.client.post('/upload', data={'count':'1','file_0':(io.BytesIO(b'abc'),'hello.txt')}, content_type='multipart/form-data')
        db = server.load_db()
        fid, rec = next(iter(db.items()))
        response = self.anon.get('/' + rec['slug'])
        self.assertEqual(response.status_code, 200)
        response.close()
        response = self.anon.get('/v/' + rec['physical_path'])
        self.assertEqual(response.status_code, 200)
        response.close()
        self.assertEqual(server.load_db()[fid]['view_count'], 1)

    def test_public_folder_slug_and_uuid(self):
        self.login()
        result = self.client.post('/api/create_folder', json={'name':'Публичная'}).get_json()
        folder_id = result['folder_id']
        folders = server.load_folders()
        slug = folders[folder_id]['slug']
        self.assertEqual(self.anon.get('/d/' + slug).status_code, 200)
        self.assertEqual(self.anon.get('/d/' + folder_id).status_code, 200)

    def test_export_selected_zip_and_public_link(self):
        self.login()
        result = self.client.post('/api/create_folder', json={'name':'Archive'}).get_json()
        folder_id = result['folder_id']
        self.client.post('/upload', data={'count':'1','folder_id':folder_id,'file_0':(io.BytesIO(b'zip-data'),'one.txt')}, content_type='multipart/form-data')
        r = self.client.post('/api/export_zip', json={'file_ids':[], 'folder_ids':[folder_id]})
        self.assertEqual(r.status_code, 200)
        url = r.get_json()['url'].replace(server.BASE_URL, '')
        response = self.anon.get(url)
        self.assertEqual(response.status_code, 200)
        response.close()

    def test_api_key_and_budget_scope(self):
        key = 'alb_' + 'a' * 48
        key2 = 'alb_' + 'b' * 48
        import hashlib
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        key_hash2 = hashlib.sha256(key2.encode()).hexdigest()
        server._save_json(server.TOKENS_FILE, {
            key_hash:{'name':'test-key'},
            key_hash2:{'name':'other-key'},
        })
        self.login()
        self.client.post('/upload', data={'count':'1','file_0':(io.BytesIO(b'123'),'data.txt')}, content_type='multipart/form-data')
        db = server.load_db()
        slug = next(iter(db.values()))['slug']
        server.ANON_DL_BUDGET_BYTES = 4
        server.TOKEN_DL_BUDGET_BYTES = 100
        response = self.anon.get('/'+slug)
        self.assertEqual(response.status_code, 200)
        response.close()
        self.assertEqual(self.anon.get('/'+slug).status_code, 429)
        key_client = server.app.test_client()
        response = key_client.get('/'+slug, headers={'X-API-Key':key})
        self.assertEqual(response.status_code, 200)
        response.close()
        response = key_client.get('/'+slug, headers={'X-API-Key':key})
        self.assertEqual(response.status_code, 200)
        response.close()
        # A second key from the same IP has a separate budget bucket.
        other_client = server.app.test_client()
        response = other_client.get('/'+slug, headers={'X-API-Key':key2})
        self.assertEqual(response.status_code, 200)
        response.close()
        # Query-string credentials are intentionally ignored.
        query_client = server.app.test_client()
        response = query_client.get('/'+slug+'?api_key='+key)
        self.assertEqual(response.status_code, 429)
        server.ANON_DL_BUDGET_BYTES = 10 * 1024**3
        server.TOKEN_DL_BUDGET_BYTES = 100 * 1024**3

    def test_blocked_download_does_not_increment_counters(self):
        self.login()
        self.client.post('/upload', data={'count':'1','file_0':(io.BytesIO(b'1234'),'budget.txt')}, content_type='multipart/form-data')
        fid, rec = next(iter(server.load_db().items()))
        server.ANON_DL_BUDGET_BYTES = 3
        try:
            response = self.anon.get('/' + rec['slug'])
            self.assertEqual(response.status_code, 200)  # one oversized file allowed when usage is zero
            response.close()
            response = self.anon.get('/' + rec['slug'])
            self.assertEqual(response.status_code, 429)
            response.close()
            self.assertEqual(server.load_db()[fid]['download_count'], 1)
            before = server.load_db()[fid].get('view_count', 0)
            response = self.anon.get('/v/' + rec['physical_path'])
            self.assertEqual(response.status_code, 429)
            response.close()
            self.assertEqual(server.load_db()[fid].get('view_count', 0), before)
        finally:
            server.ANON_DL_BUDGET_BYTES = 10 * 1024**3

    def test_export_all_hourly_limit_and_zip(self):
        self.login()
        self.client.post('/upload', data={'count':'1','file_0':(io.BytesIO(b'zip'),'export.txt')}, content_type='multipart/form-data')
        r = self.anon.get('/export_all')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data[:2], b'PK')
        self.assertEqual(self.anon.get('/export_all').status_code, 429)


if __name__ == '__main__':
    unittest.main()
