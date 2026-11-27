"""Zero-dependency HTTP REST API, SSE live log stream, and embedded Web UI server."""

import http.server
import ipaddress
import json
import mimetypes
import queue
import socketserver
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import __app_name__, __version__
from .auth import SESSION_COOKIE_NAME, SESSION_TTL_SECONDS, PanelAuth
from .catalog import CatalogManager
from .config import DATACENTERS, DEFAULT_VPS_MODELS, ENDPOINTS, OVH_ORDER_STATUS_LABELS
from .database import Database, mask_secret, now_iso
from .notifier import Notifier
from .order_engine import OrderEngine
from .sniper import SniperEngine
from .specs import GLOBAL_FX


WEB_ROOT = Path(__file__).resolve().parent / "web"

LOGIN_MAX_FAILURES = 8
LOGIN_LOCK_SECONDS = 300


def _safe_int(raw: str, default: int, lo: int = 1, hi: int = 1000) -> int:
    try:
        return max(lo, min(hi, int(str(raw).strip())))
    except (TypeError, ValueError):
        return default


SECRET_SETTING_KEYS = {
    "telegram_bot_token",
    "bark_url",
    "serverchan_key",
    "dingtalk_webhook",
    "dingtalk_secret",
    "feishu_webhook",
    "discord_webhook",
    "custom_webhook_url",
}


class AppContext:
    """Shared service container bound to the HTTP server instance."""

    def __init__(
        self,
        db: Database,
        catalog_manager: CatalogManager,
        order_engine: OrderEngine,
        notifier: Notifier,
        engine: SniperEngine,
        auth: PanelAuth,
    ):
        self.db = db
        self.catalog_manager = catalog_manager
        self.order_engine = order_engine
        self.notifier = notifier
        self.engine = engine
        self.auth = auth
        self.login_failures: Dict[str, Tuple[int, float]] = {}
        self.login_lock = threading.Lock()


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: Tuple[str, int], handler_cls, ctx: AppContext):
        self.ctx = ctx
        super().__init__(server_address, handler_cls)


class RequestHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Silence verbose per-request stdout spam; errors still go to DB logs
        return

    @property
    def ctx(self) -> AppContext:
        return self.server.ctx  # type: ignore[attr-defined]

    def _send_json(
        self,
        payload: Any,
        status: int = 200,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 2 * 1024 * 1024:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON 请求体必须为对象")
        return data

    def _serve_static(self, req_path: str) -> None:
        rel = req_path.lstrip("/") or "index.html"
        root = WEB_ROOT.resolve()
        target = (WEB_ROOT / rel).resolve()
        try:
            target.relative_to(root)
            inside = True
        except ValueError:
            inside = False
        if not inside or not target.is_file():
            target = WEB_ROOT / "index.html"
        if not target.is_file():
            self._send_json({"error": "Web UI assets missing"}, status=404)
            return
        mime, _ = mimetypes.guess_type(str(target))
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{mime or 'application/octet-stream'}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _client_ip(self) -> str:
        peer = str(self.client_address[0] if self.client_address else "?")
        try:
            ip_obj = ipaddress.ip_address(peer)
            trusted_proxy = ip_obj.is_loopback or ip_obj.is_private
        except ValueError:
            trusted_proxy = False
        if trusted_proxy:
            cf_ip = str(self.headers.get("CF-Connecting-IP") or "").strip()
            if cf_ip:
                return cf_ip
            xff = str(self.headers.get("X-Forwarded-For") or "").strip()
            if xff:
                return xff.split(",")[0].strip() or peer
        return peer

    def _is_https_request(self) -> bool:
        if str(self.headers.get("X-Forwarded-Proto") or "").lower() == "https":
            return True
        if '"https"' in str(self.headers.get("CF-Visitor") or "").lower():
            return True
        return False

    def _session_cookie(self) -> str:
        token = self.ctx.auth.create_session_token()
        secure = "; Secure" if self._is_https_request() else ""
        return f"{SESSION_COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL_SECONDS}{secure}"

    def _check_auth(self, path: str) -> bool:
        public_api = {"/api/health", "/api/auth/status", "/api/auth/login", "/api/auth/logout"}
        if not path.startswith("/api/") or path in public_api:
            return True
        if self.ctx.auth.is_request_authorized(dict(self.headers)):
            return True
        self._send_json({"error": "未授权，请先登录面板", "auth_required": True}, status=401)
        return False

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        if not path.startswith("/api/"):
            self._serve_static(path)
            return
        if not self._check_auth(path):
            return
        try:
            self._route_get(path, qs)
        except Exception as exc:
            self.ctx.db.add_log("ERROR", f"API GET {path} 异常: {exc}", "api")
            self._send_json({"error": str(exc)}, status=500)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if not self._check_auth(path):
            return
        try:
            body = self._read_json_body()
            self._route_post(path, body)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
        except Exception as exc:
            self.ctx.db.add_log("ERROR", f"API POST {path} 异常: {exc}", "api")
            self._send_json({"error": str(exc)}, status=500)

    def do_PUT(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if not self._check_auth(path):
            return
        try:
            body = self._read_json_body()
            self._route_put(path, body)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
        except Exception as exc:
            self.ctx.db.add_log("ERROR", f"API PUT {path} 异常: {exc}", "api")
            self._send_json({"error": str(exc)}, status=500)

    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if not self._check_auth(path):
            return
        try:
            self._route_delete(path)
        except Exception as exc:
            self.ctx.db.add_log("ERROR", f"API DELETE {path} 异常: {exc}", "api")
            self._send_json({"error": str(exc)}, status=500)

    # ---------- GET routes ----------

    def _route_get(self, path: str, qs: Dict[str, list]) -> None:
        def q1(key: str, default: str = "") -> str:
            vals = qs.get(key)
            return vals[0] if vals else default

        if path == "/api/health":
            self._send_json({"ok": True, "app": __app_name__, "version": __version__, "time": now_iso()})
            return

        if path == "/api/auth/status":
            enabled = self.ctx.auth.is_auth_enabled()
            authed = self.ctx.auth.is_request_authorized(dict(self.headers))
            self._send_json({"auth_enabled": enabled, "authenticated": authed})
            return

        if path == "/api/meta":
            self._send_json(
                {
                    "app": __app_name__,
                    "version": __version__,
                    "endpoints": ENDPOINTS,
                    "datacenters": DATACENTERS,
                    "default_vps_models": DEFAULT_VPS_MODELS,
                    "order_status_labels": OVH_ORDER_STATUS_LABELS,
                }
            )
            return

        if path == "/api/fx":
            self._send_json({"rates": GLOBAL_FX.get_rates()})
            return

        if path == "/api/status":
            self._send_json(self.ctx.engine.get_status())
            return

        if path == "/api/accounts":
            self._send_json({"items": self.ctx.db.list_accounts(mask=True)})
            return

        if path.startswith("/api/accounts/") and path.endswith("/ovh-orders"):
            acc_id = path.split("/")[3]
            acc = self.ctx.db.get_account(acc_id, mask=False)
            if not acc:
                self._send_json({"error": "账号不存在"}, status=404)
                return
            client = self.ctx.engine.build_client(account=acc)
            orders = client.get_recent_orders(limit=15)
            self._send_json({"items": orders})
            return

        if path.startswith("/api/accounts/") and path.endswith("/servers"):
            acc_id = path.split("/")[3]
            acc = self.ctx.db.get_account(acc_id, mask=False)
            if not acc:
                self._send_json({"error": "账号不存在"}, status=404)
                return
            servers = self.ctx.engine.list_account_servers(acc_id)
            self._send_json({"items": servers})
            return

        if path == "/api/catalog":
            endpoint = q1("endpoint", "ovh-eu")
            subsidiary = q1("subsidiary", "IE").upper()
            product_line = q1("product_line", "eco").lower()
            search = q1("search", "").strip().lower()
            force = q1("refresh", "0") in ("1", "true", "yes")
            plans = self.ctx.engine.run_monitor_call(
                endpoint,
                lambda cli: self.ctx.catalog_manager.get_catalog_plans(
                    cli, subsidiary=subsidiary, product_line=product_line, force_refresh=force
                ),
            )
            if search:
                plans = [
                    p
                    for p in plans
                    if search in p["planCode"].lower()
                    or search in p["name"].lower()
                    or search in p["cpu"].lower()
                    or search in p["memory"].lower()
                    or search in p["storage"].lower()
                ]
            self._send_json({"items": plans, "total": len(plans), "endpoint": endpoint, "subsidiary": subsidiary})
            return

        if path.startswith("/api/availability/"):
            plan_code = urllib.parse.unquote(path[len("/api/availability/") :]).strip()
            endpoint = q1("endpoint", "ovh-eu")
            subsidiary = q1("subsidiary", "IE").upper()
            items = self.ctx.engine.check_plan_live(plan_code, endpoint=endpoint, subsidiary=subsidiary)
            self._send_json({"planCode": plan_code, "endpoint": endpoint, "subsidiary": subsidiary, "items": items})
            return

        if path == "/api/tasks":
            tasks = self.ctx.db.list_tasks()
            for t in tasks:
                t["fast_lane_slots"] = self.ctx.engine.slot_pool.get_task_slots_summary(t["id"])
            self._send_json({"items": tasks})
            return

        if path == "/api/radar":
            self._send_json({"items": self.ctx.db.list_radar_rules()})
            return

        if path == "/api/vps":
            self._send_json({"items": self.ctx.db.list_vps_watches()})
            return

        if path == "/api/orders":
            limit = _safe_int(q1("limit", "100"), 100, 1, 1000)
            self._send_json({"items": self.ctx.db.list_orders(limit=limit)})
            return

        if path == "/api/settings":
            raw = self.ctx.db.get_settings()
            safe = dict(raw)
            safe.pop("panel_password_hash", None)
            safe["has_panel_password"] = self.ctx.auth.is_auth_enabled()
            for sk in SECRET_SETTING_KEYS:
                val = str(safe.get(sk) or "")
                safe[f"{sk}_set"] = bool(val)
                safe[sk] = mask_secret(val) if val else ""
            self._send_json(safe)
            return

        if path == "/api/logs":
            limit = _safe_int(q1("limit", "200"), 200, 1, 2000)
            level = q1("level", "")
            source = q1("source", "")
            search = q1("search", "")
            self._send_json({"items": self.ctx.db.get_logs(limit=limit, level=level, source=source, search=search)})
            return

        if path == "/api/logs/stream":
            self._handle_sse_logs()
            return

        if path == "/api/export":
            payload = self.ctx.db.export_config(include_secrets=False)
            self._send_json(payload)
            return

        self._send_json({"error": "Not Found"}, status=404)

    def _handle_sse_logs(self) -> None:
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()

        q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=200)

        def listener(entry: Dict[str, Any]) -> None:
            try:
                q.put_nowait(entry)
            except queue.Full:
                pass

        self.ctx.db.register_log_listener(listener)
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    item = q.get(timeout=15.0)
                    chunk = f"data: {json.dumps(item, ensure_ascii=False)}\n\n".encode("utf-8")
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.ctx.db.unregister_log_listener(listener)

    # ---------- POST routes ----------

    def _route_post(self, path: str, body: Dict[str, Any]) -> None:
        if path == "/api/auth/login":
            ip = self._client_ip()
            now = time.time()
            with self.ctx.login_lock:
                fails, until = self.ctx.login_failures.get(ip, (0, 0.0))
            if until > now:
                self._send_json({"error": f"登录失败次数过多，请 {int(until - now)} 秒后再试"}, status=429)
                return
            password = str(body.get("password") or "")
            if not self.ctx.auth.verify_secret(password):
                with self.ctx.login_lock:
                    fails += 1
                    lock_until = now + LOGIN_LOCK_SECONDS if fails >= LOGIN_MAX_FAILURES else 0.0
                    self.ctx.login_failures[ip] = (0 if lock_until else fails, lock_until)
                time.sleep(0.4)
                self._send_json({"error": "密码错误"}, status=401)
                return
            with self.ctx.login_lock:
                self.ctx.login_failures.pop(ip, None)
            self._send_json({"ok": True}, extra_headers={"Set-Cookie": self._session_cookie()})
            return

        if path == "/api/auth/logout":
            secure = "; Secure" if self._is_https_request() else ""
            cookie = f"{SESSION_COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0{secure}"
            self._send_json({"ok": True}, extra_headers={"Set-Cookie": cookie})
            return

        if path == "/api/auth/password":
            new_pw = str(body.get("password") or "").strip()
            self.ctx.auth.set_panel_password(new_pw)
            self.ctx.db.add_log("INFO", "面板访问密码已更新（旧登录会话已全部失效）", "auth")
            # Signing secret was rotated: hand the current operator a fresh session cookie
            extra = {"Set-Cookie": self._session_cookie()} if self.ctx.auth.is_auth_enabled() else None
            self._send_json({"ok": True, "auth_enabled": self.ctx.auth.is_auth_enabled()}, extra_headers=extra)
            return

        if path == "/api/engine/toggle":
            patch = {}
            for k in ("sniper_enabled", "fast_lane_enabled", "tracker_enabled", "radar_enabled", "vps_monitor_enabled"):
                if k in body:
                    patch[k] = bool(body[k])
            if patch:
                self.ctx.db.update_settings(patch)
                if patch.get("fast_lane_enabled") is False:
                    self.ctx.engine.slot_pool.cleanup_all()
                self.ctx.db.add_log("INFO", f"引擎开关更新: {patch}", "engine")
            self._send_json(self.ctx.engine.get_status())
            return

        if path == "/api/accounts":
            for req_field in ("app_key", "app_secret", "consumer_key"):
                if not str(body.get(req_field) or "").strip():
                    self._send_json({"error": f"缺少必填字段: {req_field}"}, status=400)
                    return
            acc = self.ctx.db.create_account(body)
            self.ctx.db.add_log("INFO", f"添加 OVH 账号: {acc['name']} ({acc['endpoint']}/{acc['subsidiary']})", "account")
            self._send_json(acc, status=201)
            return

        if path.startswith("/api/accounts/") and path.endswith("/test"):
            acc_id = path.split("/")[3]
            acc = self.ctx.db.get_account(acc_id, mask=False)
            if not acc:
                self._send_json({"error": "账号不存在"}, status=404)
                return
            client = self.ctx.engine.build_client(account=acc)
            info = client.verify_credentials()
            self.ctx.db.update_account(
                acc_id,
                {
                    "nichandle": info.get("nichandle", ""),
                    "currency": info.get("currency", "EUR"),
                    "default_payment_type": info.get("default_payment_type") or "",
                    "last_verified_at": now_iso(),
                },
            )
            self.ctx.db.add_log(
                "SUCCESS",
                f"账号 {acc['name']} 验证成功: {info.get('nichandle')} (默认支付: {info.get('default_payment_type') or '未绑定'})",
                "account",
            )
            self._send_json({"ok": True, "info": info})
            return

        if path.startswith("/api/orders/") and path.endswith("/track"):
            oid = path.split("/")[3]
            res = self.ctx.engine.tracker.check_order(oid)
            self._send_json({"ok": "error" not in res, "result": res})
            return

        if path == "/api/short-link/preview":
            url = str(body.get("url") or "").strip()
            if not url:
                self._send_json({"error": "请粘贴 /o/<region>/<token> 短链"}, status=400)
                return
            res = self.ctx.engine.preview_short_order_link(url, account_id=str(body.get("account_id") or ""))
            self._send_json({"ok": True, "preview": res})
            return

        if path == "/api/short-link/confirm":
            tok = str(body.get("token") or "").strip()
            if not tok:
                self._send_json({"error": "缺少预览 token"}, status=400)
                return
            res = self.ctx.engine.confirm_short_order_link(tok, auto_pay=bool(body.get("auto_pay", False)))
            self._send_json(res, status=200 if res.get("success") or res.get("status") == "pending_confirm" else 400)
            return

        if path == "/api/short-link/cancel":
            tok = str(body.get("token") or "").strip()
            ok = self.ctx.engine.cancel_short_order_link(tok)
            self._send_json({"ok": ok})
            return

        if path == "/api/price-check":
            account_id = str(body.get("account_id") or "")
            acc = self.ctx.engine.get_account_for_id(account_id)
            if not acc or not acc.get("app_key"):
                self._send_json({"error": "请先配置至少一个 OVH API 账号再查实时含税价"}, status=400)
                return
            plan_code = str(body.get("plan_code") or "").strip()
            datacenter = str(body.get("datacenter") or "gra").strip()
            if not plan_code:
                self._send_json({"error": "缺少 plan_code"}, status=400)
                return
            client = self.ctx.engine.build_client(account=acc)
            res = self.ctx.order_engine.place_order(
                client=client,
                subsidiary=acc["subsidiary"],
                plan_code=plan_code,
                datacenter=datacenter,
                product_line=str(body.get("product_line") or "eco"),
                options=body.get("options") or [],
                duration=str(body.get("duration") or "auto"),
                pricing_mode=str(body.get("pricing_mode") or "auto"),
                coupons=body.get("coupons") or [],
                dry_run=True,
            )
            self._send_json(res)
            return

        if path == "/api/tasks":
            if not str(body.get("plan_code") or "").strip():
                self._send_json({"error": "缺少目标型号 plan_code"}, status=400)
                return
            task = self.ctx.db.create_task(body)
            self.ctx.db.add_log("INFO", f"创建抢购任务: {task['name']} ({task['plan_code']})", "sniper")
            self._send_json(task, status=201)
            return

        if path.startswith("/api/tasks/") and path.endswith("/run"):
            task_id = path.split("/")[3]
            force_dry = bool(body.get("dry_run", False))
            res = self.ctx.engine.execute_task_now(task_id, force_dry_run=force_dry)
            self._send_json(res)
            return

        if path == "/api/radar":
            rule = self.ctx.db.create_radar_rule(body)
            self.ctx.db.add_log("INFO", f"创建黑五雷达规则: {rule['name']}", "radar")
            self._send_json(rule, status=201)
            return

        if path == "/api/radar/scan":
            hits = self.ctx.engine.run_radar_tick()
            self._send_json({"ok": True, "hits": hits})
            return

        if path == "/api/vps":
            if not str(body.get("plan_code") or "").strip():
                self._send_json({"error": "缺少 VPS plan_code"}, status=400)
                return
            w = self.ctx.db.create_vps_watch(body)
            self.ctx.db.add_log("INFO", f"添加 VPS 监控: {w['plan_code']} ({w['subsidiary']})", "vps")
            self._send_json(w, status=201)
            return

        if path == "/api/vps/check":
            plan_code = str(body.get("plan_code") or "").strip()
            if plan_code:
                endpoint = str(body.get("endpoint") or "ovh-eu")
                subsidiary = str(body.get("subsidiary") or "IE").upper()
                data = self.ctx.engine.run_monitor_call(
                    endpoint,
                    lambda cli: cli.get_vps_datacenter_availability(plan_code, subsidiary=subsidiary),
                )
                self._send_json({"ok": True, "plan_code": plan_code, "data": data})
            else:
                cnt = self.ctx.engine.run_vps_tick()
                self._send_json({"ok": True, "checked": cnt})
            return

        if path == "/api/proxy/test":
            raw_proxies = str(body.get("proxies") or "")
            endpoint = str(body.get("endpoint") or "ovh-eu")
            res = self.ctx.engine.test_proxies(raw_proxies=raw_proxies, endpoint=endpoint)
            self._send_json(res, status=200 if res.get("results") else 400)
            return

        if path == "/api/notifications/test":
            res = self.ctx.notifier.notify(
                title="🔔 OVH Sniper 测试通知",
                body=f"这是一条测试通知，说明你的推送渠道配置正常！\n时间: {now_iso()}",
                event="test",
            )
            self._send_json({"ok": any(res.values()), "results": res})
            return

        self._send_json({"error": "Not Found"}, status=404)

    # ---------- PUT routes ----------

    def _route_put(self, path: str, body: Dict[str, Any]) -> None:
        if path.startswith("/api/accounts/"):
            acc_id = path.split("/")[3]
            updated = self.ctx.db.update_account(acc_id, body)
            if not updated:
                self._send_json({"error": "账号不存在"}, status=404)
                return
            self._send_json(updated)
            return

        if path.startswith("/api/tasks/"):
            task_id = path.split("/")[3]
            updated = self.ctx.db.update_task(task_id, body)
            if not updated:
                self._send_json({"error": "任务不存在"}, status=404)
                return
            if updated["status"] != "running" or not updated.get("fast_lane"):
                self.ctx.engine.slot_pool.cleanup_task(task_id)
            self._send_json(updated)
            return

        if path.startswith("/api/radar/"):
            rule_id = path.split("/")[3]
            updated = self.ctx.db.update_radar_rule(rule_id, body)
            if not updated:
                self._send_json({"error": "规则不存在"}, status=404)
                return
            self._send_json(updated)
            return

        if path.startswith("/api/vps/"):
            watch_id = path.split("/")[3]
            updated = self.ctx.db.update_vps_watch(watch_id, body)
            if not updated:
                self._send_json({"error": "监控项不存在"}, status=404)
                return
            self._send_json(updated)
            return

        if path == "/api/settings":
            patch = dict(body)
            patch.pop("panel_password_hash", None)
            # Keep existing secret if UI submits masked placeholder
            existing = self.ctx.db.get_settings()
            for sk in SECRET_SETTING_KEYS:
                if sk in patch and isinstance(patch[sk], str) and "****" in patch[sk]:
                    patch[sk] = existing.get(sk, "")
            self.ctx.db.update_settings(patch)
            self.ctx.db.add_log("INFO", "系统与通知设置已保存", "settings")
            self._send_json({"ok": True})
            return

        self._send_json({"error": "Not Found"}, status=404)

    # ---------- DELETE routes ----------

    def _route_delete(self, path: str) -> None:
        if path.startswith("/api/accounts/"):
            acc_id = path.split("/")[3]
            ok = self.ctx.db.delete_account(acc_id)
            self._send_json({"ok": ok}, status=200 if ok else 404)
            return

        if path.startswith("/api/tasks/"):
            task_id = path.split("/")[3]
            self.ctx.engine.slot_pool.cleanup_task(task_id)
            ok = self.ctx.db.delete_task(task_id)
            self._send_json({"ok": ok}, status=200 if ok else 404)
            return

        if path.startswith("/api/radar/"):
            rule_id = path.split("/")[3]
            ok = self.ctx.db.delete_radar_rule(rule_id)
            self._send_json({"ok": ok}, status=200 if ok else 404)
            return

        if path.startswith("/api/vps/"):
            watch_id = path.split("/")[3]
            ok = self.ctx.db.delete_vps_watch(watch_id)
            self._send_json({"ok": ok}, status=200 if ok else 404)
            return

        if path == "/api/orders":
            self.ctx.db.clear_orders()
            self._send_json({"ok": True})
            return

        if path == "/api/logs":
            self.ctx.db.clear_logs()
            self._send_json({"ok": True})
            return

        self._send_json({"error": "Not Found"}, status=404)


def create_http_server(host: str, port: int, ctx: AppContext) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), RequestHandler, ctx)
