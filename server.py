#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import psycopg
from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field


ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
DATABASE_URL = os.environ.get("DATABASE_URL")
DEFAULT_SESSION_SECRET = "dev-secret-change-me"
SESSION_SECRET = os.environ.get("SESSION_SECRET")
if not SESSION_SECRET:
    SESSION_SECRET = secrets.token_urlsafe(32)
    print("WARNING: SESSION_SECRET is not set. Using a temporary secret; set it before deployment.")
elif SESSION_SECRET == DEFAULT_SESSION_SECRET:
    raise RuntimeError("SESSION_SECRET must not use the built-in development default")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"
SESSION_SECONDS = int(os.environ.get("SESSION_SECONDS", "28800"))
REPORT_TIME_ZONE = os.environ.get("REPORT_TIME_ZONE", "America/New_York")
INITIAL_MANAGER_EMAIL = os.environ.get("INITIAL_MANAGER_EMAIL") or os.environ.get("INITIAL_OWNER_EMAIL", "owner@example.com")
INITIAL_MANAGER_PASSWORD = os.environ.get("INITIAL_MANAGER_PASSWORD") or os.environ.get("INITIAL_OWNER_PASSWORD", "changeme")
INITIAL_MANAGER_NAME = os.environ.get("INITIAL_MANAGER_NAME") or os.environ.get("INITIAL_OWNER_NAME", "Manager")
COOKIE_NAME = "canteen_session"
TRUSTED_ORIGINS = {
    origin.strip().rstrip("/")
    for origin in os.environ.get("TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
}
if not COOKIE_SECURE:
    TRUSTED_ORIGINS.update({"http://127.0.0.1:5173", "http://localhost:5173"})

Role = Literal["cashier", "manager"]
ROLE_LEVEL = {"cashier": 1, "manager": 2}
LOGIN_ATTEMPTS: dict[str, list[float]] = {}
REVERSIBLE_TRANSACTION_TYPES = {"purchase", "funds_added", "funds_subtracted", "balance_set"}
# "refund" is intentionally excluded: it's a permanent end-of-season closeout record, not a
# live money-flow event to be voided through the same Undo button as purchases/balance edits.
REFUND_METHODS = {"cash", "check", "venmo", "zelle", "other"}

app = FastAPI(title="Canteen POS")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
}


def same_origin_request(request: Request) -> bool:
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return True
    parsed = urlparse(source)
    if parsed.netloc == request.headers.get("host", ""):
        return True
    source_origin = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    return source_origin in TRUSTED_ORIGINS


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    if (
        request.url.path.startswith("/api")
        and request.method in {"POST", "PUT", "PATCH", "DELETE"}
        and not same_origin_request(request)
    ):
        return JSONResponse({"detail": "Cross-site request blocked"}, status_code=403)
    response = await call_next(request)
    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    if request.url.path.startswith("/api"):
        response.headers.setdefault("Cache-Control", "no-store")
    if COOKIE_SECURE:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


def now() -> datetime:
    return datetime.now(timezone.utc)


def uid() -> str:
    return str(uuid.uuid4())


def money(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise HTTPException(400, "Invalid money amount")


def as_float(value: Any) -> float:
    return float(money(value))


def nullable_money(value: Any) -> Decimal | None:
    if value is None:
        return None
    return money(value)


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is required")
    return psycopg.connect(DATABASE_URL, autocommit=False)


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260_000)
    return f"pbkdf2_sha256${salt}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt, digest = stored.split("$", 2)
        if algo != "pbkdf2_sha256":
            return False
        return hmac.compare_digest(hash_password(password, salt), stored)
    except ValueError:
        return False


def sign_session(user_id: str, expires_at: int) -> str:
    payload = f"{user_id}:{expires_at}"
    sig = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).digest()
    return (
        base64.urlsafe_b64encode(payload.encode()).decode()
        + "."
        + base64.urlsafe_b64encode(sig).decode()
    )


def parse_session(token: str | None) -> str | None:
    if not token:
        return None
    try:
        raw_payload, raw_sig = token.split(".", 1)
        payload = base64.urlsafe_b64decode(raw_payload.encode()).decode()
        expected = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).digest()
        actual = base64.urlsafe_b64decode(raw_sig.encode())
        if not hmac.compare_digest(expected, actual):
            return None
        user_id, expires = payload.split(":", 1)
        if int(expires) <= int(time.time()):
            return None
        return user_id
    except Exception:
        return None


def row_dict(cur, row) -> dict[str, Any]:
    return {desc.name: row[idx] for idx, desc in enumerate(cur.description)}


def user_public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"],
        "role": row["role"],
        "active": row["active"],
        "createdAt": row["created_at"].isoformat(),
        "updatedAt": row["updated_at"].isoformat(),
    }


def require_role(user: dict[str, Any], minimum: Role) -> None:
    if ROLE_LEVEL[user["role"]] < ROLE_LEVEL[minimum]:
        raise HTTPException(403, f"{minimum} permission required")


