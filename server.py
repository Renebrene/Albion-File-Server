import os
import io
import json
from functools import wraps
import uuid
import re
import shutil
import urllib.parse
from datetime import datetime, timedelta, timezone
from flask import Flask, request, send_from_directory, render_template_string, send_file, jsonify, abort, redirect, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from PIL import Image, ImageOps
from waitress import serve
from flask import session, redirect, url_for, request
from werkzeug.security import check_password_hash, generate_password_hash
import secrets
import threading
import logging
import hashlib
import hmac
import ipaddress
import time
import zipfile
from collections import deque
from logging.handlers import RotatingFileHandler
from flask import Flask, request, send_from_directory, render_template_string, send_file, jsonify, abort, redirect, url_for, g
from markupsafe import Markup
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import token_store
# SQLite-слой метаданных. Режим выбирает ALBION_METADATA_BACKEND (json | sqlite):
# json — прежние файлы, sqlite — база albion.db. Откат — вернуть флаг.
import db as metadata_store
# Учётные записи и серверные сессии работают поверх SQLite: персональные логины,
# роли и немедленный отзыв сессий. В режиме json сохраняется прежний единственный
# администратор из auth.json, поэтому откат флага остаётся рабочим.
import users as user_store
import sessions as session_store

# Каталог данных (uploads + json-базы): '.' по умолчанию, в Docker — /app/data
DATA_DIR = os.environ.get('ALBION_DATA_DIR', '.')

# ===== КВОТЫ И СВОБОДНОЕ МЕСТО =====
def _env_gb(name, default_gb):
    """Read a gigabyte limit from env; 0/negative means disabled/unlimited."""
    raw = os.environ.get(name, str(default_gb))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = float(default_gb)
    return max(0, int(value * 1024 ** 3))

MAX_UPLOAD_BYTES = _env_gb('ALBION_MAX_UPLOAD_GB', 50)       # one request
STORAGE_QUOTA_BYTES = _env_gb('ALBION_STORAGE_QUOTA_GB', 0)  # 0 = no hard total quota
MIN_FREE_BYTES = _env_gb('ALBION_MIN_FREE_GB', 5)            # never consume this reserve
MAX_ZIP_BYTES = _env_gb('ALBION_MAX_ZIP_GB', 50)             # selected ZIP source size
try:
    MAX_ZIP_FILES = max(0, int(os.environ.get('ALBION_MAX_ZIP_FILES', '20000')))
except ValueError:
    MAX_ZIP_FILES = 20000
EXPORT_ALL_INTERNAL_ONLY = os.environ.get('ALBION_EXPORT_ALL_INTERNAL_ONLY', '1').lower() in {'1', 'true', 'yes'}
_REDIS_URL = os.environ.get('ALBION_REDIS_URL', '').strip()
RATE_LIMIT_STORAGE_URI = os.environ.get('ALBION_RATE_LIMIT_STORAGE_URI', 'memory://').strip() or 'memory://'
TRUST_PROXY = os.environ.get('ALBION_TRUST_PROXY', '0').lower() in {'1', 'true', 'yes'}
APP_HOST = os.environ.get('ALBION_HOST', '127.0.0.1')
# Второй слушатель для локальной сети: офисные пользователи ходят напрямую,
# а через тоннель остаётся только внешний доступ. Пусто — листенер выключен.
LAN_HOST = os.environ.get('ALBION_LAN_HOST', '').strip()
LAN_PORT = int(os.environ.get('ALBION_LAN_PORT') or os.environ.get('ALBION_PORT', '5000'))
TRUSTED_PROXY_IPS = tuple(
    addr.strip() for addr in os.environ.get('ALBION_TRUSTED_PROXY_IPS', '127.0.0.1,::1').split(',')
    if addr.strip()
)

# ===== ПРИКЛАДНЫЕ НАСТРОЙКИ (управляются только из админки) =====
SETTINGS_FILE = os.path.join(DATA_DIR, 'settings.json')
QUOTA_KEYS = ('max_upload_gb', 'storage_quota_gb', 'min_free_gb', 'max_zip_gb', 'max_zip_files',
              'export_all_internal_only')

def _quota_defaults():
    return {
        'max_upload_gb': MAX_UPLOAD_BYTES / (1024 ** 3),
        'storage_quota_gb': STORAGE_QUOTA_BYTES / (1024 ** 3),
        'min_free_gb': MIN_FREE_BYTES / (1024 ** 3),
        'max_zip_gb': MAX_ZIP_BYTES / (1024 ** 3),
        'max_zip_files': MAX_ZIP_FILES,
        'export_all_internal_only': EXPORT_ALL_INTERNAL_ONLY,
    }

def load_quota_settings():
    """Return validated quota settings; сохранённые значения перекрывают env-умолчания."""
    values = _quota_defaults()
    raw = None
    if metadata_store.sqlite_enabled():
        raw = metadata_store.load_document('settings') or None
    elif os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            try:
                os.chmod(SETTINGS_FILE, 0o600)
            except OSError:
                pass
        except (OSError, ValueError, json.JSONDecodeError) as e:
            print(f'⚠️ settings.json проигнорирован: {e}')
            raw = None
    if isinstance(raw, dict):
        try:
            for key in QUOTA_KEYS:
                if key not in raw:
                    continue
                val = raw[key]
                if key == 'export_all_internal_only':
                    values[key] = bool(val)
                    continue
                if key == 'max_zip_files':
                    val = int(val)
                else:
                    val = float(val)
                if val < 0:
                    raise ValueError(f'negative value for {key}')
                values[key] = val
        except (ValueError, TypeError) as e:
            print(f'⚠️ настройки квот проигнорированы: {e}')
    return values

def save_quota_settings(values):
    clean = {}
    for key in QUOTA_KEYS:
        if key == 'export_all_internal_only':
            clean[key] = bool(values[key])
        else:
            clean[key] = int(values[key]) if key == 'max_zip_files' else float(values[key])
    if metadata_store.sqlite_enabled():
        metadata_store.save_document('settings', clean)
    else:
        with DB_LOCK:
            _save_json(SETTINGS_FILE, clean)
    app.config['MAX_CONTENT_LENGTH'] = int(clean['max_upload_gb'] * 1024 ** 3) or None

app = Flask(__name__, static_folder='static')

@app.after_request
def add_security_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    csp = response.headers.get('Content-Security-Policy')
    if not csp:
        response.headers['Content-Security-Policy'] = "frame-ancestors 'none'"
    elif 'frame-ancestors' not in csp.lower():
        response.headers['Content-Security-Policy'] = f"{csp}; frame-ancestors 'none'"
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    return response

# ===== СЕКРЕТНЫЙ КЛЮЧ (сохраняется между перезапусками, сессии не слетают) =====
SECRET_KEY_FILE = os.path.join(DATA_DIR, '.secret_key')

def _get_or_create_secret_key():
    if os.path.exists(SECRET_KEY_FILE):
        try:
            with open(SECRET_KEY_FILE, 'r', encoding='utf-8') as f:
                key = f.read().strip()
                if key:
                    try:
                        os.chmod(SECRET_KEY_FILE, 0o600)
                    except OSError:
                        pass
                    return key
        except OSError:
            pass
    os.makedirs(os.path.dirname(SECRET_KEY_FILE) or '.', exist_ok=True)
    key = secrets.token_hex(32)
    fd = os.open(SECRET_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(key)
    return key

app.secret_key = _get_or_create_secret_key()
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('ALBION_COOKIE_SECURE', '0').lower() in {'1', 'true', 'yes'}
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=int(os.environ.get('ALBION_SESSION_HOURS', '12')))

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
# Локальная сеть не ограничивается: лимит нужен против роботов из интернета.
# Запрос из тоннеля приходит с loopback-соседом, поэтому ключом лимита служит
# настоящий адрес клиента (при доверии forwarded-заголовкам), а не адрес тоннеля.
# Учёт запроса отключается флагом deduct_when, а не пустым ключом: пустой ключ
# заставлял flask-limiter писать ошибку в лог на каждый запрос из локальной сети.


# Сети, которые считаются локальными. Проверка перечислением, а не по
# «приватности» в терминах Python: ipaddress.is_private возвращает True и для
# документационных диапазонов (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24),
# а также для CGNAT — путать их с офисной сетью нельзя.
_LAN_NETWORKS = tuple(ipaddress.ip_network(network) for network in (
    '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8',
    '169.254.0.0/16', 'fc00::/7', 'fe80::/10', '::1/128',
))


def _address_is_lan(address):
    """Адрес принадлежит локальной сети (или самой машине)."""
    try:
        ip = ipaddress.ip_address(address)
    except (TypeError, ValueError):
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in network for network in _LAN_NETWORKS)


def rate_limit_key():
    """Ключ лимита — настоящий адрес клиента (после доверенного прокси — его)."""
    return request.remote_addr or 'unknown'


def rate_limit_applies():
    """Считать ли запрос в лимитах: только для клиентов из интернета."""
    peer = request.environ.get('REMOTE_ADDR', '')
    if _address_is_lan(peer):
        # Прямой запрос из локальной сети — не ограничиваем. Запрос от тоннеля
        # тоже приходит с приватного адреса (loopback), но его ограничивать нужно.
        return is_trusted_proxy_peer(peer)
    return True


limiter = Limiter(
    key_func=rate_limit_key,
    app=app,
    default_limits=["100 per hour", "20 per minute"],
    default_limits_deduct_when=lambda response: rate_limit_applies(),
    storage_uri=RATE_LIMIT_STORAGE_URI
)

# ===== ОБРАБОТЧИКИ ОШИБОК =====
@app.errorhandler(413)
def request_too_large(e):
    size = request.content_length or 0
    limit = app.config.get('MAX_CONTENT_LENGTH')
    app.logger.error('Слишком большой запрос на %s: %s от %s (лимит запроса: %s, частей формы: %s)',
                     request.path, format_size(size), request.remote_addr,
                     format_size(limit) if limit else 'без лимита',
                     app.config.get('MAX_FORM_PARTS'))
    if limit:
        message = f'Размер загрузки превышает лимит {format_size(limit)}'
    else:
        message = f'Запрос слишком большой ({format_size(size)})'
    return jsonify({'error': message}), 413

@app.errorhandler(429)
def too_many_requests(e):
    response = jsonify({'error': 'Слишком много запросов — повторите позже'})
    retry_after = getattr(e, 'retry_after', None)
    if retry_after:
        response.headers['Retry-After'] = str(retry_after)
    return response, 429

@app.errorhandler(500)
def internal_error(e):
    app.logger.error('Внутренняя ошибка на %s от %s', request.path, request.remote_addr, exc_info=True)
    return jsonify({'error': 'Внутренняя ошибка сервера — подробности в albion.log'}), 500

# ===== КОНФИГУРАЦИЯ =====
BASE_URL = os.environ.get('ALBION_BASE_URL', 'https://albion-rostov.cloudpub.ru').rstrip('/')
UPLOAD_FOLDER = os.path.join(DATA_DIR, 'uploads')
STATS_FILE = os.path.join(DATA_DIR, 'stats.json')
DB_FILE = os.path.join(DATA_DIR, 'files_db.json')
FOLDERS_DB_FILE = os.path.join(DATA_DIR, 'folders_db.json')
TOKENS_FILE = os.path.join(DATA_DIR, 'tokens.json')
TRASH_FILE = os.path.join(DATA_DIR, 'trash.json')
ZIPLINKS_DIR = os.path.join(DATA_DIR, 'ziplinks')
THUMBNAIL_DIR = os.path.join(DATA_DIR, 'thumbnails')
# Корзина: удалённые файлы хранятся отдельно от uploads и живут TRASH_RETENTION_DAYS.
TRASH_DIR = os.path.join(DATA_DIR, 'trash')
try:
    TRASH_RETENTION_DAYS = max(1, int(os.environ.get('ALBION_TRASH_DAYS', '30')))
except (TypeError, ValueError):
    TRASH_RETENTION_DAYS = 30
# Версия ассета (mtime файла) — чтобы после деплоя браузер не отдавал старый JS.
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
ADMIN_JS_PATH = os.path.join(STATIC_DIR, 'js', 'admin.js')
try:
    ASSET_VERSION = str(int(os.path.getmtime(ADMIN_JS_PATH)))
except OSError:
    ASSET_VERSION = '1'
# Размеры превью подобраны по фактической вёрстке (замер в браузере):
# mini рисуется 24–32 px (список, публичная страница) — 96 px покрывает DPR3;
# medium рисуется ~180 px в плитке десктопа и до 320 px на телефоне — 384 px
# даёт запас на DPR2 без лишнего веса.
THUMBNAIL_SIZES = {'mini': 96, 'medium': 384}
THUMBNAIL_QUALITY = 80
THUMBNAIL_LOCK = threading.Lock()

ALLOWED_EXTENSIONS = {'txt', 'pdf', 'png', 'jpg', 'jpeg', 'gif', 'zip', 'rar',
                      'docx', 'xlsx', 'mp4', 'mkv', 'doc', 'xls', 'pptx', '7z'}
IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp'}

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
# Один HTTP-запрос ограничен настройкой max_upload_gb (env-дефолт 50 ГиБ).
# 0 отключает лимит Flask; Waitress всё равно ограничивает тело запроса.
app.config['MAX_CONTENT_LENGTH'] = int(load_quota_settings()['max_upload_gb'] * 1024 ** 3) or None
# Werkzeug по умолчанию допускает всего 1000 частей формы (~500 файлов за раз).
# Каждый файл в загрузке папки = 2 части (file_N + path_N) — поднимаем лимит.
app.config['MAX_FORM_PARTS'] = 50000

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)
os.makedirs(THUMBNAIL_DIR, exist_ok=True)
os.makedirs(TRASH_DIR, exist_ok=True)

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
                  'robots.txt', 'favicon.ico', 'v', 'f', 'd', 'z', 'settings', 'static', 'api'}

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
def _sqlite_target(path):
    """Какой набор метаданных соответствует файлу JSON (для режима sqlite).

    Маршрутизация живёт здесь, чтобы ни один вызов _load_json/_save_json не
    остался в обход SQLite. auth.json в неё не входит: учётные записи
    переезжают в таблицу users на следующем этапе.
    """
    if not metadata_store.sqlite_enabled():
        return None
    normalized = os.path.normpath(path)
    mapping = {
        os.path.normpath(DB_FILE): ('files', None),
        os.path.normpath(FOLDERS_DB_FILE): ('folders', None),
        os.path.normpath(TOKENS_FILE): ('tokens', None),
        os.path.normpath(STATS_FILE): ('document', 'stats'),
        os.path.normpath(SETTINGS_FILE): ('document', 'settings'),
        os.path.normpath(TRASH_FILE): ('document', 'trash'),
    }
    return mapping.get(normalized)

def _load_json(path):
    """Загрузка JSON. Повреждённый файл переносится в .corrupt-бэкап,
    чтобы пустая база не затёрла данные при следующем сохранении."""
    target = _sqlite_target(path)
    if target:
        kind, name = target
        if kind == 'files':
            return metadata_store.load_files()
        if kind == 'folders':
            return metadata_store.load_folders()
        if kind == 'tokens':
            return metadata_store.load_tokens()
        return metadata_store.load_document(name, {})
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
    """Атомарная запись приватной JSON-БД (режим 0600) через tmp + replace.

    В режиме sqlite запись уходит в базу, файл JSON не создаётся.
    """
    target = _sqlite_target(path)
    if target:
        kind, name = target
        if kind == 'files':
            metadata_store.save_files(data)
        elif kind == 'folders':
            metadata_store.save_folders(data)
        elif kind == 'tokens':
            metadata_store.save_tokens(data)
        else:
            metadata_store.save_document(name, data)
        return
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)

def load_db():
    if metadata_store.sqlite_enabled():
        return metadata_store.load_files()
    return _load_json(DB_FILE)

def save_db(db):
    if metadata_store.sqlite_enabled():
        # Запись одним запросом: обновляются только изменившиеся записи,
        # удалённые из словаря удаляются из базы.
        metadata_store.save_files(db)
        return
    with DB_LOCK:
        _save_json(DB_FILE, db)

# ===== БАЗА ДАННЫХ ПАПОК =====
def load_folders():
    if metadata_store.sqlite_enabled():
        return metadata_store.load_folders()
    return _load_json(FOLDERS_DB_FILE)

def save_folders(folders):
    if metadata_store.sqlite_enabled():
        metadata_store.save_folders(folders)
        return
    with DB_LOCK:
        _save_json(FOLDERS_DB_FILE, folders)

def get_folder_path(folder_id, folders):
    if not folder_id or folder_id not in folders:
        return []
    path = []
    seen = set()
    current = folder_id
    # seen защищает от цикла в parent_id (испорченные данные не должны вешать сервер)
    while current and current in folders and current not in seen:
        seen.add(current)
        path.insert(0, folders[current]['name'])
        current = folders[current].get('parent_id')
    return path

def get_folder_physical_path(folder_id, folders):
    path_parts = get_folder_path(folder_id, folders)
    return os.path.join(UPLOAD_FOLDER, *path_parts) if path_parts else UPLOAD_FOLDER

def folder_subtree_ids(folder_id, folders):
    """Id папки и всех потомков. Цикл в parent_id не приводит к зацикливанию."""
    subtree = set()
    stack = [folder_id]
    while stack:
        current = stack.pop()
        if current in subtree or current not in folders:
            continue
        subtree.add(current)
        stack.extend(fid for fid, rec in folders.items() if rec.get('parent_id') == current)
    return subtree


def folder_subtree_unsafe(subtree, folders, db):
    """True, если в поддереве есть небезопасный путь файла или папки."""
    for rec in db.values():
        if rec.get('folder_id') in subtree:
            phys = rec.get('physical_path', rec['current_filename'])
            if safe_upload_path(phys) is None:
                return True
    for current_id in subtree:
        candidate = get_folder_physical_path(current_id, folders)
        rel = os.path.relpath(candidate, UPLOAD_FOLDER)
        if safe_upload_path(rel) is None:
            return True
    return False


def delete_folder_tree(folder_id, folders, db):
    """Удаляет поддерево папки: файлы (диск и БД) и каталоги. Меняет folders и db на месте."""
    files_to_delete = [internal_id for internal_id, record in db.items()
                       if record.get('folder_id') == folder_id]
    for internal_id in files_to_delete:
        physical_path = db[internal_id].get('physical_path', db[internal_id]['current_filename'])
        full_path = safe_upload_path(physical_path)
        if full_path is None:
            raise ValueError(f'Небезопасный путь файла: {physical_path!r}')
        if os.path.exists(full_path):
            os.remove(full_path)
        del db[internal_id]

    for subfolder_id in [fid for fid, f in folders.items() if f.get('parent_id') == folder_id]:
        delete_folder_tree(subfolder_id, folders, db)

    folder_candidate = get_folder_physical_path(folder_id, folders)
    folder_rel = os.path.relpath(folder_candidate, UPLOAD_FOLDER)
    physical_path = safe_upload_path(folder_rel)
    if physical_path is None:
        raise ValueError(f'Небезопасный путь папки: {folder_rel!r}')
    if os.path.exists(physical_path):
        shutil.rmtree(physical_path)
    folders.pop(folder_id, None)


def selected_folder_roots(folder_ids, folders):
    """Из запрошенных id оставляет только «корни»: вложенная папка уходит с родителем."""
    requested = [fid for fid in dict.fromkeys(folder_ids) if fid in folders]
    subtrees = {fid: folder_subtree_ids(fid, folders) for fid in requested}
    roots = [fid for fid in requested
             if not any(other != fid and fid in subtrees[other] for other in requested)]
    return roots, subtrees


def safe_upload_path(relative_path, allow_root=False):
    """Return a real path inside uploads, or None for unsafe DB paths/symlinks."""
    if not isinstance(relative_path, str) or not relative_path:
        return None
    root = os.path.realpath(UPLOAD_FOLDER)
    target = os.path.realpath(os.path.join(root, relative_path))
    try:
        if target == root:
            return root if allow_root else None
        if os.path.commonpath((root, target)) != root:
            return None
    except (ValueError, OSError):
        return None
    return target


def ensure_thumbnail(record, size):
    """Build and cache a WebP thumbnail keyed by record id and size."""
    if size not in THUMBNAIL_SIZES or not record.get('is_image'):
        return None
    source = safe_upload_path(record.get('physical_path', record.get('current_filename', '')))
    if not source or not os.path.isfile(source):
        return None
    max_edge = THUMBNAIL_SIZES[size]
    safe_id = re.sub(r'[^A-Za-z0-9_-]', '_', str(record.get('access_id', 'file')))
    # Размер и качество входят в имя файла: после смены параметров старый кэш
    # не отдаётся как актуальный, а просто перестаёт использоваться.
    cache_path = os.path.join(
        THUMBNAIL_DIR, f'{safe_id}-{size}-{max_edge}-q{THUMBNAIL_QUALITY}.webp')
    try:
        source_mtime = os.path.getmtime(source)
        if os.path.isfile(cache_path) and os.path.getmtime(cache_path) >= source_mtime:
            return cache_path
    except OSError:
        return None

    with THUMBNAIL_LOCK:
        try:
            source_mtime = os.path.getmtime(source)
            if os.path.isfile(cache_path) and os.path.getmtime(cache_path) >= source_mtime:
                return cache_path
            with Image.open(source) as image:
                image.seek(0)
                image = ImageOps.exif_transpose(image)
                image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
                has_alpha = image.mode in {'RGBA', 'LA'} or (
                    image.mode == 'P' and 'transparency' in image.info
                )
                image = image.convert('RGBA' if has_alpha else 'RGB')
                tmp_path = os.path.join(THUMBNAIL_DIR, f'.{uuid.uuid4()}.tmp')
                try:
                    image.save(tmp_path, 'WEBP', quality=THUMBNAIL_QUALITY, method=6)
                    os.replace(tmp_path, cache_path)
                finally:
                    if os.path.exists(tmp_path):
                        try:
                            os.remove(tmp_path)
                        except OSError:
                            pass
            return cache_path
        except Exception as exc:
            app.logger.warning('Не удалось создать превью %s (%s): %s', record.get('access_id'), size, exc)
            return None


