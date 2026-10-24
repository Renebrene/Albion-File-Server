# -*- coding: utf-8 -*-
"""Проверка того, что waitress не вырезает forwarded-заголовки на слушателе тоннеля.

Именно на этом сломался этап 9: `clear_untrusted_proxy_headers` по умолчанию
удаляет `X-Forwarded-*` до того, как их увидит приложение, поэтому настоящий
адрес клиента терялся. Тест поднимает настоящий waitress на свободном порту.
"""
import os
import socket
import sys
import threading
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _isolation import ensure_temp_data_dir  # noqa: E402

ensure_temp_data_dir('albion-waitress-run-')

from waitress import create_server  # noqa: E402

import server  # noqa: E402


def shutdown(srv):
    """Остановить сервер и его диспетчер задач.

    Без остановки диспетчера поток waitress продолжает ждать задачу и на выходе
    интерпретатора пишет OSError в stderr, засоряя вывод тестов.
    """
    try:
        srv.close()
    except Exception:  # noqa: BLE001 — сервер мог уже остановиться
        pass
    dispatcher = getattr(srv, 'task_dispatcher', None)
    if dispatcher is not None:
        try:
            dispatcher.shutdown(timeout=1)
        except Exception:  # noqa: BLE001
            pass


def echo_app(environ, start_response):
    body = '{}|{}'.format(
        environ.get('REMOTE_ADDR', ''),
        environ.get('HTTP_X_FORWARDED_FOR', ''),
    ).encode()
    start_response('200 OK', [('Content-Length', str(len(body)))])
    return [body]


def free_port():
    sock = socket.socket()
    try:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


class WaitressProxyTests(unittest.TestCase):
    def start(self, **options):
        port = free_port()
        srv = create_server(echo_app, host='127.0.0.1', port=port, **options)
        thread = threading.Thread(target=srv.run, daemon=True)
        thread.start()
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                socket.create_connection(('127.0.0.1', port), 0.2).close()
                break
            except OSError:
                time.sleep(0.05)
        self.addCleanup(shutdown, srv)
        return port

    def request(self, port):
        req = urllib.request.Request(f'http://127.0.0.1:{port}/',
                                     headers={'X-Forwarded-For': '8.8.4.4'})
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.read().decode()

    def test_default_waitress_clears_forwarded_headers(self):
        port = self.start()
        remote, forwarded = self.request(port).split('|')
        self.assertEqual(remote, '127.0.0.1')
        self.assertEqual(forwarded, '', 'по умолчанию waitress вырезает X-Forwarded-For')

    def test_option_keeps_forwarded_headers_for_the_app(self):
        port = self.start(clear_untrusted_proxy_headers=False)
        remote, forwarded = self.request(port).split('|')
        self.assertEqual(remote, '127.0.0.1', 'адрес соседа waitress не меняет')
        self.assertEqual(forwarded, '8.8.4.4', 'заголовок должен дойти до приложения')

    def test_options_are_enabled_only_for_proxied_listener(self):
        saved = server.TRUST_PROXY
        try:
            server.TRUST_PROXY = True
            self.assertFalse(server.waitress_options(behind_proxy=True)
                             .get('clear_untrusted_proxy_headers', True))
            self.assertNotIn('clear_untrusted_proxy_headers',
                             server.waitress_options(behind_proxy=False),
                             'на слушателе локальной сети заголовки вырезаются')
            server.TRUST_PROXY = False
            self.assertNotIn('clear_untrusted_proxy_headers',
                             server.waitress_options(behind_proxy=True))
        finally:
            server.TRUST_PROXY = saved

    def test_proxy_pipeline_end_to_end(self):
        """waitress сохраняет заголовок, наш middleware применяет настоящий адрес."""
        from werkzeug.serving import WSGIRequestHandler  # noqa: F401  (наличие окружения)

        port = free_port()
        srv = create_server(server.app, host='127.0.0.1', port=port,
                            clear_untrusted_proxy_headers=False)
        threading.Thread(target=srv.run, daemon=True).start()
        self.addCleanup(shutdown, srv)
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                socket.create_connection(('127.0.0.1', port), 0.2).close()
                break
            except OSError:
                time.sleep(0.05)

        # /export_all пишет в журнал адрес клиента, который видит приложение.
        # Нам достаточно косвенного признака: без доверия адрес был бы 127.0.0.1.
        with self.assertLogs('server', level='WARNING') as captured:
            req = urllib.request.Request(f'http://127.0.0.1:{port}/export_all',
                                         headers={'X-Forwarded-For': '10.0.0.7'})
            try:
                urllib.request.urlopen(req, timeout=5)
            except urllib.error.HTTPError:
                pass
        joined = '\n'.join(captured.output)
        if not server.TRUST_PROXY:
            self.assertIn('127.0.0.1', joined)
            return
        self.assertIn('10.0.0.7', joined,
                      f'приложение должно видеть настоящий адрес, лог: {joined}')


if __name__ == '__main__':
    unittest.main()
