"""Disposable Albion instance for Playwright acceptance tests.

Usage:
    python tests/e2e/start_instance.py <port> <temporary-data-dir>

The instance is seeded with one public folder, 60 small images and the test
account e2e / E2E-Password-2026!. It must only be used with a temporary data
directory and loopback port.
"""
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

PORT = int(sys.argv[1])
DATA_DIR = sys.argv[2]
REPO_ROOT = Path(__file__).resolve().parents[2]
os.environ['ALBION_DATA_DIR'] = DATA_DIR
os.environ['ALBION_BASE_URL'] = f'http://127.0.0.1:{PORT}'
os.environ['ALBION_COOKIE_SECURE'] = '0'
os.makedirs(DATA_DIR, exist_ok=True)
sys.path.insert(0, str(REPO_ROOT))

import server  # noqa: E402
from PIL import Image  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

server.app.config.update(TESTING=True)
server.limiter.enabled = False
folder_id = '11111111-1111-1111-1111-111111111111'
server._save_json(server.AUTH_FILE, {
    'username': 'e2e',
    'password_hash': generate_password_hash('E2E-Password-2026!'),
})
server._save_json(server.FOLDERS_DB_FILE, {
    folder_id: {'name': 'Public', 'parent_id': None,
                'created': '20.10.2026 12:00', 'slug': 'public'},
})
folder_path = os.path.join(server.UPLOAD_FOLDER, 'Public')
os.makedirs(folder_path, exist_ok=True)
files = {}
now = datetime(2026, 10, 20, 12, 0)
for index in range(60):
    filename = f'photo_{index:03d}.png'
    path = os.path.join(folder_path, filename)
    color = (index * 3 % 255, 90, 180)
    with Image.new('RGB', (64, 48), color) as image:
        image.save(path, 'PNG')
    file_id = str(uuid.UUID(int=index + 1))
    files[file_id] = {
        'access_id': file_id,
        'current_filename': filename,
        'physical_path': f'Public/{filename}',
        'original_filename': filename,
        'display_name': filename,
        'folder_id': folder_id,
        'custom_alias': '',
        'slug': f'photo-{index:03d}',
        'link_rotated': False,
        'download_count': index % 3,
        'view_count': 0,
        'size': os.path.getsize(path),
        'mtime': now.strftime('%d.%m.%Y %H:%M'),
        'ext': 'PNG',
        'is_image': True,
    }
server._save_json(server.DB_FILE, files)
server._save_json(server.TOKENS_FILE, {})
server._save_json(server.STATS_FILE, {'last_upload': None, 'last_export': None})
print(f'READY http://127.0.0.1:{PORT}/login folder={folder_id}', flush=True)
server.app.run(host='127.0.0.1', port=PORT, threaded=True, use_reloader=False, debug=False)