def get_json_object():
    """Read a JSON object; arrays, scalars, null and malformed bodies are rejected."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None

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
                    'slug': make_short_slug(filename, db, exclude_id=internal_id),
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
    if metadata_store.sqlite_enabled():
        stats = metadata_store.load_document('stats')
    else:
        stats = _load_json(STATS_FILE)
    if not isinstance(stats, dict):
        return {'last_upload': None, 'last_export': None}
    stats.setdefault('last_upload', None)
    stats.setdefault('last_export', None)
    return stats

def save_stats(stats):
    if metadata_store.sqlite_enabled():
        metadata_store.save_document('stats', stats)
        return
    with DB_LOCK:
        _save_json(STATS_FILE, stats)

# ===== КОРЗИНА =====
# Файлы лежат в отдельном каталоге TRASH_DIR, индекс — документ trash
# (в sqlite — таблица documents). Удалённые записи уходят из files, поэтому
# листинги, статистика, ZIP и квоты их не видят.

def load_trash():
    data = _load_json(TRASH_FILE)
    return data if isinstance(data, dict) else {}

def save_trash(entries):
    with DB_LOCK:
        _save_json(TRASH_FILE, entries)

def trash_entry_path(entry_id):
    """Каталог записи корзины (создаётся при первом обращении)."""
    path = os.path.join(TRASH_DIR, str(entry_id))
    os.makedirs(path, exist_ok=True)
    return path

def move_file_to_trash(internal_id, record, db, trash):
    """Переносит файл в корзину и убирает запись из db.

    Возвращает id записи корзины или None, если переносить было нечего
    (файла нет на диске либо безопасность пути не подтверждена).
    """
    physical_path = record.get('physical_path', record.get('current_filename'))
    filepath = safe_upload_path(physical_path) if physical_path else None
    entry_id = str(uuid.uuid4())
    if filepath and os.path.exists(filepath):
        target_dir = trash_entry_path(entry_id)
        target_path = os.path.join(target_dir, os.path.basename(filepath))
        try:
            os.replace(filepath, target_path)
        except OSError as exc:
            # Другая ФС или гонка: откатываемся к жёсткому удалению.
            app.logger.warning('Корзина недоступна для %s (%s); удаляю безвозвратно', physical_path, exc)
            try:
                shutil.rmtree(target_dir, ignore_errors=True)
            except OSError:
                pass
            try:
                os.remove(filepath)
            except OSError:
                pass
            db.pop(internal_id, None)
            return None
        trash[entry_id] = {
            'id': entry_id,
            'internal_id': internal_id,
            'deleted_at': datetime.now(timezone.utc).isoformat(),
            'name': record.get('display_name') or record.get('original_filename') or record.get('current_filename'),
            'folder_id': record.get('folder_id'),
            'size': record.get('size', 0),
            'trash_path': os.path.relpath(target_path, DATA_DIR),
            'record': record,
        }
    db.pop(internal_id, None)
    return entry_id if entry_id in trash else None

def purge_expired_trash(trash=None):
    """Удаляет из корзины записи старше TRASH_RETENTION_DAYS. Возвращает число удалённых."""
    owns_trash = trash is None
    trash = load_trash() if owns_trash else trash
    cutoff = datetime.now(timezone.utc) - timedelta(days=TRASH_RETENTION_DAYS)
    expired = []
    for entry_id, entry in trash.items():
        try:
            deleted = datetime.fromisoformat(str(entry.get('deleted_at')))
        except (TypeError, ValueError):
            expired.append(entry_id)
            continue
        if deleted.tzinfo is None:
            deleted = deleted.replace(tzinfo=timezone.utc)
        if deleted < cutoff:
            expired.append(entry_id)
    for entry_id in expired:
        purge_trash_entry(entry_id, trash)
    if owns_trash and expired:
        save_trash(trash)
    return len(expired)

def purge_trash_entry(entry_id, trash):
    """Окончательно удаляет одну запись корзины (файл и индекс)."""
    entry = trash.pop(entry_id, None)
    if not entry:
        return False
    target_dir = os.path.join(TRASH_DIR, str(entry_id))
    shutil.rmtree(target_dir, ignore_errors=True)
    return True

def restore_trash_entries(entry_ids, folders, db, trash, view=None, client_folder=None):
    """Возвращает файлы из корзины (в исходную папку или в корень).

    Возвращает (restored_ids, rows_html): rows_html — строки для файлов,
    попавших в папку client_folder (для точечной вставки в интерфейсе).
    """
    restored_ids = []
    rows_html = ''
    for entry_id in entry_ids:
        entry = trash.get(entry_id)
        if not isinstance(entry, dict):
            continue
        record = entry.get('record') or {}
        target_folder = entry.get('folder_id')
        if target_folder and target_folder not in folders:
            target_folder = None   # исходной папки уже нет — кладём в корень
        target_dir = get_folder_physical_path(target_folder, folders)
        os.makedirs(target_dir, exist_ok=True)

        filename = record.get('current_filename') or entry.get('name') or 'file'
        existing = {r.get('current_filename') for r in db.values()
                    if r.get('folder_id') == target_folder}
        for name in os.listdir(target_dir):
            if os.path.isfile(os.path.join(target_dir, name)):
                existing.add(name)
        final_name = filename
        if final_name in existing:
            base, dot, ext = filename.rpartition('.')
            base = base if dot else filename
            counter = 1
            while True:
                candidate = f'{base}_{counter}.{ext}' if dot else f'{base}_{counter}'
                if candidate not in existing:
                    final_name = candidate
                    break
                counter += 1

        source = os.path.join(DATA_DIR, entry['trash_path']) if entry.get('trash_path') else None
        if not source or not os.path.isfile(source):
            purge_trash_entry(entry_id, trash)   # файл потерян — запись лишняя
            continue
        destination = os.path.join(target_dir, final_name)
        try:
            os.replace(source, destination)
        except OSError as exc:
            app.logger.error('Восстановление из корзины: %s -> %s: %s', source, destination, exc)
            continue

        used_slugs = {r.get('slug') for r in db.values()}
        used_slugs |= {r.get('custom_alias') for r in db.values()}
        used_slugs |= {f.get('slug') for f in folders.values()}
        slug = record.get('slug')
        if not slug or slug in used_slugs:
            slug = make_short_slug(final_name, db, folders=folders)

        # Исходный id сохраняем, если он свободен: строка в интерфейсе остаётся той же.
        new_id = entry.get('internal_id')
        if not new_id or new_id in db:
            new_id = str(uuid.uuid4())
        new_record = dict(record)
        new_record.update({
            'access_id': new_id,
            'current_filename': final_name,
            'physical_path': os.path.relpath(destination, UPLOAD_FOLDER),
            'folder_id': target_folder,
            'slug': slug,
            'size': os.path.getsize(destination),
            'mtime': datetime.fromtimestamp(os.path.getmtime(destination)).strftime('%d.%m.%Y %H:%M'),
        })
        db[new_id] = new_record
        purge_trash_entry(entry_id, trash)
        restored_ids.append(new_id)
        if view and target_folder == client_folder:
            rows_html += str(render_file_row(file_view_record(new_id, new_record), view))
    return restored_ids, rows_html

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

def url_path(path):
    """Encode a stored path for use in a URL while preserving directory separators."""
    return urllib.parse.quote(path, safe='/')

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

def file_view_record(internal_id, record):
    """Данные файла для шаблона (форма, которую ждёт разметка строки)."""
    physical_path = record.get('physical_path', record['current_filename'])
    return {
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
        'folder_id': record.get('folder_id'),
        # Ссылки с нормальной кириллицей (без URL-кодирования)
        'download_url': f"{BASE_URL}/{url_path(physical_path)}",
        'view_url': f"{BASE_URL}/v/{url_path(physical_path)}",
        # Thumbnail requests must not inflate view_count.
        'thumb_url': f"/thumb/mini/{internal_id}",
        'thumb_medium_url': f"/thumb/medium/{internal_id}",
        'slug': record.get('slug', ''),
        # Короткая латинизированная ссылка (переживает переименование)
        'short_url': f"{BASE_URL}/{record['slug']}" if record.get('slug') else f"{BASE_URL}/{url_path(physical_path)}",
    }

def folder_view_record(fid, folders, stats_map):
    """Данные папки для шаблона (форма разметки строки папки)."""
    folder = folders[fid]
    f_count, f_size, f_subdirs = stats_map.get(fid, (0, 0, 0))
    return {
        'id': fid,
        'name': folder['name'],
        'created': folder.get('created', ''),
        'slug': folder.get('slug', ''),
        'file_count': f_count,
        'subdir_count': f_subdirs,
        'size': format_size(f_size),
    }

def get_files_in_folder(folder_id):
    db = load_db()
    files = []
    for internal_id, record in db.items():
        if record.get('folder_id') == folder_id:
            physical_path = record.get('physical_path', record['current_filename'])
            filepath = safe_upload_path(physical_path)
            if filepath and os.path.isfile(filepath):
                files.append(file_view_record(internal_id, record))
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

def current_storage_bytes(db=None):
    db = db if db is not None else load_db()
    return sum(record.get('size', 0) for record in db.values())

def upload_capacity_error(incoming_bytes, db=None):
    """Return (HTTP status, message) if an upload cannot fit, else None.
    incoming_bytes=None means chunked/unknown; quotas require Content-Length.
    Values are re-read on every request so admin settings apply immediately."""
    limits = load_quota_settings()
    max_upload = int(float(limits['max_upload_gb']) * 1024 ** 3)
    storage_quota = int(float(limits['storage_quota_gb']) * 1024 ** 3)
    min_free = int(float(limits['min_free_gb']) * 1024 ** 3)
    if incoming_bytes is None:
        if storage_quota or min_free or max_upload:
            return 411, 'Для загрузки требуется заголовок Content-Length (chunked uploads отключены)'
        incoming_bytes = 0
    incoming_bytes = max(0, int(incoming_bytes))
    if max_upload and incoming_bytes > max_upload:
        return 413, f'Размер загрузки превышает лимит {format_size(max_upload)}'
    if storage_quota:
        used = current_storage_bytes(db)
        if used + incoming_bytes > storage_quota:
            return 507, f'Квота хранилища исчерпана ({format_size(storage_quota)})'
    if min_free:
        free = shutil.disk_usage(UPLOAD_FOLDER).free
        if free - incoming_bytes < min_free:
            return 507, f'Недостаточно свободного места: требуется резерв {format_size(min_free)}'
    return None

def zip_capacity_error(files, target_dir=None):
    """files: iterable of (source_path, arcname). Return (status, message) or None."""
    limits = load_quota_settings()
    max_zip = int(float(limits['max_zip_gb']) * 1024 ** 3)
    max_files = int(limits['max_zip_files'])
    min_free = int(float(limits['min_free_gb']) * 1024 ** 3)
    files = list(files)
    count = len(files)
    if max_files and count > max_files:
        return 413, f'Слишком много файлов для архива: {count} (лимит {max_files})'
    total = sum(os.path.getsize(path) for path, _ in files)
    if max_zip and total > max_zip:
        return 413, f'Размер архива превышает лимит {format_size(max_zip)}'
    if min_free:
        target = target_dir or (ZIPLINKS_DIR if os.path.isdir(ZIPLINKS_DIR) else UPLOAD_FOLDER)
        free = shutil.disk_usage(target).free
        if free - total < min_free:
            return 507, f'Недостаточно свободного места для архива (резерв {format_size(min_free)})'
    return None

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
    visiting = set()
    def stats(fid):
        if fid in memo:
            return memo[fid]
        if fid in visiting:   # цикл в parent_id: не уходим в бесконечную рекурсию
            return (0, 0, 0)
        visiting.add(fid)
        count, size = files_agg.get(fid, (0, 0))
        subdirs = 0
        for ch in children.get(fid, []):
            c, s, sd = stats(ch)
            count += c
            size += s
            subdirs += 1 + sd
        visiting.discard(fid)
        memo[fid] = (count, size, subdirs)
        return memo[fid]

    for fid in folders:
        stats(fid)
    return memo

# ===== РАЗМЕТКА СТРОК (единый источник для страницы и фрагментов) =====
# Одни и те же шаблоны используются и при полной отрисовке страницы (через
# Jinja-глобалы folder_row_html/file_row_html), и при точечных обновлениях:
# после загрузки/переименования/создания папки сервер возвращает готовый HTML строки.
_ROW_TEMPLATES = {
    'folder_table': """<tr class="folder-row" tabindex="0" data-folder-id="{{ folder.id }}" ondragover="handleDragOver(event)" ondragleave="handleDragLeave(event)" ondrop="handleDropOnFolder(event, '{{ folder.id }}')">
    <td class="checkbox-cell"><input type="checkbox" class="folder-checkbox" data-folder-id="{{ folder.id }}" aria-label="Выбрать папку {{ folder.name }}" onchange="updateSelection(this)"></td>
    <td class="link-cell"><button class="btn btn-sm btn-icon" onclick='copyToClipboard("{{ base_url }}/d/{{ folder.slug or folder.id }}")' title="Копировать ссылку на папку" aria-label="Копировать ссылку на папку">🔗</button></td>
    <td class="icon-cell"><span class="folder-icon">📁</span></td>
    <td data-label="Имя"><a href="/f/{{ folder.id }}" style="font-weight: 600;">{{ folder.name }}</a></td>
    <td class="size-col" data-label="Размер">{% if folder.file_count %}{{ folder.size }}{% else %}—{% endif %}</td>
    <td class="date-col" data-label="Изменён">{{ folder.created }}</td>
    <td class="type-col" data-label="Тип">Папка · 📄 {{ folder.file_count }} · 📁 {{ folder.subdir_count }}</td>
    <td class="dl-col" data-label="Скачивания">—</td>
    <td class="actions-cell" data-label="Действия">
        <button class="btn btn-sm btn-icon btn-icon-danger" onclick='confirmDeleteFolder("{{ folder.id }}", {{ folder.name | tojson }})' title="Удалить папку" aria-label="Удалить папку {{ folder.name }}">🗑️</button>
    </td>
</tr>""",
    'file_table': """<tr class="file-row" tabindex="0" draggable="true" ondragstart="handleDragStart(event, '{{ f.internal_id }}')" ondragend="handleDragEnd(event)" data-file-id="{{ f.internal_id }}">
    <td class="checkbox-cell"><input type="checkbox" class="file-checkbox" data-file-id="{{ f.internal_id }}" aria-label="Выбрать файл {{ f.name }}" onchange="updateSelection(this)"></td>
    <td class="link-cell"><button class="btn btn-sm btn-icon" onclick="copyToClipboard('{{ f.short_url }}')" title="Копировать короткую ссылку" aria-label="Копировать короткую ссылку">🔗</button></td>
    <td class="icon-cell">
        {% if f.is_image %}
            <img src="{{ f.thumb_url }}" class="thumb-sm" loading="lazy" decoding="async" alt="">
        {% else %}
            <span class="emoji-icon">{{ f.icon }}</span>
        {% endif %}
    </td>
    <td data-label="Имя">
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
    <td class="size-col" data-label="Размер">{{ f.size }}</td>
    <td class="date-col" data-label="Изменён">{{ f.mtime }}</td>
    <td class="type-col" data-label="Тип">{{ f.ext }}</td>
    <td class="dl-col" data-label="Скачивания"><span class="dl-badge">⬇ {{ f.download_count }} | 👁 {{ f.view_count }}</span></td>
    <td class="actions-cell" data-label="Действия">
        {% if can_move_up %}
        <button class="btn btn-sm btn-icon" onclick="moveFileUp('{{ f.internal_id }}')" title="Переместить на уровень выше" aria-label="Переместить на уровень выше">⬆️</button>
        {% endif %}
        <button class="btn btn-sm btn-icon" onclick='openEditModal("{{ f.internal_id }}", {{ f.name | tojson }}, {{ f.current_filename | tojson }}, {{ f.custom_alias | tojson }}, "{{ f.folder_id or '' }}")' title="Редактировать" aria-label="Редактировать {{ f.name }}">✏️</button>
        <button class="btn btn-sm btn-icon btn-icon-danger" onclick='confirmDelete("{{ f.internal_id }}", {{ f.name | tojson }})' title="Удалить" aria-label="Удалить {{ f.name }}">🗑️</button>
    </td>
</tr>""",
    'folder_grid': """<div class="tile" data-folder-id="{{ folder.id }}" ondragover="handleDragOver(event)" ondragleave="handleDragLeave(event)" ondrop="handleDropOnFolder(event, '{{ folder.id }}')">
    <div class="tile-checkbox"><input type="checkbox" class="folder-checkbox" data-folder-id="{{ folder.id }}" aria-label="Выбрать папку {{ folder.name }}" onchange="updateSelection(this)"></div>
    <a href="/f/{{ folder.id }}" class="tile-folder">
        <div class="tile-folder-icon">📁</div>
        <div class="tile-folder-name">{{ folder.name }}</div>
    </a>
    <div class="tile-name row-actions">
        <button class="btn btn-sm btn-icon" onclick='copyToClipboard("{{ base_url }}/d/{{ folder.slug or folder.id }}")' title="Копировать ссылку на папку" aria-label="Копировать ссылку на папку">🔗</button>
        <button class="btn btn-sm btn-icon btn-icon-danger" onclick='confirmDeleteFolder("{{ folder.id }}", {{ folder.name | tojson }})' title="Удалить" aria-label="Удалить папку {{ folder.name }}">🗑️</button>
    </div>
</div>""",
    'file_grid': """<div class="tile" draggable="true" ondragstart="handleDragStart(event, '{{ f.internal_id }}')" ondragend="handleDragEnd(event)" data-file-id="{{ f.internal_id }}">
    <div class="tile-checkbox"><input type="checkbox" class="file-checkbox" data-file-id="{{ f.internal_id }}" aria-label="Выбрать файл {{ f.name }}" onchange="updateSelection(this)"></div>
    <div class="tile-actions">
        {% if can_move_up %}
        <button onclick="moveFileUp('{{ f.internal_id }}')" title="Переместить выше" aria-label="Переместить {{ f.name }} на уровень выше">⬆️</button>
        {% endif %}
        <button onclick='openEditModal("{{ f.internal_id }}", {{ f.name | tojson }}, {{ f.current_filename | tojson }}, {{ f.custom_alias | tojson }}, "{{ f.folder_id or '' }}")' title="Редактировать" aria-label="Редактировать {{ f.name }}">✏️</button>
        <button onclick="copyToClipboard('{{ f.short_url }}')" title="Копировать короткую ссылку" aria-label="Копировать короткую ссылку">🔗</button>
        <button class="del-btn" onclick='confirmDelete("{{ f.internal_id }}", {{ f.name | tojson }})' title="Удалить" aria-label="Удалить {{ f.name }}">🗑️</button>
    </div>
    {% if f.is_image %}
        <a href="{{ f.view_url }}" target="_blank" class="tile-preview-wrap">
            <img src="{{ f.thumb_medium_url }}" class="tile-preview" loading="lazy" decoding="async" alt="">
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
</div>""",
    'folder_list': """<div class="list-item folder-item" data-folder-id="{{ folder.id }}" ondragover="handleDragOver(event)" ondragleave="handleDragLeave(event)" ondrop="handleDropOnFolder(event, '{{ folder.id }}')">
    <input type="checkbox" class="folder-checkbox" data-folder-id="{{ folder.id }}" aria-label="Выбрать папку {{ folder.name }}" onchange="updateSelection(this)">
    <button class="btn btn-sm btn-icon list-link" onclick='copyToClipboard("{{ base_url }}/d/{{ folder.slug or folder.id }}")' title="Копировать ссылку на папку" aria-label="Копировать ссылку на папку">🔗</button>
    <span class="emoji-icon-sm">📁</span>
    <div class="list-name-wrap">
        <a href="/f/{{ folder.id }}" class="list-name" style="font-weight: 600;">{{ folder.name }}</a>
    </div>
    <span class="list-date">{{ folder.created }}</span>
    <div class="list-actions">
        <button class="btn btn-sm btn-icon btn-icon-danger" onclick='confirmDeleteFolder("{{ folder.id }}", {{ folder.name | tojson }})' title="Удалить" aria-label="Удалить папку {{ folder.name }}">🗑️</button>
    </div>
</div>""",
    'file_list': """<div class="list-item" draggable="true" ondragstart="handleDragStart(event, '{{ f.internal_id }}')" ondragend="handleDragEnd(event)" data-file-id="{{ f.internal_id }}">
    <input type="checkbox" class="file-checkbox" data-file-id="{{ f.internal_id }}" aria-label="Выбрать файл {{ f.name }}" onchange="updateSelection(this)">
    <button class="btn btn-sm btn-icon list-link" onclick="copyToClipboard('{{ f.short_url }}')" title="Копировать короткую ссылку" aria-label="Копировать короткую ссылку">🔗</button>
    {% if f.is_image %}
        <img src="{{ f.thumb_url }}" class="thumb-xs" loading="lazy" decoding="async" alt="">
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
        {% if can_move_up %}
        <button class="btn btn-sm btn-icon" onclick="moveFileUp('{{ f.internal_id }}')" title="Переместить выше" aria-label="Переместить {{ f.name }} на уровень выше">⬆️</button>
        {% endif %}
        <button class="btn btn-sm btn-icon" onclick='openEditModal("{{ f.internal_id }}", {{ f.name | tojson }}, {{ f.current_filename | tojson }}, {{ f.custom_alias | tojson }}, "{{ f.folder_id or '' }}")' title="Редактировать" aria-label="Редактировать {{ f.name }}">✏️</button>
        <button class="btn btn-sm btn-icon btn-icon-danger" onclick='confirmDelete("{{ f.internal_id }}", {{ f.name | tojson }})' title="Удалить" aria-label="Удалить {{ f.name }}">🗑️</button>
    </div>
