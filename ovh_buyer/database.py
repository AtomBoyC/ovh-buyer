"""Thread-safe SQLite persistence layer for OVH Sniper."""

import base64
import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .config import DATA_DIR, DB_PATH, get_data_dir, get_db_path
from .ovh_client import redact_proxy_list_text


BEIJING_TZ = timezone(timedelta(hours=8))

# Settings that must never leave the box in clear text via /api/export
EXPORT_SECRET_SETTING_KEYS = (
    "telegram_bot_token",
    "bark_url",
    "serverchan_key",
    "dingtalk_webhook",
    "dingtalk_secret",
    "feishu_webhook",
    "discord_webhook",
    "custom_webhook_url",
)


def now_iso() -> str:
    """Return current timestamp in UTC+8 ISO format."""
    return datetime.now(BEIJING_TZ).isoformat(timespec="seconds")


def _derive_stream_key(secret: str, length: int) -> bytes:
    """Derive a keystream for optional at-rest obfuscation using SHA-256."""
    out = bytearray()
    counter = 0
    secret_bytes = secret.encode("utf-8")
    while len(out) < length:
        block = hashlib.sha256(secret_bytes + counter.to_bytes(4, "big")).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def protect_secret(value: str, key: str = "") -> str:
    """Optionally obfuscate credentials at rest when DATA_ENCRYPTION_KEY is set."""
    if not value or not key:
        return value
    raw = value.encode("utf-8")
    nonce = os.urandom(8)
    mask = _derive_stream_key(key + nonce.hex(), len(raw))
    xored = bytes(a ^ b for a, b in zip(raw, mask))
    return "enc1:" + base64.urlsafe_b64encode(nonce + xored).decode("ascii")


def reveal_secret(value: str, key: str = "") -> str:
    """Decode obfuscated credentials if encrypted with protect_secret."""
    if not value or not value.startswith("enc1:"):
        return value
    if not key:
        return ""
    try:
        blob = base64.urlsafe_b64decode(value[5:].encode("ascii"))
        nonce, xored = blob[:8], blob[8:]
        mask = _derive_stream_key(key + nonce.hex(), len(xored))
        raw = bytes(a ^ b for a, b in zip(xored, mask))
        return raw.decode("utf-8")
    except Exception:
        return ""


def mask_secret(value: str) -> str:
    """Return a masked display string for UI/API responses."""
    if not value:
        return ""
    if len(value) <= 6:
        return "*" * len(value)
    return value[:3] + "****" + value[-4:]


DEFAULT_SETTINGS: Dict[str, Any] = {
    "global_qps": 4.0,
    "default_poll_interval": 5,
    "radar_poll_interval": 30,
    "vps_poll_interval": 15,
    "bulk_availability_threshold": 3,
    "http_proxy": "",
    "monitor_proxies": "",
    "monitor_proxy_fallback_direct": True,
    "order_use_proxy": False,
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "telegram_topic_id": "",
    "telegram_admin_ids": "",
    "telegram_polling_enabled": True,
    "telegram_proxy": "",
    "bark_url": "",
    "serverchan_key": "",
    "dingtalk_webhook": "",
    "dingtalk_secret": "",
    "feishu_webhook": "",
    "discord_webhook": "",
    "custom_webhook_url": "",
    "notify_on_stock": True,
    "notify_on_order": True,
    "notify_on_paid": True,
    "notify_on_delivered": True,
    "notify_on_error": False,
    "edit_sold_out_telegram": True,
    "sniper_enabled": True,
    "fast_lane_enabled": True,
    "tracker_enabled": True,
    "radar_enabled": False,
    "vps_monitor_enabled": False,
    "panel_password_hash": "",
}


