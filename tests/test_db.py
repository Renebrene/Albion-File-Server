"""Тесты SQLite-слоя метаданных (db.py).

Запуск: python -m unittest tests.test_db -v
"""
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest

import db


def _write_json(path, payload):
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, ensure_ascii=False)


def _file_record(name, size=10, folder_id=None, slug=None):
    return {
        'access_id': name,
        'current_filename': f'{name}.jpg',
        'physical_path': f'albion-previews/Ростов/{name}.jpg',
        'original_filename': f'{name}.jpg',
        'display_name': f'{name}.jpg',
        'folder_id': folder_id,
        'custom_alias': '',
        'link_rotated': False,
        'download_count': 0,
        'size': size,
        'mtime': '22.10.2026 12:00',
        'ext': 'JPG',
        'is_image': True,
        'slug': slug or f'slug-{name}',
    }


class DbLayerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='albion-db-test-')
        os.environ['ALBION_DATA_DIR'] = self.dir
        db.close()
        self.conn = db.init_schema(db.db_path(self.dir))

    def tearDown(self):
        db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    # --- схема и backend ---

    def test_schema_init_is_idempotent(self):
        db.init_schema(db.db_path(self.dir))
        db.init_schema(db.db_path(self.dir))
        tables = {row[0] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ('files', 'folders', 'tokens', 'users', 'sessions',
                      'sections', 'links', 'audit', 'documents', 'quota_usage'):
            self.assertIn(table, tables)
        self.assertEqual(self.conn.execute('PRAGMA journal_mode').fetchone()[0], 'wal')

    def test_backend_flag_defaults_to_json(self):
        saved = os.environ.get('ALBION_METADATA_BACKEND')
        try:
            os.environ.pop('ALBION_METADATA_BACKEND', None)
            self.assertEqual(db.backend(), 'json')
            self.assertFalse(db.sqlite_enabled())
            os.environ['ALBION_METADATA_BACKEND'] = 'sqlite'
            self.assertTrue(db.sqlite_enabled())
            os.environ['ALBION_METADATA_BACKEND'] = 'что-то-иное'
            self.assertEqual(db.backend(), 'json')
        finally:
            if saved is None:
                os.environ.pop('ALBION_METADATA_BACKEND', None)
            else:
                os.environ['ALBION_METADATA_BACKEND'] = saved

    # --- запись и чтение ---

    def test_roundtrip_is_lossless(self):
        data = {f'id{i}': _file_record(f'f{i}') for i in range(5)}
        db.save_files(data)
        self.assertEqual(db.load_files(), data)

    def test_save_updates_only_changed_and_removes_absent(self):
        db.save_files({'a': _file_record('a'), 'b': _file_record('b')})
        written, removed = db.save_files({'a': _file_record('a')})
        self.assertEqual((written, removed), (0, 1))
        self.assertEqual(list(db.load_files()), ['a'])

    def test_mutation_rolls_back_on_error(self):
        db.save_files({'a': _file_record('a')})

        def boom(files):
            files['b'] = _file_record('b')
            raise RuntimeError('сбой посреди мутации')

        with self.assertRaises(RuntimeError):
            db.mutate_files(boom)
        self.assertEqual(list(db.load_files()), ['a'])

    def test_mutation_sees_committed_state(self):
        db.save_files({'a': _file_record('a')})

        def add_counter(files):
            files['a']['download_count'] = files['a'].get('download_count', 0) + 1
            return files['a']['download_count']

        self.assertEqual(db.mutate_files(add_counter), 1)
        self.assertEqual(db.mutate_files(add_counter), 2)
        self.assertEqual(db.load_files()['a']['download_count'], 2)

    def test_folders_roundtrip(self):
        folders = {'f1': {'name': 'Ростов', 'parent_id': None,
                          'created': '22.10.2026 12:00', 'slug': 'rostov'}}
        db.save_folders(folders)
        self.assertEqual(db.load_folders(), folders)

    def test_tokens_roundtrip_and_scopes(self):
        tokens = {'hash1': {'name': 'Сайт', 'created': '22.10.2026 12:00',
                            'created_at': '2026-10-22T12:00:00+03:00',
                            'scopes': ['upload'], 'folder_id': 'f1', 'expires_at': None}}
        db.save_tokens(tokens)
        self.assertEqual(db.load_tokens(), tokens)
        row = self.conn.execute('SELECT scopes, folder_id FROM tokens').fetchone()
        self.assertEqual(json.loads(row['scopes']), ['upload'])
        self.assertEqual(row['folder_id'], 'f1')

    def test_documents_roundtrip(self):
        db.save_document('stats', {'last_upload': 'сегодня'})
        db.save_document('settings', {'min_free_gb': 5.0})
        self.assertEqual(db.load_document('stats'), {'last_upload': 'сегодня'})
        self.assertEqual(db.load_document('settings')['min_free_gb'], 5.0)
        self.assertEqual(db.load_document('нет-такого'), {})

    # --- параллельная запись ---

    def test_parallel_mutations_do_not_lose_records(self):
        errors = []

        def worker(index):
            try:
                def add(files):
                    files[f'p{index}'] = _file_record(f'p{index}')
                db.mutate_files(add)
            except Exception as exc:  # noqa: BLE001 — фиксируем любую ошибку потока
                errors.append(exc)
            finally:
                db.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(db.load_files()), 50)

    def test_parallel_readers_and_writer(self):
        db.save_files({f'id{i}': _file_record(f'f{i}') for i in range(20)})
        errors = []
        stop = threading.Event()

        def reader():
            try:
                while not stop.is_set():
                    files = db.load_files()
                    # число записей растёт по ходу работы писателя, важно, что
                    # чтение всегда согласовано и ничего не теряется
                    self.assertGreaterEqual(len(files), 20)
                    self.assertTrue(all(isinstance(rec, dict) and 'slug' in rec
                                        for rec in files.values()))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                db.close()

        def writer():
            try:
                for i in range(20):
                    db.mutate_files(lambda files: files.update({f'new{i}': _file_record(f'n{i}')}))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                stop.set()
                db.close()

        threads = [threading.Thread(target=reader) for _ in range(3)] + [threading.Thread(target=writer)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(db.load_files()), 40)


