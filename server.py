import os
import io
import json
from functools import wraps
import uuid
import re
import shutil
import urllib.parse
from datetime import datetime
from flask import Flask, request, send_from_directory, render_template_string, send_file, jsonify, abort, redirect, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from waitress import serve
from flask import session, redirect, url_for, request
from werkzeug.security import check_password_hash, generate_password_hash
import secrets
import threading
import logging
import hashlib
import time
import zipfile
from collections import deque
from logging.handlers import RotatingFileHandler
from flask import Flask, request, send_from_directory, render_template_string, send_file, jsonify, abort, redirect, url_for, g
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# Каталог данных (uploads + json-базы): '.' по умолчанию, в Docker — /app/data
DATA_DIR = os.environ.get('ALBION_DATA_DIR', '.')

app = Flask(__name__, static_folder='static')

# ===== СЕКРЕТНЫЙ КЛЮЧ (сохраняется между перезапусками, сессии не слетают) =====
SECRET_KEY_FILE = os.path.join(DATA_DIR, '.secret_key')

def _get_or_create_secret_key():
    if os.path.exists(SECRET_KEY_FILE):
        try:
            with open(SECRET_KEY_FILE, 'r', encoding='utf-8') as f:
                key = f.read().strip()
                if key:
                    return key
        except OSError:
            pass
    key = secrets.token_hex(32)
    with open(SECRET_KEY_FILE, 'w', encoding='utf-8') as f:
        f.write(key)
    return key

app.secret_key = _get_or_create_secret_key()
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# ===== ЛОГИРОВАНИЕ (файл + консоль/journal) =====
LOG_FILE = os.path.join(DATA_DIR, 'albion.log')

_log_file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024,
                                        backupCount=3, encoding='utf-8')
_log_file_handler.setFormatter(logging.Formatter(
    '%(asctime)s %(levelname)s [%(threadName)s] %(message)s'))
_log_console_handler = logging.StreamHandler()
_log_console_handler.setFormatter(logging.Formatter(
    '%(asctime)s %(levelname)s [%(threadName)s] %(message)s'))

app.logger.setLevel(logging.INFO)
app.logger.addHandler(_log_file_handler)
app.logger.addHandler(_log_console_handler)

# ===== БЛОКИРОВКА JSON-БД (защита от гонок при многопоточности waitress) =====
DB_LOCK = threading.Lock()
# Сериализует read-modify-write всей files_db в upload-эндпоинтах:
# параллельные батчи иначе затирают записи друг друга (last writer wins)
UPLOAD_LOCK = threading.Lock()

# ===== RATE LIMITING =====
# Локальные адреса (LAN/localhost) не лимитируем — лимит нужен против роботов из интернета.
# Если key_func вернёт None, flask-limiter пропускает запрос без учёта.
_PRIVATE_RE = re.compile(r'^(127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|::1|localhost)')

def _rate_limit_key():
    addr = get_remote_address()
    return None if (not addr or _PRIVATE_RE.match(addr)) else addr

limiter = Limiter(
    key_func=_rate_limit_key,
    app=app,
    default_limits=["100 per hour", "20 per minute"],
    storage_uri="memory://"
)

# ===== ОБРАБОТЧИКИ ОШИБОК =====
@app.errorhandler(413)
def request_too_large(e):
    size = request.content_length or 0
    app.logger.error('Слишком большой запрос на %s: %s от %s (частей формы: лимит %s)',
                     request.path, format_size(size), request.remote_addr,
                     app.config.get('MAX_FORM_PARTS'))
    return jsonify({'error': f'Запрос слишком большой ({format_size(size)}). Проверьте размер и число файлов в папке'}), 413

@app.errorhandler(500)
def internal_error(e):
    app.logger.error('Внутренняя ошибка на %s от %s', request.path, request.remote_addr, exc_info=True)
    return jsonify({'error': 'Внутренняя ошибка сервера — подробности в albion.log'}), 500

# ===== КОНФИГУРАЦИЯ =====
BASE_URL = "https://albion-rostov.cloudpub.ru"
UPLOAD_FOLDER = os.path.join(DATA_DIR, 'uploads')
STATS_FILE = os.path.join(DATA_DIR, 'stats.json')
DB_FILE = os.path.join(DATA_DIR, 'files_db.json')
FOLDERS_DB_FILE = os.path.join(DATA_DIR, 'folders_db.json')
TOKENS_FILE = os.path.join(DATA_DIR, 'tokens.json')
ZIPLINKS_DIR = os.path.join(DATA_DIR, 'ziplinks')

ALLOWED_EXTENSIONS = {'txt', 'pdf', 'png', 'jpg', 'jpeg', 'gif', 'zip', 'rar',
                      'docx', 'xlsx', 'mp4', 'mkv', 'doc', 'xls', 'pptx', '7z'}
IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp'}

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
# Лимит размера загрузки снят (по запросу владельца) — контроль места в статус-баре и albion.log
app.config['MAX_CONTENT_LENGTH'] = None
# Werkzeug по умолчанию допускает всего 1000 частей формы (~500 файлов за раз).
# Каждый файл в загрузке папки = 2 части (file_N + path_N) — поднимаем лимит.
app.config['MAX_FORM_PARTS'] = 50000

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# ===== ПРОВЕРКА ИМЕНИ =====
# Имя файла: белый список (расширен реальным миром: запятые, '!', '+', '&', '#', '@', ';', '=', '№')
def is_valid_filename(filename):
    if not filename or len(filename) > 255:
        return False
    pattern = r'^[\wа-яА-ЯёЁ\s\-\.\(\),!+&#@;=№]+$'
    return bool(re.match(pattern, filename, re.UNICODE))

# ===== КОРОТКИЕ ССЫЛКИ (латинизированный слаг из имени файла) =====
# Путь, занятый системой: короткая ссылка не может его замаскировать
RESERVED_PATHS = {'login', 'logout', 'export_xlsx', 'export_all', 'upload', 'upload_folder',
                  'robots.txt', 'favicon.ico', 'v', 'f', 'd', 'z', 'static', 'api'}

# ===== БЮДЖЕТ СКАЧИВАНИЯ (защита от массового выкачивания из интернета) =====
# Аноним: ГБ в скользящем окне часа; с API-ключом: ГБ в сутки (настраивается env)
ANON_DL_BUDGET_BYTES = int(os.environ.get('ALBION_DL_BUDGET_GB', '10')) * 1024 ** 3
TOKEN_DL_BUDGET_BYTES = int(os.environ.get('ALBION_TOKEN_DL_BUDGET_GB', '100')) * 1024 ** 3
ANON_DL_WINDOW = 3600          # 1 час
TOKEN_DL_WINDOW = 86400        # сутки
EXPORT_ALL_INTERVAL = 3600     # массовое скачивание всего — не чаще раза в час

# Русская латинизация для слагов
TRANSLIT_MAP = str.maketrans({
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo', 'ж': 'zh',
    'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n', 'о': 'o',
    'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u', 'ф': 'f', 'х': 'h', 'ц': 'ts',
    'ч': 'ch', 'ш': 'sh', 'щ': 'shch', 'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e',
    'ю': 'yu', 'я': 'ya',
})

def unique_slug(base, db=None, exclude_id=None, folders=None):
    """Обеспечивает уникальность слага среди slug/алиасов файлов И слагов папок."""
    if db is None:
        db = load_db()
    taken = {rec.get('slug') for rec in db.values() if rec.get('slug')}
    taken |= {rec.get('custom_alias') for rec in db.values() if rec.get('custom_alias')}
    if folders is None:
        folders = load_folders()
    taken |= {f.get('slug') for f in folders.values() if f.get('slug')}
    if exclude_id and exclude_id in db:
        taken.discard(db[exclude_id].get('slug'))
        taken.discard(db[exclude_id].get('custom_alias'))
    if base not in taken:
        return base
    for i in range(2, 100):
        cand = f"{base}-{i}"
        if cand not in taken:
            return cand
    return f"{base}-{secrets.token_hex(4)}"

def make_short_slug(name, db=None, exclude_id=None, folders=None):
    """Короткая ссылка из имени: 'Ростов-на-Дону-Театральная пл. 2, РОЛЛЕР-B(4).jpg'
    -> 'rostov-donu-teatra2-roller4'. Слова <=2 символов без цифр выбрасываются,
    цифры приклеиваются к предыдущему слову, капа 48 символов."""
    # Отрезаем только похоже на расширение хвост (jpg, docx...), но не «пл.» из адреса
    text = name
    dot_parts = name.rsplit('.', 1)
    if len(dot_parts) == 2 and dot_parts[1].isalnum() and len(dot_parts[1]) <= 5:
        text = dot_parts[0]
    tokens = re.split(r'[^a-z0-9]+', text.lower().translate(TRANSLIT_MAP))
    filtered = [t for t in tokens if len(t) > 2 or any(c.isdigit() for c in t)]
    merged = []
    for t in filtered:
        if t.isdigit() and merged:
            merged[-1] = (merged[-1] + t)[:8]
        else:
            merged.append(t[:6])
    parts = []
    total = 0
    for t in merged:
        if parts and total + len(t) + 1 > 48:
            break
        parts.append(t)
        total += len(t) + 1
    base = '-'.join(parts) if parts else 'f-' + secrets.token_hex(4)
    if base in RESERVED_PATHS:
        base += '-f'
    return unique_slug(base, db, exclude_id, folders)

# Имя папки: чёрный список — как в Windows (запрещены \ / : * ? " < > |, точки/пробелы по краям).
# Так папки вида "Фото 2024, лето" или "Иванов & Ко" загружаются без ошибок.
def is_valid_foldername(name):
    if not name or len(name) > 100:
        return False
    if name.strip() in ('.', '..'):
        return False
    if re.search(r'[\\/:*?"<>|\x00-\x1f]', name):
        return False
    if name != name.strip(' .'):
        return False
    return True

# ===== БАЗА ДАННЫХ ФАЙЛОВ =====
def _load_json(path):
    """Загрузка JSON. Повреждённый файл переносится в .corrupt-бэкап,
    чтобы пустая база не затёрла данные при следующем сохранении."""
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError, ValueError) as e:
            backup = f"{path}.corrupt-{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            try:
                os.replace(path, backup)
                print(f"⚠️ {path} повреждён ({e}). Сохранён как {backup}.")
            except OSError:
                print(f"⚠️ {path} повреждён ({e}), бэкап создать не удалось.")
    return {}

def _save_json(path, data):
    """Атомарная запись: сначала во временный файл, затем замена.
    При сбое питания/процесса старый файл не теряется."""
    tmp = f"{path}.tmp"
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def load_db():
    return _load_json(DB_FILE)

def save_db(db):
    with DB_LOCK:
        _save_json(DB_FILE, db)

# ===== БАЗА ДАННЫХ ПАПОК =====
def load_folders():
    return _load_json(FOLDERS_DB_FILE)

def save_folders(folders):
    with DB_LOCK:
        _save_json(FOLDERS_DB_FILE, folders)

def get_folder_path(folder_id, folders):
    if not folder_id or folder_id not in folders:
        return []
    path = []
    current = folder_id
    while current and current in folders:
        path.insert(0, folders[current]['name'])
        current = folders[current].get('parent_id')
    return path

def get_folder_physical_path(folder_id, folders):
    path_parts = get_folder_path(folder_id, folders)
    return os.path.join(UPLOAD_FOLDER, *path_parts) if path_parts else UPLOAD_FOLDER

def get_subfolders(parent_id, folders):
    return [f for fid, f in folders.items() if f.get('parent_id') == parent_id]

def ensure_folder_path(folder_parts, parent_id):
    """Находит или создаёт цепочку вложенных папок. Потокобезопасно:
    под блокировкой перечитывает folders_db, поэтому параллельные батчи
    не создадут дубли одной и той же папки.
    Возвращает (folder_id, физический_путь, список_созданных_id)."""
    with DB_LOCK:
        folders = _load_json(FOLDERS_DB_FILE)
        current_parent = parent_id
        current_physical = get_folder_physical_path(parent_id, folders)
        created_ids = []
        for folder_name in folder_parts:
            found = None
            for fid, f in folders.items():
                if f.get('parent_id') == current_parent and f['name'] == folder_name:
                    found = fid
                    break
            if not found:
                found = str(uuid.uuid4())
                folders[found] = {
                    'name': folder_name,
                    'parent_id': current_parent,
                    'created': datetime.now().strftime('%d.%m.%Y %H:%M')
                }
                folders[found]['slug'] = make_short_slug(folder_name, folders=folders)
                created_ids.append(found)
            current_parent = found
            current_physical = os.path.join(current_physical, folder_name)
        if created_ids:
            _save_json(FOLDERS_DB_FILE, folders)
        return current_parent, current_physical, created_ids

# ===== МИГРАЦИЯ =====
def get_file_ext(filename):
    return filename.rsplit('.', 1)[1].lower() if '.' in filename else ''

def migrate_files():
    db = load_db()
    folders = load_folders()
    changed = False
    
    for internal_id, record in db.items():
        if 'folder_id' not in record:
            record['folder_id'] = None
            record['physical_path'] = record.get('current_filename', '')
            changed = True
        if 'download_count' not in record:
            record['download_count'] = 0
            changed = True
        if not record.get('slug'):
            record['slug'] = make_short_slug(
                record.get('display_name') or record.get('original_filename') or record.get('current_filename', ''),
                db, exclude_id=internal_id)
            changed = True
    
    for filename in os.listdir(UPLOAD_FOLDER):
        if filename.startswith('.'):  # служебные/временные файлы (.export_all-*.zip и пр.)
            continue
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        if os.path.isfile(filepath):
            if not any(r.get('physical_path') == filename for r in db.values()):
                internal_id = str(uuid.uuid4())
                ext = get_file_ext(filename)
                db[internal_id] = {
                    'access_id': internal_id,
                    'current_filename': filename,
                    'physical_path': filename,
                    'original_filename': filename,
                    'display_name': filename,
                    'folder_id': None,
                    'custom_alias': '',
                    'link_rotated': False,
                    'download_count': 0,
                    'size': os.path.getsize(filepath),
                    'mtime': datetime.fromtimestamp(os.path.getmtime(filepath)).strftime('%d.%m.%Y %H:%M'),
                    'ext': ext.upper() if ext else '—',
                    'is_image': ext in IMAGE_EXTENSIONS
                }
                changed = True
    
    if changed:
        save_db(db)
    return db

migrate_files()

def migrate_folders():
    """Добавляет короткие слаги папкам, у которых их ещё нет."""
    folders = load_folders()
    if not folders:
        return
    changed = False
    db = load_db()
    for f in folders.values():
        if not f.get('slug'):
            f['slug'] = make_short_slug(f['name'], db, folders=folders)
            changed = True
    if changed:
        save_folders(folders)

