"""
tasks.py — Celery задачи
Здесь живёт вся логика сканирования (ScamDoc + VirusTotal).
Flask только ставит задачу в очередь и возвращает task_id.
Celery worker забирает задачу и выполняет её в фоне.
Прогресс/логи/результаты пишутся в Redis по ключу task_id.
"""

import csv
import os
import io
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import redis
import requests as http_requests
import openpyxl
from celery import Celery

from config import REDIS_URL, RAPIDAPI_KEY, VT_API_KEY

# ── Celery app ────────────────────────────────────────────────────────────────
# broker  — откуда Celery берёт задачи (Redis db 0)
# backend — куда пишет результат задачи (Redis db 0)
celery_app = Celery("scanner", broker=REDIS_URL, backend=REDIS_URL)

# ── Redis клиент для хранения состояния сканов ────────────────────────────────
# Отдельный клиент (не через Celery backend) — для прямой записи прогресса
r = redis.from_url(REDIS_URL, decode_responses=True)

# Время жизни ключей в Redis — 24 часа
# После этого состояние скана автоматически удалится
SCAN_TTL = 60 * 60 * 24


# ── Утилиты для работы с состоянием скана ────────────────────────────────────

def state_key(task_id: str) -> str:
    """Ключ в Redis для состояния скана."""
    return f"scan:{task_id}"


def get_state(task_id: str) -> dict:
    """Читает текущее состояние скана из Redis."""
    raw = r.get(state_key(task_id))
    if not raw:
        return {}
    return json.loads(raw)


def set_state(task_id: str, state: dict):
    """Записывает состояние скана в Redis с TTL."""
    r.set(state_key(task_id), json.dumps(state), ex=SCAN_TTL)


def update_state(task_id: str, **kwargs):
    """Обновляет отдельные поля состояния не перезаписывая всё."""
    state = get_state(task_id)
    state.update(kwargs)
    set_state(task_id, state)


def log(task_id: str, msg: str, level: str = "info"):
    """Добавляет строку в лог скана."""
    state = get_state(task_id)
    logs = state.get("logs", [])
    logs.append({
        "msg": msg,
        "level": level,
        "time": datetime.now().strftime("%H:%M:%S"),
    })
    state["logs"] = logs
    set_state(task_id, state)


# ── Парсинг входного файла ────────────────────────────────────────────────────

def parse_rows(file_bytes: bytes, filename: str):
    """
    Генератор строк из xlsx или csv файла.
    Каждая строка — список строк минимум 15 элементов.
    """
    ext = Path(filename).suffix.lower() if filename else ""

    if ext in (".csv", ".tsv"):
        text = file_bytes.decode("utf-8", errors="ignore")
        reader = csv.reader(io.StringIO(text))
        next(reader, None)  # пропускаем заголовок
        for row in reader:
            if len(row) >= 15:
                yield row
            elif len(row) == 1 and "," in row[0]:
                parts = row[0].split(",")
                if len(parts) >= 15:
                    yield parts
    else:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True)
        ws = wb.active
        for row in ws.iter_rows(min_row=2, values_only=True):
            val = row[0]
            if not val or not isinstance(val, str):
                continue
            parts = val.split(",")
            if len(parts) >= 15:
                yield parts
            elif len(row) >= 15:
                yield [str(c) if c is not None else "" for c in row]
        wb.close()


def load_and_filter(task_id: str, file_bytes: bytes, filename: str,
                    min_hours: float, max_hours: float, max_price: float) -> list:
    """
    Фильтрует домены по критериям:
    - только .com
    - зарегистрированы в 2000-2016
    - аукцион заканчивается в заданном временном окне
    - цена не превышает лимит
    """
    now = datetime.now(timezone.utc)
    window_start = now + timedelta(hours=min_hours)
    window_end = now + timedelta(hours=max_hours)

    domains = []
    total = passed_com = passed_reg = passed_auction = passed_price = 0

    for parts in parse_rows(file_bytes, filename):
        total += 1
        domain = parts[1].strip().lower()

        if not domain.endswith(".com"):
            continue
        passed_com += 1

        # Год регистрации из колонки 14
        reg_date = parts[14].strip() if len(parts) > 14 else ""
        reg_year = 0
        if reg_date:
            try:
                reg_year = int(reg_date[:4])
            except (ValueError, IndexError):
                pass
        if reg_year < 2000 or reg_year > 2016:
            continue
        passed_reg += 1

        # Дата окончания аукциона из колонки 3
        end_str = parts[3].strip()
        if not end_str:
            continue
        try:
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        if end_dt < window_start or end_dt > window_end:
            continue
        passed_auction += 1

        def sf(idx):
            try:
                return float(parts[idx])
            except (IndexError, ValueError, TypeError):
                return 0.0

        price = sf(4)
        if max_price > 0 and price > max_price:
            continue
        passed_price += 1

        domains.append({
            "domain": domain,
            "reg_date": reg_date[:10],
            "reg_year": reg_year,
            "end_date": end_str[:19].replace("T", " "),
            "hours_left": round((end_dt - now).total_seconds() / 3600, 1),
            "price": price,
            "ahrefs_dr": sf(8),
            "majestic_tf": sf(23),
            "backlinks": sf(20),
            "bid_count": int(sf(7)),
            "url": parts[0].strip(),
        })

    domains.sort(key=lambda x: x["hours_left"])

    # Сохраняем статистику фильтрации в состояние
    stats = {
        "total_rows": total,
        "dot_com": passed_com,
        "reg_2000_2016": passed_reg,
        "auction_window": passed_auction,
        "price_ok": passed_price,
    }
    update_state(task_id, stats=stats)
    return domains


