# Domain Scanner

Инструмент для анализа дропающихся доменов на аукционах Namecheap.

**Стек:** Flask · Celery · Redis · Nginx · Docker Compose

## Как работает

1. Загружаешь `.xlsx` или `.csv` экспорт из Namecheap Marketplace
2. Фильтрация: только `.com`, зарегистрированные в 2000–2016, аукцион заканчивается в заданном окне, цена в лимите
3. Каждый домен проверяется через **ScamDoc** (доверие в %)
4. Прошедшие порог — через **VirusTotal** (malicious/suspicious флаги)
5. Результаты появляются в реальном времени, выгрузка в `.xlsx`

Несколько пользователей могут запускать сканы одновременно — у каждого свой `task_id`.

## Структура

```
domain_scanner/
├── app/
│   ├── app.py              # Flask роуты
│   ├── tasks.py            # Celery — логика сканирования
│   ├── config.py           # Настройки из env
│   ├── requirements.txt
│   ├── Dockerfile
│   └── templates/
│       └── index.html      # Веб-интерфейс
├── nginx/
│   ├── nginx.conf          # Продакшен (SSL + Basic Auth)
│   └── nginx.dev.conf      # Dev (без SSL)
├── redis/
│   └── redis.conf
├── docker-compose.yml      # Продакшен
├── docker-compose.dev.yml  # Локальная разработка
├── .env.example            # Шаблон переменных окружения
└── .gitignore
```

## Локальный запуск

```bash
# 1. Переменные окружения
cp .env.example .env
nano .env  # вписать ключи API

# 2. Запуск
docker compose -f docker-compose.dev.yml up --build

# Открыть http://localhost
```

## Деплой на VPS

### Требования
- Ubuntu 22.04+
- Docker + Docker Compose Plugin
- Домен с A-записью на IP сервера

### Установка

```bash
# Docker
apt update && apt install -y docker.io docker-compose-plugin

# Проект
git clone <repo> /opt/domain-scanner
cd /opt/domain-scanner

# Переменные окружения
cp .env.example .env
nano .env

# Basic Auth (логин/пароль для входа на сайт)
apt install -y apache2-utils
htpasswd -c nginx/htpasswd username
```

### SSL сертификат

```bash
# Временно поднять nginx только на 80 порту
# (закомментировать 443 блок в nginx/nginx.conf)
docker compose up -d nginx

# Получить сертификат
docker compose run --rm certbot certonly \
  --webroot --webroot-path=/var/www/certbot \
  -d yourdomain.com \
  --email your@email.com \
  --agree-tos

# Вписать домен в nginx.conf
sed -i 's/YOUR_DOMAIN.com/yourdomain.com/g' nginx/nginx.conf

# Раскомментировать 443 блок и перезапустить
docker compose restart nginx
```

### Запуск

```bash
docker compose up -d
```

### Обновление

```bash
git pull
docker compose up -d --build app celery_worker
```

## Управление

```bash
# Статус сервисов
docker compose ps

# Логи в реальном времени
docker compose logs -f

# Логи конкретного сервиса
docker compose logs -f app
docker compose logs -f celery_worker

# Остановка
docker compose down
```

## Переменные окружения

| Переменная | Описание |
|---|---|
| `REDIS_URL` | URL Redis (default: `redis://redis:6379/0`) |
| `RAPIDAPI_KEY` | Ключ RapidAPI (ScamDoc) |
| `VT_API_KEY` | Ключ VirusTotal |

## Ограничения

- **ScamDoc:** пауза 1.5с между запросами (лимит API)
- **VirusTotal:** пауза 16с между запросами (free tier: 4 req/min)
- Скорость сканирования определяется лимитами API, не железом