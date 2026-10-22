# -*- coding: utf-8 -*-
"""Защита тестов от работы в каталоге проекта.

Причина появления: тестовый модуль, запущенный отдельно без ALBION_DATA_DIR,
брал значение по умолчанию ('.') и писал в рабочую базу — так в production
попали тестовые учётные записи, а auth.json был удалён.

Правило простое: ни один тест не имеет права открывать базу данных, лежащую
в каталоге проекта. Проверка вызывается и при импорте модуля, и перед каждым
изменяющим тестом.
"""
import os
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Модули проекта (db.py, server.py) должны быть importable независимо от того,
# из какого каталога запущены тесты.
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


class IsolationError(RuntimeError):
    """Тест пытается работать с рабочим каталогом проекта."""


def assert_not_project_dir(path):
    if path and os.path.abspath(path) == PROJECT_ROOT:
        raise IsolationError(
            f'ALBION_DATA_DIR указывает на каталог проекта ({path}): '
            'тесты обязаны работать во временном каталоге')


def assert_isolated(data_dir=None, db_path=None):
    """Проверить, что тест не трогает рабочую базу."""
    assert_not_project_dir(data_dir or os.environ.get('ALBION_DATA_DIR'))
    if db_path:
        production_db = os.path.join(PROJECT_ROOT, 'albion.db')
        if os.path.abspath(db_path) == production_db:
            raise IsolationError(f'Тест работает с рабочей базой {production_db}')


def ensure_temp_data_dir(prefix='albion-test-'):
    """Вернуть каталог данных для теста, создав временный при необходимости.

    Если переменная уже задана (общий прогон unittest discover), она сохраняется,
    но проверяется на совпадение с каталогом проекта.
    """
    current = os.environ.get('ALBION_DATA_DIR')
    if current:
        assert_not_project_dir(current)
        return current
    path = tempfile.mkdtemp(prefix=prefix)
    os.environ['ALBION_DATA_DIR'] = path
    return path
