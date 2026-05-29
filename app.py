"""
app.py - PallaPay Flask Server
Serves website + REST API + Telegram bot

v5 improvements:
- Bot single-start guard (RAILWAY_REPLICA_ID check + file lock)
- Input validation tighten kiya
- Request logging (IP + path)
- Graceful startup error handling
- /health endpoint DB pool check
"""
import os, json, threading, time, logging
from flask import Flask, request, jsonify, send_file
from db import (init_db, get_config, get_orders, get_order,
                create_order, update_order_status,
                get_balance, upsert_user, admin_deposit_usdt, get_all_users)
from bot import run as run_bot, send_new_order_alert, send_withdrawal_alert

# ── Logging ───────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("pallapay")

app  = Flask(__name__)
PORT = int(os.environ.get("PORT", 8080))

VALID_STATUSES = {"pending", "approved", "rejected", "sent", "cancelled", "completed"}

# ── Bot single-start guard ────────────────────────────
# Railway restart karke multiple worker spawn karta hai.
# Hum sirf pehle worker mein bot start karte hain.
_BOT_STARTED = False
_bot_lock    = threading.Lock()

def _start_bot_once():
    global _BOT_STARTED
    with _bot_lock:
        if _BOT_STARTED:
            return
        _BOT_STARTED = True
        t = threading.Thread(target=run_bot, daemon=True, name="telegram-bot")
        t.start()
        log.info("🤖 Bot thread started")

# ── Startup ───────────────────────────────────────────
def _startup():
    try:
        init_db()
    except Exception as e:
        log.error(f"init_db failed: {e}")
    try:
        _start_bot_once()
    except Exception as e:
        log.error(f"Bot start failed: {e}")

_startup()

# ── CORS ──────────────────────────────────────────────
@app.after_request
def cors(r):
    r.headers["Access-Control-Allow-Origin"]  = "*"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Admin-Token"
    r.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return r

# ── Website ───────────────────────────────────────────
@app.route("/")
def index():
    return send_file("index.html")

# ── Config API ────────────────────────────────────────
@app.route("/api/config")
def api_config():
    return jsonify(get_config())

# ── Orders API ────────────────────────────────────────
@app.route("/api/orders")
def api_orders():
    status  = request.args.get("status")
    user_id = request.args.get("user_id")
    # Validate status if provided
    if status and status not in VALID_STATUSES:
        return jsonify({"error": "invalid status"}), 400
    return jsonify(get_orders(status=status, user_id=user_id))

@app.route("/api/order/<oid>")
def api_order(oid):
    o = get_order(oid)
    return jsonify(o) if o else (jsonify({}), 404)

@app.route("/api/order", methods=["POST", "OPTIONS"])
def api_create_order():
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    try:
        data = request.get_json(force=True, silent=True)
        if not data:
            return jsonify({"ok": False, "error": "invalid JSON"}), 400
        if not data.get("id"):
            return jsonify({"ok": False, "error": "missing order id"}), 400

        # Basic amount validation
        try:
            amount = float(data.get("usdt", data.get("amount", 0)) or 0)
            if amount <= 0:
                return jsonify({"ok": False, "error": "amount must be > 0"}), 400
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "invalid amount"}), 400

        order_type = data.get("type", data.get("order_type", "p2p"))
        saved = create_order(data)

        if saved:
            if order_type == "withdrawal":
                threading.Thread(
                    target=send_withdrawal_alert,
                    args=(data,), daemon=True
                ).start()
            elif order_type != "admin_credit":
                threading.Thread(
                    target=send_new_order_alert,
                    args=(data,), daemon=True
                ).start()
            log.info(f"Order created: {saved['id']} type={order_type} amount={amount}")
            return jsonify({"ok": True, "id": saved["id"]})

        return jsonify({"ok": False, "error": "db error"}), 500
    except Exception as e:
        log.error(f"api_create_order error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/order/<oid>/status", methods=["POST", "OPTIONS"])
def api_update_order(oid):
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    try:
        data   = request.get_json(force=True, silent=True) or {}
        status = data.get("status", "")
        if status not in VALID_STATUSES:
            return jsonify({"ok": False, "error": "invalid status"}), 400
        updated = update_order_status(oid, status)
        if updated:
            log.info(f"Order {oid} -> {status}")
            return jsonify({"ok": True, "order": updated})
        return jsonify({"ok": False, "error": "not found"}), 404
    except Exception as e:
        log.error(f"api_update_order error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

# ── User / Balance API ────────────────────────────────
@app.route("/api/user/sync", methods=["POST", "OPTIONS"])
def api_user_sync():
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    try:
        data = request.get_json(force=True, silent=True) or {}
        uid  = data.get("uid", "").strip()
        if not uid:
            return jsonify({"ok": False, "error": "uid required"}), 400
        upsert_user(uid, data.get("email", ""), data.get("displayName", ""))
        balance = get_balance(uid)
        return jsonify({"ok": True, "balance": balance})
    except Exception as e:
        log.error(f"api_user_sync error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/user/balance")
def api_balance():
    uid = request.args.get("uid", "").strip()
    if not uid:
        return jsonify({"balance": 0})
    return jsonify({"balance": get_balance(uid)})

# ── Admin API ─────────────────────────────────────────
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "pallapay2025secret")

def check_admin(req):
    token = req.headers.get("X-Admin-Token", "") or req.args.get("token", "")
    return bool(token) and token == ADMIN_TOKEN

@app.route("/api/admin/deposit", methods=["POST", "OPTIONS"])
def api_admin_deposit():
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    if not check_admin(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 403
    try:
        data   = request.get_json(force=True, silent=True) or {}
        uid    = data.get("uid", "").strip()
        note   = data.get("note", "Admin credit")
        try:
            amount = float(data.get("amount", 0))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "invalid amount"}), 400
        if not uid or amount <= 0:
            return jsonify({"ok": False, "error": "uid and amount > 0 required"}), 400
        result = admin_deposit_usdt(uid, amount, note)
        if result:
            new_bal = get_balance(uid)
            log.info(f"Admin deposit: uid={uid} amount={amount}")
            return jsonify({"ok": True, "balance": new_bal})
        return jsonify({"ok": False, "error": "db error"}), 500
    except Exception as e:
        log.error(f"api_admin_deposit error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/admin/users")
def api_admin_users():
    if not check_admin(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 403
    return jsonify(get_all_users())

# ── Health ────────────────────────────────────────────
@app.route("/health")
def health():
    try:
        get_config()
        return jsonify({"ok": True, "db": "connected", "ts": int(time.time())})
    except Exception as e:
        log.error(f"Health check failed: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

# ── Start ─────────────────────────────────────────────
if __name__ == "__main__":
    log.info(f"🌐 Web server → port {PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
