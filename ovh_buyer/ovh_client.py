"""Zero-dependency OVHcloud API client (1.0 & v1) with SHA-1 signing, clock sync, rate limiting, and proxy support."""

import base64
from collections import OrderedDict
import hashlib
import http.client
import io
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
_GLOBAL_RATE_LIMITERS: "OrderedDict[str, RateLimiter]" = OrderedDict()
_GLOBAL_LIMITER_LOCK = threading.Lock()
MAX_RATE_LIMITERS = 256
_TIME_DELTA_CACHE: Dict[str, Tuple[int, float]] = {}
_TIME_DELTA_LOCK = threading.Lock()


def get_endpoint_rate_limiter(endpoint: str, qps: Optional[float] = 4.0) -> RateLimiter:
    with _GLOBAL_LIMITER_LOCK:
        limiter = _GLOBAL_RATE_LIMITERS.get(endpoint)
        if limiter is None:
            limiter = RateLimiter(4.0 if qps is None else qps)
            _GLOBAL_RATE_LIMITERS[endpoint] = limiter
            while len(_GLOBAL_RATE_LIMITERS) > MAX_RATE_LIMITERS:
                _GLOBAL_RATE_LIMITERS.popitem(last=False)
        else:
            _GLOBAL_RATE_LIMITERS.move_to_end(endpoint)
            if qps is not None:
                limiter.set_qps(qps)
        return limiter


def _try_shorthand_to_url(scheme: str, body: str) -> Optional[str]:
    """Convert host:port:user:pass shorthand to scheme://user:pass@host:port when unambiguous."""
    s = body.strip()
    if not s or s.startswith("["):
        return None
    parts = s.split(":")
    if len(parts) < 4 or not parts[1].isdigit() or "@" in parts[0] or "@" in parts[1]:
        return None
    port_num = int(parts[1])
    if not (1 <= port_num <= 65535):
        return None
    # When '@' appears later in the string, disambiguate host:port:user:p@ss from user:12345:foo@host:port:
    # a proxy host is an IP or domain (contains '.' or is 'localhost').
    if "@" in s and "." not in parts[0] and parts[0].lower() != "localhost":
        return None
    host, port, user = parts[0], parts[1], parts[2]
    pw = ":".join(parts[3:])
    u_enc = urllib.parse.quote(user, safe="")
    p_enc = urllib.parse.quote(pw, safe="")
    return f"{scheme}://{u_enc}:{p_enc}@{host}:{port}"


def normalize_proxy_url(raw: str) -> str:
    """Normalize proxy string into standard URL (supports http/https/socks5/socks5h & [scheme://]host:port:user:pass)."""
    s = str(raw or "").strip()
    if not s or s.lower() in ("direct", "none", "off"):
        return ""
    if "://" not in s:
        shorthand = _try_shorthand_to_url("http", s)
        if shorthand:
            return shorthand
        return f"http://{s}"
    scheme, rest = s.split("://", 1)
    shorthand = _try_shorthand_to_url(scheme.lower(), rest)
    if shorthand:
        return shorthand
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


_PROXY_INLINE_SEP_RE = re.compile(
    r"[,;]+(?=\s*(?:(?:https?|socks5?h?|socks)://|[A-Za-z0-9._-]+:\d{2,5}(?:\b|:)|\[[0-9a-fA-F:]+\]:\d+))"
)


def parse_proxy_list(raw: str) -> List[str]:
    """Split multi-line / comma / semicolon proxy list into normalized URLs without splitting passwords."""
    if not raw:
        return []
    out: List[str] = []
    for raw_line in str(raw).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        for token in _PROXY_INLINE_SEP_RE.split(line):
            cleaned = token.strip()
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


def mask_proxy_url_for_ui(raw: str) -> str:
    """Mask only the proxy password with '****' while preserving username/host/port for editable UI fields."""
    try:
        cfg = parse_proxy_config(raw)
    except ValueError:
        return ""
    if not cfg:
        return ""
    host_fmt = f"[{cfg['host']}]" if ":" in cfg["host"] else cfg["host"]
    if cfg["username"] is not None:
        u_enc = urllib.parse.quote(cfg["username"], safe="")
        has_pw = bool(cfg.get("password"))
        p_part = ":****" if has_pw else ""
        return f"{cfg['scheme']}://{u_enc}{p_part}@{host_fmt}:{cfg['port']}"
    return f"{cfg['scheme']}://{host_fmt}:{cfg['port']}"


def mask_proxy_list_for_ui(raw: str) -> str:
    """Mask passwords across a multi-line/comma proxy list for safe UI round-tripping."""
    items = parse_proxy_list(raw)
    return "\n".join(mask_proxy_url_for_ui(x) for x in items)


