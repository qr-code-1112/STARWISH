import csv
import hmac
import io
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from flask import (Flask, Response, jsonify, redirect, render_template_string,
                   request, send_from_directory, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "orders.db"))
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
TZ = ZoneInfo("Asia/Taipei")
STATUSES = ["新訂單", "已聯絡", "已確認", "製作中", "已完成", "已取消"]

with open(os.path.join(BASE_DIR, "catalog.json"), encoding="utf-8") as f:
    CATALOG = json.load(f)
PR = CATALOG["print"]
PRICES = {n: p for n, p, _u, _s in PR["goods"] + PR["decor"]}
UNITS = {n: u for n, _p, u, _s in PR["goods"] + PR["decor"]}
DESIGN = {n: p for n, p in CATALOG["design"]["items"]}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)  # 在反向代理後面時取得真實 IP


# ---------- 資料庫 ----------
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created TEXT NOT NULL,
            status TEXT NOT NULL,
            idol TEXT, event_date TEXT, who TEXT, contact TEXT,
            total INTEGER NOT NULL,
            data TEXT NOT NULL)"""
        )


init_db()


# ---------- 訂單驗證與計價（以伺服器價格為準） ----------
def clean(v, n):
    return (v if isinstance(v, str) else "").strip()[:n]


def pick_opts(options, given):
    """依 catalog 驗證選項，回傳 ({標籤: 文字}, 加價)。未提供的單選用第一個選項。"""
    given = given if isinstance(given, dict) else {}
    shown, price = {}, 0
    for o in options:
        prices = {c["v"]: c["p"] for c in o["choices"]}
        v = given.get(o["id"])
        if o["type"] == "radio":
            v = o["choices"][0]["v"] if v is None else v
            if not isinstance(v, str) or v not in prices:
                raise ValueError("選項內容錯誤")
            shown[o["label"]] = v
            price += prices[v]
        else:
            vs = []
            for x in v if isinstance(v, list) else []:
                if not isinstance(x, str) or x not in prices:
                    raise ValueError("選項內容錯誤")
                if x not in vs:
                    vs.append(x)
            shown[o["label"]] = "、".join(vs) or "無"
            price += sum(prices[x] for x in vs)
    return shown, price


def build_order(p):
    if not isinstance(p, dict):
        raise ValueError("資料格式錯誤")
    # 基本服務：勾選幾項就按比例計價（全選 = 套組價）
    given = p.get("services") if isinstance(p.get("services"), dict) else {}
    services, svc_opt = [], 0
    for s in CATALOG["services"]:
        e = given.get(s["id"])
        if isinstance(e, dict) and e.get("on"):
            shown, price = pick_opts(s["options"], e.get("opts"))
            services.append({"name": s["name"], "opts": shown, "extra": price})
            svc_opt += price
    base_total = round(CATALOG["base"] * len(services) / len(CATALOG["services"]))

    # 印刷
    pr = p.get("print") if isinstance(p.get("print"), dict) else {}
    items = {}
    for name, q in (pr.get("items") if isinstance(pr.get("items"), dict) else {}).items():
        if name not in PRICES:
            raise ValueError("未知的品項")
        try:
            q = int(q)
        except (TypeError, ValueError):
            raise ValueError("數量格式錯誤")
        if q < 0 or q > 100000:
            raise ValueError("數量超出範圍")
        if q:
            items[name] = q
    goods_total = sum(PRICES[n] * q for n, q in items.items())
    print_opts, print_opt = pick_opts(PR["options"], pr.get("opts"))
    if not goods_total:
        print_opts, print_opt = {}, 0

    # 設計
    ds = p.get("design") if isinstance(p.get("design"), dict) else {}
    design = []
    for d in ds.get("items") if isinstance(ds.get("items"), list) else []:
        if not isinstance(d, str) or d not in DESIGN:
            raise ValueError("未知的設計項目")
        if d not in design:
            design.append(d)
    design_total = sum(DESIGN[d] for d in design)
    design_opts, design_opt = pick_opts(CATALOG["design"]["options"], ds.get("opts"))
    if not design_total:
        design_opts, design_opt = {}, 0

    fee = round((goods_total + print_opt + design_total + design_opt) * CATALOG["fee"])
    total = base_total + svc_opt + goods_total + print_opt + design_total + design_opt + fee
    order = {
        "idol": clean(p.get("idol"), 60), "date": clean(p.get("date"), 10),
        "who": clean(p.get("who"), 40), "contact": clean(p.get("contact"), 120),
        "services": services, "items": items, "print_opts": print_opts,
        "design": design, "design_opts": design_opts,
        "style": clean(p.get("style"), 200), "colors": clean(p.get("colors"), 200),
        "ref": clean(p.get("ref"), 300), "memo": clean(p.get("memo"), 1000),
        "base_total": base_total, "fee": fee, "total": total,
    }
    if not (order["idol"] and order["who"] and order["contact"]):
        raise ValueError("請填寫偶像名稱、主辦人稱呼與聯絡方式")
    if total <= 0:
        raise ValueError("請至少選擇一項服務或品項")
    return order


# ---------- 簡易頻率限制 ----------
_hits, _lock = {}, threading.Lock()


def limited(ip, limit=5, window=600):
    now = time.time()
    with _lock:
        q = [t for t in _hits.get(ip, []) if now - t < window]
        if len(q) >= limit:
            _hits[ip] = q
            return True
        q.append(now)
        _hits[ip] = q
        return False


# ---------- Discord 通知 ----------
def order_text(o):
    lines = [f"偶像／角色：{o['idol']}", f"活動日期：{o['date'] or '未填'}",
             f"主辦人：{o['who']}　聯絡：{o['contact']}", "", "【基本服務】"]
    for s in o["services"] or []:
        lines.append(f"- {s['name']}：" + "；".join(f"{k}：{v}" for k, v in s["opts"].items()))
    if not o["services"]:
        lines.append("- 無")
    if o["items"]:
        lines += ["", "【印刷】" + "、".join(f"{n} × {q}{UNITS[n]}" for n, q in o["items"].items())]
        lines.append("；".join(f"{k}：{v}" for k, v in o["print_opts"].items()))
    if o["design"]:
        lines += ["", "【設計】" + "、".join(o["design"]),
                  "；".join(f"{k}：{v}" for k, v in o["design_opts"].items())]
    for label, key in (("風格", "style"), ("色調／元素", "colors"), ("參考連結", "ref"), ("說明", "memo")):
        if o[key]:
            lines.append(f"{label}：{o[key]}")
    return "\n".join(lines)


def notify(order_id, o):
    if not WEBHOOK:
        return
    try:
        requests.post(
            WEBHOOK,
            json={
                "allowed_mentions": {"parse": []},
                "embeds": [{
                    "title": f"新訂單 #{order_id}　NT$ {o['total']:,}",
                    "description": order_text(o)[:3900],
                    "color": 0x4B64A8,
                }],
            },
            timeout=10,
        )
    except requests.RequestException as e:
        app.logger.warning("Discord 通知失敗：%s", e)


# ---------- 公開路由 ----------
@app.get("/")
def index():
    return send_from_directory(os.path.join(BASE_DIR, "static"), "index.html")


@app.get("/api/catalog")
def catalog():
    return jsonify(CATALOG)


@app.get("/healthz")
def healthz():
    return "ok"


@app.post("/api/orders")
def create_order():
    payload = request.get_json(silent=True)
    if isinstance(payload, dict) and payload.get("website"):
        return jsonify(ok=True, id=0, total=0)  # honeypot：假裝成功，不存檔
    if limited(request.remote_addr or "?"):
        return jsonify(error="送出太頻繁，請稍後再試"), 429
    try:
        o = build_order(payload)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db() as con:
        cur = con.execute(
            "INSERT INTO orders(created,status,idol,event_date,who,contact,total,data) VALUES(?,?,?,?,?,?,?,?)",
            (created, STATUSES[0], o["idol"], o["date"], o["who"], o["contact"], o["total"],
             json.dumps(o, ensure_ascii=False)),
        )
        order_id = cur.lastrowid
    threading.Thread(target=notify, args=(order_id, o), daemon=True).start()
    return jsonify(ok=True, id=order_id, total=o["total"]), 201


# ---------- 管理後台 ----------
def require_admin(f):
    @wraps(f)
    def wrapper(*a, **k):
        if not ADMIN_PASSWORD:
            return Response("尚未設定 ADMIN_PASSWORD，後台停用。", 503)
        au = request.authorization
        ok = bool(au) and hmac.compare_digest((au.username or "").encode(), ADMIN_USER.encode()) \
            and hmac.compare_digest((au.password or "").encode(), ADMIN_PASSWORD.encode())
        if not ok:
            return Response("需要登入", 401, {"WWW-Authenticate": 'Basic realm="Starwish Admin"'})
        return f(*a, **k)
    return wrapper


def local_time(iso):
    return datetime.fromisoformat(iso).astimezone(TZ).strftime("%Y-%m-%d %H:%M")


ADMIN_HTML = """<!doctype html><html lang="zh-Hant"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>訂單管理</title>
<style>body{font-family:"Noto Sans TC","Microsoft JhengHei",sans-serif;margin:20px;color:#3A4060}
table{border-collapse:collapse;width:100%}th,td{border-bottom:1px solid #DCE1F0;padding:8px;text-align:left;vertical-align:top;font-size:14px}
pre{white-space:pre-wrap;margin:6px 0 0;font:inherit;color:#6B7191}select,button{font:inherit}
.n{white-space:nowrap}</style>
<h1>訂單管理（{{ rows|length }}）</h1><p><a href="{{ url_for('export') }}">匯出 CSV</a></p>
<table><tr><th>編號</th><th>時間</th><th>內容</th><th class="n">總額</th><th>狀態</th></tr>
{% for r in rows %}<tr><td>#{{ r.id }}</td><td class="n">{{ r.time }}</td>
<td><b>{{ r.idol }}</b>｜{{ r.event_date }}｜{{ r.who }}（{{ r.contact }}）<pre>{{ r.text }}</pre></td>
<td class="n">NT$ {{ '{:,}'.format(r.total) }}</td>
<td><form method="post" action="{{ url_for('set_status', order_id=r.id) }}">
<select name="status">{% for s in statuses %}<option{% if s==r.status %} selected{% endif %}>{{ s }}</option>{% endfor %}</select>
<button>更新</button></form></td></tr>{% endfor %}</table></html>"""


@app.get("/admin")
@require_admin
def admin():
    with db() as con:
        rows = con.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()
    view = [
        {"id": r["id"], "time": local_time(r["created"]), "idol": r["idol"], "event_date": r["event_date"],
         "who": r["who"], "contact": r["contact"], "total": r["total"], "status": r["status"],
         "text": order_text(json.loads(r["data"]))}
        for r in rows
    ]
    return render_template_string(ADMIN_HTML, rows=view, statuses=STATUSES)


@app.post("/admin/orders/<int:order_id>/status")
@require_admin
def set_status(order_id):
    origin = request.headers.get("Origin")
    if origin and urlparse(origin).netloc != request.host:
        return Response("來源不符", 403)
    status = request.form.get("status")
    if status in STATUSES:
        with db() as con:
            con.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
    return redirect(url_for("admin"))


def safe_cell(v):
    v = str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@") else v  # 避免 Excel 公式注入


@app.get("/admin/export.csv")
@require_admin
def export():
    with db() as con:
        rows = con.execute("SELECT * FROM orders ORDER BY id").fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["編號", "時間", "狀態", "偶像", "活動日期", "主辦人", "聯絡方式", "總額", "品項", "設計", "風格", "說明"])
    for r in rows:
        o = json.loads(r["data"])
        w.writerow([r["id"], local_time(r["created"]), r["status"], *map(safe_cell, (
            r["idol"], r["event_date"], r["who"], r["contact"], r["total"],
            "、".join(s["name"] for s in o["services"]) + "｜" + "、".join(f"{n}×{q}" for n, q in o["items"].items()), "、".join(o["design"]),
            o["style"], o["memo"]))])
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=starwish-orders.csv"})


if __name__ == "__main__":
    app.run(debug=False, port=int(os.environ.get("PORT", 5000)))
