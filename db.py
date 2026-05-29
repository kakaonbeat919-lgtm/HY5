"""
db.py - PallaPay PostgreSQL Database Layer
Tables: config, orders, users (balances + deposits)

v5 improvements:
- ThreadedConnectionPool — ek hi pool, har request pe naya conn nahi
- DB indexes for speed (status, user_email, created_at)
- Better error logging with traceback
- Pool auto-reconnect on stale connection
"""
import os, json, traceback
import psycopg2, psycopg2.extras, psycopg2.pool
from contextlib import contextmanager

DATABASE_URL = os.environ.get("DATABASE_URL", "")

DEFAULT_CONFIG = {
    "upi_id":      "nextgen@upi",
    "rate":        84.60,
    "inr_markup":  0.10,
    "avail_usdt":  500,
    "min_order":   50,
    "max_order":   200,
    "speed_min":   10,
    "aed_rate":    3.685,
    "tg_handle":   "@PallaPayOfficial",
    "site_active": True,
}

# ── Connection pool ───────────────────────────────────
_pool = None

def _get_pool():
    global _pool
    if _pool is None or _pool.closed:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=10,
            dsn=DATABASE_URL,
            connect_timeout=10,
        )
    return _pool

@contextmanager
def cursor():
    pool = _get_pool()
    conn = pool.getconn()
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield cur
    except psycopg2.OperationalError:
        # Stale conn — reset pool so next call gets fresh
        global _pool
        _pool = None
        raise
    finally:
        cur.close()
        pool.putconn(conn)