def merge_masked_proxy_input(submitted_raw: str, existing_raw: str) -> str:
    """Restore original proxy passwords when UI submits unchanged ':****@' masked placeholders."""
    sub = str(submitted_raw or "").strip()
    if not sub:
        return ""
    if "****" not in sub:
        return sub

    by_full: Dict[Tuple[str, str, int, Optional[str]], str] = {}
    by_host: Dict[Tuple[str, str, int], str] = {}
    for old_url in parse_proxy_list(existing_raw):
        try:
            oc = parse_proxy_config(old_url)
        except ValueError:
            continue
        if not oc:
            continue
        by_full[(oc["scheme"], oc["host"].lower(), oc["port"], oc["username"])] = oc["url"]
        by_host.setdefault((oc["scheme"], oc["host"].lower(), oc["port"]), oc["url"])

    restored: List[str] = []
    for cand in parse_proxy_list(sub):
        try:
            cc = parse_proxy_config(cand)
        except ValueError:
            restored.append(cand)
            continue
        if not cc:
            continue
        if cc.get("password") == "****":
            orig = by_full.get((cc["scheme"], cc["host"].lower(), cc["port"], cc["username"]))
            if not orig and cc["username"] and cc["username"].endswith("**"):
                orig = by_host.get((cc["scheme"], cc["host"].lower(), cc["port"]))
            restored.append(orig or cand)
        else:
            restored.append(cc["url"])
    return "\n".join(restored)


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


def proxy_short_id(proxy_url: str) -> str:
    """Deterministic credential-safe short ID for UI actions (e.g. manual evict)."""
    norm = normalize_proxy_url(proxy_url)
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:10] if norm else ""