def required_text(value: str, label: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise HTTPException(400, f"{label} is required")
    return cleaned


def parse_date(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return now()


def safe_dist_file(path: str) -> Path | None:
    if not path:
        return None
    dist_root = DIST.resolve()
    target = (dist_root / path).resolve()
    if not target.is_file():
        return None
    try:
        target.relative_to(dist_root)
    except ValueError:
        return None
    return target


def current_user(canteen_session: str | None = Cookie(default=None)) -> dict[str, Any]:
    user_id = parse_session(canteen_session)
    if not user_id:
        raise HTTPException(401, "Authentication required")
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE id = %s AND active = TRUE", (user_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(401, "Authentication required")
            return row_dict(cur, row)


def manager_user(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    require_role(user, "manager")
    return user


def delete_duplicate_named_rows(cur, table: str) -> None:
    if table not in {"accounts", "items"}:
        raise ValueError("Unsupported duplicate cleanup table")
    cur.execute(
        f"""
        DELETE FROM {table}
        WHERE id IN (
          SELECT id FROM (
            SELECT
              id,
              row_number() OVER (
                PARTITION BY lower(name)
                ORDER BY created_at ASC, id ASC
              ) AS duplicate_rank
            FROM {table}
          ) ranked
          WHERE duplicate_rank > 1
        )
        """
    )


def ensure_unique_name(cur, table: str, name: str, current_id: str | None = None) -> None:
    if table not in {"accounts", "items"}:
        raise ValueError("Unsupported unique-name table")
    label = "Account" if table == "accounts" else "Product"
    params: list[Any] = [name]
    extra = ""
    if current_id is not None:
        extra = "AND id <> %s"
        params.append(current_id)
    cur.execute(f"SELECT id FROM {table} WHERE lower(name)=lower(%s) {extra} LIMIT 1", params)
    if cur.fetchone():
        raise HTTPException(400, f"{label} name already exists")


def insert_account(cur, account_name: str, balance: Decimal, user: dict[str, Any], note: str = "") -> str:
    account_id = uid()
    txn_id = uid()
    cur.execute(
        "INSERT INTO accounts (id,name,balance,status,note) VALUES (%s,%s,%s,'active',%s)",
        (account_id, account_name, balance, note.strip()),
    )
    cur.execute(
        """
        INSERT INTO transactions
          (id,type,account_id,account_name,amount,balance_before,balance_after,note,
           actor_user_id,actor_name,actor_role)
        VALUES (%s,'account_created',%s,%s,%s,0,%s,'Account created',%s,%s,%s)
        """,
        (txn_id, account_id, account_name, balance, balance, user["id"], user["name"], user["role"]),
    )
    return account_id


def init_db() -> None:
    if COOKIE_SECURE:
        if INITIAL_MANAGER_PASSWORD == "changeme":
            raise RuntimeError("Set a strong INITIAL_MANAGER_PASSWORD before running with COOKIE_SECURE=1")
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                  id TEXT PRIMARY KEY,
                  email TEXT NOT NULL UNIQUE,
                  name TEXT NOT NULL,
                  password_hash TEXT NOT NULL,
                  role TEXT NOT NULL CHECK (role IN ('cashier','manager')),
                  active BOOLEAN NOT NULL DEFAULT TRUE,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                CREATE TABLE IF NOT EXISTS accounts (
                  id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  balance NUMERIC(12,2) NOT NULL DEFAULT 0,
                  status TEXT NOT NULL DEFAULT 'active',
                  note TEXT NOT NULL DEFAULT '',
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                CREATE TABLE IF NOT EXISTS items (
                  id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  category TEXT NOT NULL DEFAULT 'General',
                  sku TEXT NOT NULL DEFAULT '',
                  price NUMERIC(12,2) NOT NULL DEFAULT 0,
                  quantity INTEGER NOT NULL DEFAULT 0,
                  low_stock_at INTEGER NOT NULL DEFAULT 0,
                  active BOOLEAN NOT NULL DEFAULT TRUE,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                CREATE TABLE IF NOT EXISTS transactions (
                  id TEXT PRIMARY KEY,
                  date TIMESTAMPTZ NOT NULL DEFAULT now(),
                  type TEXT NOT NULL,
                  account_id TEXT REFERENCES accounts(id) ON DELETE SET NULL,
                  account_name TEXT NOT NULL DEFAULT '',
                  amount NUMERIC(12,2) NOT NULL DEFAULT 0,
                  balance_before NUMERIC(12,2),
                  balance_after NUMERIC(12,2),
                  note TEXT NOT NULL DEFAULT '',
                  actor_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                  actor_name TEXT NOT NULL,
                  actor_role TEXT NOT NULL,
                  details_json JSONB
                );
                CREATE TABLE IF NOT EXISTS sale_lines (
                  id TEXT PRIMARY KEY,
                  transaction_id TEXT NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
                  item_id TEXT REFERENCES items(id) ON DELETE SET NULL,
                  item_name TEXT NOT NULL,
                  quantity INTEGER NOT NULL,
                  unit_price NUMERIC(12,2) NOT NULL,
                  line_total NUMERIC(12,2) NOT NULL,
                  stock_before INTEGER NOT NULL,
                  stock_after INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_accounts_name ON accounts(name);
                CREATE INDEX IF NOT EXISTS idx_items_name ON items(name);
                CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(date);
                CREATE INDEX IF NOT EXISTS idx_transactions_actor ON transactions(actor_user_id);
                """
            )
            cur.execute("UPDATE users SET role='manager', updated_at=now() WHERE role='owner'")
            cur.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS note TEXT NOT NULL DEFAULT ''")
            cur.execute(
                """
                DO $$
                BEGIN
                  IF EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'users_role_check'
                      AND conrelid = 'users'::regclass
                  ) THEN
                    ALTER TABLE users DROP CONSTRAINT users_role_check;
                  END IF;
                END $$;
                """
            )
            cur.execute("ALTER TABLE users ADD CONSTRAINT users_role_check CHECK (role IN ('cashier','manager'))")
            delete_duplicate_named_rows(cur, "accounts")
            delete_duplicate_named_rows(cur, "items")
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_name_unique ON accounts (lower(name))")
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_items_name_unique ON items (lower(name))")
            cur.execute("SELECT count(*) FROM users")
            if cur.fetchone()[0] == 0:
                if INITIAL_MANAGER_PASSWORD == "changeme":
                    raise RuntimeError("Set INITIAL_MANAGER_PASSWORD before first startup; the default password is not allowed")
                cur.execute(
                    """
                    INSERT INTO users (id, email, name, password_hash, role)
                    VALUES (%s, %s, %s, %s, 'manager')
                    """,
                    (
                        uid(),
                        INITIAL_MANAGER_EMAIL.lower(),
                        INITIAL_MANAGER_NAME,
                        hash_password(INITIAL_MANAGER_PASSWORD),
                    ),
                )
        conn.commit()


@app.on_event("startup")
def startup() -> None:
    init_db()


class LoginIn(BaseModel):
    email: str
    password: str


class AccountIn(BaseModel):
    name: str = Field(min_length=1)
    openingBalance: float = 0
    note: str = ""


class BulkAccountsIn(BaseModel):
    names: list[str] = Field(min_length=1)
    openingBalance: float = 0
    note: str = ""


class AccountPatch(BaseModel):
    name: str | None = None
    note: str | None = None


class FundsIn(BaseModel):
    amount: float
    note: str = Field(min_length=1)


class SetBalanceIn(BaseModel):
    balance: float
    note: str = Field(min_length=1)


class RefundIn(BaseModel):
    method: str
    note: str = Field(min_length=1)


class ItemIn(BaseModel):
    name: str = Field(min_length=1)
    category: str = "General"
    sku: str = ""
    price: float = 0
    quantity: int = 0
    lowStockAt: int = 0
    active: bool = True


class SaleLineIn(BaseModel):
    itemId: str
    quantity: int = Field(gt=0)


class SaleIn(BaseModel):
    accountId: str
    lines: list[SaleLineIn]
    note: str = ""
    allowNegative: bool = False


class UndoTransactionIn(BaseModel):
    note: str = ""


class UserIn(BaseModel):
    email: str
    name: str
    password: str = Field(min_length=8)
    role: Role


class UserPatch(BaseModel):
    name: str | None = None
    password: str | None = Field(default=None, min_length=8)
    role: Role | None = None
    active: bool | None = None


def account_json(row) -> dict[str, Any]:
    return {
        "id": row[0],
        "name": row[1],
        "balance": as_float(row[2]),
        "status": row[3],
        "note": row[4],
        "createdAt": row[5].isoformat(),
        "updatedAt": row[6].isoformat(),
    }


def item_json(row) -> dict[str, Any]:
    return {
        "id": row[0],
        "name": row[1],
        "category": row[2],
        "sku": row[3],
        "price": as_float(row[4]),
        "quantity": row[5],
        "lowStockAt": row[6],
        "active": row[7],
        "createdAt": row[8].isoformat(),
        "updatedAt": row[9].isoformat(),
    }


def transaction_json(row, lines: list[dict[str, Any]]) -> dict[str, Any]:
    details = row[12] or {}
    if not isinstance(details, dict):
        details = {}
    undone = bool(details.get("undoneAt"))
    undo_of = bool(details.get("undoOf"))
    return {
        "id": row[0],
        "date": row[1].isoformat(),
        "type": row[2],
        "accountId": row[3],
        "accountName": row[4],
        "amount": as_float(row[5]),
        "balanceBefore": None if row[6] is None else as_float(row[6]),
        "balanceAfter": None if row[7] is None else as_float(row[7]),
        "note": row[8],
        "actorUserId": row[9],
        "actorName": row[10],
        "actorRole": row[11],
        "details": details,
        "undone": undone,
        "canUndo": row[2] in REVERSIBLE_TRANSACTION_TYPES and not undone and not undo_of,
        "items": lines,
    }


def load_state(include_ledger: bool) -> dict[str, Any]:
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,name,balance,status,note,created_at,updated_at FROM accounts ORDER BY name")
            accounts = [account_json(row) for row in cur.fetchall()]
            cur.execute(
                "SELECT id,name,category,sku,price,quantity,low_stock_at,active,created_at,updated_at FROM items ORDER BY name"
            )
            items = [item_json(row) for row in cur.fetchall()]
            transactions: list[dict[str, Any]] = []
            if include_ledger:
                cur.execute(
                    """
                    SELECT id,date,type,account_id,account_name,amount,balance_before,balance_after,
                           note,actor_user_id,actor_name,actor_role,details_json
                    FROM transactions
                    ORDER BY date DESC
                    LIMIT 1000
                    """
                )
                txn_rows = cur.fetchall()
                txn_ids = [row[0] for row in txn_rows]
                lines_by_txn: dict[str, list[dict[str, Any]]] = {txn_id: [] for txn_id in txn_ids}
                if txn_ids:
                    cur.execute(
                        """
                        SELECT transaction_id,item_id,item_name,quantity,unit_price,line_total,stock_before,stock_after
                        FROM sale_lines
                        WHERE transaction_id = ANY(%s)
                        ORDER BY item_name
                        """,
                        (txn_ids,),
                    )
                    for line in cur.fetchall():
                        lines_by_txn[line[0]].append(
                            {
                                "itemId": line[1],
                                "name": line[2],
                                "quantity": line[3],
                                "unitPrice": as_float(line[4]),
                                "lineTotal": as_float(line[5]),
                                "stockBefore": line[6],
                                "stockAfter": line[7],
                            }
                        )
                transactions = [transaction_json(row, lines_by_txn[row[0]]) for row in txn_rows]
            return {"accounts": accounts, "items": items, "transactions": transactions}


def daily_report_json(row) -> dict[str, Any]:
    return {
        "date": row[0].isoformat(),
        "startsAt": row[1].isoformat(),
        "endsAt": row[2].isoformat(),
        "transactionCount": row[3],
        "purchaseCount": row[4],
        "salesTotal": as_float(row[5]),
        "itemsSold": row[6],
        "fundsAdded": as_float(row[7]),
        "fundsSubtracted": as_float(row[8]),
        "undoCount": row[9],
        "undoTotal": as_float(row[10]),
        "balanceAdjustments": as_float(row[11]),
    }


def load_daily_reports(days: int = 14) -> list[dict[str, Any]]:
    report_days = max(1, min(days, 90))
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH days AS (
                  SELECT generate_series(
                    date_trunc('day', now() AT TIME ZONE %s) - ((%s::integer - 1) * interval '1 day'),
                    date_trunc('day', now() AT TIME ZONE %s),
                    interval '1 day'
                  ) AS local_start
                ),
                boundaries AS (
                  SELECT
                    local_start::date AS local_day,
                    local_start AT TIME ZONE %s AS starts_at,
                    (local_start + interval '1 day') AT TIME ZONE %s AS ends_at
                  FROM days
                ),
                line_totals AS (
                  SELECT transaction_id, SUM(quantity)::integer AS items_sold
                  FROM sale_lines
                  GROUP BY transaction_id
                )
                SELECT
                  b.local_day,
                  b.starts_at,
                  b.ends_at,
                  COUNT(t.id)::integer AS transaction_count,
                  COUNT(t.id) FILTER (
                    WHERE t.type = 'purchase'
                      AND NOT COALESCE(t.details_json ? 'undoneAt', FALSE)
                  )::integer AS purchase_count,
                  COALESCE(SUM(-t.amount) FILTER (
                    WHERE t.type = 'purchase'
                      AND NOT COALESCE(t.details_json ? 'undoneAt', FALSE)
                  ), 0) AS sales_total,
                  COALESCE(SUM(lt.items_sold) FILTER (
                    WHERE t.type = 'purchase'
                      AND NOT COALESCE(t.details_json ? 'undoneAt', FALSE)
                  ), 0)::integer AS items_sold,
                  COALESCE(SUM(t.amount) FILTER (
                    WHERE t.type = 'funds_added'
                      AND NOT COALESCE(t.details_json ? 'undoneAt', FALSE)
                  ), 0) AS funds_added,
                  COALESCE(SUM(-t.amount) FILTER (
                    WHERE t.type = 'funds_subtracted'
                      AND NOT COALESCE(t.details_json ? 'undoneAt', FALSE)
                  ), 0) AS funds_subtracted,
                  COUNT(t.id) FILTER (WHERE t.type = 'transaction_undone')::integer AS undo_count,
                  COALESCE(SUM(t.amount) FILTER (WHERE t.type = 'transaction_undone'), 0) AS undo_total,
                  COALESCE(SUM(t.amount) FILTER (
                    WHERE t.type = 'balance_set'
                      AND NOT COALESCE(t.details_json ? 'undoneAt', FALSE)
                  ), 0) AS balance_adjustments
                FROM boundaries b
                LEFT JOIN transactions t
                  ON t.date >= b.starts_at
                 AND t.date < b.ends_at
                LEFT JOIN line_totals lt ON lt.transaction_id = t.id
                GROUP BY b.local_day, b.starts_at, b.ends_at
                ORDER BY b.starts_at DESC
                """,
                (REPORT_TIME_ZONE, report_days, REPORT_TIME_ZONE, REPORT_TIME_ZONE, REPORT_TIME_ZONE),
            )
            return [daily_report_json(row) for row in cur.fetchall()]


@app.post("/api/login")
def login(data: LoginIn, request: Request, response: Response):
    ip = request.client.host if request.client else "unknown"
    attempts = [t for t in LOGIN_ATTEMPTS.get(ip, []) if time.time() - t < 900]
    if len(attempts) >= 8:
        raise HTTPException(429, "Too many failed login attempts")

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE email = %s AND active = TRUE", (data.email.lower(),))
            row = cur.fetchone()
            if not row or not verify_password(data.password, row[3]):
                attempts.append(time.time())
                LOGIN_ATTEMPTS[ip] = attempts
                raise HTTPException(401, "Invalid email or password")
            user_id = row[0]
    LOGIN_ATTEMPTS[ip] = []
    expires = int(time.time()) + SESSION_SECONDS
    response.set_cookie(
        COOKIE_NAME,
        sign_session(user_id, expires),
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
        max_age=SESSION_SECONDS,
    )
    return {"authenticated": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    return {"authenticated": False}


@app.get("/api/session")
def session(user: dict[str, Any] = Depends(current_user)):
    return {"authenticated": True, "user": user_public(user)}


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/state")
def state(user: dict[str, Any] = Depends(current_user)):
    is_manager = ROLE_LEVEL[user["role"]] >= ROLE_LEVEL["manager"]
    data = load_state(include_ledger=is_manager)
    data["currentUser"] = user_public(user)
    if is_manager:
        data["dailyReports"] = load_daily_reports()
        data["reportTimeZone"] = REPORT_TIME_ZONE
    return data


@app.post("/api/accounts")
def create_account(data: AccountIn, user: dict[str, Any] = Depends(manager_user)):
    account_name = required_text(data.name, "Account name")
    balance = money(data.openingBalance)
    with db() as conn:
        with conn.cursor() as cur:
            ensure_unique_name(cur, "accounts", account_name)
            account_id = insert_account(cur, account_name, balance, user, data.note)
        conn.commit()
    return {"ok": True, "id": account_id}


@app.post("/api/accounts/bulk")
def create_accounts_bulk(data: BulkAccountsIn, user: dict[str, Any] = Depends(manager_user)):
    balance = money(data.openingBalance)
    incoming: list[str] = []
    seen: set[str] = set()
    skipped: list[str] = []
    for raw_name in data.names:
        cleaned = raw_name.strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            skipped.append(cleaned)
            continue
        seen.add(key)
        incoming.append(cleaned)
    if not incoming:
        raise HTTPException(400, "At least one account name is required")

    created: list[dict[str, str]] = []
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT lower(name) FROM accounts WHERE lower(name) = ANY(%s::text[])", ([name.lower() for name in incoming],))
            existing = {row[0] for row in cur.fetchall()}
            for account_name in incoming:
                if account_name.lower() in existing:
                    skipped.append(account_name)
                    continue
                account_id = insert_account(cur, account_name, balance, user, data.note)
                created.append({"id": account_id, "name": account_name})
                existing.add(account_name.lower())
        conn.commit()
    return {"ok": True, "created": created, "skipped": skipped}


@app.patch("/api/accounts/{account_id}")
def update_account(account_id: str, data: AccountPatch, user: dict[str, Any] = Depends(manager_user)):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,name,note FROM accounts WHERE id=%s FOR UPDATE", (account_id,))
            before = cur.fetchone()
            if not before:
                raise HTTPException(404, "Account not found")
            account_name = required_text(data.name, "Account name") if data.name is not None else before[1]
            account_note = data.note.strip() if data.note is not None else before[2]
            if account_name != before[1]:
                ensure_unique_name(cur, "accounts", account_name, account_id)
            if account_name == before[1] and account_note == before[2]:
                return {"ok": True}
            cur.execute(
                "UPDATE accounts SET name=%s, note=%s, updated_at=now() WHERE id=%s",
                (account_name, account_note, account_id),
            )
            cur.execute(
                """
                INSERT INTO transactions
                  (id,type,account_id,account_name,amount,note,actor_user_id,actor_name,actor_role,details_json)
                VALUES (%s,'account_updated',%s,%s,0,%s,%s,%s,%s,%s)
                """,
                (
                    uid(),
                    account_id,
                    account_name,
                    "Account details updated",
                    user["id"],
                    user["name"],
                    user["role"],
                    Jsonb(
                        {
                            "before": {"name": before[1], "note": before[2]},
                            "after": {"name": account_name, "note": account_note},
                        }
                    ),
                ),
            )
        conn.commit()
    return {"ok": True}


def balance_txn(account_id: str, txn_type: str, amount: Decimal, note: str, user: dict[str, Any]):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,name,balance FROM accounts WHERE id=%s FOR UPDATE", (account_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Account not found")
            before = money(row[2])
            after = money(amount if txn_type == "balance_set" else before + amount)
            cur.execute(
                "UPDATE accounts SET balance=%s, updated_at=now() WHERE id=%s",
                (after, account_id),
            )
            cur.execute(
                """
                INSERT INTO transactions
                  (id,type,account_id,account_name,amount,balance_before,balance_after,note,
                   actor_user_id,actor_name,actor_role)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    uid(),
                    txn_type,
                    account_id,
                    row[1],
                    after - before,
                    before,
                    after,
                    note,
                    user["id"],
                    user["name"],
                    user["role"],
                ),
            )
        conn.commit()
    return {"ok": True}


@app.post("/api/accounts/{account_id}/funds/add")
def add_funds(account_id: str, data: FundsIn, user: dict[str, Any] = Depends(manager_user)):
    if data.amount <= 0:
        raise HTTPException(400, "Amount must be above zero")
    return balance_txn(account_id, "funds_added", money(data.amount), required_text(data.note, "Reason"), user)


@app.post("/api/accounts/{account_id}/funds/subtract")
def subtract_funds(account_id: str, data: FundsIn, user: dict[str, Any] = Depends(manager_user)):
    if data.amount <= 0:
        raise HTTPException(400, "Amount must be above zero")
    return balance_txn(account_id, "funds_subtracted", -money(data.amount), required_text(data.note, "Reason"), user)


@app.post("/api/accounts/{account_id}/balance/set")
def set_balance(account_id: str, data: SetBalanceIn, user: dict[str, Any] = Depends(manager_user)):
    return balance_txn(account_id, "balance_set", money(data.balance), required_text(data.note, "Reason"), user)


@app.post("/api/accounts/{account_id}/refund")
def refund_account(account_id: str, data: RefundIn, user: dict[str, Any] = Depends(manager_user)):
    method = data.method.strip().lower()
    if method not in REFUND_METHODS:
        raise HTTPException(400, "Invalid refund method")
    note = required_text(data.note, "Reason")
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,name,balance FROM accounts WHERE id=%s FOR UPDATE", (account_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Account not found")
            before = money(row[2])
            if before <= 0:
                raise HTTPException(400, "Account has no balance to refund")
            cur.execute(
                "UPDATE accounts SET balance=0, updated_at=now() WHERE id=%s",
                (account_id,),
            )
            cur.execute(
                """
                INSERT INTO transactions
                  (id,type,account_id,account_name,amount,balance_before,balance_after,note,
                   actor_user_id,actor_name,actor_role,details_json)
                VALUES (%s,'refund',%s,%s,%s,%s,0,%s,%s,%s,%s,%s)
                """,
                (
                    uid(),
                    account_id,
                    row[1],
                    -before,
                    before,
                    note,
                    user["id"],
                    user["name"],
                    user["role"],
                    Jsonb({"method": method}),
                ),
            )
        conn.commit()
    return {"ok": True}


@app.post("/api/items")
def create_item(data: ItemIn, user: dict[str, Any] = Depends(manager_user)):
    item_id = uid()
    item_name = required_text(data.name, "Item name")
    item_category = data.category.strip() or "General"
    item_sku = data.sku.strip()
    item_price = money(data.price)
    item_quantity = max(0, data.quantity)
    item_low_stock = max(0, data.lowStockAt)
    with db() as conn:
        with conn.cursor() as cur:
            ensure_unique_name(cur, "items", item_name)
            cur.execute(
                """
                INSERT INTO items (id,name,category,sku,price,quantity,low_stock_at,active)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    item_id,
                    item_name,
                    item_category,
                    item_sku,
                    item_price,
                    item_quantity,
                    item_low_stock,
                    data.active,
                ),
            )
            cur.execute(
                """
                INSERT INTO transactions (id,type,note,actor_user_id,actor_name,actor_role,details_json)
                VALUES (%s,'item_created',%s,%s,%s,%s,%s)
                """,
                (
                    uid(),
                    f"{item_name} created",
                    user["id"],
                    user["name"],
                    user["role"],
                    Jsonb(
                        {
                            "itemId": item_id,
                            "after": {
                                "name": item_name,
                                "category": item_category,
                                "sku": item_sku,
                                "price": as_float(item_price),
                                "quantity": item_quantity,
                                "lowStockAt": item_low_stock,
                                "active": data.active,
                            },
                        }
                    ),
                ),
            )
        conn.commit()
    return {"ok": True, "id": item_id}


@app.patch("/api/items/{item_id}")
def update_item(item_id: str, data: ItemIn, user: dict[str, Any] = Depends(manager_user)):
    item_name = required_text(data.name, "Item name")
    item_category = data.category.strip() or "General"
    item_sku = data.sku.strip()
    item_price = money(data.price)
    item_quantity = max(0, data.quantity)
    item_low_stock = max(0, data.lowStockAt)
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id,name,category,sku,price,quantity,low_stock_at,active FROM items WHERE id=%s FOR UPDATE",
                (item_id,),
            )
            before = cur.fetchone()
            if not before:
                raise HTTPException(404, "Item not found")
            ensure_unique_name(cur, "items", item_name, item_id)
            cur.execute(
                """
                UPDATE items
                SET name=%s, category=%s, sku=%s, price=%s, quantity=%s, low_stock_at=%s, active=%s, updated_at=now()
                WHERE id=%s
                """,
                (
                    item_name,
                    item_category,
                    item_sku,
                    item_price,
                    item_quantity,
                    item_low_stock,
                    data.active,
                    item_id,
                ),
            )
            cur.execute(
                """
                INSERT INTO transactions (id,type,note,actor_user_id,actor_name,actor_role,details_json)
                VALUES (%s,'item_updated',%s,%s,%s,%s,%s)
                """,
                (
                    uid(),
                    f"{item_name} updated",
                    user["id"],
                    user["name"],
                    user["role"],
                    Jsonb(
                        {
                            "itemId": item_id,
                            "before": {
                                "name": before[1],
                                "category": before[2],
                                "sku": before[3],
                                "price": as_float(before[4]),
                                "quantity": before[5],
                                "lowStockAt": before[6],
                                "active": before[7],
                            },
                            "after": {
                                "name": item_name,
                                "category": item_category,
                                "sku": item_sku,
                                "price": as_float(item_price),
                                "quantity": item_quantity,
                                "lowStockAt": item_low_stock,
                                "active": data.active,
                            },
                        }
                    ),
                ),
            )
        conn.commit()
    return {"ok": True}


@app.delete("/api/items/{item_id}")
def delete_item(item_id: str, user: dict[str, Any] = Depends(manager_user)):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id,name,active FROM items WHERE id=%s FOR UPDATE",
                (item_id,),
            )
            before = cur.fetchone()
            if not before:
                raise HTTPException(404, "Item not found")
            cur.execute("UPDATE items SET active=FALSE, updated_at=now() WHERE id=%s", (item_id,))
            cur.execute(
                """
                INSERT INTO transactions (id,type,note,actor_user_id,actor_name,actor_role,details_json)
                VALUES (%s,'item_deleted',%s,%s,%s,%s,%s)
                """,
                (
                    uid(),
                    f"{before[1]} hidden",
                    user["id"],
                    user["name"],
                    user["role"],
                    Jsonb({"itemId": item_id, "before": {"active": before[2]}, "after": {"active": False}}),
                ),
            )
        conn.commit()
    return {"ok": True}


@app.post("/api/sales")
def create_sale(data: SaleIn, user: dict[str, Any] = Depends(current_user)):
    if not data.lines:
        raise HTTPException(400, "Sale requires at least one item")
    requested: dict[str, int] = {}
    for line in data.lines:
        requested[line.itemId] = requested.get(line.itemId, 0) + line.quantity
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,name,balance,status FROM accounts WHERE id=%s FOR UPDATE", (data.accountId,))
            account = cur.fetchone()
            if not account:
                raise HTTPException(404, "Account not found")
            if account[3] != "active":
                raise HTTPException(400, "Account is not active")
            before_balance = money(account[2])
            total = Decimal("0.00")
            prepared = []
            for item_id, quantity in requested.items():
                cur.execute(
                    "SELECT id,name,price,quantity,active FROM items WHERE id=%s FOR UPDATE",
                    (item_id,),
                )
                item = cur.fetchone()
                if not item or not item[4]:
                    raise HTTPException(400, "Item unavailable")
                if quantity > item[3]:
                    raise HTTPException(400, f"Not enough stock for {item[1]}")
                line_total = money(item[2]) * quantity
                total += line_total
                prepared.append((item, quantity, line_total))
            after_balance = money(before_balance - total)
            if after_balance < 0 and not data.allowNegative:
                raise HTTPException(400, "Insufficient balance")
            if after_balance < 0 and data.allowNegative and ROLE_LEVEL[user["role"]] < ROLE_LEVEL["manager"]:
                raise HTTPException(403, "Manager permission required for negative balances")
            txn_id = uid()
            cur.execute(
                """
                UPDATE accounts SET balance=%s, updated_at=now() WHERE id=%s
                """,
                (after_balance, account[0]),
            )
            cur.execute(
                """
                INSERT INTO transactions
                  (id,type,account_id,account_name,amount,balance_before,balance_after,note,
                   actor_user_id,actor_name,actor_role)
                VALUES (%s,'purchase',%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    txn_id,
                    account[0],
                    account[1],
                    -total,
                    before_balance,
                    after_balance,
                    data.note.strip(),
                    user["id"],
                    user["name"],
                    user["role"],
                ),
            )
            for item, qty, line_total in prepared:
                stock_before = item[3]
                stock_after = stock_before - qty
                cur.execute("UPDATE items SET quantity=%s, updated_at=now() WHERE id=%s", (stock_after, item[0]))
                cur.execute(
                    """
                    INSERT INTO sale_lines
                      (id,transaction_id,item_id,item_name,quantity,unit_price,line_total,stock_before,stock_after)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (uid(), txn_id, item[0], item[1], qty, money(item[2]), line_total, stock_before, stock_after),
                )
        conn.commit()
    return {"ok": True, "transactionId": txn_id}


@app.get("/api/ledger")
def ledger(user: dict[str, Any] = Depends(manager_user)):
    return {"transactions": load_state(include_ledger=True)["transactions"]}


@app.get("/api/reports/daily")
def daily_reports(days: int = 14, user: dict[str, Any] = Depends(manager_user)):
    return {"reports": load_daily_reports(days), "timeZone": REPORT_TIME_ZONE}


@app.post("/api/transactions/{transaction_id}/undo")
def undo_transaction(transaction_id: str, data: UndoTransactionIn, user: dict[str, Any] = Depends(manager_user)):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id,date,type,account_id,account_name,amount,balance_before,balance_after,note,details_json
                FROM transactions
                WHERE id=%s
                FOR UPDATE
                """,
                (transaction_id,),
            )
            txn = cur.fetchone()
            if not txn:
                raise HTTPException(404, "Transaction not found")
            txn_type = txn[2]
            details = txn[9] or {}
            if not isinstance(details, dict):
                details = {}
            if details.get("undoneAt") or details.get("undoOf") or txn_type == "transaction_undone":
                raise HTTPException(400, "Transaction is already an undo entry or has already been undone")
            if txn_type not in REVERSIBLE_TRANSACTION_TYPES:
                raise HTTPException(400, "This transaction type cannot be undone")
            cur.execute("SELECT id FROM transactions WHERE details_json->>'undoOf'=%s LIMIT 1", (transaction_id,))
            if cur.fetchone():
                raise HTTPException(400, "Transaction has already been undone")
            account_id = txn[3]
            if not account_id:
                raise HTTPException(400, "Transaction is not attached to an account")
            cur.execute("SELECT id,name,balance,status FROM accounts WHERE id=%s FOR UPDATE", (account_id,))
            account = cur.fetchone()
            if not account:
                raise HTTPException(404, "Account not found")

            restored_items: list[dict[str, Any]] = []
            if txn_type == "purchase":
                cur.execute(
                    """
                    SELECT item_id,item_name,quantity
                    FROM sale_lines
                    WHERE transaction_id=%s
                    ORDER BY item_name
                    """,
                    (transaction_id,),
                )
                lines = cur.fetchall()
                if not lines:
                    raise HTTPException(400, "Purchase has no item lines to restore")
                for item_id, item_name, quantity in lines:
                    if not item_id:
                        raise HTTPException(400, f"Cannot restore stock for {item_name}")
                    cur.execute("SELECT id,name,quantity FROM items WHERE id=%s FOR UPDATE", (item_id,))
                    item = cur.fetchone()
                    if not item:
                        raise HTTPException(400, f"Cannot restore stock for {item_name}")
                    stock_before = item[2]
                    stock_after = stock_before + quantity
                    cur.execute("UPDATE items SET quantity=%s, updated_at=now() WHERE id=%s", (stock_after, item_id))
                    restored_items.append(
                        {
                            "itemId": item_id,
                            "name": item[1],
                            "quantity": quantity,
                            "stockBefore": stock_before,
                            "stockAfter": stock_after,
                        }
                    )

            original_amount = money(txn[5])
            reversal_amount = money(-original_amount)
            before_balance = money(account[2])
            after_balance = money(before_balance + reversal_amount)
            undo_id = uid()
            undo_note = data.note.strip() or f"Undo {txn_type}"
            undone_at = now().isoformat()

            cur.execute(
                "UPDATE accounts SET balance=%s, updated_at=now() WHERE id=%s",
                (after_balance, account_id),
            )
            cur.execute(
                "UPDATE transactions SET details_json = COALESCE(details_json, '{}'::jsonb) || %s WHERE id=%s",
                (
                    Jsonb(
                        {
                            "undoneAt": undone_at,
                            "undoneBy": user["id"],
                            "undoneByName": user["name"],
                            "undoTransactionId": undo_id,
                            "undoNote": undo_note,
                        }
                    ),
                    transaction_id,
                ),
            )
            cur.execute(
                """
                INSERT INTO transactions
                  (id,type,account_id,account_name,amount,balance_before,balance_after,note,
                   actor_user_id,actor_name,actor_role,details_json)
                VALUES (%s,'transaction_undone',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    undo_id,
                    account_id,
                    account[1],
                    reversal_amount,
                    before_balance,
                    after_balance,
                    undo_note,
                    user["id"],
                    user["name"],
                    user["role"],
                    Jsonb(
                        {
                            "undoOf": transaction_id,
                            "undoType": txn_type,
                            "originalAmount": as_float(original_amount),
                            "originalAccountName": txn[4],
                            "restoredItems": restored_items,
                        }
                    ),
                ),
            )
        conn.commit()
    return {"ok": True, "id": undo_id}