# ── API вызовы ────────────────────────────────────────────────────────────────

def check_scamdoc(task_id: str, domain: str, retries: int = 3):
    """
    Проверяет домен через ScamDoc API.
    final_score — риск (0-1), пересчитываем в trust (0-100%).
    """
    url = f"https://scampredictor.p.rapidapi.com/domain/{domain}"
    headers = {
        "x-rapidapi-key": RAPIDAPI_KEY,
        "x-rapidapi-host": "scampredictor.p.rapidapi.com",
        "Content-Type": "application/json",
    }

    for attempt in range(retries):
        try:
            timeout = 45 + (attempt * 15)
            resp = http_requests.get(url, headers=headers, timeout=timeout)

            if resp.status_code == 200:
                data = resp.json()
                final_score = data.get("final_score")
                if final_score is not None:
                    risk = float(final_score)
                    trust = max(0, min(100, round((1 - risk) * 100)))
                    log(task_id, f"ScamDoc [{domain}]: risk={risk} trust={trust}%", "dim")
                    return trust
                log(task_id, f"ScamDoc [{domain}]: no final_score in {str(data)[:200]}", "warning")
                return None

            elif resp.status_code == 429:
                log(task_id, "ScamDoc rate limited. Waiting 15s...", "warning")
                time.sleep(15)
                continue

            else:
                log(task_id, f"ScamDoc [{domain}]: HTTP {resp.status_code}", "warning")
                return None

        except Exception as e:
            if attempt < retries - 1:
                wait = 5 * (attempt + 1)
                log(task_id, f"ScamDoc timeout for {domain}, retry {attempt + 2}/{retries} in {wait}s...", "warning")
                time.sleep(wait)
            else:
                log(task_id, f"ScamDoc failed after {retries} tries: {domain}", "error")

    return None


def check_virustotal(task_id: str, domain: str) -> dict:
    """
    Проверяет домен через VirusTotal API.
    Free tier: 4 запроса/минуту → задержка 16с между запросами.
    """
    headers = {"x-apikey": VT_API_KEY}
    try:
        resp = http_requests.get(
            f"https://www.virustotal.com/api/v3/domains/{domain}",
            headers=headers,
            timeout=30,
        )

        if resp.status_code == 200:
            stats = (resp.json()
                     .get("data", {})
                     .get("attributes", {})
                     .get("last_analysis_stats", {}))
            malicious = stats.get("malicious", 0)
            suspicious = stats.get("suspicious", 0)
            return {
                "malicious": malicious,
                "suspicious": suspicious,
                "clean": malicious == 0 and suspicious == 0,
            }

        elif resp.status_code == 429:
            # Превышен лимит — ждём минуту и повторяем
            log(task_id, "VirusTotal rate limit, waiting 60s...", "warning")
            time.sleep(60)
            return check_virustotal(task_id, domain)

        elif resp.status_code == 404:
            # Домен не найден в базе VT — считаем чистым
            return {"malicious": 0, "suspicious": 0, "clean": True}

    except Exception:
        pass

    # При ошибке считаем чистым (не блокируем домен из-за сбоя API)
    return {"malicious": 0, "suspicious": 0, "clean": True}


# ── Celery задача ─────────────────────────────────────────────────────────────

