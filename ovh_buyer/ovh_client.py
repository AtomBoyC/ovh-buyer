"""Zero-dependency OVHcloud API client (1.0 & v1) with SHA-1 signing, clock sync, rate limiting, and proxy support."""

import hashlib
import http.client
import json
import random
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

from .config import ENDPOINTS


class OVHAPIError(Exception):
    """Error returned by OVHcloud API or HTTP transport."""

    def __init__(self, message: str, status_code: int = 0, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload

    @property
    def is_definite_rejection(self) -> bool:
        """True if OVH definitively rejected the request (4xx except 429) and did NOT create an order."""
        return self.status_code in (400, 401, 403, 404, 409, 410, 422)


class OVHAuthError(OVHAPIError):
    """Authentication or permission error from OVHcloud API."""


class OVHRateLimitError(OVHAPIError):
    """HTTP 429 rate-limit error from OVHcloud API."""


class RateLimiter:
    """Thread-safe minimum-interval rate limiter."""

    def __init__(self, qps: float = 4.0):
        self._lock = threading.Lock()
        self._qps = max(0.2, float(qps))
        self._last_ts = 0.0

    def set_qps(self, qps: float) -> None:
        with self._lock:
            self._qps = max(0.2, float(qps))

    def acquire(self) -> None:
        with self._lock:
            min_interval = 1.0 / self._qps
            now = time.monotonic()
            wait = self._last_ts + min_interval - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._last_ts = now


class _HostPool:
    """Simple thread-safe HTTP/HTTPS keep-alive connection pool per (scheme, host, port)."""

    def __init__(self, max_per_host: int = 8):
        self._lock = threading.Lock()
        self._pools: Dict[Tuple[str, str, int], list] = {}
        self._max_per_host = max_per_host
        self._ssl_context = ssl.create_default_context()

    def _create_conn(self, scheme: str, host: str, port: int, timeout: float) -> http.client.HTTPConnection:
        if scheme == "https":
            return http.client.HTTPSConnection(host, port, timeout=timeout, context=self._ssl_context)
        return http.client.HTTPConnection(host, port, timeout=timeout)

    def acquire(self, scheme: str, host: str, port: int, timeout: float) -> http.client.HTTPConnection:
        key = (scheme, host, port)
        with self._lock:
            bucket = self._pools.get(key)
            if bucket:
                conn = bucket.pop()
                conn.timeout = timeout
                return conn
        return self._create_conn(scheme, host, port, timeout)

    def release(self, scheme: str, host: str, port: int, conn: http.client.HTTPConnection) -> None:
        key = (scheme, host, port)
        with self._lock:
            bucket = self._pools.setdefault(key, [])
            if len(bucket) < self._max_per_host:
                bucket.append(conn)
            else:
                try:
                    conn.close()
                except Exception:
                    pass

    def close_all(self) -> None:
        with self._lock:
            for bucket in self._pools.values():
                for conn in bucket:
                    try:
                        conn.close()
                    except Exception:
                        pass
            self._pools.clear()


_GLOBAL_POOL = _HostPool()
_GLOBAL_RATE_LIMITERS: Dict[str, RateLimiter] = {}
_GLOBAL_LIMITER_LOCK = threading.Lock()
_TIME_DELTA_CACHE: Dict[str, Tuple[int, float]] = {}
_TIME_DELTA_LOCK = threading.Lock()


def get_endpoint_rate_limiter(endpoint: str, qps: float = 4.0) -> RateLimiter:
    with _GLOBAL_LIMITER_LOCK:
        limiter = _GLOBAL_RATE_LIMITERS.get(endpoint)
        if limiter is None:
            limiter = RateLimiter(qps)
            _GLOBAL_RATE_LIMITERS[endpoint] = limiter
        else:
            limiter.set_qps(qps)
        return limiter


class OVHClient:
    """Lightweight, fast OVH API client with keep-alive pooling & automatic time sync."""

    USER_AGENT = "ovh-buyer/1.0 (+https://github.com/ovh)"

    def __init__(
        self,
        endpoint: str = "ovh-eu",
        app_key: str = "",
        app_secret: str = "",
        consumer_key: str = "",
        proxy_url: str = "",
        qps: float = 4.0,
        timeout: float = 20.0,
        max_retries: int = 3,
        base_url_override: str = "",
        v1_url_override: str = "",
    ):
        ep_info = ENDPOINTS.get(endpoint, ENDPOINTS["ovh-eu"])
        self.endpoint = endpoint if endpoint in ENDPOINTS else "ovh-eu"
        self.api_base = (base_url_override or str(ep_info["api_base"])).rstrip("/")
        self.v1_base = (v1_url_override or str(ep_info["v1_base"])).rstrip("/")
        self.app_key = (app_key or "").strip()
        self.app_secret = (app_secret or "").strip()
        self.consumer_key = (consumer_key or "").strip()
        self.proxy_url = (proxy_url or "").strip()
        self.timeout = float(timeout)
        self.max_retries = max(1, int(max_retries))
        self.rate_limiter = get_endpoint_rate_limiter(self.api_base, qps=qps)

        # Telemetry
        self.total_requests = 0
        self.failed_requests = 0

    @property
    def is_authenticated(self) -> bool:
        return bool(self.app_key and self.app_secret and self.consumer_key)

    def _get_time_delta(self, force: bool = False) -> int:
        now = time.time()
        with _TIME_DELTA_LOCK:
            cached = _TIME_DELTA_CACHE.get(self.api_base)
            if cached and not force and (now - cached[1] < 1800):
                return cached[0]
        try:
            server_ts = self._raw_request("GET", f"{self.api_base}/auth/time", headers={"Accept": "application/json"})
            if isinstance(server_ts, (int, float)):
                delta = int(server_ts) - int(time.time())
                with _TIME_DELTA_LOCK:
                    _TIME_DELTA_CACHE[self.api_base] = (delta, time.time())
                return delta
        except Exception:
            pass
        return 0

    def sign_request(self, method: str, full_url: str, body_str: str, timestamp: int) -> str:
        """Compute OVH API SHA-1 signature ($1$<hex>)."""
        raw = f"{self.app_secret}+{self.consumer_key}+{method.upper()}+{full_url}+{body_str}+{timestamp}"
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()
        return f"$1${digest}"

    def _raw_request(
        self,
        method: str,
        full_url: str,
        headers: Optional[Dict[str, str]] = None,
        body_bytes: Optional[bytes] = None,
        allow_resend: bool = True,
    ) -> Any:
        req_headers = {
            "Accept": "application/json",
            "User-Agent": self.USER_AGENT,
        }
        if headers:
            req_headers.update(headers)
        if body_bytes is not None:
            req_headers.setdefault("Content-Type", "application/json; charset=utf-8")

        if self.proxy_url:
            return self._request_via_urllib(method, full_url, req_headers, body_bytes)
        return self._request_via_pool(method, full_url, req_headers, body_bytes, allow_resend=allow_resend)

    def _request_via_urllib(
        self,
        method: str,
        full_url: str,
        headers: Dict[str, str],
        body_bytes: Optional[bytes],
    ) -> Any:
        handlers = [urllib.request.ProxyHandler({"http": self.proxy_url, "https": self.proxy_url})]
        opener = urllib.request.build_opener(*handlers)
        req = urllib.request.Request(full_url, data=body_bytes, headers=headers, method=method)
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                status = resp.status
                raw = resp.read()
                return self._decode_response(status, raw, dict(resp.headers))
        except urllib.error.HTTPError as err:
            raw = err.read()
            self._raise_http_error(err.code, raw, dict(err.headers))
        except urllib.error.URLError as err:
            raise OVHAPIError(f"网络/代理连接失败: {err.reason}", status_code=0) from err

    def _request_via_pool(
        self,
        method: str,
        full_url: str,
        headers: Dict[str, str],
        body_bytes: Optional[bytes],
        allow_resend: bool = True,
    ) -> Any:
        parsed = urllib.parse.urlsplit(full_url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname or ""
        port = parsed.port or (443 if scheme == "https" else 80)
        path_with_query = parsed.path or "/"
        if parsed.query:
            path_with_query += "?" + parsed.query

        # Try once on a pooled connection; if stale keep-alive socket drops, retry on a fresh socket.
        # Non-idempotent requests (e.g. checkout) are NEVER re-sent: the server may already have processed them.
        last_exc: Optional[Exception] = None
        max_attempts = 2 if allow_resend else 1
        for attempt in range(max_attempts):
            conn = _GLOBAL_POOL.acquire(scheme, host, port, self.timeout)
            try:
                conn.request(method, path_with_query, body=body_bytes, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
                resp_headers = {k.lower(): v for k, v in resp.getheaders()}
                status = resp.status
                connection_hdr = resp_headers.get("connection", "").lower()
                if connection_hdr == "close":
                    conn.close()
                else:
                    _GLOBAL_POOL.release(scheme, host, port, conn)
                if 200 <= status < 300:
                    return self._decode_response(status, raw, resp_headers)
                self._raise_http_error(status, raw, resp_headers)
            except (http.client.RemoteDisconnected, http.client.BadStatusLine, BrokenPipeError, ConnectionResetError) as exc:
                try:
                    conn.close()
                except Exception:
                    pass
                last_exc = exc
                if attempt + 1 < max_attempts:
                    continue
                raise OVHAPIError(f"连接重置: {exc}", status_code=0) from exc
            except OVHAPIError:
                raise
            except Exception as exc:
                try:
                    conn.close()
                except Exception:
                    pass
                raise OVHAPIError(f"请求异常: {exc}", status_code=0) from exc
        raise OVHAPIError(f"连接异常: {last_exc}", status_code=0)

    def _decode_response(self, status: int, raw: bytes, headers: Dict[str, str]) -> Any:
        if not raw:
            return None
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def _raise_http_error(self, status: int, raw: bytes, headers: Dict[str, str]) -> None:
        text = raw.decode("utf-8", errors="replace").strip()
        payload: Any = None
        msg = text or f"HTTP {status}"
        try:
            payload = json.loads(text)
            if isinstance(payload, dict) and payload.get("message"):
                msg = str(payload["message"])
                if payload.get("errorCode"):
                    msg = f"[{payload['errorCode']}] {msg}"
        except json.JSONDecodeError:
            pass
        if status == 429:
            retry_after = headers.get("retry-after") or headers.get("Retry-After")
            if retry_after:
                msg = f"{msg} (Retry-After: {retry_after}s)"
            raise OVHRateLimitError(msg, status_code=status, payload=payload)
        if status in (401, 403):
            raise OVHAuthError(msg, status_code=status, payload=payload)
        raise OVHAPIError(msg, status_code=status, payload=payload)

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        need_auth: bool = True,
        use_v1: bool = False,
    ) -> Any:
        """Perform an OVH API request with rate-limiting, retries, and auto clock sync."""
        method_u = method.upper()
        base = self.v1_base if use_v1 else self.api_base
        if not path.startswith("/"):
            path = "/" + path
        full_url = base + path
        if params:
            cleaned_params = {k: v for k, v in params.items() if v is not None}
            if cleaned_params:
                qs = urllib.parse.urlencode(cleaned_params, doseq=True)
                sep = "&" if "?" in full_url else "?"
                full_url = f"{full_url}{sep}{qs}"

        body_str = ""
        body_bytes: Optional[bytes] = None
        if body is not None:
            body_str = json.dumps(body, separators=(",", ":"))
            body_bytes = body_str.encode("utf-8")

        if need_auth and not self.is_authenticated:
            raise OVHAuthError("未配置 OVH API 凭据 (App Key / App Secret / Consumer Key)", status_code=401)

        # Money-moving POSTs (checkout / pay) must never be re-sent after an ambiguous transport error or 5xx:
        # the server may already have created the order. Only definite rejections (429, clock-skew 400) are retried.
        non_idempotent = method_u == "POST" and (
            path.rstrip("/").endswith("/checkout") or "/payWithRegisteredPaymentMean" in path or path.rstrip("/").endswith("/pay")
        )

        resynced_clock = False
        for attempt in range(self.max_retries):
            self.rate_limiter.acquire()
            self.total_requests += 1
            headers: Dict[str, str] = {}
            if need_auth:
                delta = self._get_time_delta(force=False)
                ts = int(time.time()) + delta
                sig = self.sign_request(method_u, full_url, body_str, ts)
                headers.update(
                    {
                        "X-Ovh-Application": self.app_key,
                        "X-Ovh-Consumer": self.consumer_key,
                        "X-Ovh-Timestamp": str(ts),
                        "X-Ovh-Signature": sig,
                    }
                )
            elif self.app_key:
                headers["X-Ovh-Application"] = self.app_key

            try:
                return self._raw_request(
                    method_u, full_url, headers=headers, body_bytes=body_bytes, allow_resend=not non_idempotent
                )
            except OVHRateLimitError as exc:
                self.failed_requests += 1
                if attempt + 1 >= self.max_retries:
                    raise
                delay = min(15.0, (2 ** attempt) * 1.5 + random.uniform(0.2, 0.8))
                time.sleep(delay)
            except OVHAPIError as exc:
                self.failed_requests += 1
                # Auto-fix clock skew if OVH rejects timestamp/signature on first try
                if (
                    need_auth
                    and not resynced_clock
                    and exc.status_code == 400
                    and ("timestamp" in str(exc).lower() or "signature" in str(exc).lower())
                ):
                    resynced_clock = True
                    self._get_time_delta(force=True)
                    continue
                # Retry transient transport errors & 5xx (idempotent requests only!)
                if (
                    not non_idempotent
                    and exc.status_code in (0, 500, 502, 503, 504)
                    and attempt + 1 < self.max_retries
                ):
                    delay = min(10.0, (2 ** attempt) * 0.8 + random.uniform(0.1, 0.5))
                    time.sleep(delay)
                    continue
                raise

        raise OVHAPIError("达到最大重试次数", status_code=0)

    def get(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        need_auth: bool = True,
        use_v1: bool = False,
    ) -> Any:
        return self.request("GET", path, params=params, need_auth=need_auth, use_v1=use_v1)

    def post(
        self,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        need_auth: bool = True,
    ) -> Any:
        return self.request("POST", path, params=params, body=body, need_auth=need_auth)

    def put(
        self,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        need_auth: bool = True,
    ) -> Any:
        return self.request("PUT", path, params=params, body=body, need_auth=need_auth)

    def delete(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        need_auth: bool = True,
    ) -> Any:
        return self.request("DELETE", path, params=params, need_auth=need_auth)

    # ---------- High-level Helpers ----------

    def verify_credentials(self) -> Dict[str, Any]:
        """Verify API keys against /me, credit balance, and default payment method."""
        me = self.get("/me", need_auth=True)
        result: Dict[str, Any] = {
            "nichandle": me.get("nichandle", ""),
            "email": me.get("email", ""),
            "subsidiary": me.get("ovhSubsidiary", ""),
            "currency": (me.get("currency") or {}).get("code", "EUR"),
            "state": me.get("state", ""),
            "credit_balance": None,
            "payment_methods_count": 0,
            "default_payment_type": None,
        }
        try:
            balances = self.get("/me/credit/balance", need_auth=True)
            if isinstance(balances, list) and balances:
                total_val = 0.0
                for b_name in balances[:3]:
                    detail = self.get(f"/me/credit/balance/{urllib.parse.quote(str(b_name))}", need_auth=True)
                    amt = (detail.get("amount") or {}).get("value")
                    if isinstance(amt, (int, float)):
                        total_val += float(amt)
                result["credit_balance"] = round(total_val, 2)
        except Exception:
            pass
        try:
            methods = self.get("/me/payment/method", need_auth=True)
            if isinstance(methods, list):
                result["payment_methods_count"] = len(methods)
                for m in methods[:10]:
                    md = m if isinstance(m, dict) else self.get(f"/me/payment/method/{m}", need_auth=True)
                    if isinstance(md, dict) and md.get("default"):
                        result["default_payment_type"] = str(md.get("paymentType") or md.get("label") or "DEFAULT")
                        break
        except Exception:
            pass
        return result

    def get_recent_orders(self, limit: int = 15, since_iso: str = "") -> list:
        """Fetch recent OVH account orders with URL & live status."""
        params = {"date.from": since_iso} if since_iso else None
        order_ids = self.get("/me/order", params=params, need_auth=True)
        if not isinstance(order_ids, list):
            return []
        recent_ids = sorted(order_ids, reverse=True)[:limit]
        details = []
        for oid in recent_ids:
            try:
                item = self.get(f"/me/order/{oid}", need_auth=True)
                if isinstance(item, dict):
                    try:
                        item["status"] = self.get(f"/me/order/{oid}/status", need_auth=True)
                    except Exception:
                        item["status"] = "unknown"
                    details.append(item)
            except Exception:
                continue
        return details

    def get_order_status(self, order_id: Any) -> str:
        st = self.get(f"/me/order/{order_id}/status", need_auth=True)
        return str(st or "unknown")

    def get_order_details(self, order_id: Any) -> list:
        ids = self.get(f"/me/order/{order_id}/details", need_auth=True)
        if not isinstance(ids, list):
            return []
        out = []
        for did in ids[:20]:
            try:
                d = self.get(f"/me/order/{order_id}/details/{did}", need_auth=True)
                if isinstance(d, dict):
                    out.append(d)
            except Exception:
                continue
        return out

    def list_dedicated_servers(self) -> list:
        names = self.get("/dedicated/server", need_auth=True)
        return names if isinstance(names, list) else []

    def get_dedicated_server(self, service_name: str) -> Dict[str, Any]:
        res = self.get(f"/dedicated/server/{urllib.parse.quote(service_name)}", need_auth=True)
        return res if isinstance(res, dict) else {}

    def get_dedicated_hardware(self, service_name: str) -> Dict[str, Any]:
        res = self.get(f"/dedicated/server/{urllib.parse.quote(service_name)}/specifications/hardware", need_auth=True)
        return res if isinstance(res, dict) else {}

    def get_dedicated_service_infos(self, service_name: str) -> Dict[str, Any]:
        res = self.get(f"/dedicated/server/{urllib.parse.quote(service_name)}/serviceInfos", need_auth=True)
        return res if isinstance(res, dict) else {}

    def get_availabilities(self, plan_code: str = "") -> list:
        """Query /dedicated/server/datacenter/availabilities (public endpoint, works with or without auth)."""
        params = {"planCode": plan_code} if plan_code else None
        data = self.get("/dedicated/server/datacenter/availabilities", params=params, need_auth=False)
        return data if isinstance(data, list) else []

    def get_catalog(self, subsidiary: str = "IE", product_line: str = "eco") -> Dict[str, Any]:
        """Fetch public commercial catalog for eco / baremetalServers / vps."""
        endpoint_map = {
            "eco": "/order/catalog/public/eco",
            "baremetal": "/order/catalog/public/baremetalServers",
            "baremetalservers": "/order/catalog/public/baremetalServers",
            "vps": "/order/catalog/public/vps",
        }
        path = endpoint_map.get(product_line.lower(), "/order/catalog/public/eco")
        data = self.get(path, params={"ovhSubsidiary": subsidiary.upper()}, need_auth=False)
        return data if isinstance(data, dict) else {}

    def get_vps_datacenter_availability(self, plan_code: str, subsidiary: str = "IE") -> Dict[str, Any]:
        """Fetch VPS datacenter availability from OVH v1 API."""
        data = self.get(
            "/vps/order/rule/datacenter",
            params={"ovhSubsidiary": subsidiary.upper(), "planCode": plan_code},
            need_auth=False,
            use_v1=True,
        )
        return data if isinstance(data, dict) else {}
