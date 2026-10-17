# Deploy checklist

Полные инструкции: [`docs/DEPLOYMENT.md`](../docs/DEPLOYMENT.md).

## Native systemd + nginx

1. Скопировать проект в `/opt/albion-files`, создать venv и установить `requirements.txt`.
2. Создать пользователя `albion` и `/srv/albion/data`; владелец — `albion:albion`.
3. Создать `/etc/albion-files.env` (права 0600) и указать:
   - `ALBION_BASE_URL=https://files.example.ru`
   - `ALBION_DATA_DIR=/srv/albion/data`
   - `ALBION_COOKIE_SECURE=1`
   - `ALBION_HOST=127.0.0.1`
   - `ALBION_TRUST_PROXY=1` и `ALBION_TRUSTED_PROXY_IPS=127.0.0.1,::1` для nginx на том же хосте (nginx перезаписывает XFF через `$remote_addr`).
   - при необходимости квоты `ALBION_MAX_UPLOAD_GB`, `ALBION_STORAGE_QUOTA_GB`, `ALBION_MIN_FREE_GB`.
4. Установить `deploy/albion-files.service.example` как `/etc/systemd/system/albion-files.service`, выполнить `systemctl daemon-reload && systemctl enable --now albion-files`.
5. Настроить nginx по `deploy/nginx.conf.example`, TLS-сертификат и `nginx -t && systemctl reload nginx`.
6. Firewall: снаружи только 80/443; 5000 закрыт. Для админки желательно VPN/IP allowlist.
7. При первом запуске без `auth.json` используется bootstrap-учётка `admin / альбион`. Сразу смените её в `/settings` (или через `change_password.py`).
8. Открыть `/settings` под администратором и задать квоты.

## Docker Compose

```bash
cp .env.example .env
# заполнить ALBION_BASE_URL и при необходимости квоты
cp deploy/nginx.docker.conf.example deploy/nginx.conf
mkdir -p data certs
sudo chown -R 10001:10001 data
docker compose config
docker compose up -d
docker compose exec app python change_password.py
```

Наружу публикуются только порты nginx; контейнер приложения не публикует 5000. Проверить `docker compose logs -f app` и `/login`.

## DNS для интернета и LAN

Создайте одну публичную A/AAAA-запись домена. Для LAN настройте split DNS: внутри офиса тот же домен должен указывать на внутренний адрес nginx. Если split DNS невозможен, проверьте NAT loopback. Не делайте отдельный HTTP-доступ к админке по IP.

## После запуска

- Проверить `https://домен/login`, `/f/`, загрузку тестового файла, короткую ссылку, `/settings`.
- Убедиться, что `/export_all` из внешней сети без API-ключа/админ-сессии возвращает 403 (LAN-доступ требует доверенного proxy peer).
- Настроить backup всего `ALBION_DATA_DIR` и мониторинг свободного диска.
- Не коммитить `auth.json`, `tokens.json`, `settings.json`, `.secret_key`, `uploads/`, `ziplinks/` и сертификаты.
