# -*- coding: utf-8 -*-
"""Тест самой защиты: тесты не должны иметь возможности писать в рабочую базу.

Повод: однажды модуль тестов запустился в каталоге проекта и создал учётные
записи в рабочей базе, а также удалил auth.json. Проверка должна падать громко.
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolation  # noqa: E402


class IsolationGuardTests(unittest.TestCase):
    def test_project_dir_is_rejected(self):
        with self.assertRaises(_isolation.IsolationError):
            _isolation.assert_not_project_dir(_isolation.PROJECT_ROOT)
        with self.assertRaises(_isolation.IsolationError):
            _isolation.assert_not_project_dir(os.path.join(_isolation.PROJECT_ROOT, '.'))

    def test_production_database_path_is_rejected(self):
        production_db = os.path.join(_isolation.PROJECT_ROOT, 'albion.db')
        with self.assertRaises(_isolation.IsolationError):
            _isolation.assert_isolated(tempfile.gettempdir(), production_db)

    def test_temp_dir_is_accepted(self):
        path = tempfile.mkdtemp(prefix='albion-guard-')
        try:
            _isolation.assert_isolated(path, os.path.join(path, 'albion.db'))
        finally:
            shutil.rmtree(path, ignore_errors=True)

    def test_environment_never_points_to_project(self):
        """В прогоне тестов ALBION_DATA_DIR не должен указывать на проект."""
        _isolation.assert_isolated(os.environ.get('ALBION_DATA_DIR'))

    def test_ensure_temp_data_dir_creates_and_reuses(self):
        saved = os.environ.pop('ALBION_DATA_DIR', None)
        try:
            first = _isolation.ensure_temp_data_dir('albion-guard-run-')
            self.assertTrue(os.path.isdir(first))
            self.assertNotEqual(os.path.abspath(first), _isolation.PROJECT_ROOT)
            self.assertEqual(_isolation.ensure_temp_data_dir(), first)
        finally:
            shutil.rmtree(first, ignore_errors=True) if 'first' in dir() else None
            if saved is None:
                os.environ.pop('ALBION_DATA_DIR', None)
            else:
                os.environ['ALBION_DATA_DIR'] = saved


if __name__ == '__main__':
    unittest.main()