migrate_folders()

# ===== СТАТИСТИКА =====
def load_stats():
    stats = _load_json(STATS_FILE)
    if not isinstance(stats, dict):
        return {'last_upload': None, 'last_export': None}
    stats.setdefault('last_upload', None)
    stats.setdefault('last_export', None)
    return stats

def save_stats(stats):
    with DB_LOCK:
        _save_json(STATS_FILE, stats)

def bump_counter(match_fn, counter_field):
    """Атомарно увеличивает счётчик у записи БД (под блокировкой).
    Возвращает запись или None, если совпадений нет."""
    with DB_LOCK:
        db = _load_json(DB_FILE)
        target = None
        for record in db.values():
            if match_fn(record):
                target = record
                break
        if target is None:
            return None
        target[counter_field] = target.get(counter_field, 0) + 1
        _save_json(DB_FILE, db)
        return target

def update_last_upload():
    stats = load_stats()
    stats['last_upload'] = datetime.now().strftime('%d.%m.%Y %H:%M:%S')
    save_stats(stats)

def update_last_export():
    stats = load_stats()
    stats['last_export'] = datetime.now().strftime('%d.%m.%Y %H:%M:%S')
    save_stats(stats)

# ===== ВСПОМОГАТЕЛЬНЫЕ =====
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_file_icon(ext):
    icons = {
        'pdf': '📕', 'doc': '📘', 'docx': '📘', 'xls': '📗', 'xlsx': '📗',
        'ppt': '📙', 'pptx': '📙', 'txt': '📄', 'zip': '🗜️', 'rar': '🗜️', '7z': '🗜️',
        'mp3': '🎵', 'wav': '🎵', 'mp4': '🎬', 'mkv': '🎬', 'avi': '🎬',
        'py': '🐍', 'js': '📜', 'html': '🌐', 'css': '🎨'
    }
    return icons.get(ext.lower(), '📄')

def format_size(size):
    if size < 1024: return f"{size} Б"
    if size < 1024 * 1024: return f"{size / 1024:.1f} КБ"
    if size < 1024 * 1024 * 1024: return f"{size / (1024 * 1024):.1f} МБ"
    return f"{size / (1024 * 1024 * 1024):.2f} ГБ"

def get_disk_info():
    usage = shutil.disk_usage(UPLOAD_FOLDER)
    return {
        'total': format_size(usage.total),
        'used': format_size(usage.used),
        'free': format_size(usage.free),
        'percent': round(usage.used / usage.total * 100, 1)
    }

def get_files_in_folder(folder_id):
    db = load_db()
    files = []
    for internal_id, record in db.items():
        if record.get('folder_id') == folder_id:
            physical_path = record.get('physical_path', record['current_filename'])
            filepath = os.path.join(UPLOAD_FOLDER, physical_path)
            if os.path.exists(filepath):
                files.append({
                    'internal_id': internal_id,
                    'name': record['display_name'],
                    'current_filename': record['current_filename'],
                    'physical_path': physical_path,
                    'ext': record['ext'],
                    'size': format_size(record['size']),
                    'size_raw': record['size'],
                    'mtime': record['mtime'],
                    'is_image': record.get('is_image', False),
                    'icon': get_file_icon(record['ext']),
                    'custom_alias': record['custom_alias'],
                    'link_rotated': record.get('link_rotated', False),
                    'download_count': record.get('download_count', 0),
                    'view_count': record.get('view_count', 0),
                    'folder_id': folder_id,
                    # Ссылки с нормальной кириллицей (без URL-кодирования)
                    'download_url': f"{BASE_URL}/{physical_path}",
                    'view_url': f"{BASE_URL}/v/{physical_path}",
                    # Короткая латинизированная ссылка (переживает переименование)
                    'short_url': f"{BASE_URL}/{record['slug']}" if record.get('slug') else f"{BASE_URL}/{physical_path}",
                })
    return sorted(files, key=lambda x: x['mtime'], reverse=True)

def get_all_files():
    db = load_db()
    total_size = 0
    total_downloads = 0
    for record in db.values():
        total_downloads += record.get('download_count', 0)
        total_size += record.get('size', 0)
    return {
        'total_size': format_size(total_size),
        'total_downloads': total_downloads
    }

def get_folder_stats_map(folders, db):
    """Рекурсивная статистика по каждой папке: (файлов, суммарный размер, подпапок).
    Один проход по БД + мемоизация — дёшево даже на тысячах папок."""
    files_agg = {}   # folder_id -> [файлов, байт]
    children = {}    # parent_id -> [folder_id, ...]
    for rec in db.values():
        fid = rec.get('folder_id')
        if fid in folders:
            agg = files_agg.setdefault(fid, [0, 0])
            agg[0] += 1
            agg[1] += rec.get('size', 0)
    for fid, f in folders.items():
        children.setdefault(f.get('parent_id'), []).append(fid)

    memo = {}
    def stats(fid):
        if fid in memo:
            return memo[fid]
        count, size = files_agg.get(fid, (0, 0))
        subdirs = 0
        for ch in children.get(fid, []):
            c, s, sd = stats(ch)
            count += c
            size += s
            subdirs += 1 + sd
        memo[fid] = (count, size, subdirs)
        return memo[fid]

    for fid in folders:
        stats(fid)
    return memo

