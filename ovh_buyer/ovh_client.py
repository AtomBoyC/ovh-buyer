"""Zero-dependency OVHcloud API client (1.0 & v1) with SHA-1 signing, clock sync, rate limiting, and proxy support."""

import base64
import hashlib
import http.client
import ipaddress
import json
import random
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

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


def normalize_proxy_url(raw: str) -> str:
    """Normalize proxy string into standard URL (supports http/https/socks5/socks5h & host:port:user:pass)."""
    s = str(raw or "").strip()
    if not s or s.lower() in ("direct", "none", "off"):
        return ""
    if "://" not in s:
        parts = s.split(":")
        # Commercial shorthand: host:port:user:pass (check BEFORE '@' so '@' inside password works)
        if len(parts) >= 4 and parts[1].isdigit() and "@" not in parts[0] and "@" not in parts[1]:
            host, port, user = parts[0], parts[1], parts[2]
            pw = ":".join(parts[3:])
            u_enc = urllib.parse.quote(user, safe="")
            p_enc = urllib.parse.quote(pw, safe="")
            return f"http://{u_enc}:{p_enc}@{host}:{port}"
        if "@" in s:
            return f"http://{s}"
        return f"http://{s}"
    return s


def parse_proxy_config(raw: str) -> Optional[Dict[str, Any]]:
    """Parse normalized proxy URL into {scheme, host, port, username, password, url}."""
    norm = normalize_proxy_url(raw)
    if not norm:
        return None
    scheme_sep = norm.find("://")
    if scheme_sep <= 0:
        raise ValueError(f"无效的代理格式: {raw}")
    scheme = norm[:scheme_sep].lower()
    if scheme == "socks":
        scheme = "socks5"
    if scheme not in ("http", "https", "socks5", "socks5h"):
        raise ValueError(f"不支持的代理协议 '{scheme}' (支持 http / https / socks5 / socks5h)")

    rest = norm[scheme_sep + 3 :].strip().rstrip("/")
    username: Optional[str] = None
    password: Optional[str] = None
    if "@" in rest:
        auth_part, host_part = rest.rsplit("@", 1)
        if ":" in auth_part:
            u_raw, p_raw = auth_part.split(":", 1)
            username = urllib.parse.unquote(u_raw)
            password = urllib.parse.unquote(p_raw)
        else:
            username = urllib.parse.unquote(auth_part)
            password = ""
    else:
        host_part = rest

    parsed = urllib.parse.urlsplit(f"{scheme}://{host_part}")
    host = parsed.hostname
    port = parsed.port
    if not host or not port:
        raise ValueError(f"代理缺少主机或端口: {raw}")
    return {
        "scheme": scheme,
        "host": host,
        "port": int(port),
        "username": username,
        "password": password,
        "url": norm,
    }


def redact_proxy_url(raw: str) -> str:
    """Return display-safe proxy URL with password masked."""
    try:
        cfg = parse_proxy_config(raw)
    except ValueError:
        return "***"
    if not cfg:
        return ""
    host_fmt = f"[{cfg['host']}]" if ":" in cfg["host"] else cfg["host"]
    if cfg["username"] is not None:
        u_disp = cfg["username"][:2] + "**" if len(cfg["username"]) > 2 else "**"
        return f"{cfg['scheme']}://{u_disp}:****@{host_fmt}:{cfg['port']}"
    return f"{cfg['scheme']}://{host_fmt}:{cfg['port']}"


def parse_proxy_list(raw: str) -> List[str]:
    """Split multi-line / comma / semicolon proxy list into normalized URLs."""
    if not raw:
        return []
    out: List[str] = []
    for line in re.split(r"[\r\n,;]+", str(raw)):
        cleaned = line.strip()
        if not cleaned or cleaned.startswith("#"):
            continue
        norm = normalize_proxy_url(cleaned)
        if norm and norm not in out:
            out.append(norm)
    return out


def redact_proxy_list_text(raw: str) -> str:
    """Redact credentials across all proxies in a multi-line/comma proxy config string."""
    items = parse_proxy_list(raw)
    return "\n".join(redact_proxy_url(x) for x in items)


