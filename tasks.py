import csv
import io
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

import redis
import openpyxl
from celery import Celery

from config import REDIS_URL


celery_app = Celery("scanner", broker=REDIS_URL, backend=REDIS_URL)
r = redis.from_url(REDIS_URL, decode_responses=True)

SCAN_TTL = 60 * 60 * 24


# =========================
# STATE MANAGEMENT
# =========================

def state_key(task_id: str) -> str:
    return f"scan:{task_id}"


def get_state(task_id: str) -> dict:
    raw = r.get(state_key(task_id))
    return json.loads(raw) if raw else {}


def set_state(task_id: str, state: dict):
    r.set(state_key(task_id), json.dumps(state), ex=SCAN_TTL)


def update_state(task_id: str, **kwargs):
    state = get_state(task_id)
    state.update(kwargs)
    set_state(task_id, state)


def log(task_id: str, msg: str, level: str = "info"):
    state = get_state(task_id)
    logs = state.get("logs", [])
    logs.append({
        "msg": msg,
        "level": level,
        "time": datetime.now().strftime("%H:%M:%S"),
    })
    state["logs"] = logs
    set_state(task_id, state)


# =========================
# PARSING
# =========================

def parse_rows(file_bytes: bytes, filename: str):
    ext = Path(filename).suffix.lower() if filename else ""

    # ---- CSV / TSV ----
    if ext in (".csv", ".tsv"):
        text = file_bytes.decode("utf-8", errors="ignore")
        reader = csv.reader(io.StringIO(text))
        next(reader, None)

        for row in reader:
            if len(row) >= 15:
                yield row
            elif len(row) == 1 and "," in row[0]:
                parts = row[0].split(",")
                if len(parts) >= 15:
                    yield parts

    # ---- XLSX ----
    else:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True)
        ws = wb.active

        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row:
                continue

            val = row[0]

            # Excel с CSV внутри одной ячейки
            if isinstance(val, str) and "," in val:
                parts = val.split(",")
                if len(parts) >= 15:
                    yield parts

            # нормальный XLSX
            elif len(row) >= 15:
                yield [str(c).strip() if c is not None else "" for c in row]

        wb.close()


# =========================
# FILTER
# =========================

def load_and_filter(task_id: str, file_bytes: bytes, filename: str,
                    min_hours: float, max_hours: float, max_price: float):

    now = datetime.now(timezone.utc)
    window_start = now + timedelta(hours=min_hours)
    window_end = now + timedelta(hours=max_hours)

    domains = []

    total = passed_com = passed_reg = passed_auction = passed_price = 0

    log(task_id, f"Loading and filtering: {filename}")

    for parts in parse_rows(file_bytes, filename):
        total += 1

        try:
            domain = parts[1].strip().lower()
            end_str = parts[3].strip()
            reg_str = parts[14].strip()   # ВАЖНО
        except:
            continue

        # ---- .com ----
        if not domain.endswith(".com"):
            continue
        passed_com += 1

        # ---- регистрация ----
        try:
            reg_year = int(reg_str[:4])
        except:
            continue

        if reg_year < 2000 or reg_year > 2016:
            continue
        passed_reg += 1

        # ---- время ----
        try:
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        except:
            continue

        if end_dt < window_start or end_dt > window_end:
            continue
        passed_auction += 1

        # ---- safe float ----
        def sf(idx):
            try:
                return float(parts[idx])
            except:
                return 0.0

        price = sf(4)

        if max_price > 0 and price > max_price:
            continue
        passed_price += 1

        domains.append({
            "domain": domain,
            "reg_date": reg_str[:10],
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

    stats = {
        "total_rows": total,
        "dot_com": passed_com,
        "reg_2000_2016": passed_reg,
        "auction_window": passed_auction,
        "price_ok": passed_price,
        "final_domains": len(domains),
    }

    update_state(task_id, stats=stats)

    log(task_id, f"Total rows: {total} | Final domains: {len(domains)}", "success")

    domains.sort(key=lambda x: x["hours_left"])
    return domains


# =========================
# TASK
# =========================

@celery_app.task(bind=True)
def run_scan(self, task_id: str, file_path: str, filename: str,
             min_hours: float, max_hours: float, limit: int,
             min_sd_score: int, max_sd_score: int, max_price: float):

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

    log(task_id, f"Loading file: {filename}")

    with open(file_path, "rb") as fh:
        file_bytes = fh.read()

    try:
        os.remove(file_path)
    except:
        pass

    domains = load_and_filter(
        task_id,
        file_bytes,
        filename,
        min_hours,
        max_hours,
        max_price
    )

    if not domains:
        log(task_id, "No domains match filters", "error")
        update_state(task_id, running=False, step="done")
        return

    if limit > 0 and limit < len(domains):
        domains = domains[:limit]

    log(task_id, f"Found {len(domains)} domains. Ready for scanning.", "success")

    update_state(
        task_id,
        total=len(domains),
        step="done",
        running=False
    )
