# Domain Scanner — Deploy Guide

## Структура проекта

```
domain_scanner/
├── app/
│   ├── app.py              # Flask — HTTP роуты
│   ├── tasks.py            # Celery — логика сканирования
│   ├── requirements.txt
│   └── Dockerfile
├── nginx/
│   ├── nginx.conf          # конфиг reverse proxy
│   └── htpasswd            # логины/пароли (генерируется)
├── redis/
│   └── redis.conf
├── docker-compose.yml
└── README.md
```

## Локальный запуск (без SSL, без Basic Auth)

```bash
# 1. Создать .venv для разработки (опционально, для IDE)
python -m venv app/.venv
source app/.venv/bin/activate
pip install -r app/requirements.txt

# 2. Запустить через Docker Compose
docker compose up --build

# Открыть http://localhost
```

## Деплой на VPS

### 1. Подготовка сервера
```bash
apt update && apt install -y docker.io docker-compose-plugin
```

### 2. Клонировать проект
```bash
git clone <repo> /opt/domain_scanner
cd /opt/domain_scanner
```

### 3. Заменить домен в nginx.conf
```bash
sed -i 's/YOUR_DOMAIN.com/yourdomain.com/g' nginx/nginx.conf
```

### 4. Создать файл паролей Basic Auth
```bash
# Установить htpasswd если нет
apt install -y apache2-utils

# Создать пользователя (запросит пароль)
htpasswd -c nginx/htpasswd username
```

### 5. Получить SSL сертификат
```bash
# Сначала поднять nginx без SSL (закомментировать 443 блок временно)
docker compose up -d nginx

# Получить сертификат
docker compose run --rm certbot certonly \
  --webroot --webroot-path=/var/www/certbot \
  -d yourdomain.com \
  --email your@email.com \
  --agree-tos

# Раскомментировать 443 блок, перезапустить
docker compose restart nginx
```

### 6. Запустить всё
```bash
docker compose up -d
```

### 7. Проверить логи
```bash
docker compose logs -f app
docker compose logs -f celery_worker
```

## Обновление приложения
```bash
git pull
docker compose up -d --build app celery_worker
```

## Мониторинг
```bash
# Статус всех сервисов
docker compose ps

# Логи в реальном времени
docker compose logs -f

# Redis — сколько памяти используется
docker compose exec redis redis-cli info memory
```