class ProxyPoolManager:
    """Two-tier monitoring proxy pool (Master Pool -> Top-N lowest-latency Active Pool).

    Features:
    - Maintains up to `max_active` fastest healthy proxies in Active Pool (sorted by latency ascending)
    - Evicts failing/rate-limited active proxies into temporary cooldown and hot-swaps the fastest standby
    - Staggered 1-by-1 background probing with randomized inter-probe jitter (never bursts all proxies at once)
    """

    def __init__(
        self,
        on_active_changed=None,
        log_fn=None,
        probe_fn=None,
        delay_range: Tuple[float, float] = (1.5, 4.0),
    ):
        self._lock = threading.RLock()
        self._probe_lock = threading.Lock()
        self._on_active_changed = on_active_changed
        self._log = log_fn or (lambda level, msg, source="proxy": None)
        self._custom_probe_fn = probe_fn
        self.delay_min = max(0.0, float(delay_range[0]))
        self.delay_max = max(self.delay_min, float(delay_range[1]))

        # Configuration snapshot
        self.auto_enabled: bool = False
        self.max_active: int = 5
        self.fail_threshold: int = 2
        self.cooldown_sec: int = 300
        self.max_latency_ms: int = 3000
        self.recheck_interval: int = 600
        self.test_endpoint: str = "ovh-eu"

        # Pool state
        self._master: List[str] = []
        self._active: List[str] = []
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._cursor: int = 0

        # Background staggered probe telemetry
        self._cancel_probe = threading.Event()
        self._probing: bool = False
        self._probe_done: int = 0
        self._probe_total: int = 0
        self._current_probe_proxy: str = ""
        self._last_probe_summary: str = ""
        self.last_pass_at: float = 0.0

    def _ensure_entry_locked(self, norm_url: str) -> Dict[str, Any]:
        entry = self._entries.get(norm_url)
        if entry is None:
            entry = {
                "url": norm_url,
                "id": proxy_short_id(norm_url),
                "latency_ms": None,
                "ok": None,
                "fail_streak": 0,
                "ok_count": 0,
                "fail_count": 0,
                "cooldown_until": 0.0,
                "last_checked_ts": 0.0,
                "last_error": "",
            }
            self._entries[norm_url] = entry
        return entry

    def _rebalance_locked(self, now: Optional[float] = None) -> Tuple[List[str], List[str]]:
        """Recompute Top-N active list from Master Pool. Returns (prev_active, new_active)."""
        ts = time.time() if now is None else now
        prev = list(self._active)
        if not self.auto_enabled or not self._master:
            self._active = []
            return prev, []

        tier_measured = []
        tier_untested = []
        for idx, url in enumerate(self._master):
            ent = self._ensure_entry_locked(url)
            if ent["cooldown_until"] > ts:
                continue
            if ent["ok"] is False:
                # Cooldown expired after a previous failure: keep in standby until next probe verifies it,
                # unless NO measured/untested candidates exist at all (handled below).
                continue
            lat = ent["latency_ms"]
            if ent["ok"] is True and lat is not None:
                if lat <= self.max_latency_ms:
                    tier_measured.append((lat, idx, url))
            elif ent["ok"] is None:
                tier_untested.append((idx, url))

        tier_measured.sort(key=lambda x: (x[0], x[1]))
        chosen = [u for _, _, u in tier_measured[: self.max_active]]
        if len(chosen) < self.max_active and tier_untested:
            need = self.max_active - len(chosen)
            chosen.extend(u for _, u in tier_untested[:need])

        self._active = chosen
        return prev, list(self._active)

    def _notify_if_changed(self, prev: List[str], cur: List[str]) -> None:
        if prev != cur and self._on_active_changed:
            try:
                self._on_active_changed(list(cur))
            except Exception:
                pass

    def sync_from_settings(self, settings: Dict[str, Any]) -> Tuple[bool, bool]:
        """Sync pool config & master list from DB settings.

        Returns (active_changed, needs_initial_probe).
        """
        auto = bool(settings.get("proxy_auto_pool", False))
        max_act = max(1, min(50, int(settings.get("proxy_max_active") or 5)))
        fail_th = max(1, min(20, int(settings.get("proxy_fail_threshold") or 2)))
        cd_sec = max(10, min(86400, int(settings.get("proxy_cooldown_sec") or 300)))
        max_lat = max(100, min(30000, int(settings.get("proxy_max_latency_ms") or 3000)))
        recheck = max(0, min(86400, int(settings.get("proxy_recheck_interval") or 600)))
        ep = str(settings.get("proxy_test_endpoint") or "ovh-eu")
        if ep not in ENDPOINTS:
            ep = "ovh-eu"

        master_raw = str(settings.get("monitor_proxies_pool") or "").strip()
        master_list = parse_proxy_list(master_raw)

        needs_probe = False
        with self._lock:
            prev_master = list(self._master)
            prev_auto = self.auto_enabled
            self.auto_enabled = auto
            self.max_active = max_act
            self.fail_threshold = fail_th
            self.cooldown_sec = cd_sec
            self.max_latency_ms = max_lat
            self.recheck_interval = recheck
            self.test_endpoint = ep
            self._master = master_list

            # Prune removed entries so memory stays bounded
            keep = set(master_list)
            for k in [k for k in self._entries if k not in keep]:
                self._entries.pop(k, None)
            for u in master_list:
                ent = self._ensure_entry_locked(u)
                if ent["ok"] is None:
                    needs_probe = True

            if auto and master_list and (not prev_auto or master_list != prev_master):
                needs_probe = True

            prev_act, new_act = self._rebalance_locked()

        changed = prev_act != new_act
        if changed:
            self._notify_if_changed(prev_act, new_act)
        return changed, (auto and bool(master_list) and needs_probe)

    @property
    def master_count(self) -> int:
        with self._lock:
            return len(self._master)

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def get_active_urls(self) -> List[str]:
        with self._lock:
            return list(self._active)

    def pick_active(self, exclude: Optional[set] = None) -> str:
        """Round-robin pick from the current Active Pool."""
        excluded = exclude or set()
        with self._lock:
            if not self._active:
                self._rebalance_locked()
            pool = [u for u in self._active if u not in excluded]
            if not pool:
                return ""
            idx = self._cursor % len(pool)
            chosen = pool[idx]
            self._cursor = (idx + 1) % len(pool)
            return chosen

    def report_success(self, proxy_url: str, latency_ms: Optional[int] = None) -> None:
        norm = normalize_proxy_url(proxy_url)
        if not norm:
            return
        with self._lock:
            if norm not in self._entries:
                return
            ent = self._entries[norm]
            ent["ok"] = True
            ent["fail_streak"] = 0
            ent["cooldown_until"] = 0.0
            ent["ok_count"] += 1
            ent["last_error"] = ""
            if latency_ms is not None and latency_ms >= 0:
                prev_lat = ent["latency_ms"]
                ent["latency_ms"] = int(round(prev_lat * 0.6 + latency_ms * 0.4)) if prev_lat is not None else int(latency_ms)

    def report_failure(self, proxy_url: str, status_code: int = 0, error: str = "") -> Optional[str]:
        """Record a runtime failure; if threshold or 403/407/429 hit, evict & hot-swap fastest standby.

        Returns replacement proxy URL (if one was promoted), else None.
        """
        norm = normalize_proxy_url(proxy_url)
        if not norm:
            return None
        now = time.time()
        evicted = False
        promoted: Optional[str] = None
        prom_lat: Optional[int] = None
        active_rem = 0
        prev_act: List[str] = []
        new_act: List[str] = []

        with self._lock:
            if norm not in self._entries:
                return None
            ent = self._entries[norm]
            ent["fail_streak"] += 1
            ent["fail_count"] += 1
            ent["last_error"] = str(error or f"HTTP {status_code}")[:240]

            immediate = status_code in (403, 407, 429)
            if immediate or ent["fail_streak"] >= self.fail_threshold:
                evicted = norm in self._active
                ent["ok"] = False
                ent["cooldown_until"] = now + self.cooldown_sec
                prev_act, new_act = self._rebalance_locked(now)
                active_rem = len(new_act)
                added = [u for u in new_act if u not in prev_act]
                if added:
                    promoted = added[0]
                    prom_lat = self._entries[promoted]["latency_ms"]

        if prev_act != new_act:
            self._notify_if_changed(prev_act, new_act)
        if evicted:
            bad_disp = redact_proxy_url(norm)
            if promoted:
                good_disp = redact_proxy_url(promoted)
                lat_txt = f"{prom_lat}ms" if prom_lat is not None else "待测速"
                self._log(
                    "INFO",
                    f"🔄 [代理池热替补] 移出异常代理 {bad_disp} (冷却 {self.cooldown_sec}s)，已自动补入最快候选 {good_disp} ({lat_txt})",
                    "proxy",
                )
            else:
                self._log(
                    "WARNING",
                    f"⚠️ [代理池剔除] 移出异常代理 {bad_disp} (冷却 {self.cooldown_sec}s)，总池暂无健康替补（当前活跃 {active_rem} 条）",
                    "proxy",
                )
        return promoted

    def evict_by_id(self, proxy_id: str) -> Dict[str, Any]:
        """Manually evict an active proxy into cooldown by its short ID and promote fastest standby."""
        now = time.time()
        target_url = ""
        promoted = ""
        prom_lat = None
        with self._lock:
            for url, ent in self._entries.items():
                if ent["id"] == proxy_id:
                    target_url = url
                    ent["ok"] = False
                    ent["fail_streak"] = self.fail_threshold
                    ent["cooldown_until"] = now + self.cooldown_sec
                    ent["last_error"] = "手动踢出冷却"
                    break
            if not target_url:
                return {"ok": False, "error": "未找到对应代理节点"}
            prev_act, new_act = self._rebalance_locked(now)
            added = [u for u in new_act if u not in prev_act]
            if added:
                promoted = added[0]
                prom_lat = self._entries[promoted]["latency_ms"]

        self._notify_if_changed(prev_act, new_act)
        if promoted:
            self._log(
                "INFO",
                f"🔄 [手动换血] 已踢出 {redact_proxy_url(target_url)}，替补入池: {redact_proxy_url(promoted)}"
                + (f" ({prom_lat}ms)" if prom_lat is not None else ""),
                "proxy",
            )
        return {
            "ok": True,
            "evicted": redact_proxy_url(target_url),
            "promoted": redact_proxy_url(promoted) if promoted else "",
            "promoted_latency_ms": prom_lat,
        }

    def _default_probe_one(self, proxy_url: str, endpoint: str, timeout: float) -> int:
        t0 = time.monotonic()
        cli = OVHClient(
            endpoint=endpoint,
            proxy_url=proxy_url,
            timeout=timeout,
            max_retries=1,
            bypass_rate_limit=True,
        )
        cli._raw_request("GET", f"{cli.api_base}/auth/time")
        return max(1, int((time.monotonic() - t0) * 1000))

    def stop_probe(self, wait_seconds: float = 0.0) -> None:
        """Signal any running staggered probe pass to abort immediately."""
        self._cancel_probe.set()
        if wait_seconds > 0:
            deadline = time.monotonic() + wait_seconds
            while self._probing and time.monotonic() < deadline:
                time.sleep(0.03)

    def run_staggered_probe_pass(
        self,
        stop_event: Optional[threading.Event] = None,
        force_all: bool = False,
    ) -> Dict[str, Any]:
        """Probe Master Pool proxies strictly ONE BY ONE with randomized delay between probes."""
        if not self._probe_lock.acquire(blocking=False):
            return {"started": False, "reason": "already_probing"}

        try:
            self._cancel_probe.clear()
            now = time.time()
            with self._lock:
                ep = self.test_endpoint
                max_lat = self.max_latency_ms
                cd_sec = self.cooldown_sec
                master_snap = list(self._master)
                active_set = set(self._active)
                if force_all:
                    # Manual full re-probe: test every node in Master Pool in order from 1..N
                    queue = list(master_snap)
                else:
                    untested = []
                    standby_ready = []
                    active_list = []
                    for u in master_snap:
                        ent = self._ensure_entry_locked(u)
                        if ent["cooldown_until"] > now and u not in active_set:
                            continue
                        if ent["ok"] is None:
                            untested.append(u)
                        elif u not in active_set:
                            standby_ready.append(u)
                        else:
                            active_list.append(u)
                    queue = untested + standby_ready + active_list
                self._probing = True
                self._probe_done = 0
                self._probe_total = len(queue)
                self._current_probe_proxy = ""
                self._last_probe_summary = ""

            if not queue:
                return {"started": True, "probed": 0, "passed": 0}

            timeout_sec = min(8.0, max(2.0, max_lat / 1000.0 + 1.5))
            probed_cnt = 0
            passed_cnt = 0

            for idx, p_url in enumerate(queue):
                if (stop_event and stop_event.is_set()) or self._cancel_probe.is_set():
                    break
                with self._lock:
                    if p_url not in self._master:
                        self._probe_done = idx + 1
                        continue
                    self._current_probe_proxy = redact_proxy_url(p_url)

                probe_ok = False
                ms: Optional[int] = None
                err_msg = ""
                try:
                    if self._custom_probe_fn is not None:
                        ms = int(self._custom_probe_fn(p_url, ep, timeout_sec))
                    else:
                        ms = self._default_probe_one(p_url, ep, timeout_sec)
                    probe_ok = True
                except Exception as exc:
                    err_msg = str(exc)

                if self._cancel_probe.is_set() or (stop_event and stop_event.is_set()):
                    break

                probed_cnt += 1
                ts_now = time.time()
                disp_p = redact_proxy_url(p_url)
                with self._lock:
                    if p_url in self._entries:
                        ent = self._entries[p_url]
                        ent["last_checked_ts"] = ts_now
                        if probe_ok and ms is not None and ms <= self.max_latency_ms:
                            ent["ok"] = True
                            ent["latency_ms"] = ms
                            ent["fail_streak"] = 0
                            ent["cooldown_until"] = 0.0
                            ent["last_error"] = ""
                            ent["ok_count"] += 1
                            passed_cnt += 1
                            self._last_probe_summary = f"✅ {disp_p} ({ms} ms)"
                        else:
                            ent["ok"] = False
                            ent["latency_ms"] = ms if probe_ok else None
                            ent["fail_count"] += 1
                            ent["cooldown_until"] = ts_now + cd_sec
                            ent["last_error"] = (
                                f"延迟超上限 ({ms}ms > {self.max_latency_ms}ms)" if probe_ok else err_msg[:240]
                            )
                            self._last_probe_summary = (
                                f"⚠️ {disp_p} 超上限 ({ms}ms)" if probe_ok else f"❌ {disp_p} 失败"
                            )
                        prev_act, new_act = self._rebalance_locked(ts_now)
                    else:
                        prev_act, new_act = [], []
                    self._probe_done = idx + 1

                if prev_act != new_act:
                    self._notify_if_changed(prev_act, new_act)

                # Staggered random delay before probing the next proxy (interruptible by stop or cancel!)
                if idx + 1 < len(queue) and self.delay_max > 0:
                    delay = random.uniform(self.delay_min, self.delay_max)
                    end_wait = time.monotonic() + delay
                    while time.monotonic() < end_wait:
                        if self._cancel_probe.is_set() or (stop_event and stop_event.is_set()):
                            break
                        rem = min(0.1, end_wait - time.monotonic())
                        if rem > 0:
                            time.sleep(rem)
                    if self._cancel_probe.is_set() or (stop_event and stop_event.is_set()):
                        break

            cancelled = self._cancel_probe.is_set()
            with self._lock:
                act_cnt = len(self._active)
                top_lat = (
                    self._entries[self._active[0]]["latency_ms"]
                    if self._active and self._active[0] in self._entries
                    else None
                )
            if probed_cnt > 0 and not cancelled:
                best_str = f"，最快 {top_lat}ms" if top_lat is not None else ""
                self._log(
                    "INFO",
                    f"🌐 [代理池巡检完成] 逐条测速 {probed_cnt} 条，达标 {passed_cnt} 条，当前活跃池 {act_cnt}/{self.max_active} 条{best_str}",
                    "proxy",
                )
            return {"started": True, "probed": probed_cnt, "passed": passed_cnt, "cancelled": cancelled}
        finally:
            with self._lock:
                self._probing = False
                self._current_probe_proxy = ""
                self.last_pass_at = time.time()
            self._probe_lock.release()

    def get_snapshot(self) -> Dict[str, Any]:
        """Return UI-safe snapshot of Active Pool, Standby Pool, Cooldowns, and probe progress."""
        now = time.time()
        with self._lock:
            active_set = set(self._active)

            def _fmt(url: str, is_active: bool) -> Dict[str, Any]:
                ent = self._ensure_entry_locked(url)
                cd_rem = max(0, int(ent["cooldown_until"] - now))
                return {
                    "id": ent["id"],
                    "proxy": redact_proxy_url(url),
                    "active": is_active,
                    "ok": ent["ok"],
                    "latency_ms": ent["latency_ms"],
                    "fail_streak": ent["fail_streak"],
                    "ok_count": ent["ok_count"],
                    "fail_count": ent["fail_count"],
                    "cooldown_remaining": cd_rem,
                    "last_checked_ago": int(now - ent["last_checked_ts"]) if ent["last_checked_ts"] > 0 else None,
                    "last_error": ent["last_error"],
                }

            active_items = [_fmt(u, True) for u in self._active]
            standby_urls = [u for u in self._master if u not in active_set]
            standby_urls.sort(
                key=lambda u: (
                    1 if self._entries[u]["cooldown_until"] > now else 0,
                    0 if self._entries[u]["ok"] is True else (1 if self._entries[u]["ok"] is None else 2),
                    self._entries[u]["latency_ms"] if self._entries[u]["latency_ms"] is not None else 999999,
                )
            )
            standby_items = [_fmt(u, False) for u in standby_urls]
            cooldown_cnt = sum(1 for x in standby_items if x["cooldown_remaining"] > 0)
            healthy_standby = sum(1 for x in standby_items if x["ok"] is True and x["cooldown_remaining"] == 0)

            return {
                "auto_enabled": self.auto_enabled,
                "max_active": self.max_active,
                "fail_threshold": self.fail_threshold,
                "cooldown_sec": self.cooldown_sec,
                "max_latency_ms": self.max_latency_ms,
                "recheck_interval": self.recheck_interval,
                "test_endpoint": self.test_endpoint,
                "probing": self._probing,
                "probe_done": self._probe_done,
                "probe_total": self._probe_total,
                "current_probe_proxy": self._current_probe_proxy,
                "last_probe_summary": self._last_probe_summary,
                "master_count": len(self._master),
                "active_count": len(active_items),
                "healthy_standby_count": healthy_standby,
                "cooldown_count": cooldown_cnt,
                "active": active_items,
                "standby": standby_items,
            }


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
                raise OVHAPIError("SOCKS5 代理要求账号密码认证", status_code=407)
            u_bytes = username.encode("utf-8")
            p_bytes = password.encode("utf-8")
            if len(u_bytes) > 255 or len(p_bytes) > 255:
                raise OVHAPIError("SOCKS5 账号或密码长度超出限制", status_code=407)
            sock.sendall(b"\x01" + bytes([len(u_bytes)]) + u_bytes + bytes([len(p_bytes)]) + p_bytes)
            _, auth_status = _recv_exact(sock, 2)
            if auth_status != 0x00:
                raise OVHAPIError("SOCKS5 代理认证失败（用户名或密码错误）", status_code=407)
        elif method != 0x00:
            raise OVHAPIError("SOCKS5 代理不支持可用的认证方式", status_code=407)

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


