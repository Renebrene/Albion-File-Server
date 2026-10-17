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
        if os.path.exists(server.SETTINGS_FILE):
            os.remove(server.SETTINGS_FILE)
        server.app.config['MAX_CONTENT_LENGTH'] = (
            int(server.load_quota_settings()['max_upload_gb'] * 1024 ** 3) or None)
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

    def test_default_bind_is_loopback_and_proxy_trust_is_opt_in(self):
        self.assertEqual(server.APP_HOST, '127.0.0.1')
        self.assertFalse(server.TRUST_PROXY)

    def test_security_headers_on_html_and_errors(self):
        for path in ('/login', '/unknown-security-probe'):
            response = self.anon.get(path)
            self.assertEqual(response.headers.get('X-Frame-Options'), 'DENY')
            self.assertIn("frame-ancestors 'none'", response.headers.get('Content-Security-Policy', ''))
            self.assertEqual(response.headers.get('X-Content-Type-Options'), 'nosniff')
            response.close()

    def test_static_branding_routes(self):
        for path in ('/favicon.ico', '/static/logo.png', '/robots.txt'):
            response = self.anon.get(path)
            self.assertEqual(response.status_code, 200)
            response.close()

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

    def test_default_auth_bootstrap(self):
        os.remove(server.AUTH_FILE)
        page = self.anon.get('/login')
        self.assertEqual(page.status_code, 200)
        self.assertTrue(os.path.exists(server.AUTH_FILE))
        token = re.search(rb'name="csrf_token" value="([^"]+)"', page.data).group(1).decode()
        response = self.anon.post('/login', data={
            'username': 'admin', 'password': 'альбион', 'csrf_token': token,
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith('/f/'))

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

    def test_rename_alias_move_and_stable_slug(self):
        self.login()
        upload = self.client.post('/upload', data={'count':'1','file_0':(io.BytesIO(b'rename'),'before.txt')}, content_type='multipart/form-data')
        self.assertEqual(upload.status_code, 200)
        fid, rec = next(iter(server.load_db().items()))
        original_slug = rec['slug']
        folder = self.client.post('/api/create_folder', json={'name':'Target'}).get_json()['folder_id']
        response = self.client.post('/api/update_file', json={
            'internal_id':fid,'display_name':'After','physical_filename':'after.txt','custom_alias':'after-link','folder_id':None
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.anon.get('/after-link').status_code, 200)
        response = self.client.post('/api/move_file', json={'file_id':fid,'target_folder_id':folder})
        self.assertEqual(response.status_code, 200)
        current = server.load_db()[fid]
        self.assertEqual(current['folder_id'], folder)
        self.assertTrue(current['link_rotated'])
        self.assertEqual(current['slug'], original_slug)
        self.assertEqual(self.anon.get('/' + original_slug).status_code, 200)

    def test_batch_move_and_delete(self):
        self.login()
        for name in ('one.txt','two.txt'):
            self.client.post('/upload', data={'count':'1','file_0':(io.BytesIO(name.encode()),name)}, content_type='multipart/form-data')
        db=server.load_db()
        ids=[fid for fid,rec in db.items()]
        folder=self.client.post('/api/create_folder',json={'name':'Batch'}).get_json()['folder_id']
        moved=self.client.post('/api/move_files_batch',json={'file_ids':ids,'target_folder_id':folder})
        self.assertEqual(moved.status_code,200)
        self.assertEqual(moved.get_json()['count'],2)
        self.assertTrue(all(server.load_db()[fid]['folder_id']==folder for fid in ids))
        deleted=self.client.post('/api/delete_files_batch',json={'file_ids':ids})
        self.assertEqual(deleted.status_code,200)
        self.assertEqual(deleted.get_json()['count'],2)
        self.assertFalse(server.load_db())

    def test_recursive_folder_delete(self):
        self.login()
        parent=self.client.post('/api/create_folder',json={'name':'Parent'}).get_json()['folder_id']
        child=self.client.post('/api/create_folder',json={'name':'Child','parent_id':parent}).get_json()['folder_id']
        self.client.post('/upload',data={'count':'1','folder_id':child,'file_0':(io.BytesIO(b'x'),'child.txt')},content_type='multipart/form-data')
        response=self.client.post('/api/delete_folder',json={'folder_id':parent})
        self.assertEqual(response.status_code,200)
        self.assertFalse(server.load_db())
        self.assertNotIn(parent,server.load_folders())
        self.assertNotIn(child,server.load_folders())

    def test_redis_budget_branch(self):
        class FakeRedis:
            def __init__(self):
                self.used = 0
            def eval(self, script, numkeys, key, limit, size, ttl):
                if self.used > 0 and self.used + size > limit:
                    return -1
                self.used += size
                return 1

        fake = FakeRedis()
        old_getter = server._get_redis_client
        old_budget = server.ANON_DL_BUDGET_BYTES
        server._get_redis_client = lambda: fake
        server.ANON_DL_BUDGET_BYTES = 10
        try:
            with server.app.test_request_context('/'):
                self.assertIsNone(server.check_download_budget('203.0.113.1', 5))
                self.assertIsNotNone(server.check_download_budget('203.0.113.1', 6))
        finally:
            server._get_redis_client = old_getter
            server.ANON_DL_BUDGET_BYTES = old_budget

    def test_admin_settings_update_quotas(self):
        self.assertEqual(self.anon.get('/settings').status_code, 302)
        self.login()
        page = self.client.get('/settings')
        self.assertEqual(page.status_code, 200)
        token = re.search(rb'<meta name="csrf-token" content="([^"]+)"', page.data).group(1).decode()
        response = self.client.post('/settings', data={
            'username': 'test',
            'max_upload_gb': '1.5',
            'storage_quota_gb': '200',
            'min_free_gb': '2',
            'max_zip_gb': '25',
            'max_zip_files': '500',
            'export_all_internal_only': '1',
            'csrf_token': token,
        }, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        saved = server.load_quota_settings()
        self.assertTrue(saved['export_all_internal_only'])
        self.assertEqual(saved['max_upload_gb'], 1.5)
        self.assertEqual(saved['storage_quota_gb'], 200.0)
        self.assertEqual(saved['max_zip_files'], 500)
        self.assertEqual(server.app.config['MAX_CONTENT_LENGTH'], int(1.5 * 1024 ** 3))
        invalid = self.client.post('/settings', data={
            'username': 'test',
            'max_upload_gb': '-1', 'storage_quota_gb': '0', 'min_free_gb': '0',
            'max_zip_gb': '0', 'max_zip_files': '0', 'csrf_token': token,
        })
        self.assertEqual(invalid.status_code, 200)
        self.assertIn('Проверьте значения', invalid.get_data(as_text=True))

    def test_settings_changes_admin_password(self):
        stale_client = server.app.test_client()
        page = stale_client.get('/login')
        stale_token = re.search(rb'name="csrf_token" value="([^"]+)"', page.data).group(1).decode()
        stale_client.post('/login', data={
            'username': 'test', 'password': 'Test-Password-2026!', 'csrf_token': stale_token,
        })
        self.login()
        page = self.client.get('/settings')
        token = re.search(rb'<meta name="csrf-token" content="([^"]+)"', page.data).group(1).decode()
        response = self.client.post('/settings', data={
            'username': 'newadmin',
            'new_password': 'NewPass123!',
            'new_password_confirm': 'NewPass123!',
            'max_upload_gb': '50', 'storage_quota_gb': '0', 'min_free_gb': '5',
            'max_zip_gb': '50', 'max_zip_files': '20000',
            'export_all_internal_only': '1', 'csrf_token': token,
        })
        self.assertEqual(response.status_code, 302)
        auth = server.get_auth()
        self.assertEqual(auth['username'], 'newadmin')
        # Changing credentials invalidates the session that performed the change.
        self.assertEqual(self.client.get('/f/').status_code, 302)
        new_client = server.app.test_client()
        page = new_client.get('/login')
        token = re.search(rb'name="csrf_token" value="([^"]+)"', page.data).group(1).decode()
        login = new_client.post('/login', data={
            'username': 'newadmin', 'password': 'NewPass123!', 'csrf_token': token,
        })
        self.assertEqual(login.status_code, 302)
        # A second session established before the change is rejected.
        self.assertEqual(stale_client.get('/f/').status_code, 302)

    def test_external_auth_file_change_invalidates_existing_session(self):
        self.login()
        old_auth = server.get_auth()
        server._save_json(server.AUTH_FILE, {
            'username': old_auth['username'],
            'password_hash': generate_password_hash('Changed-Externally-2026!'),
        })
        response = self.client.get('/f/')
        self.assertEqual(response.status_code, 302)
        response.close()

    def test_upload_quota_and_free_space_helpers(self):
        old_quota = server.STORAGE_QUOTA_BYTES
        old_free = server.MIN_FREE_BYTES
        try:
            server.STORAGE_QUOTA_BYTES = 1000
            server.MIN_FREE_BYTES = 0
            status, _ = server.upload_capacity_error(200, {'a': {'size': 900}})
            self.assertEqual(status, 507)
            self.assertIsNone(server.upload_capacity_error(50, {'a': {'size': 900}}))
        finally:
            server.STORAGE_QUOTA_BYTES = old_quota
            server.MIN_FREE_BYTES = old_free

    def test_upload_request_limit_returns_413(self):
        self.login()
        old_limit, old_cfg = server.MAX_UPLOAD_BYTES, server.app.config.get('MAX_CONTENT_LENGTH')
        try:
            server.MAX_UPLOAD_BYTES = 100
            server.app.config['MAX_CONTENT_LENGTH'] = 100
            response = self.client.post('/upload', data={
                'count':'1', 'file_0':(io.BytesIO(b'x' * 200), 'too-big.txt')
            }, content_type='multipart/form-data')
            self.assertEqual(response.status_code, 413)
        finally:
            server.MAX_UPLOAD_BYTES = old_limit
            server.app.config['MAX_CONTENT_LENGTH'] = old_cfg

    def test_zip_build_lock_serializes_capacity_check(self):
        for route in ('/api/export_zip', '/export_all'):
            endpoint = next(rule.endpoint for rule in server.app.url_map.iter_rules() if rule.rule == route)
            self.assertTrue(hasattr(server.app.view_functions[endpoint], '__wrapped__'))
        import threading
        import time
        first_entered = threading.Event()
        second_entered = threading.Event()
        second_attempting = threading.Event()
        release = threading.Event()

        @server.serialized_zip_build
        def first_build():
            first_entered.set()
            release.wait(2)

        @server.serialized_zip_build
        def second_build():
            second_entered.set()

        def run_second():
            first_entered.wait(2)
            second_attempting.set()
            second_build()

        first = threading.Thread(target=first_build)
        second = threading.Thread(target=run_second)
        first.start()
        second.start()
        self.assertTrue(first_entered.wait(2))
        self.assertTrue(second_attempting.wait(2))
        time.sleep(0.05)
        self.assertFalse(second_entered.is_set())
        release.set()
        first.join(2)
        second.join(2)
        self.assertTrue(second_entered.is_set())

    def test_zip_limits(self):
        import tempfile
        old_bytes, old_files = server.MAX_ZIP_BYTES, server.MAX_ZIP_FILES
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.write(b'x' * 20)
        tmp.close()
        try:
            server.MAX_ZIP_BYTES = 10
            server.MAX_ZIP_FILES = 10
            self.assertEqual(server.zip_capacity_error([(tmp.name, 'x')])[0], 413)
            server.MAX_ZIP_BYTES = 1000
            server.MAX_ZIP_FILES = 0
            self.assertEqual(server.zip_capacity_error([(tmp.name, 'x'), (tmp.name, 'y')]), None)
            server.MAX_ZIP_FILES = 1
            self.assertEqual(server.zip_capacity_error([(tmp.name, 'x'), (tmp.name, 'y')])[0], 413)
        finally:
            server.MAX_ZIP_BYTES, server.MAX_ZIP_FILES = old_bytes, old_files
            os.remove(tmp.name)

    def test_export_xlsx_response(self):
        self.login()
        self.client.post('/upload',data={'count':'1','file_0':(io.BytesIO(b'x'),'sheet.txt')},content_type='multipart/form-data')
        response=self.client.get('/export_xlsx')
        self.assertEqual(response.status_code,200)
        self.assertIn('spreadsheetml',response.mimetype)
        self.assertTrue(response.data.startswith(b'PK'))
        response.close()

    def test_public_folder_slug_and_uuid(self):
        self.login()
        result = self.client.post('/api/create_folder', json={'name':'Публичная'}).get_json()
        folder_id = result['folder_id']
        child = self.client.post('/api/create_folder', json={
            'name':'Секретная подпапка', 'parent_id': folder_id}).get_json()['folder_id']
        folders = server.load_folders()
        slug = folders[folder_id]['slug']
        child_slug = folders[child]['slug']
        page = self.anon.get('/d/' + slug)
        self.assertEqual(page.status_code, 200)
        self.assertNotIn(child_slug, page.get_data(as_text=True))
        self.assertNotIn('Секретная подпапка', page.get_data(as_text=True))
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
        # External anonymous client is denied by the default internal-only setting.
        external = self.anon.get('/export_all', environ_base={'REMOTE_ADDR': '203.0.113.5'})
        self.assertEqual(external.status_code, 403)
        proxy_localhost = self.anon.get('/export_all')
        self.assertEqual(proxy_localhost.status_code, 403)
        proxy_localhost.close()
        spoofed_lan = self.anon.get('/export_all',
                                    headers={'X-Forwarded-For': '10.0.0.8'},
                                    environ_base={'REMOTE_ADDR': '203.0.113.5'})
        self.assertEqual(spoofed_lan.status_code, 403)
        spoofed_lan.close()
        self.login()
        self.client.post('/upload', data={'count':'1','file_0':(io.BytesIO(b'zip'),'export.txt')}, content_type='multipart/form-data')
        r = self.anon.get('/export_all', environ_base={'REMOTE_ADDR': '192.168.1.20'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data[:2], b'PK')
        r.close()
        limited = self.anon.get('/export_all', environ_base={'REMOTE_ADDR': '192.168.1.20'})
        self.assertEqual(limited.status_code, 429)
        limited.close()

    def test_export_all_rejects_spoofed_private_forwarded_ip(self):
        original_wsgi = server.app.wsgi_app
        original_trust = server.TRUST_PROXY
        original_peers = server.TRUSTED_PROXY_IPS
        try:
            server.TRUST_PROXY = True
            server.TRUSTED_PROXY_IPS = ('127.0.0.1',)
            server.app.wsgi_app = server.trusted_proxy_middleware(original_wsgi)
            client = server.app.test_client()
            client.environ_base['REMOTE_ADDR'] = '203.0.113.5'
            denied = client.get('/export_all', headers={'X-Forwarded-For': '10.0.0.7'})
            self.assertEqual(denied.status_code, 403)
            denied.close()

            # A request genuinely received from the trusted local proxy may use the LAN allowance.
            client.environ_base['REMOTE_ADDR'] = '127.0.0.1'
            server._save_json(server.DB_FILE, {})
            local = client.get('/export_all', headers={'X-Forwarded-For': '10.0.0.7'})
            self.assertEqual(local.status_code, 404)  # authorized; no files in fixture
            local.close()
        finally:
            server.app.wsgi_app = original_wsgi
            server.TRUST_PROXY = original_trust
            server.TRUSTED_PROXY_IPS = original_peers


if __name__ == '__main__':
    unittest.main()
