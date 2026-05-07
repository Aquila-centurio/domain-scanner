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

celery_app = Celery("scanner", broker=REDIS_URL, backend=REDIS_URL)
r = redis.from_url(REDIS_URL, decode_responses=True)
SCAN_TTL = 60 * 60 * 24

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


def parse_rows_streaming(file_path: str, filename: str):
    """Читает файл построчно — без загрузки всего в память."""
    ext = Path(filename).suffix.lower() if filename else ""
    if ext in (".csv", ".tsv"):
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            reader = csv.reader(f)
            next(reader, None)  # пропуск заголовка
            for row in reader:
                if len(row) >= 15:
                    yield row
                elif len(row) == 1 and "," in row[0]:
                    parts = row[0].split(",")
                    if len(parts) >= 15:
                        yield parts
    else:
        # xlsx грузим целиком (openpyxl не поддерживает стриминг иначе)
        with open(file_path, "rb") as fh:
            file_bytes = fh.read()
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True)
        ws = wb.active
        for row in ws.iter_rows(min_row=2, values_only=True):
            if row and row[0]:
                yield [str(c).strip() if c is not None else "" for c in row]
        wb.close()


def load_and_filter(task_id: str, file_path: str, filename: str, min_hours: float, max_hours: float, max_price: float):
    now = datetime.now(timezone.utc)
    window_start = now + timedelta(hours=min_hours)
    window_end = now + timedelta(hours=max_hours)

    domains = []
    total = passed_com = passed_reg = passed_auction = passed_price = 0

    log(task_id, f"Loading and filtering: {filename}")

    for parts in parse_rows_streaming(file_path, filename):
        total += 1
        try:
            domain = str(parts[1]).strip().lower()
            end_str = str(parts[3]).strip()
            price = float(parts[4])
            reg_str = str(parts[14]).strip() if len(parts) > 14 else ""
        except Exception:
            continue

        if not domain.endswith(".com"):
            continue
        passed_com += 1

        if not reg_str:
            continue
        try:
            reg_year = int(reg_str[:4])
        except (ValueError, IndexError):
            continue
        if reg_year < 2000 or reg_year > 2016:
            continue
        passed_reg += 1

        try:
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        except ValueError:
            continue

        if end_dt < window_start or end_dt > window_end:
            continue
        passed_auction += 1

        if max_price > 0 and price > max_price:
            continue
        passed_price += 1

        def sf(idx):
            try:
                return float(parts[idx])
            except (IndexError, ValueError, TypeError):
                return 0.0

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
            "url": str(parts[0]).strip(),
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

    log(task_id, f"Total rows: {total} | .com: {passed_com} | Reg 2000-2016: {passed_reg} | Auction window: {passed_auction} | Price ok: {passed_price} | Final: {len(domains)}", "success")
    domains.sort(key=lambda x: x["hours_left"])
    return domains


def check_scamdoc(task_id: str, domain: str, retries: int = 3):
    """API returns final_score as risk (0-1). Trust = (1 - risk) * 100."""
    url = "https://scampredictor.p.rapidapi.com/domain/" + domain
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


def check_virustotal(task_id: str, domain: str):
    headers = {"x-apikey": VT_API_KEY}
    try:
        resp = http_requests.get(
            "https://www.virustotal.com/api/v3/domains/" + domain,
            headers=headers, timeout=30
        )
        if resp.status_code == 200:
            stats = resp.json().get("data", {}).get("attributes", {}).get("last_analysis_stats", {})
            return {
                "malicious": stats.get("malicious", 0),
                "suspicious": stats.get("suspicious", 0),
                "clean": stats.get("malicious", 0) == 0 and stats.get("suspicious", 0) == 0,
            }
        elif resp.status_code == 429:
            time.sleep(60)
            return check_virustotal(task_id, domain)
        elif resp.status_code == 404:
            return {"malicious": 0, "suspicious": 0, "clean": True}
    except Exception:
        pass
    return {"malicious": 0, "suspicious": 0, "clean": True}


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

    try:
        # Фильтруем стримингом — файл не грузится целиком в память
        domains = load_and_filter(task_id, file_path, filename, min_hours, max_hours, max_price)
    finally:
        # Удаляем файл после фильтрации
        try:
            os.remove(file_path)
        except Exception:
            pass

    if not domains:
        log(task_id, "No domains match filters. Try widening the auction window.", "error")
        update_state(task_id, running=False, step="done")
        return

    if limit > 0 and limit < len(domains):
        domains = domains[:limit]
        log(task_id, f"Limited to first {limit} domains.")

    update_state(task_id, total=len(domains))

    # Step 2: ScamDoc
    update_state(task_id, step="scamdoc")
    log(task_id, f"--- ScamDoc scan ({len(domains)} domains) ---")

    passed_sd = []
    for i, entry in enumerate(domains):
        state = get_state(task_id)
        if not state.get("running"):
            break
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

    # Step 3: VirusTotal
    update_state(task_id, step="virustotal", progress=0, total=len(passed_sd))
    log(task_id, f"--- VirusTotal scan ({len(passed_sd)} domains) ---")

    for i, entry in enumerate(passed_sd):
        state = get_state(task_id)
        if not state.get("running"):
            break
        update_state(task_id, progress=i + 1)
        domain = entry["domain"]
        vt = check_virustotal(task_id, domain)
        entry["vt_malicious"] = vt["malicious"]
        entry["vt_suspicious"] = vt["suspicious"]

        state = get_state(task_id)
        if vt["clean"]:
            if entry["scamdoc_score"] >= max_sd_score:
                results_great = state.get("results_great", [])
                results_great.append(entry)
                update_state(task_id, results_great=results_great)
                log(task_id, f"CLEAN+GREAT {domain} (SD: {entry['scamdoc_score']}%)", "success")
            else:
                results_good = state.get("results_good", [])
                results_good.append(entry)
                update_state(task_id, results_good=results_good)
                log(task_id, f"CLEAN+GOOD {domain} (SD: {entry['scamdoc_score']}%)", "success")
        else:
            flagged = state.get("flagged", [])
            flagged.append(entry)
            update_state(task_id, flagged=flagged)
            log(task_id, f"FLAGGED {domain} ({vt['malicious']} malicious)", "error")
        time.sleep(16)

    final_state = get_state(task_id)
    great = len(final_state.get("results_great", []))
    good = len(final_state.get("results_good", []))
    flagged = len(final_state.get("flagged", []))
    level = "success" if (great + good) > 0 else "warning"
    log(task_id, f"--- DONE: {great} great, {good} good, {flagged} flagged ---", level)
    update_state(task_id, step="done", running=False)