class _BIOStream(io.RawIOBase):
    """RawIOBase adapter exposing recv_into() on _TLSTunnelOverBIO for http.client.HTTPResponse."""

    def __init__(self, bio_sock: "_TLSTunnelOverBIO"):
        self._bio_sock: Optional["_TLSTunnelOverBIO"] = bio_sock

    def readable(self) -> bool:
        return True

    def readinto(self, b: Any) -> int:
        if self.closed or self._bio_sock is None:
            raise ValueError("I/O operation on closed stream")
        return self._bio_sock.recv_into(b)

    def close(self) -> None:
        if not self.closed:
            sock = self._bio_sock
            self._bio_sock = None
            super().close()
            if sock is not None:
                sock._decref_socketios()


class _TLSTunnelOverBIO:
    """Nested TLS-in-TLS socket wrapper using stdlib `ssl.MemoryBIO` over an already-TLS proxy socket.

    Note: Calling `ssl_ctx.wrap_socket(tls_proxy_sock)` directly in Python detaches the raw OS file
    descriptor and destroys the outer proxy TLS session. MemoryBIO preserves both TLS layers cleanly.
    Implements `_io_refs` matching `socket.socket` so `http.client` closing the connection after
    `Connection: close` headers does not tear down `_outer` before `HTTPResponse.read()` finishes.
    """

    def __init__(
        self,
        outer_tls_sock: ssl.SSLSocket,
        ssl_ctx: ssl.SSLContext,
        server_hostname: str,
        initial_bytes: bytes = b"",
    ):
        self._outer = outer_tls_sock
        self._incoming = ssl.MemoryBIO()
        self._outgoing = ssl.MemoryBIO()
        if initial_bytes:
            self._incoming.write(initial_bytes)
        self._sslobj = ssl_ctx.wrap_bio(
            self._incoming,
            self._outgoing,
            server_side=False,
            server_hostname=server_hostname,
        )
        self._io_refs = 0
        self._closed = False
        self._real_closed = False
        self._eof = False
        self._do_handshake()

    def _flush_out(self) -> None:
        pending = self._outgoing.read()
        if pending:
            self._outer.sendall(pending)

    def _pull_in(self) -> bool:
        if self._eof:
            return False
        chunk = self._outer.recv(16384)
        if not chunk:
            self._eof = True
            self._incoming.write_eof()
            return False
        self._incoming.write(chunk)
        return True

    def _do_handshake(self) -> None:
        while True:
            try:
                self._sslobj.do_handshake()
                self._flush_out()
                return
            except ssl.SSLWantReadError:
                self._flush_out()
                if not self._pull_in():
                    raise ssl.SSLEOFError("HTTPS 代理隧道在内层 TLS 握手期间断开")
            except ssl.SSLWantWriteError:
                self._flush_out()

    def settimeout(self, value: Optional[float]) -> None:
        self._outer.settimeout(value)

    def gettimeout(self) -> Optional[float]:
        return self._outer.gettimeout()

    def sendall(self, data: bytes) -> None:
        view = memoryview(data)
        offset = 0
        total = len(view)
        while offset < total:
            try:
                written = self._sslobj.write(view[offset:])
                offset += written
                self._flush_out()
            except ssl.SSLWantReadError:
                self._flush_out()
                if not self._pull_in():
                    raise ssl.SSLEOFError("HTTPS 代理隧道在发送数据时意外关闭")
            except ssl.SSLWantWriteError:
                self._flush_out()

    def send(self, data: bytes) -> int:
        self.sendall(data)
        return len(data)

    def recv(self, bufsize: int = 4096) -> bytes:
        if self._eof and self._incoming.pending == 0:
            return b""
        while True:
            try:
                data = self._sslobj.read(bufsize)
                self._flush_out()
                if not data:
                    self._eof = True
                return data
            except ssl.SSLWantReadError:
                self._flush_out()
                if not self._pull_in():
                    return b""
            except ssl.SSLWantWriteError:
                self._flush_out()
            except (ssl.SSLEOFError, ssl.ZeroReturnError):
                # Suppress ragged EOF after handshake (matching SSLSocket suppress_ragged_eofs=True)
                self._eof = True
                return b""

    def recv_into(self, buffer: Any, nbytes: int = 0) -> int:
        mv = memoryview(buffer)
        want = nbytes if (nbytes and nbytes < len(mv)) else len(mv)
        chunk = self.recv(max(1, want))
        n = len(chunk)
        mv[:n] = chunk
        return n

    def makefile(self, mode: str = "rb", buffering: int = -1) -> Any:
        self._io_refs += 1
        raw = _BIOStream(self)
        buf_size = io.DEFAULT_BUFFER_SIZE if buffering <= 0 else buffering
        return io.BufferedReader(raw, buffer_size=buf_size)

    def _decref_socketios(self) -> None:
        if self._io_refs > 0:
            self._io_refs -= 1
        if self._closed and self._io_refs <= 0:
            self._real_close()

    def _real_close(self) -> None:
        if self._real_closed:
            return
        self._real_closed = True
        try:
            self._outer.close()
        except Exception:
            pass

    def close(self) -> None:
        self._closed = True
        if self._io_refs <= 0:
            self._real_close()