# ── Schema ────────────────────────────────────────────
def init_db():
    with cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id          TEXT PRIMARY KEY,
                user_email  TEXT,
                seller      TEXT,
                amount      NUMERIC(12,4),
                rate        NUMERIC(12,4),
                inr_amount  TEXT,
                wallet      TEXT,
                payment     TEXT,
                upi_utr     TEXT,
                gc_code     TEXT,
                contact     TEXT,
                status      TEXT DEFAULT 'pending',
                order_type  TEXT DEFAULT 'p2p',
                order_date  TEXT,
                order_time  TEXT,
                note        TEXT,
                created_at  TIMESTAMP DEFAULT NOW(),
                updated_at  TIMESTAMP DEFAULT NOW()
            )
        """)
        # Indexes for common query patterns
        cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_user_email ON orders(user_email)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at DESC)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                uid         TEXT PRIMARY KEY,
                email       TEXT,
                display_name TEXT,
                balance     NUMERIC(12,4) DEFAULT 0,
                created_at  TIMESTAMP DEFAULT NOW(),
                updated_at  TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")
        # Seed config defaults
        for k, v in DEFAULT_CONFIG.items():
            cur.execute("""
                INSERT INTO config (key, value)
                VALUES (%s, %s) ON CONFLICT (key) DO NOTHING
            """, (k, json.dumps(v)))
    print("✅ Database ready")

# ── Config ────────────────────────────────────────────
def get_config():
    try:
        with cursor() as cur:
            cur.execute("SELECT key, value FROM config")
            rows = cur.fetchall()
        cfg = dict(DEFAULT_CONFIG)
        for r in rows:
            try:    cfg[r['key']] = json.loads(r['value'])
            except: cfg[r['key']] = r['value']
        return cfg
    except Exception as e:
        print(f"get_config error: {e}")
        return dict(DEFAULT_CONFIG)

def set_config(key, value):
    try:
        with cursor() as cur:
            cur.execute("""
                INSERT INTO config (key, value, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (key) DO UPDATE
                  SET value=EXCLUDED.value, updated_at=NOW()
            """, (key, json.dumps(value)))
        return True
    except Exception as e:
        print(f"set_config error: {e}")
        return False

# ── Orders ────────────────────────────────────────────
def get_orders(status=None, user_id=None, limit=200):
    try:
        with cursor() as cur:
            if user_id and status:
                cur.execute(
                    "SELECT * FROM orders WHERE (user_email=%s OR user_email=%s) AND status=%s ORDER BY created_at DESC LIMIT %s",
                    (user_id, user_id, status, limit))
            elif user_id:
                cur.execute(
                    "SELECT * FROM orders WHERE user_email=%s ORDER BY created_at DESC LIMIT %s",
                    (user_id, limit))
            elif status:
                cur.execute(
                    "SELECT * FROM orders WHERE status=%s ORDER BY created_at DESC LIMIT %s",
                    (status, limit))
            else:
                cur.execute(
                    "SELECT * FROM orders ORDER BY created_at DESC LIMIT %s",
                    (limit,))
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"get_orders error: {e}")
        return []

def get_order(oid):
    try:
        with cursor() as cur:
            cur.execute("SELECT * FROM orders WHERE UPPER(id)=UPPER(%s)", (oid,))
            r = cur.fetchone()
            return dict(r) if r else None
    except Exception as e:
        print(f"get_order error: {e}")
        return None

def create_order(o):
    try:
        with cursor() as cur:
            cur.execute("""
                INSERT INTO orders
                  (id, user_email, seller, amount, rate, inr_amount,
                   wallet, payment, upi_utr, gc_code, contact,
                   status, order_type, order_date, order_time, note)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s,%s,%s)
                ON CONFLICT (id) DO NOTHING
                RETURNING *
            """, (
                o.get('id'),
                o.get('user_id', o.get('user_email','')),
                o.get('seller',''),
                float(o.get('usdt', o.get('amount', 0)) or 0),
                float(o.get('rate', 0) or 0),
                o.get('inr_amount', o.get('inr','')),
                o.get('wallet',''),
                o.get('payment','gc'),
                o.get('upi_utr', o.get('utr','')),
                o.get('gc_code', o.get('gc','')),
                o.get('contact',''),
                o.get('type', o.get('order_type','p2p')),
                o.get('date', o.get('order_date','')),
                o.get('time', o.get('order_time','')),
                o.get('note',''),
            ))
            r = cur.fetchone()
            return dict(r) if r else None
    except Exception as e:
        print(f"create_order error: {e}\n{traceback.format_exc()}")
        return None

def update_order_status(oid, status):
    try:
        with cursor() as cur:
            cur.execute("""
                UPDATE orders
                SET status=%s, updated_at=NOW()
                WHERE UPPER(id)=UPPER(%s)
                RETURNING *
            """, (status, oid))
            r = cur.fetchone()
            return dict(r) if r else None
    except Exception as e:
        print(f"update_order_status error: {e}")
        return None

# ── Users ─────────────────────────────────────────────
def get_user(uid):
    try:
        with cursor() as cur:
            cur.execute("SELECT * FROM users WHERE uid=%s OR email=%s LIMIT 1", (uid, uid))
            r = cur.fetchone()
            return dict(r) if r else None
    except Exception as e:
        print(f"get_user error: {e}")
        return None

def upsert_user(uid, email="", display_name=""):
    try:
        with cursor() as cur:
            # Check if user exists by uid
            cur.execute("SELECT uid FROM users WHERE uid=%s LIMIT 1", (uid,))
            exists_by_uid = cur.fetchone()

            if not exists_by_uid and email:
                # Check for email-based entry (bot deposit before login)
                cur.execute(
                    "SELECT uid, balance FROM users WHERE email=%s AND uid!=%s LIMIT 1",
                    (email, uid))
                email_entry = cur.fetchone()
                if email_entry:
                    cur.execute(
                        "UPDATE users SET uid=%s, updated_at=NOW() WHERE email=%s AND uid=%s",
                        (uid, email, email_entry['uid']))
                    print(f"Migrated user email={email} uid {email_entry['uid']} -> {uid}")

            cur.execute("""
                INSERT INTO users (uid, email, display_name)
                VALUES (%s, %s, %s)
                ON CONFLICT (uid) DO UPDATE
                  SET email=COALESCE(NULLIF(EXCLUDED.email,''), users.email),
                      display_name=COALESCE(NULLIF(EXCLUDED.display_name,''), users.display_name),
                      updated_at=NOW()
                RETURNING *
            """, (uid, email, display_name))
            r = cur.fetchone()
            return dict(r) if r else None
    except Exception as e:
        print(f"upsert_user error: {e}")
        return None

def get_balance(uid):
    try:
        with cursor() as cur:
            cur.execute(
                "SELECT balance FROM users WHERE uid=%s OR email=%s LIMIT 1",
                (uid, uid))
            r = cur.fetchone()
            manual_bal = float(r['balance']) if r else 0.0

            cur.execute("""
                SELECT COALESCE(SUM(amount),0) as earned FROM orders
                WHERE (user_email=%s OR user_email=%s)
                  AND status IN ('approved','sent','completed')
                  AND order_type IN ('p2p','admin_credit')
            """, (uid, uid))
            r2 = cur.fetchone()
            order_bal = float(r2['earned']) if r2 else 0.0

            cur.execute("""
                SELECT COALESCE(SUM(amount),0) as withdrawn FROM orders
                WHERE (user_email=%s OR user_email=%s)
                  AND status IN ('approved','sent','completed')
                  AND order_type='withdrawal'
            """, (uid, uid))
            r3 = cur.fetchone()
            withdrawn = float(r3['withdrawn']) if r3 else 0.0

            return round(manual_bal + order_bal - withdrawn, 4)
    except Exception as e:
        print(f"get_balance error: {e}")
        return 0.0

def admin_deposit_usdt(uid, amount, note="Admin credit"):
    """Admin: add USDT directly to user balance (matches by uid OR email)"""
    try:
        with cursor() as cur:
            cur.execute(
                "SELECT uid FROM users WHERE uid=%s OR email=%s LIMIT 1",
                (uid, uid))
            existing = cur.fetchone()
            if existing:
                real_uid = existing['uid']
                cur.execute("""
                    UPDATE users
                    SET balance = balance + %s, updated_at = NOW()
                    WHERE uid = %s
                    RETURNING *
                """, (float(amount), real_uid))
            else:
                cur.execute("""
                    INSERT INTO users (uid, email, balance)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (uid) DO UPDATE
                      SET balance = users.balance + %s,
                          updated_at = NOW()
                    RETURNING *
                """, (uid, uid, float(amount), float(amount)))
            r = cur.fetchone()
            return dict(r) if r else None
    except Exception as e:
        print(f"admin_deposit_usdt error: {e}")
        return None

def get_all_users(limit=50):
    try:
        with cursor() as cur:
            cur.execute("""
                SELECT uid, email, display_name, balance, created_at,
                  COALESCE((
                    SELECT SUM(o.amount) FROM orders o
                    WHERE (o.user_email=u.uid OR o.user_email=u.email)
                      AND o.status IN ('approved','sent','completed')
                      AND o.order_type IN ('p2p','admin_credit')
                  ),0) -
                  COALESCE((
                    SELECT SUM(o.amount) FROM orders o
                    WHERE (o.user_email=u.uid OR o.user_email=u.email)
                      AND o.status IN ('approved','sent','completed')
                      AND o.order_type='withdrawal'
                  ),0) + u.balance AS total_balance
                FROM users u
                ORDER BY u.created_at DESC
                LIMIT %s
            """, (limit,))
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"get_all_users error: {e}")
        return []

def get_stats():
    try:
        with cursor() as cur:
            cur.execute("""
                SELECT
                  COUNT(*)                                    AS total,
                  COUNT(*) FILTER (WHERE status='pending')   AS pending,
                  COUNT(*) FILTER (WHERE status IN ('approved','completed')) AS approved,
                  COUNT(*) FILTER (WHERE status='sent')      AS sent,
                  COUNT(*) FILTER (WHERE status IN ('rejected','cancelled')) AS rejected,
                  COALESCE(SUM(amount),0)                    AS total_usdt,
                  COALESCE(SUM(amount) FILTER (WHERE DATE(created_at)=CURRENT_DATE),0) AS today_usdt,
                  COUNT(*) FILTER (WHERE DATE(created_at)=CURRENT_DATE) AS today_count
                FROM orders
                WHERE order_type NOT IN ('admin_credit')
            """)
            return dict(cur.fetchone())
    except Exception as e:
        print(f"get_stats error: {e}")
        return {}
