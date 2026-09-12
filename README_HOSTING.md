# FUCKBR Viewer — хостинг

Это обычное Flask-приложение (`app.py` / `wsgi.py`). Больше не привязано
к `127.0.0.1` и Termux — можно поднять на любом Linux-хостинге/VPS.

## Быстрый локальный запуск (для проверки)

```bash
pip install -r requirements.txt
python3 app.py
# -> http://127.0.0.1:8765
```

## Вариант 1 — Docker (проще всего)

```bash
docker build -t fuckbr-viewer .
docker run -p 8000:8000 fuckbr-viewer
```

Открыть `http://<IP-сервера>:8000`.

## Вариант 2 — обычный VPS (systemd + nginx)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# ручной тест
gunicorn -w 2 -k gthread --threads 4 -b 127.0.0.1:8000 wsgi:app
```

Пример unit-файла `/etc/systemd/system/fuckbr.service`:

```ini
[Unit]
Description=FUCKBR Viewer
After=network.target

[Service]
WorkingDirectory=/opt/fuckbr_web
Environment=PORT=8000
ExecStart=/opt/fuckbr_web/venv/bin/gunicorn -w 2 -k gthread --threads 4 \
    -b 127.0.0.1:8000 wsgi:app --timeout 120
Restart=always
User=www-data

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now fuckbr
```

Nginx как reverse-proxy (плюс отдаём большие ZIP/PNG загрузки):

```nginx
server {
    listen 80;
    server_name your-domain.example;

    client_max_body_size 300M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 120s;
    }
}
```

## Вариант 3 — PaaS (Render / Railway / Fly.io и т.п.)

В репозитории уже есть `Procfile`. Просто подключить репозиторий — платформа
сама поставит зависимости из `requirements.txt` и запустит `web:` процесс.
Убедись, что план хостинга даёт **Linux x86_64/amd64** контейнер — рендер
и распаковка текстур используют бинарники из `engine/kram/linux/` и
`engine/pvr/linux/`.

## Важно про текстуры (BTX/KTX → PNG)

Приложение конвертирует `.btx`/`.ktx` в PNG на сервере через уже
вшитые в архив бинарники `kram` и `PVRTexToolCLI`
(`engine/kram/linux/`, `engine/pvr/linux/`) — на Linux-хостинге
**отдельно ставить `astcenc` не нужно**, всё работает "из коробки".
`astcenc` требуется только для:
- одиночных файлов `.astc` без обёртки (редкий случай);
- конвертации PNG → BTX в разделе "Конвертеры".

Если тебе это нужно — положи бинарник `astcenc` в `~/astcenc` на сервере
(`chmod +x`), либо в `PATH`.

## Ограничения / что стоит знать

- Сессии (загруженные архивы) хранятся в `sessions/` на диске и живут
  6 часов с момента последнего обращения, потом чистятся фоновым потоком.
  Для настоящей многопользовательской нагрузки стоит вынести это в Redis/S3,
  но для личного/группового использования файловой сессии достаточно.
- Рендер — программный (CPU, numpy), не WebGL. Поэтому "вращение" в
  браузере работает через повторные запросы кадра с сервера (debounced
  drag), а не через локальный 3D-движок. Для одной модели это быстро;
  если сервер слабый — увеличь троттлинг в `static/app.js` (`dragTimer`,
  сейчас 40мс) или уменьши `size` в превью при перетаскивании.
- `MAX_CONTENT_LENGTH` в `app.py` ограничен 300 МБ — подними при
  необходимости.