class ProxyRotator:
    """Thread-safe round-robin proxy selector with temporary failure cooldown."""

    def __init__(self, cooldown_seconds: float = 30.0):
        self._lock = threading.Lock()
        self._cursor: int = 0
        self._cooldown_seconds = cooldown_seconds
        self._cooldown_until: Dict[str, float] = {}

    def pick(self, raw_proxies: str, exclude: Optional[set] = None) -> str:
        candidates = parse_proxy_list(raw_proxies)
        if not candidates:
            return ""
        excluded = exclude or set()
        pool = [p for p in candidates if p not in excluded]
        if not pool:
            return ""
        now = time.monotonic()
        with self._lock:
            n = len(pool)
            healthy = []
            for offset in range(n):
                idx = (self._cursor + offset) % n
                cand = pool[idx]
                if self._cooldown_until.get(cand, 0.0) <= now:
                    healthy.append((idx, cand))
            if healthy:
                chosen_idx, chosen = healthy[0]
                self._cursor = (chosen_idx + 1) % n
                return chosen
            # All candidates in cooldown: pick the one expiring soonest
            soonest = min(pool, key=lambda p: self._cooldown_until.get(p, 0.0))
            self._cursor = (pool.index(soonest) + 1) % n
            return soonest

    def mark_failed(self, proxy_url: str) -> None:
        norm = normalize_proxy_url(proxy_url)
        if not norm:
            return
        with self._lock:
            self._cooldown_until[norm] = time.monotonic() + self._cooldown_seconds

    def mark_ok(self, proxy_url: str) -> None:
        norm = normalize_proxy_url(proxy_url)
        if not norm:
            return
        with self._lock:
            self._cooldown_until.pop(norm, None)


def _recv_exact(sock: socket.socket, length: int) -> bytes:
    buf = bytearray()
    while len(buf) < length:
        chunk = sock.recv(length - len(buf))
        if not chunk:
            raise OSError("代理连接在握手阶段意外断开")
        buf.extend(chunk)
    return bytes(buf)


def _connect_socks5_socket(
    proxy_cfg: Dict[str, Any],
    target_host: str,
    target_port: int,
    timeout: float,
) -> socket.socket:
    """Establish a TCP connection to (target_host, target_port) via SOCKS5/SOCKS5H (RFC 1928 + RFC 1929)."""
    sock = socket.create_connection((proxy_cfg["host"], proxy_cfg["port"]), timeout=timeout)
    try:
        username = proxy_cfg.get("username")
        password = proxy_cfg.get("password") or ""
        if username is not None:
            sock.sendall(b"\x05\x02\x00\x02")
        else:
            sock.sendall(b"\x05\x01\x00")
        ver, method = _recv_exact(sock, 2)
        if ver != 5:
            raise OVHAPIError(f"SOCKS5 代理协议版本异常 (ver={ver})", status_code=0)
        if method == 0x02:
            if username is None:
                raise OVHAPIError("SOCKS5 代理要求账号密码认证", status_code=0)
            u_bytes = username.encode("utf-8")
            p_bytes = password.encode("utf-8")
            if len(u_bytes) > 255 or len(p_bytes) > 255:
                raise OVHAPIError("SOCKS5 账号或密码长度超出限制", status_code=0)
            sock.sendall(b"\x01" + bytes([len(u_bytes)]) + u_bytes + bytes([len(p_bytes)]) + p_bytes)
            _, auth_status = _recv_exact(sock, 2)
            if auth_status != 0x00:
                raise OVHAPIError("SOCKS5 代理认证失败（用户名或密码错误）", status_code=0)
        elif method != 0x00:
            raise OVHAPIError("SOCKS5 代理不支持可用的认证方式", status_code=0)

        # Build CONNECT request (remote DNS via ATYP=0x03 for domain names, avoiding local DNS pollution)
        try:
            ip_obj = ipaddress.ip_address(target_host)
            if ip_obj.version == 4:
                addr_part = b"\x01" + ip_obj.packed
            else:
                addr_part = b"\x04" + ip_obj.packed
        except ValueError:
            host_bytes = target_host.encode("idna")
            if len(host_bytes) > 255:
                raise OVHAPIError("目标域名过长", status_code=0)
            addr_part = b"\x03" + bytes([len(host_bytes)]) + host_bytes

        req = b"\x05\x01\x00" + addr_part + int(target_port).to_bytes(2, "big")
        sock.sendall(req)
        r_ver, rep, _, bnd_atyp = _recv_exact(sock, 4)
        if r_ver != 5 or rep != 0x00:
            raise OVHAPIError(f"SOCKS5 代理无法连接目标服务器 (错误码={rep})", status_code=0)
        if bnd_atyp == 0x01:
            _recv_exact(sock, 6)
        elif bnd_atyp == 0x04:
            _recv_exact(sock, 18)
        elif bnd_atyp == 0x03:
            dlen = _recv_exact(sock, 1)[0]
            _recv_exact(sock, dlen + 2)
        return sock
    except Exception:
        try:
            sock.close()
        except Exception:
            pass
        raise


