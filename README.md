# АЛЬБИОН — файловый сервер

Flask + Waitress веб-проводник для файлов/папок с публичными ссылками на скачивание, приватной админкой и поддержкой автоматизированных загрузчиков.

## Запуск (нативный Linux)

```bash
python3 -m venv venv
. venv/bin/activate
pip install -r requirements.txt
python change_password.py       # задайте admin username и пароль; пароль не отображается
python server.py
```

По умолчанию слушает `0.0.0.0:5000`. Для сервера за локальным nginx выставить `ALBION_HOST=127.0.0.1` в systemd environment. Все данные размещаются в `ALBION_DATA_DIR` (по умолчанию текущий каталог).

**Не публикуйте порт Waitress напрямую в интернет.** Используйте HTTPS reverse proxy, firewall и ограничения соединений. Шаблон: `deploy/nginx.conf.example`.

## Первоначальная настройка данных

Вход закрыт, если `auth.json` отсутствует или повреждён. Создайте его командой `python change_password.py`; она учитывает `ALBION_DATA_DIR` и записывает файл с правами 0600. Никаких встроенных `admin/admin` паролей нет.

Перед эксплуатацией настройте резервное копирование всего `ALBION_DATA_DIR` (файлы, БД, `auth.json`, `tokens.json`, ключ сессии, архивы-ссылки).

Для Docker: скопируйте `.env.example` в `.env`, укажите реальный HTTPS-домен; подготовьте bind volume правами `sudo chown -R 10001:10001 data`; запустите `docker compose up -d`, затем `docker compose exec app python change_password.py`.

## Тесты

```bash
python -m unittest discover -s tests -v
```

Suite использует временный каталог и не меняет рабочие/боевые данные.

## Функции и API

См. [docs/API.md](docs/API.md): страницы, методы API, auth, лимиты, примеры автоматического скачивания.