</div>""",
}

BROWSE_VIEWS = ('table', 'grid', 'list')


def render_file_row(record, view='table'):
    """HTML одной строки файла для нужного представления."""
    template = _ROW_TEMPLATES.get(f'file_{view}') or _ROW_TEMPLATES['file_table']
    html = render_template_string(template, f=record, base_url=BASE_URL,
                                  can_move_up=bool(record.get('folder_id')))
    return Markup(html)


def render_folder_row(folder, view='table'):
    """HTML одной строки папки для нужного представления."""
    template = _ROW_TEMPLATES.get(f'folder_{view}') or _ROW_TEMPLATES['folder_table']
    html = render_template_string(template, folder=folder, base_url=BASE_URL)
    return Markup(html)


# ===== HTML ШАБЛОН =====
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="csrf-token" content="{{ csrf_token }}">
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
        .header-actions { display: flex; align-items: center; gap: 8px; margin-left: auto; flex-wrap: wrap; }
        .user-chip { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 14px; background: rgba(255,255,255,.14); font-size: 13px; white-space: nowrap; }
        .role-badge { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .3px; padding: 1px 7px; border-radius: 9px; background: rgba(255,255,255,.28); }

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
        .btn:disabled, a.btn[aria-disabled="true"] { opacity: 0.6; cursor: not-allowed; }
        @keyframes albion-spin { to { transform: rotate(360deg); } }
        .btn.is-busy::before { content: ''; display: inline-block; width: 12px; height: 12px; margin-right: 6px; border: 2px solid currentColor; border-right-color: transparent; border-radius: 50%; animation: albion-spin 0.7s linear infinite; vertical-align: -2px; }
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

        .batch-result { padding: 8px 12px; margin-top: 8px; background: #ecfdf5; border: 1px solid #a7f3d0; border-radius: 6px; font-size: 13px; }
        .batch-result a { color: #047857; font-weight: 600; }
        .batch-result.is-error { background: #fef2f2; border-color: #fecaca; color: #b91c1c; }
        .batch-actions { display: none; align-items: center; gap: 10px; padding: 10px 15px; background: #eff6ff; border-radius: 6px; margin-bottom: 12px; }
        .batch-actions.active { display: flex; }
        .batch-actions .count { font-weight: 600; color: #1e3a8a; }

        .file-filters { display: flex; align-items: center; gap: 7px; margin-bottom: 12px; flex-wrap: wrap; }
        .file-filters input[type="search"], .file-filters select { padding: 6px 9px; border: 1px solid #cbd5e1; border-radius: 5px; font-size: 13px; background: white; }
        .file-filters input[type="search"] { min-width: 230px; flex: 1 1 230px; }
        .file-filters .filter-result { color: #64748b; font-size: 12px; margin-left: auto; }

        .view-switcher { display: flex; gap: 5px; margin-bottom: 12px; padding: 4px; background: #f1f5f9; border-radius: 6px; width: fit-content; flex-shrink: 0; flex-wrap: wrap; }
        .view-btn { background: transparent; color: #475569; padding: 6px 14px; border: none; border-radius: 4px; cursor: pointer; font-size: 13px; font-weight: 500; margin: 0; transition: all 0.15s; }
        .view-btn:hover { background: #e2e8f0; }
        .view-btn.active { background: white; color: #1e3a8a; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }

        .files-content { flex: 1; overflow-y: auto; overflow-x: hidden; min-height: 0; }

        .files-table { width: 100%; border-collapse: collapse; font-size: 13px; }
        .files-table thead { position: sticky; top: 0; background: #f8fafc; z-index: 1; }
        .files-table th { text-align: left; padding: 8px 12px; color: #1e3a8a; font-weight: 600; border-bottom: 2px solid #e2e8f0; font-size: 12px; text-transform: uppercase; letter-spacing: 0.3px; }
        .files-table td { padding: 6px 12px; border-bottom: 1px solid #f1f5f9; vertical-align: middle; }
        .files-table tr:hover { background: #f8fafc; }
        .files-table tr:focus-visible { outline: 2px solid #f59e0b; outline-offset: -2px; background: #eff6ff; }
        .files-table .checkbox-cell { width: 40px; text-align: center; }
        .files-table .icon-cell { width: 40px; text-align: center; }
        .files-table .link-cell { width: 40px; text-align: center; }
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
        .folder-row.selected { background: #dbeafe !important; }
        .folder-row.drag-over { background: #dbeafe !important; border: 2px dashed #2563eb; }
        .file-row.dragging { opacity: 0.5; }
        .file-row.selected { background: #dbeafe !important; }

        .files-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 12px; padding: 4px; }
        .tile { background: white; border: 1px solid #e2e8f0; border-radius: 8px; overflow: hidden; transition: all 0.15s; display: flex; flex-direction: column; position: relative; }
        .tile:hover { border-color: #2563eb; box-shadow: 0 4px 12px rgba(37, 99, 235, 0.15); transform: translateY(-2px); }
        .tile:focus-within { border-color: #2563eb; box-shadow: 0 0 0 2px #f59e0b; }
        .tile.selected { border-color: #2563eb; background: #dbeafe; }
        .tile.drag-over { border: 2px dashed #2563eb; background: #dbeafe; }
        .tile.dragging { opacity: 0.5; }
        .tile-checkbox { position: absolute; top: 5px; left: 5px; z-index: 3; }
        .tile-actions { position: absolute; top: 5px; right: 5px; display: flex; gap: 4px; opacity: 0; transition: opacity 0.2s; z-index: 2; }
        .tile:hover .tile-actions, .tile:focus-within .tile-actions { opacity: 1; }
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
        .list-link { flex-shrink: 0; padding: 2px 6px; }
        .emoji-icon-sm { font-size: 18px; flex-shrink: 0; width: 24px; text-align: center; }
        .list-name-wrap { flex: 1; display: flex; align-items: center; gap: 8px; min-width: 0; }
        .list-name { color: #1e293b; text-decoration: none; word-break: break-all; }
        .list-name:hover { color: #2563eb; }
        .list-size { color: #64748b; white-space: nowrap; font-size: 12px; }
        .list-date { color: #94a3b8; white-space: nowrap; font-size: 12px; }
        .list-actions { display: flex; gap: 4px; opacity: 0; transition: opacity 0.2s; }
        .list-item:hover .list-actions, .list-item:focus-within .list-actions { opacity: 1; }

        .status-bar { background: #1e293b; color: #cbd5e1; padding: 0 25px; height: 38px; display: flex; align-items: center; gap: 20px; font-size: 12px; flex-shrink: 0; border-top: 1px solid #0f172a; overflow-x: auto; }
        .status-item { display: flex; align-items: center; gap: 6px; white-space: nowrap; }
        .status-item .label { color: #64748b; }
        .status-item .value { color: #f1f5f9; font-weight: 500; }
        .status-bar .separator { color: #475569; }
        .disk-bar { width: 80px; height: 8px; background: #475569; border-radius: 4px; overflow: hidden; }
        .disk-bar-fill { height: 100%; border-radius: 4px; transition: width 0.3s; }

        .toast-stack { position: fixed; top: 80px; right: 20px; display: flex; flex-direction: column; gap: 8px; z-index: 1000; max-width: min(420px, calc(100vw - 40px)); }
        .toast { display: flex; align-items: center; gap: 10px; padding: 12px 20px; background: #28a745; color: white; border-radius: 6px; box-shadow: 0 4px 12px rgba(0,0,0,0.2); opacity: 0; transform: translateY(-6px); transition: opacity 0.3s, transform 0.3s; font-size: 13px; font-weight: 500; }
        .toast-action { background: rgba(255,255,255,.28); color: inherit; border: none; border-radius: 4px; padding: 3px 10px; cursor: pointer; font-weight: 600; font-size: 12px; white-space: nowrap; }
        .toast-action:hover { background: rgba(255,255,255,.45); }
        .toast-action:focus-visible { outline: 2px solid #fff; outline-offset: 1px; }
        .toast.show { opacity: 1; transform: none; }
        .toast.error { background: #dc3545; }

        .empty-state { text-align: center; padding: 40px 20px; color: #94a3b8; font-size: 14px; }
        .empty-state .icon { font-size: 48px; margin-bottom: 10px; }

        .files-content::-webkit-scrollbar { width: 8px; }
        .files-content::-webkit-scrollbar-track { background: #f1f5f9; }
        .files-content::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 4px; }
        .files-content::-webkit-scrollbar-thumb:hover { background: #94a3b8; }
        .lazy-loader { display: flex; align-items: center; justify-content: center; gap: 10px; padding-top: 12px; color: #64748b; font-size: 12px; }
        .lazy-loader[hidden] { display: none; }
        .lazy-status { color: #64748b; font-size: 12px; }
        .pagination { display: flex; align-items: center; justify-content: center; gap: 5px; padding-top: 12px; flex-wrap: wrap; }
        .page-link, .lazy-more { color: #2563eb; border: 1px solid #dbeafe; border-radius: 4px; padding: 4px 9px; text-decoration: none; font-size: 12px; background: #fff; cursor: pointer; }
        .page-link:hover, .lazy-more:hover { background: #eff6ff; }
        .page-link.active { background: #2563eb; border-color: #2563eb; color: white; font-weight: 600; }
        .page-ellipsis { color: #94a3b8; padding: 0 2px; }

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
        button:focus-visible, .btn:focus-visible, a:focus-visible,
        input:focus-visible, select:focus-visible {
            outline: 2px solid #f59e0b;
            outline-offset: 2px;
        }

        @media (max-width: 700px) {
            .top-row { flex-direction: column; }
            .header { padding: 12px 15px; flex-wrap: wrap; }
            .header-title { font-size: 18px; }
            .main { padding: 10px; }
            .file-filters input[type="search"] { min-width: 100%; flex-basis: 100%; }
            .file-filters .filter-result { margin-left: 0; }
            .files-table, .files-table tbody, .files-table tr, .files-table td { display: block; width: 100%; box-sizing: border-box; }
            .files-table { min-width: 0; }
            .files-table thead { display: none; }
            .files-table tr { border: 1px solid #e2e8f0; border-radius: 8px; margin-bottom: 10px; padding: 8px 10px; background: #fff; }
            .files-table td { border: none; padding: 3px 0; }
            .files-table td[data-label]::before { content: attr(data-label) ": "; color: #64748b; font-size: 12px; }
            .files-table td.checkbox-cell, .files-table td.link-cell, .files-table td.icon-cell { display: inline-block; width: auto; vertical-align: middle; padding-right: 6px; }
            .files-table td.actions-cell { padding-top: 6px; }
            .batch-actions { flex-wrap: wrap; }
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
        {% set me = current_user() %}
        <div class="header-actions">
            {% if me %}
            <span class="user-chip" title="Вы вошли как {{ me.login }}">
                👤 {{ me.display_name or me.login }}
                <span class="role-badge">{{ 'админ' if me.role == 'admin' else 'пользователь' }}</span>
            </span>
            {% endif %}
            <a href="/password" class="btn btn-sm" style="margin:0; background:rgba(255,255,255,.18);" title="Сменить свой пароль" aria-label="Сменить свой пароль">🔒 Пароль</a>
            {% if is_admin() %}
            <a href="/settings/tokens" class="btn btn-sm" style="margin:0; background:rgba(255,255,255,.18);" title="API-токены" aria-label="API-токены">🔑 Токены</a>
            <a href="/trash" class="btn btn-sm" style="margin:0; background:rgba(255,255,255,.18);" title="Корзина" aria-label="Корзина">🗑️ Корзина</a>
            <a href="/settings/users" class="btn btn-sm" style="margin:0; background:rgba(255,255,255,.18);" title="Пользователи" aria-label="Пользователи">👥 Пользователи</a>
            <a href="/settings" class="btn btn-sm" style="margin:0; background:rgba(255,255,255,.18);" title="Настройки квот" aria-label="Настройки">⚙️ Настройки</a>
            {% endif %}
            <a href="/logout" class="btn btn-sm" style="margin:0; background:rgba(255,255,255,.18);" title="Завершить сессию" aria-label="Выйти">Выйти</a>
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
            <h2>📂 {{ 'Корень' if not current_folder else current_folder.name }} <span style="color:#64748b; font-weight:400; font-size:13px;">(<span id="titleFileCount">{{ total_files }}</span> файлов, <span id="titleFolderCount">{{ subfolders|length }}</span> папок)</span>
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

            <form class="file-filters" method="get" action="{{ request.path }}">
                <input type="search" name="q" value="{{ q }}" placeholder="Поиск по имени, ссылке или алиасу (Enter — применить)" aria-label="Поиск файлов">
                <select name="ext" aria-label="Тип файла">
                    <option value="">Все типы</option>
                    {% for option in ext_options %}
                    <option value="{{ option }}" {% if ext == option %}selected{% endif %}>{{ option|upper }}</option>
                    {% endfor %}
                </select>
                <select name="sort" aria-label="Сортировка">
                    {% for value, label in sort_options %}
                    <option value="{{ value }}" {% if sort == value %}selected{% endif %}>{{ label }}</option>
                    {% endfor %}
                </select>
                <select name="per_page" aria-label="Файлов на странице">
                    {% for option in per_page_options %}
                    <option value="{{ option }}" {% if per_page == option %}selected{% endif %}>{{ option }} на странице</option>
                    {% endfor %}
                </select>
                <input type="hidden" name="view" value="{{ view }}">
                <button class="btn btn-sm" type="submit">Применить</button>
                <a class="btn btn-sm btn-secondary" href="{{ reset_url }}">Сбросить</a>
                <span class="filter-result">Найдено: <strong id="foundCount">{{ total_files }}</strong>{% if files|length < total_files %} · показано <span id="shownCount">{{ files|length }}</span>{% endif %}</span>
            </form>

            <div class="batch-actions" id="batchActions">
                <span class="count">Выбрано: <span id="selectedCount">0</span></span>
                <button class="btn btn-sm btn-success" id="batchZipBtn" data-idle-label="⬇ Скачать архив" onclick="executeBatchZip()">⬇ Скачать архив</button>
                <button class="btn btn-sm btn-danger file-selection-action" id="batchDeleteBtn" data-idle-label="🗑️ Удалить выбранные" onclick="batchDelete()">🗑️ Удалить выбранные</button>
                <button class="btn btn-sm file-selection-action" onclick="openBatchMoveModal()">📁 Переместить</button>
                <button class="btn btn-sm btn-secondary" onclick="clearSelection()">✕ Снять выделение</button>
            </div>
            <div class="batch-result" id="batchResult" role="status" aria-live="polite" hidden></div>

            <div class="view-switcher">
                <button class="view-btn {% if view == 'table' %}active{% endif %}" data-view="table" aria-pressed="{{ 'true' if view == 'table' else 'false' }}">🗂 Таблица</button>
                <button class="view-btn {% if view == 'grid' %}active{% endif %}" data-view="grid" aria-pressed="{{ 'true' if view == 'grid' else 'false' }}">🖼 Плитка</button>
                <button class="view-btn {% if view == 'list' %}active{% endif %}" data-view="list" aria-pressed="{{ 'true' if view == 'list' else 'false' }}">📋 Список</button>
                <button type="button" class="view-btn" id="selectAllBtn" onclick="selectAllItems()" title="Выбрать все на текущей странице" aria-label="Выбрать все на странице">☑️ Выбрать все</button>
                {% if total_files > files|length %}
                <button type="button" class="view-btn" id="selectAllFoundBtn" onclick="selectAllFound()"
                        data-idle-label="🔎 Выбрать все найденные ({{ total_files }})"
                        title="Выделить все файлы и папки по текущему фильтру, включая не подгруженные"
                        aria-label="Выбрать все найденные">{{ '🔎 Выбрать все найденные (%d)' % total_files }}</button>
                {% endif %}
            </div>

            <div class="files-content">
                {% if subfolders or files %}
                    {% if view == 'table' %}
                    <div id="view-table" class="view-container">
                        <table class="files-table">
                            <thead>
                                <tr>
                                    <th class="checkbox-cell"><input type="checkbox" id="selectAll" aria-label="Выбрать все на странице" onchange="toggleSelectAll()"></th>
                                    <th class="link-cell"></th>
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
                                {{ folder_row_html(folder, view) }}
                                {% endfor %}
                                {% for f in files %}
                                {{ file_row_html(f, view) }}
                                {% endfor %}
                            </tbody>
                        </table>
                    </div>
                    {% elif view == 'grid' %}
                    <div id="view-grid" class="view-container">
                        <div class="files-grid">
                            {% for folder in subfolders %}
                            {{ folder_row_html(folder, view) }}
                            {% endfor %}
                            {% for f in files %}
                            {{ file_row_html(f, view) }}
                            {% endfor %}
                        </div>
                    </div>
                    {% else %}
                    <div id="view-list" class="view-container">
                        <div class="files-list">
                            {% for folder in subfolders %}
                            {{ folder_row_html(folder, view) }}
                            {% endfor %}
                            {% for f in files %}
                            {{ file_row_html(f, view) }}
                            {% endfor %}
                        </div>
                    </div>
                    {% endif %}
                {% else %}
                    <div class="empty-state">
                        <div class="icon">{% if has_filters %}🔎{% else %}📭{% endif %}</div>
                        <div>{% if has_filters %}По фильтрам ничего не найдено. <a href="{{ reset_url }}">Сбросить фильтры</a>{% else %}Папка пуста{% endif %}</div>
                    </div>
                {% endif %}
                <div id="lazyLoader" class="lazy-loader" data-next-url="{{ next_url or '' }}"
                     data-view="{{ view }}" {% if not next_url %}hidden{% endif %}>
                    <button type="button" class="btn btn-sm btn-secondary" id="loadMoreBtn" onclick="loadNextPage()">Показать ещё</button>
                    <span id="lazyStatus" class="lazy-status"></span>
                </div>
            </div>
        </div>
    </div>

    <footer class="status-bar">
        <div class="status-item">
            <span class="label">📁 Файлов:</span>
            <span class="value" id="footerFileCount">{{ total_files }}</span>
        </div>
        <span class="separator">│</span>
        <div class="status-item">
            <span class="label">📂 Папок:</span>
            <span class="value" id="footerFolderCount">{{ subfolders|length }}</span>
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

    <div class="modal-overlay" id="editModal" role="dialog" aria-modal="true" aria-labelledby="editModalTitle" aria-hidden="true">
        <div class="modal">
            <h3 id="editModalTitle">✏️ Редактирование файла</h3>
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
                <button class="btn" id="editSaveBtn" data-idle-label="Сохранить" data-modal-primary onclick="saveFileEdit()">Сохранить</button>
            </div>
        </div>
    </div>

    <div class="modal-overlay" id="createFolderModal" role="dialog" aria-modal="true" aria-labelledby="createFolderModalTitle" aria-hidden="true">
        <div class="modal">
            <h3 id="createFolderModalTitle">➕ Новая папка</h3>
            <div class="form-group">
                <label>Имя папки</label>
                <input type="text" id="newFolderName" placeholder="Например: Отчёты">
            </div>
            <div class="modal-actions">
                <button class="btn btn-secondary" onclick="closeCreateFolderModal()">Отмена</button>
                <button class="btn" id="createFolderBtn" data-idle-label="Создать" data-modal-primary onclick="createFolder()">Создать</button>
            </div>
        </div>
    </div>

    <div class="modal-overlay" id="batchMoveModal" role="dialog" aria-modal="true" aria-labelledby="batchMoveModalTitle" aria-hidden="true">
        <div class="modal">
            <h3 id="batchMoveModalTitle">📁 Переместить выбранное</h3>
            <div class="form-group">
                <label>Выберите папку</label>
                <select id="batchMoveFolderId"></select>
            </div>
            <div class="modal-actions">
                <button class="btn btn-secondary" onclick="closeBatchMoveModal()">Отмена</button>
                <button class="btn" id="batchMoveBtn" data-idle-label="Переместить" data-modal-primary onclick="executeBatchMove()">Переместить</button>
            </div>
        </div>
    </div>

    <div class="modal-overlay" id="deleteModal" role="dialog" aria-modal="true" aria-labelledby="deleteModalTitle" aria-hidden="true">
        <div class="modal">
            <h3 id="deleteModalTitle">🗑️ Удаление файла</h3>
            <input type="hidden" id="deleteFileId">
            <p class="confirm-text">Удалить файл <span class="confirm-filename" id="deleteFileName"></span>?</p>
            <p class="confirm-text" style="color: #dc3545; font-size: 13px;">⚠️ Файл будет удалён безвозвратно</p>
            <div class="modal-actions">
                <button class="btn btn-secondary" onclick="closeDeleteModal()">Отмена</button>
                <button class="btn btn-danger" id="deleteFileBtn" data-idle-label="Удалить" data-modal-primary onclick="executeDelete()">Удалить</button>
            </div>
        </div>
    </div>

    <div class="modal-overlay" id="deleteFolderModal" role="dialog" aria-modal="true" aria-labelledby="deleteFolderModalTitle" aria-hidden="true">
        <div class="modal">
            <h3 id="deleteFolderModalTitle">🗑️ Удаление папки</h3>
            <input type="hidden" id="deleteFolderId">
            <p class="confirm-text">Удалить папку <span class="confirm-filename" id="deleteFolderName"></span>?</p>
            <p class="confirm-text" style="color: #dc3545; font-size: 13px;">⚠️ Будут удалены все файлы и подпапки внутри!</p>
            <div class="modal-actions">
                <button class="btn btn-secondary" onclick="closeDeleteFolderModal()">Отмена</button>
                <button class="btn btn-danger" id="deleteFolderBtn" data-idle-label="Удалить" data-modal-primary onclick="executeDeleteFolder()">Удалить</button>
            </div>
        </div>
    </div>

    <div class="toast-stack" id="toastStack" role="status" aria-live="polite"></div>

    <script>
        window.ALBION_CONFIG = {
            currentFolderId: {{ (current_folder_id or "") | tojson }},
            parentFolderId: {{ ((current_folder.parent_id or "") if current_folder else "") | tojson }},
            currentView: {{ view | tojson }},
            csrfToken: {{ csrf_token | tojson }},
            baseUrl: {{ base_url | tojson }}
        };
    </script>
    <script src="{{ url_for('static', filename='js/admin.js') }}?v={{ asset_version }}" defer></script>
</body>
</html>
"""