class _PreconnectedHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection wrapper around an already-connected (plain or TLS-wrapped) socket."""

    def __init__(self, host: str, port: int, pre_sock: socket.socket, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._pre_sock = pre_sock

    def connect(self) -> None:
        self.sock = self._pre_sock


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
        self.proxy_url = normalize_proxy_url(proxy_url)
        self.timeout = float(timeout)
        self.max_retries = max(1, int(max_retries))
        rate_key = f"{self.api_base}|{self.proxy_url or 'direct'}"
        self.rate_limiter = get_endpoint_rate_limiter(rate_key, qps=qps)

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
            return self._request_via_proxy(method, full_url, req_headers, body_bytes)
        return self._request_via_pool(method, full_url, req_headers, body_bytes, allow_resend=allow_resend)

    def _request_via_proxy(
        self,
        method: str,
        full_url: str,
        headers: Dict[str, str],
        body_bytes: Optional[bytes],
    ) -> Any:
        try:
            proxy_cfg = parse_proxy_config(self.proxy_url)
        except ValueError as exc:
            raise OVHAPIError(str(exc), status_code=0) from exc
        if not proxy_cfg:
            return self._request_via_pool(method, full_url, headers, body_bytes, allow_resend=False)

        parsed = urllib.parse.urlsplit(full_url)
        scheme = parsed.scheme.lower()
        target_host = parsed.hostname or ""
        target_port = parsed.port or (443 if scheme == "https" else 80)
        path_with_query = parsed.path or "/"
        if parsed.query:
            path_with_query += "?" + parsed.query

        pscheme = proxy_cfg["scheme"]
        proxy_disp = redact_proxy_url(self.proxy_url)
        ssl_ctx = _GLOBAL_POOL._ssl_context

        conn: Optional[http.client.HTTPConnection] = None
        try:
            if pscheme in ("socks5", "socks5h"):
                raw_sock = _connect_socks5_socket(proxy_cfg, target_host, target_port, timeout=self.timeout)
                if scheme == "https":
                    raw_sock = ssl_ctx.wrap_socket(raw_sock, server_hostname=target_host)
                conn = _PreconnectedHTTPConnection(target_host, target_port, raw_sock, timeout=self.timeout)
                conn.request(method, path_with_query, body=body_bytes, headers=headers)
            else:
                proxy_headers: Dict[str, str] = {}
                if proxy_cfg.get("username") is not None:
                    cred = f"{proxy_cfg['username']}:{proxy_cfg.get('password') or ''}".encode("utf-8")
                    proxy_headers["Proxy-Authorization"] = "Basic " + base64.b64encode(cred).decode("ascii")

                if scheme == "https":
                    if pscheme == "https":
                        outer_sock = socket.create_connection((proxy_cfg["host"], proxy_cfg["port"]), timeout=self.timeout)
                        tls_proxy_sock = ssl_ctx.wrap_socket(outer_sock, server_hostname=proxy_cfg["host"])
                        connect_lines = [
                            f"CONNECT {target_host}:{target_port} HTTP/1.1",
                            f"Host: {target_host}:{target_port}",
                        ]
                        for hk, hv in proxy_headers.items():
                            connect_lines.append(f"{hk}: {hv}")
                        tls_proxy_sock.sendall(("\r\n".join(connect_lines) + "\r\n\r\n").encode("utf-8"))
                        resp_buf = bytearray()
                        while b"\r\n\r\n" not in resp_buf and len(resp_buf) < 16384:
                            ch = tls_proxy_sock.recv(1024)
                            if not ch:
                                break
                            resp_buf.extend(ch)
                        first_line = resp_buf.split(b"\r\n", 1)[0].decode("latin1", errors="replace")
                        if " 200" not in first_line:
                            tls_proxy_sock.close()
                            raise OVHAPIError(f"HTTPS 代理隧道握手失败 ({proxy_disp}): {first_line}", status_code=0)
                        inner_tls = ssl_ctx.wrap_socket(tls_proxy_sock, server_hostname=target_host)
                        conn = _PreconnectedHTTPConnection(target_host, target_port, inner_tls, timeout=self.timeout)
                    else:
                        conn = http.client.HTTPSConnection(
                            proxy_cfg["host"], proxy_cfg["port"], timeout=self.timeout, context=ssl_ctx
                        )
                        conn.set_tunnel(target_host, target_port, headers=proxy_headers or None)
                    conn.request(method, path_with_query, body=body_bytes, headers=headers)
                else:
                    if pscheme == "https":
                        conn = http.client.HTTPSConnection(
                            proxy_cfg["host"], proxy_cfg["port"], timeout=self.timeout, context=ssl_ctx
                        )
                    else:
                        conn = http.client.HTTPConnection(proxy_cfg["host"], proxy_cfg["port"], timeout=self.timeout)
                    merged_headers = dict(headers)
                    merged_headers.update(proxy_headers)
                    conn.request(method, full_url, body=body_bytes, headers=merged_headers)

            resp = conn.getresponse()
            raw = resp.read()
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            status = resp.status
            if 200 <= status < 300:
                return self._decode_response(status, raw, resp_headers)
            self._raise_http_error(status, raw, resp_headers)
        except OVHAPIError:
            raise
        except Exception as exc:
            raise OVHAPIError(f"代理请求失败 ({proxy_disp}): {exc}", status_code=0) from exc
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

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
