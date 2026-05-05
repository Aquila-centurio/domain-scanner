"""
app.py — Flask HTTP роуты.
Логика сканирования — в tasks.py (Celery).
HTML — в templates/index.html (Jinja2).
"""

import io
import os
import uuid

import openpyxl
from flask import Flask, jsonify, render_template, request, send_file

from tasks import get_state, run_scan, update_state

app = Flask(__name__)

# Временная директория для загруженных файлов.
# Шарится между app и celery_worker через Docker volume.
UPLOAD_DIR = "/tmp/scanner_uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/scan", methods=["POST"])
def start_scan():
    """
    Принимает файл + параметры, ставит задачу в Celery.
    Возвращает task_id — по нему клиент поллит /status.
    """
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file uploaded"}), 400

    task_id  = str(uuid.uuid4())
    filename = f.filename or "upload"

    # Сохраняем файл на диск — не гоним 200MB через Redis
    file_path = os.path.join(UPLOAD_DIR, f"{task_id}_{filename}")
    f.save(file_path)

    run_scan.delay(
        task_id,
        file_path,
        filename,
        float(request.form.get("min_hours", 6)),
        float(request.form.get("max_hours", 24)),
        int(request.form.get("limit", 50)),
        int(request.form.get("min_sd_score", 80)),
        int(request.form.get("max_sd_score", 100)),
        float(request.form.get("max_price", 0)),
    )

    return jsonify({"status": "started", "task_id": task_id})


@app.route("/status")
def status():
    """Текущее состояние скана — фронтенд поллит каждые 1.5с."""
    task_id = request.args.get("task_id")
    if not task_id:
        return jsonify({"error": "task_id required"}), 400

    state = get_state(task_id)
    if not state:
        return jsonify({"error": "task not found"}), 404

    return jsonify({
        "running":       state.get("running", False),
        "step":          state.get("step", ""),
        "progress":      state.get("progress", 0),
        "total":         state.get("total", 0),
        "logs":          state.get("logs", [])[-50:],
        "results_great": state.get("results_great", []),
        "results_good":  state.get("results_good", []),
        "flagged":       state.get("flagged", []),
        "stats":         state.get("stats", {}),
    })


@app.route("/stop", methods=["POST"])
def stop_scan():
    """Мягкая остановка — Celery worker проверяет флаг перед каждым доменом."""
    task_id = request.args.get("task_id")
    if not task_id:
        return jsonify({"error": "task_id required"}), 400

    update_state(task_id, running=False)
    return jsonify({"status": "stopping"})


@app.route("/download")
def download_xlsx():
    """Отдаёт результаты в xlsx — два листа: Great и Good."""
    task_id = request.args.get("task_id")
    if not task_id:
        return "task_id required", 400

    state = get_state(task_id)
    if not state:
        return "task not found", 404

    results_great = state.get("results_great", [])
    results_good  = state.get("results_good", [])

    if not results_great and not results_good:
        return "No results", 404

    fields = [
        "domain", "scamdoc_score", "hours_left", "end_date", "price",
        "bid_count", "reg_date", "ahrefs_dr", "backlinks",
        "vt_malicious", "vt_suspicious", "url",
    ]

    wb  = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "Great Domains"
    ws1.append(fields)
    for d in sorted(results_great, key=lambda x: x.get("scamdoc_score", 0), reverse=True):
        ws1.append([d.get(f, "") for f in fields])

    ws2 = wb.create_sheet("Good Domains")
    ws2.append(fields)
    for d in sorted(results_good, key=lambda x: x.get("scamdoc_score", 0), reverse=True):
        ws2.append([d.get(f, "") for f in fields])

    mem = io.BytesIO()
    wb.save(mem)
    mem.seek(0)

    return send_file(
        mem,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="domain_results.xlsx",
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8080, debug=True)