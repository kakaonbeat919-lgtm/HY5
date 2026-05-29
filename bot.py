"""
bot.py - PallaPay Telegram Admin Bot
Full admin: orders, deposits, withdrawals, UPI, rate, limits, stats

v5 improvements:
- Thread-safe _state dict with threading.Lock
- Retry logic on Telegram API failures
- Better exception messages in callbacks
- /setaed command for AED rate
- Cleaner polling loop with backoff
"""
import os, json, time, urllib.request, urllib.error, threading
from db import (
    get_config, set_config,
    get_orders, get_order,
    update_order_status, get_stats,
    admin_deposit_usdt, get_balance,
    get_all_users, upsert_user
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = set(
    int(x.strip())
    for x in os.environ.get("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
)

# ─── Telegram helpers ─────────────────────────────────
def _call(method, params=None, retries=2):
    url  = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = json.dumps(params or {}).encode()
    req  = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"}
    )
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            print(f"TG {method} HTTP {e.code}: {body}")
            return {}
        except Exception as e:
            if attempt < retries:
                time.sleep(1)
                continue
            print(f"TG {method} failed: {e}")
            return {}

def send(cid, text, kb=None, parse_mode="HTML"):
    # Telegram max 4096 chars per message
    if len(text) > 4000:
        text = text[:3997] + "..."
    p = {"chat_id": cid, "text": text, "parse_mode": parse_mode}
    if kb:
        p["reply_markup"] = json.dumps(kb)
    return _call("sendMessage", p)

def answer_cb(cb_id, text=""):
    _call("answerCallbackQuery", {"callback_query_id": cb_id, "text": text})

def edit_msg(cid, mid, text, kb=None):
    p = {"chat_id": cid, "message_id": mid, "text": text, "parse_mode": "HTML"}
    if kb:
        p["reply_markup"] = json.dumps(kb)
    _call("editMessageText", p)

# ─── Thread-safe state machine ────────────────────────
_state      = {}
_state_lock = threading.Lock()

def _get_state(cid):
    with _state_lock:
        return _state.get(cid)

def _set_state(cid, val):
    with _state_lock:
        _state[cid] = val

def _clear_state(cid):
    with _state_lock:
        _state.pop(cid, None)

# ─── Keyboards ────────────────────────────────────────
MENU_KB = {"keyboard": [
    ["📋 /orders",     "📊 /stats"],
    ["✅ /approve",    "❌ /reject"],
    ["📤 /sent",       "⏳ /pending"],
    ["🏦 /setupi",     "💰 /setrate"],
    ["⚙️ /setlimits",  "⚡ /setspeed"],
    ["📦 /setstock",   "🔴 /pause"],
    ["💳 /deposit",    "👥 /users"],
    ["📊 /dashboard",  "❓ /help"],
], "resize_keyboard": True}

def order_kb(oid):
    return {"inline_keyboard": [[
        {"text": "✅ Approve", "callback_data": f"approve:{oid}"},
        {"text": "📤 Sent",    "callback_data": f"sent:{oid}"},
        {"text": "❌ Reject",  "callback_data": f"reject:{oid}"},
    ]]}

def withdrawal_kb(oid):
    return {"inline_keyboard": [[
        {"text": "✅ Mark Sent",  "callback_data": f"sent:{oid}"},
        {"text": "❌ Reject",     "callback_data": f"reject:{oid}"},
    ]]}

def pick_kb(orders, action):
    rows = [
        [{"text": f"#{o['id']} — ${o['amount']} USDT ({o['status']})",
          "callback_data": f"{action}:{o['id']}"}]
        for o in orders[:8]
    ]
    rows.append([{"text": "🔙 Cancel", "callback_data": "cancel"}])
    return {"inline_keyboard": rows}

# ─── Formatters ───────────────────────────────────────
STATUS_ICON = {
    "pending":   "⏳", "approved": "✅",
    "completed": "✅", "sent":     "📤",
    "rejected":  "❌", "cancelled":"❌",
}

def fmt_order(o):
    s     = o.get("status", "pending")
    pay   = o.get("payment", "gc")
    otype = o.get("order_type", "p2p")
    if otype == "withdrawal":
        return (
            f"{'─'*22}\n"
            f"📤 <b>WITHDRAWAL REQUEST</b>\n"
            f"🆔 <code>{o.get('id','?')}</code>\n"
            f"💵 <b>{o.get('amount','?')} USDT</b>\n"
            f"💼 <code>{o.get('wallet','—')}</code>\n"
            f"🌐 Network: {o.get('contact','—')}\n"
            f"📞 Telegram: {o.get('gc_code','—')}\n"
            f"📅 {o.get('order_date','?')} {o.get('order_time','')}\n"
            f"{STATUS_ICON.get(s,'?')} <b>{s.upper()}</b>\n"
            f"{'─'*22}"
        )
    detail = o.get("upi_utr", o.get("utr","")) if pay=="upi" else o.get("gc_code","")
    return (
        f"{'─'*22}\n"
        f"🆔 <code>{o.get('id','?')}</code>\n"
        f"💵 <b>${o.get('amount','?')} USDT</b> @ ₹{o.get('rate','?')}\n"
        f"💰 INR: {o.get('inr_amount','?')}\n"
        f"👤 {o.get('seller','?')}\n"
        f"💼 <code>{o.get('wallet','—')}</code>\n"
        f"{'🏦 UTR' if pay=='upi' else '🎁 GC'}: <code>{detail or '—'}</code>\n"
        f"📞 {o.get('contact','—')}\n"
        f"📅 {o.get('order_date','?')} {o.get('order_time','')}\n"
        f"{STATUS_ICON.get(s,'?')} <b>{s.upper()}</b>\n"
        f"{'─'*22}"
    )

def do_update(cid, oid, status):
    found = update_order_status(oid, status)
    labels = {
        "approved":  "✅ APPROVED",
        "rejected":  "❌ REJECTED",
        "sent":      "📤 USDT SENT",
        "pending":   "⏳ RESET TO PENDING",
        "cancelled": "❌ CANCELLED",
    }
    if found:
        send(cid,
            f"{labels.get(status, status.upper())}!\n\n"
            f"🆔 <code>{found['id']}</code>\n"
            f"💵 ${found['amount']} USDT\n"
            f"💼 <code>{found.get('wallet','—')}</code>",
            MENU_KB)
    else:
        send(cid, f"❌ Order <code>{oid}</code> not found!", MENU_KB)

def _is_admin(cid):
    return int(cid) in ADMIN_IDS

# ─── Command handlers ─────────────────────────────────
def cmd_start(cid):
    cfg   = get_config()
    stats = get_stats()
    send(cid,
        f"🤖 <b>PallaPay Admin Bot</b>\n{'═'*24}\n\n"
        f"<b>Quick Status</b>\n"
        f"{'🟢 LIVE' if cfg.get('site_active') else '🔴 PAUSED'} | "
        f"Rate: ₹{cfg.get('rate',84.60)} | "
        f"UPI: <code>{cfg.get('upi_id','—')}</code>\n"
        f"⏳ Pending: <b>{stats.get('pending',0)}</b>\n\n"
        f"<b>Commands</b>\n"
        f"/orders — View orders\n"
        f"/approve ID — Approve order\n"
        f"/reject ID — Reject order\n"
        f"/sent ID — Mark USDT sent\n"
        f"/setupi — Change UPI ID\n"
        f"/setrate — Change rate\n"
        f"/setmarkup — INR markup\n"
        f"/setstock — USDT stock\n"
        f"/setlimits MIN MAX — Limits\n"
        f"/setspeed — Avg minutes\n"
        f"/setaed — AED rate\n"
        f"/pause — Toggle P2P on/off\n"
        f"/deposit UID AMOUNT — Add USDT\n"
        f"/users — All users\n"
        f"/stats — Statistics\n"
        f"/dashboard — Full dashboard",
        MENU_KB)

def cmd_orders(cid, args):
    status_filter = args[0] if args else None
    orders = get_orders(status=status_filter, limit=15)
    if not orders:
        send(cid, "📋 No orders found.", MENU_KB)
        return
    send(cid, f"📋 <b>{len(orders)} Orders</b>", MENU_KB)
    for o in orders[:8]:
        otype = o.get("order_type", "p2p")
        kb = None
        if o.get("status") == "pending":
            kb = withdrawal_kb(o['id']) if otype == "withdrawal" else order_kb(o['id'])
        send(cid, fmt_order(o), kb)

def cmd_stats(cid):
    s   = get_stats()
    cfg = get_config()
    send(cid,
        f"📊 <b>PallaPay Statistics</b>\n{'═'*24}\n\n"
        f"<b>Orders</b>\n"
        f"Total:       {s.get('total',0)}\n"
        f"✅ Approved: {s.get('approved',0)}\n"
        f"📤 Sent:     {s.get('sent',0)}\n"
        f"⏳ Pending:  {s.get('pending',0)}\n"
        f"❌ Rejected: {s.get('rejected',0)}\n\n"
        f"<b>Volume</b>\n"
        f"Total USDT: {float(s.get('total_usdt',0)):.2f}\n"
        f"Today: {s.get('today_count',0)} orders | "
        f"{float(s.get('today_usdt',0)):.1f} USDT\n\n"
        f"<b>Config</b>\n"
        f"Rate: ₹{cfg.get('rate',84.60)}\n"
        f"UPI: <code>{cfg.get('upi_id','—')}</code>\n"
        f"Stock: {cfg.get('avail_usdt',500)} USDT\n"
        f"Limits: ${cfg.get('min_order',50)}–${cfg.get('max_order',200)}\n"
        f"Status: {'🟢 LIVE' if cfg.get('site_active') else '🔴 PAUSED'}",
        MENU_KB)

def cmd_setupi(cid, args):
    if not args:
        cfg = get_config()
        _set_state(cid, "awaiting_upi")
        send(cid,
            f"🏦 <b>Change UPI ID</b>\n"
            f"Current: <code>{cfg.get('upi_id','—')}</code>\n\n"
            f"Enter new UPI ID:")
        return
    upi = args[0].strip()
    if len(upi) >= 5 and "@" in upi:
        set_config("upi_id", upi)
        send(cid, f"✅ <b>UPI Updated!</b>\nNew: <code>{upi}</code>", MENU_KB)
    else:
        send(cid, "❌ Invalid UPI. e.g. <code>name@paytm</code>", MENU_KB)

def cmd_setrate(cid, args):
    if not args:
        cfg = get_config()
        _set_state(cid, "awaiting_rate")
        send(cid,
            f"💰 <b>Change USDT Rate</b>\n"
            f"Current: ₹{cfg.get('rate',84.60)}\n\n"
            f"Enter new rate: <code>85.50</code>")
        return
    try:
        v = float(args[0])
        assert 70 <= v <= 200
        set_config("rate", v)
        set_config("inr_markup", 0)
        send(cid, f"✅ <b>Rate → ₹{v}</b> per USDT", MENU_KB)
    except:
        send(cid, "❌ Invalid. Range: 70–200. e.g. <code>85.50</code>", MENU_KB)

def cmd_setmarkup(cid, args):
    if not args:
        cfg = get_config()
        _set_state(cid, "awaiting_markup")
        send(cid,
            f"💱 <b>INR Markup</b>\n"
            f"Current: +₹{cfg.get('inr_markup',0.1)}\n\n"
            f"Enter: <code>0.10</code>  <code>0.50</code>")
        return
    try:
        v = float(args[0])
        assert 0 <= v <= 20
        set_config("inr_markup", v)
        send(cid, f"✅ Markup → +₹{v}", MENU_KB)
    except:
        send(cid, "❌ e.g. <code>0.10</code>", MENU_KB)

def cmd_setstock(cid, args):
    if not args:
        cfg = get_config()
        _set_state(cid, "awaiting_stock")
        send(cid,
            f"📦 <b>USDT Stock</b>\n"
            f"Current: {cfg.get('avail_usdt',500)} USDT\n\n"
            f"Enter: <code>500</code>")
        return
    try:
        v = int(args[0])
        assert v >= 0
        set_config("avail_usdt", v)
        send(cid, f"✅ Stock → {v} USDT", MENU_KB)
    except:
        send(cid, "❌ e.g. <code>500</code>", MENU_KB)

def cmd_setlimits(cid, args):
    if len(args) < 2:
        cfg = get_config()
        _set_state(cid, "awaiting_limits")
        send(cid,
            f"⚙️ <b>Order Limits</b>\n"
            f"Current: ${cfg.get('min_order',50)}–${cfg.get('max_order',200)}\n\n"
            f"Send: <code>MIN MAX</code> e.g. <code>50 500</code>")
        return
    try:
        mn, mx = int(args[0]), int(args[1])
        assert mn >= 10 and mn < mx
        set_config("min_order", mn)
        set_config("max_order", mx)
        send(cid, f"✅ Limits → ${mn}–${mx}", MENU_KB)
    except:
        send(cid, "❌ e.g. <code>50 500</code>", MENU_KB)

def cmd_setspeed(cid, args):
    if not args:
        cfg = get_config()
        _set_state(cid, "awaiting_speed")
        send(cid,
            f"⚡ <b>Avg Speed</b>\n"
            f"Current: {cfg.get('speed_min',10)} min\n\n"
            f"Enter minutes: <code>5</code>  <code>15</code>")
        return
    try:
        v = int(args[0])
        assert 1 <= v <= 120
        set_config("speed_min", v)
        send(cid, f"✅ Speed → {v} min avg", MENU_KB)
    except:
        send(cid, "❌ Enter 1–120", MENU_KB)

def cmd_setaed(cid, args):
    """Set AED/USD rate"""
    if not args:
        cfg = get_config()
        _set_state(cid, "awaiting_aed")
        send(cid,
            f"🇦🇪 <b>AED Rate</b>\n"
            f"Current: {cfg.get('aed_rate',3.685)}\n\n"
            f"Enter AED per USD: <code>3.685</code>")
        return
    try:
        v = float(args[0])
        assert 3.0 <= v <= 5.0
        set_config("aed_rate", v)
        send(cid, f"✅ AED Rate → {v}", MENU_KB)
    except:
        send(cid, "❌ Enter valid rate e.g. <code>3.685</code>", MENU_KB)

def cmd_pause(cid):
    cfg     = get_config()
    new_val = not cfg.get("site_active", True)
    set_config("site_active", new_val)
    if new_val:
        send(cid, "🟢 <b>P2P is LIVE!</b>", MENU_KB)
    else:
        send(cid, "🔴 <b>P2P PAUSED!</b>", MENU_KB)

def cmd_approve(cid, args):
    if not args:
        pending = get_orders("pending")
        if not pending:
            send(cid, "✅ No pending orders!", MENU_KB)
            return
        _set_state(cid, "awaiting_approve_id")
        send(cid, "✅ Tap to approve:", pick_kb(pending, "approve"))
        return
    do_update(cid, args[0].upper().replace("#",""), "approved")

def cmd_reject(cid, args):
    if not args:
        pending = get_orders("pending")
        if not pending:
            send(cid, "No pending orders!", MENU_KB)
            return
        _set_state(cid, "awaiting_reject_id")
        send(cid, "❌ Tap to reject:", pick_kb(pending, "reject"))
        return
    do_update(cid, args[0].upper().replace("#",""), "rejected")

def cmd_sent(cid, args):
    if not args:
        orders = get_orders()
        act = [o for o in orders if o.get("status") in ("pending","approved")]
        if not act:
            send(cid, "Nothing to mark as sent!", MENU_KB)
            return
        _set_state(cid, "awaiting_sent_id")
        send(cid, "📤 Tap order to mark sent:", pick_kb(act, "sent"))
        return
    do_update(cid, args[0].upper().replace("#",""), "sent")

def cmd_pending(cid):
    orders = get_orders("pending")
    if not orders:
        send(cid, "✅ No pending orders! 🎉", MENU_KB)
        return
    send(cid, f"⏳ <b>{len(orders)} Pending</b>", MENU_KB)
    for o in orders[:5]:
        otype = o.get("order_type", "p2p")
        kb = withdrawal_kb(o["id"]) if otype == "withdrawal" else order_kb(o["id"])
        send(cid, fmt_order(o), kb)

def cmd_deposit(cid, args):
    if len(args) < 2:
        _set_state(cid, "awaiting_deposit")
        send(cid,
            "💳 <b>Deposit USDT to User</b>\n\n"
            "Format: <code>UID AMOUNT note</code>\n\n"
            "Example:\n"
            "<code>user@gmail.com 100 P2P bonus</code>\n"
            "<code>firebaseUID123 50 Admin credit</code>\n\n"
            "Send UID + Amount now:")
        return
    uid = args[0].strip()
    try:
        amount = float(args[1])
        assert amount > 0
    except:
        send(cid, "❌ Invalid amount.", MENU_KB)
        return
    note   = " ".join(args[2:]) if len(args) > 2 else "Admin credit"
    result = admin_deposit_usdt(uid, amount, note)
    if result:
        new_bal = get_balance(uid)
        send(cid,
            f"✅ <b>USDT Deposited!</b>\n\n"
            f"👤 UID: <code>{uid}</code>\n"
            f"💵 Added: <b>{amount} USDT</b>\n"
            f"📝 Note: {note}\n"
            f"💰 New Balance: <b>{new_bal:.2f} USDT</b>",
            MENU_KB)
    else:
        send(cid, "❌ Failed. Check UID and try again.", MENU_KB)

def cmd_users(cid):
    users = get_all_users(20)
    if not users:
        send(cid, "👥 No users yet.", MENU_KB)
        return
    lines = [f"👥 <b>{len(users)} Users</b>\n{'─'*22}"]
    for u in users[:15]:
        bal  = float(u.get('total_balance') or 0)
        name = u.get('display_name') or u.get('email') or u.get('uid','?')
        lines.append(
            f"👤 {name[:20]}\n"
            f"   💰 {bal:.2f} USDT\n"
            f"   🆔 <code>{u.get('uid','?')}</code>"
        )
    lines.append("\n💡 Use full UID above with /deposit")
    send(cid, "\n".join(lines), MENU_KB)

def cmd_dashboard(cid):
    s       = get_stats()
    cfg     = get_config()
    pending = get_orders("pending")
    send(cid,
        f"📊 <b>Full Dashboard</b>\n{'═'*24}\n"
        f"{'🟢 LIVE' if cfg.get('site_active') else '🔴 PAUSED'}\n\n"
        f"<b>Settings</b>\n"
        f"📍 UPI: <code>{cfg.get('upi_id','—')}</code>\n"
        f"💰 Rate: ₹{cfg.get('rate',84.60)}\n"
        f"💱 Markup: +₹{cfg.get('inr_markup',0.1)}\n"
        f"📦 Stock: {cfg.get('avail_usdt',500)} USDT\n"
        f"⚙️ Limits: ${cfg.get('min_order',50)}–${cfg.get('max_order',200)}\n"
        f"⚡ Speed: {cfg.get('speed_min',10)} min\n"
        f"🇦🇪 AED: {cfg.get('aed_rate',3.685)}\n\n"
        f"<b>Orders</b>\n"
        f"Total: {s.get('total',0)} | "
        f"✅{s.get('approved',0)} | "
        f"📤{s.get('sent',0)} | "
        f"⏳{s.get('pending',0)} | "
        f"❌{s.get('rejected',0)}\n"
        f"💰 USDT Sold: {float(s.get('total_usdt',0)):.2f}\n"
        f"📅 Today: {s.get('today_count',0)} | "
        f"{float(s.get('today_usdt',0)):.1f} USDT\n\n"
        f"⏳ Pending: <b>{len(pending)}</b>",
        MENU_KB)
    for o in pending[:3]:
        otype = o.get("order_type","p2p")
        kb = withdrawal_kb(o["id"]) if otype == "withdrawal" else order_kb(o["id"])
        send(cid, fmt_order(o), kb)

# ─── Message router ───────────────────────────────────
def handle_message(msg):
    cid = msg.get("chat", {}).get("id")
    txt = msg.get("text", "").strip()

    if not _is_admin(cid):
        send(cid, "⛔ <b>Unauthorized</b>\nContact @PallaPayOfficial")
        return

    st = _get_state(cid)

    # ── State inputs ──────────────────────────────────
    if st == "awaiting_upi":
        upi = txt.strip()
        if len(upi) >= 5 and "@" in upi:
            set_config("upi_id", upi)
            _clear_state(cid)
            send(cid, f"✅ UPI → <code>{upi}</code>", MENU_KB)
        else:
            send(cid, "❌ Invalid. e.g. <code>name@paytm</code>")
        return

    if st == "awaiting_rate":
        try:
            v = float(txt)
            assert 70 <= v <= 200
            set_config("rate", v)
            _clear_state(cid)
            send(cid, f"✅ Rate → ₹{v}", MENU_KB)
        except:
            send(cid, "❌ Invalid. Range: 70–200.")
        return

    if st == "awaiting_markup":
        try:
            v = float(txt)
            assert 0 <= v <= 20
            set_config("inr_markup", v)
            _clear_state(cid)
            send(cid, f"✅ Markup → +₹{v}", MENU_KB)
        except:
            send(cid, "❌ e.g. <code>0.10</code>")
        return

    if st == "awaiting_stock":
        try:
            v = int(txt)
            assert v >= 0
            set_config("avail_usdt", v)
            _clear_state(cid)
            send(cid, f"✅ Stock → {v} USDT", MENU_KB)
        except:
            send(cid, "❌ e.g. <code>500</code>")
        return

    if st == "awaiting_limits":
        try:
            parts = txt.split()
            mn, mx = int(parts[0]), int(parts[1])
            assert mn >= 10 and mn < mx
            set_config("min_order", mn)
            set_config("max_order", mx)
            _clear_state(cid)
            send(cid, f"✅ Limits → ${mn}–${mx}", MENU_KB)
        except:
            send(cid, "❌ e.g. <code>50 500</code>")
        return

    if st == "awaiting_speed":
        try:
            v = int(txt)
            assert 1 <= v <= 120
            set_config("speed_min", v)
            _clear_state(cid)
            send(cid, f"✅ Speed → {v} min", MENU_KB)
        except:
            send(cid, "❌ Enter 1–120")
        return

    if st == "awaiting_aed":
        try:
            v = float(txt)
            assert 3.0 <= v <= 5.0
            set_config("aed_rate", v)
            _clear_state(cid)
            send(cid, f"✅ AED Rate → {v}", MENU_KB)
        except:
            send(cid, "❌ e.g. <code>3.685</code>")
        return

    if st == "awaiting_deposit":
        parts = txt.split()
        if len(parts) >= 2:
            cmd_deposit(cid, parts)
            _clear_state(cid)
        else:
            send(cid, "❌ Format: <code>UID AMOUNT note</code>")
        return

    if st == "awaiting_approve_id":
        do_update(cid, txt.upper().replace("#",""), "approved")
        _clear_state(cid)
        return

    if st == "awaiting_reject_id":
        do_update(cid, txt.upper().replace("#",""), "rejected")
        _clear_state(cid)
        return

    if st == "awaiting_sent_id":
        do_update(cid, txt.upper().replace("#",""), "sent")
        _clear_state(cid)
        return

    # ── Command parsing ───────────────────────────────
    parts   = txt.split()
    cmd_raw = parts[0].lower().split("@")[0]
    args    = parts[1:]

    cmds = {
        "/start":     lambda: cmd_start(cid),
        "/menu":      lambda: cmd_start(cid),
        "/help":      lambda: cmd_start(cid),
        "/orders":    lambda: cmd_orders(cid, args),
        "/stats":     lambda: cmd_stats(cid),
        "/approve":   lambda: cmd_approve(cid, args),
        "/reject":    lambda: cmd_reject(cid, args),
        "/sent":      lambda: cmd_sent(cid, args),
        "/pending":   lambda: cmd_pending(cid),
        "/setupi":    lambda: cmd_setupi(cid, args),
        "/setrate":   lambda: cmd_setrate(cid, args),
        "/setmarkup": lambda: cmd_setmarkup(cid, args),
        "/setstock":  lambda: cmd_setstock(cid, args),
        "/setlimits": lambda: cmd_setlimits(cid, args),
        "/setspeed":  lambda: cmd_setspeed(cid, args),
        "/setaed":    lambda: cmd_setaed(cid, args),
        "/pause":     lambda: cmd_pause(cid),
        "/deposit":   lambda: cmd_deposit(cid, args),
        "/users":     lambda: cmd_users(cid),
        "/dashboard": lambda: cmd_dashboard(cid),
    }

    fn = cmds.get(cmd_raw)
    if fn:
        fn()
    else:
        send(cid, "❓ Unknown command.\nSend /start for help.", MENU_KB)

def handle_callback(cb):
    cid   = cb["from"]["id"]
    data  = cb.get("data", "")
    cb_id = cb["id"]

    if not _is_admin(cid):
        answer_cb(cb_id, "⛔ Unauthorized")
        return

    if data == "cancel":
        _clear_state(cid)
        answer_cb(cb_id, "Cancelled")
        return

    if ":" in data:
        action, oid = data.split(":", 1)
        action_map = {
            "approve": "approved",
            "reject":  "rejected",
            "sent":    "sent",
            "pending": "pending",
        }
        if action in action_map:
            answer_cb(cb_id, "Processing...")
            _clear_state(cid)
            do_update(cid, oid, action_map[action])
        else:
            answer_cb(cb_id, "Unknown action")

# ─── Alerts ───────────────────────────────────────────
def send_new_order_alert(order):
    pay = order.get("payment","gc")
    det = order.get("upi_utr", order.get("utr","")) if pay=="upi" else order.get("gc_code","")
    oid = order.get("id","?")
    text = (
        f"🔔 <b>NEW P2P ORDER!</b>\n"
        f"{'━'*24}\n"
        f"🆔 <code>{oid}</code>\n"
        f"💵 <b>${order.get('amount', order.get('usdt','?'))} USDT</b>\n"
        f"💱 Rate: ₹{order.get('rate','?')}\n"
        f"💰 INR: {order.get('inr_amount', order.get('inr','?'))}\n"
        f"💳 Payment: {'🏦 UPI' if pay=='upi' else '🎁 GC'}\n"
        f"👤 {order.get('seller','?')}\n"
        f"💼 <code>{order.get('wallet','—')}</code>\n"
        f"{'🏦 UTR' if pay=='upi' else '🎁 GC'}: <code>{det or '—'}</code>\n"
        f"📞 {order.get('contact','—')}\n"
        f"{'━'*24}"
    )
    for admin_id in ADMIN_IDS:
        _call("sendMessage", {
            "chat_id":      admin_id,
            "text":         text,
            "parse_mode":   "HTML",
            "reply_markup": json.dumps(order_kb(oid)),
        })

def send_withdrawal_alert(order):
    oid = order.get("id","?")
    text = (
        f"📤 <b>WITHDRAWAL REQUEST!</b>\n"
        f"{'━'*24}\n"
        f"🆔 <code>{oid}</code>\n"
        f"💵 <b>{order.get('amount','?')} USDT</b>\n"
        f"💼 <code>{order.get('wallet','—')}</code>\n"
        f"🌐 Network: {order.get('network', order.get('contact','—'))}\n"
        f"📞 Telegram: {order.get('telegram', order.get('gc_code','—'))}\n"
        f"👤 User: {order.get('user_id','—')}\n"
        f"{'━'*24}\n"
        f"Send USDT to above wallet, then tap Sent ✅"
    )
    for admin_id in ADMIN_IDS:
        _call("sendMessage", {
            "chat_id":      admin_id,
            "text":         text,
            "parse_mode":   "HTML",
            "reply_markup": json.dumps(withdrawal_kb(oid)),
        })

# ─── Polling loop ─────────────────────────────────────
def run():
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN not set — bot disabled")
        return
    if not ADMIN_IDS:
        print("⚠️  ADMIN_IDS not set — no admins configured")

    print(f"🤖 Bot started (admins: {ADMIN_IDS})")
    offset      = 0
    backoff     = 1   # seconds, doubles on error up to 30s

    while True:
        try:
            r = _call("getUpdates", {
                "offset":  offset,
                "timeout": 30,
                "allowed_updates": ["message", "callback_query"],
            })
            updates = r.get("result", [])
            backoff = 1  # reset on success

            for upd in updates:
                offset = upd["update_id"] + 1
                try:
                    if "message" in upd:
                        handle_message(upd["message"])
                    elif "callback_query" in upd:
                        handle_callback(upd["callback_query"])
                except Exception as e:
                    print(f"Handler error: {e}")

        except Exception as e:
            print(f"Poll error: {e} — retry in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