_TUNNEL_STATUS_RE = re.compile(r"(?:HTTP/\d\.\d\s+|Tunnel connection failed:\s*)(\d{3})\b", re.IGNORECASE)


def _extract_tunnel_status_code(text: str) -> int:
    m = _TUNNEL_STATUS_RE.search(str(text or ""))
    if m:
        code = int(m.group(1))
        if 100 <= code <= 599:
            return code
    return 0


class _PreconnectedHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection wrapper around an already-connected (plain, SSLSocket, or _TLSTunnelOverBIO) transport."""

    def __init__(self, host: str, port: int, pre_sock: Any, timeout: float, is_https: bool = False):
        self.default_port = 443 if is_https else 80
        super().__init__(host, port, timeout=timeout)
        self._pre_sock = pre_sock

    def connect(self) -> None:
        self.sock = self._pre_sock


def _execute_proxy_transport(
    method: str,
    full_url: str,
    headers: Dict[str, str],
    body_bytes: Optional[bytes],
    proxy_url: str,
    timeout: float,
) -> Tuple[int, Dict[str, str], bytes]:
    """Execute a single HTTP/HTTPS request through HTTP, HTTPS (TLS-in-TLS), or SOCKS5/SOCKS5H proxy."""
    try:
        proxy_cfg = parse_proxy_config(proxy_url)
    except ValueError as exc:
        raise OVHAPIError(str(exc), status_code=0) from exc
    if not proxy_cfg:
        raise ValueError("proxy_url is empty")

    parsed = urllib.parse.urlsplit(full_url)
    scheme = parsed.scheme.lower()
    target_host = parsed.hostname or ""
    target_port = parsed.port or (443 if scheme == "https" else 80)
    path_with_query = parsed.path or "/"
    if parsed.query:
        path_with_query += "?" + parsed.query

    pscheme = proxy_cfg["scheme"]
    proxy_disp = redact_proxy_url(proxy_url)
    ssl_ctx = _GLOBAL_POOL._ssl_context

    conn: Optional[http.client.HTTPConnection] = None
    opened_transports: List[Any] = []
    try:
        if pscheme in ("socks5", "socks5h"):
            raw_sock: Any = _connect_socks5_socket(proxy_cfg, target_host, target_port, timeout=timeout)
            opened_transports.append(raw_sock)
            if scheme == "https":
                raw_sock = ssl_ctx.wrap_socket(raw_sock, server_hostname=target_host)
                opened_transports.append(raw_sock)
            conn = _PreconnectedHTTPConnection(
                target_host, target_port, raw_sock, timeout=timeout, is_https=(scheme == "https")
            )
            conn.request(method, path_with_query, body=body_bytes, headers=headers)
        else:
            proxy_headers: Dict[str, str] = {}
            if proxy_cfg.get("username") is not None:
                cred = f"{proxy_cfg['username']}:{proxy_cfg.get('password') or ''}".encode("utf-8")
                proxy_headers["Proxy-Authorization"] = "Basic " + base64.b64encode(cred).decode("ascii")

            if scheme == "https":
                if pscheme == "https":
                    outer_sock = socket.create_connection((proxy_cfg["host"], proxy_cfg["port"]), timeout=timeout)
                    opened_transports.append(outer_sock)
                    tls_proxy_sock = ssl_ctx.wrap_socket(outer_sock, server_hostname=proxy_cfg["host"])
                    opened_transports.append(tls_proxy_sock)
                    auth_host = f"[{target_host}]" if (":" in target_host and not target_host.startswith("[")) else target_host
                    connect_lines = [
                        f"CONNECT {auth_host}:{target_port} HTTP/1.1",
                        f"Host: {auth_host}:{target_port}",
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
                    hdr_end = resp_buf.find(b"\r\n\r\n")
                    first_line = resp_buf.split(b"\r\n", 1)[0].decode("latin1", errors="replace")
                    if hdr_end < 0 or not re.match(r"^HTTP/\d\.\d\s+200\b", first_line):
                        p_code = _extract_tunnel_status_code(first_line)
                        err_cls = OVHRateLimitError if p_code == 429 else OVHAPIError
                        raise err_cls(f"HTTPS 代理隧道握手失败 ({proxy_disp}): {first_line}", status_code=p_code)
                    extra_bytes = bytes(resp_buf[hdr_end + 4 :])
                    inner_tls = _TLSTunnelOverBIO(
                        tls_proxy_sock, ssl_ctx=ssl_ctx, server_hostname=target_host, initial_bytes=extra_bytes
                    )
                    opened_transports.append(inner_tls)
                    conn = _PreconnectedHTTPConnection(
                        target_host, target_port, inner_tls, timeout=timeout, is_https=True
                    )
                else:
                    conn = http.client.HTTPSConnection(
                        proxy_cfg["host"], proxy_cfg["port"], timeout=timeout, context=ssl_ctx
                    )
                    conn.set_tunnel(target_host, target_port, headers=proxy_headers or None)
                conn.request(method, path_with_query, body=body_bytes, headers=headers)
            else:
                if pscheme == "https":
                    conn = http.client.HTTPSConnection(
                        proxy_cfg["host"], proxy_cfg["port"], timeout=timeout, context=ssl_ctx
                    )
                else:
                    conn = http.client.HTTPConnection(proxy_cfg["host"], proxy_cfg["port"], timeout=timeout)
                merged_headers = dict(headers)
                merged_headers.update(proxy_headers)
                conn.request(method, full_url, body=body_bytes, headers=merged_headers)

        resp = conn.getresponse()
        try:
            raw = resp.read()
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            return resp.status, resp_headers, raw
        finally:
            try:
                resp.close()
            except Exception:
                pass
    except OVHAPIError:
        raise
    except Exception as exc:
        p_code = _extract_tunnel_status_code(str(exc))
        err_cls = OVHRateLimitError if p_code == 429 else OVHAPIError
        raise err_cls(f"代理请求失败 ({proxy_disp}): {exc}", status_code=p_code) from exc
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        for tr in reversed(opened_transports):
            try:
                tr.close()
            except Exception:
                pass


def proxy_http_request(
    method: str,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    body_bytes: Optional[bytes] = None,
    proxy_url: str = "",
    timeout: float = 15.0,
    max_redirects: int = 3,
) -> Tuple[int, Dict[str, str], bytes]:
    """Perform an HTTP/HTTPS request supporting direct, HTTP, HTTPS (TLS-in-TLS), SOCKS5, and shorthand proxies.

    Used by Telegram Bot, multi-channel Webhooks, and FX rate fetcher so every outbound channel supports
    all proxy formats identically to `OVHClient`.
    """
    norm_proxy = normalize_proxy_url(proxy_url)
    # Support multi-proxy lists in notification/global proxy fields by picking the first valid entry
    if norm_proxy:
        candidates = parse_proxy_list(proxy_url)
        norm_proxy = candidates[0] if candidates else norm_proxy

    req_headers = {"User-Agent": "ovh-buyer/2.2"}
    if headers:
        req_headers.update(headers)

    cur_url = url
    cur_method = method.upper()
    cur_body = body_bytes

    for _ in range(max(1, max_redirects + 1)):
        if norm_proxy:
            status, resp_headers, raw = _execute_proxy_transport(
                cur_method, cur_url, req_headers, cur_body, norm_proxy, timeout=timeout
            )
        else:
            parsed = urllib.parse.urlsplit(cur_url)
            scheme = parsed.scheme.lower()
            host = parsed.hostname or ""
            port = parsed.port or (443 if scheme == "https" else 80)
            pq = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
            conn = (
                http.client.HTTPSConnection(host, port, timeout=timeout, context=_GLOBAL_POOL._ssl_context)
                if scheme == "https"
                else http.client.HTTPConnection(host, port, timeout=timeout)
            )
            try:
                conn.request(cur_method, pq, body=cur_body, headers=req_headers)
                resp = conn.getresponse()
                raw = resp.read()
                status = resp.status
                resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

        if status in (301, 302, 303, 307, 308) and resp_headers.get("location"):
            prev_p = urllib.parse.urlsplit(cur_url)
            cur_url = urllib.parse.urljoin(cur_url, resp_headers["location"])
            next_p = urllib.parse.urlsplit(cur_url)
            if (next_p.scheme.lower(), next_p.hostname, next_p.port) != (prev_p.scheme.lower(), prev_p.hostname, prev_p.port):
                for hk in list(req_headers.keys()):
                    hkl = hk.lower()
                    if hkl in ("authorization", "cookie", "proxy-authorization") or hkl.startswith("x-ovh-"):
                        req_headers.pop(hk, None)
            if status == 303 or (status in (301, 302) and cur_method == "POST"):
                cur_method = "GET"
                cur_body = None
                for hk in list(req_headers.keys()):
                    if hk.lower() in ("content-length", "content-type"):
                        req_headers.pop(hk, None)
            continue
        return status, resp_headers, raw

    return status, resp_headers, raw


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
        bypass_rate_limit: bool = False,
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
        self.bypass_rate_limit = bool(bypass_rate_limit)
        if self.bypass_rate_limit:
            self.rate_limiter: Optional[RateLimiter] = None
        else:
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
        if not normalize_proxy_url(self.proxy_url):
            return self._request_via_pool(method, full_url, headers, body_bytes, allow_resend=False)
        status, resp_headers, raw = _execute_proxy_transport(
            method, full_url, headers, body_bytes, self.proxy_url, timeout=self.timeout
        )
        if 200 <= status < 300:
            return self._decode_response(status, raw, resp_headers)
        self._raise_http_error(status, raw, resp_headers)

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
            if self.rate_limiter is not None:
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
