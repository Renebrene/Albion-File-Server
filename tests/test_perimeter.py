# -*- coding: utf-8 -*-
"""Этап 9: настоящие адреса клиентов, классификация «локальный/внешний», лимиты.

Проверяется то, из-за чего этап и делался: адрес клиента из тоннеля, доверие
forwarded-заголовкам только от прокси, лимиты для внешних и их отсутствие для
локальной сети.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _isolation import ensure_temp_data_dir  # noqa: E402

ensure_temp_data_dir('albion-perimeter-run-')

import server  # noqa: E402


def fake_environ(peer, headers=None, path='/login', method='GET'):
    environ = {
        'REQUEST_METHOD': method,
        'PATH_INFO': path,
        'SERVER_NAME': 'albion-rostov.cloudpub.ru',
        'SERVER_PORT': '443',
        # Схема до применения forwarded-заголовков: у тоннеля снаружи https,
        # но сам он стучится в приложение по http.
        'wsgi.url_scheme': 'http',
        'REMOTE_ADDR': peer,
    }
    for name, value in (headers or {}).items():
        environ['HTTP_' + name.upper().replace('-', '_')] = value
    return environ


class AddressHelpersTests(unittest.TestCase):
    def test_lan_addresses(self):
        for address in ('127.0.0.1', '::1', '192.168.1.50', '10.0.0.7',
                        '172.16.5.4', '::ffff:192.168.1.7'):
            self.assertTrue(server._address_is_lan(address), address)

    def test_external_addresses(self):
        # 203.0.113.0/24 — документационный диапазон: Python считает его
        # «приватным», но локальной сетью он не является.
        for address in ('8.8.8.8', '188.114.10.139', '203.0.113.5', '192.0.2.10',
                        '100.64.0.1', '2a00:1450:4001::1', '', 'мусор'):
            self.assertFalse(server._address_is_lan(address), address)

    def test_loopback_peer_is_the_tunnel(self):
        with server.app.test_request_context(environ_base={'REMOTE_ADDR': '127.0.0.1'}):
            self.assertTrue(server.request_is_external(), 'тоннель приходит с loopback')
        with server.app.test_request_context(environ_base={'REMOTE_ADDR': '192.168.1.50'}):
            self.assertFalse(server.request_is_external(), 'офисный клиент — не внешний')

    def test_rate_limit_key_is_the_client_address(self):
        with server.app.test_request_context(environ_base={'REMOTE_ADDR': '188.114.10.139'}):
            self.assertEqual(server.rate_limit_key(), '188.114.10.139')

    def test_limits_apply_to_public_and_to_tunnel(self):
        with server.app.test_request_context(environ_base={'REMOTE_ADDR': '188.114.10.139'}):
            self.assertTrue(server.rate_limit_applies())
        # тоннель: сосед приватный, но это доверенный прокси — лимит нужен
        with server.app.test_request_context(environ_base={'REMOTE_ADDR': '127.0.0.1'}):
            self.assertTrue(server.rate_limit_applies())

    def test_limits_do_not_apply_to_lan(self):
        with server.app.test_request_context(environ_base={'REMOTE_ADDR': '192.168.1.50'}):
            self.assertFalse(server.rate_limit_applies(),
                             'запросы из локальной сети не считаются в лимитах')


class TrustedProxyMiddlewareTests(unittest.TestCase):
    """Заголовкам верим только от доверенного соседа (тоннеля)."""

    def _run(self, environ):
        captured = {}

        def inner(environ, start_response):
            captured.update(environ)
            return []

        wrapped = server.trusted_proxy_middleware(inner)
        wrapped(environ, lambda *args: None)
        return captured

    def test_forwarded_headers_from_trusted_peer_are_used(self):
        environ = fake_environ('127.0.0.1', {
            'X-Forwarded-For': '188.114.10.139',
            'X-Forwarded-Proto': 'https',
            'X-Forwarded-Host': 'albion-rostov.cloudpub.ru',
        })
        result = self._run(environ)
        self.assertEqual(result.get('REMOTE_ADDR'), '188.114.10.139')
        self.assertEqual(result.get('wsgi.url_scheme'), 'https')
        self.assertEqual(result.get('HTTP_HOST'), 'albion-rostov.cloudpub.ru')

    def test_forwarded_headers_from_untrusted_peer_are_dropped(self):
        environ = fake_environ('203.0.113.10', {
            'X-Forwarded-For': '127.0.0.1',
            'X-Forwarded-Proto': 'https',
        })
        result = self._run(environ)
        self.assertEqual(result.get('REMOTE_ADDR'), '203.0.113.10',
                         'подставленный адрес не должен подменять клиента')
        self.assertEqual(result.get('wsgi.url_scheme'), 'http',
                         'подставленная схема от недоверенного соседа не применяется')
        self.assertNotIn('HTTP_X_FORWARDED_PROTO', result)

    def test_forwarded_for_from_lan_peer_is_dropped(self):
        environ = fake_environ('192.168.1.50', {'X-Forwarded-For': '10.0.0.1'})
        result = self._run(environ)
        self.assertEqual(result.get('REMOTE_ADDR'), '192.168.1.50')


class RateLimitBehaviourTests(unittest.TestCase):
    """Лимит действительно срабатывает для внешнего клиента и не мешает локальному."""

    def setUp(self):
        server.app.config.update(TESTING=True)
        self.was_enabled = server.limiter.enabled
        server.limiter.reset()
        server.limiter.enabled = True

    def tearDown(self):
        server.limiter.enabled = self.was_enabled
        server.limiter.reset()

    def _get_login(self, peer, times=12):
        client = server.app.test_client()
        client.environ_base['REMOTE_ADDR'] = peer
        statuses = [client.get('/login').status_code for _ in range(times)]
        return statuses

    def test_external_client_gets_429_after_login_limit(self):
        statuses = self._get_login('188.114.10.139')
        self.assertIn(429, statuses, f'внешний перебор должен упираться в лимит: {statuses}')
        # 503 возможен в режиме SQLite, когда учётных записей ещё нет, — важно,
        # что до лимита страница входа отвечает, а после него приходит 429.
        self.assertIn(statuses[0], (200, 503))

    def test_lan_client_is_not_limited(self):
        statuses = self._get_login('192.168.1.50')
        self.assertNotIn(429, statuses, f'локальная сеть не ограничивается: {statuses}')


class ExportAllClassificationTests(unittest.TestCase):
    """Полный экспорт: локальная сеть допускается, тоннель — только с правами."""

    def setUp(self):
        server.app.config.update(TESTING=True)
        server.limiter.enabled = False
        self.saved_internal_only = server.EXPORT_ALL_INTERNAL_ONLY
        server.EXPORT_ALL_INTERNAL_ONLY = True
        server._export_all_scopes.clear()

    def tearDown(self):
        server.EXPORT_ALL_INTERNAL_ONLY = self.saved_internal_only

    def test_direct_lan_client_is_allowed_to_export(self):
        client = server.app.test_client()
        client.environ_base['REMOTE_ADDR'] = '192.168.1.50'
        response = client.get('/export_all')
        self.assertNotEqual(response.status_code, 403,
                            'клиент из локальной сети не должен получать отказ по адресу')

    def test_tunnel_client_without_rights_is_refused(self):
        client = server.app.test_client()
        client.environ_base['REMOTE_ADDR'] = '127.0.0.1'
        response = client.get('/export_all')
        self.assertEqual(response.status_code, 403)


if __name__ == '__main__':
    unittest.main()