# ===== HTML ШАБЛОН =====
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <title>АЛЬБИОН: Файлы</title>
    <link rel="icon" type="image/png" href="{{ url_for('static', filename='logo.png') }}">
    <style>
        * { box-sizing: border-box; }
        html, body { height: 100%; margin: 0; padding: 0; }
        body { font-family: 'Segoe UI', Arial, sans-serif; background: #f4f4f9; display: flex; flex-direction: column; overflow: hidden; }

        .header { background: linear-gradient(135deg, #1e3a8a 0%, #2563eb 100%); color: white; padding: 15px 30px; display: flex; align-items: center; gap: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.15); flex-shrink: 0; }
        .header-logo { height: 50px; width: auto; background: white; padding: 5px 8px; border-radius: 6px; }
        .header-title { font-size: 24px; font-weight: 700; margin: 0; }
        .header-subtitle { font-size: 13px; opacity: 0.85; margin-top: 2px; }

        .main { flex: 1; display: flex; flex-direction: column; overflow: hidden; padding: 15px 25px; gap: 12px; }
        .card { background: white; padding: 15px 20px; border-radius: 8px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); flex-shrink: 0; }
        .card h2 { color: #1e3a8a; margin: 0 0 10px 0; font-size: 16px; display: flex; align-items: center; gap: 8px; }

        .top-row { display: flex; gap: 12px; flex-shrink: 0; }
        .top-row .card { flex: 1; }
        .top-row .card-upload { flex: 2; }

        .drop-zone { border: 2px dashed #2563eb; border-radius: 8px; padding: 15px; text-align: center; cursor: pointer; transition: all 0.2s; background: #f8f9ff; }
        .drop-zone:hover, .drop-zone.dragover { background: #e7f0ff; border-color: #1e3a8a; }
        .drop-zone p { margin: 3px 0; color: #555; font-size: 13px; }

        .upload-buttons { display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap; }

        .file-list { margin-top: 10px; max-height: 120px; overflow-y: auto; }
        .file-item { display: flex; justify-content: space-between; align-items: center; padding: 5px 10px; background: #f8f9fa; border-radius: 4px; margin-bottom: 3px; font-size: 13px; }
        .file-item .name { flex: 1; word-break: break-all; }
        .file-item .size { color: #888; margin: 0 10px; white-space: nowrap; }
        .file-item .remove { background: #dc3545; color: white; border: none; border-radius: 3px; padding: 2px 8px; cursor: pointer; font-size: 11px; }

        .progress-container { margin-top: 10px; display: none; }
        .progress-bar { width: 100%; height: 18px; background: #e9ecef; border-radius: 9px; overflow: hidden; }
        .progress-fill { height: 100%; background: linear-gradient(90deg, #28a745, #20c997); width: 0%; transition: width 0.3s; text-align: center; color: white; font-size: 11px; line-height: 18px; font-weight: 600; }

        button, .btn { background: #2563eb; color: white; padding: 8px 16px; border: none; border-radius: 5px; cursor: pointer; text-decoration: none; display: inline-block; font-size: 13px; font-weight: 500; margin-top: 8px; transition: all 0.15s; }
        button:hover, .btn:hover { background: #1e3a8a; }
        button:disabled { background: #ccc; cursor: not-allowed; }
        .btn-success { background: #28a745; }
        .btn-success:hover { background: #1e7e34; }
        .btn-danger { background: #dc3545; }
        .btn-danger:hover { background: #b02a37; }
        .btn-secondary { background: #f1f5f9; color: #475569; }
        .btn-secondary:hover { background: #e2e8f0; }
        .btn-sm { padding: 4px 10px; font-size: 12px; margin: 0 2px; }
        .btn-icon { background: transparent; color: #64748b; padding: 4px 8px; font-size: 16px; }
        .btn-icon:hover { background: #f1f5f9; color: #1e3a8a; }
        .btn-icon-danger:hover { background: #fef2f2; color: #dc3545; }

        .card-files { flex: 1; display: flex; flex-direction: column; overflow: hidden; padding: 15px 20px; min-height: 0; }
        
        .breadcrumbs { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; font-size: 14px; flex-wrap: wrap; }
        .breadcrumbs a { color: #2563eb; text-decoration: none; }
        .breadcrumbs a:hover { text-decoration: underline; }
        .breadcrumbs .separator { color: #94a3b8; }
        .breadcrumbs .current { color: #1e293b; font-weight: 600; }

        .batch-actions { display: none; align-items: center; gap: 10px; padding: 10px 15px; background: #eff6ff; border-radius: 6px; margin-bottom: 12px; }
        .batch-actions.active { display: flex; }
        .batch-actions .count { font-weight: 600; color: #1e3a8a; }

        .view-switcher { display: flex; gap: 5px; margin-bottom: 12px; padding: 4px; background: #f1f5f9; border-radius: 6px; width: fit-content; flex-shrink: 0; }
        .view-btn { background: transparent; color: #475569; padding: 6px 14px; border: none; border-radius: 4px; cursor: pointer; font-size: 13px; font-weight: 500; margin: 0; transition: all 0.15s; }
        .view-btn:hover { background: #e2e8f0; }
        .view-btn.active { background: white; color: #1e3a8a; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }

        .files-content { flex: 1; overflow-y: auto; overflow-x: hidden; min-height: 0; }

        .files-table { width: 100%; border-collapse: collapse; font-size: 13px; }
        .files-table thead { position: sticky; top: 0; background: #f8fafc; z-index: 1; }
        .files-table th { text-align: left; padding: 8px 12px; color: #1e3a8a; font-weight: 600; border-bottom: 2px solid #e2e8f0; font-size: 12px; text-transform: uppercase; letter-spacing: 0.3px; }
        .files-table td { padding: 6px 12px; border-bottom: 1px solid #f1f5f9; vertical-align: middle; }
        .files-table tr:hover { background: #f8fafc; }
        .files-table .checkbox-cell { width: 40px; text-align: center; }
        .files-table .icon-cell { width: 40px; text-align: center; }
        .files-table .actions-cell { width: 160px; text-align: right; white-space: nowrap; }
        .thumb-sm { width: 32px; height: 32px; object-fit: cover; border-radius: 3px; vertical-align: middle; }
        .emoji-icon { font-size: 22px; }
        .folder-icon { font-size: 24px; }
        .files-table a { color: #2563eb; text-decoration: none; }
        .files-table a:hover { text-decoration: underline; }
        .files-table .size-col, .files-table .date-col, .files-table .type-col, .files-table .dl-col { color: #64748b; white-space: nowrap; }
        .dl-badge { display: inline-flex; align-items: center; gap: 3px; background: #eff6ff; color: #2563eb; padding: 2px 8px; border-radius: 10px; font-size: 12px; font-weight: 600; }
        .rotated-badge { display: inline-flex; align-items: center; gap: 4px; background: #fef3c7; color: #92400e; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: 600; margin-left: 8px; cursor: help; }
        .folder-row { background: #fefce8 !important; }
        .folder-row:hover { background: #fef3c7 !important; }
        .folder-row.drag-over { background: #dbeafe !important; border: 2px dashed #2563eb; }
        .file-row.dragging { opacity: 0.5; }
        .file-row.selected { background: #dbeafe !important; }

        .files-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 12px; padding: 4px; }
        .tile { background: white; border: 1px solid #e2e8f0; border-radius: 8px; overflow: hidden; transition: all 0.15s; display: flex; flex-direction: column; position: relative; }
        .tile:hover { border-color: #2563eb; box-shadow: 0 4px 12px rgba(37, 99, 235, 0.15); transform: translateY(-2px); }
        .tile.selected { border-color: #2563eb; background: #dbeafe; }
        .tile.drag-over { border: 2px dashed #2563eb; background: #dbeafe; }
        .tile.dragging { opacity: 0.5; }
        .tile-checkbox { position: absolute; top: 5px; left: 5px; z-index: 3; }
        .tile-actions { position: absolute; top: 5px; right: 5px; display: flex; gap: 4px; opacity: 0; transition: opacity 0.2s; z-index: 2; }
        .tile:hover .tile-actions { opacity: 1; }
        .tile-actions button { background: rgba(255,255,255,0.95); border: none; border-radius: 4px; width: 28px; height: 28px; cursor: pointer; font-size: 14px; display: flex; align-items: center; justify-content: center; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
        .tile-actions button:hover { background: white; }
        .tile-actions button.del-btn:hover { color: #dc3545; }
        
        .tile-icon-wrap { width: 100%; aspect-ratio: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; cursor: pointer; position: relative; background: linear-gradient(135deg, #f1f5f9, #e2e8f0); }
        .tile-preview-wrap { width: 100%; display: flex; align-items: center; justify-content: center; cursor: pointer; position: relative; background: #f8fafc; overflow: hidden; min-height: 150px; max-height: 300px; }
        .tile-preview { width: 100%; height: auto; max-height: 300px; object-fit: contain; display: block; }
        .tile-folder { width: 100%; aspect-ratio: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; cursor: pointer; background: #fefce8; transition: background 0.15s; }
        .tile-folder:hover { background: #fef3c7; }
        .tile-folder-icon { font-size: 64px; }
        .tile-folder-name { font-size: 12px; font-weight: 600; color: #92400e; margin-top: 8px; text-align: center; padding: 0 8px; word-break: break-word; }

        .tile-icon { font-size: 48px; }
        .tile-ext { margin-top: 4px; font-size: 11px; font-weight: 700; color: #1e3a8a; background: white; padding: 2px 8px; border-radius: 3px; }
        .tile-name { padding: 8px; font-size: 12px; text-align: center; word-break: break-word; border-top: 1px solid #f1f5f9; background: #fafbfc; display: flex; flex-direction: column; align-items: center; gap: 4px; }
        .tile-name a { color: #1e293b; text-decoration: none; display: block; overflow: hidden; text-overflow: ellipsis; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; width: 100%; }
        .tile-name a:hover { color: #2563eb; }
        .tile-name.row-actions { flex-direction: row; justify-content: space-between; align-items: center; padding: 6px 10px; }
        .tile-dl { font-size: 10px; color: #2563eb; font-weight: 600; }

        .files-list { display: flex; flex-direction: column; }
        .list-item { display: flex; align-items: center; padding: 6px 12px; border-bottom: 1px solid #f1f5f9; font-size: 13px; gap: 12px; transition: background 0.1s; }
        .list-item:hover { background: #f8fafc; }
        .list-item.selected { background: #dbeafe; }
        .list-item.folder-item { background: #fefce8; }
        .list-item.folder-item:hover { background: #fef3c7; }
        .list-item.folder-item.drag-over { background: #dbeafe; border: 2px dashed #2563eb; }
        .list-item.dragging { opacity: 0.5; }
        .thumb-xs { width: 24px; height: 24px; object-fit: cover; border-radius: 3px; flex-shrink: 0; }
        .emoji-icon-sm { font-size: 18px; flex-shrink: 0; width: 24px; text-align: center; }
        .list-name-wrap { flex: 1; display: flex; align-items: center; gap: 8px; min-width: 0; }
        .list-name { color: #1e293b; text-decoration: none; word-break: break-all; }
        .list-name:hover { color: #2563eb; }
        .list-size { color: #64748b; white-space: nowrap; font-size: 12px; }
        .list-date { color: #94a3b8; white-space: nowrap; font-size: 12px; }
        .list-actions { display: flex; gap: 4px; opacity: 0; transition: opacity 0.2s; }
        .list-item:hover .list-actions { opacity: 1; }

        .status-bar { background: #1e293b; color: #cbd5e1; padding: 0 25px; height: 38px; display: flex; align-items: center; gap: 20px; font-size: 12px; flex-shrink: 0; border-top: 1px solid #0f172a; overflow-x: auto; }
        .status-item { display: flex; align-items: center; gap: 6px; white-space: nowrap; }
        .status-item .label { color: #64748b; }
        .status-item .value { color: #f1f5f9; font-weight: 500; }
        .status-bar .separator { color: #475569; }
        .disk-bar { width: 80px; height: 8px; background: #475569; border-radius: 4px; overflow: hidden; }
        .disk-bar-fill { height: 100%; border-radius: 4px; transition: width 0.3s; }

        .toast { position: fixed; top: 80px; right: 20px; padding: 12px 20px; background: #28a745; color: white; border-radius: 6px; box-shadow: 0 4px 12px rgba(0,0,0,0.2); opacity: 0; transition: opacity 0.3s; z-index: 1000; font-size: 13px; font-weight: 500; }
        .toast.show { opacity: 1; }
        .toast.error { background: #dc3545; }

        .empty-state { text-align: center; padding: 40px 20px; color: #94a3b8; font-size: 14px; }
        .empty-state .icon { font-size: 48px; margin-bottom: 10px; }

        .files-content::-webkit-scrollbar { width: 8px; }
        .files-content::-webkit-scrollbar-track { background: #f1f5f9; }
        .files-content::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 4px; }
        .files-content::-webkit-scrollbar-thumb:hover { background: #94a3b8; }

        .modal-overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5); display: none; align-items: center; justify-content: center; z-index: 2000; }
        .modal-overlay.active { display: flex; }
        .modal { background: white; border-radius: 10px; padding: 25px; width: 90%; max-width: 500px; box-shadow: 0 10px 25px rgba(0,0,0,0.2); max-height: 90vh; overflow-y: auto; }
        .modal h3 { margin: 0 0 20px 0; color: #1e3a8a; font-size: 18px; }
        .form-group { margin-bottom: 15px; }
        .form-group label { display: block; font-size: 13px; color: #475569; margin-bottom: 5px; font-weight: 500; }
        .form-group input, .form-group select { width: 100%; padding: 8px 12px; border: 1px solid #cbd5e1; border-radius: 5px; font-size: 14px; outline: none; transition: border 0.2s; }
        .form-group input:focus, .form-group select:focus { border-color: #2563eb; }
        .form-group input[readonly] { background: #f8fafc; color: #64748b; cursor: not-allowed; }
        .form-group .hint { font-size: 12px; color: #64748b; margin-top: 4px; }
        .form-group .hint a { color: #2563eb; word-break: break-all; }
        .form-group .warning { font-size: 12px; color: #dc2626; margin-top: 4px; font-weight: 500; display: flex; align-items: center; gap: 4px; }
        .modal-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 20px; }

        .confirm-text { font-size: 14px; color: #334155; line-height: 1.6; }
        .confirm-filename { font-weight: 700; color: #dc3545; }

        input[type="checkbox"] { width: 18px; height: 18px; cursor: pointer; }

        @media (max-width: 700px) {
            .top-row { flex-direction: column; }
            .header { padding: 12px 15px; }
            .header-title { font-size: 18px; }
            .main { padding: 10px; }
            .status-bar { font-size: 11px; gap: 10px; padding: 6px 15px; height: auto; min-height: 38px; flex-wrap: wrap; }
        }
    </style>
</head>
<body>
    <header class="header">
        <img src="{{ url_for('static', filename='logo.png') }}" alt="АЛЬБИОН" class="header-logo">
        <div>
            <h1 class="header-title">АЛЬБИОН: Файлы</h1>
            <div class="header-subtitle">Файловый сервер для обмена документами</div>
        </div>
    </header>

    <div class="main">
        <div class="top-row">
            <div class="card card-upload">
                <h2>📤 Загрузка</h2>
                <div class="drop-zone" id="dropZone">
                    <p><strong>Перетащите файлы сюда</strong> или нажмите для выбора</p>
                    <input type="file" id="fileInput" multiple hidden>
                </div>
                <div class="upload-buttons">
                    <button class="btn btn-sm" onclick="document.getElementById('folderInput').click()">📁 Загрузить папку</button>
                    <button class="btn btn-sm btn-success" onclick="openCreateFolderModal()">➕ Новая папка</button>
                    <input type="file" id="folderInput" webkitdirectory directory multiple hidden>
                </div>
                <div class="file-list" id="fileList"></div>
                <div class="progress-container" id="progressContainer">
                    <div class="progress-bar"><div class="progress-fill" id="progressFill">0%</div></div>
                </div>
                <button id="uploadBtn" disabled>Загрузить всё</button>
            </div>

            <div class="card">
                <h2>📊 Статистика</h2>
                <p style="margin: 5px 0; font-size: 13px; color: #64748b;">
                    Всего скачиваний: <strong style="color:#2563eb">{{ stats_info.total_downloads }}</strong><br>
                    Общий размер: <strong>{{ stats_info.total_size }}</strong>
                </p>
                <a href="/export_xlsx" class="btn btn-success">⬇ Скачать XLSX</a>
            </div>
        </div>

        <div class="card card-files">
            <h2>📂 {{ 'Корень' if not current_folder else current_folder.name }} <span style="color:#64748b; font-weight:400; font-size:13px;">({{ files|length }} файлов, {{ subfolders|length }} папок)</span>
                {% if current_folder %}
                <button class="btn btn-sm" style="margin:0 0 0 8px; vertical-align:middle;" onclick='copyToClipboard("{{ base_url }}/d/{{ current_folder_slug or current_folder_id }}")' title="Скопировать публичную ссылку на эту папку">🔗 Ссылка на папку</button>
                {% endif %}
            </h2>

            <div class="breadcrumbs">
                <a href="/f/">🏠 Корень</a>
                {% for crumb in breadcrumbs %}
                <span class="separator">/</span>
                {% if loop.last %}
                <span class="current">{{ crumb.name }}</span>
                {% else %}
                <a href="/f/{{ crumb.id }}">{{ crumb.name }}</a>
                {% endif %}
                {% endfor %}
            </div>

            <div class="batch-actions" id="batchActions">
                <span class="count">Выбрано: <span id="selectedCount">0</span></span>
                <button class="btn btn-sm btn-success" onclick="executeBatchZip()">⬇ Скачать архив</button>
                <button class="btn btn-sm btn-danger" onclick="batchDelete()">🗑️ Удалить выбранные</button>
                <button class="btn btn-sm" onclick="openBatchMoveModal()">📁 Переместить</button>
                <button class="btn btn-sm btn-secondary" onclick="clearSelection()">✕ Снять выделение</button>
            </div>

            <div class="view-switcher">
                <button class="view-btn" data-view="table">🗂 Таблица</button>
                <button class="view-btn" data-view="grid">🖼 Плитка</button>
                <button class="view-btn" data-view="list">📋 Список</button>
            </div>

            <div class="files-content">
                {% if subfolders or files %}
                    <div id="view-table" class="view-container">
                        <table class="files-table">
                            <thead>
                                <tr>
                                    <th class="checkbox-cell"><input type="checkbox" id="selectAll" onchange="toggleSelectAll()"></th>
                                    <th class="icon-cell"></th>
                                    <th>Имя</th>
                                    <th class="size-col">Размер</th>
                                    <th class="date-col">Изменён</th>
                                    <th class="type-col">Тип</th>
                                    <th class="dl-col">⬇</th>
                                    <th class="actions-cell">Действия</th>
                                </tr>
                            </thead>
                            <tbody>
                                {% for folder in subfolders %}
                                <tr class="folder-row" data-folder-id="{{ folder.id }}" ondragover="handleDragOver(event)" ondragleave="handleDragLeave(event)" ondrop="handleDropOnFolder(event, '{{ folder.id }}')">
                                    <td class="checkbox-cell"><input type="checkbox" class="folder-checkbox" data-folder-id="{{ folder.id }}" onchange="updateSelection()"></td>
                                    <td class="icon-cell"><span class="folder-icon">📁</span></td>
                                    <td><a href="/f/{{ folder.id }}" style="font-weight: 600;">{{ folder.name }}</a></td>
                                    <td class="size-col">{% if folder.file_count %}{{ folder.size }}{% else %}—{% endif %}</td>
                                    <td class="date-col">{{ folder.created }}</td>
                                    <td class="type-col">Папка · 📄 {{ folder.file_count }} · 📁 {{ folder.subdir_count }}</td>
                                    <td class="dl-col">—</td>
                                    <td class="actions-cell">
                                        <button class="btn btn-sm btn-icon" onclick='copyToClipboard("{{ base_url }}/d/{{ folder.slug or folder.id }}")' title="Копировать ссылку на папку">🔗</button>
                                        <button class="btn btn-sm btn-icon btn-icon-danger" onclick='confirmDeleteFolder("{{ folder.id }}", {{ folder.name | tojson }})' title="Удалить папку">🗑️</button>
                                    </td>
                                </tr>
                                {% endfor %}
                                {% for f in files %}
                                <tr class="file-row" draggable="true" ondragstart="handleDragStart(event, '{{ f.internal_id }}')" ondragend="handleDragEnd(event)" data-file-id="{{ f.internal_id }}">
                                    <td class="checkbox-cell"><input type="checkbox" class="file-checkbox" data-file-id="{{ f.internal_id }}" onchange="updateSelection()"></td>
                                    <td class="icon-cell">
                                        {% if f.is_image %}
                                            <img src="{{ f.view_url }}" class="thumb-sm" alt="">
                                        {% else %}
                                            <span class="emoji-icon">{{ f.icon }}</span>
                                        {% endif %}
                                    </td>
                                    <td>
                                        <span style="display: flex; align-items: center; flex-wrap: wrap; gap: 4px;">
                                            {% if f.is_image %}
                                                <a href="{{ f.view_url }}" target="_blank">{{ f.name }}</a>
                                            {% else %}
                                                <a href="{{ f.download_url }}">{{ f.name }}</a>
                                            {% endif %}
                                            {% if f.link_rotated %}
                                                <span class="rotated-badge" title="Старые ссылки не работают">⚠️</span>
                                            {% endif %}
                                        </span>
                                    </td>
                                    <td class="size-col">{{ f.size }}</td>
                                    <td class="date-col">{{ f.mtime }}</td>
                                    <td class="type-col">{{ f.ext }}</td>
                                    <td class="dl-col"><span class="dl-badge">⬇ {{ f.download_count }} | 👁 {{ f.view_count }}</span></td>
                                    <td class="actions-cell">
                                        {% if current_folder %}
                                        <button class="btn btn-sm btn-icon" onclick="moveFileUp('{{ f.internal_id }}')" title="Переместить на уровень выше">⬆️</button>
                                        {% endif %}
                                        <button class="btn btn-sm btn-icon" onclick='openEditModal("{{ f.internal_id }}", {{ f.name | tojson }}, {{ f.current_filename | tojson }}, {{ f.custom_alias | tojson }}, "{{ f.folder_id or '' }}")' title="Редактировать">✏️</button>
                                        <button class="btn btn-sm btn-icon" onclick="copyToClipboard('{{ f.short_url }}')" title="Копировать короткую ссылку">🔗</button>
                                        <button class="btn btn-sm btn-icon btn-icon-danger" onclick='confirmDelete("{{ f.internal_id }}", {{ f.name | tojson }})' title="Удалить">🗑️</button>
                                    </td>
                                </tr>
                                {% endfor %}
                            </tbody>
                        </table>
                    </div>

                    <div id="view-grid" class="view-container" style="display:none">
                        <div class="files-grid">
                            {% for folder in subfolders %}
                            <div class="tile" data-folder-id="{{ folder.id }}" ondragover="handleDragOver(event)" ondragleave="handleDragLeave(event)" ondrop="handleDropOnFolder(event, '{{ folder.id }}')">
                                <div class="tile-checkbox"><input type="checkbox" class="folder-checkbox" data-folder-id="{{ folder.id }}" onchange="updateSelection()"></div>
                                <a href="/f/{{ folder.id }}" class="tile-folder">
                                    <div class="tile-folder-icon">📁</div>
                                    <div class="tile-folder-name">{{ folder.name }}</div>
                                </a>
                                <div class="tile-name row-actions">
                                    <button class="btn btn-sm btn-icon" onclick='copyToClipboard("{{ base_url }}/d/{{ folder.slug or folder.id }}")' title="Копировать ссылку на папку">🔗</button>
                                    <button class="btn btn-sm btn-icon btn-icon-danger" onclick='confirmDeleteFolder("{{ folder.id }}", {{ folder.name | tojson }})' title="Удалить">🗑️</button>
                                </div>
                            </div>
                            {% endfor %}
                            {% for f in files %}
                            <div class="tile" draggable="true" ondragstart="handleDragStart(event, '{{ f.internal_id }}')" ondragend="handleDragEnd(event)" data-file-id="{{ f.internal_id }}">
                                <div class="tile-checkbox"><input type="checkbox" class="file-checkbox" data-file-id="{{ f.internal_id }}" onchange="updateSelection()"></div>
                                <div class="tile-actions">
                                    {% if current_folder %}
                                    <button onclick="moveFileUp('{{ f.internal_id }}')" title="Переместить выше">⬆️</button>
                                    {% endif %}
                                    <button onclick='openEditModal("{{ f.internal_id }}", {{ f.name | tojson }}, {{ f.current_filename | tojson }}, {{ f.custom_alias | tojson }}, "{{ f.folder_id or '' }}")' title="Редактировать">✏️</button>
                                    <button onclick="copyToClipboard('{{ f.short_url }}')" title="Копировать короткую ссылку">🔗</button>
                                    <button class="del-btn" onclick='confirmDelete("{{ f.internal_id }}", {{ f.name | tojson }})' title="Удалить">🗑️</button>
                                </div>
                                {% if f.is_image %}
                                    <a href="{{ f.view_url }}" target="_blank" class="tile-preview-wrap">
                                        <img src="{{ f.view_url }}" class="tile-preview" alt="">
                                    </a>
                                {% else %}
                                    <a href="{{ f.download_url }}" class="tile-icon-wrap">
                                        <div class="tile-icon">{{ f.icon }}</div>
                                        <div class="tile-ext">{{ f.ext }}</div>
                                    </a>
                                {% endif %}
                                <div class="tile-name">
                                    <a href="{{ f.download_url }}" title="{{ f.name }}">{{ f.name }}</a>
                                    <span class="tile-dl">⬇ {{ f.download_count }}</span>
                                </div>
                            </div>
                            {% endfor %}
                        </div>
                    </div>

                    <div id="view-list" class="view-container" style="display:none">
                        <div class="files-list">
                            {% for folder in subfolders %}
                            <div class="list-item folder-item" data-folder-id="{{ folder.id }}" ondragover="handleDragOver(event)" ondragleave="handleDragLeave(event)" ondrop="handleDropOnFolder(event, '{{ folder.id }}')">
                                <input type="checkbox" class="folder-checkbox" data-folder-id="{{ folder.id }}" onchange="updateSelection()">
                                <span class="emoji-icon-sm">📁</span>
                                <div class="list-name-wrap">
                                    <a href="/f/{{ folder.id }}" class="list-name" style="font-weight: 600;">{{ folder.name }}</a>
                                </div>
                                <span class="list-date">{{ folder.created }}</span>
                                <div class="list-actions">
                                    <button class="btn btn-sm btn-icon" onclick='copyToClipboard("{{ base_url }}/d/{{ folder.slug or folder.id }}")' title="Копировать ссылку на папку">🔗</button>
                                    <button class="btn btn-sm btn-icon btn-icon-danger" onclick='confirmDeleteFolder("{{ folder.id }}", {{ folder.name | tojson }})' title="Удалить">🗑️</button>
                                </div>
                            </div>
                            {% endfor %}
                            {% for f in files %}
                            <div class="list-item" draggable="true" ondragstart="handleDragStart(event, '{{ f.internal_id }}')" ondragend="handleDragEnd(event)" data-file-id="{{ f.internal_id }}">
                                <input type="checkbox" class="file-checkbox" data-file-id="{{ f.internal_id }}" onchange="updateSelection()">
                                {% if f.is_image %}
                                    <img src="{{ f.view_url }}" class="thumb-xs" alt="">
                                {% else %}
                                    <span class="emoji-icon-sm">{{ f.icon }}</span>
                                {% endif %}
                                <div class="list-name-wrap">
                                    <a href="{{ f.download_url }}" class="list-name">{{ f.name }}</a>
                                    {% if f.link_rotated %}<span class="rotated-badge">⚠️</span>{% endif %}
                                </div>
                                <span class="dl-badge">⬇ {{ f.download_count }}</span>
                                <span class="list-size">{{ f.size }}</span>
                                <span class="list-date">{{ f.mtime }}</span>
                                <div class="list-actions">
                                    {% if current_folder %}
                                    <button class="btn btn-sm btn-icon" onclick="moveFileUp('{{ f.internal_id }}')" title="Переместить выше">⬆️</button>
                                    {% endif %}
                                    <button class="btn btn-sm btn-icon" onclick='openEditModal("{{ f.internal_id }}", {{ f.name | tojson }}, {{ f.current_filename | tojson }}, {{ f.custom_alias | tojson }}, "{{ f.folder_id or '' }}")' title="Редактировать">✏️</button>
                                    <button class="btn btn-sm btn-icon" onclick="copyToClipboard('{{ f.short_url }}')" title="Копировать короткую ссылку">🔗</button>
                                    <button class="btn btn-sm btn-icon btn-icon-danger" onclick='confirmDelete("{{ f.internal_id }}", {{ f.name | tojson }})' title="Удалить">🗑️</button>
                                </div>
                            </div>
                            {% endfor %}
                        </div>
                    </div>
                {% else %}
                    <div class="empty-state">
                        <div class="icon">📭</div>
                        <div>Папка пуста</div>
                    </div>
                {% endif %}
            </div>
        </div>
    </div>

    <footer class="status-bar">
        <div class="status-item">
            <span class="label">📁 Файлов:</span>
            <span class="value">{{ files|length }}</span>
        </div>
        <span class="separator">│</span>
        <div class="status-item">
            <span class="label">📂 Папок:</span>
            <span class="value">{{ subfolders|length }}</span>
        </div>
        <span class="separator">│</span>
        <div class="status-item">
            <span class="label">💾 Диск:</span>
            <span class="value">{{ disk.free }} из {{ disk.total }}</span>
            <div class="disk-bar">
                <div class="disk-bar-fill" style="width: {{ disk.percent }}%; background: {% if disk.percent > 90 %}#dc3545{% elif disk.percent > 70 %}#f59e0b{% else %}#28a745{% endif %};"></div>
            </div>
        </div>
        <span class="separator">│</span>
        <div class="status-item">
            <span class="label">📤 Загрузка:</span>
            <span class="value">{{ stats.last_upload or '—' }}</span>
        </div>
    </footer>

    <div class="modal-overlay" id="editModal">
        <div class="modal">
            <h3>✏️ Редактирование файла</h3>
            <input type="hidden" id="editInternalId">
            <div class="form-group">
                <label>Отображаемое имя</label>
                <input type="text" id="editDisplayName">
            </div>
            <div class="form-group">
                <label>Физическое имя</label>
                <input type="text" id="editPhysicalFilename">
                <div class="hint">Допустимы: буквы, цифры, пробелы, дефис, подчеркивание, точка</div>
                <div class="warning" id="filenameWarning" style="display:none">⚠️ Изменение имени сделает старые ссылки недействительными</div>
            </div>
            <div class="form-group">
                <label>Папка</label>
                <select id="editFolderId"></select>
            </div>
            <div class="form-group">
                <label>Индивидуальная ссылка (алиас)</label>
                <input type="text" id="editCustomAlias" oninput="updateAliasPreview()">
                <div class="hint">Только латиница, цифры, дефис, подчеркивание<br>Ссылка: <a id="aliasPreview" href="#" target="_blank"></a></div>
            </div>
            <div class="modal-actions">
                <button class="btn btn-secondary" onclick="closeEditModal()">Отмена</button>
                <button class="btn" onclick="saveFileEdit()">Сохранить</button>
            </div>
        </div>
    </div>

    <div class="modal-overlay" id="createFolderModal">
        <div class="modal">
            <h3>➕ Новая папка</h3>
            <div class="form-group">
                <label>Имя папки</label>
                <input type="text" id="newFolderName" placeholder="Например: Отчёты">
            </div>
            <div class="modal-actions">
                <button class="btn btn-secondary" onclick="closeCreateFolderModal()">Отмена</button>
                <button class="btn" onclick="createFolder()">Создать</button>
            </div>
        </div>
    </div>

    <div class="modal-overlay" id="batchMoveModal">
        <div class="modal">
            <h3>📁 Переместить файлы</h3>
            <div class="form-group">
                <label>Выберите папку</label>
                <select id="batchMoveFolderId"></select>
            </div>
            <div class="modal-actions">
                <button class="btn btn-secondary" onclick="closeBatchMoveModal()">Отмена</button>
                <button class="btn" onclick="executeBatchMove()">Переместить</button>
            </div>
        </div>
    </div>

    <div class="modal-overlay" id="deleteModal">
        <div class="modal">
            <h3>🗑️ Удаление файла</h3>
            <input type="hidden" id="deleteFileId">
            <p class="confirm-text">Удалить файл <span class="confirm-filename" id="deleteFileName"></span>?</p>
            <p class="confirm-text" style="color: #dc3545; font-size: 13px;">⚠️ Файл будет удалён безвозвратно</p>
            <div class="modal-actions">
                <button class="btn btn-secondary" onclick="closeDeleteModal()">Отмена</button>
                <button class="btn btn-danger" onclick="executeDelete()">Удалить</button>
            </div>
        </div>
    </div>

    <div class="modal-overlay" id="deleteFolderModal">
        <div class="modal">
            <h3>🗑️ Удаление папки</h3>
            <input type="hidden" id="deleteFolderId">
            <p class="confirm-text">Удалить папку <span class="confirm-filename" id="deleteFolderName"></span>?</p>
            <p class="confirm-text" style="color: #dc3545; font-size: 13px;">⚠️ Будут удалены все файлы и подпапки внутри!</p>
            <div class="modal-actions">
                <button class="btn btn-secondary" onclick="closeDeleteFolderModal()">Отмена</button>
                <button class="btn btn-danger" onclick="executeDeleteFolder()">Удалить</button>
            </div>
        </div>
    </div>

    <div class="toast" id="toast"></div>

    <script>
        const currentFolderId = '{{ current_folder_id or "" }}';
        const parentFolderId = '{{ (current_folder.parent_id or "") if current_folder else "" }}';
        let draggedFileId = null;
        let selectedFiles = new Set();
        let selectedFolders = new Set();
        
        const viewBtns = document.querySelectorAll('.view-btn');
        const viewContainers = { table: document.getElementById('view-table'), grid: document.getElementById('view-grid'), list: document.getElementById('view-list') };
        function setView(view) {
            if (!viewContainers[view]) view = 'table';
            Object.entries(viewContainers).forEach(([key, el]) => { if (el) el.style.display = (key === view) ? '' : 'none'; });
            viewBtns.forEach(b => b.classList.toggle('active', b.dataset.view === view));
            try { localStorage.setItem('albionFilesView', view); } catch(e) {}
        }
        viewBtns.forEach(b => b.addEventListener('click', () => setView(b.dataset.view)));
        let savedView = 'table';
        try { savedView = localStorage.getItem('albionFilesView') || 'table'; } catch(e) {}
        setView(savedView);

        function handleDragStart(event, fileId) {
            draggedFileId = fileId;
            event.target.classList.add('dragging');
            event.dataTransfer.effectAllowed = 'move';
            event.dataTransfer.setData('text/plain', fileId);
        }

        function handleDragEnd(event) {
            event.target.classList.remove('dragging');
            document.querySelectorAll('.drag-over').forEach(el => el.classList.remove('drag-over'));
            draggedFileId = null;
        }

        function handleDragOver(event) {
            event.preventDefault();
            event.dataTransfer.dropEffect = 'move';
            event.currentTarget.classList.add('drag-over');
        }

        function handleDragLeave(event) {
            event.currentTarget.classList.remove('drag-over');
        }

        async function handleDropOnFolder(event, targetFolderId) {
            event.preventDefault();
            event.currentTarget.classList.remove('drag-over');
            const fileId = event.dataTransfer.getData('text/plain');
            if (!fileId) return;
            try {
                const response = await fetch('/api/move_file', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ file_id: fileId, target_folder_id: targetFolderId })
                });
                const result = await response.json();
                if (response.ok) { showToast('✅ Файл перемещён'); setTimeout(() => location.reload(), 800); }
                else { showToast('❌ ' + (result.error || 'Ошибка'), true); }
            } catch (err) { showToast('❌ Ошибка сети', true); }
        }

        function updateSelection() {
            selectedFiles.clear();
            document.querySelectorAll('.file-checkbox:checked').forEach(cb => selectedFiles.add(cb.dataset.fileId));
            selectedFolders.clear();
            document.querySelectorAll('.folder-checkbox:checked').forEach(cb => selectedFolders.add(cb.dataset.folderId));
            document.querySelectorAll('.file-row, .tile, .list-item').forEach(el => {
                const fileId = el.dataset.fileId;
                const folderId = el.dataset.folderId;
                const on = (fileId && selectedFiles.has(fileId)) || (folderId && selectedFolders.has(folderId));
                el.classList.toggle('selected', !!on);
            });
            const batchActions = document.getElementById('batchActions');
            const selectedCount = document.getElementById('selectedCount');
            const total = selectedFiles.size + selectedFolders.size;
            if (total > 0) { batchActions.classList.add('active'); selectedCount.textContent = total; }
            else { batchActions.classList.remove('active'); }
        }

        function toggleSelectAll() {
            const selectAll = document.getElementById('selectAll');
            document.querySelectorAll('.file-checkbox, .folder-checkbox').forEach(cb => { cb.checked = selectAll.checked; });
            updateSelection();
        }

        function clearSelection() {
            document.querySelectorAll('.file-checkbox, .folder-checkbox').forEach(cb => { cb.checked = false; });
            document.getElementById('selectAll').checked = false;
            updateSelection();
        }

        async function batchDelete() {
            if (selectedFiles.size === 0) return;
            if (!confirm(`Удалить ${selectedFiles.size} файл(ов)?`)) return;
            try {
                const response = await fetch('/api/delete_files_batch', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ file_ids: Array.from(selectedFiles) })
                });
                const result = await response.json();
                if (response.ok) { showToast(`✅ Удалено: ${result.count}`); setTimeout(() => location.reload(), 800); }
                else { showToast('❌ ' + (result.error || 'Ошибка'), true); }
            } catch (err) { showToast('❌ Ошибка', true); }
        }

        async function executeBatchZip() {
            if (selectedFiles.size === 0 && selectedFolders.size === 0) return;
            const btn = document.querySelector('#batchActions .btn-success');
            btn.disabled = true; btn.textContent = '⏳ Создание архива...';
            try {
                const response = await fetch('/api/export_zip', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ file_ids: Array.from(selectedFiles), folder_ids: Array.from(selectedFolders) })
                });
                const result = await response.json();
                if (response.ok) {
                    copyToClipboard(result.url);
                    showToast(`✅ Архив: ${result.count} файл(ов), ${result.size}. Ссылка скопирована`);
                    const a = document.createElement('a');
                    a.href = result.url; a.download = '';
                    document.body.appendChild(a); a.click(); a.remove();
                    clearSelection();
                } else { showToast('❌ ' + (result.error || 'Ошибка'), true); }
            } catch (err) { showToast('❌ Ошибка сети', true); }
            finally { btn.disabled = false; btn.textContent = '⬇ Скачать архив'; }
        }

        function openBatchMoveModal() {
            if (selectedFiles.size === 0) return;
            loadFolderSelect('batchMoveFolderId');
            document.getElementById('batchMoveModal').classList.add('active');
        }

        function closeBatchMoveModal() { document.getElementById('batchMoveModal').classList.remove('active'); }

        async function executeBatchMove() {
            const targetFolderId = document.getElementById('batchMoveFolderId').value || null;
            try {
                const response = await fetch('/api/move_files_batch', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ file_ids: Array.from(selectedFiles), target_folder_id: targetFolderId })
                });
                const result = await response.json();
                if (response.ok) { showToast(`✅ Перемещено: ${result.count}`); closeBatchMoveModal(); setTimeout(() => location.reload(), 800); }
                else { showToast('❌ ' + (result.error || 'Ошибка'), true); }
            } catch (err) { showToast('❌ Ошибка', true); }
        }

        async function moveFileUp(fileId) {
            try {
                const response = await fetch('/api/move_file', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ file_id: fileId, target_folder_id: parentFolderId || null })
                });
                const result = await response.json();
                if (response.ok) { showToast('✅ Перемещён выше'); setTimeout(() => location.reload(), 800); }
                else { showToast('❌ ' + (result.error || 'Ошибка'), true); }
            } catch (err) { showToast('❌ Ошибка', true); }
        }

        function openEditModal(internalId, displayName, physicalFilename, customAlias, folderId) {
            document.getElementById('editInternalId').value = internalId;
            document.getElementById('editDisplayName').value = displayName;
            document.getElementById('editPhysicalFilename').value = physicalFilename;
            document.getElementById('editCustomAlias').value = customAlias;
            document.getElementById('editFolderId').value = folderId || '';
            document.getElementById('filenameWarning').style.display = 'none';
            updateAliasPreview();
            loadFolderSelect('editFolderId');
            document.getElementById('editModal').classList.add('active');
        }
        function closeEditModal() { document.getElementById('editModal').classList.remove('active'); }
        
        async function loadFolderSelect(selectId) {
            const response = await fetch('/api/get_all_folders');
            const folders = await response.json();
            const select = document.getElementById(selectId);
            select.innerHTML = '<option value="">🏠 Корень</option>';
            folders.forEach(f => {
                select.innerHTML += `<option value="${f.id}">${'—'.repeat(f.depth)} 📁 ${f.name}</option>`;
            });
        }
        
        function updateAliasPreview() {
            const alias = document.getElementById('editCustomAlias').value.trim();
            const url = alias ? `https://albion-rostov.cloudpub.ru/${alias}` : '';
            document.getElementById('aliasPreview').textContent = url || '(прямая ссылка на файл)';
            document.getElementById('aliasPreview').href = url || '#';
        }
        
        async function saveFileEdit() {
            const data = {
                internal_id: document.getElementById('editInternalId').value,
                display_name: document.getElementById('editDisplayName').value.trim(),
                physical_filename: document.getElementById('editPhysicalFilename').value.trim(),
                custom_alias: document.getElementById('editCustomAlias').value.trim(),
                folder_id: document.getElementById('editFolderId').value || null
            };
            if (!data.display_name || !data.physical_filename) { showToast('❌ Имена не могут быть пустыми', true); return; }
            const btn = document.querySelector('#editModal .modal-actions .btn:last-child');
            btn.disabled = true; btn.textContent = 'Сохранение...';
            try {
                const response = await fetch('/api/update_file', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(data)
                });
                const result = await response.json();
                if (response.ok) { showToast('✅ Сохранено'); closeEditModal(); setTimeout(() => location.reload(), 800); }
                else { showToast('❌ ' + (result.error || 'Ошибка'), true); }
            } catch (err) { showToast('❌ Ошибка', true); }
            finally { btn.disabled = false; btn.textContent = 'Сохранить'; }
        }

        function openCreateFolderModal() { document.getElementById('createFolderModal').classList.add('active'); }
        function closeCreateFolderModal() { document.getElementById('createFolderModal').classList.remove('active'); }
        async function createFolder() {
            const name = document.getElementById('newFolderName').value.trim();
            if (!name) { showToast('❌ Введите имя папки', true); return; }
            const btn = document.querySelector('#createFolderModal .modal-actions .btn:last-child');
            btn.disabled = true;
            try {
                const response = await fetch('/api/create_folder', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name, parent_id: currentFolderId || null })
                });
                const result = await response.json();
                if (response.ok) { showToast('✅ Папка создана'); closeCreateFolderModal(); setTimeout(() => location.reload(), 800); }
                else { showToast('❌ ' + (result.error || 'Ошибка'), true); }
            } catch (err) { showToast('❌ Ошибка', true); }
            finally { btn.disabled = false; }
        }

        function confirmDelete(internalId, fileName) {
            document.getElementById('deleteFileId').value = internalId;
            document.getElementById('deleteFileName').textContent = fileName;
            document.getElementById('deleteModal').classList.add('active');
        }
        function closeDeleteModal() { document.getElementById('deleteModal').classList.remove('active'); }
        async function executeDelete() {
            const internalId = document.getElementById('deleteFileId').value;
            const btn = document.querySelector('#deleteModal .btn-danger');
            btn.disabled = true;
            try {
                const response = await fetch('/api/delete_file', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ internal_id: internalId })
                });
                if (response.ok) { showToast('✅ Удалено'); closeDeleteModal(); setTimeout(() => location.reload(), 800); }
                else { const data = await response.json(); showToast('❌ ' + (data.error || 'Ошибка'), true); }
            } catch (err) { showToast('❌ Ошибка', true); }
            finally { btn.disabled = false; }
        }

        function confirmDeleteFolder(folderId, folderName) {
            document.getElementById('deleteFolderId').value = folderId;
            document.getElementById('deleteFolderName').textContent = folderName;
            document.getElementById('deleteFolderModal').classList.add('active');
        }
        function closeDeleteFolderModal() { document.getElementById('deleteFolderModal').classList.remove('active'); }
        async function executeDeleteFolder() {
            const folderId = document.getElementById('deleteFolderId').value;
            const btn = document.querySelector('#deleteFolderModal .btn-danger');
            btn.disabled = true;
            try {
                const response = await fetch('/api/delete_folder', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ folder_id: folderId })
                });
                if (response.ok) { showToast('✅ Папка удалена'); closeDeleteFolderModal(); setTimeout(() => location.reload(), 800); }
                else { const data = await response.json(); showToast('❌ ' + (data.error || 'Ошибка'), true); }
            } catch (err) { showToast('❌ Ошибка', true); }
            finally { btn.disabled = false; }
        }

        const dropZone = document.getElementById('dropZone');
        const fileInput = document.getElementById('fileInput');
        const folderInput = document.getElementById('folderInput');
        const fileList = document.getElementById('fileList');
        const uploadBtn = document.getElementById('uploadBtn');
        const progressContainer = document.getElementById('progressContainer');
        const progressFill = document.getElementById('progressFill');
        const toast = document.getElementById('toast');
        let selectedUploadFiles = [];
        let isFolderUpload = false;

        dropZone.addEventListener('click', () => fileInput.click());
        fileInput.addEventListener('change', (e) => { isFolderUpload = false; addFiles(e.target.files); });
        folderInput.addEventListener('change', (e) => { isFolderUpload = true; addFiles(e.target.files); });
        
        ['dragenter', 'dragover'].forEach(evt => dropZone.addEventListener(evt, (e) => { e.preventDefault(); dropZone.classList.add('dragover'); }));
        ['dragleave', 'drop'].forEach(evt => dropZone.addEventListener(evt, (e) => { e.preventDefault(); dropZone.classList.remove('dragover'); }));
        dropZone.addEventListener('drop', (e) => { isFolderUpload = false; addFiles(e.dataTransfer.files); });

        function addFiles(files) {
            for (const file of files) {
                if (!selectedUploadFiles.some(f => f.name === file.name && f.size === file.size)) selectedUploadFiles.push(file);
            }
            renderFileList();
        }
        function renderFileList() {
            fileList.innerHTML = '';
            selectedUploadFiles.forEach((file, idx) => {
                const item = document.createElement('div');
                item.className = 'file-item';
                const relPath = isFolderUpload ? (file.webkitRelativePath || file.name) : file.name;
                item.innerHTML = `<span class="name">${escapeHtml(relPath)}</span><span class="size">${formatSize(file.size)}</span><button class="remove" data-idx="${idx}">✕</button>`;
                fileList.appendChild(item);
            });
            fileList.querySelectorAll('.remove').forEach(btn => {
                btn.addEventListener('click', (e) => { selectedUploadFiles.splice(parseInt(e.target.dataset.idx), 1); renderFileList(); });
            });
            uploadBtn.disabled = selectedUploadFiles.length === 0;
        }
        function formatSize(bytes) {
            if (bytes < 1024) return bytes + ' Б';
            if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' КБ';
            if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + ' МБ';
            return (bytes / (1024 * 1024 * 1024)).toFixed(2) + ' ГБ';
        }
        function escapeHtml(str) { const div = document.createElement('div'); div.textContent = str; return div.innerHTML; }
        function showToast(msg, isError = false) {
            toast.textContent = msg;
            toast.className = 'toast show' + (isError ? ' error' : '');
            setTimeout(() => toast.classList.remove('show'), 3500);
        }
        function copyToClipboard(text) {
            navigator.clipboard.writeText(text).then(() => showToast('✅ Ссылка скопирована')).catch(() => showToast('❌ Не удалось', true));
        }

        // ===== Параллельная загрузка: батчи файлов в несколько потоков =====
        const PARALLEL_UPLOADS = 4;   // одновременных запросов
        const FILES_PER_BATCH = 25;   // файлов в одном запросе

        uploadBtn.addEventListener('click', async () => {
            if (selectedUploadFiles.length === 0) return;
            uploadBtn.disabled = true;
            progressContainer.style.display = 'block';
            progressFill.style.width = '0%'; progressFill.textContent = '0%';

            // Группируем по целевой папке: файлы одной папки не должны попасть
            // в разные параллельные запросы (иначе гонка имён)
            const groups = new Map();
            for (const f of selectedUploadFiles) {
                const key = isFolderUpload
                    ? (f.webkitRelativePath || f.name).split('/').slice(0, -1).join('/')
                    : '__plain__';
                if (!groups.has(key)) groups.set(key, []);
                groups.get(key).push(f);
            }
            const batches = [];
            for (const files of groups.values()) {
                for (let i = 0; i < files.length; i += FILES_PER_BATCH)
                    batches.push(files.slice(i, i + FILES_PER_BATCH));
            }

            const totalBytes = selectedUploadFiles.reduce((s, f) => s + f.size, 0);
            const loadedByBatch = new Map();
            let savedTotal = 0, failedTotal = 0, lastError = '';

            const updateProgress = () => {
                let loaded = 0;
                loadedByBatch.forEach(v => loaded += v);
                const pct = totalBytes ? Math.min(100, Math.round(loaded / totalBytes * 100)) : 100;
                progressFill.style.width = pct + '%'; progressFill.textContent = pct + '%';
            };

            const uploadBatch = (batch) => new Promise((resolve) => {
                const fd = new FormData();
                fd.append('folder_id', currentFolderId);
                batch.forEach((f, i) => {
                    fd.append(`file_${i}`, f);
                    if (isFolderUpload) fd.append(`path_${i}`, f.webkitRelativePath || f.name);
                });
                fd.append('count', String(batch.length));

                const xhr = new XMLHttpRequest();
                xhr.open('POST', isFolderUpload ? '/upload_folder' : '/upload');
                xhr.upload.onprogress = (e) => {
                    if (e.lengthComputable) { loadedByBatch.set(batch, e.loaded); updateProgress(); }
                };
                xhr.onload = () => {
                    if (xhr.status === 200) {
                        try { savedTotal += JSON.parse(xhr.responseText).count || 0; }
                        catch (e) { savedTotal += batch.length; }
                    } else {
                        failedTotal += batch.length;
                        let msg = `Ошибка ${xhr.status}`;
                        try { const d = JSON.parse(xhr.responseText); if (d.error) msg = d.error; } catch (e) {}
                        lastError = msg;
                        console.error('Ошибка батча:', xhr.status, xhr.responseText);
                    }
                    resolve();
                };
                xhr.onerror = () => { failedTotal += batch.length; lastError = 'Ошибка сети'; resolve(); };
                xhr.send(fd);
            });

            // Очередь батчей, PARALLEL_UPLOADS воркеров
            let idx = 0;
            const worker = async () => {
                while (idx < batches.length) {
                    await uploadBatch(batches[idx++]);
                }
            };
            await Promise.all(Array.from({ length: Math.min(PARALLEL_UPLOADS, batches.length) }, worker));

            if (failedTotal === 0) {
                progressFill.style.width = '100%'; progressFill.textContent = '100%';
                showToast(`✅ Загружено: ${savedTotal}`);
                selectedUploadFiles = []; renderFileList(); fileInput.value = ''; folderInput.value = '';
                setTimeout(() => location.reload(), 800);
            } else {
                showToast(`❌ Загружено: ${savedTotal}, не удалось: ${failedTotal}. ${lastError}`, true);
                progressContainer.style.display = 'none'; uploadBtn.disabled = false;
                setTimeout(() => location.reload(), 2000);
            }
        });

        document.querySelectorAll('.modal-overlay').forEach(m => {
            m.addEventListener('click', (e) => { if (e.target === m) m.classList.remove('active'); });
        });
    </script>
</body>
</html>
"""

# ===== ПУБЛИЧНАЯ СТРАНИЦА ПАПКИ (без логина, по неугадываемому id) =====
PUBLIC_FOLDER_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <title>АЛЬБИОН: {{ folder.name }}</title>
    <link rel="icon" type="image/png" href="{{ url_for('static', filename='logo.png') }}">
    <meta name="robots" content="noindex">
    <style>
        * { box-sizing: border-box; }
        body { font-family: 'Segoe UI', Arial, sans-serif; background: #f4f4f9; margin: 0; min-height: 100vh; display: flex; flex-direction: column; }
        .header { background: linear-gradient(135deg, #1e3a8a 0%, #2563eb 100%); color: white; padding: 15px 30px; display: flex; align-items: center; gap: 15px; }
        .header img { height: 42px; background: white; padding: 4px 7px; border-radius: 6px; }
        .header h1 { margin: 0; font-size: 19px; font-weight: 700; }
        .header .crumbs { font-size: 12px; opacity: 0.85; margin-top: 3px; }
        .header .crumbs span.sep { opacity: 0.6; margin: 0 4px; }
        .main { flex: 1; width: 100%; max-width: 950px; margin: 0 auto; padding: 20px 15px; }
        .card { background: white; border-radius: 8px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); overflow: hidden; }
        .item { display: flex; align-items: center; gap: 12px; padding: 10px 18px; border-bottom: 1px solid #f1f5f9; }
        .item:last-child { border-bottom: none; }
        .item:hover { background: #f8fafc; }
        .item .icon { font-size: 22px; width: 28px; text-align: center; flex-shrink: 0; }
        .item img.thumb { width: 32px; height: 32px; object-fit: cover; border-radius: 3px; }
        .item .name { flex: 1; min-width: 0; }
        .item .name a { color: #1e293b; text-decoration: none; font-size: 14px; word-break: break-all; }
        .item .name a:hover { color: #2563eb; text-decoration: underline; }
        .item .name .sub { font-size: 11px; color: #94a3b8; }
        .item .meta { color: #64748b; font-size: 12px; white-space: nowrap; }
        .item .actions a { color: #2563eb; font-size: 12px; text-decoration: none; background: #eff6ff; padding: 4px 10px; border-radius: 5px; font-weight: 600; }
        .item .actions a:hover { background: #dbeafe; }
        .folder-item { background: #fefce8; }
        .folder-item:hover { background: #fef3c7; }
        .empty { text-align: center; padding: 50px 20px; color: #94a3b8; }
        .empty .icon { font-size: 44px; margin-bottom: 8px; }
        .footer { text-align: center; color: #94a3b8; font-size: 11px; padding: 12px; }
    </style>
</head>
<body>
    <header class="header">
        <img src="{{ url_for('static', filename='logo.png') }}" alt="АЛЬБИОН">
        <div>
            <h1>📁 {{ folder.name }}</h1>
            <div class="crumbs">
                {% for crumb in breadcrumbs %}{{ crumb.name }}{% if not loop.last %}<span class="sep">/</span>{% endif %}{% endfor %}
            </div>
        </div>
    </header>
    <div class="main">
        <div class="card">
            {% if subfolders or files %}
                {% for sf in subfolders %}
                <div class="item folder-item">
                    <span class="icon">📁</span>
                    <div class="name">
                        <a href="/d/{{ sf.slug or sf.id }}">{{ sf.name }}</a>
                        <div class="sub">папка</div>
                    </div>
                </div>
                {% endfor %}
                {% for f in files %}
                <div class="item">
                    {% if f.is_image %}
                        <img class="thumb" src="{{ f.view_url }}" alt="">
                    {% else %}
                        <span class="icon">{{ f.icon }}</span>
                    {% endif %}
                    <div class="name">
                        <a href="{{ f.view_url if f.is_image else f.download_url }}" {% if not f.is_image %}download{% endif %} target="_blank">{{ f.name }}</a>
                        <div class="sub">{{ f.ext }}</div>
                    </div>
                    <span class="meta">{{ f.size }} · {{ f.mtime }}</span>
                    <span class="actions">
                        <a href="{{ f.view_url }}" target="_blank">Открыть</a>
                        <a href="{{ f.download_url }}" download>Скачать</a>
                    </span>
                </div>
                {% endfor %}
            {% else %}
                <div class="empty">
                    <div class="icon">📭</div>
                    <div>Папка пуста</div>
                </div>
            {% endif %}
        </div>
    </div>
    <div class="footer">АЛЬБИОН: Файловый сервер</div>
</body>
</html>
"""


# ===== АУТЕНТИФИКАЦИЯ =====
AUTH_FILE = os.path.join(DATA_DIR, 'auth.json')

# ===== API-КЛЮЧИ ДЛЯ АВТОМАТИКИ (tokens.json: {sha256(ключ): {name, created}}) =====
def load_tokens():
    return _load_json(TOKENS_FILE)

def check_api_key(provided):
    """Имя ключа, если предоставлен валидный API-ключ, иначе None."""
    if not provided:
        return None
    key_hash = hashlib.sha256(provided.encode('utf-8')).hexdigest()
    meta = load_tokens().get(key_hash)
    return meta.get('name') if meta else None

@app.before_request
def _resolve_api_key():
    """Определяем API-ключ один раз на запрос (заголовок X-API-Key или ?api_key=)."""
    g.api_key = check_api_key(request.headers.get('X-API-Key') or request.args.get('api_key'))

# ===== БЮДЖЕТ СКАЧИВАНИЯ: скользящее окно на IP =====
_download_windows = {}

def check_download_budget(ip, size_bytes):
    """None — скачивание разрешено (usage записан); иначе сколько секунд ждать.
    Одиночный файл больше лимита пропускается (used == 0), чтобы не блокировать
    легитимное скачивание одного большого файла."""
    now = time.time()
    if g.get('api_key'):
        limit, window = TOKEN_DL_BUDGET_BYTES, TOKEN_DL_WINDOW
    else:
        limit, window = ANON_DL_BUDGET_BYTES, ANON_DL_WINDOW
    dq = _download_windows.setdefault(ip, deque())
    while dq and now - dq[0][0] > window:
        dq.popleft()
    used = sum(b for _, b in dq)
    if used > 0 and used + size_bytes > limit:
        return max(1, int(window - (now - dq[0][0])))
    dq.append((now, size_bytes))
    if len(dq) > 4096:  # страховка памяти на экзотических клиентах
        dq.popleft()
    return None

# Массовый экспорт: последнее время на область (IP или ключ)
_export_all_scopes = {}

def get_auth():
    if os.path.exists(AUTH_FILE):
        with open(AUTH_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    # Дефолтные данные, если файл не создан (пользователь должен запустить change_password.py)
    return {"username": "admin", "password_hash": generate_password_hash("admin")}

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        auth = get_auth()
        if request.form.get('username') == auth['username'] and check_password_hash(auth['password_hash'], request.form.get('password', '')):
            session['logged_in'] = True
            app.logger.info('Вход: пользователь "%s" с %s', request.form.get('username'), request.remote_addr)
            next_page = request.args.get('next', '/f/')
            # Защита от open redirect: только относительные пути внутри сайта
            if not next_page.startswith('/') or next_page.startswith('//'):
                next_page = '/f/'
            return redirect(next_page)
        app.logger.warning('Неудачная попытка входа: логин "%s" с %s',
                           request.form.get('username'), request.remote_addr)
        return '''<!DOCTYPE html><html><head><title>Вход - АЛЬБИОН</title>
        <style>body{font-family:'Segoe UI',Arial,sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;background:#f4f4f9;margin:0}
        .box{background:white;padding:40px;border-radius:10px;box-shadow:0 4px 12px rgba(0,0,0,0.1);width:320px;text-align:center}
        h2{color:#1e3a8a;margin-top:0}input{width:100%;padding:12px;margin:10px 0;border:1px solid #cbd5e1;border-radius:6px;font-size:14px;box-sizing:border-box}
        button{width:100%;padding:12px;background:#2563eb;color:white;border:none;border-radius:6px;cursor:pointer;font-size:15px;font-weight:600;margin-top:10px}
        button:hover{background:#1e3a8a}.error{color:#dc3545;font-size:14px;margin-bottom:10px}</style></head><body>
        <div class="box"><h2>🔒 АЛЬБИОН: Вход</h2>
        <p class="error">Неверный логин или пароль</p>
        <form method="post"><input name="username" placeholder="Логин" required>
        <input type="password" name="password" placeholder="Пароль" required>
        <button type="submit">Войти</button></form></div></body></html>'''
    
    # Если уже залогинен, редирект
    if session.get('logged_in'):
        return redirect('/f/')
        
    return '''<!DOCTYPE html><html><head><title>Вход - АЛЬБИОН</title>
    <style>body{font-family:'Segoe UI',Arial,sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;background:#f4f4f9;margin:0}
    .box{background:white;padding:40px;border-radius:10px;box-shadow:0 4px 12px rgba(0,0,0,0.1);width:320px;text-align:center}
    h2{color:#1e3a8a;margin-top:0}input{width:100%;padding:12px;margin:10px 0;border:1px solid #cbd5e1;border-radius:6px;font-size:14px;box-sizing:border-box}
    button{width:100%;padding:12px;background:#2563eb;color:white;border:none;border-radius:6px;cursor:pointer;font-size:15px;font-weight:600;margin-top:10px}
    button:hover{background:#1e3a8a}</style></head><body>
    <div class="box"><h2>🔒 АЛЬБИОН: Вход</h2>
    <form method="post"><input name="username" placeholder="Логин" required>
    <input type="password" name="password" placeholder="Пароль" required>
    <button type="submit">Войти</button></form></div></body></html>'''

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect('/login')

@app.route('/')
def index():
    return redirect('/f/')

@app.route('/f/')
@app.route('/f/<folder_id>')
@login_required
def browse_folder(folder_id=None):
    folders = load_folders()
    
    if folder_id and folder_id not in folders:
        abort(404)
    
    current_folder = folders.get(folder_id) if folder_id else None
    breadcrumbs_data = []
    
    if folder_id:
        path = get_folder_path(folder_id, folders)
        current = folder_id
        for name in reversed(path):
            breadcrumbs_data.append({'id': current, 'name': name})
            current = folders[current].get('parent_id')
        breadcrumbs_data.reverse()
    
    stats_map = get_folder_stats_map(folders, load_db())
    subfolders_data = []
    for fid, f in folders.items():
        if f.get('parent_id') == folder_id:
            f_count, f_size, f_subdirs = stats_map.get(fid, (0, 0, 0))
            subfolders_data.append({
                'id': fid,
                'name': f['name'],
                'created': f.get('created', ''),
                'slug': f.get('slug', ''),
                'file_count': f_count,
                'subdir_count': f_subdirs,
                'size': format_size(f_size),
            })
    
    files = get_files_in_folder(folder_id)
    stats_info = get_all_files()
    stats = load_stats()
    disk = get_disk_info()
    
    return render_template_string(HTML_TEMPLATE,
        current_folder=current_folder,
        current_folder_id=folder_id,
        current_folder_slug=(folders[folder_id].get('slug', '') if folder_id else ''),
        breadcrumbs=breadcrumbs_data,
        subfolders=subfolders_data,
        files=files,
        stats_info=stats_info,
        stats=stats,
        disk=disk,
        base_url=BASE_URL)

@app.route('/d/<folder_ref>')
@limiter.limit("60 per minute")
def public_folder(folder_ref):
    """Публичный просмотр папки: по короткой ссылке (слагу) или по uuid.
    Без логина: скачивание в проекте публичное, ссылка лишь открывает доступ
    к содержимому конкретной папки."""
    folders = load_folders()
    fid = folder_ref if folder_ref in folders else None
    if fid is None:
        for cand_id, cand in folders.items():
            if cand.get('slug') == folder_ref:
                fid = cand_id
                break
    if fid is None:
        abort(404)

    folder = folders[fid]

    breadcrumbs_data = []
    path = get_folder_path(fid, folders)
    current = fid
    for name in reversed(path):
        breadcrumbs_data.append({'id': current, 'name': name})
        current = folders[current].get('parent_id')
    breadcrumbs_data.reverse()

    subfolders_data = [{'id': fid2, 'name': f['name'], 'slug': f.get('slug', '')}
                       for fid2, f in folders.items() if f.get('parent_id') == fid]

    files_data = []
    db = load_db()
    for record in db.values():
        if record.get('folder_id') != fid:
            continue
        physical_path = record.get('physical_path', record['current_filename'])
        if not os.path.isfile(os.path.join(UPLOAD_FOLDER, physical_path)):
            continue
        files_data.append({
            'name': record['display_name'],
            'ext': record['ext'],
            'icon': get_file_icon(record['ext']),
            'is_image': record.get('is_image', False),
            'size': format_size(record.get('size', 0)),
            'mtime': record['mtime'],
            'download_url': f"{BASE_URL}/{physical_path}",
            'view_url': f"{BASE_URL}/v/{physical_path}",
        })
    files_data.sort(key=lambda x: x['mtime'], reverse=True)

    app.logger.info('Публичный просмотр папки "%s" (%s) от %s',
                    folder['name'], fid, request.remote_addr)

    return render_template_string(PUBLIC_FOLDER_TEMPLATE,
        folder=folder,
        breadcrumbs=breadcrumbs_data,
        subfolders=subfolders_data,
        files=files_data)

@app.route('/upload', methods=['POST'])
@login_required
def upload_file():
    if 'files' not in request.files and 'file_0' not in request.files:
        return jsonify({'error': 'Нет файлов'}), 400

    folder_id = request.form.get('folder_id') or None
    folders = load_folders()
    
    if folder_id and folder_id not in folders:
        return jsonify({'error': 'Папка не найдена'}), 404
    
    files = []
    try:
        count = int(request.form.get('count', 0))
    except (TypeError, ValueError):
        return jsonify({'error': 'Некорректное значение count'}), 400
    for i in range(count):
        file = request.files.get(f'file_{i}')
        if file and file.filename:
            files.append((file, ''))
    
    if not files:
        files_list = request.files.getlist('files')
        files = [(f, '') for f in files_list if f.filename]

    with UPLOAD_LOCK:
        saved_count = 0
        db = load_db()

        folder_physical_path = get_folder_physical_path(folder_id, folders)
        os.makedirs(folder_physical_path, exist_ok=True)

        existing_names = set()
        for record in db.values():
            if record.get('folder_id') == folder_id:
                existing_names.add(record['current_filename'])
        for fname in os.listdir(folder_physical_path):
            if os.path.isfile(os.path.join(folder_physical_path, fname)):
                existing_names.add(fname)

        for file, _ in files:
            if not allowed_file(file.filename):
                app.logger.warning('Загрузка: "%s" отклонён — расширение не в белом списке', file.filename)
                continue

            internal_id = str(uuid.uuid4())
            ext = get_file_ext(file.filename)
            original_name = file.filename.replace('+', '_')

            if is_valid_filename(original_name):
                current_filename = original_name
                if current_filename in existing_names:
                    name_part = original_name.rsplit('.', 1)[0] if '.' in original_name else original_name
                    counter = 1
                    while True:
                        current_filename = f"{name_part}_{counter}.{ext}" if ext else f"{name_part}_{counter}"
                        if current_filename not in existing_names:
                            break
                        counter += 1
            else:
                current_filename = f"{internal_id}.{ext}" if ext else internal_id

            physical_path = os.path.relpath(os.path.join(folder_physical_path, current_filename), UPLOAD_FOLDER)

            save_path = os.path.join(UPLOAD_FOLDER, physical_path)
            file.save(save_path)
            existing_names.add(current_filename)

            db[internal_id] = {
                'access_id': internal_id,
                'current_filename': current_filename,
                'physical_path': physical_path,
                'original_filename': original_name,
                'display_name': original_name,
                'folder_id': folder_id,
                'custom_alias': '',
                'slug': make_short_slug(original_name, db, exclude_id=internal_id),
                'link_rotated': False,
                'download_count': 0,
                'size': os.path.getsize(save_path),
                'mtime': datetime.fromtimestamp(os.path.getmtime(save_path)).strftime('%d.%m.%Y %H:%M'),
                'ext': ext.upper() if ext else '—',
                'is_image': ext in IMAGE_EXTENSIONS
            }
            saved_count += 1
            app.logger.info('Загружен файл: %s (%s), короткая ссылка: /%s',
                            physical_path, format_size(os.path.getsize(save_path)), db[internal_id]['slug'])

        if saved_count > 0:
            save_db(db)
            update_last_upload()
            app.logger.info('Загрузка завершена: %d файл(ов) в папку %s', saved_count, folder_id or 'корень')

    return jsonify({'count': saved_count})

@app.route('/upload_folder', methods=['POST'])
@login_required
def upload_folder():
    folder_id = request.form.get('folder_id') or None
    folders = load_folders()
    
    if folder_id and folder_id not in folders:
        return jsonify({'error': 'Папка не найдена'}), 404

    try:
        count = int(request.form.get('count', 0))
    except (TypeError, ValueError):
        return jsonify({'error': 'Некорректное значение count'}), 400
    if count == 0:
        return jsonify({'error': 'Нет файлов'}), 400

    saved_count = 0
    created_folders = set()

    file_data = []
    for i in range(count):
        file = request.files.get(f'file_{i}')
        rel_path = request.form.get(f'path_{i}', '')
        if file and file.filename:
            file_data.append((file, rel_path))

    with UPLOAD_LOCK:
        db = load_db()

        for file, rel_path in file_data:
            if not rel_path:
                app.logger.warning('Загрузка папки: "%s" без пути (path_%d отсутствует) — пропущен',
                                   file.filename, file_data.index((file, rel_path)))
                continue

            parts = rel_path.replace('\\', '/').split('/')
            filename = parts[-1].replace('+', '_')
            folder_parts = parts[:-1]

            if not folder_parts:
                app.logger.warning('Загрузка папки: "%s" без подпапки в пути — пропущен', rel_path)
                continue

            # Защита от path traversal: '..' и '.' в пути папки запрещают загрузку файла
            if any(not is_valid_foldername(p) for p in folder_parts):
                app.logger.warning('Загрузка папки: "%s" отклонён — недопустимое имя папки в пути %s',
                                   rel_path, folder_parts)
                continue

            if not allowed_file(filename):
                app.logger.warning('Загрузка папки: "%s" отклонён — расширение не в белом списке', rel_path)
                continue

            # Потокобезопасный поиск/создание цепочки папок (важно при параллельных батчах)
            current_parent, current_physical, new_folder_ids = ensure_folder_path(folder_parts, folder_id)
            created_folders.update(new_folder_ids)
            os.makedirs(current_physical, exist_ok=True)

            internal_id = str(uuid.uuid4())
            ext = get_file_ext(filename)

            existing_names = set()
            for record in db.values():
                if record.get('folder_id') == current_parent:
                    existing_names.add(record['current_filename'])
            if os.path.isdir(current_physical):
                existing_names.update(os.listdir(current_physical))

            current_filename = filename
            if is_valid_filename(filename):
                if current_filename in existing_names:
                    name_part = filename.rsplit('.', 1)[0] if '.' in filename else filename
                    counter = 1
                    while True:
                        current_filename = f"{name_part}_{counter}.{ext}" if ext else f"{name_part}_{counter}"
                        if current_filename not in existing_names:
                            break
                        counter += 1
            else:
                current_filename = f"{internal_id}.{ext}" if ext else internal_id

            physical_path = os.path.relpath(os.path.join(current_physical, current_filename), UPLOAD_FOLDER)
            save_path = os.path.join(UPLOAD_FOLDER, physical_path)

            # Страховка от выхода за пределы uploads
            upload_root = os.path.realpath(UPLOAD_FOLDER)
            if not os.path.realpath(save_path).startswith(upload_root + os.sep):
                app.logger.error('Загрузка папки: "%s" отклонён страховкой realpath (%s)', rel_path, save_path)
                continue

            file.save(save_path)

            db[internal_id] = {
                'access_id': internal_id,
                'current_filename': current_filename,
                'physical_path': physical_path,
                'original_filename': filename,
                'display_name': filename,
                'folder_id': current_parent,
                'custom_alias': '',
                'slug': make_short_slug(filename, db, exclude_id=internal_id),
                'link_rotated': False,
                'download_count': 0,
                'size': os.path.getsize(save_path),
                'mtime': datetime.fromtimestamp(os.path.getmtime(save_path)).strftime('%d.%m.%Y %H:%M'),
                'ext': ext.upper() if ext else '—',
                'is_image': ext in IMAGE_EXTENSIONS
            }
            saved_count += 1
            app.logger.info('Загружен файл из папки: %s (%s), короткая ссылка: /%s',
                            physical_path, format_size(os.path.getsize(save_path)), db[internal_id]['slug'])

        if saved_count > 0 or created_folders:
            save_db(db)
            update_last_upload()

    app.logger.info('Загрузка папки завершена: %d файл(ов), %d папок создано (принято в запросе: %d)',
                    saved_count, len(created_folders), count)
    return jsonify({'count': saved_count, 'folders_created': len(created_folders)})

@app.route('/v/<path:filepath>')
@limiter.limit("200 per minute", deduct_when=lambda response: g.get('api_key') is None)
def view_file(filepath):
    try:
        filepath = urllib.parse.unquote(filepath)
    except:
        pass

    # Счётчик растёт только для реальных файлов в БД
    full_path = os.path.join(UPLOAD_FOLDER, filepath)
    if not os.path.isfile(full_path):
        return "Файл не найден", 404

    record = bump_counter(lambda r: r.get('physical_path') == filepath, 'view_count')
    if record is None:
        return "Файл не найден", 404

    retry_after = check_download_budget(request.remote_addr, record.get('size', 0) or 0)
    if retry_after:
        app.logger.warning('Бюджет скачивания исчерпан (%s), файл %s, retry через %d c',
                           request.remote_addr, filepath, retry_after)
        resp = jsonify({'error': f'Слишком много скачиваний, повторите через {max(1, retry_after // 60)} мин'})
        resp.headers['Retry-After'] = str(retry_after)
        return resp, 429

    app.logger.info('Просмотр: %s (👁=%d) [%s]', filepath, record.get('view_count', 0),
                    g.get('api_key') or 'анонимно')
    return send_from_directory(UPLOAD_FOLDER, filepath, as_attachment=False)

@app.route('/api/update_file', methods=['POST'])
@login_required
def update_file():
    data = request.json
    internal_id = data.get('internal_id')
    display_name = data.get('display_name', '').strip()
    physical_filename = data.get('physical_filename', '').strip().replace('+', '_')
    custom_alias = data.get('custom_alias', '').strip()
    new_folder_id = data.get('folder_id') or None

    db = load_db()
    folders = load_folders()
    
    if internal_id not in db:
        return jsonify({'error': 'Файл не найден'}), 404

    record = db[internal_id]
    old_physical_path = record.get('physical_path', record['current_filename'])
    
    if new_folder_id and new_folder_id not in folders:
        return jsonify({'error': 'Папка не найдена'}), 404

    if not is_valid_filename(physical_filename):
        return jsonify({'error': 'Недопустимые символы'}), 400

    # Дубли имён проверяем и по БД, а не только по диску
    for fid, other in db.items():
        if fid != internal_id and other.get('folder_id') == new_folder_id \
                and other.get('current_filename') == physical_filename:
            return jsonify({'error': 'Файл с таким именем уже есть в этой папке'}), 400

    new_folder_physical = get_folder_physical_path(new_folder_id, folders)
    os.makedirs(new_folder_physical, exist_ok=True)
    
    old_full_path = os.path.join(UPLOAD_FOLDER, old_physical_path)
    new_physical_path = os.path.join(new_folder_physical, physical_filename)
    new_rel_path = os.path.relpath(new_physical_path, UPLOAD_FOLDER)

    if os.path.exists(old_full_path):
        if os.path.exists(new_physical_path) and old_physical_path != new_rel_path:
            return jsonify({'error': 'Файл с таким именем уже существует'}), 400
        
        try:
            if old_physical_path != new_rel_path:
                os.rename(old_full_path, new_physical_path)
                record['physical_path'] = new_rel_path
                record['current_filename'] = physical_filename
                
                if old_physical_path != new_rel_path and not old_physical_path.startswith(f"{internal_id}."):
                    record['access_id'] = str(uuid.uuid4())
                    record['custom_alias'] = ''
                    record['link_rotated'] = True
            
            record['folder_id'] = new_folder_id
            record['ext'] = get_file_ext(physical_filename).upper() or '—'
            record['is_image'] = get_file_ext(physical_filename) in IMAGE_EXTENSIONS
        except Exception as e:
            return jsonify({'error': f'Ошибка: {str(e)}'}), 500

    if display_name:
        record['display_name'] = display_name

    if custom_alias:
        if not re.match(r'^[a-zA-Z0-9_-]+$', custom_alias):
            return jsonify({'error': 'Алиас: только латиница, цифры, дефис'}), 400
        # Алиас не может маскироваться под системный маршрут
        if custom_alias.lower() in RESERVED_PATHS:
            return jsonify({'error': 'Этот алиас зарезервирован системой'}), 400
        for fid, rec in db.items():
            if fid != internal_id and rec.get('custom_alias') == custom_alias:
                return jsonify({'error': 'Алиас уже занят'}), 400
        record['custom_alias'] = custom_alias

    save_db(db)
    return jsonify({'success': True})

@app.route('/api/move_file', methods=['POST'])
@login_required
def move_file():
    data = request.json
    file_id = data.get('file_id')
    target_folder_id = data.get('target_folder_id') or None

    db = load_db()
    folders = load_folders()

    if file_id not in db:
        return jsonify({'error': 'Файл не найден'}), 404

    if target_folder_id and target_folder_id not in folders:
        return jsonify({'error': 'Целевая папка не найдена'}), 404

    record = db[file_id]
    old_physical_path = record.get('physical_path', record['current_filename'])
    old_filename = record['current_filename']

    if record.get('folder_id') == target_folder_id:
        return jsonify({'error': 'Файл уже в этой папке'}), 400

    new_folder_physical = get_folder_physical_path(target_folder_id, folders)
    os.makedirs(new_folder_physical, exist_ok=True)

    old_full_path = os.path.join(UPLOAD_FOLDER, old_physical_path)
    new_physical_path = os.path.join(new_folder_physical, old_filename)
    new_rel_path = os.path.relpath(new_physical_path, UPLOAD_FOLDER)

    existing_names = set()
    for rid, rec in db.items():
        if rid != file_id and rec.get('folder_id') == target_folder_id:
            existing_names.add(rec['current_filename'])

    final_filename = old_filename
    if final_filename in existing_names:
        name_part = old_filename.rsplit('.', 1)[0] if '.' in old_filename else old_filename
        ext = get_file_ext(old_filename)
        counter = 1
        while True:
            final_filename = f"{name_part}_{counter}.{ext}" if ext else f"{name_part}_{counter}"
            if final_filename not in existing_names:
                break
            counter += 1
        new_physical_path = os.path.join(new_folder_physical, final_filename)
        new_rel_path = os.path.relpath(new_physical_path, UPLOAD_FOLDER)

    if os.path.exists(old_full_path):
        try:
            os.rename(old_full_path, new_physical_path)
            record['physical_path'] = new_rel_path
            record['current_filename'] = final_filename
            record['folder_id'] = target_folder_id
            
            if old_physical_path != new_rel_path:
                record['access_id'] = str(uuid.uuid4())
                record['custom_alias'] = ''
                record['link_rotated'] = True
        except Exception as e:
            return jsonify({'error': f'Ошибка: {str(e)}'}), 500

    save_db(db)
    return jsonify({'success': True})

@app.route('/api/move_files_batch', methods=['POST'])
@login_required
def move_files_batch():
    data = request.json
    file_ids = data.get('file_ids', [])
    target_folder_id = data.get('target_folder_id') or None

    if not file_ids:
        return jsonify({'error': 'Не выбраны файлы'}), 400

    db = load_db()
    folders = load_folders()

    if target_folder_id and target_folder_id not in folders:
        return jsonify({'error': 'Целевая папка не найдена'}), 404

    moved_count = 0
    new_folder_physical = get_folder_physical_path(target_folder_id, folders)
    os.makedirs(new_folder_physical, exist_ok=True)

    for file_id in file_ids:
        if file_id not in db:
            continue

        record = db[file_id]
        if record.get('folder_id') == target_folder_id:
            continue

        old_physical_path = record.get('physical_path', record['current_filename'])
        old_filename = record['current_filename']
        old_full_path = os.path.join(UPLOAD_FOLDER, old_physical_path)

        existing_names = set()
        for rid, rec in db.items():
            if rid != file_id and rec.get('folder_id') == target_folder_id:
                existing_names.add(rec['current_filename'])

        final_filename = old_filename
        if final_filename in existing_names:
            name_part = old_filename.rsplit('.', 1)[0] if '.' in old_filename else old_filename
            ext = get_file_ext(old_filename)
            counter = 1
            while True:
                final_filename = f"{name_part}_{counter}.{ext}" if ext else f"{name_part}_{counter}"
                if final_filename not in existing_names:
                    break
                counter += 1

        new_physical_path = os.path.join(new_folder_physical, final_filename)
        new_rel_path = os.path.relpath(new_physical_path, UPLOAD_FOLDER)

        if os.path.exists(old_full_path):
            try:
                os.rename(old_full_path, new_physical_path)
                record['physical_path'] = new_rel_path
                record['current_filename'] = final_filename
                record['folder_id'] = target_folder_id
                
                if old_physical_path != new_rel_path:
                    record['access_id'] = str(uuid.uuid4())
                    record['custom_alias'] = ''
                    record['link_rotated'] = True
                
                moved_count += 1
            except Exception as e:
                continue

    save_db(db)
    return jsonify({'success': True, 'count': moved_count})

@app.route('/api/delete_files_batch', methods=['POST'])
@login_required
def delete_files_batch():
    data = request.json
    file_ids = data.get('file_ids', [])

    if not file_ids:
        return jsonify({'error': 'Не выбраны файлы'}), 400

    db = load_db()
    deleted_count = 0

    for file_id in file_ids:
        if file_id not in db:
            continue

        record = db[file_id]
        physical_path = record.get('physical_path', record['current_filename'])
        filepath = os.path.join(UPLOAD_FOLDER, physical_path)

        if os.path.exists(filepath):
            try:
                os.remove(filepath)
                del db[file_id]
                deleted_count += 1
            except Exception as e:
                continue

    save_db(db)
    return jsonify({'success': True, 'count': deleted_count})

@app.route('/api/create_folder', methods=['POST'])
@login_required
def create_folder():
    data = request.json
    name = data.get('name', '').strip()
    parent_id = data.get('parent_id') or None

    if not is_valid_foldername(name):
        return jsonify({'error': 'Недопустимое имя папки'}), 400

    folders = load_folders()
    
    if parent_id and parent_id not in folders:
        return jsonify({'error': 'Родительская папка не найдена'}), 404

    for fid, f in folders.items():
        if f.get('parent_id') == parent_id and f['name'] == name:
            return jsonify({'error': 'Папка уже существует'}), 400

    folder_id = str(uuid.uuid4())
    folders[folder_id] = {
        'name': name,
        'parent_id': parent_id,
        'created': datetime.now().strftime('%d.%m.%Y %H:%M')
    }
    folders[folder_id]['slug'] = make_short_slug(name, folders=folders)
    
    physical_path = get_folder_physical_path(folder_id, folders)
    os.makedirs(physical_path, exist_ok=True)
    
    save_folders(folders)
    return jsonify({'success': True, 'folder_id': folder_id})

@app.route('/api/delete_folder', methods=['POST'])
@login_required
def delete_folder():
    data = request.json
    folder_id = data.get('folder_id')

    folders = load_folders()
    db = load_db()
    
    if folder_id not in folders:
        return jsonify({'error': 'Папка не найдена'}), 404

    def delete_recursive(fid):
        files_to_delete = [internal_id for internal_id, record in db.items() if record.get('folder_id') == fid]
        for internal_id in files_to_delete:
            physical_path = db[internal_id].get('physical_path', db[internal_id]['current_filename'])
            full_path = os.path.join(UPLOAD_FOLDER, physical_path)
            if os.path.exists(full_path):
                os.remove(full_path)
            del db[internal_id]
        
        subfolders = [fid2 for fid2, f in folders.items() if f.get('parent_id') == fid]
        for subfolder_id in subfolders:
            delete_recursive(subfolder_id)
        
        physical_path = get_folder_physical_path(fid, folders)
        if os.path.exists(physical_path):
            try:
                shutil.rmtree(physical_path)
            except:
                pass
        del folders[fid]

    delete_recursive(folder_id)
    save_db(db)
    save_folders(folders)
    
    return jsonify({'success': True})

@app.route('/api/delete_file', methods=['POST'])
@login_required
def delete_file():
    data = request.json
    internal_id = data.get('internal_id')

    db = load_db()
    if internal_id not in db:
        return jsonify({'error': 'Файл не найден'}), 404

    record = db[internal_id]
    physical_path = record.get('physical_path', record['current_filename'])
    filepath = os.path.join(UPLOAD_FOLDER, physical_path)

    if os.path.exists(filepath):
        try:
            os.remove(filepath)
        except Exception as e:
            return jsonify({'error': f'Ошибка: {str(e)}'}), 500

    del db[internal_id]
    save_db(db)
    return jsonify({'success': True})

@app.route('/api/get_all_folders')
@login_required
def get_all_folders():
    folders = load_folders()
    result = []
    
    def add_folder(fid, depth):
        result.append({
            'id': fid,
            'name': folders[fid]['name'],
            'depth': depth
        })
        for fid2, f in folders.items():
            if f.get('parent_id') == fid:
                add_folder(fid2, depth + 1)
    
    for fid, f in folders.items():
        if f.get('parent_id') is None:
            add_folder(fid, 0)
    
    return jsonify(result)

@app.route('/export_xlsx')
@login_required
def export_xlsx():
    """Экспорт в XLSX: папка, физическое имя и ссылка (с кириллицей)"""
    wb = Workbook()
    ws = wb.active
    ws.title = "Список файлов"
    
    # 4 колонки
    ws.append(["Папка", "Физическое имя", "Ссылка", "Короткая ссылка"])

    # Стилизация заголовка
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")

    folders = load_folders()
    db = load_db()
    
    for internal_id, record in db.items():
        # Путь к папке
        folder_path = '/'.join(get_folder_path(record.get('folder_id'), folders)) if record.get('folder_id') else 'Корень'
        physical_path = record.get('physical_path', record['current_filename'])
        # Ссылка с нормальной кириллицей (без URL-кодирования)
        download_url = f"{BASE_URL}/{physical_path}"
        
        ws.append([
            folder_path,
            record['current_filename'],
            download_url,
            f"{BASE_URL}/{record['slug']}" if record.get('slug') else download_url
        ])

    # Автоширина колонок
    for col in ws.columns:
        max_len = 0
        col_letter = col[0].column_letter
        for cell in col:
            try:
                if cell.value:
                    max_len = max(max_len, len(str(cell.value)))
            except:
                pass
        ws.column_dimensions[col_letter].width = min(max_len + 2, 80)

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    update_last_export()

    return send_file(output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name='file_list.xlsx')


@app.route('/<path:filepath>')
@limiter.limit("30 per minute", deduct_when=lambda response: g.get('api_key') is None)
def direct_file_access(filepath):
    if filepath.startswith('static/') or filepath.startswith('f/') or filepath.startswith('d/') or filepath.startswith('z/') or filepath in [
        'export_xlsx', 'export_all', 'upload', 'upload_folder', 'favicon.ico'
    ] or filepath.startswith('api/'):
        abort(404)

    try:
        filepath = urllib.parse.unquote(filepath)
    except:
        pass

    full_path = os.path.join(UPLOAD_FOLDER, filepath)
    record = None

    # 1) Прямая ссылка по физическому пути (счётчик растёт только для реальных файлов)
    if os.path.isfile(full_path):
        record = bump_counter(lambda r: r.get('physical_path') == filepath, 'download_count')

    # 2) Индивидуальная ссылка (алиас) или короткая ссылка (слаг)
    if record is None:
        record = bump_counter(
            lambda r: (r.get('custom_alias') and r.get('custom_alias') == filepath)
                   or (r.get('slug') and r.get('slug') == filepath),
            'download_count')
        if record is not None:
            filepath = record.get('physical_path', record['current_filename'])
            if not os.path.isfile(os.path.join(UPLOAD_FOLDER, filepath)):
                return "Файл не найден", 404

    if record is None:
        return "Файл не найден", 404

    retry_after = check_download_budget(request.remote_addr, record.get('size', 0) or 0)
    if retry_after:
        app.logger.warning('Бюджет скачивания исчерпан (%s), файл %s, retry через %d c',
                           request.remote_addr, filepath, retry_after)
        resp = jsonify({'error': f'Слишком много скачиваний, повторите через {max(1, retry_after // 60)} мин'})
        resp.headers['Retry-After'] = str(retry_after)
        return resp, 429

    app.logger.info('Скачивание: %s (⬇=%d) от %s [%s]', filepath,
                    record.get('download_count', 0), request.remote_addr,
                    g.get('api_key') or 'анонимно')
    # '+' в имени файла браузеры трактовали по-разному — отдаём с '_'
    return send_from_directory(UPLOAD_FOLDER, filepath, as_attachment=True,
                               download_name=record['current_filename'].replace('+', '_'))

@app.route('/export_all')
@limiter.limit("2 per minute")
def export_all():
    """Массовое скачивание всего хранилища одним ZIP.
    Не чаще раза в час на IP (анонимно) или на API-ключ."""
    now = time.time()
    scope = f"key:{g.api_key}" if g.get('api_key') else f"ip:{request.remote_addr}"
    last = _export_all_scopes.get(scope)
    if last and now - last < EXPORT_ALL_INTERVAL:
        retry = int(EXPORT_ALL_INTERVAL - (now - last))
        app.logger.warning('Экспорт всего: отказ (повтор раньше часа), scope=%s, retry %d c', scope, retry)
        resp = jsonify({'error': f'Массовое скачивание доступно раз в час, повторите через {max(1, retry // 60)} мин'})
        resp.headers['Retry-After'] = str(retry)
        return resp, 429

    db = load_db()
    items = []
    for rec in db.values():
        phys = rec.get('physical_path', rec['current_filename'])
        if os.path.isfile(os.path.join(UPLOAD_FOLDER, phys)):
            items.append((phys, rec['current_filename']))
    if not items:
        return jsonify({'error': 'Нет файлов для архивирования'}), 404

    # Подчистка старых временных архивов (старше 2 часов)
    for fn in os.listdir(UPLOAD_FOLDER):
        if fn.startswith('.export_all-') and fn.endswith('.zip'):
            try:
                if now - os.path.getmtime(os.path.join(UPLOAD_FOLDER, fn)) > 7200:
                    os.remove(os.path.join(UPLOAD_FOLDER, fn))
            except OSError:
                pass

    _export_all_scopes[scope] = now
    tmp_path = os.path.join(UPLOAD_FOLDER, f".export_all-{int(now)}.zip")
    with zipfile.ZipFile(tmp_path, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for phys, _orig in items:
            zf.write(os.path.join(UPLOAD_FOLDER, phys), arcname=phys.replace('+', '_'))

    app.logger.info('Экспорт всего хранилища: %d файл(ов), %s, scope=%s',
                    len(items), format_size(os.path.getsize(tmp_path)), scope)
    return send_file(tmp_path, as_attachment=True,
                     download_name=f"albion-files-{datetime.now().strftime('%Y%m%d-%H%M')}.zip")

@app.route('/api/export_zip', methods=['POST'])
@login_required
def export_zip():
    """ZIP по выделению: выбранные файлы + выбранные папки (рекурсивно).
    Возвращает публичную ссылку на архив (id — uuid, неугадываемый), живёт сутки."""
    data = request.json or {}
    file_ids = data.get('file_ids', [])
    folder_ids = data.get('folder_ids', [])
    if not file_ids and not folder_ids:
        return jsonify({'error': 'Ничего не выбрано'}), 400

    db = load_db()
    folders = load_folders()

    children = {}
    for fid, f in folders.items():
        children.setdefault(f.get('parent_id'), []).append(fid)

    # Файлы выбранных папок — рекурсивно
    seen_folders = set()
    stack = list(folder_ids)
    while stack:
        fid = stack.pop()
        if fid in seen_folders or fid not in folders:
            continue
        seen_folders.add(fid)
        stack.extend(children.get(fid, []))

    items_map = {}  # physical_path -> имя; словарь убирает дубли (файл и его папка выбраны вместе)
    for file_id in file_ids:
        rec = db.get(file_id)
        if rec:
            phys = rec.get('physical_path', rec['current_filename'])
            items_map[phys] = phys
    for rec in db.values():
        if rec.get('folder_id') in seen_folders:
            phys = rec.get('physical_path', rec['current_filename'])
            items_map[phys] = phys
    items = [(phys, phys) for phys in items_map
             if os.path.isfile(os.path.join(UPLOAD_FOLDER, phys))]
    if not items:
        return jsonify({'error': 'Нет файлов для архивирования'}), 404

    os.makedirs(ZIPLINKS_DIR, exist_ok=True)
    # Архивы-ссылки живут сутки, старые подчищаем
    now = time.time()
    for fn in os.listdir(ZIPLINKS_DIR):
        if fn.endswith('.zip'):
            try:
                if now - os.path.getmtime(os.path.join(ZIPLINKS_DIR, fn)) > 86400:
                    os.remove(os.path.join(ZIPLINKS_DIR, fn))
            except OSError:
                pass

    zip_id = str(uuid.uuid4())
    zip_path = os.path.join(ZIPLINKS_DIR, zip_id + '.zip')
    with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for phys, _ in items:
            zf.write(os.path.join(UPLOAD_FOLDER, phys), arcname=phys.replace('+', '_'))

    size = os.path.getsize(zip_path)
    app.logger.info('Создан архив по выборке: %d файл(ов), %s, /z/%s, от %s',
                    len(items), format_size(size), zip_id, request.remote_addr)
    return jsonify({'url': f"{BASE_URL}/z/{zip_id}", 'count': len(items), 'size': format_size(size)})

@app.route('/z/<zip_id>')
@limiter.limit("30 per minute")
def download_zip(zip_id):
    """Публичная ссылка на созданный архив (id неугадываемый, живёт сутки)."""
    if not re.match(r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$', zip_id):
        abort(404)
    path = os.path.join(ZIPLINKS_DIR, zip_id + '.zip')
    if not os.path.isfile(path):
        abort(404)
    app.logger.info('Скачивание архива /z/%s (%s) от %s', zip_id,
                    format_size(os.path.getsize(path)), request.remote_addr)
    return send_file(path, as_attachment=True,
                     download_name=f"albion-archive-{datetime.now().strftime('%Y%m%d-%H%M')}.zip")

@app.route('/robots.txt')
def robots():
    return send_from_directory('static', 'robots.txt', mimetype='text/plain')

if __name__ == '__main__':
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
    print("=" * 60)
    print("АЛЬБИОН: Файловый сервер с каталогами")
    print(f"Локально: http://127.0.0.1:5000")
    print("=" * 60)
    serve(app,
          host=os.environ.get('ALBION_HOST', '0.0.0.0'),   # в офисе за nginx: ALBION_HOST=127.0.0.1
          port=int(os.environ.get('ALBION_PORT', '5000')),
          threads=8,
          max_request_body_size=100 * 1024 * 1024 * 1024,  # 100 ГБ (None waitress не принимает)
          channel_timeout=300)