@celery_app.task(bind=True)
def run_scan(self, task_id: str, file_path: str, filename: str,
             min_hours: float, max_hours: float, limit: int,
             min_sd_score: int, max_sd_score: int, max_price: float):
    """
    Основная задача сканирования.
    bind=True — self это экземпляр задачи Celery (нужен для self.request.id).
    Выполняется в Celery worker, не блокирует Flask.
    """

    # Инициализируем состояние скана в Redis
    set_state(task_id, {
        "running": True,
        "step": "filtering",
        "progress": 0,
        "total": 0,
        "logs": [],
        "results_great": [],
        "results_good": [],
        "flagged": [],
        "stats": {},
    })

    # ── Шаг 1: Фильтрация ────────────────────────────────────────────────────
    log(task_id, f"Loading and filtering: {filename}")

    # Читаем файл с диска, удаляем после загрузки
    with open(file_path, "rb") as fh:
        file_bytes = fh.read()
    try:
        os.remove(file_path)
    except Exception:
        pass

    domains = load_and_filter(task_id, file_bytes, filename, min_hours, max_hours, max_price)

    state = get_state(task_id)
    s = state.get("stats", {})
    log(task_id, f"Total rows: {s.get('total_rows', 0)}")
    log(task_id, f".com domains: {s.get('dot_com', 0)}")
    log(task_id, f"Registered 2000-2016: {s.get('reg_2000_2016', 0)}")
    log(task_id, f"Auction {min_hours}-{max_hours}h: {s.get('auction_window', 0)}")
    log(task_id, f"Price filter passed: {s.get('price_ok', 0)}", "success")

    if not domains:
        log(task_id, "No domains match filters.", "error")
        update_state(task_id, running=False, step="done")
        return

    if limit > 0 and limit < len(domains):
        domains = domains[:limit]
        log(task_id, f"Limited to first {limit} domains.")

    update_state(task_id, total=len(domains))

    # ── Шаг 2: ScamDoc ───────────────────────────────────────────────────────
    update_state(task_id, step="scamdoc")
    log(task_id, f"--- ScamDoc scan ({len(domains)} domains) ---")

    passed_sd = []
    for i, entry in enumerate(domains):
        # Проверяем не остановил ли пользователь скан
        if not get_state(task_id).get("running"):
            log(task_id, "Scan stopped by user.", "warning")
            update_state(task_id, step="done")
            return

        update_state(task_id, progress=i + 1)
        domain = entry["domain"]
        score = check_scamdoc(task_id, domain)

        if score is not None:
            entry["scamdoc_score"] = score
            if score >= min_sd_score:
                tier = "GREAT" if score >= max_sd_score else "GOOD"
                passed_sd.append(entry)
                log(task_id, f"{tier} {domain}: {score}% | {entry['hours_left']}h | ${entry['price']}", "success")
            else:
                log(task_id, f"SKIP {domain}: {score}% (below {min_sd_score})", "dim")
        else:
            log(task_id, f"? {domain}: no score returned", "warning")

        time.sleep(1.5)

    if not passed_sd:
        log(task_id, f"No domains scored {min_sd_score}%+.", "warning")
        update_state(task_id, running=False, step="done")
        return

    log(task_id, f"--- {len(passed_sd)} domains passed ScamDoc ---", "success")

    # ── Шаг 3: VirusTotal ────────────────────────────────────────────────────
    update_state(task_id, step="virustotal", progress=0, total=len(passed_sd))
    log(task_id, f"--- VirusTotal scan ({len(passed_sd)} domains) ---")

    for i, entry in enumerate(passed_sd):
        if not get_state(task_id).get("running"):
            log(task_id, "Scan stopped by user.", "warning")
            update_state(task_id, step="done")
            return

        update_state(task_id, progress=i + 1)
        domain = entry["domain"]
        vt = check_virustotal(task_id, domain)

        entry["vt_malicious"] = vt["malicious"]
        entry["vt_suspicious"] = vt["suspicious"]

        # Читаем текущее состояние и дописываем результат
        state = get_state(task_id)
        if vt["clean"]:
            if entry["scamdoc_score"] >= max_sd_score:
                state["results_great"].append(entry)
                log(task_id, f"CLEAN+GREAT {domain} (SD: {entry['scamdoc_score']}%)", "success")
            else:
                state["results_good"].append(entry)
                log(task_id, f"CLEAN+GOOD {domain} (SD: {entry['scamdoc_score']}%)", "success")
        else:
            state["flagged"].append(entry)
            log(task_id, f"FLAGGED {domain} ({vt['malicious']} malicious)", "error")
        set_state(task_id, state)

        time.sleep(16)  # лимит VT free tier: 4 запроса/минуту

    state = get_state(task_id)
    great = len(state.get("results_great", []))
    good = len(state.get("results_good", []))
    flagged = len(state.get("flagged", []))
    level = "success" if (great + good) > 0 else "warning"
    log(task_id, f"--- DONE: {great} great, {good} good, {flagged} flagged ---", level)

    update_state(task_id, running=False, step="done")