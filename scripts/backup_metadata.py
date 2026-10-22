#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Резервная копия метаданных АЛЬБИОН.ДИСК.

Копирует метаданные (SQLite-базу и/или JSON-файлы) в каталог локальных копий,
раз в неделю дублирует свежую копию во внешний каталог (офсайт) и чистит старые
копии по сроку хранения.

Примеры:
    python scripts/backup_metadata.py                 # ежедневный запуск (таймер)
    python scripts/backup_metadata.py --offsite       # принудительно сделать и офсайт-копию
    python scripts/backup_metadata.py --list          # показать имеющиеся копии

Переменные окружения:
    ALBION_DATA_DIR         каталог данных (по умолчанию текущий)
    ALBION_BACKUP_DIR       каталог локальных копий (по умолчанию <данные>/backups)
    ALBION_BACKUP_OFFSITE   каталог офсайт-копий; пусто — офсайт не делается
    ALBION_BACKUP_KEEP      сколько локальных копий хранить (по умолчанию 30)
    ALBION_BACKUP_OFFSITE_KEEP  сколько офсайт-копий хранить (по умолчанию 8)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db  # noqa: E402

JSON_FILES = ('files_db.json', 'folders_db.json', 'tokens.json',
              'stats.json', 'settings.json', 'auth.json', '.secret_key')


def _stamp():
    return datetime.now().strftime('%Y%m%d-%H%M%S')


def _copy_json(data_dir, target):
    copied = []
    for name in JSON_FILES:
        source = os.path.join(data_dir, name)
        if not os.path.exists(source):
            continue
        destination = os.path.join(target, name)
        shutil.copy2(source, destination)
        os.chmod(destination, 0o600)
        copied.append(name)
    return copied


def _copy_database(data_dir, target):
    source = db.db_path(data_dir)
    if not os.path.exists(source):
        return None
    destination = os.path.join(target, db.DB_FILE_NAME)
    # Согласованная копия через sqlite3 backup API: сервис останавливать не нужно.
    source_conn = sqlite3.connect(source)
    target_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(target_conn)
    finally:
        target_conn.close()
        source_conn.close()
    os.chmod(destination, 0o600)
    return destination


def _counts(database_path):
    """Проверка копии: база должна открываться и содержать записи."""
    if not database_path or not os.path.exists(database_path):
        return {}
    conn = sqlite3.connect(database_path)
    try:
        result = {}
        for table in ('files', 'folders', 'tokens', 'documents'):
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if row:
                result[table] = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        result['integrity'] = conn.execute('PRAGMA integrity_check').fetchone()[0]
        return result
    finally:
        conn.close()


def _prune(directory, keep):
    entries = sorted(
        (name for name in os.listdir(directory)
         if os.path.isdir(os.path.join(directory, name)) and name.startswith('20')),
        reverse=True)
    removed = []
    for name in entries[keep:]:
        shutil.rmtree(os.path.join(directory, name), ignore_errors=True)
        removed.append(name)
    return removed


def make_backup(data_dir, backup_dir, offsite_dir=None, keep=30, offsite_keep=8, force_offsite=False):
    os.makedirs(backup_dir, exist_ok=True)
    target = os.path.join(backup_dir, _stamp())
    suffix = 1
    while os.path.exists(target):  # два запуска в одну секунду не должны затирать друг друга
        suffix += 1
        target = os.path.join(backup_dir, f'{_stamp()}-{suffix}')
    os.makedirs(target, mode=0o700)

    database = _copy_database(data_dir, target)
    json_copied = _copy_json(data_dir, target)
    counts = _counts(database)

    manifest = {
        'created': datetime.now().isoformat(timespec='seconds'),
        'source': os.path.abspath(data_dir),
        'database': os.path.basename(database) if database else None,
        'json_files': json_copied,
        'counts': counts,
        'host': os.uname().nodename,
    }
    with open(os.path.join(target, 'MANIFEST.json'), 'w', encoding='utf-8') as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)

    report = {'target': target, 'counts': counts, 'json_files': json_copied}

    offsite_wanted = bool(offsite_dir) and (force_offsite or datetime.now().weekday() == 6)
    if offsite_wanted:
        try:
            # Внешний носитель может быть не подключён: это не ошибка, а пропуск.
            os.makedirs(offsite_dir, exist_ok=True)
            destination = os.path.join(offsite_dir, os.path.basename(target))
            if os.path.exists(destination):
                shutil.rmtree(destination, ignore_errors=True)
            shutil.copytree(target, destination)
            report['offsite'] = destination
            report['offsite_removed'] = _prune(offsite_dir, offsite_keep)
        except OSError as exc:
            report['offsite'] = f'пропущено ({exc})'

    report['removed'] = _prune(backup_dir, keep)
    return report


def list_backups(backup_dir, offsite_dir=None):
    for directory in (backup_dir, offsite_dir):
        if not directory or not os.path.isdir(directory):
            continue
        print(f'\n{directory}:')
        entries = sorted(os.listdir(directory), reverse=True)
        if not entries:
            print('  (пусто)')
        for name in entries:
            path = os.path.join(directory, name)
            size = sum(os.path.getsize(os.path.join(root, f))
                       for root, _, files in os.walk(path) for f in files)
            print(f'  {name}  {size / 1024:.0f} КБ')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Резервная копия метаданных АЛЬБИОН.ДИСК')
    parser.add_argument('--data-dir', default=os.environ.get('ALBION_DATA_DIR', '.'))
    parser.add_argument('--backup-dir', default=os.environ.get('ALBION_BACKUP_DIR'))
    parser.add_argument('--offsite', default=os.environ.get('ALBION_BACKUP_OFFSITE'))
    parser.add_argument('--keep', type=int, default=int(os.environ.get('ALBION_BACKUP_KEEP', 30)))
    parser.add_argument('--offsite-keep', type=int,
                        default=int(os.environ.get('ALBION_BACKUP_OFFSITE_KEEP', 8)))
    parser.add_argument('--force-offsite', action='store_true',
                        help='сделать офсайт-копию независимо от дня недели')
    parser.add_argument('--list', action='store_true', help='показать имеющиеся копии')
    args = parser.parse_args(argv)

    backup_dir = args.backup_dir or os.path.join(args.data_dir, 'backups')
    if args.list:
        list_backups(backup_dir, args.offsite)
        return 0

    try:
        report = make_backup(args.data_dir, backup_dir, args.offsite,
                             keep=args.keep, offsite_keep=args.offsite_keep,
                             force_offsite=args.force_offsite)
    except (OSError, sqlite3.Error) as exc:
        print(f'ОШИБКА резервного копирования: {exc}', file=sys.stderr)
        return 1

    print('Копия:', report['target'])
    if report['counts']:
        print('Записей:', ', '.join(f'{k}={v}' for k, v in report['counts'].items()))
    print('JSON-файлов:', len(report['json_files']))
    if 'offsite' in report:
        print('Офсайт:', report['offsite'])
    if report['removed']:
        print('Удалено старых копий:', len(report['removed']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