@app.get("/api/users")
def users(user: dict[str, Any] = Depends(manager_user)):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users ORDER BY name")
            return {"users": [user_public(row_dict(cur, row)) for row in cur.fetchall()]}


@app.post("/api/users")
def create_user(data: UserIn, user: dict[str, Any] = Depends(manager_user)):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (id,email,name,password_hash,role)
                VALUES (%s,%s,%s,%s,%s)
                """,
                (uid(), data.email.lower(), data.name.strip(), hash_password(data.password), data.role),
            )
        conn.commit()
    return {"ok": True}


@app.patch("/api/users/{user_id}")
def update_user(user_id: str, data: UserPatch, user: dict[str, Any] = Depends(manager_user)):
    fields = []
    values = []
    if data.name is not None:
        fields.append("name=%s")
        values.append(data.name.strip())
    if data.password is not None:
        fields.append("password_hash=%s")
        values.append(hash_password(data.password))
    if data.role is not None:
        fields.append("role=%s")
        values.append(data.role)
    if data.active is not None:
        fields.append("active=%s")
        values.append(data.active)
    if not fields:
        return {"ok": True}
    values.append(user_id)
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT role, active FROM users WHERE id=%s", (user_id,))
            existing = cur.fetchone()
            if not existing:
                raise HTTPException(404, "Worker not found")
            next_role = data.role if data.role is not None else existing[0]
            next_active = data.active if data.active is not None else existing[1]
            if existing[0] == "manager" and existing[1] and (next_role != "manager" or not next_active):
                cur.execute("SELECT count(*) FROM users WHERE role='manager' AND active=TRUE AND id<>%s", (user_id,))
                if cur.fetchone()[0] == 0:
                    raise HTTPException(400, "At least one active manager is required")
            cur.execute(f"UPDATE users SET {', '.join(fields)}, updated_at=now() WHERE id=%s", values)
        conn.commit()
    return {"ok": True}


@app.get("/api/backups")
def backup(user: dict[str, Any] = Depends(manager_user)):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id,email,name,password_hash,role,active,created_at,updated_at
                FROM users
                ORDER BY name
                """
            )
            users = [
                {
                    "id": row[0],
                    "email": row[1],
                    "name": row[2],
                    "passwordHash": row[3],
                    "role": row[4],
                    "active": row[5],
                    "createdAt": row[6].isoformat(),
                    "updatedAt": row[7].isoformat(),
                }
                for row in cur.fetchall()
            ]
    return load_state(include_ledger=True) | {"users": users, "exportedAt": now().isoformat(), "app": "canteen-pos"}


