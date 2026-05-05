"""
config.py — все настройки из переменных окружения.
Ключи НЕ хранятся в коде — только в .env файле (не коммитится в git).
"""
 
import os
 
REDIS_URL    = os.environ["REDIS_URL"]
RAPIDAPI_KEY = os.environ["RAPIDAPI_KEY"]
VT_API_KEY   = os.environ["VT_API_KEY"]
 