class MigrationTests(unittest.TestCase):
    """Миграция JSON → SQLite на синтетическом наборе, близком к рабочему."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='albion-migrate-')
        os.environ['ALBION_DATA_DIR'] = self.dir
        db.close()
        self.files = {f'id{i}': _file_record(f'f{i}', size=100 + i,
                                            folder_id='folder1',
                                            slug=f'slug-{i}') for i in range(120)}
        self.folders = {'folder1': {'name': 'Ростов', 'parent_id': None,
                                    'created': '22.10.2026 12:00', 'slug': 'rostov'}}
        _write_json(os.path.join(self.dir, 'files_db.json'), self.files)
        _write_json(os.path.join(self.dir, 'folders_db.json'), self.folders)
        _write_json(os.path.join(self.dir, 'stats.json'), {'last_upload': None})
        _write_json(os.path.join(self.dir, 'settings.json'), {'min_free_gb': 5.0})

    def tearDown(self):
        db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_migration_matches_source(self):
        report = db.migrate_from_json(self.dir)
        self.assertTrue(report['ok'], report['mismatches'])
        self.assertEqual(report['target']['files'], 120)
        self.assertEqual(report['target']['size'],
                         sum(rec['size'] for rec in self.files.values()))
        self.assertEqual(db.load_files(), self.files)
        self.assertEqual(db.load_folders(), self.folders)
        self.assertEqual(db.load_document('settings'), {'min_free_gb': 5.0})

    def test_migration_refuses_to_overwrite_without_force(self):
        db.migrate_from_json(self.dir)
        with self.assertRaises(db.DbError):
            db.migrate_from_json(self.dir)
        self.assertTrue(db.migrate_from_json(self.dir, force=True)['ok'])

    def test_verify_reports_missing_records(self):
        db.migrate_from_json(self.dir)
        db.save_files({})
        report = db.verify_against_json(self.dir)
        self.assertTrue(report['mismatches'])
        self.assertIn('нет в базе', report['mismatches'][0])

    def test_backup_creates_openable_copy(self):
        db.migrate_from_json(self.dir)
        dest = os.path.join(self.dir, 'backup')
        target = db.backup(dest)
        self.assertTrue(os.path.exists(target))
        copy = sqlite3.connect(target)
        try:
            self.assertEqual(copy.execute('SELECT COUNT(*) FROM files').fetchone()[0], 120)
        finally:
            copy.close()


if __name__ == '__main__':
    unittest.main()