@app.post("/api/backups")
def restore_backup(data: dict[str, Any], user: dict[str, Any] = Depends(manager_user)):
    if not isinstance(data, dict):
        raise HTTPException(400, "Invalid backup file")
    for key in ("accounts", "items", "transactions"):
        if not isinstance(data.get(key), list):
            raise HTTPException(400, "Invalid backup file")
    if data.get("users") is not None and not isinstance(data.get("users"), list):
        raise HTTPException(400, "Invalid backup file")

    with db() as conn:
        with conn.cursor() as cur:
            for backup_user in data.get("users") or []:
                if not isinstance(backup_user, dict):
                    raise HTTPException(400, "Invalid worker in backup file")
                email = str(backup_user.get("email") or "").strip().lower()
                password_hash = backup_user.get("passwordHash") or backup_user.get("password_hash")
                role = backup_user.get("role") if backup_user.get("role") in ROLE_LEVEL else "manager" if backup_user.get("role") == "owner" else "cashier"
                if not email or not password_hash:
                    continue
                cur.execute("SELECT id FROM users WHERE email=%s", (email,))
                existing = cur.fetchone()
                if existing:
                    cur.execute(
                        """
                        UPDATE users
                        SET name=%s, password_hash=%s, role=%s, active=%s, updated_at=now()
                        WHERE id=%s
                        """,
                        (
                            str(backup_user.get("name") or email),
                            password_hash,
                            role,
                            backup_user.get("active") is not False,
                            existing[0],
                        ),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO users (id,email,name,password_hash,role,active)
                        VALUES (%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            str(backup_user.get("id") or uid()),
                            email,
                            str(backup_user.get("name") or email),
                            password_hash,
                            role,
                            backup_user.get("active") is not False,
                        ),
                    )
            cur.execute("UPDATE users SET role='manager', active=TRUE, updated_at=now() WHERE id=%s", (user["id"],))

            cur.execute("DELETE FROM sale_lines")
            cur.execute("DELETE FROM transactions")
            cur.execute("DELETE FROM items")
            cur.execute("DELETE FROM accounts")
            account_ids: set[str] = set()
            account_names: set[str] = set()
            for account in data["accounts"]:
                if not isinstance(account, dict):
                    raise HTTPException(400, "Invalid account in backup file")
                account_id = str(account.get("id") or uid())
                account_name = str(account.get("name") or "Unnamed").strip() or "Unnamed"
                account_key = account_name.lower()
                if account_key in account_names:
                    continue
                account_names.add(account_key)
                account_ids.add(account_id)
                cur.execute(
                    "INSERT INTO accounts (id,name,balance,status,note) VALUES (%s,%s,%s,%s,%s)",
                    (
                        account_id,
                        account_name,
                        money(account.get("balance")),
                        str(account.get("status") or "active"),
                        str(account.get("note") or ""),
                    ),
                )
            item_ids: set[str] = set()
            item_names: set[str] = set()
            for item in data["items"]:
                if not isinstance(item, dict):
                    raise HTTPException(400, "Invalid item in backup file")
                item_id = str(item.get("id") or uid())
                item_name = str(item.get("name") or "Unnamed").strip() or "Unnamed"
                item_key = item_name.lower()
                if item_key in item_names:
                    continue
                item_names.add(item_key)
                item_ids.add(item_id)
                cur.execute(
                    """
                    INSERT INTO items (id,name,category,sku,price,quantity,low_stock_at,active)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        item_id,
                        item_name,
                        str(item.get("category") or "General"),
                        str(item.get("sku") or ""),
                        money(item.get("price")),
                        max(0, to_int(item.get("quantity"))),
                        max(0, to_int(item.get("lowStockAt") or item.get("low_stock_at"))),
                        item.get("active") is not False,
                    ),
                )
            cur.execute("SELECT id FROM users")
            user_ids = {row[0] for row in cur.fetchall()}
            for txn in data["transactions"]:
                if not isinstance(txn, dict):
                    raise HTTPException(400, "Invalid transaction in backup file")
                txn_id = str(txn.get("id") or uid())
                account_id = txn.get("accountId") or txn.get("account_id")
                account_id = str(account_id) if account_id is not None and str(account_id) in account_ids else None
                actor_id = txn.get("actorUserId") or txn.get("actor_user_id")
                actor_id = str(actor_id) if actor_id is not None and str(actor_id) in user_ids else None
                actor_role = txn.get("actorRole") or txn.get("actor_role") or "manager"
                if actor_role == "owner":
                    actor_role = "manager"
                if actor_role not in ROLE_LEVEL:
                    actor_role = "manager"
                details = txn.get("details") or txn.get("details_json") or {"imported": True}
                if not isinstance(details, dict):
                    details = {"imported": True, "value": str(details)}
                cur.execute(
                    """
                    INSERT INTO transactions
                      (id,date,type,account_id,account_name,amount,balance_before,balance_after,note,
                       actor_user_id,actor_name,actor_role,details_json)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        txn_id,
                        parse_date(txn.get("date") or txn.get("createdAt") or txn.get("created_at")),
                        str(txn.get("type") or "imported"),
                        account_id,
                        str(txn.get("accountName") or txn.get("account_name") or ""),
                        money(txn.get("amount")),
                        nullable_money(txn.get("balanceBefore") if "balanceBefore" in txn else txn.get("balance_before")),
                        nullable_money(txn.get("balanceAfter") if "balanceAfter" in txn else txn.get("balance_after")),
                        str(txn.get("note") or ""),
                        actor_id,
                        str(txn.get("actorName") or txn.get("actor_name") or "Imported"),
                        actor_role,
                        Jsonb(details),
                    ),
                )
                for line in txn.get("items") or txn.get("lines") or []:
                    if not isinstance(line, dict):
                        raise HTTPException(400, "Invalid sale line in backup file")
                    line_item_id = line.get("itemId") or line.get("item_id")
                    line_item_id = str(line_item_id) if line_item_id is not None and str(line_item_id) in item_ids else None
                    quantity = max(1, to_int(line.get("quantity") or line.get("qty"), 1))
                    unit_price = money(line.get("unitPrice") or line.get("unit_price") or line.get("price"))
                    line_total = money(line.get("lineTotal") or line.get("line_total") or (unit_price * quantity))
                    stock_before = to_int(line.get("stockBefore") or line.get("stock_before"))
                    stock_after = to_int(line.get("stockAfter") or line.get("stock_after"), stock_before)
                    cur.execute(
                        """
                        INSERT INTO sale_lines
                          (id,transaction_id,item_id,item_name,quantity,unit_price,line_total,stock_before,stock_after)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            uid(),
                            txn_id,
                            line_item_id,
                            str(line.get("name") or line.get("itemName") or line.get("item_name") or "Imported item"),
                            quantity,
                            unit_price,
                            line_total,
                            stock_before,
                            stock_after,
                        ),
                    )
        conn.commit()
    return {"ok": True}


@app.delete("/api/backups")
def clear_all(user: dict[str, Any] = Depends(manager_user)):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sale_lines")
            cur.execute("DELETE FROM transactions")
            cur.execute("DELETE FROM items")
            cur.execute("DELETE FROM accounts")
        conn.commit()
    return {"ok": True}


if (DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")


@app.get("/{path:path}")
def frontend(path: str):
    target = safe_dist_file(path)
    if target:
        return FileResponse(target)
    index = DIST / "index.html"
    if index.exists():
        return FileResponse(index)
    return JSONResponse({"error": "Frontend not built. Run npm run build."}, status_code=500)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