class Database:
    """Thread-safe SQLite database with WAL mode and atomic helpers."""

    def __init__(self, db_path: Optional[Path] = None, encryption_key: Optional[str] = None):
        self.db_path = Path(db_path) if db_path is not None else get_db_path()
        self.encryption_key = (
            encryption_key if encryption_key is not None else os.environ.get("DATA_ENCRYPTION_KEY", "")
        )
        self._lock = threading.RLock()
        self._log_listeners: List[Callable[[Dict[str, Any]], None]] = []
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30.0)
        except (OSError, sqlite3.OperationalError) as exc:
            uid = getattr(os, "getuid", lambda: "N/A")()
            gid = getattr(os, "getgid", lambda: "N/A")()
            parent = self.db_path.parent
            writable = os.access(str(parent), os.W_OK) if parent.exists() else False
            raise sqlite3.OperationalError(
                f"无法打开或创建数据库文件 '{self.db_path}' (原始错误: {exc}; "
                f"当前进程 UID:GID={uid}:{gid}, 数据目录 '{parent}' 可写={writable})。"
                f"若使用 Docker 挂载宿主机目录，请确保宿主机 ./data 目录可写 "
                f"(例如执行: chown -R 1000:1000 ./data 或更新至最新镜像)。"
            ) from exc
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("PRAGMA journal_mode=WAL;")
            except sqlite3.OperationalError:
                # Fallback for network/virtual filesystems (NFS/SMB) where WAL shared-memory mmap fails
                cur.execute("PRAGMA journal_mode=DELETE;")
            cur.execute("PRAGMA synchronous=NORMAL;")
            cur.execute("PRAGMA foreign_keys=ON;")
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    endpoint TEXT NOT NULL DEFAULT 'ovh-eu',
                    subsidiary TEXT NOT NULL DEFAULT 'IE',
                    app_key TEXT NOT NULL,
                    app_secret TEXT NOT NULL,
                    consumer_key TEXT NOT NULL,
                    is_default INTEGER NOT NULL DEFAULT 0,
                    nichandle TEXT DEFAULT '',
                    currency TEXT DEFAULT 'EUR',
                    last_verified_at TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sniper_tasks (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    product_line TEXT NOT NULL DEFAULT 'eco',
                    plan_code TEXT NOT NULL,
                    datacenters TEXT NOT NULL DEFAULT '[]',
                    memory_filter TEXT DEFAULT '',
                    storage_filter TEXT DEFAULT '',
                    options TEXT NOT NULL DEFAULT '[]',
                    duration TEXT NOT NULL DEFAULT 'auto',
                    pricing_mode TEXT NOT NULL DEFAULT 'auto',
                    os_code TEXT NOT NULL DEFAULT 'none_64.en',
                    coupons TEXT NOT NULL DEFAULT '[]',
                    max_price REAL NOT NULL DEFAULT 0,
                    price_includes_tax INTEGER NOT NULL DEFAULT 0,
                    quantity INTEGER NOT NULL DEFAULT 1,
                    max_orders INTEGER NOT NULL DEFAULT 1,
                    ordered_count INTEGER NOT NULL DEFAULT 0,
                    auto_pay INTEGER NOT NULL DEFAULT 0,
                    waive_retractation INTEGER NOT NULL DEFAULT 1,
                    dry_run INTEGER NOT NULL DEFAULT 0,
                    mode TEXT NOT NULL DEFAULT 'buy',
                    only_instant INTEGER NOT NULL DEFAULT 0,
                    poll_interval INTEGER NOT NULL DEFAULT 5,
                    priority INTEGER NOT NULL DEFAULT 10,
                    status TEXT NOT NULL DEFAULT 'running',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_check_at TEXT DEFAULT '',
                    last_status_msg TEXT DEFAULT '',
                    last_availability TEXT DEFAULT '{}',
                    source TEXT DEFAULT 'manual',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS radar_rules (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    rule_type TEXT NOT NULL DEFAULT 'new_sku',
                    account_id TEXT DEFAULT '',
                    endpoint TEXT NOT NULL DEFAULT 'ovh-eu',
                    subsidiary TEXT NOT NULL DEFAULT 'IE',
                    keyword_pattern TEXT DEFAULT '',
                    memory_pattern TEXT DEFAULT '',
                    storage_pattern TEXT DEFAULT '',
                    datacenters TEXT NOT NULL DEFAULT '[]',
                    max_price REAL NOT NULL DEFAULT 0,
                    auto_snipe INTEGER NOT NULL DEFAULT 0,
                    auto_pay INTEGER NOT NULL DEFAULT 0,
                    dry_run INTEGER NOT NULL DEFAULT 1,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    known_plan_codes TEXT NOT NULL DEFAULT '[]',
                    matched_plan_codes TEXT NOT NULL DEFAULT '[]',
                    last_check_at TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS vps_watches (
                    id TEXT PRIMARY KEY,
                    plan_code TEXT NOT NULL,
                    account_id TEXT DEFAULT '',
                    endpoint TEXT NOT NULL DEFAULT 'ovh-eu',
                    subsidiary TEXT NOT NULL DEFAULT 'IE',
                    datacenters TEXT NOT NULL DEFAULT '[]',
                    auto_order INTEGER NOT NULL DEFAULT 0,
                    auto_pay INTEGER NOT NULL DEFAULT 0,
                    dry_run INTEGER NOT NULL DEFAULT 1,
                    max_orders INTEGER NOT NULL DEFAULT 1,
                    ordered_count INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_state TEXT NOT NULL DEFAULT '{}',
                    last_check_at TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY,
                    task_id TEXT DEFAULT '',
                    account_id TEXT DEFAULT '',
                    account_name TEXT DEFAULT '',
                    product_line TEXT NOT NULL DEFAULT 'eco',
                    plan_code TEXT NOT NULL,
                    datacenter TEXT NOT NULL,
                    options TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL,
                    dry_run INTEGER NOT NULL DEFAULT 0,
                    order_id TEXT DEFAULT '',
                    order_url TEXT DEFAULT '',
                    price_without_tax REAL,
                    price_with_tax REAL,
                    currency TEXT DEFAULT '',
                    error_message TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    level TEXT NOT NULL,
                    source TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS kv_cache (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_logs_created ON logs(id DESC);
                CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_tasks_status ON sniper_tasks(status);
                """
            )
            self._conn.commit()
            self._run_migrations()
            self._seed_default_settings()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        cur = self._conn.cursor()
        cur.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in cur.fetchall()}
        if column not in existing:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _run_migrations(self) -> None:
        """Idempotent v2.0 schema migrations preserving existing v1.0 user data."""
        self._ensure_column("accounts", "default_payment_type", "TEXT DEFAULT ''")
        self._ensure_column("sniper_tasks", "fast_lane", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("sniper_tasks", "allow_non_default", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column("orders", "memory", "TEXT DEFAULT ''")
        self._ensure_column("orders", "storage", "TEXT DEFAULT ''")
        self._ensure_column("orders", "track_state", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("orders", "ovh_status", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("orders", "server_name", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("orders", "server_ip", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("orders", "spec_json", "TEXT NOT NULL DEFAULT '{}'")
        self._ensure_column("orders", "notified_events", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("orders", "next_check_at", "REAL NOT NULL DEFAULT 0")
        self._ensure_column("accounts", "proxy_url", "TEXT DEFAULT ''")
        self._ensure_column("sniper_tasks", "proxy_url", "TEXT DEFAULT ''")
        self._ensure_column("radar_rules", "proxy_url", "TEXT DEFAULT ''")
        self._ensure_column("vps_watches", "proxy_url", "TEXT DEFAULT ''")
        self._conn.commit()

    def _seed_default_settings(self) -> None:
        env_overrides = {
            "http_proxy": os.environ.get("OVH_HTTP_PROXY", ""),
            "monitor_proxies": os.environ.get("OVH_MONITOR_PROXIES", ""),
            "telegram_bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
            "telegram_topic_id": os.environ.get("TELEGRAM_TOPIC_ID", ""),
            "telegram_admin_ids": os.environ.get("TELEGRAM_ADMIN_IDS", ""),
            "telegram_proxy": os.environ.get("TELEGRAM_PROXY", ""),
            "bark_url": os.environ.get("BARK_URL", ""),
            "serverchan_key": os.environ.get("SERVERCHAN_KEY", ""),
            "dingtalk_webhook": os.environ.get("DINGTALK_WEBHOOK", ""),
            "dingtalk_secret": os.environ.get("DINGTALK_SECRET", ""),
            "feishu_webhook": os.environ.get("FEISHU_WEBHOOK", ""),
            "discord_webhook": os.environ.get("DISCORD_WEBHOOK", ""),
            "custom_webhook_url": os.environ.get("CUSTOM_WEBHOOK_URL", ""),
        }
        cur = self._conn.cursor()
        for k, v in DEFAULT_SETTINGS.items():
            cur.execute("SELECT value FROM settings WHERE key = ?", (k,))
            row = cur.fetchone()
            if row is None:
                initial = env_overrides.get(k) if env_overrides.get(k) else v
                cur.execute(
                    "INSERT INTO settings(key, value) VALUES (?, ?)",
                    (k, json.dumps(initial, ensure_ascii=False)),
                )
        self._conn.commit()

    # ---------- Settings ----------

    def get_settings(self) -> Dict[str, Any]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT key, value FROM settings")
            data = dict(DEFAULT_SETTINGS)
            for row in cur.fetchall():
                try:
                    data[row["key"]] = json.loads(row["value"])
                except json.JSONDecodeError:
                    data[row["key"]] = row["value"]
            return data

    def get_setting(self, key: str, default: Any = None) -> Any:
        settings = self.get_settings()
        return settings.get(key, default)

    def update_settings(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            cur = self._conn.cursor()
            for key, value in patch.items():
                if key not in DEFAULT_SETTINGS:
                    continue
                cur.execute(
                    "INSERT INTO settings(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, json.dumps(value, ensure_ascii=False)),
                )
            self._conn.commit()
        return self.get_settings()

    # ---------- Logs & SSE listeners ----------

    def register_log_listener(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        with self._lock:
            self._log_listeners.append(callback)

    def unregister_log_listener(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        with self._lock:
            if callback in self._log_listeners:
                self._log_listeners.remove(callback)

    def add_log(self, level: str, message: str, source: str = "system") -> Dict[str, Any]:
        ts = now_iso()
        lvl = level.upper()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "INSERT INTO logs(level, source, message, created_at) VALUES (?, ?, ?, ?)",
                (lvl, source, message, ts),
            )
            log_id = cur.lastrowid
            # Keep last 2000 log lines bounded
            if log_id and log_id % 100 == 0:
                cur.execute(
                    "DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT 2000)"
                )
            self._conn.commit()
            listeners = list(self._log_listeners)
        entry = {"id": log_id, "level": lvl, "source": source, "message": message, "created_at": ts}
        for cb in listeners:
            try:
                cb(entry)
            except Exception:
                pass
        return entry

    def get_logs(self, limit: int = 200, level: str = "", source: str = "", search: str = "") -> List[Dict[str, Any]]:
        with self._lock:
            clauses = []
            params: List[Any] = []
            if level:
                clauses.append("level = ?")
                params.append(level.upper())
            if source:
                clauses.append("source = ?")
                params.append(source)
            if search:
                clauses.append("message LIKE ?")
                params.append(f"%{search}%")
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            params.append(max(1, min(limit, 1000)))
            cur = self._conn.cursor()
            cur.execute(
                f"SELECT id, level, source, message, created_at FROM logs {where} ORDER BY id DESC LIMIT ?",
                params,
            )
            rows = [dict(r) for r in cur.fetchall()]
            rows.reverse()
            return rows

    def clear_logs(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM logs")
            self._conn.commit()

    # ---------- Accounts ----------

    def _row_to_account(self, row: sqlite3.Row, mask: bool = False) -> Dict[str, Any]:
        d = dict(row)
        app_secret = reveal_secret(d["app_secret"], self.encryption_key)
        consumer_key = reveal_secret(d["consumer_key"], self.encryption_key)
        d["is_default"] = bool(d["is_default"])
        if mask:
            d["app_secret"] = mask_secret(app_secret)
            d["consumer_key"] = mask_secret(consumer_key)
            d["has_secrets"] = bool(app_secret and consumer_key)
        else:
            d["app_secret"] = app_secret
            d["consumer_key"] = consumer_key
        return d

    def list_accounts(self, mask: bool = True) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT * FROM accounts ORDER BY is_default DESC, created_at ASC")
            return [self._row_to_account(r, mask=mask) for r in cur.fetchall()]

    def get_account(self, account_id: str, mask: bool = False) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            if account_id:
                cur.execute("SELECT * FROM accounts WHERE id = ?", (account_id,))
            else:
                cur.execute("SELECT * FROM accounts ORDER BY is_default DESC, created_at ASC LIMIT 1")
            row = cur.fetchone()
            return self._row_to_account(row, mask=mask) if row else None

    def get_default_account(self, mask: bool = False) -> Optional[Dict[str, Any]]:
        return self.get_account("", mask=mask)

    def create_account(self, data: Dict[str, Any]) -> Dict[str, Any]:
        now = now_iso()
        acc_id = str(uuid.uuid4())[:8]
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT COUNT(*) FROM accounts")
            count = cur.fetchone()[0]
            is_default = 1 if (data.get("is_default") or count == 0) else 0
            if is_default:
                cur.execute("UPDATE accounts SET is_default = 0")
            cur.execute(
                """
                INSERT INTO accounts (
                    id, name, endpoint, subsidiary, app_key, app_secret, consumer_key,
                    is_default, nichandle, currency, proxy_url, last_verified_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    acc_id,
                    data.get("name", "默认账号").strip() or "默认账号",
                    data.get("endpoint", "ovh-eu").strip(),
                    data.get("subsidiary", "IE").strip().upper(),
                    data.get("app_key", "").strip(),
                    protect_secret(data.get("app_secret", "").strip(), self.encryption_key),
                    protect_secret(data.get("consumer_key", "").strip(), self.encryption_key),
                    is_default,
                    data.get("nichandle", ""),
                    data.get("currency", "EUR"),
                    str(data.get("proxy_url", "") or "").strip(),
                    data.get("last_verified_at", ""),
                    now,
                    now,
                ),
            )
            self._conn.commit()
        return self.get_account(acc_id, mask=True)  # type: ignore[return-value]

    def update_account(self, account_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        existing = self.get_account(account_id, mask=False)
        if not existing:
            return None
        now = now_iso()
        name = patch.get("name", existing["name"]).strip() or existing["name"]
        endpoint = patch.get("endpoint", existing["endpoint"]).strip()
        subsidiary = patch.get("subsidiary", existing["subsidiary"]).strip().upper()
        app_key = patch.get("app_key", existing["app_key"]).strip()
        raw_secret = patch.get("app_secret", "")
        raw_ck = patch.get("consumer_key", "")
        # Preserve existing secret if UI sent blank or masked value
        app_secret = existing["app_secret"] if (not raw_secret or "****" in raw_secret) else raw_secret.strip()
        consumer_key = existing["consumer_key"] if (not raw_ck or "****" in raw_ck) else raw_ck.strip()
        is_default = 1 if patch.get("is_default", existing["is_default"]) else 0
        nichandle = patch.get("nichandle", existing["nichandle"])
        currency = patch.get("currency", existing["currency"])
        last_verified_at = patch.get("last_verified_at", existing["last_verified_at"])
        default_payment_type = patch.get("default_payment_type", existing.get("default_payment_type", ""))
        proxy_url = str(patch.get("proxy_url", existing.get("proxy_url", "")) or "").strip()

        with self._lock:
            cur = self._conn.cursor()
            if is_default:
                cur.execute("UPDATE accounts SET is_default = 0")
            cur.execute(
                """
                UPDATE accounts SET
                    name = ?, endpoint = ?, subsidiary = ?, app_key = ?,
                    app_secret = ?, consumer_key = ?, is_default = ?,
                    nichandle = ?, currency = ?, default_payment_type = ?,
                    proxy_url = ?, last_verified_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    name,
                    endpoint,
                    subsidiary,
                    app_key,
                    protect_secret(app_secret, self.encryption_key),
                    protect_secret(consumer_key, self.encryption_key),
                    is_default,
                    nichandle,
                    currency,
                    default_payment_type or "",
                    proxy_url,
                    last_verified_at,
                    now,
                    account_id,
                ),
            )
            self._conn.commit()
        return self.get_account(account_id, mask=True)

    def delete_account(self, account_id: str) -> bool:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
            deleted = cur.rowcount > 0
            if deleted:
                cur.execute("SELECT id FROM accounts WHERE is_default = 1 LIMIT 1")
                if not cur.fetchone():
                    cur.execute("SELECT id FROM accounts ORDER BY created_at ASC LIMIT 1")
                    first = cur.fetchone()
                    if first:
                        cur.execute("UPDATE accounts SET is_default = 1 WHERE id = ?", (first["id"],))
            self._conn.commit()
            return deleted

    # ---------- Sniper Tasks ----------

    @staticmethod
    def _normalize_poll_interval(raw: Any, default: float = 5) -> Any:
        if raw in (None, ""):
            val = float(default)
        else:
            val = max(0.0, float(raw))
        return int(val) if val.is_integer() else val

    def _row_to_task(self, row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        for field in ("datacenters", "options", "coupons"):
            try:
                d[field] = json.loads(d[field]) if d[field] else []
            except json.JSONDecodeError:
                d[field] = []
        try:
            d["last_availability"] = json.loads(d["last_availability"]) if d["last_availability"] else {}
        except json.JSONDecodeError:
            d["last_availability"] = {}
        for bf in (
            "price_includes_tax",
            "auto_pay",
            "waive_retractation",
            "dry_run",
            "only_instant",
            "fast_lane",
            "allow_non_default",
        ):
            d[bf] = bool(d.get(bf, 0))
        return d

    def list_tasks(self) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT * FROM sniper_tasks ORDER BY priority DESC, created_at DESC")
            return [self._row_to_task(r) for r in cur.fetchall()]

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT * FROM sniper_tasks WHERE id = ?", (task_id,))
            row = cur.fetchone()
            return self._row_to_task(row) if row else None

    def create_task(self, data: Dict[str, Any]) -> Dict[str, Any]:
        now = now_iso()
        task_id = str(uuid.uuid4())[:8]
        plan_code = str(data.get("plan_code", "")).strip()
        name = str(data.get("name", "")).strip() or plan_code
        account_id = str(data.get("account_id", "")).strip()
        if not account_id:
            default_acc = self.get_default_account(mask=True)
            account_id = default_acc["id"] if default_acc else ""
        dcs = data.get("datacenters") or []
        if isinstance(dcs, str):
            dcs = [x.strip().lower() for x in dcs.split(",") if x.strip()]
        else:
            dcs = [str(x).strip().lower() for x in dcs if str(x).strip()]
        options = data.get("options") or []
        if isinstance(options, str):
            options = [x.strip() for x in options.split(",") if x.strip()]
        coupons = data.get("coupons") or []
        if isinstance(coupons, str):
            coupons = [x.strip() for x in coupons.split(",") if x.strip()]

        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO sniper_tasks (
                    id, name, account_id, product_line, plan_code, datacenters,
                    memory_filter, storage_filter, options, duration, pricing_mode,
                    os_code, coupons, max_price, price_includes_tax, quantity,
                    max_orders, ordered_count, auto_pay, waive_retractation,
                    dry_run, mode, only_instant, fast_lane, allow_non_default,
                    poll_interval, priority, proxy_url, status,
                    attempts, last_check_at, last_status_msg, last_availability,
                    source, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '', '', '{}', ?, ?, ?
                )
                """,
                (
                    task_id,
                    name,
                    account_id,
                    str(data.get("product_line", "eco")).strip().lower() or "eco",
                    plan_code,
                    json.dumps(dcs, ensure_ascii=False),
                    str(data.get("memory_filter", "")).strip(),
                    str(data.get("storage_filter", "")).strip(),
                    json.dumps(options, ensure_ascii=False),
                    str(data.get("duration", "auto")).strip() or "auto",
                    str(data.get("pricing_mode", "auto")).strip() or "auto",
                    str(data.get("os_code", "none_64.en")).strip() or "none_64.en",
                    json.dumps(coupons, ensure_ascii=False),
                    float(data.get("max_price") or 0),
                    1 if data.get("price_includes_tax") else 0,
                    max(1, int(data.get("quantity") or 1)),
                    max(1, int(data.get("max_orders") or 1)),
                    1 if data.get("auto_pay") else 0,
                    1 if data.get("waive_retractation", True) else 0,
                    1 if data.get("dry_run") else 0,
                    str(data.get("mode", "buy")).strip().lower() or "buy",
                    1 if data.get("only_instant") else 0,
                    1 if data.get("fast_lane") else 0,
                    1 if data.get("allow_non_default", True) else 0,
                    self._normalize_poll_interval(data.get("poll_interval"), self.get_setting("default_poll_interval", 5)),
                    int(data.get("priority") or 10),
                    str(data.get("proxy_url", "") or "").strip(),
                    str(data.get("status", "running")).strip().lower() or "running",
                    str(data.get("source", "manual")),
                    now,
                    now,
                ),
            )
            self._conn.commit()
        return self.get_task(task_id)  # type: ignore[return-value]

    def update_task(self, task_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        # Whole read-modify-write under the (re-entrant) lock so concurrent writers can never
        # overwrite a fresher status (e.g. pending_confirm) or ordered_count with stale values.
        with self._lock:
            return self._update_task_locked(task_id, patch)

    def _update_task_locked(self, task_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        existing = self.get_task(task_id)
        if not existing:
            return None
        merged = dict(existing)
        merged.update(patch)
        now = now_iso()

        dcs = merged.get("datacenters") or []
        if isinstance(dcs, str):
            dcs = [x.strip().lower() for x in dcs.split(",") if x.strip()]
        options = merged.get("options") or []
        if isinstance(options, str):
            options = [x.strip() for x in options.split(",") if x.strip()]
        coupons = merged.get("coupons") or []
        if isinstance(coupons, str):
            coupons = [x.strip() for x in coupons.split(",") if x.strip()]
        last_avail = merged.get("last_availability")
        if not isinstance(last_avail, str):
            last_avail = json.dumps(last_avail or {}, ensure_ascii=False)

        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                UPDATE sniper_tasks SET
                    name = ?, account_id = ?, product_line = ?, plan_code = ?,
                    datacenters = ?, memory_filter = ?, storage_filter = ?, options = ?,
                    duration = ?, pricing_mode = ?, os_code = ?, coupons = ?,
                    max_price = ?, price_includes_tax = ?, quantity = ?,
                    max_orders = ?, ordered_count = ?, auto_pay = ?,
                    waive_retractation = ?, dry_run = ?, mode = ?, only_instant = ?,
                    fast_lane = ?, allow_non_default = ?,
                    poll_interval = ?, priority = ?, proxy_url = ?, status = ?, attempts = ?,
                    last_check_at = ?, last_status_msg = ?, last_availability = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    str(merged.get("name", merged["plan_code"])).strip(),
                    str(merged.get("account_id", "")).strip(),
                    str(merged.get("product_line", "eco")).strip().lower(),
                    str(merged.get("plan_code", "")).strip(),
                    json.dumps(dcs, ensure_ascii=False),
                    str(merged.get("memory_filter", "")).strip(),
                    str(merged.get("storage_filter", "")).strip(),
                    json.dumps(options, ensure_ascii=False),
                    str(merged.get("duration", "auto")).strip(),
                    str(merged.get("pricing_mode", "auto")).strip(),
                    str(merged.get("os_code", "none_64.en")).strip(),
                    json.dumps(coupons, ensure_ascii=False),
                    float(merged.get("max_price") or 0),
                    1 if merged.get("price_includes_tax") else 0,
                    max(1, int(merged.get("quantity") or 1)),
                    max(1, int(merged.get("max_orders") or 1)),
                    max(0, int(merged.get("ordered_count") or 0)),
                    1 if merged.get("auto_pay") else 0,
                    1 if merged.get("waive_retractation", True) else 0,
                    1 if merged.get("dry_run") else 0,
                    str(merged.get("mode", "buy")).strip().lower(),
                    1 if merged.get("only_instant") else 0,
                    1 if merged.get("fast_lane") else 0,
                    1 if merged.get("allow_non_default", True) else 0,
                    self._normalize_poll_interval(merged.get("poll_interval"), 5),
                    int(merged.get("priority") or 10),
                    str(merged.get("proxy_url", "") or "").strip(),
                    str(merged.get("status", "running")).strip().lower(),
                    max(0, int(merged.get("attempts") or 0)),
                    str(merged.get("last_check_at", "")),
                    str(merged.get("last_status_msg", "")),
                    last_avail,
                    now,
                    task_id,
                ),
            )
            self._conn.commit()
        return self.get_task(task_id)

    def record_task_order_success(self, task_id: str, status_msg: str) -> Optional[Dict[str, Any]]:
        """Atomically increment ordered_count and mark completed when quota reached."""
        with self._lock:
            task = self.get_task(task_id)
            if not task:
                return None
            new_count = int(task["ordered_count"]) + 1
            new_status = "completed" if new_count >= int(task["max_orders"]) else task["status"]
            return self.update_task(
                task_id,
                {
                    "ordered_count": new_count,
                    "status": new_status,
                    "last_status_msg": status_msg,
                    "last_check_at": now_iso(),
                },
            )

    def delete_task(self, task_id: str) -> bool:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("DELETE FROM sniper_tasks WHERE id = ?", (task_id,))
            self._conn.commit()
            return cur.rowcount > 0

    # ---------- Black Friday Radar Rules ----------

    def _row_to_radar(self, row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        for field in ("datacenters", "known_plan_codes", "matched_plan_codes"):
            try:
                d[field] = json.loads(d[field]) if d[field] else []
            except json.JSONDecodeError:
                d[field] = []
        for bf in ("auto_snipe", "auto_pay", "dry_run", "enabled"):
            d[bf] = bool(d[bf])
        return d

    def list_radar_rules(self) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT * FROM radar_rules ORDER BY created_at DESC")
            return [self._row_to_radar(r) for r in cur.fetchall()]

    def get_radar_rule(self, rule_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT * FROM radar_rules WHERE id = ?", (rule_id,))
            row = cur.fetchone()
            return self._row_to_radar(row) if row else None

    def create_radar_rule(self, data: Dict[str, Any]) -> Dict[str, Any]:
        now = now_iso()
        rule_id = str(uuid.uuid4())[:8]
        dcs = data.get("datacenters") or []
        if isinstance(dcs, str):
            dcs = [x.strip().lower() for x in dcs.split(",") if x.strip()]
        known = data.get("known_plan_codes") or []
        matched = data.get("matched_plan_codes") or []
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO radar_rules (
                    id, name, rule_type, account_id, endpoint, subsidiary,
                    keyword_pattern, memory_pattern, storage_pattern, datacenters,
                    max_price, auto_snipe, auto_pay, dry_run, enabled, proxy_url,
                    known_plan_codes, matched_plan_codes, last_check_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?)
                """,
                (
                    rule_id,
                    str(data.get("name", "黑五雷达规则")).strip() or "黑五雷达规则",
                    str(data.get("rule_type", "new_sku")).strip(),
                    str(data.get("account_id", "")).strip(),
                    str(data.get("endpoint", "ovh-eu")).strip(),
                    str(data.get("subsidiary", "IE")).strip().upper(),
                    str(data.get("keyword_pattern", "")).strip(),
                    str(data.get("memory_pattern", "")).strip(),
                    str(data.get("storage_pattern", "")).strip(),
                    json.dumps(dcs, ensure_ascii=False),
                    float(data.get("max_price") or 0),
                    1 if data.get("auto_snipe") else 0,
                    1 if data.get("auto_pay") else 0,
                    1 if data.get("dry_run", True) else 0,
                    1 if data.get("enabled", True) else 0,
                    str(data.get("proxy_url", "") or "").strip(),
                    json.dumps(known, ensure_ascii=False),
                    json.dumps(matched, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            self._conn.commit()
        return self.get_radar_rule(rule_id)  # type: ignore[return-value]

    def update_radar_rule(self, rule_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        existing = self.get_radar_rule(rule_id)
        if not existing:
            return None
        merged = dict(existing)
        merged.update(patch)
        now = now_iso()
        dcs = merged.get("datacenters") or []
        if isinstance(dcs, str):
            dcs = [x.strip().lower() for x in dcs.split(",") if x.strip()]
        with self._lock:
            self._conn.execute(
                """
                UPDATE radar_rules SET
                    name = ?, rule_type = ?, account_id = ?, endpoint = ?, subsidiary = ?,
                    keyword_pattern = ?, memory_pattern = ?, storage_pattern = ?,
                    datacenters = ?, max_price = ?, auto_snipe = ?, auto_pay = ?,
                    dry_run = ?, enabled = ?, proxy_url = ?, known_plan_codes = ?, matched_plan_codes = ?,
                    last_check_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    str(merged.get("name", "")).strip(),
                    str(merged.get("rule_type", "new_sku")).strip(),
                    str(merged.get("account_id", "")).strip(),
                    str(merged.get("endpoint", "ovh-eu")).strip(),
                    str(merged.get("subsidiary", "IE")).strip().upper(),
                    str(merged.get("keyword_pattern", "")).strip(),
                    str(merged.get("memory_pattern", "")).strip(),
                    str(merged.get("storage_pattern", "")).strip(),
                    json.dumps(dcs, ensure_ascii=False),
                    float(merged.get("max_price") or 0),
                    1 if merged.get("auto_snipe") else 0,
                    1 if merged.get("auto_pay") else 0,
                    1 if merged.get("dry_run") else 0,
                    1 if merged.get("enabled", True) else 0,
                    str(merged.get("proxy_url", "") or "").strip(),
                    json.dumps(merged.get("known_plan_codes") or [], ensure_ascii=False),
                    json.dumps(merged.get("matched_plan_codes") or [], ensure_ascii=False),
                    str(merged.get("last_check_at", "")),
                    now,
                    rule_id,
                ),
            )
            self._conn.commit()
        return self.get_radar_rule(rule_id)

    def delete_radar_rule(self, rule_id: str) -> bool:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("DELETE FROM radar_rules WHERE id = ?", (rule_id,))
            self._conn.commit()
            return cur.rowcount > 0

    # ---------- VPS Watches ----------

    def _row_to_vps(self, row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        try:
            d["datacenters"] = json.loads(d["datacenters"]) if d["datacenters"] else []
        except json.JSONDecodeError:
            d["datacenters"] = []
        try:
            d["last_state"] = json.loads(d["last_state"]) if d["last_state"] else {}
        except json.JSONDecodeError:
            d["last_state"] = {}
        for bf in ("auto_order", "auto_pay", "dry_run", "enabled"):
            d[bf] = bool(d[bf])
        return d

    def list_vps_watches(self) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT * FROM vps_watches ORDER BY created_at DESC")
            return [self._row_to_vps(r) for r in cur.fetchall()]

    def create_vps_watch(self, data: Dict[str, Any]) -> Dict[str, Any]:
        now = now_iso()
        wid = str(uuid.uuid4())[:8]
        dcs = data.get("datacenters") or []
        if isinstance(dcs, str):
            dcs = [x.strip().upper() for x in dcs.split(",") if x.strip()]
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO vps_watches (
                    id, plan_code, account_id, endpoint, subsidiary, datacenters,
                    auto_order, auto_pay, dry_run, max_orders, ordered_count,
                    enabled, proxy_url, last_state, last_check_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, '{}', '', ?, ?)
                """,
                (
                    wid,
                    str(data.get("plan_code", "")).strip(),
                    str(data.get("account_id", "")).strip(),
                    str(data.get("endpoint", "ovh-eu")).strip(),
                    str(data.get("subsidiary", "IE")).strip().upper(),
                    json.dumps(dcs, ensure_ascii=False),
                    1 if data.get("auto_order") else 0,
                    1 if data.get("auto_pay") else 0,
                    1 if data.get("dry_run", True) else 0,
                    max(1, int(data.get("max_orders") or 1)),
                    1 if data.get("enabled", True) else 0,
                    str(data.get("proxy_url", "") or "").strip(),
                    now,
                    now,
                ),
            )
            self._conn.commit()
        return self.get_vps_watch(wid)  # type: ignore[return-value]

    def get_vps_watch(self, watch_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT * FROM vps_watches WHERE id = ?", (watch_id,))
            row = cur.fetchone()
            return self._row_to_vps(row) if row else None

    def update_vps_watch(self, watch_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        existing = self.get_vps_watch(watch_id)
        if not existing:
            return None
        merged = dict(existing)
        merged.update(patch)
        now = now_iso()
        dcs = merged.get("datacenters") or []
        if isinstance(dcs, str):
            dcs = [x.strip().upper() for x in dcs.split(",") if x.strip()]
        with self._lock:
            self._conn.execute(
                """
                UPDATE vps_watches SET
                    plan_code = ?, account_id = ?, endpoint = ?, subsidiary = ?,
                    datacenters = ?, auto_order = ?, auto_pay = ?, dry_run = ?,
                    max_orders = ?, ordered_count = ?, enabled = ?, proxy_url = ?,
                    last_state = ?, last_check_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    str(merged.get("plan_code", "")).strip(),
                    str(merged.get("account_id", "")).strip(),
                    str(merged.get("endpoint", "ovh-eu")).strip(),
                    str(merged.get("subsidiary", "IE")).strip().upper(),
                    json.dumps(dcs, ensure_ascii=False),
                    1 if merged.get("auto_order") else 0,
                    1 if merged.get("auto_pay") else 0,
                    1 if merged.get("dry_run") else 0,
                    max(1, int(merged.get("max_orders") or 1)),
                    max(0, int(merged.get("ordered_count") or 0)),
                    1 if merged.get("enabled", True) else 0,
                    str(merged.get("proxy_url", "") or "").strip(),
                    json.dumps(merged.get("last_state") or {}, ensure_ascii=False),
                    str(merged.get("last_check_at", "")),
                    now,
                    watch_id,
                ),
            )
            self._conn.commit()
        return self.get_vps_watch(watch_id)

    def delete_vps_watch(self, watch_id: str) -> bool:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("DELETE FROM vps_watches WHERE id = ?", (watch_id,))
            self._conn.commit()
            return cur.rowcount > 0

    # ---------- Orders ----------

    def _row_to_order(self, row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        try:
            d["options"] = json.loads(d["options"]) if d.get("options") else []
        except json.JSONDecodeError:
            d["options"] = []
        try:
            d["spec"] = json.loads(d["spec_json"]) if d.get("spec_json") else {}
        except json.JSONDecodeError:
            d["spec"] = {}
        d["dry_run"] = bool(d.get("dry_run"))
        return d

    def add_order(self, data: Dict[str, Any]) -> Dict[str, Any]:
        oid = data.get("id") or str(uuid.uuid4())[:12]
        now = now_iso()
        options = data.get("options") or []
        is_real_success = data.get("status") == "success" and not data.get("dry_run") and bool(data.get("order_id"))
        track_state = data.get("track_state")
        if track_state is None:
            track_state = "tracking" if is_real_success else ""
        spec_json = data.get("spec_json")
        if not isinstance(spec_json, str):
            spec_json = json.dumps(data.get("spec") or {}, ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO orders (
                    id, task_id, account_id, account_name, product_line, plan_code,
                    datacenter, options, memory, storage, status, dry_run, order_id, order_url,
                    price_without_tax, price_with_tax, currency, error_message,
                    track_state, ovh_status, server_name, server_ip, spec_json,
                    notified_events, next_check_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    oid,
                    str(data.get("task_id", "")),
                    str(data.get("account_id", "")),
                    str(data.get("account_name", "")),
                    str(data.get("product_line", "eco")),
                    str(data.get("plan_code", "")),
                    str(data.get("datacenter", "")),
                    json.dumps(options, ensure_ascii=False),
                    str(data.get("memory", "")),
                    str(data.get("storage", "")),
                    str(data.get("status", "failed")),
                    1 if data.get("dry_run") else 0,
                    str(data.get("order_id", "")),
                    str(data.get("order_url", "")),
                    data.get("price_without_tax"),
                    data.get("price_with_tax"),
                    str(data.get("currency", "")),
                    str(data.get("error_message", "")),
                    str(track_state),
                    str(data.get("ovh_status", "")),
                    str(data.get("server_name", "")),
                    str(data.get("server_ip", "")),
                    spec_json,
                    str(data.get("notified_events", "")),
                    float(data.get("next_check_at") or 0),
                    now,
                ),
            )
            self._conn.commit()
        return self.get_order(oid)  # type: ignore[return-value]

    def update_order(self, oid: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        existing = self.get_order(oid)
        if not existing:
            return None
        merged = dict(existing)
        merged.update(patch)
        spec_json = merged.get("spec_json")
        if "spec" in patch and "spec_json" not in patch:
            spec_json = json.dumps(patch.get("spec") or {}, ensure_ascii=False)
        elif not isinstance(spec_json, str):
            spec_json = json.dumps(spec_json or {}, ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                """
                UPDATE orders SET
                    status = ?, order_id = ?, order_url = ?, error_message = ?,
                    track_state = ?, ovh_status = ?, server_name = ?, server_ip = ?,
                    spec_json = ?, notified_events = ?, next_check_at = ?
                WHERE id = ?
                """,
                (
                    str(merged.get("status", "")),
                    str(merged.get("order_id", "")),
                    str(merged.get("order_url", "")),
                    str(merged.get("error_message", "")),
                    str(merged.get("track_state", "")),
                    str(merged.get("ovh_status", "")),
                    str(merged.get("server_name", "")),
                    str(merged.get("server_ip", "")),
                    spec_json,
                    str(merged.get("notified_events", "")),
                    float(merged.get("next_check_at") or 0),
                    oid,
                ),
            )
            self._conn.commit()
        return self.get_order(oid)

    def get_order(self, oid: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT * FROM orders WHERE id = ?", (oid,))
            row = cur.fetchone()
            return self._row_to_order(row) if row else None

    def list_orders(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 500)),))
            return [self._row_to_order(row) for row in cur.fetchall()]

    def list_due_tracking_orders(self, limit: int = 6) -> List[Dict[str, Any]]:
        now_ts = time.time()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT * FROM orders WHERE track_state = 'tracking' AND order_id != '' AND next_check_at <= ? "
                "ORDER BY next_check_at ASC LIMIT ?",
                (now_ts, limit),
            )
            return [self._row_to_order(row) for row in cur.fetchall()]

    def list_linked_server_names(self, account_id: str, exclude_order_id: str = "") -> set:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT server_name FROM orders WHERE account_id = ? AND server_name != '' AND id != ?",
                (account_id, exclude_order_id),
            )
            return {row["server_name"] for row in cur.fetchall()}

    def clear_orders(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM orders")
            self._conn.commit()

    # ---------- Ephemeral KV Cache (Telegram callback UUIDs, etc.) ----------

    def kv_set(self, key: str, value: Any, ttl_seconds: int = 86400) -> None:
        expires_at = time.time() + ttl_seconds
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv_cache(key, value, expires_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, expires_at = excluded.expires_at",
                (key, json.dumps(value, ensure_ascii=False), expires_at),
            )
            self._conn.commit()

    def kv_pop(self, key: str) -> Optional[Any]:
        """Atomically read AND delete a key (single-use tokens, e.g. Telegram buy buttons)."""
        with self._lock:
            val = self.kv_get(key)
            self._conn.execute("DELETE FROM kv_cache WHERE key = ?", (key,))
            self._conn.commit()
            return val

    def kv_get(self, key: str) -> Optional[Any]:
        now = time.time()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT value, expires_at FROM kv_cache WHERE key = ?", (key,))
            row = cur.fetchone()
            if not row:
                return None
            if row["expires_at"] < now:
                cur.execute("DELETE FROM kv_cache WHERE key = ?", (key,))
                self._conn.commit()
                return None
            try:
                return json.loads(row["value"])
            except json.JSONDecodeError:
                return None

    # ---------- Stats & Export/Import ----------

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT status, COUNT(*) AS cnt FROM sniper_tasks GROUP BY status")
            task_counts = {r["status"]: r["cnt"] for r in cur.fetchall()}
            cur.execute("SELECT COUNT(*) FROM accounts")
            accounts_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM orders WHERE status = 'success' AND dry_run = 0")
            real_orders = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM orders WHERE status = 'dry_run'")
            dry_run_orders = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM orders WHERE status = 'failed'")
            failed_orders = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM radar_rules WHERE enabled = 1")
            active_radars = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM vps_watches WHERE enabled = 1")
            active_vps = cur.fetchone()[0]
            return {
                "accounts_count": accounts_count,
                "tasks_running": task_counts.get("running", 0),
                "tasks_paused": task_counts.get("paused", 0),
                "tasks_completed": task_counts.get("completed", 0),
                "tasks_total": sum(task_counts.values()),
                "orders_success": real_orders,
                "orders_dry_run": dry_run_orders,
                "orders_failed": failed_orders,
                "active_radars": active_radars,
                "active_vps": active_vps,
            }

    def export_config(self, include_secrets: bool = False) -> Dict[str, Any]:
        settings = dict(self.get_settings())
        settings.pop("panel_password_hash", None)
        settings.pop("panel_signing_secret", None)
        accounts = self.list_accounts(mask=not include_secrets)
        tasks = self.list_tasks()
        radar_rules = self.list_radar_rules()
        vps_watches = self.list_vps_watches()
        if not include_secrets:
            for sk in EXPORT_SECRET_SETTING_KEYS:
                val = str(settings.get(sk) or "")
                settings[sk] = mask_secret(val) if val else ""
            for pk in ("http_proxy", "monitor_proxies", "telegram_proxy"):
                if settings.get(pk):
                    settings[pk] = redact_proxy_list_text(str(settings[pk]))
            for collection in (accounts, tasks, radar_rules, vps_watches):
                for item in collection:
                    if item.get("proxy_url"):
                        item["proxy_url"] = redact_proxy_list_text(str(item["proxy_url"]))
        return {
            "exported_at": now_iso(),
            "settings": settings,
            "accounts": accounts,
            "tasks": tasks,
            "radar_rules": radar_rules,
            "vps_watches": vps_watches,
        }

    def seed_from_env_or_file(self, seed_file: Optional[Path] = None) -> None:
        """Seed initial account & tasks from .env or config.json when DB is empty."""
        # 1. Seed account from env vars if no accounts exist yet
        if not self.list_accounts(mask=True):
            ak = os.environ.get("OVH_APP_KEY", "").strip()
            ask = os.environ.get("OVH_APP_SECRET", "").strip()
            ck = os.environ.get("OVH_CONSUMER_KEY", "").strip()
            if ak and ask and ck:
                self.create_account(
                    {
                        "name": os.environ.get("OVH_ACCOUNT_NAME", "环境变量账号"),
                        "endpoint": os.environ.get("OVH_ENDPOINT", "ovh-eu"),
                        "subsidiary": os.environ.get("OVH_SUBSIDIARY", "IE"),
                        "app_key": ak,
                        "app_secret": ask,
                        "consumer_key": ck,
                        "is_default": True,
                    }
                )
                self.add_log("INFO", "已从环境变量自动导入默认 OVH 账号", "system")

        # 2. Seed from config.json (data/config.json or ./config.json) if present and no tasks exist
        candidates = []
        if seed_file:
            candidates.append(seed_file)
        candidates.extend([get_data_dir() / "config.json", Path(os.environ.get("OVH_CONFIG_FILE", ""))])
        for path in candidates:
            if not path or not str(path) or not path.is_file():
                continue
            marker_key = f"seeded:{path.resolve()}"
            if self.kv_get(marker_key):
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw.get("settings"), dict):
                    self.update_settings(raw["settings"])
                existing_acc_names = {a["name"] for a in self.list_accounts(mask=True)}
                acc_name_to_id = {a["name"]: a["id"] for a in self.list_accounts(mask=True)}
                for acc in raw.get("accounts", []):
                    if acc.get("name") not in existing_acc_names and acc.get("app_key"):
                        created = self.create_account(acc)
                        acc_name_to_id[created["name"]] = created["id"]
                if not self.list_tasks():
                    for task in raw.get("tasks", []):
                        if not task.get("account_id") and task.get("account_name") in acc_name_to_id:
                            task["account_id"] = acc_name_to_id[task["account_name"]]
                        if task.get("plan_code"):
                            self.create_task(task)
                for rule in raw.get("radar_rules", []):
                    if rule.get("name"):
                        self.create_radar_rule(rule)
                for vw in raw.get("vps_watches", []):
                    if vw.get("plan_code"):
                        self.create_vps_watch(vw)
                self.kv_set(marker_key, True, ttl_seconds=365 * 86400)
                self.add_log("INFO", f"已从配置文件 {path.name} 导入初始配置", "system")
            except Exception as exc:
                self.add_log("WARNING", f"读取初始配置文件 {path} 失败: {exc}", "system")
            break
