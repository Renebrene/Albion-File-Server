# Production deployment

## Нативно под systemd + nginx

Схема предпочтительна для существующего офиса: nginx слушает 80/443 и завершает TLS; Waitress доступен только локально.

1. Подготовить Linux-пользователя без shell-доступа и каталог данных, например `/srv/albion/data`; владелец — service user.
2. Создать venv, установить `requirements.txt`, скопировать код.
3. Задать environment в systemd unit:

```ini
[Service]
User=albion
Group=albion
WorkingDirectory=/opt/albion-files
Environment=ALBION_DATA_DIR=/srv/albion/data
Environment=ALBION_HOST=127.0.0.1
Environment=ALBION_PORT=5000
Environment=ALBION_BASE_URL=https://files.example.ru
Environment=ALBION_COOKIE_SECURE=1
Environment=ALBION_SESSION_HOURS=12
Environment=ALBION_MAX_UPLOAD_GB=50
Environment=ALBION_STORAGE_QUOTA_GB=100
Environment=ALBION_MIN_FREE_GB=5
Environment=ALBION_MAX_ZIP_GB=50
Environment=ALBION_EXPORT_ALL_INTERNAL_ONLY=1
# Optional shared state for multiple processes/replicas:
# Environment=ALBION_REDIS_URL=redis://127.0.0.1:6379/0
# Environment=ALBION_RATE_LIMIT_STORAGE_URI=redis://127.0.0.1:6379/1
ExecStart=/opt/albion-files/venv/bin/python /opt/albion-files/server.py
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/srv/albion/data

[Install]
WantedBy=multi-user.target
```

Проверить unit через `systemd-analyze verify`, затем `systemctl daemon-reload`, `enable --now albion-files`. Убедиться, что порт 5000 слушает только `127.0.0.1`.

4. Настроить TLS сертификат и nginx на `files.example.ru`; пример — `deploy/nginx.conf.example`. Nginx должен проксировать к `http://127.0.0.1:5000` и задавать `Host`, `X-Forwarded-For`, `X-Forwarded-Proto`.
5. Firewall: снаружи разрешить только 443 (и 80 для TLS-проверки/redirect); порт 5000 закрыть. Для доступа только сотрудникам предпочтительнее VPN или IP allowlist на nginx.
6. Создать пароль: `ALBION_DATA_DIR=/srv/albion/data /opt/albion-files/venv/bin/python change_password.py`.
7. При необходимости создать ключ автоматики `manage_tokens.py add "Назначение ключа"`.

`ProxyFix` в коде доверяет одному proxy hop. Не выставляйте порт приложения в сеть, где клиент может сам посылать поддельный `X-Forwarded-For`.

## Docker Compose

Файлы: `Dockerfile`, `docker-compose.yml`, `.env.example`, `deploy/nginx.conf.example`.

```bash
cp .env.example .env
# В .env укажите реальный внешний HTTPS origin
mkdir -p data certs deploy
sudo chown -R 10001:10001 data
cp deploy/nginx.conf.example deploy/nginx.conf
# Положите fullchain.pem и privkey.pem в certs/
docker compose config
docker compose up -d
# Настроить логин при первом запуске
docker compose exec app python change_password.py
```

Порт приложения не публикуется на host, к нему обращается только контейнер nginx. Перед production нужно проверить права `data/`, корректность TLS, DNS и DNS split-horizon для LAN.

## Домен доступен из интернета и LAN

Используйте один публичный HTTPS hostname. Для LAN настройте split DNS: внутри офиса домен разрешается во внутренний адрес nginx, извне — в публичный IP. Альтернатива — NAT loopback на router. Не создавайте отдельный HTTP URL по IP для админки.

Если хранилище предназначено только для сотрудников, VPN/IP allowlist — сильнее, чем секретность slug. Скачивание и `/d/<slug>` по требованиям публичны: ссылка является bearer-capability, любой получивший её может открыть содержимое.

## Резервные копии и обновления

- Регулярно резервировать весь `ALBION_DATA_DIR` согласованным snapshot/rsync/borg: `uploads/`, JSON DB, `auth.json`, `tokens.json`, `.secret_key`, `ziplinks/`.
- Периодически проверять восстановление из backup, а не только факт создания архива.
- Обновление: остановить сервис/контейнер, сохранить backup данных, обновить код, выполнить тесты, поднять сервис, проверить `/login`, `/f/`, публичную тестовую ссылку и журналы.
- Не коммитить реальные `auth.json`, `tokens.json`, `.secret_key`, пользовательские файлы, архивы или экспорт чата.
- Проверять зависимости `requirements.txt` и уязвимости перед обновлением.

## Мониторинг/лимиты

- `albion.log` ротируется 5 MiB × 3; systemd вывод также доступен через `journalctl -u albion-files`.
- Следить за свободным местом в `ALBION_DATA_DIR`: upload ограничен настройкой `max_upload_gb`, а ZIP и временные файлы занимают диск. Включить disk alert и filesystem quota.
- После входа администратор может изменить квоты на `/settings`; они сохраняются в `ALBION_DATA_DIR/settings.json` и применяются без перезапуска. Environment-переменные задают defaults только до первого сохранения.
- Без Redis бюджеты и Flask-Limiter хранятся in-memory и сбрасываются при restart; для нескольких процессов/реплик задайте `ALBION_REDIS_URL`/`ALBION_RATE_LIMIT_STORAGE_URI`, но метаданные JSON-БД всё равно требуют SQLite/PostgreSQL для транзакционности.
- `/export_all` публичен по требованиям и позволяет один полный архив в час на IP/key; если это слишком открыто — ограничить через VPN, nginx allowlist или потребовать API key/auth.

## Проверка после деплоя

```bash
systemctl is-active albion-files
ss -tlnp | grep 5000             # ожидается только 127.0.0.1:5000 за nginx
curl -I https://files.example.ru/login
curl -I https://files.example.ru/favicon.ico
```

Проверить затем browser flow: login, загрузка тестового файла, короткий URL, публичная папка, CSRF-отказ на POST без токена, выбранный ZIP, повторный `/export_all` в час (429 + Retry-After). Не запускать полную выгрузку в тесте на production-данных без необходимости.