# ===== ПУБЛИЧНАЯ СТРАНИЦА ПАПКИ (без логина, по неугадываемому id) =====
PUBLIC_FOLDER_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
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
        .pagination { display: flex; align-items: center; justify-content: center; gap: 5px; padding-top: 14px; flex-wrap: wrap; }
        .page-link, .lazy-more { color: #2563eb; border: 1px solid #dbeafe; border-radius: 4px; padding: 4px 9px; text-decoration: none; font-size: 12px; background: #fff; cursor: pointer; }
        .page-link:hover, .lazy-more:hover { background: #eff6ff; }
        .page-link.active { background: #2563eb; border-color: #2563eb; color: white; font-weight: 600; }
        .page-ellipsis { color: #94a3b8; padding: 0 2px; }
        .lazy-more:focus-visible, .item a:focus-visible, .page-link:focus-visible { outline: 2px solid #f59e0b; outline-offset: 2px; }
        .lazy-more:disabled { opacity: 0.6; cursor: not-allowed; }
        .footer { text-align: center; color: #94a3b8; font-size: 11px; padding: 12px; }
        @media (max-width: 650px) {
            .header { padding: 12px 15px; }
            .item { flex-wrap: wrap; }
            .item .name { flex-basis: calc(100% - 50px); }
            .item .meta { margin-left: 40px; }
            .item .actions { margin-left: 40px; }
        }
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
                <div id="publicItems">
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
                        <img class="thumb" src="{{ f.thumb_url }}" loading="lazy" decoding="async" alt="">
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
                </div>
            {% else %}
                <div class="empty">
                    <div class="icon">📭</div>
                    <div>Нет файлов для публикации</div>
                </div>
            {% endif %}
        </div>
        <div id="publicLazyLoader" class="lazy-loader" data-next-url="{{ next_url or '' }}" {% if not next_url %}hidden{% endif %}>
            <button type="button" class="lazy-more" id="publicLoadMore" onclick="publicLoadNextPage()" aria-label="Показать ещё файлы">Показать ещё</button>
            <span id="publicLazyStatus" role="status" aria-live="polite"></span>
        </div>
        <div class="footer">Файлов: {{ total_files }}</div>
    </div>
    <script>
        const publicLoader = document.getElementById('publicLazyLoader');
        const publicStatus = document.getElementById('publicLazyStatus');
        const publicButton = document.getElementById('publicLoadMore');
        let publicNextUrl = publicLoader ? (publicLoader.dataset.nextUrl || '') : '';
        let publicLoading = false;
        async function publicLoadNextPage() {
            if (!publicNextUrl || publicLoading) return;
            publicLoading = true;
            publicButton.disabled = true;
            publicStatus.textContent = 'Загрузка…';
            try {
                const response = await fetch(publicNextUrl);
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
                const target = document.getElementById('publicItems');
                doc.querySelectorAll('#publicItems .item').forEach(node => {
                    target.appendChild(document.importNode(node, true));
                });
                const nextLoader = doc.getElementById('publicLazyLoader');
                publicNextUrl = nextLoader ? (nextLoader.dataset.nextUrl || '') : '';
                publicLoader.dataset.nextUrl = publicNextUrl;
                if (!publicNextUrl) {
                    publicLoader.hidden = true;
                    publicStatus.textContent = '';
                } else {
                    publicStatus.textContent = '';
                }
            } catch (err) {
                console.error('Ошибка публичной lazy-загрузки:', err);
                publicStatus.textContent = 'Не удалось загрузить ещё файлы';
            } finally {
                publicLoading = false;
                publicButton.disabled = false;
            }
        }
        if (publicNextUrl && 'IntersectionObserver' in window) {
            const observer = new IntersectionObserver(entries => {
                if (entries.some(entry => entry.isIntersecting)) publicLoadNextPage();
            }, { rootMargin: '400px' });
            observer.observe(publicLoader);
        } else if (publicNextUrl) {
            publicStatus.textContent = 'Прокрутите вниз или нажмите кнопку';
        }
    </script>
</body>
</html>
"""


# ===== НАСТРОЙКИ (только администратор) =====
SETTINGS_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="csrf-token" content="{{ csrf_token }}">
    <title>АЛЬБИОН: Настройки</title>
    <link rel="icon" type="image/png" href="{{ url_for('static', filename='logo.png') }}">
    <style>
        * { box-sizing: border-box; }
        body { font-family: 'Segoe UI', Arial, sans-serif; background: #f4f4f9; margin: 0; color: #1e293b; }
        .header { background: linear-gradient(135deg, #1e3a8a 0%, #2563eb 100%); color: white; padding: 15px 30px; display: flex; align-items: center; gap: 15px; }
        .header img { height: 42px; background: white; padding: 4px 7px; border-radius: 6px; }
        .header h1 { margin: 0; font-size: 20px; }
        .main { max-width: 760px; margin: 0 auto; padding: 22px 15px; }
        .card { background: white; border-radius: 8px; box-shadow: 0 1px 4px rgba(0,0,0,.08); padding: 20px 24px; }
        h2 { color: #1e3a8a; margin-top: 0; font-size: 18px; }
        .group { margin: 16px 0; }
        label { display: block; font-weight: 600; margin-bottom: 5px; font-size: 13px; }
        input { width: 100%; max-width: 260px; padding: 8px 10px; border: 1px solid #cbd5e1; border-radius: 5px; font-size: 14px; }
        .hint { color: #64748b; font-size: 12px; margin-top: 4px; }
        .actions { display: flex; gap: 10px; margin-top: 22px; }
        .btn { display: inline-block; padding: 8px 16px; border-radius: 5px; text-decoration: none; color: white; background: #2563eb; border: none; cursor: pointer; font-size: 13px; font-weight: 500; }
        .btn:hover { background: #1e3a8a; }
        .btn-secondary { background: #f1f5f9; color: #475569; }
        .btn-secondary:hover { background: #e2e8f0; }
        .toast { background: #dcfce7; color: #166534; padding: 10px 14px; border-radius: 6px; margin-bottom: 15px; font-size: 13px; }
        .error { background: #fee2e2; color: #991b1b; padding: 10px 14px; border-radius: 6px; margin-bottom: 15px; font-size: 13px; }
    </style>
</head>
<body>
    <header class="header">
        <img src="{{ url_for('static', filename='logo.png') }}" alt="АЛЬБИОН">
        <h1>⚙️ Настройки квот</h1>
    </header>
    <div class="main">
        <div class="card">
            {% if saved %}<div class="toast">Настройки сохранены.</div>{% endif %}
            {% if error %}<div class="error">{{ error }}</div>{% endif %}
            <p style="color:#64748b;font-size:13px;margin-top:0;">Изменения применяются сразу. Значение 0 отключает соответствующее ограничение.</p>
            <form method="post" action="/settings">
                <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                <div class="group">
                    <label>Максимальный размер одной загрузки, ГиБ</label>
                    <input type="number" name="max_upload_gb" min="0" step="0.1" value="{{ values.max_upload_gb }}">
                    <div class="hint">Ограничивает один HTTP-запрос; 0 — без Flask-лимита.</div>
                </div>
                <div class="group">
                    <label>Общая квота хранилища, ГиБ</label>
                    <input type="number" name="storage_quota_gb" min="0" step="0.1" value="{{ values.storage_quota_gb }}">
                    <div class="hint">Сумма учтённых файлов; 0 — без жёсткой общей квоты.</div>
                </div>
                <div class="group">
                    <label>Неприкосновенный резерв диска, ГиБ</label>
                    <input type="number" name="min_free_gb" min="0" step="0.1" value="{{ values.min_free_gb }}">
                    <div class="hint">Загрузки и создание ZIP не могут занять этот объём.</div>
                </div>
                <div class="group">
                    <label>Максимальный размер исходных файлов ZIP, ГиБ</label>
                    <input type="number" name="max_zip_gb" min="0" step="0.1" value="{{ values.max_zip_gb }}">
                    <div class="hint">Применяется к ZIP по выделению и /export_all; 0 — без ограничения.</div>
                </div>
                <div class="group">
                    <label>Максимум файлов в одном ZIP</label>
                    <input type="number" name="max_zip_files" min="0" step="1" value="{{ values.max_zip_files }}">
                    <div class="hint">0 — без ограничения по числу файлов.</div>
                </div>
                <div class="group">
                    <label><input type="checkbox" name="export_all_internal_only" value="1" style="width:auto;margin-right:6px;" {% if values.export_all_internal_only %}checked{% endif %}> Полный экспорт только из локальной сети/с API-ключом</label>
                    <div class="hint">Если выключено, GET /export_all доступен анонимно в пределах часовой квоты.</div>
                </div>
                <hr style="border:0;border-top:1px solid #e2e8f0;margin:22px 0;">
                {% if multi_user %}
                <h2>Учётные записи</h2>
                <p style="color:#64748b;font-size:13px;margin-top:0;">
                    Логины, роли и пароли ведутся в разделе
                    <a href="/settings/users">👥 Пользователи</a>; свой пароль каждый меняет
                    на странице <a href="/password">🔒 Пароль</a>.
                </p>
                {% else %}
                <h2>Учётные данные администратора</h2>
                <div class="group">
                    <label>Логин</label>
                    <input type="text" name="username" autocomplete="username" value="{{ auth_username }}" required>
                </div>
                <div class="group">
                    <label>Новый пароль</label>
                    <input type="password" name="new_password" autocomplete="new-password" placeholder="Оставьте пустым, чтобы не менять">
                    <div class="hint">Минимум 6 символов (режим JSON). В режиме SQLite пароли ведёт раздел «Пользователи».</div>
                </div>
                <div class="group">
                    <label>Повторите новый пароль</label>
                    <input type="password" name="new_password_confirm" autocomplete="new-password">
                </div>
                {% endif %}
                <div class="actions">
                    <button class="btn" type="submit">Сохранить</button>
                    {% if multi_user %}<a class="btn btn-secondary" href="/settings/users">👥 Пользователи</a>{% endif %}
                    <a class="btn btn-secondary" href="/settings/tokens">🔑 API-токены</a>
                    <a class="btn btn-secondary" href="/trash">🗑️ Корзина</a>
                    <a class="btn btn-secondary" href="/f/">← К файлам</a>
                    <a class="btn btn-secondary" href="/logout">Выйти</a>
                </div>
            </form>
        </div>
    </div>
</body>
</html>
"""


TOKENS_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>АЛЬБИОН: API-токены</title>
    <link rel="icon" type="image/png" href="{{ url_for('static', filename='logo.png') }}">
    <style>
        * { box-sizing: border-box; }
        body { font-family: 'Segoe UI', Arial, sans-serif; background: #f4f4f9; margin: 0; color: #1e293b; }
        .header { background: linear-gradient(135deg, #1e3a8a 0%, #2563eb 100%); color: white; padding: 15px 30px; display: flex; align-items: center; gap: 15px; flex-wrap: wrap; }
        .header img { height: 42px; background: white; padding: 4px 7px; border-radius: 6px; }
        .header h1 { margin: 0; font-size: 20px; }
        .header a { color: white; margin-left: auto; }
        .main { max-width: 1100px; margin: 0 auto; padding: 22px 15px; display: grid; gap: 18px; }
        .card { background: white; border-radius: 8px; box-shadow: 0 1px 4px rgba(0,0,0,.08); padding: 20px 24px; overflow-x: auto; }
        h2 { color: #1e3a8a; margin-top: 0; font-size: 18px; }
        .group { margin: 14px 0; }
        label { display: block; font-weight: 600; margin-bottom: 5px; font-size: 13px; }
        input[type=text], input[type=number], select { width: 100%; max-width: 360px; padding: 8px 10px; border: 1px solid #cbd5e1; border-radius: 5px; font-size: 14px; }
        .scopes { display: flex; gap: 15px; flex-wrap: wrap; font-weight: 400; }
        .scopes label { display: flex; align-items: center; gap: 5px; font-weight: 400; }
        .hint { color: #64748b; font-size: 12px; margin-top: 4px; }
        .actions { display: flex; gap: 10px; margin-top: 18px; flex-wrap: wrap; }
        .btn { display: inline-block; padding: 8px 16px; border-radius: 5px; text-decoration: none; color: white; background: #2563eb; border: none; cursor: pointer; font-size: 13px; font-weight: 500; }
        .btn:hover { background: #1e3a8a; }
        .btn-secondary { background: #f1f5f9; color: #475569; }
        .btn-secondary:hover { background: #e2e8f0; }
        .btn-danger { background: #dc3545; }
        .btn-danger:hover { background: #b02a37; }
        .toast { background: #dcfce7; color: #166534; padding: 10px 14px; border-radius: 6px; margin-bottom: 15px; font-size: 13px; }
        .error { background: #fee2e2; color: #991b1b; padding: 10px 14px; border-radius: 6px; margin-bottom: 15px; font-size: 13px; }
        .secret { background: #f0fdf4; border: 1px solid #86efac; border-radius: 7px; padding: 14px; margin: 12px 0; }
        .secret code { display: block; margin-top: 8px; padding: 10px; background: #fff; border: 1px solid #cbd5e1; border-radius: 4px; word-break: break-all; font-size: 13px; }
        table { width: 100%; border-collapse: collapse; font-size: 13px; }
        th, td { text-align: left; padding: 8px; border-bottom: 1px solid #e2e8f0; vertical-align: top; }
        th { color: #1e3a8a; font-size: 12px; text-transform: uppercase; }
        .badge { display: inline-block; padding: 2px 7px; border-radius: 9px; font-size: 11px; font-weight: 600; background: #eff6ff; color: #1d4ed8; white-space: nowrap; }
        .badge.expired, .badge.invalid { background: #fee2e2; color: #991b1b; }
        .badge.legacy { background: #fef3c7; color: #92400e; }
        .empty { color: #94a3b8; padding: 12px 0; }
        .revoke-form { display: inline; }
        .revoke-form button { padding: 4px 9px; font-size: 12px; }
        code { word-break: break-all; }
        @media (max-width: 700px) {
            .header { padding: 12px 15px; }
            .header a { margin-left: 0; }
            .main { padding: 12px 8px; }
            .card { padding: 14px; }
            table { min-width: 850px; }
        }
    </style>
</head>
<body>
    <header class="header">
        <img src="{{ url_for('static', filename='logo.png') }}" alt="АЛЬБИОН">
        <h1>🔑 API-токены</h1>
        <a class="btn btn-secondary" href="/settings">← Настройки</a>
        <a class="btn btn-secondary" href="/logout">Выйти</a>
    </header>
    <div class="main">
        {% if new_token %}
        <div class="card secret">
            <h2>Токен создан</h2>
            <p>Сохраните секрет сейчас — после обновления страницы он больше не будет показан.</p>
            <code id="newToken">{{ new_token }}</code>
            <div class="hint">Имя: {{ new_token_name }}. Передавайте его только в заголовке <code>X-API-Key</code>.</div>
        </div>
        {% endif %}
        <div class="card">
            <h2>Создать токен для приложения</h2>
            {% if error %}<div class="error">{{ error }}</div>{% endif %}
            {% if revoked %}<div class="toast">Токен отозван.</div>{% endif %}
            <p class="hint">Токены нужны для доверенных интеграций. Для загрузки и удаления обязательна привязка к папке. Срок по умолчанию — 90 дней.</p>
            <form method="post" action="/settings/tokens/create" autocomplete="off">
                <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                <div class="group">
                    <label for="tokenName">Имя токена</label>
                    <input id="tokenName" type="text" name="name" maxlength="64" required value="{{ form.name }}">
                </div>
                <div class="group">
                    <label>Права</label>
                    <div class="scopes">
                        <label><input type="checkbox" name="scopes" value="download" {% if 'download' in form.scopes %}checked{% endif %}> Скачивание / экспорт</label>
                        <label><input type="checkbox" name="scopes" value="upload" {% if 'upload' in form.scopes %}checked{% endif %}> Загрузка</label>
                        <label><input type="checkbox" name="scopes" value="delete" {% if 'delete' in form.scopes %}checked{% endif %}> Удаление</label>
                    </div>
                    <div class="hint">Для загрузки/удаления выберите папку ниже.</div>
                </div>
                <div class="group">
                    <label for="tokenFolder">Папка (обязательна для загрузки/удаления)</label>
                    <select id="tokenFolder" name="folder_id">
                        <option value="">— без привязки, только download —</option>
                        {% for folder in folder_options %}
                        <option value="{{ folder.id }}" {% if form.folder_id == folder.id %}selected{% endif %}>{{ folder.label }}</option>
                        {% endfor %}
                    </select>
                </div>
                <div class="group">
                    <label style="font-weight:400"><input type="checkbox" name="simple" value="1" {% if form.simple %}checked{% endif %}> Простой токен из 8 кириллических букв</label>
                    <div class="hint">Регистр не важен. Удобно для бригад: токен можно записать в папку бригады.</div>
                </div>
                <div class="group">
                    <label for="tokenExpires">Срок действия, дней</label>
                    <input id="tokenExpires" type="number" name="expires_days" min="1" max="3650" step="1" value="{{ form.expires_days }}">
                    <label style="font-weight:400;margin-top:8px"><input type="checkbox" name="no_expiry" value="1" {% if form.no_expiry %}checked{% endif %}> Бессрочный токен</label>
                </div>
                <div class="actions">
                    <button class="btn" type="submit">Создать токен</button>
                    <a class="btn btn-secondary" href="/f/">← К файлам</a>
                </div>
            </form>
        </div>
        <div class="card">
            <h2>Выданные токены</h2>
            {% if tokens %}
            <table>
                <thead><tr><th>Имя</th><th>Права</th><th>Папка</th><th>Создан</th><th>Срок</th><th>Состояние</th><th>Hash</th><th></th></tr></thead>
                <tbody>
                {% for token in tokens %}
                <tr>
                    <td><strong>{{ token.name }}</strong></td>
                    <td>{{ token.scopes_label }}</td>
                    <td>{{ token.folder_label }}</td>
                    <td>{{ token.created }}</td>
                    <td>{{ token.expires_label }}</td>
                    <td><span class="badge {{ token.status_class }}">{{ token.status_label }}</span></td>
                    <td><code>{{ token.hash_prefix }}…</code></td>
                    <td>
                        <form class="revoke-form" method="post" action="/settings/tokens/revoke" onsubmit="return confirm('Отозвать этот токен?');">
                            <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                            <input type="hidden" name="token_id" value="{{ token.token_id }}">
                            <button class="btn btn-danger" type="submit">Отозвать</button>
                        </form>
                    </td>
                </tr>
                {% endfor %}
                </tbody>
            </table>
            {% else %}
            <div class="empty">Пока нет выданных токенов.</div>
            {% endif %}
        </div>
    </div>
    <script>
        {% if new_token %}
        document.getElementById('newToken').addEventListener('click', () => {
            const text = document.getElementById('newToken').textContent;
            const selection = window.getSelection();
            const range = document.createRange();
            range.selectNodeContents(document.getElementById('newToken'));
            selection.removeAllRanges(); selection.addRange(range);
            navigator.clipboard?.writeText(text).catch(() => {});
        });
        {% endif %}
    </script>
</body>
</html>
"""

# ===== АУТЕНТИФИКАЦИЯ =====
AUTH_FILE = os.path.join(DATA_DIR, 'auth.json')
DEFAULT_AUTH_USERNAME = 'admin'
DEFAULT_AUTH_PASSWORD = 'альбион'

def ensure_default_auth():
    """Create the requested bootstrap admin only when auth.json is absent.

    В режиме SQLite стартовая учётная запись с известным паролем не создаётся
    никогда (находка F-01 аудита): администратор заводится командой
    `manage_users.py add-admin`, а до этого вход закрыт.
    """
    if metadata_store.sqlite_enabled():
        return os.path.exists(AUTH_FILE)
    if os.path.exists(AUTH_FILE):
        return True
    try:
        _save_json(AUTH_FILE, {
            'username': DEFAULT_AUTH_USERNAME,
            'password_hash': generate_password_hash(DEFAULT_AUTH_PASSWORD),
        })
        app.logger.warning(
            'Создан стартовый администратор admin/альбион. '
            'Смените пароль в /settings перед публикацией сервера в интернет.')
        return True
    except OSError as e:
        app.logger.critical('Не удалось создать auth.json: %s', e)
        return False

# ===== API-КЛЮЧИ ДЛЯ АВТОМАТИКИ (tokens.json: {sha256(ключ): {name, created}}) =====
def load_tokens():
    tokens = _load_json(TOKENS_FILE)
    if os.path.exists(TOKENS_FILE):
        try:
            os.chmod(TOKENS_FILE, 0o600)
        except OSError:
            pass
    return tokens

def _token_scopes(meta):
    """Права токена; старые записи без scopes работают только на скачивание."""
    return token_store.scopes_of(meta)

def _token_is_expired(value):
    return token_store.token_expired(value)

def check_api_key(provided):
    """Метаданные ключа + его hash-id, либо None.

    Поддерживаются оба формата:
    * legacy-ключ `alb_...` (регистрозависимый);
    * простой токен из 8 кириллических букв (регистронезависимый,
      передаётся в percent-encoding, например `%D0%9F%D0%A0...`).
    """
    if not provided:
        return None
    try:
        tokens = load_tokens()
    except token_store.TokenStoreError:
        app.logger.exception('Не удалось прочитать tokens.json')
        return None

    key_hash = None
    meta = None
    for candidate in token_store.api_key_hash_candidates(provided):
        found = tokens.get(candidate)
        if isinstance(found, dict):
            key_hash, meta = candidate, found
            break

    if not meta:
        return None
    scopes = _token_scopes(meta)
    expired = _token_is_expired(meta.get('expires_at'))
    if expired is None:
        return None
    return {'id': key_hash, 'name': meta.get('name', 'без имени'),
            'scopes': scopes, 'folder_id': meta.get('folder_id'),
            'expires_at': meta.get('expires_at'), 'expired': bool(expired)}

def require_api_scope(scope):
    """Проверка X-API-Key и права для машинных роутов.

    Возвращает (meta, None) либо (None, (jsonify(...), status)).
    Просроченный токен — 403, отсутствующий/неверный — 401.
    """
    meta = check_api_key(request.headers.get('X-API-Key'))
    if not meta:
        return None, (jsonify({'error': 'Требуется корректный заголовок X-API-Key'}), 401)
    if meta.get('expired'):
        return None, (jsonify({'error': 'Токен просрочен'}), 403)
    scopes = meta.get('scopes') or ['download']
    if '*' not in scopes and scope not in scopes:
        return None, (jsonify({'error': f'Токену не разрешено: {scope}'}), 403)
    if scope in token_store.FOLDER_REQUIRED_SCOPES and not meta.get('folder_id'):
        return None, (jsonify({'error': 'Токен не привязан к папке'}), 403)
    return meta, None

def _integration_rate_limit_key():
    """Лимит частоты на конкретный токен; fallback — IP клиента."""
    key_id = g.get('api_key_id')
    if key_id:
        return f'token:{key_id}'
    return get_remote_address() or 'unknown'

@app.before_request
def _resolve_api_key():
    """API-ключ принимается только заголовком, чтобы секрет не попадал в URL/логи."""
    key = check_api_key(request.headers.get('X-API-Key'))
    g.api_key_meta = key
    # Просроченный токен не даёт бюджет скачивания и не считается авторизацией.
    active = key if key and not key.get('expired') else None
    g.api_key = active['name'] if active else None
    g.api_key_id = active['id'] if active else None

@app.before_request
def _enforce_csrf():
    """Защита авторизованных изменений от межсайтовых POST/PUT/PATCH/DELETE."""
    if request.method not in {'POST', 'PUT', 'PATCH', 'DELETE'} or not session.get('logged_in'):
        return None
    expected = session.get('_csrf_token')
    provided = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token')
    if not expected or not provided or not hmac.compare_digest(expected, provided):
        app.logger.warning('CSRF token missing/invalid: %s %s from %s', request.method, request.path, request.remote_addr)
        return jsonify({'error': 'CSRF-токен отсутствует или неверен'}), 403
    return None

@app.before_request
def _early_upload_capacity():
    """Reject oversized/unsafe uploads before Werkzeug parses multipart data."""
    if not session.get('logged_in') or request.path not in {'/upload', '/upload_folder'}:
        return None
    error = upload_capacity_error(request.content_length)
    if error:
        status, message = error
        app.logger.warning('Ранняя проверка загрузки отклонена: %s (content_length=%s)',
                           message, request.content_length)
        return jsonify({'error': message}), status
    return None

# ===== БЮДЖЕТ СКАЧИВАНИЯ: Redis (общий) или memory fallback =====
_download_windows = {}
_DOWNLOAD_BUDGET_LOCK = threading.Lock()
_redis_client = None
_redis_checked = False

def _get_redis_client():
    """Optional shared state for multi-process deployments. Requires redis package/server."""
    global _redis_client, _redis_checked
    if _redis_checked:
        return _redis_client
    _redis_checked = True
    if not _REDIS_URL:
        return None
    try:
        import redis  # optional dependency
        client = redis.Redis.from_url(
            _REDIS_URL, decode_responses=True,
            socket_connect_timeout=0.5, socket_timeout=0.5,
        )
        client.ping()
        _redis_client = client
        app.logger.info('Redis подключён для бюджетов и квот экспорта')
    except Exception as e:
        app.logger.warning('Redis недоступен (%s); бюджеты работают в памяти процесса', e)
        _redis_client = None
    return _redis_client

_REDIS_BUDGET_SCRIPT = """
local used = tonumber(redis.call('GET', KEYS[1]) or '0')
local limit = tonumber(ARGV[1])
local size = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
if used > 0 and used + size > limit then
  return -1
end
redis.call('INCRBY', KEYS[1], size)
if used == 0 then redis.call('EXPIRE', KEYS[1], ttl) end
return 1
"""

def _redis_budget_check(scope, size_bytes, limit, window):
    """Return (handled, retry_after_seconds_or_None); fallback to memory on Redis error."""
    client = _get_redis_client()
    if client is None:
        return False, None
    now = time.time()
    bucket = int(now // window)
    key = f"albion:budget:{scope}:{bucket}"
    try:
        result = client.eval(_REDIS_BUDGET_SCRIPT, 1, key, limit, size_bytes, window + 5)
        if result == -1:
            return True, max(1, int(window - (now % window)))
        return True, None
    except Exception as e:
        app.logger.warning('Redis budget недоступен (%s); fallback в память', e)
        return False, None

def check_download_budget(ip, size_bytes):
    """None — скачивание разрешено (usage записан); иначе сколько секунд ждать.
    Одиночный файл больше лимита пропускается (used == 0), чтобы не блокировать
    легитимное скачивание одного большого файла."""
    now = time.time()
    if g.get('api_key'):
        limit, window = TOKEN_DL_BUDGET_BYTES, TOKEN_DL_WINDOW
    else:
        limit, window = ANON_DL_BUDGET_BYTES, ANON_DL_WINDOW
    scope = f"key:{g.api_key_id}" if g.get('api_key_id') else f"ip:{ip}"
    handled, retry = _redis_budget_check(scope, size_bytes, limit, window)
    if handled:
        return retry
    with _DOWNLOAD_BUDGET_LOCK:
        dq = _download_windows.setdefault(scope, deque())
        while dq and now - dq[0][0] > window:
            dq.popleft()
        used = sum(b for _, b in dq)
        if used > 0 and used + size_bytes > limit:
            return max(1, int(window - (now - dq[0][0])))
        dq.append((now, size_bytes))
        if len(dq) > 4096:  # страховка от чрезмерного числа запросов
            dq.popleft()
        if len(_download_windows) > 10000:
            # Удаляем пустые окна; если все активны, очищаем самый старый scope.
            empty = [key for key, entries in _download_windows.items() if not entries]
            for key in empty[:2000]:
                _download_windows.pop(key, None)
            if len(_download_windows) > 10000:
                oldest = min(_download_windows, key=lambda key: _download_windows[key][0][0] if _download_windows[key] else 0)
                _download_windows.pop(oldest, None)
        return None

# Массовый экспорт: последнее время на область (IP или ключ)
_export_all_scopes = {}
_EXPORT_ALL_LOCK = threading.Lock()
ZIP_BUILD_LOCK = threading.Lock()

def get_auth():
    if not ensure_default_auth():
        return None
    try:
        with open(AUTH_FILE, 'r', encoding='utf-8') as f:
            auth = json.load(f)
        try:
            os.chmod(AUTH_FILE, 0o600)
        except OSError:
            pass
        if not isinstance(auth, dict) or not auth.get('username') or not auth.get('password_hash'):
            raise ValueError('auth.json должен содержать username и password_hash')
        return auth
    except (OSError, json.JSONDecodeError, ValueError) as e:
        app.logger.exception('Не удалось прочитать файл авторизации %s: %s', AUTH_FILE, e)
        return None

def serialized_mutation(f):
    """Serialize DB read-modify-write admin operations against parallel uploads."""
    @wraps(f)
    def wrapped(*args, **kwargs):
        with UPLOAD_LOCK:
            return f(*args, **kwargs)
    return wrapped

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not _session_valid():
            session.clear()
            # fetch() следует за 302 и получает HTML логина, из-за чего клиент
            # не замечал истёкшую сессию. Для /api/ отдаём явный 401.
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Сессия истекла, войдите заново'}), 401
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function


# ===== КТО ПРИШЁЛ: ЛОКАЛЬНАЯ СЕТЬ ИЛИ ИНТЕРНЕТ =====

def _peer_address():
    """Адрес непосредственного клиента — до подстановок из forwarded-заголовков."""
    original = request.environ.get('werkzeug.proxy_fix.orig') or {}
    return original.get('REMOTE_ADDR') or request.environ.get('REMOTE_ADDR', '')


def _is_loopback(address):
    try:
        peer = ipaddress.ip_address(address)
    except (TypeError, ValueError):
        return False
    if isinstance(peer, ipaddress.IPv6Address) and peer.ipv4_mapped:
        peer = peer.ipv4_mapped
    return peer.is_loopback


def request_is_external():
    """True — запрос пришёл через тоннель, False — напрямую из локальной сети.

    Тоннель cloudpub подключается с loopback, а офисные клиенты — со своих
    адресов локальной сети, поэтому признак «внешний» — loopback-сосед.
    """
    return _is_loopback(_peer_address())


def multi_user_auth():
    """Персональные учётные записи и сессии в базе доступны в режиме SQLite."""
    return metadata_store.sqlite_enabled()


def remote_access_allowed(user):
    """Кому разрешён вход через тоннель.

    По решению правил доступа из интернета входит только администратор; флаг
    учётки `allow_remote` — явное исключение для конкретного сотрудника.
    """
    if not user:
        return False
    return user.get('role') == 'admin' or bool(user.get('allow_remote'))


def ensure_users_ready():
    """Перенести единственного администратора из auth.json в таблицу users.

    Выполняется лениво, при первой попытке входа: к этому моменту тесты уже
    создали свой auth.json, а сервер не трогает базу на импорте модуля.
    """
    if not multi_user_auth():
        return None
    try:
        if user_store.list_users():
            return None
        user, note = user_store.migrate_from_auth_json(AUTH_FILE)
        if user:
            app.logger.warning('Учётная запись перенесена из auth.json: «%s» (%s)',
                               user['login'], note)
        return user
    except Exception:
        app.logger.exception('Не удалось подготовить таблицу пользователей')
        return None


def current_user():
    """Кто выполняет запрос: запись из users либо прежний админ из auth.json."""
    if multi_user_auth():
        user_id = session.get('user_id')
        if not user_id:
            return None
        user = user_store.get_user_by_id(user_id)
        if not user or not user['active']:
            return None
        return user
    auth = get_auth()
    if not auth:
        return None
    return {'id': None, 'login': auth['username'], 'display_name': auth['username'],
            'role': 'admin', 'active': True, 'must_change_password': False,
            'allow_remote': True, 'email': None}


def current_role():
    user = current_user()
    return user['role'] if user else None


def is_admin():
    return current_role() == 'admin'


def admin_required(f):
    """Раздел настроек доступен только администратору: роль user туда не входит."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not _session_valid():
            session.clear()
            return redirect(url_for('login', next=request.url))
        if not is_admin():
            app.logger.warning('Отказ в доступе к настройкам: «%s» (%s)',
                               session.get('username'), request.path)
            return 'Раздел доступен только администратору', 403
        return f(*args, **kwargs)
    return decorated_function


# Шаблоны проверяют роль теми же функциями, что и маршруты, — расхождение между
# «что видно» и «что разрешено» становится невозможным.
app.jinja_env.globals['is_admin'] = is_admin
app.jinja_env.globals['current_user'] = current_user
# Строки файлов и папок рисуются общей функцией — и на странице, и во фрагментах.
app.jinja_env.globals['file_row_html'] = render_file_row
app.jinja_env.globals['folder_row_html'] = render_folder_row


def _session_valid():
    """Сессия жива: cookie действительна, запись сессии не отозвана и не истекла."""
    if not session.get('logged_in'):
        return False
    if multi_user_auth():
        # Проверка записи в базе даёт немедленный отзыв: выход, смена пароля,
        # деактивация учётки или ручной отзыв в разделе пользователей.
        return bool(session_store.resolve(session.get('sid'))) and current_user() is not None
    auth = get_auth()
    return bool(auth) and session.get('_auth_fingerprint') == auth_fingerprint(auth)


# Страницы, доступные пользователю с обязательной сменой пароля
PASSWORD_CHANGE_ALLOWED = {'/password', '/logout', '/login', '/favicon.ico', '/robots.txt'}


@app.before_request
def _enforce_session_policy():
    """Обязательная смена пароля и правило «из интернета — только администратор»."""
    if not multi_user_auth() or not session.get('logged_in'):
        return None
    if request.path in PASSWORD_CHANGE_ALLOWED or request.path.startswith('/static/'):
        return None
    if not _session_valid():
        session.clear()
        return redirect(url_for('login', next=request.url))

    user = current_user()
    if user is None:
        session.clear()
        return redirect(url_for('login', next=request.url))
    if user['must_change_password']:
        return redirect('/password')
    if not remote_access_allowed(user) and LAN_HOST and request_is_external():
        session_store.revoke(session.get('sid'))
        session.clear()
        return _login_page('Вход из интернета разрешён только администратору. '
                           'Подключитесь к локальной сети.'), 403
    return None


def auth_fingerprint(auth):
    """Return a non-secret session version that changes with stored credentials."""
    material = json.dumps(
        [auth.get('username', ''), auth.get('password_hash', '')],
        ensure_ascii=False, separators=(',', ':'),
    )
    return hashlib.sha256(material.encode('utf-8')).hexdigest()


def is_trusted_proxy_peer(peer_addr):
    try:
        peer = ipaddress.ip_address(peer_addr)
        if isinstance(peer, ipaddress.IPv6Address) and peer.ipv4_mapped:
            peer = peer.ipv4_mapped
    except (TypeError, ValueError):
        return False
    for value in TRUSTED_PROXY_IPS:
        try:
            if peer in ipaddress.ip_network(value, strict=False):
                return True
        except ValueError:
            continue
    return False


def trusted_proxy_middleware(app_wsgi):
    # x_for — настоящий адрес клиента, x_proto — схема (у тоннеля https),
    # x_host — публичное имя: от него зависят абсолютные ссылки в ответах.
    proxy_app = ProxyFix(app_wsgi, x_for=1, x_proto=1, x_host=1)

    def wrapped(environ, start_response):
        if is_trusted_proxy_peer(environ.get('REMOTE_ADDR', '')):
            return proxy_app(environ, start_response)
        # Drop untrusted forwarded headers; the app uses REMOTE_ADDR for access
        # and rate limiting, so no header supplied by this peer may replace it.
        environ.pop('HTTP_X_FORWARDED_FOR', None)
        environ.pop('HTTP_X_FORWARDED_PROTO', None)
        return app_wsgi(environ, start_response)

    return wrapped


def serialized_zip_build(f):
    """Serialize ZIP capacity checks and writes within this app process."""
    @wraps(f)
    def wrapped(*args, **kwargs):
        with ZIP_BUILD_LOCK:
            return f(*args, **kwargs)
    return wrapped

def _login_csrf_input():
    token = session.setdefault('_csrf_token', secrets.token_urlsafe(32))
    return f'<input type="hidden" name="csrf_token" value="{token}">'

LOGIN_STYLE = """body{font-family:'Segoe UI',Arial,sans-serif;display:flex;justify-content:center;align-items:center;min-height:100vh;background:#f4f4f9;margin:0}
.box{background:white;padding:40px;border-radius:10px;box-shadow:0 4px 12px rgba(0,0,0,0.1);width:320px;text-align:center}
h2{color:#1e3a8a;margin-top:0}input{width:100%;padding:12px;margin:10px 0;border:1px solid #cbd5e1;border-radius:6px;font-size:14px;box-sizing:border-box}
button{width:100%;padding:12px;background:#2563eb;color:white;border:none;border-radius:6px;cursor:pointer;font-size:15px;font-weight:600;margin-top:10px}
button:hover{background:#1e3a8a}.error{color:#dc3545;font-size:14px;margin-bottom:10px}.notice{color:#475569;font-size:13px;margin-bottom:10px}"""


def _login_page(error=None, notice=None):
    message = f'<p class="error">{error}</p>' if error else ''
    if notice:
        message += f'<p class="notice">{notice}</p>'
    page = ('<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Вход - АЛЬБИОН</title><style>' + LOGIN_STYLE + '</style></head><body>'
            '<div class="box"><h2>🔒 АЛЬБИОН: Вход</h2>' + message +
            '<form method="post"><input name="username" placeholder="Логин" required autocomplete="username">'
            '<input type="password" name="password" placeholder="Пароль" required autocomplete="current-password">'
            '<button type="submit">Войти</button></form></div></body></html>')
    return page.replace('<form method="post">', '<form method="post">' + _login_csrf_input())


def _safe_next_page(default='/f/'):
    """Защита от open redirect: только относительные пути внутри сайта."""
    next_page = request.args.get('next', default)
    if not next_page.startswith('/') or next_page.startswith('//'):
        return default
    return next_page


@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute", deduct_when=lambda response: rate_limit_applies())
def login():
    """Вход: персональные учётки в режиме SQLite, прежний администратор в json.

    Частота попыток ограничена для внешних клиентов; дополнительно учётка
    блокируется после 10 неудачных попыток.
    """
    if multi_user_auth():
        return _login_multi_user()
    return _login_legacy()


def _login_multi_user():
    ensure_users_ready()
    if not user_store.list_users():
        return ('<!DOCTYPE html><html lang="ru"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                '<title>Требуется настройка</title>'
                '<h1>Вход отключён: учётных записей нет</h1>'
                '<p>Создайте администратора командой '
                '<code>venv/bin/python manage_users.py add-admin ЛОГИН</code> '
                'и обновите страницу.</p></html>'), 503

    if request.method == 'POST':
        expected_csrf = session.get('_csrf_token')
        provided_csrf = request.form.get('csrf_token', '')
        if not expected_csrf or not hmac.compare_digest(expected_csrf, provided_csrf):
            return 'CSRF-токен отсутствует или неверен', 400

        login_name = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = user_store.get_user(login_name)

        # Текст ошибки одинаков для «нет такого логина» и «неверный пароль»:
        # иначе форма подсказывает, какие логины существуют.
        if user is None:
            app.logger.warning('Неудачная попытка входа: логин «%s» с %s', login_name, request.remote_addr)
            return _login_page('Неверный логин или пароль')

        if not user['active']:
            app.logger.warning('Вход отклонён (учётка отключена): «%s» с %s', login_name, request.remote_addr)
            return _login_page('Учётная запись отключена администратором')

        if user_store.is_locked(user):
            minutes = user_store.lock_remaining_minutes(user)
            app.logger.warning('Вход отклонён (учётка заблокирована): «%s» с %s', login_name, request.remote_addr)
            return _login_page(f'Учётная запись временно заблокирована, повторите через {minutes} мин'), 429

        ok, needs_rehash = user_store.verify_password(user['password_hash'], password)
        if not ok:
            locked_until = user_store.register_failure(user['id'])
            app.logger.warning('Неудачная попытка входа: логин «%s» с %s', login_name, request.remote_addr)
            if locked_until:
                return _login_page(
                    f'Превышено число попыток. Учётная запись заблокирована на '
                    f'{user_store.LOCK_MINUTES} мин'), 429
            return _login_page('Неверный логин или пароль')

        if not remote_access_allowed(user) and LAN_HOST and request_is_external():
            app.logger.warning('Вход снаружи запрещён для «%s» с %s', login_name, request.remote_addr)
            return _login_page('Вход из интернета разрешён только администратору. '
                              'Подключитесь к локальной сети.')

        rehash = user_store.hash_password(password) if needs_rehash else None
        user_store.register_success(user['id'], rehash=rehash)

        session.clear()
        session.permanent = True
        session['logged_in'] = True
        session['user_id'] = user['id']
        session['username'] = user['login']
        session['role'] = user['role']
        session['_csrf_token'] = secrets.token_urlsafe(32)
        # Токен сессии: в базе лежит только его хэш, поэтому запись можно отозвать.
        session['sid'] = session_store.create(
            user['id'], role=user['role'], ip=request.remote_addr,
            user_agent=request.headers.get('User-Agent'))
        app.logger.info('Вход: «%s» (%s) с %s', user['login'], user['role'], request.remote_addr)
        if user['must_change_password']:
            return redirect('/password')
        return redirect(_safe_next_page())

    if _session_valid():
        return redirect('/f/')
    if session.get('logged_in'):
        session.clear()
    return _login_page()


def _login_legacy():
    auth = get_auth()
    if auth is None:
        return '''<!DOCTYPE html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Требуется настройка</title>
        <h1>Вход отключён: не настроены учётные данные</h1>
        <p>Администратор должен создать auth.json через change_password.py и перезапустить сервис.</p></html>''', 503
    if request.method == 'POST':
        expected_csrf = session.get('_csrf_token')
        provided_csrf = request.form.get('csrf_token', '')
        if not expected_csrf or not hmac.compare_digest(expected_csrf, provided_csrf):
            return 'CSRF-токен отсутствует или неверен', 400
        if request.form.get('username') == auth['username'] and check_password_hash(auth['password_hash'], request.form.get('password', '')):
            session.clear()
            session['logged_in'] = True
            session['_auth_fingerprint'] = auth_fingerprint(auth)
            session['_csrf_token'] = secrets.token_urlsafe(32)
            session.permanent = True
            app.logger.info('Вход: пользователь "%s" с %s', request.form.get('username'), request.remote_addr)
            next_page = request.args.get('next', '/f/')
            # Защита от open redirect: только относительные пути внутри сайта
            if not next_page.startswith('/') or next_page.startswith('//'):
                next_page = '/f/'
            return redirect(next_page)
        app.logger.warning('Неудачная попытка входа: логин "%s" с %s',
                           request.form.get('username'), request.remote_addr)
        page = '''<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Вход - АЛЬБИОН</title>
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
        return page.replace('<form method="post">', '<form method="post">' + _login_csrf_input())

    # Redirect only for a session associated with the current credentials.
    if session.get('logged_in') and session.get('_auth_fingerprint') == auth_fingerprint(auth):
        return redirect('/f/')
    if session.get('logged_in'):
        session.clear()
        
    page = '''<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Вход - АЛЬБИОН</title>
    <style>body{font-family:'Segoe UI',Arial,sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;background:#f4f4f9;margin:0}
    .box{background:white;padding:40px;border-radius:10px;box-shadow:0 4px 12px rgba(0,0,0,0.1);width:320px;text-align:center}
    h2{color:#1e3a8a;margin-top:0}input{width:100%;padding:12px;margin:10px 0;border:1px solid #cbd5e1;border-radius:6px;font-size:14px;box-sizing:border-box}
    button{width:100%;padding:12px;background:#2563eb;color:white;border:none;border-radius:6px;cursor:pointer;font-size:15px;font-weight:600;margin-top:10px}
    button:hover{background:#1e3a8a}</style></head><body>
    <div class="box"><h2>🔒 АЛЬБИОН: Вход</h2>
    <form method="post"><input name="username" placeholder="Логин" required>
    <input type="password" name="password" placeholder="Пароль" required>
    <button type="submit">Войти</button></form></div></body></html>'''
    return page.replace('<form method="post">', '<form method="post">' + _login_csrf_input())

@app.route('/logout')
def logout():
    if multi_user_auth():
        session_store.revoke(session.get('sid'))
    session.clear()
    return redirect('/login')


@app.route('/password', methods=['GET', 'POST'])
@login_required
def change_own_password():
    """Смена собственного пароля; при must_change_password поле текущего пароля не спрашиваем."""
    user = current_user()
    forced = bool(user and user['must_change_password'])
    error = None
    notice = None
    if request.method == 'POST':
        current = request.form.get('current_password', '')
        new_password = request.form.get('new_password', '')
        confirm = request.form.get('new_password_confirm', '')

        if not forced:
            ok, _ = user_store.verify_password(user['password_hash'], current)
            if not ok:
                error = 'Текущий пароль указан неверно'
        if error is None and new_password != confirm:
            error = 'Повтор нового пароля не совпадает'
        if error is None and password_reuse(user, new_password):
            error = 'Новый пароль совпадает с текущим'
        if error is None:
            problem = user_store.password_error(new_password)
            if problem:
                error = problem

        if error is None:
            try:
                user_store.set_password(user['id'], new_password)
                # Смена пароля завершает все прочие сессии этого пользователя.
                current_record = session_store.resolve(session.get('sid'), touch=False)
                session_store.revoke_all_for_user(
                    user['id'], keep_session_id=current_record['id'] if current_record else None)
                app.logger.info('Пароль изменён: «%s» с %s', user['login'], request.remote_addr)
                session['logged_in'] = True
                return redirect('/f/')
            except user_store.UserError as exc:
                error = str(exc)

    return render_template_string(
        PASSWORD_TEMPLATE, user=user, forced=forced, error=error, notice=notice,
        csrf_token=session.get('_csrf_token', ''), admin=is_admin())


def password_reuse(user, new_password):
    if not user or not new_password:
        return False
    ok, _ = user_store.verify_password(user['password_hash'], new_password)
    return ok

@app.route('/')
def index():
    return redirect('/f/')

BROWSE_SORTS = (
    ('mtime_desc', 'Сначала новые'),
    ('name_asc', 'Имя А–Я'),
    ('size_desc', 'Размер: больше'),
    ('downloads_desc', 'Скачивания'),
)
BROWSE_PER_PAGE = (25, 50, 100)


def foldername_needle(query):
    return query.casefold()


def folder_files_matching(files, query, ext):
    """Файлы, подходящие под строку поиска и тип — те же правила, что в проводнике."""
    needle = foldername_needle(query or '')
    result = []
    for file_record in files:
        if ext and file_record['ext'].lower() != ext:
            continue
        if needle:
            haystack = ' '.join((
                file_record.get('name', ''),
                file_record.get('current_filename', ''),
                file_record.get('physical_path', ''),
                file_record.get('slug', ''),
                file_record.get('custom_alias', ''),
            )).casefold()
            if needle not in haystack:
                continue
        result.append(file_record)
    return result


def sort_folder_files(files, sort):
    if sort == 'name_asc':
        return sorted(files, key=lambda item: item['name'].casefold())
    if sort == 'size_desc':
        return sorted(files, key=lambda item: item['size_raw'], reverse=True)
    if sort == 'downloads_desc':
        return sorted(files, key=lambda item: item.get('download_count', 0), reverse=True)
    return files


def _browse_url(folder_id, **params):
    path = f'/f/{folder_id}' if folder_id else '/f/'
    clean = {key: value for key, value in params.items() if value not in (None, '')}
    query = urllib.parse.urlencode(clean)
    return f'{path}?{query}' if query else path



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

    q = request.args.get('q', '').strip()[:200]
    ext = request.args.get('ext', '').strip().lower()
    if ext not in ALLOWED_EXTENSIONS:
        ext = ''
    sort = request.args.get('sort', 'mtime_desc')
    if sort not in {key for key, _ in BROWSE_SORTS}:
        sort = 'mtime_desc'
    try:
        per_page = int(request.args.get('per_page', '50'))
    except (TypeError, ValueError):
        per_page = 50
    if per_page not in BROWSE_PER_PAGE:
        per_page = 50
    try:
        page = int(request.args.get('page', '1'))
    except (TypeError, ValueError):
        page = 1
    view = request.args.get('view', 'table')
    if view not in {'table', 'grid', 'list'}:
        view = 'table'

    stats_map = get_folder_stats_map(folders, load_db())
    subfolders_data = []
    for fid, f in folders.items():
        if f.get('parent_id') == folder_id:
            subfolders_data.append(folder_view_record(fid, folders, stats_map))

    if q:
        needle = q.casefold()
        subfolders_data = [
            folder for folder in subfolders_data
            if needle in folder['name'].casefold()
        ]

    all_files = get_files_in_folder(folder_id)
    filtered_files = folder_files_matching(all_files, q, ext)
    filtered_files = sort_folder_files(filtered_files, sort)
    total_files = len(filtered_files)
    total_pages = max(1, (total_files + per_page - 1) // per_page)
    page = min(max(1, page), total_pages)
    start = (page - 1) * per_page
    files = filtered_files[start:start + per_page]

    base_params = {
        'q': q or None,
        'ext': ext or None,
        'sort': sort,
        'per_page': per_page,
        'view': view,
    }
    reset_url = _browse_url(folder_id, view=view)
    next_url = _browse_url(folder_id, page=page + 1, **base_params) if page < total_pages else None
    ext_options = sorted({item['ext'].lower() for item in all_files if item.get('ext') not in (None, '—')})

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
        total_files=total_files,
        total_pages=total_pages,
        page=page,
        per_page=per_page,
        next_url=next_url,
        view=view,
        q=q,
        ext=ext,
        sort=sort,
        sort_options=BROWSE_SORTS,
        per_page_options=BROWSE_PER_PAGE,
        ext_options=ext_options,
        reset_url=reset_url,
        has_filters=bool(q or ext),
        stats_info=stats_info,
        stats=stats,
        disk=disk,
        base_url=BASE_URL,
        asset_version=ASSET_VERSION,
        csrf_token=session.setdefault('_csrf_token', secrets.token_urlsafe(32)))

@app.route('/settings', methods=['GET', 'POST'])
@admin_required
def settings_page():
    """Quota and credential settings, available only after admin login."""
    values = load_quota_settings()
    current_auth = get_auth() or {}
    auth_username = current_auth.get('username', DEFAULT_AUTH_USERNAME)
    error = None
    if request.method == 'POST':
        form_username = request.form.get('username', '').strip()
        new_password = request.form.get('new_password', '')
        confirm_password = request.form.get('new_password_confirm', '')
        # В режиме SQLite учётные данные здесь не меняются: логины и пароли ведёт
        # раздел «Пользователи», свой пароль пользователь меняет на /password.
        if not multi_user_auth():
            if not form_username:
                error = 'Логин не может быть пустым'
            elif new_password or confirm_password:
                if len(new_password) < 6:
                    error = 'Новый пароль должен быть не короче 6 символов'
                elif new_password != confirm_password:
                    error = 'Повтор нового пароля не совпадает'
        if error is None:
            try:
                parsed = {}
                for key in ('max_upload_gb', 'storage_quota_gb', 'min_free_gb', 'max_zip_gb'):
                    raw = request.form.get(key, '').strip().replace(',', '.')
                    value = float(raw)
                    max_allowed = 1024 if key == 'max_upload_gb' else 10 ** 9
                    if value < 0 or value != value or value > max_allowed:
                        raise ValueError('Недопустимое значение')
                    parsed[key] = value
                raw_files = request.form.get('max_zip_files', '').strip()
                max_files = int(raw_files)
                if max_files < 0 or max_files > 10 ** 9:
                    raise ValueError('Недопустимое число файлов')
                parsed['max_zip_files'] = max_files
                parsed['export_all_internal_only'] = request.form.get('export_all_internal_only') == '1'

                save_quota_settings(parsed)
                if not multi_user_auth():
                    # Режим JSON: единственный администратор правится здесь же.
                    new_auth = {
                        'username': form_username,
                        'password_hash': (generate_password_hash(new_password)
                                          if new_password else current_auth['password_hash']),
                    }
                    with DB_LOCK:
                        _save_json(AUTH_FILE, new_auth)
                    try:
                        os.chmod(AUTH_FILE, 0o600)
                    except OSError:
                        pass
                    if form_username != current_auth.get('username') or new_password:
                        session.clear()
                app.logger.info('Квоты изменены администратором «%s»',
                                session.get('username') or 'admin')
                return redirect('/settings?saved=1')
            except (TypeError, ValueError, OSError) as e:
                error = f'Проверьте значения: {e}'
        auth_username = form_username or auth_username
        values = {**values, **{k: request.form.get(k, values[k]) for k in values}}
    return render_template_string(
        SETTINGS_TEMPLATE,
        values=values,
        auth_username=auth_username,
        multi_user=multi_user_auth(),
        saved=request.args.get('saved') == '1',
        error=error,
        csrf_token=session.setdefault('_csrf_token', secrets.token_urlsafe(32)),
    )


TOKEN_HASH_RE = re.compile(r'^[0-9a-f]{64}$')


def _token_folder_path(folder_id, folders):
    if not folder_id:
        return '—'
    if folder_id not in folders:
        return f'{folder_id} (папка удалена)'
    return ' / '.join(get_folder_path(folder_id, folders))


def _token_expiry_label(value):
    if not value:
        return 'бессрочный'
    try:
        expires = datetime.fromisoformat(str(value))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires.astimezone(timezone.utc).strftime('%d.%m.%Y %H:%M UTC')
    except (TypeError, ValueError):
        return f'{value} (некорректно)'


def _render_tokens_page(error=None, form=None, new_token=None,
                        new_token_name=None, status=200):
    folders = load_folders()
    raw_tokens = load_tokens()
    form = form or {
        'name': '',
        'scopes': ['download'],
        'folder_id': '',
        'expires_days': token_store.DEFAULT_TTL_DAYS,
        'no_expiry': False,
        'simple': False,
    }
    folder_options = []
    for folder_id in folders:
        folder_options.append({
            'id': folder_id,
            'label': ' / '.join(get_folder_path(folder_id, folders)),
        })
    folder_options.sort(key=lambda item: item['label'].casefold())

    token_rows = []
    for token_id, meta in raw_tokens.items():
        if not isinstance(meta, dict):
            continue
        scopes = token_store.scopes_of(meta)
        folder_id = meta.get('folder_id')
        expired = token_store.token_expired(meta.get('expires_at'))
        binding_invalid = (
            any(scope in token_store.FOLDER_REQUIRED_SCOPES for scope in scopes)
            and not folder_id
        )
        if binding_invalid:
            status_class, status_label = 'invalid', 'нет папки'
        elif expired is None:
            status_class, status_label = 'invalid', 'некорректный срок'
        elif expired:
            status_class, status_label = 'expired', 'истёк'
        elif not meta.get('expires_at'):
            status_class, status_label = 'legacy', 'бессрочный'
        else:
            status_class, status_label = '', 'активен'
        token_rows.append({
            'token_id': token_id,
            'hash_prefix': token_id[:12],
            'name': meta.get('name') or 'без имени',
            'scopes_label': ', '.join('все (*)' if s == '*' else s for s in scopes),
            'folder_label': _token_folder_path(folder_id, folders),
            'created': meta.get('created') or meta.get('created_at') or '—',
            'expires_label': _token_expiry_label(meta.get('expires_at')),
            'status_class': status_class,
            'status_label': status_label,
        })
    token_rows.sort(key=lambda item: item['name'].casefold())

    body = render_template_string(
        TOKENS_TEMPLATE,
        tokens=token_rows,
        folder_options=folder_options,
        form=form,
        error=error,
        revoked=request.args.get('revoked') == '1',
        new_token=new_token,
        new_token_name=new_token_name,
        csrf_token=session.setdefault('_csrf_token', secrets.token_urlsafe(32)),
    )
    return body, status, {'Cache-Control': 'no-store', 'Pragma': 'no-cache'}


@app.route('/settings/tokens')
@admin_required
def tokens_page():
    return _render_tokens_page()


@app.route('/settings/tokens/create', methods=['POST'])
@admin_required
def token_create():
    form = {
        'name': request.form.get('name', '').strip(),
        'scopes': request.form.getlist('scopes'),
        'folder_id': request.form.get('folder_id', '').strip(),
        'expires_days': request.form.get('expires_days', '').strip(),
        'no_expiry': request.form.get('no_expiry') == '1',
        'simple': request.form.get('simple') == '1',
    }
    folders = load_folders()
    if form['simple']:
        raw_token = token_store.generate_simple_token()
        token_id = token_store.simple_token_hash(raw_token)
    else:
        raw_token, token_id = token_store.generate_token()
    created = {}

    def add_token(tokens):
        if token_id in tokens:
            raise ValueError('Сгенерированный токен уже существует, попробуйте ещё раз.')
        name, scopes, folder_id, expires_at = token_store.validate_issuance(
            form['name'], form['scopes'], form['folder_id'], folders,
            form['expires_days'], no_expiry=form['no_expiry'],
            existing_names=[
                meta.get('name', '') for meta in tokens.values()
                if isinstance(meta, dict)
            ],
        )
        record = token_store.build_record(
            name, scopes, folder_id, expires_at,
            kind='simple' if form['simple'] else None)
        tokens[token_id] = record
        created['record'] = record

    try:
        token_store.mutate_tokens(TOKENS_FILE, add_token)
    except (ValueError, token_store.TokenStoreError) as exc:
        app.logger.warning('Не удалось создать API-токен: %s', exc)
        return _render_tokens_page(error=str(exc), form=form, status=400)

    record = created['record']
    app.logger.info('Создан API-токен: имя=%s, права=%s, папка=%s, срок=%s',
                    record['name'], ','.join(record['scopes']),
                    record['folder_id'] or '—', record['expires_at'] or 'бессрочный')
    return _render_tokens_page(new_token=raw_token, new_token_name=record['name'])


@app.route('/settings/tokens/revoke', methods=['POST'])
@admin_required
def token_revoke():
    token_id = request.form.get('token_id', '').strip()
    if not TOKEN_HASH_RE.fullmatch(token_id):
        return _render_tokens_page(error='Некорректный идентификатор токена.', status=400)
    removed = {}

    def remove_token(tokens):
        meta = tokens.pop(token_id, None)
        if meta is None:
            raise ValueError('Токен не найден или уже отозван.')
        removed['meta'] = meta

    try:
        token_store.mutate_tokens(TOKENS_FILE, remove_token)
    except (ValueError, token_store.TokenStoreError) as exc:
        return _render_tokens_page(error=str(exc), status=400)

    app.logger.info('Отозван API-токен: имя=%s, hash=%s…',
                    removed['meta'].get('name', 'без имени'), token_id[:12])
    return redirect('/settings/tokens?revoked=1')


# ===== КОРЗИНА: СТРАНИЦА (только администратор) =====

TRASH_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Корзина — АЛЬБИОН</title>
    <link rel="icon" type="image/png" href="{{ url_for('static', filename='logo.png') }}">
    <style>
        * { box-sizing: border-box; }
        body { font-family: 'Segoe UI', Arial, sans-serif; background: #f4f4f9; margin: 0; color: #1e293b; }
        .header { background: linear-gradient(135deg, #1e3a8a 0%, #2563eb 100%); color: white; padding: 15px 30px; display: flex; align-items: center; gap: 15px; flex-wrap: wrap; }
        .header img { height: 42px; background: white; padding: 4px 7px; border-radius: 6px; }
        .header h1 { margin: 0; font-size: 19px; font-weight: 700; }
        .header .spacer { margin-left: auto; }
        .main { max-width: 1000px; margin: 0 auto; padding: 20px 15px; }
        .card { background: white; border-radius: 8px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); padding: 18px 20px; margin-bottom: 16px; }
        table { width: 100%; border-collapse: collapse; font-size: 13px; }
        th { text-align: left; padding: 8px 10px; color: #1e3a8a; font-size: 12px; text-transform: uppercase; border-bottom: 2px solid #e2e8f0; }
        td { padding: 8px 10px; border-bottom: 1px solid #f1f5f9; vertical-align: middle; }
        .muted { color: #64748b; }
        .btn { display: inline-block; background: #2563eb; color: white; padding: 7px 14px; border: none; border-radius: 5px; cursor: pointer; text-decoration: none; font-size: 13px; }
        .btn:hover { background: #1e3a8a; }
        .btn-secondary { background: #e2e8f0; color: #1e293b; }
        .btn-secondary:hover { background: #cbd5e1; }
        .btn-danger { background: #dc3545; }
        .btn-danger:hover { background: #b02a37; }
        .btn:focus-visible, a:focus-visible { outline: 2px solid #f59e0b; outline-offset: 2px; }
        .toast { background: #dcfce7; color: #166534; padding: 10px 14px; border-radius: 6px; margin-bottom: 15px; font-size: 13px; }
        .empty { text-align: center; padding: 40px 20px; color: #94a3b8; }
        form.inline { display: inline; }
        h2 { color: #1e3a8a; font-size: 16px; margin: 0 0 12px; }
    </style>
</head>
<body>
    <header class="header">
        <img src="{{ url_for('static', filename='logo.png') }}" alt="АЛЬБИОН">
        <div>
            <h1>🗑️ Корзина</h1>
        </div>
        <div class="spacer"></div>
        <a href="/f/" class="btn btn-secondary">← К файлам</a>
        <a href="/settings" class="btn btn-secondary">⚙️ Настройки</a>
    </header>
    <div class="main">
        {% if restored %}<div class="toast">Файлы восстановлены.</div>{% endif %}
        {% if purged %}<div class="toast">Корзина очищена ({{ purged }}).</div>{% endif %}
        <div class="card">
            <h2>Удалённые файлы хранятся {{ retention_days }} дней и затем удаляются автоматически</h2>
            {% if entries %}
            <p class="muted">Занято в корзине: {{ total_size }}. Такие файлы не входят в квоты.</p>
            <form method="post" action="/trash/empty" onsubmit="return confirm('Удалить всё из корзины безвозвратно?');">
                <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                <button class="btn btn-danger" type="submit">Очистить корзину</button>
            </form>
            <table>
                <thead><tr><th>Имя</th><th>Исходная папка</th><th>Размер</th><th>Удалён</th><th>Осталось</th><th></th></tr></thead>
                <tbody>
                {% for entry in entries %}
                <tr>
                    <td>{{ entry.name }}</td>
                    <td class="muted">{{ entry.folder_label }}</td>
                    <td>{{ entry.size }}</td>
                    <td class="muted">{{ entry.deleted_at }}</td>
                    <td>{{ entry.days_left }}</td>
                    <td>
                        <form class="inline" method="post" action="/trash/restore">
                            <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                            <input type="hidden" name="entry_id" value="{{ entry.id }}">
                            <button class="btn" type="submit">Восстановить</button>
                        </form>
                        <form class="inline" method="post" action="/trash/purge">
                            <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                            <input type="hidden" name="entry_id" value="{{ entry.id }}">
                            <button class="btn btn-secondary" type="submit">Удалить навсегда</button>
                        </form>
                    </td>
                </tr>
                {% endfor %}
                </tbody>
            </table>
            {% else %}
            <div class="empty">Корзина пуста.</div>
            {% endif %}
        </div>
    </div>
</body>
</html>
"""


def _trash_entry_rows(trash, folders):
    """Строки корзины для страницы, отсортированные по дате удаления."""
    rows = []
    now = datetime.now(timezone.utc)
    for entry_id, entry in trash.items():
        if not isinstance(entry, dict):
            continue
        try:
            deleted = datetime.fromisoformat(str(entry.get('deleted_at')))
        except (TypeError, ValueError):
            deleted = None
        if deleted is not None and deleted.tzinfo is None:
            deleted = deleted.replace(tzinfo=timezone.utc)
        days_left = ''
        if deleted is not None:
            remaining = TRASH_RETENTION_DAYS - (now - deleted).days
            days_left = f'{max(0, remaining)} дн.'
        folder_id = entry.get('folder_id')
        rows.append({
            'id': entry_id,
            'name': entry.get('name') or '—',
            'folder_label': ' / '.join(get_folder_path(folder_id, folders)) if folder_id in folders else 'Корень',
            'size': format_size(entry.get('size', 0)),
            'deleted_at': deleted.astimezone(timezone.utc).strftime('%d.%m.%Y %H:%M') if deleted else '—',
            'days_left': days_left,
            'sort_key': str(entry.get('deleted_at') or ''),
        })
    rows.sort(key=lambda item: item['sort_key'], reverse=True)
    return rows


def _render_trash_page(restored=None, purged=None):
    purge_expired_trash()
    trash = load_trash()
    folders = load_folders()
    entries = _trash_entry_rows(trash, folders)
    total_size = sum(entry.get('size', 0) for entry in trash.values() if isinstance(entry, dict))
    body = render_template_string(
        TRASH_TEMPLATE,
        entries=entries,
        total_size=format_size(total_size),
        retention_days=TRASH_RETENTION_DAYS,
        restored=restored,
        purged=purged,
        csrf_token=session.setdefault('_csrf_token', secrets.token_urlsafe(32)),
    )
    return body, 200, {'Cache-Control': 'no-store', 'Pragma': 'no-cache'}


@app.route('/trash')
@admin_required
def trash_page():
    return _render_trash_page(restored=request.args.get('restored') == '1')


@app.route('/trash/restore', methods=['POST'])
@admin_required
@serialized_mutation
def trash_page_restore():
    entry_id = request.form.get('entry_id', '').strip()
    if entry_id:
        trash = load_trash()
        if entry_id in trash:
            db = load_db()
            folders = load_folders()
            restore_trash_entries([entry_id], folders, db, trash)
            save_db(db)
            save_trash(trash)
    return redirect('/trash?restored=1')


@app.route('/trash/purge', methods=['POST'])
@admin_required
@serialized_mutation
def trash_page_purge():
    entry_id = request.form.get('entry_id', '').strip()
    if entry_id:
        trash = load_trash()
        if purge_trash_entry(entry_id, trash):
            save_trash(trash)
    return redirect('/trash')


@app.route('/trash/empty', methods=['POST'])
@admin_required
@serialized_mutation
def trash_page_empty():
    trash = load_trash()
    purged = len(trash)
    for entry_id in list(trash):
        purge_trash_entry(entry_id, trash)
    save_trash(trash)
    return redirect(f'/trash?purged={purged}')


# ===== УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ (только администратор) =====

PASSWORD_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Смена пароля - АЛЬБИОН</title>
<style>
    * { box-sizing: border-box; }
    body { font-family: 'Segoe UI', Arial, sans-serif; background: #f4f4f9; margin: 0; }
    .wrapper { max-width: 460px; margin: 60px auto; padding: 0 16px; }
    .card { background: white; border-radius: 10px; padding: 28px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); }
    h1 { color: #1e3a8a; margin-top: 0; font-size: 20px; }
    label { display: block; font-size: 13px; color: #334155; margin: 14px 0 4px; }
    input { width: 100%; padding: 11px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 14px; }
    .hint { font-size: 12px; color: #64748b; margin-top: 4px; }
    .error { background: #fee2e2; color: #991b1b; padding: 10px 12px; border-radius: 6px; font-size: 13px; }
    .actions { display: flex; gap: 10px; margin-top: 20px; }
    .btn { padding: 11px 18px; background: #2563eb; color: white; border: none; border-radius: 6px; font-size: 14px; cursor: pointer; text-decoration: none; text-align: center; }
    .btn-secondary { background: #e2e8f0; color: #334155; }
</style>
</head>
<body>
<div class="wrapper">
    <div class="card">
        <h1>🔒 {% if forced %}Задайте новый пароль{% else %}Смена пароля{% endif %}</h1>
        {% if forced %}<p class="hint">Администратор выдал временный пароль — его нужно заменить.</p>{% endif %}
        {% if error %}<p class="error">{{ error }}</p>{% endif %}
        <form method="post" autocomplete="off">
            <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
            {% if not forced %}
            <label>Текущий пароль</label>
            <input type="password" name="current_password" autocomplete="current-password" required>
            {% endif %}
            <label>Новый пароль</label>
            <input type="password" name="new_password" autocomplete="new-password" required>
            <div class="hint">Не короче {{ min_length }} символов.</div>
            <label>Повторите новый пароль</label>
            <input type="password" name="new_password_confirm" autocomplete="new-password" required>
            <div class="actions">
                <button class="btn" type="submit">Сохранить пароль</button>
                <a class="btn btn-secondary" href="/f/">← К файлам</a>
            </div>
        </form>
    </div>
</div>
</body>
</html>
"""


USERS_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Пользователи - АЛЬБИОН</title>
<style>
    * { box-sizing: border-box; }
    body { font-family: 'Segoe UI', Arial, sans-serif; background: #f4f4f9; margin: 0; }
    header { background: #1e3a8a; color: white; padding: 16px 24px; display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
    header h1 { font-size: 18px; margin: 0; }
    .btn { padding: 9px 14px; background: #2563eb; color: white; border: none; border-radius: 6px; font-size: 13px; cursor: pointer; text-decoration: none; display: inline-block; }
    .btn-sm { padding: 6px 10px; font-size: 12px; }
    .btn-secondary { background: #e2e8f0; color: #334155; }
    .btn-danger { background: #dc2626; }
    .main { max-width: 1100px; margin: 20px auto; padding: 0 16px 40px; display: flex; flex-direction: column; gap: 18px; }
    .card { background: white; border-radius: 10px; padding: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); }
    .card h2 { font-size: 16px; color: #1e3a8a; margin-top: 0; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #eef2f7; vertical-align: middle; }
    th { color: #64748b; font-weight: 600; font-size: 12px; }
    .muted { color: #94a3b8; font-size: 12px; }
    .pill { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
    .pill-admin { background: #dbeafe; color: #1e40af; }
    .pill-user { background: #e2e8f0; color: #475569; }
    .pill-off { background: #fee2e2; color: #991b1b; }
    .error { background: #fee2e2; color: #991b1b; padding: 10px 12px; border-radius: 6px; font-size: 13px; margin-bottom: 12px; }
    .notice { background: #dcfce7; color: #166534; padding: 10px 12px; border-radius: 6px; font-size: 13px; margin-bottom: 12px; }
    .secret { font-family: monospace; background: #0f172a; color: #e2e8f0; padding: 10px 12px; border-radius: 6px; display: inline-block; margin: 6px 0; }
    form.inline { display: inline; }
    .row-actions { display: flex; gap: 6px; flex-wrap: wrap; }
    label { display: block; font-size: 13px; color: #334155; margin: 10px 0 4px; }
    input[type=text], input[type=password], select, textarea { width: 100%; padding: 9px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 13px; }
    textarea { min-height: 110px; font-family: monospace; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; }
    .checkline { display: flex; align-items: center; gap: 8px; margin-top: 10px; font-size: 13px; }
    .checkline input { width: auto; }
    .hint { font-size: 12px; color: #64748b; }
</style>
</head>
<body>
<header>
    <h1>👥 Пользователи</h1>
    <a href="/settings" class="btn btn-sm" style="margin-left:auto;background:rgba(255,255,255,.18);">⚙️ Настройки</a>
    <a href="/settings/tokens" class="btn btn-sm" style="background:rgba(255,255,255,.18);">🔑 Токены</a>
    <a href="/f/" class="btn btn-sm" style="background:rgba(255,255,255,.18);">← К файлам</a>
    <a href="/logout" class="btn btn-sm" style="background:rgba(255,255,255,.18);">Выйти</a>
</header>
<div class="main">
    {% if error %}<div class="error">{{ error }}</div>{% endif %}
    {% if notice %}<div class="notice">{{ notice }}</div>{% endif %}
    {% if new_secret %}
    <div class="card" style="border-left:4px solid #16a34a;">
        <h2>Пароль для «{{ new_secret.login }}»</h2>
        <div class="hint">Показывается один раз — передайте пользователю и попросите сменить при входе.</div>
        <div class="secret">{{ new_secret.password }}</div>
    </div>
    {% endif %}
    {% if import_report %}
    <div class="card">
        <h2>Импорт из CSV</h2>
        {% if import_report.created %}
        <p>Создано учётных записей: <b>{{ import_report.created|length }}</b></p>
        <table>
            <tr><th>Логин</th><th>Роль</th><th>Временный пароль</th></tr>
            {% for row in import_report.created %}
            <tr><td>{{ row.login }}</td><td>{{ row.role }}</td>
                <td>{% if row.password %}<span class="secret">{{ row.password }}</span>{% else %}<span class="muted">задан в файле</span>{% endif %}</td></tr>
            {% endfor %}
        </table>
        {% endif %}
        {% if import_report.errors %}
        <p class="error" style="margin-top:12px;">Отклонённые строки:</p>
        <ul style="font-size:13px;color:#991b1b;">
            {% for line in import_report.errors %}<li>{{ line }}</li>{% endfor %}
        </ul>
        {% endif %}
    </div>
    {% endif %}

    <div class="card">
        <h2>Учётные записи ({{ users|length }})</h2>
        <table>
            <tr>
                <th>Логин</th><th>Имя</th><th>Роль</th><th>Состояние</th>
                <th>Внешний вход</th><th>Вход</th><th>Действия</th>
            </tr>
            {% for u in users %}
            <tr>
                <td><b>{{ u.login }}</b>{% if u.must_change_password %}<div class="muted">сменит пароль при входе</div>{% endif %}</td>
                <td>{{ u.display_name }}{% if u.email %}<div class="muted">{{ u.email }}</div>{% endif %}</td>
                <td><span class="pill pill-{{ u.role }}">{{ role_titles[u.role] }}</span></td>
                <td>{% if u.active %}<span class="pill pill-user">активна</span>{% else %}<span class="pill pill-off">отключена</span>{% endif %}
                    {% if locked[u.id] %}<div class="muted">заблокирована</div>{% endif %}</td>
                <td>{% if u.allow_remote %}разрешён{% else %}<span class="muted">только локальная сеть</span>{% endif %}</td>
                <td class="muted">{{ u.last_login_at or '—' }}<div>{{ session_counts.get(u.id, 0) }} сессий</div></td>
                <td>
                    <div class="row-actions">
                        <form class="inline" method="post" action="/settings/users/update">
                            <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                            <input type="hidden" name="user_id" value="{{ u.id }}">
                            <input type="hidden" name="action" value="{{ 'deactivate' if u.active else 'activate' }}">
                            <button class="btn btn-sm btn-secondary" type="submit">{{ 'Отключить' if u.active else 'Включить' }}</button>
                        </form>
                        <form class="inline" method="post" action="/settings/users/update">
                            <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                            <input type="hidden" name="user_id" value="{{ u.id }}">
                            <input type="hidden" name="action" value="{{ 'to_user' if u.role == 'admin' else 'to_admin' }}">
                            <button class="btn btn-sm btn-secondary" type="submit">{{ 'Сделать пользователем' if u.role == 'admin' else 'Сделать админом' }}</button>
                        </form>
                        <form class="inline" method="post" action="/settings/users/update">
                            <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                            <input type="hidden" name="user_id" value="{{ u.id }}">
                            <input type="hidden" name="action" value="{{ 'deny_remote' if u.allow_remote else 'allow_remote' }}">
                            <button class="btn btn-sm btn-secondary" type="submit">{{ 'Запретить снаружи' if u.allow_remote else 'Разрешить снаружи' }}</button>
                        </form>
                        <form class="inline" method="post" action="/settings/users/reset">
                            <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                            <input type="hidden" name="user_id" value="{{ u.id }}">
                            <button class="btn btn-sm" type="submit">Сбросить пароль</button>
                        </form>
                        <form class="inline" method="post" action="/settings/users/revoke">
                            <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                            <input type="hidden" name="user_id" value="{{ u.id }}">
                            <button class="btn btn-sm btn-secondary" type="submit">Завершить сессии</button>
                        </form>
                        <form class="inline" method="post" action="/settings/users/delete"
                              onsubmit="return confirm('Удалить учётную запись и её сессии?');">
                            <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                            <input type="hidden" name="user_id" value="{{ u.id }}">
                            <button class="btn btn-sm btn-danger" type="submit">Удалить</button>
                        </form>
                    </div>
                </td>
            </tr>
            {% endfor %}
        </table>
    </div>

    <div class="card">
        <h2>Новая учётная запись</h2>
        <form method="post" action="/settings/users/create" autocomplete="off">
            <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
            <div class="grid">
                <div>
                    <label>Логин</label>
                    <input type="text" name="login" required placeholder="ivan">
                    <div class="hint">3–64 символа: латиница, цифры, . _ @ + -</div>
                </div>
                <div>
                    <label>Имя</label>
                    <input type="text" name="display_name" placeholder="Иван Петров">
                </div>
                <div>
                    <label>Роль</label>
                    <select name="role">
                        <option value="user">Пользователь</option>
                        <option value="admin">Администратор</option>
                    </select>
                </div>
                <div>
                    <label>Пароль</label>
                    <input type="text" name="password" placeholder="оставьте пустым — сгенерируется">
                    <div class="hint">Не короче {{ min_length }} символов</div>
                </div>
            </div>
            <label class="checkline"><input type="checkbox" name="allow_remote" value="1"> Разрешить вход из интернета</label>
            <div class="actions" style="margin-top:16px;">
                <button class="btn" type="submit">Создать</button>
            </div>
        </form>
    </div>

    <div class="card">
        <h2>Импорт из CSV</h2>
        <div class="hint">Колонки: <code>login</code>, <code>display_name</code>, <code>role</code>, <code>allow_remote</code>, <code>password</code>.
            Обязательна только <code>login</code>; пустой пароль означает «сгенерировать».</div>
        <form method="post" action="/settings/users/import">
            <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
            <label>Вставьте содержимое CSV</label>
            <textarea name="csv_text" placeholder="login;display_name;role;allow_remote&#10;ivan;Иван Петров;user;да"></textarea>
            <div class="actions" style="margin-top:12px;">
                <button class="btn" type="submit">Импортировать</button>
            </div>
        </form>
    </div>
</div>
</body>
</html>
"""


def _render_users_page(error=None, notice=None, import_report=None, new_secret=None, status=200):
    users = user_store.list_users()
    counts = {}
    for record in session_store.list_active():
        counts[record['user_id']] = counts.get(record['user_id'], 0) + 1
    body = render_template_string(
        USERS_TEMPLATE,
        users=users,
        locked={user['id']: user_store.is_locked(user) for user in users},
        session_counts=counts,
        role_titles=user_store.ROLE_TITLES,
        min_length=user_store.MIN_PASSWORD_LENGTH,
        csrf_token=session.setdefault('_csrf_token', secrets.token_urlsafe(32)),
        error=error,
        notice=notice,
        import_report=import_report,
        new_secret=new_secret,
    )
    return body, status


@app.route('/settings/users')
@admin_required
def users_page():
    if not multi_user_auth():
        return _render_users_page(
            error='Управление пользователями доступно в режиме ALBION_METADATA_BACKEND=sqlite.', status=400)
    pending = session.pop('_users_secret', None)
    report = session.pop('_users_import', None)
    return _render_users_page(new_secret=pending, import_report=report,
                              notice=request.args.get('saved') == '1' and 'Сохранено.' or None)


@app.route('/settings/users/create', methods=['POST'])
@admin_required
def user_create():
    if not multi_user_auth():
        return _render_users_page(error='Режим JSON: управление пользователями недоступно.', status=400)
    login_name = request.form.get('login', '').strip()
    password = request.form.get('password', '')
    role = request.form.get('role', 'user')
    generated = False
    if not password:
        password = user_store.generate_password()
        generated = True
    try:
        user = user_store.create_user(
            login_name, password, role=role,
            display_name=request.form.get('display_name') or None,
            allow_remote=request.form.get('allow_remote') == '1',
            must_change_password=generated)
    except user_store.UserError as exc:
        return _render_users_page(error=str(exc), status=400)
    app.logger.info('Создана учётная запись «%s» (%s) администратором «%s»',
                    user['login'], user['role'], session.get('username'))
    if generated:
        session['_users_secret'] = {'login': user['login'], 'password': password}
    return redirect('/settings/users?saved=1')


@app.route('/settings/users/update', methods=['POST'])
@admin_required
def user_update():
    if not multi_user_auth():
        return _render_users_page(error='Режим JSON: управление пользователями недоступно.', status=400)
    user_id = request.form.get('user_id', '')
    action = request.form.get('action', '')
    actions = {
        'activate': {'active': True},
        'deactivate': {'active': False},
        'to_admin': {'role': 'admin'},
        'to_user': {'role': 'user'},
        'allow_remote': {'allow_remote': True},
        'deny_remote': {'allow_remote': False},
    }
    if action not in actions:
        return _render_users_page(error='Неизвестное действие.', status=400)
    try:
        user = user_store.update_user(user_id, **actions[action])
    except user_store.UserError as exc:
        return _render_users_page(error=str(exc), status=400)
    if action == 'deactivate' or action == 'to_user' or action == 'deny_remote':
        # Изменение прав немедленно завершает уже открытые сессии этой учётки.
        session_store.revoke_all_for_user(user['id'])
    app.logger.info('Учётная запись «%s» изменена (%s) администратором «%s»',
                    user['login'], action, session.get('username'))
    return redirect('/settings/users?saved=1')


@app.route('/settings/users/reset', methods=['POST'])
@admin_required
def user_reset_password():
    if not multi_user_auth():
        return _render_users_page(error='Режим JSON: управление пользователями недоступно.', status=400)
    user = user_store.get_user_by_id(request.form.get('user_id', ''))
    if not user:
        return _render_users_page(error='Учётная запись не найдена.', status=404)
    password = user_store.generate_password()
    try:
        user_store.set_password(user['id'], password, must_change=True)
    except user_store.UserError as exc:
        return _render_users_page(error=str(exc), status=400)
    session_store.revoke_all_for_user(user['id'])
    session['_users_secret'] = {'login': user['login'], 'password': password}
    app.logger.info('Пароль «%s» сброшен администратором «%s»', user['login'], session.get('username'))
    return redirect('/settings/users?saved=1')


@app.route('/settings/users/revoke', methods=['POST'])
@admin_required
def user_revoke_sessions():
    user = user_store.get_user_by_id(request.form.get('user_id', ''))
    if not user:
        return _render_users_page(error='Учётная запись не найдена.', status=404)
    revoked = session_store.revoke_all_for_user(user['id'])
    app.logger.info('Завершены сессии «%s» (%d) администратором «%s»',
                    user['login'], revoked, session.get('username'))
    return _render_users_page(notice=f'Завершено сессий: {revoked}.')


@app.route('/settings/users/delete', methods=['POST'])
@admin_required
def user_delete():
    if not multi_user_auth():
        return _render_users_page(error='Режим JSON: управление пользователями недоступно.', status=400)
    user = user_store.get_user_by_id(request.form.get('user_id', ''))
    if not user:
        return _render_users_page(error='Учётная запись не найдена.', status=404)
    if user['id'] == (current_user() or {}).get('id'):
        return _render_users_page(error='Нельзя удалить учётную запись, под которой вы работаете.', status=400)
    try:
        user_store.delete_user(user['id'])
    except user_store.UserError as exc:
        return _render_users_page(error=str(exc), status=400)
    session_store.revoke_all_for_user(user['id'])
    app.logger.info('Учётная запись «%s» удалена администратором «%s»',
                    user['login'], session.get('username'))
    return redirect('/settings/users?saved=1')


@app.route('/settings/users/import', methods=['POST'])
@admin_required
def users_import():
    if not multi_user_auth():
        return _render_users_page(error='Режим JSON: управление пользователями недоступно.', status=400)
    text = request.form.get('csv_text', '')
    uploaded = request.files.get('csv_file')
    if uploaded and uploaded.filename:
        try:
            text = uploaded.read().decode('utf-8-sig', errors='replace')
        except (OSError, UnicodeDecodeError) as exc:
            return _render_users_page(error=f'Файл не прочитан: {exc}', status=400)
    try:
        created, errors = user_store.import_csv(text)
    except user_store.UserError as exc:
        return _render_users_page(error=str(exc), status=400)
    app.logger.info('Импорт CSV: создано %d, отклонено %d (администратор «%s»)',
                    len(created), len(errors), session.get('username'))
    return _render_users_page(import_report={'created': created, 'errors': errors})


@app.route('/d/<folder_ref>')
@limiter.limit("60 per minute", deduct_when=lambda response: rate_limit_applies())
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

    # Publish only this folder's direct files. Do not expose parent path or
    # child-folder links: each subfolder must be shared separately.
    breadcrumbs_data = []
    subfolders_data = []

    files_data = []
    db = load_db()
    for internal_id, record in db.items():
        if record.get('folder_id') != fid:
            continue
        physical_path = record.get('physical_path', record['current_filename'])
        safe_path = safe_upload_path(physical_path)
        if not safe_path or not os.path.isfile(safe_path):
            continue
        files_data.append({
            'internal_id': internal_id,
            'name': record['display_name'],
            'ext': record['ext'],
            'icon': get_file_icon(record['ext']),
            'is_image': record.get('is_image', False),
            'size': format_size(record.get('size', 0)),
            'mtime': record['mtime'],
            'download_url': f"{BASE_URL}/{url_path(physical_path)}",
            'view_url': f"{BASE_URL}/v/{url_path(physical_path)}",
            'thumb_url': f"/thumb/mini/{internal_id}",
            'thumb_medium_url': f"/thumb/medium/{internal_id}",
        })
    files_data.sort(key=lambda x: x['mtime'], reverse=True)
    total_files = len(files_data)
    per_page = 50
    total_pages = max(1, (total_files + per_page - 1) // per_page)
    try:
        page = int(request.args.get('page', '1'))
    except (TypeError, ValueError):
        page = 1
    page = min(max(1, page), total_pages)
    start = (page - 1) * per_page
    page_files = files_data[start:start + per_page]
    public_base = f'/d/{urllib.parse.quote(str(folder_ref), safe="")}'
    next_url = f'{public_base}?page={page + 1}' if page < total_pages else None

    app.logger.info('Публичный просмотр папки "%s" (%s) от %s',
                    folder['name'], fid, request.remote_addr)

    return render_template_string(PUBLIC_FOLDER_TEMPLATE,
        folder=folder,
        breadcrumbs=breadcrumbs_data,
        subfolders=subfolders_data,
        files=page_files,
        total_files=total_files,
        total_pages=total_pages,
        page=page,
        next_url=next_url)

# ===== ИНТЕГРАЦИЯ: МАШИННЫЕ ТОКЕНЫ (без сессии и CSRF) =====
INTEGRATION_TTL_MAX_DAYS = 3650
INTEGRATION_UPLOAD_RATE = '60 per minute'

# Для интеграции разрешены ещё и файлы базы данных; веб-загрузка их по-прежнему
# не принимает (ALLOWED_EXTENSIONS не меняем).
INTEGRATION_EXTRA_EXTENSIONS = {'db', 'sqlite', 'sqlite3'}


def allowed_integration_file(filename):
    if not filename or '.' not in filename:
        return False
    ext = filename.rsplit('.', 1)[1].lower()
    return ext in ALLOWED_EXTENSIONS or ext in INTEGRATION_EXTRA_EXTENSIONS

def _integration_ttl_days():
    """TTL по умолчанию из ALBION_INTEGRATION_TTL_DAYS, всегда в диапазоне 1..3650."""
    raw = os.environ.get('ALBION_INTEGRATION_TTL_DAYS', '90')
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 90
    return max(1, min(INTEGRATION_TTL_MAX_DAYS, value))

@app.route('/api/v1/integration/verify', methods=['GET'])
def integration_verify():
    """Проверка токена и его прав. Ничего не меняет."""
    # Для интеграции нужен именно upload: download-only токен не подходит.
    meta, error = require_api_scope('upload')
    if error:
        return error
    max_upload_gb = load_quota_settings()['max_upload_gb']
    if float(max_upload_gb).is_integer():
        max_upload_gb = int(max_upload_gb)
    return jsonify({
        'ok': True,
        'name': meta.get('name'),
        'scopes': meta.get('scopes') or ['download'],
        'folder_id': meta.get('folder_id'),
        'token_expires_at': meta.get('expires_at'),
        'base_url': BASE_URL,
        'limits': {'max_upload_gb': max_upload_gb},
    })

@app.route('/api/v1/integration/upload', methods=['POST'])
@limiter.limit(INTEGRATION_UPLOAD_RATE, key_func=_integration_rate_limit_key)
def integration_upload():
    """Приём одного файла от доверенного сервиса по X-API-Key."""
    meta, error = require_api_scope('upload')
    if error:
        return error

    # Запись всегда ограничена папкой, выданной вместе с токеном.
    token_folder = meta.get('folder_id')
    requested_folder = (request.form.get('folder_id') or '').strip() or None
    if not token_folder:
        return jsonify({'error': 'Токен не привязан к папке'}), 403
    if requested_folder and requested_folder != token_folder:
        return jsonify({'error': 'Токен привязан к другой папке'}), 403
    folder_id = token_folder

    folders = load_folders()
    if folder_id not in folders:
        return jsonify({'error': 'Папка не найдена'}), 404

    upload = request.files.get('file')
    if upload is None or not upload.filename:
        return jsonify({'error': 'Нет файла (ожидается поле file)'}), 400

    original_name = upload.filename.replace('+', '_')
    if (not original_name or original_name in {'.', '..'}
            or '\0' in original_name or os.path.isabs(original_name)
            or '/' in original_name or '\\' in original_name
            or os.path.basename(original_name) != original_name):
        return jsonify({'error': 'Недопустимое имя файла'}), 400
    if not is_valid_filename(original_name):
        return jsonify({'error': 'Недопустимое имя файла'}), 400
    if not allowed_integration_file(original_name):
        return jsonify({'error': 'Недопустимое расширение файла'}), 400

    display_name = (request.form.get('display_name') or '').strip() or original_name
    if '\0' in display_name:
        return jsonify({'error': 'Недопустимое display_name'}), 400
    display_name = display_name[:255]

    raw_ttl = request.form.get('ttl_days')
    if raw_ttl in (None, ''):
        ttl_days = _integration_ttl_days()
    else:
        try:
            ttl_days = int(raw_ttl)
        except (TypeError, ValueError):
            return jsonify({'error': 'ttl_days должен быть целым числом'}), 400
        ttl_days = max(1, min(INTEGRATION_TTL_MAX_DAYS, ttl_days))
    expires_at = (datetime.now(timezone.utc) + timedelta(days=ttl_days)).isoformat()

    with UPLOAD_LOCK:
        db = load_db()
        capacity_error = upload_capacity_error(request.content_length, db)
        if capacity_error:
            status, message = capacity_error
            app.logger.warning('Интеграционная загрузка отклонена: %s (токен=%s, content_length=%s)',
                               message, meta.get('name'), request.content_length)
            return jsonify({'error': message}), status

        folder_physical_path = get_folder_physical_path(folder_id, folders)
        os.makedirs(folder_physical_path, exist_ok=True)

        existing_names = set()
        for record in db.values():
            if record.get('folder_id') == folder_id:
                existing_names.add(record['current_filename'])
        for fname in os.listdir(folder_physical_path):
            if os.path.isfile(os.path.join(folder_physical_path, fname)):
                existing_names.add(fname)

        internal_id = str(uuid.uuid4())
        ext = get_file_ext(original_name)
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

        physical_path = os.path.relpath(
            os.path.join(folder_physical_path, current_filename), UPLOAD_FOLDER)
        save_path = safe_upload_path(physical_path)
        if save_path is None:
            app.logger.error('Интеграционная загрузка: небезопасный путь %r', physical_path)
            return jsonify({'error': 'Недопустимый путь сохранения'}), 400

        # Атомарная запись: tmp рядом с целевым файлом + os.replace.
        tmp_path = os.path.join(os.path.dirname(save_path), f'.{internal_id}.part')
        try:
            upload.save(tmp_path)
            os.replace(tmp_path, save_path)
        except OSError as e:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            app.logger.error('Интеграционная загрузка: не удалось сохранить %s: %s', physical_path, e)
            return jsonify({'error': 'Не удалось сохранить файл'}), 500

        size = os.path.getsize(save_path)
        db[internal_id] = {
            'access_id': internal_id,
            'current_filename': current_filename,
            'physical_path': physical_path,
            'original_filename': original_name,
            'display_name': display_name,
            'folder_id': folder_id,
            'custom_alias': '',
            'slug': make_short_slug(original_name, db, exclude_id=internal_id, folders=folders),
            'link_rotated': False,
            'download_count': 0,
            'size': size,
            'mtime': datetime.fromtimestamp(os.path.getmtime(save_path)).strftime('%d.%m.%Y %H:%M'),
            'ext': ext.upper() if ext else '—',
            'is_image': ext in IMAGE_EXTENSIONS,
        }
        save_db(db)
        update_last_upload()

    slug = db[internal_id]['slug']
    public_url = f"{BASE_URL}/{slug}"
    app.logger.info('Интеграционная загрузка: токен=%s, файл=%s (%s), папка=%s',
                    meta.get('name'), physical_path, format_size(size), folder_id)
    return jsonify({
        'file_id': internal_id,
        'url': public_url,
        'short_url': public_url,
        'object_key': 'uploads/' + physical_path.replace(os.sep, '/'),
        'name': current_filename,
        'folder_id': folder_id,
        'expires_at': expires_at,
    })

@app.route('/api/v1/integration/files', methods=['GET'])
@limiter.limit(INTEGRATION_UPLOAD_RATE, key_func=_integration_rate_limit_key)
def integration_list_files():
    """Список файлов папки, к которой привязан токен (право download)."""
    meta, error = require_api_scope('download')
    if error:
        return error

    folder_id = meta.get('folder_id')
    if not folder_id:
        return jsonify({'error': 'Токен не привязан к папке'}), 403

    folders = load_folders()
    if folder_id not in folders:
        return jsonify({'error': 'Папка не найдена'}), 404

    db = load_db()
    files = []
    for internal_id, record in db.items():
        if record.get('folder_id') != folder_id:
            continue
        physical_path = record.get('physical_path', record.get('current_filename'))
        safe_path = safe_upload_path(physical_path)
        if not safe_path or not os.path.isfile(safe_path):
            continue
        if record.get('deleted_at'):
            continue
        files.append({
            'file_id': internal_id,
            'name': record.get('current_filename'),
            'display_name': record.get('display_name'),
            'ext': record.get('ext'),
            'size': os.path.getsize(safe_path),
            'mtime': os.path.getmtime(safe_path),
            'url': f"{BASE_URL}/{url_path(physical_path)}",
        })

    files.sort(key=lambda item: item['mtime'], reverse=True)
    return jsonify({'folder_id': folder_id, 'files': files})


@app.route('/api/v1/integration/files/<file_id>/download', methods=['GET'])
@limiter.limit(INTEGRATION_UPLOAD_RATE, key_func=_integration_rate_limit_key)
def integration_download_file(file_id):
    """Скачивание файла из папки токена (право download)."""
    meta, error = require_api_scope('download')
    if error:
        return error

    folder_id = meta.get('folder_id')
    if not folder_id:
        return jsonify({'error': 'Токен не привязан к папке'}), 403

    db = load_db()
    record = db.get(file_id)
    if not record or record.get('deleted_at'):
        return jsonify({'error': 'Файл не найден'}), 404
    if record.get('folder_id') != folder_id:
        return jsonify({'error': 'Файл находится в другой папке'}), 403

    physical_path = record.get('physical_path', record.get('current_filename'))
    safe_path = safe_upload_path(physical_path)
    if not safe_path or not os.path.isfile(safe_path):
        return jsonify({'error': 'Файл не найден на диске'}), 404

    return send_file(
        safe_path,
        as_attachment=True,
        download_name=record.get('current_filename') or os.path.basename(safe_path),
        max_age=0,
    )


@app.route('/api/v1/integration/files/<file_id>', methods=['DELETE'])
@limiter.limit(INTEGRATION_UPLOAD_RATE, key_func=_integration_rate_limit_key)
@serialized_mutation
def integration_delete_file(file_id):
    """Удаление файла, загруженного интеграцией (право delete)."""
    meta, error = require_api_scope('delete')
    if error:
        return error

    db = load_db()
    record = db.get(file_id)
    if not record:
        return jsonify({'error': 'Файл не найден'}), 404

    token_folder = meta.get('folder_id')
    if token_folder and record.get('folder_id') != token_folder:
        return jsonify({'error': 'Файл находится в другой папке'}), 403

    physical_path = record.get('physical_path') or record.get('current_filename')
    if safe_upload_path(physical_path) is None:
        app.logger.error('Интеграционное удаление: небезопасный путь %r', physical_path)
        return jsonify({'error': 'Небезопасный путь файла'}), 400

    trash = load_trash()
    entry_id = move_file_to_trash(file_id, record, db, trash)
    save_db(db)
    if entry_id:
        save_trash(trash)
    app.logger.info('Интеграционное удаление: токен=%s, файл=%s',
                    meta.get('name'), physical_path)
    return jsonify({'deleted': True, 'file_id': file_id, 'trash_id': entry_id})

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
        created_ids = []
        db = load_db()

        capacity_error = upload_capacity_error(request.content_length, db)
        if capacity_error:
            status, message = capacity_error
            app.logger.warning('Загрузка отклонена: %s (content_length=%s, файлов=%d)',
                               message, request.content_length, len(files))
            return jsonify({'error': message}), status

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

            save_path = safe_upload_path(physical_path)
            if save_path is None:
                app.logger.error('Загрузка отклонена: небезопасный путь %r', physical_path)
                continue
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
            created_ids.append(internal_id)
            app.logger.info('Загружен файл: %s (%s), короткая ссылка: /%s',
                            physical_path, format_size(os.path.getsize(save_path)), db[internal_id]['slug'])

        if saved_count > 0:
            save_db(db)
            update_last_upload()
            app.logger.info('Загрузка завершена: %d файл(ов) в папку %s', saved_count, folder_id or 'корень')

    view = request.form.get('view') if request.form.get('view') in BROWSE_VIEWS else 'table'
    rows_html = ''.join(str(render_file_row(file_view_record(fid, db[fid]), view))
                        for fid in created_ids if fid in db)
    return jsonify({'count': saved_count, 'rows_html': rows_html, 'view': view})

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
    created_ids = []

    file_data = []
    for i in range(count):
        file = request.files.get(f'file_{i}')
        rel_path = request.form.get(f'path_{i}', '')
        if file and file.filename:
            file_data.append((file, rel_path))

    with UPLOAD_LOCK:
        db = load_db()

        capacity_error = upload_capacity_error(request.content_length, db)
        if capacity_error:
            status, message = capacity_error
            app.logger.warning('Загрузка папки отклонена: %s (content_length=%s, файлов=%d)',
                               message, request.content_length, len(file_data))
            return jsonify({'error': message}), status

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
            current_rel = os.path.relpath(current_physical, UPLOAD_FOLDER)
            safe_current = safe_upload_path(current_rel)
            if safe_current is None:
                app.logger.error('Загрузка папки отклонена: небезопасная папка %r', current_rel)
                continue
            os.makedirs(safe_current, exist_ok=True)

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
            save_path = safe_upload_path(physical_path)
            if save_path is None:
                app.logger.error('Загрузка папки: "%s" отклонена страховкой realpath (%s)', rel_path, physical_path)
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
            created_ids.append(internal_id)
            app.logger.info('Загружен файл из папки: %s (%s), короткая ссылка: /%s',
                            physical_path, format_size(os.path.getsize(save_path)), db[internal_id]['slug'])

        if saved_count > 0 or created_folders:
            save_db(db)
            update_last_upload()

    app.logger.info('Загрузка папки завершена: %d файл(ов), %d папок создано (принято в запросе: %d)',
                    saved_count, len(created_folders), count)
    view = request.form.get('view') if request.form.get('view') in BROWSE_VIEWS else 'table'
    stats_map = get_folder_stats_map(folders, db)
    rows_html = ''.join(str(render_file_row(file_view_record(fid, db[fid]), view))
                        for fid in created_ids if fid in db)
    # В текущем списке видны только папки — прямые потомки открытой папки.
    rows_html += ''.join(
        str(render_folder_row(folder_view_record(fid, folders, stats_map), view))
        for fid in created_folders
        if fid in folders and folders[fid].get('parent_id') == folder_id)
    return jsonify({'count': saved_count, 'folders_created': len(created_folders),
                    'rows_html': rows_html, 'view': view})

@app.route('/thumb/<size>/<file_id>', methods=['GET', 'HEAD'])
@limiter.exempt
def thumbnail_file(size, file_id):
    """Serve a cached mini/medium preview without incrementing view_count.

    Anonymous preview loads still consume the download budget so /thumb/ cannot
    become an unlimited full-size download bypass. Opening /v/ remains the
    explicit, counted view action.
    """
    if size not in THUMBNAIL_SIZES:
        abort(404)
    record = load_db().get(file_id)
    if not isinstance(record, dict) or not record.get('is_image'):
        abort(404)
    thumb_path = ensure_thumbnail(record, size)
    if not thumb_path:
        abort(404)
    if request.method != 'HEAD' and not session.get('logged_in'):
        retry_after = check_download_budget(request.remote_addr, os.path.getsize(thumb_path))
        if retry_after:
            response = jsonify({'error': 'Бюджет скачивания исчерпан'})
            response.headers['Retry-After'] = str(retry_after)
            return response, 429
    return send_file(thumb_path, mimetype='image/webp', as_attachment=False,
                     conditional=True, max_age=3600)


@app.route('/v/<path:filepath>')
@limiter.limit("200 per minute",
               deduct_when=lambda response: g.get('api_key') is None and rate_limit_applies())
def view_file(filepath):
    try:
        filepath = urllib.parse.unquote(filepath)
    except:
        pass

    # Счётчик растёт только для реальных файлов в БД
    full_path = safe_upload_path(filepath)
    if not full_path or not os.path.isfile(full_path):
        return "Файл не найден", 404

    db = load_db()
    record = next((r for r in db.values() if r.get('physical_path') == filepath), None)
    if record is None:
        return "Файл не найден", 404

    if request.method == 'HEAD':
        return send_from_directory(UPLOAD_FOLDER, filepath, as_attachment=False)

    retry_after = check_download_budget(request.remote_addr, record.get('size', 0) or 0)
    if retry_after:
        app.logger.warning('Бюджет просмотра исчерпан (%s), файл %s, retry через %d c',
                           request.remote_addr, filepath, retry_after)
        resp = jsonify({'error': f'Слишком много скачиваний, повторите через {max(1, retry_after // 60)} мин'})
        resp.headers['Retry-After'] = str(retry_after)
        return resp, 429

    updated = bump_counter(lambda r: r.get('physical_path') == filepath, 'view_count')
    if updated is None:
        return "Файл не найден", 404
    app.logger.info('Просмотр: %s (👁=%d) [%s]', filepath, updated.get('view_count', 0),
                    g.get('api_key') or 'анонимно')
    return send_from_directory(UPLOAD_FOLDER, filepath, as_attachment=False)

@app.route('/api/update_file', methods=['POST'])
@login_required
@serialized_mutation
def update_file():
    data = get_json_object()
    if data is None:
        return jsonify({'error': 'Ожидается JSON-объект'}), 400
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

    # Алиас проверяем ДО переименования на диске: иначе ошибка алиаса
    # оставила бы файл переименованным, а запись в БД — со старым путём.
    if custom_alias:
        if not re.match(r'^[a-zA-Z0-9_-]+$', custom_alias):
            return jsonify({'error': 'Алиас: только латиница, цифры, дефис'}), 400
        # Алиас не может маскироваться под системный маршрут
        if custom_alias.lower() in RESERVED_PATHS:
            return jsonify({'error': 'Этот алиас зарезервирован системой'}), 400
        for fid, rec in db.items():
            if fid != internal_id and rec.get('custom_alias') == custom_alias:
                return jsonify({'error': 'Алиас уже занят'}), 400

    new_folder_candidate = get_folder_physical_path(new_folder_id, folders)
    new_folder_rel = os.path.relpath(new_folder_candidate, UPLOAD_FOLDER)
    new_folder_physical = safe_upload_path(new_folder_rel, allow_root=True)
    old_full_path = safe_upload_path(old_physical_path)
    if new_folder_physical is None or old_full_path is None:
        return jsonify({'error': 'Небезопасный путь файла или папки'}), 400
    os.makedirs(new_folder_physical, exist_ok=True)

    new_physical_path = os.path.join(new_folder_physical, physical_filename)
    new_rel_path = os.path.relpath(new_physical_path, UPLOAD_FOLDER)
    new_physical_path = safe_upload_path(new_rel_path)
    if new_physical_path is None:
        return jsonify({'error': 'Небезопасный путь назначения'}), 400

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
        record['custom_alias'] = custom_alias

    save_db(db)
    view = data.get('view') if data.get('view') in BROWSE_VIEWS else 'table'
    row_html = str(render_file_row(file_view_record(internal_id, record), view))
    return jsonify({'success': True, 'row_html': row_html, 'view': view,
                    'folder_id': record.get('folder_id')})

@app.route('/api/move_file', methods=['POST'])
@login_required
@serialized_mutation
def move_file():
    data = get_json_object()
    if data is None:
        return jsonify({'error': 'Ожидается JSON-объект'}), 400
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

    new_folder_candidate = get_folder_physical_path(target_folder_id, folders)
    new_folder_rel = os.path.relpath(new_folder_candidate, UPLOAD_FOLDER)
    new_folder_physical = safe_upload_path(new_folder_rel, allow_root=True)
    old_full_path = safe_upload_path(old_physical_path)
    if new_folder_physical is None or old_full_path is None:
        return jsonify({'error': 'Небезопасный путь файла или папки'}), 400
    os.makedirs(new_folder_physical, exist_ok=True)

    new_physical_path = os.path.join(new_folder_physical, old_filename)
    new_rel_path = os.path.relpath(new_physical_path, UPLOAD_FOLDER)
    new_physical_path = safe_upload_path(new_rel_path)
    if new_physical_path is None:
        return jsonify({'error': 'Небезопасный путь назначения'}), 400

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

    moved = False
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
            moved = True
        except Exception as e:
            return jsonify({'error': f'Ошибка: {str(e)}'}), 500

    save_db(db)
    return jsonify({'success': True, 'moved': moved})

@app.route('/api/move_files_batch', methods=['POST'])
@login_required
@serialized_mutation
def move_files_batch():
    data = get_json_object()
    if data is None:
        return jsonify({'error': 'Ожидается JSON-объект'}), 400
    file_ids = data.get('file_ids', [])
    target_folder_id = data.get('target_folder_id') or None

    if not file_ids:
        return jsonify({'error': 'Не выбраны файлы'}), 400

    db = load_db()
    folders = load_folders()

    if target_folder_id and target_folder_id not in folders:
        return jsonify({'error': 'Целевая папка не найдена'}), 404

    moved_count = 0
    moved_ids = []
    new_folder_candidate = get_folder_physical_path(target_folder_id, folders)
    new_folder_rel = os.path.relpath(new_folder_candidate, UPLOAD_FOLDER)
    new_folder_physical = safe_upload_path(new_folder_rel, allow_root=True)
    if new_folder_physical is None:
        return jsonify({'error': 'Небезопасный путь назначения'}), 400
    os.makedirs(new_folder_physical, exist_ok=True)

    for file_id in file_ids:
        if file_id not in db:
            continue

        record = db[file_id]
        if record.get('folder_id') == target_folder_id:
            continue

        old_physical_path = record.get('physical_path', record['current_filename'])
        old_filename = record['current_filename']
        old_full_path = safe_upload_path(old_physical_path)
        if old_full_path is None:
            app.logger.error('Перемещение отклонено: небезопасный путь %r', old_physical_path)
            continue

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
        new_physical_path = safe_upload_path(new_rel_path)
        if new_physical_path is None:
            continue

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
                moved_ids.append(file_id)
            except Exception as e:
                continue

    save_db(db)
    return jsonify({'success': True, 'count': moved_count, 'moved_ids': moved_ids})

@app.route('/api/delete_files_batch', methods=['POST'])
@login_required
@serialized_mutation
def delete_files_batch():
    data = get_json_object()
    if data is None:
        return jsonify({'error': 'Ожидается JSON-объект'}), 400
    file_ids = data.get('file_ids', [])

    if not file_ids:
        return jsonify({'error': 'Не выбраны файлы'}), 400

    db = load_db()
    trash = load_trash()
    deleted_ids = []
    trash_ids = []

    for file_id in file_ids:
        if file_id not in db:
            continue

        record = db[file_id]
        physical_path = record.get('physical_path', record['current_filename'])
        if safe_upload_path(physical_path) is None:
            app.logger.error('Удаление отклонено: небезопасный путь %r', physical_path)
            continue

        entry_id = move_file_to_trash(file_id, record, db, trash)
        deleted_ids.append(file_id)
        if entry_id:
            trash_ids.append(entry_id)

    save_db(db)
    if trash_ids:
        save_trash(trash)
    return jsonify({'success': True, 'count': len(deleted_ids),
                    'deleted_ids': deleted_ids, 'trash_ids': trash_ids})

@app.route('/api/create_folder', methods=['POST'])
@login_required
@serialized_mutation
def create_folder():
    data = get_json_object()
    if data is None:
        return jsonify({'error': 'Ожидается JSON-объект'}), 400
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
    
    folder_candidate = get_folder_physical_path(folder_id, folders)
    folder_rel = os.path.relpath(folder_candidate, UPLOAD_FOLDER)
    physical_path = safe_upload_path(folder_rel)
    if physical_path is None:
        return jsonify({'error': 'Небезопасный путь папки'}), 400
    os.makedirs(physical_path, exist_ok=True)

    save_folders(folders)
    view = data.get('view') if data.get('view') in BROWSE_VIEWS else 'table'
    stats_map = get_folder_stats_map(folders, load_db())
    row_html = str(render_folder_row(folder_view_record(folder_id, folders, stats_map), view))
    return jsonify({'success': True, 'folder_id': folder_id, 'row_html': row_html, 'view': view})

@app.route('/api/delete_folder', methods=['POST'])
@login_required
@serialized_mutation
def delete_folder():
    data = get_json_object()
    if data is None:
        return jsonify({'error': 'Ожидается JSON-объект'}), 400
    folder_id = data.get('folder_id')

    folders = load_folders()
    db = load_db()
    
    if folder_id not in folders:
        return jsonify({'error': 'Папка не найдена'}), 404

    subtree = folder_subtree_ids(folder_id, folders)
    if folder_subtree_unsafe(subtree, folders, db):
        return jsonify({'error': 'В БД найден небезопасный путь; удаление отменено'}), 400

    delete_folder_tree(folder_id, folders, db)
    save_db(db)
    save_folders(folders)

    return jsonify({'success': True, 'deleted_folder_ids': sorted(subtree)})

@app.route('/api/delete_folders_batch', methods=['POST'])
@login_required
@serialized_mutation
def delete_folders_batch():
    """Удаление нескольких выбранных папок (вместе с поддеревьями)."""
    data = get_json_object()
    if data is None:
        return jsonify({'error': 'Ожидается JSON-объект'}), 400
    raw_ids = data.get('folder_ids', [])
    if not raw_ids:
        return jsonify({'error': 'Не выбраны папки'}), 400

    folders = load_folders()
    db = load_db()

    roots, subtrees = selected_folder_roots(raw_ids, folders)
    if not roots:
        return jsonify({'success': True, 'count': 0, 'deleted_folder_ids': []})

    union = set()
    for fid in roots:
        union |= subtrees[fid]
    if folder_subtree_unsafe(union, folders, db):
        return jsonify({'error': 'В БД найден небезопасный путь; удаление отменено'}), 400

    for fid in roots:
        delete_folder_tree(fid, folders, db)

    save_db(db)
    save_folders(folders)
    return jsonify({'success': True, 'count': len(roots), 'deleted_folder_ids': sorted(roots)})

@app.route('/api/move_folders_batch', methods=['POST'])
@login_required
@serialized_mutation
def move_folders_batch():
    """Перемещение выбранных папок в другую папку (или в корень)."""
    data = get_json_object()
    if data is None:
        return jsonify({'error': 'Ожидается JSON-объект'}), 400
    raw_ids = data.get('folder_ids', [])
    target_folder_id = data.get('target_folder_id') or None
    if not raw_ids:
        return jsonify({'error': 'Не выбраны папки'}), 400

    folders = load_folders()
    db = load_db()

    if target_folder_id and target_folder_id not in folders:
        return jsonify({'error': 'Целевая папка не найдена'}), 404

    roots, subtrees = selected_folder_roots(raw_ids, folders)
    if not roots:
        return jsonify({'success': True, 'count': 0, 'moved_ids': []})

    # Цикл: цель не может быть внутри перемещаемого поддерева (иначе папка исчезнет).
    if target_folder_id:
        for fid in roots:
            if target_folder_id == fid or target_folder_id in subtrees[fid]:
                return jsonify({'error': 'Нельзя переместить папку внутрь себя'}), 400

    to_move = [fid for fid in roots if folders[fid].get('parent_id') != target_folder_id]
    if not to_move:
        return jsonify({'success': True, 'count': 0, 'moved_ids': []})

    # Коллизия имён на целевом уровне: и с существующими, и между перемещаемыми.
    moving = set(to_move)
    taken = {f['name'] for fid2, f in folders.items()
             if fid2 not in moving and f.get('parent_id') == target_folder_id}
    for fid in to_move:
        name = folders[fid]['name']
        if name in taken:
            return jsonify({'error': f'Папка «{name}» уже есть в целевой папке'}), 400
        taken.add(name)

    target_physical = get_folder_physical_path(target_folder_id, folders)
    target_rel = os.path.relpath(target_physical, UPLOAD_FOLDER)
    if safe_upload_path(target_rel, allow_root=True) is None:
        return jsonify({'error': 'Небезопасный путь назначения'}), 400
    os.makedirs(target_physical, exist_ok=True)

    moved_ids = []
    for fid in to_move:
        old_dir = get_folder_physical_path(fid, folders)
        old_rel_dir = os.path.relpath(old_dir, UPLOAD_FOLDER)
        new_dir = os.path.join(target_physical, folders[fid]['name'])
        new_rel_dir = os.path.relpath(new_dir, UPLOAD_FOLDER)

        if safe_upload_path(old_rel_dir) is None or safe_upload_path(new_rel_dir) is None:
            continue
        if os.path.exists(new_dir):   # коллизия на диске (файл или папка)
            continue

        if os.path.exists(old_dir):
            try:
                os.rename(old_dir, new_dir)
            except OSError as exc:
                app.logger.error('Перемещение папки: не удалось переименовать %s: %s', old_rel_dir, exc)
                continue

        # Каталог переехал целиком — переписываем physical_path всех файлов поддерева.
        for rec in db.values():
            if rec.get('folder_id') not in subtrees[fid]:
                continue
            phys = rec.get('physical_path', rec.get('current_filename'))
            if phys == old_rel_dir:
                rec['physical_path'] = new_rel_dir
            elif phys.startswith(old_rel_dir + os.sep):
                rec['physical_path'] = new_rel_dir + phys[len(old_rel_dir):]

        folders[fid]['parent_id'] = target_folder_id
        moved_ids.append(fid)

    save_db(db)
    save_folders(folders)
    return jsonify({'success': True, 'count': len(moved_ids), 'moved_ids': moved_ids})

@app.route('/api/delete_file', methods=['POST'])
@login_required
@serialized_mutation
def delete_file():
    data = get_json_object()
    if data is None:
        return jsonify({'error': 'Ожидается JSON-объект'}), 400
    internal_id = data.get('internal_id')

    db = load_db()
    if internal_id not in db:
        return jsonify({'error': 'Файл не найден'}), 404

    record = db[internal_id]
    physical_path = record.get('physical_path', record['current_filename'])
    if safe_upload_path(physical_path) is None:
        return jsonify({'error': 'Небезопасный путь файла'}), 400

    trash = load_trash()
    entry_id = move_file_to_trash(internal_id, record, db, trash)
    save_db(db)
    if entry_id:
        save_trash(trash)
    return jsonify({'success': True, 'deleted_ids': [internal_id],
                    'trash_ids': [entry_id] if entry_id else []})

@app.route('/api/trash/restore', methods=['POST'])
@login_required
@serialized_mutation
def trash_restore():
    """Возвращает файлы из корзины: в исходную папку или в корень."""
    data = get_json_object()
    if data is None:
        return jsonify({'error': 'Ожидается JSON-объект'}), 400
    entry_ids = data.get('entry_ids', [])
    if not entry_ids:
        return jsonify({'error': 'Не выбраны записи корзины'}), 400
    view = data.get('view') if data.get('view') in BROWSE_VIEWS else 'table'
    client_folder = data.get('folder_id') or None

    trash = load_trash()
    db = load_db()
    folders = load_folders()
    restored_ids, rows_html = restore_trash_entries(
        entry_ids, folders, db, trash, view=view, client_folder=client_folder)

    save_db(db)
    save_trash(trash)
    return jsonify({'success': True, 'count': len(restored_ids), 'restored_ids': restored_ids,
                    'rows_html': rows_html, 'view': view})

@app.route('/api/trash/purge', methods=['POST'])
@login_required
@serialized_mutation
def trash_purge():
    """Окончательно удаляет записи корзины (по ids или всю)."""
    data = get_json_object() or {}
    entry_ids = data.get('entry_ids') or []
    trash = load_trash()
    if entry_ids:
        purged = sum(1 for entry_id in entry_ids if purge_trash_entry(entry_id, trash))
    else:
        purged = len(trash)
        for entry_id in list(trash):
            purge_trash_entry(entry_id, trash)
    save_trash(trash)
    return jsonify({'success': True, 'count': purged})

@app.route('/api/get_all_folders')
@login_required
def get_all_folders():
    folders = load_folders()
    result = []

    def add_folder(fid, depth, seen):
        # seen защищает от циклов в parent_id (бесконечная рекурсия).
        if fid in seen:
            return
        seen.add(fid)
        # parent_id нужен клиенту, чтобы исключить перемещаемые папки и их потомков.
        result.append({
            'id': fid,
            'name': folders[fid]['name'],
            'depth': depth,
            'parent_id': folders[fid].get('parent_id'),
        })
        for fid2, f in folders.items():
            if f.get('parent_id') == fid:
                add_folder(fid2, depth + 1, seen)

    for fid, f in folders.items():
        if f.get('parent_id') is None:
            add_folder(fid, 0, set())

    return jsonify(result)


# Сколько элементов разрешено выделить одним запросом: защита от гигантского
# ответа и от случайного «выбрать всё» на десятки тысяч объектов.
SELECT_BY_FILTER_LIMIT = 20000


@app.route('/api/select_by_filter', methods=['POST'])
@login_required
def select_by_filter():
    """Идентификаторы файлов и папок по текущему фильтру проводника.

    Нужен для кнопки «Выбрать все найденные»: без него выделялись только
    подгруженные строки, и пользователь мог собрать архив не из всего набора.
    """
    data = get_json_object()
    if data is None:
        return jsonify({'error': 'Ожидается JSON-объект'}), 400

    folder_id = data.get('folder_id') or None
    folders = load_folders()
    if folder_id and folder_id not in folders:
        return jsonify({'error': 'Папка не найдена'}), 404

    q = (data.get('q') or '').strip()[:200]
    ext = (data.get('ext') or '').strip().lower()
    if ext not in ALLOWED_EXTENSIONS:
        ext = ''

    matched_files = folder_files_matching(get_files_in_folder(folder_id), q, ext)
    matched_folders = [
        fid for fid, folder in folders.items()
        if folder.get('parent_id') == folder_id
        and (not q or q.casefold() in folder['name'].casefold())
    ]

    truncated = len(matched_files) + len(matched_folders) > SELECT_BY_FILTER_LIMIT
    return jsonify({
        'file_ids': [item['internal_id'] for item in matched_files][:SELECT_BY_FILTER_LIMIT],
        'folder_ids': matched_folders[:SELECT_BY_FILTER_LIMIT],
        'total': len(matched_files) + len(matched_folders),
        'truncated': truncated,
        'limit': SELECT_BY_FILTER_LIMIT,
    })


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
        download_url = f"{BASE_URL}/{url_path(physical_path)}"
        
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
@limiter.limit("30 per minute",
               deduct_when=lambda response: g.get('api_key') is None and rate_limit_applies())
def direct_file_access(filepath):
    if filepath.startswith('static/') or filepath.startswith('f/') or filepath.startswith('d/') or filepath.startswith('z/') or filepath in [
        'export_xlsx', 'export_all', 'upload', 'upload_folder', 'settings', 'favicon.ico'
    ] or filepath.startswith('api/'):
        abort(404)

    try:
        filepath = urllib.parse.unquote(filepath)
    except:
        pass

    full_path = safe_upload_path(filepath)
    record = None

    # Сначала находим запись, проверяем файл и бюджет; счётчик качания меняем
    # только после принятия запроса, чтобы ответы 404/429 не засоряли статистику.
    db = load_db()
    if full_path and os.path.isfile(full_path):
        record = next((r for r in db.values() if r.get('physical_path') == filepath), None)
    if record is None:
        record = next((r for r in db.values()
                       if (r.get('custom_alias') and r.get('custom_alias') == filepath)
                       or (r.get('slug') and r.get('slug') == filepath)), None)
        if record is not None:
            filepath = record.get('physical_path', record['current_filename'])
            full_path = safe_upload_path(filepath)
            if not full_path or not os.path.isfile(full_path):
                return "Файл не найден", 404

    if record is None:
        return "Файл не найден", 404

    if request.method == 'HEAD':
        return send_from_directory(UPLOAD_FOLDER, filepath, as_attachment=True,
                                   download_name=record['current_filename'].replace('+', '_'))

    retry_after = check_download_budget(request.remote_addr, record.get('size', 0) or 0)
    if retry_after:
        app.logger.warning('Бюджет скачивания исчерпан (%s), файл %s, retry через %d c',
                           request.remote_addr, filepath, retry_after)
        resp = jsonify({'error': f'Слишком много скачиваний, повторите через {max(1, retry_after // 60)} мин'})
        resp.headers['Retry-After'] = str(retry_after)
        return resp, 429

    updated = bump_counter(lambda r: r.get('physical_path') == filepath, 'download_count')
    if updated is None:
        return "Файл не найден", 404
    record = updated
    app.logger.info('Скачивание: %s (⬇=%d) от %s [%s]', filepath,
                    record.get('download_count', 0), request.remote_addr,
                    g.get('api_key') or 'анонимно')
    # '+' в имени файла браузеры трактовали по-разному — отдаём с '_'
    return send_from_directory(UPLOAD_FOLDER, filepath, as_attachment=True,
                               download_name=record['current_filename'].replace('+', '_'))

@app.route('/export_all', methods=['GET', 'HEAD'])
@limiter.limit("2 per minute", deduct_when=lambda response: rate_limit_applies())
@serialized_zip_build
def export_all():
    """Массовое скачивание всего хранилища одним ZIP.
    Не чаще раза в час на IP (анонимно) или на API-ключ."""
    if request.method == 'HEAD':
        return '', 405

    # A provided machine key must explicitly allow downloads/exports. Otherwise
    # an upload-only key could retrieve the entire storage archive.
    provided_key = request.headers.get('X-API-Key')
    api_key_meta = g.get('api_key_meta')
    export_key_id = None
    if provided_key:
        if not api_key_meta:
            return jsonify({'error': 'Требуется корректный заголовок X-API-Key'}), 401
        if api_key_meta.get('expired'):
            return jsonify({'error': 'Токен просрочен'}), 403
        if not token_store.has_scope(api_key_meta, 'download'):
            return jsonify({'error': 'Токену не разрешено: download'}), 403
        export_key_id = api_key_meta['id']

    quota_settings = load_quota_settings()
    if quota_settings.get('export_all_internal_only', True):
        client_addr = request.remote_addr or ''
        if multi_user_auth():
            # Решение принимается по роли, а не по адресу: администратор может
            # выгрузить архив и из интернета, обычный пользователь — нет.
            has_valid_admin_session = bool(session.get('logged_in') and is_admin())
        else:
            current_auth = get_auth() or {}
            has_valid_admin_session = bool(
                session.get('logged_in') and current_auth
                and session.get('_auth_fingerprint') == auth_fingerprint(current_auth)
            )
        # Локальным считается клиент, пришедший напрямую из приватной сети.
        # Запрос через тоннель тоже имеет приватного соседа (loopback), но
        # локальным не является — поэтому доверенный прокси исключается.
        peer_addr = _peer_address()
        trusted_lan_client = (_address_is_lan(peer_addr)
                              and not is_trusted_proxy_peer(peer_addr))
        if not (export_key_id or has_valid_admin_session or trusted_lan_client):
            app.logger.warning('Полный экспорт запрещён для внешнего адреса %s', client_addr)
            return jsonify({'error': 'Полный экспорт доступен только из локальной сети или с API-ключом'}), 403
    scope = f"key:{export_key_id}" if export_key_id else f"ip:{request.remote_addr}"
    with _EXPORT_ALL_LOCK:
        now = time.time()
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
            full = safe_upload_path(phys)
            if full and os.path.isfile(full):
                arcname = os.path.relpath(full, os.path.realpath(UPLOAD_FOLDER))
                items.append((full, arcname))
        if not items:
            return jsonify({'error': 'Нет файлов для архивирования'}), 404
        zip_error = zip_capacity_error(items, target_dir=UPLOAD_FOLDER)
        if zip_error:
            status, message = zip_error
            return jsonify({'error': message}), status

        # Cross-process hourly quota; memory fallback remains single-process.
        redis_export_key = None
        redis_client = _get_redis_client()
        if redis_client is not None:
            try:
                redis_export_key = f"albion:export_all:{scope}"
                if not redis_client.set(redis_export_key, '1', nx=True, ex=EXPORT_ALL_INTERVAL):
                    ttl = redis_client.ttl(redis_export_key)
                    retry = int(ttl) if isinstance(ttl, int) and ttl > 0 else EXPORT_ALL_INTERVAL
                    resp = jsonify({'error': f'Массовое скачивание доступно раз в час, повторите через {max(1, retry // 60)} мин'})
                    resp.headers['Retry-After'] = str(retry)
                    return resp, 429
            except Exception as e:
                app.logger.warning('Redis export quota недоступен (%s); fallback в память', e)
                redis_export_key = None

        total_bytes = sum(os.path.getsize(path) for path, _ in items)
        retry_after = check_download_budget(request.remote_addr, total_bytes)
        if retry_after:
            if redis_export_key and redis_client is not None:
                try:
                    redis_client.delete(redis_export_key)
                except Exception:
                    pass
            resp = jsonify({'error': f'Бюджет скачивания исчерпан; повторите через {max(1, retry_after // 60)} мин'})
            resp.headers['Retry-After'] = str(retry_after)
            return resp, 429

        for fn in os.listdir(UPLOAD_FOLDER):
            if fn.startswith('.export_all-') and fn.endswith('.zip'):
                try:
                    if now - os.path.getmtime(os.path.join(UPLOAD_FOLDER, fn)) > 7200:
                        os.remove(os.path.join(UPLOAD_FOLDER, fn))
                except OSError:
                    pass

        tmp_path = os.path.join(UPLOAD_FOLDER, f".export_all-{uuid.uuid4()}.zip")
        try:
            with zipfile.ZipFile(tmp_path, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
                for full_path, arcname in items:
                    zf.write(full_path, arcname=arcname.replace('+', '_'))
            _export_all_scopes[scope] = now
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            if redis_export_key and redis_client is not None:
                try:
                    redis_client.delete(redis_export_key)
                except Exception:
                    pass
            raise

    app.logger.info('Экспорт всего хранилища: %d файл(ов), %s, scope=%s',
                    len(items), format_size(os.path.getsize(tmp_path)), scope)
    return send_file(tmp_path, as_attachment=True,
                     download_name=f"albion-files-{datetime.now().strftime('%Y%m%d-%H%M')}.zip")

@app.route('/api/export_zip', methods=['POST'])
@login_required
@serialized_zip_build
def export_zip():
    """ZIP по выделению: выбранные файлы + выбранные папки (рекурсивно).
    Возвращает публичную ссылку на архив (id — uuid, неугадываемый), живёт сутки."""
    data = get_json_object()
    if data is None:
        return jsonify({'error': 'Ожидается JSON-объект'}), 400
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
    items = []
    for phys in items_map:
        safe_path = safe_upload_path(phys)
        if safe_path and os.path.isfile(safe_path):
            arcname = os.path.relpath(safe_path, os.path.realpath(UPLOAD_FOLDER))
            items.append((safe_path, arcname))
    if not items:
        return jsonify({'error': 'Нет файлов для архивирования'}), 404

    os.makedirs(ZIPLINKS_DIR, exist_ok=True)
    zip_error = zip_capacity_error(items, target_dir=ZIPLINKS_DIR)
    if zip_error:
        status, message = zip_error
        app.logger.warning('ZIP по выделению отклонён: %s (файлов=%d)', message, len(items))
        return jsonify({'error': message}), status
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
    try:
        with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
            for safe_path, arcname in items:
                zf.write(safe_path, arcname=arcname.replace('+', '_'))
    except Exception:
        try:
            os.remove(zip_path)
        except OSError:
            pass
        raise

    size = os.path.getsize(zip_path)
    app.logger.info('Создан архив по выборке: %d файл(ов), %s, /z/%s, от %s',
                    len(items), format_size(size), zip_id, request.remote_addr)
    return jsonify({'url': f"{BASE_URL}/z/{zip_id}", 'count': len(items), 'size': format_size(size)})

@app.route('/z/<zip_id>')
@limiter.limit("30 per minute", deduct_when=lambda response: rate_limit_applies())
def download_zip(zip_id):
    """Публичная ссылка на созданный архив (id неугадываемый, живёт сутки)."""
    if not re.match(r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$', zip_id):
        abort(404)
    path = os.path.join(ZIPLINKS_DIR, zip_id + '.zip')
    if not os.path.isfile(path) or time.time() - os.path.getmtime(path) > 86400:
        abort(404)
    size = os.path.getsize(path)
    if request.method == 'HEAD':
        return send_file(path, as_attachment=True,
                         download_name=f"albion-archive-{datetime.now().strftime('%Y%m%d-%H%M')}.zip")
    retry_after = check_download_budget(request.remote_addr, size)
    if retry_after:
        resp = jsonify({'error': f'Бюджет скачивания исчерпан; повторите через {max(1, retry_after // 60)} мин'})
        resp.headers['Retry-After'] = str(retry_after)
        return resp, 429
    app.logger.info('Скачивание архива /z/%s (%s) от %s [%s]', zip_id,
                    format_size(size), request.remote_addr, g.get('api_key') or 'анонимно')
    return send_file(path, as_attachment=True,
                     download_name=f"albion-archive-{datetime.now().strftime('%Y%m%d-%H%M')}.zip")

@app.route('/favicon.ico')
def favicon():
    return send_from_directory('static', 'logo.png', mimetype='image/png')

@app.route('/robots.txt')
def robots():
    return send_from_directory('static', 'robots.txt', mimetype='text/plain')

if TRUST_PROXY:
    # Trust exactly one proxy hop, and ignore forwarded headers from any other peer.
    app.wsgi_app = trusted_proxy_middleware(app.wsgi_app)

def waitress_options(behind_proxy):
    """Параметры waitress для конкретного слушателя.

    Waitress по умолчанию сам вырезает `X-Forwarded-*` у всех, кто не объявлен
    доверенным (`clear_untrusted_proxy_headers=True`), — тогда наш middleware их
    просто не видит, и адрес клиента остаётся loopback. Решение о доверии
    принимает приложение по списку `ALBION_TRUSTED_PROXY_IPS`, поэтому на
    слушателе для тоннеля заголовки сохраняются. На слушателе локальной сети
    они по-прежнему вырезаются.
    """
    options = {
        'threads': 8,
        # Transport ceiling; the admin-configurable per-request limit is lower.
        'max_request_body_size': 1024 * 1024 * 1024 * 1024,
        'channel_timeout': 300,
    }
    if behind_proxy and TRUST_PROXY:
        options['clear_untrusted_proxy_headers'] = False
    return options


def start_lan_listener():
    """Поднять второй слушатель для локальной сети (если задан ALBION_LAN_HOST).

    Тоннель подключается к loopback, офисные пользователи — напрямую к адресу
    локальной сети. По этому признаку различаются «внешний» и «локальный» клиент.
    """
    if not LAN_HOST:
        return None

    def run():
        try:
            serve(app, host=LAN_HOST, port=LAN_PORT, **waitress_options(behind_proxy=False))
        except Exception:
            app.logger.exception('Локальный слушатель %s:%s не запустился', LAN_HOST, LAN_PORT)

    thread = threading.Thread(target=run, name='waitress-lan', daemon=True)
    thread.start()
    return thread


if __name__ == '__main__':
    port = int(os.environ.get('ALBION_PORT', '5000'))
    print("=" * 60)
    print("АЛЬБИОН: Файловый сервер с каталогами")
    print(f"Тоннель/локально: http://{APP_HOST}:{port}")
    if LAN_HOST:
        print(f"Локальная сеть:   http://{LAN_HOST}:{LAN_PORT}")
    print(f"Учётные записи:   {'SQLite (роли и сессии)' if multi_user_auth() else 'JSON (один админ)'}")
    print("=" * 60)
    start_lan_listener()
    serve(app, host=APP_HOST, port=port, **waitress_options(behind_proxy=True))
