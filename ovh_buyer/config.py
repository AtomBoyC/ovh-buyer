"""Configuration, constants, OVH regional metadata, Express URL & short-link helpers."""

import base64
import binascii
import os
import re
import urllib.parse
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("OVH_DATA_DIR", str(BASE_DIR / "data"))).resolve()
DB_PATH = DATA_DIR / "ovh_buyer.db"
CACHE_DIR = DATA_DIR / "cache"


def load_dotenv_file(env_path: Optional[Path] = None) -> None:
    """Load simple KEY=VALUE lines from .env without overriding existing env vars."""
    target = env_path or (BASE_DIR / ".env")
    if not target.is_file():
        return
    try:
        with target.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


load_dotenv_file()


# Least-privilege OVH API rights: account read, cart/order CRUD, delivered server read-only (no server delete/reboot!)
MINIMAL_ACCESS_RULES: List[Dict[str, str]] = [
    {"method": "GET", "path": "/me"},
    {"method": "GET", "path": "/me/*"},
    {"method": "GET", "path": "/order/*"},
    {"method": "POST", "path": "/order/*"},
    {"method": "PUT", "path": "/order/*"},
    {"method": "DELETE", "path": "/order/*"},
    {"method": "GET", "path": "/dedicated/server"},
    {"method": "GET", "path": "/dedicated/server/*"},
]


def create_token_url(endpoint: str) -> str:
    """Return OVH createToken URL prefilled with least-privilege access rules."""
    hosts = {
        "ovh-eu": "https://eu.api.ovh.com",
        "ovh-ca": "https://ca.api.ovh.com",
        "ovh-us": "https://api.us.ovhcloud.com",
    }
    base = hosts.get(endpoint, hosts["ovh-eu"])
    qs = "&".join(
        f"{r['method']}={urllib.parse.quote(r['path'], safe='/*')}" for r in MINIMAL_ACCESS_RULES
    )
    return f"{base}/createToken/?{qs}"


ENDPOINTS: Dict[str, Dict[str, object]] = {
    "ovh-eu": {
        "label": "欧洲区 (OVH EU / IE / FR / DE / GB)",
        "api_base": "https://eu.api.ovh.com/1.0",
        "v1_base": "https://eu.api.ovh.com/v1",
        "token_url": create_token_url("ovh-eu"),
        "subsidiaries": ["IE", "FR", "DE", "ES", "GB", "IT", "NL", "PL", "PT", "CZ", "FI", "LT", "MA", "SN", "TN", "WE", "WS"],
        "default_subsidiary": "IE",
    },
    "ovh-ca": {
        "label": "加拿大 / 亚太区 (OVH CA / SG / AU / ASIA)",
        "api_base": "https://ca.api.ovh.com/1.0",
        "v1_base": "https://ca.api.ovh.com/v1",
        "token_url": create_token_url("ovh-ca"),
        "subsidiaries": ["CA", "QC", "ASIA", "AU", "IN", "SG", "WE", "WS"],
        "default_subsidiary": "CA",
    },
    "ovh-us": {
        "label": "美国区 (OVHcloud US)",
        "api_base": "https://api.us.ovhcloud.com/1.0",
        "v1_base": "https://api.us.ovhcloud.com/v1",
        "token_url": create_token_url("ovh-us"),
        "subsidiaries": ["US"],
        "default_subsidiary": "US",
    },
}


# Datacenter metadata: code -> {name, country, flag, region}
DATACENTERS: Dict[str, Dict[str, str]] = {
    "gra": {"name": "格拉沃利讷", "country": "法国", "flag": "🇫🇷", "region": "europe"},
    "rbx": {"name": "鲁贝", "country": "法国", "flag": "🇫🇷", "region": "europe"},
    "sbg": {"name": "斯特拉斯堡", "country": "法国", "flag": "🇫🇷", "region": "europe"},
    "par": {"name": "巴黎", "country": "法国", "flag": "🇫🇷", "region": "europe"},
    "fra": {"name": "法兰克福", "country": "德国", "flag": "🇩🇪", "region": "europe"},
    "lim": {"name": "林堡", "country": "德国", "flag": "🇩🇪", "region": "europe"},
    "lon": {"name": "伦敦", "country": "英国", "flag": "🇬🇧", "region": "europe"},
    "eri": {"name": "埃里思", "country": "英国", "flag": "🇬🇧", "region": "europe"},
    "waw": {"name": "华沙", "country": "波兰", "flag": "🇵🇱", "region": "europe"},
    "mil": {"name": "米兰", "country": "意大利", "flag": "🇮🇹", "region": "europe"},
    "mad": {"name": "马德里", "country": "西班牙", "flag": "🇪🇸", "region": "europe"},
    "bhs": {"name": "博阿尔诺", "country": "加拿大", "flag": "🇨🇦", "region": "canada"},
    "tor": {"name": "多伦多", "country": "加拿大", "flag": "🇨🇦", "region": "canada"},
    "yyz": {"name": "多伦多", "country": "加拿大", "flag": "🇨🇦", "region": "canada"},
    "vin": {"name": "弗吉尼亚", "country": "美国", "flag": "🇺🇸", "region": "usa"},
    "hil": {"name": "俄勒冈", "country": "美国", "flag": "🇺🇸", "region": "usa"},
    "sgp": {"name": "新加坡", "country": "新加坡", "flag": "🇸🇬", "region": "apac"},
    "syd": {"name": "悉尼", "country": "澳大利亚", "flag": "🇦🇺", "region": "apac"},
    "ynm": {"name": "孟买", "country": "印度", "flag": "🇮🇳", "region": "apac"},
    "bom": {"name": "孟买", "country": "印度", "flag": "🇮🇳", "region": "apac"},
    "tok": {"name": "东京", "country": "日本", "flag": "🇯🇵", "region": "apac"},
}

DC_ALIASES: Dict[str, str] = {
    "mum": "ynm",
    "mumbai": "ynm",
    "sg": "sgp",
    "de": "fra",
    "uk": "lon",
    "ca": "bhs",
}

UNAVAILABLE_STATUSES = {"unavailable", "unknown", "out-of-stock", "comingsoon", ""}
INSTANT_STATUSES = {"available", "1h-high", "1h-low", "1h"}

AVAILABILITY_LABELS: Dict[str, str] = {
    "1h-high": "1小时内 · 现货充足",
    "1h-low": "1小时内 · 少量现货",
    "available": "现货可订",
    "24h": "24小时内交付",
    "72h": "72小时内交付",
    "240h": "约10天交付",
    "480h": "约20天交付",
    "comingsoon": "即将上架",
    "unavailable": "无货",
    "unknown": "未知",
}

OVH_ORDER_STATUS_LABELS: Dict[str, str] = {
    "notPaid": "未付款",
    "checking": "核查中",
    "delivering": "交付中",
    "delivered": "已交付",
    "documentsRequested": "需提交资料",
    "cancelling": "取消中",
    "cancelled": "已取消",
    "expired": "已过期",
    "unknown": "未知",
}

OVH_PAID_STATUSES = {"checking", "delivering", "delivered", "documentsRequested"}
OVH_CANCELLED_STATUSES = {"cancelled", "cancelling", "expired"}

NON_HARDWARE_OPTION_TERMS = (
    "windows-server",
    "sql-server",
    "cpanel-license",
    "plesk-",
    "-license-",
    "os-",
    "control-panel",
    "spla",
    "license",
)

DEFAULT_VPS_MODELS: List[str] = [
    "vps-2025-model1",
    "vps-2025-model2",
    "vps-2025-model3",
    "vps-2025-model4",
    "vps-2025-model5",
    "vps-2025-model6",
    "vps-2026-model1",
    "vps-2026-model2",
]

# Express Order URL mappings
EXPRESS_ORDER_HOST: Dict[str, str] = {
    "ovh-eu": "https://order.eu.ovhcloud.com",
    "ovh-ca": "https://order.ca.ovhcloud.com",
    "ovh-us": "https://order.us.ovhcloud.com",
}
EXPRESS_ORDER_LOCALE: Dict[str, str] = {
    "FR": "fr",
    "IE": "en-ie",
    "GB": "en-gb",
    "DE": "de",
    "ES": "es",
    "IT": "it",
    "NL": "nl",
    "PL": "pl",
    "PT": "pt",
    "MA": "ma",
    "SN": "sn",
    "TN": "tn",
    "CA": "en",
    "QC": "fr",
    "ASIA": "asia",
    "AU": "au",
    "SG": "sg",
    "IN": "in",
    "WS": "es",
    "WE": "en",
    "US": "en",
}

SHORT_LINK_FIELDS = (
    "plan_code",
    "datacenter",
    "memory",
    "storage",
    "bandwidth",
    "system_storage",
    "vrack",
)
_SHORT_LINK_CODE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
_SHORT_LINK_MAX_BYTES = 2048
SHORT_LINK_REGION_TO_ENDPOINT = {
    "e": "ovh-eu",
    "a": "ovh-ca",
    "u": "ovh-us",
}


def normalize_datacenter(dc: str) -> str:
    """Convert display or alias datacenter code to OVH API datacenter code."""
    if not dc:
        return ""
    cleaned = dc.strip().lower()
    return DC_ALIASES.get(cleaned, cleaned)


def infer_region_for_datacenter(dc: str, allowed: Optional[List[str]] = None) -> Optional[str]:
    """Infer OVH API `region` configuration value from datacenter code."""
    api_dc = normalize_datacenter(dc)
    if not api_dc:
        return allowed[0] if allowed else None
    info = DATACENTERS.get(api_dc) or DATACENTERS.get(api_dc[:3])
    cand = info["region"] if info else None
    if cand == "apac" and allowed and "apac" not in allowed and "asia" in allowed:
        cand = "asia"
    if cand == "usa" and allowed and "usa" not in allowed and "us" in allowed:
        cand = "us"
    if allowed and cand not in allowed:
        return allowed[0]
    return cand


def format_datacenter_label(dc: str) -> str:
    """Human-friendly datacenter display name."""
    api_dc = normalize_datacenter(dc)
    prefix = api_dc[:3]
    info = DATACENTERS.get(api_dc) or DATACENTERS.get(prefix)
    if not info:
        return api_dc.upper()
    return f"{info['flag']} {info['country']}·{info['name']} ({api_dc.upper()})"


def format_availability_label(status: str) -> str:
    key = (status or "unknown").strip().lower()
    return AVAILABILITY_LABELS.get(key, status or "未知")


def is_availability_orderable(status: str, instant_only: bool = False) -> bool:
    """Return True if an OVH availability string represents purchasable stock."""
    if not status:
        return False
    norm = str(status).strip().lower()
    if norm in UNAVAILABLE_STATUSES:
        return False
    if instant_only:
        return norm in INSTANT_STATUSES
    return True


def _jsurl_encode(value: Any) -> str:
    """Minimal JSURL encoder matching OVH's Express Order ?products= query format."""
    if isinstance(value, dict):
        return "~(" + "~".join(f"{k}{_jsurl_encode(v)}" for k, v in value.items()) + ")"
    if isinstance(value, list):
        return "~(" + "".join(_jsurl_encode(v) for v in value) + ")"
    if isinstance(value, bool):
        return "~" + ("true" if value else "false")
    if isinstance(value, (int, float)):
        return "~" + str(value)
    encoded = "".join(
        c
        if (c.isalnum() or c in "-_.*")
        else ("!" + c if c in "~'!()" else ("*" + format(ord(c), "02x") if c != " " else " "))
        for c in str(value)
    )
    return "~'" + encoded


def build_express_order_url(
    endpoint: str,
    subsidiary: str,
    plan_code: str,
    product_line: str = "eco",
    datacenter: str = "",
    duration: str = "P1M",
    pricing_mode: str = "default",
    os_code: str = "none_64.en",
    options: Optional[List[str]] = None,
) -> str:
    """Build direct OVH Express Order review URL with plan, DC, region, OS & options preselected."""
    sub = (subsidiary or "IE").upper()
    host = EXPRESS_ORDER_HOST.get(endpoint, EXPRESS_ORDER_HOST["ovh-eu"])
    loc = EXPRESS_ORDER_LOCALE.get(sub, "en-ie")
    api_dc = normalize_datacenter(datacenter) if datacenter else ""
    cfg: List[Dict[str, str]] = []
    if api_dc:
        cfg.append({"label": "dedicated_datacenter", "value": api_dc})
        reg = infer_region_for_datacenter(api_dc)
        if reg:
            cfg.append({"label": "region", "value": reg})
    if os_code:
        cfg.append({"label": "dedicated_os", "value": os_code})

    product_id = "baremetalServers" if product_line.lower() in ("baremetal", "baremetalservers") else "eco"
    prod: Dict[str, Any] = {
        "planCode": plan_code,
        "productId": product_id,
        "pricingMode": pricing_mode if pricing_mode and pricing_mode != "auto" else "default",
        "quantity": 1,
        "duration": duration if duration and duration != "auto" else "P1M",
        "configuration": cfg,
    }
    if options:
        prod["option"] = [
            {
                "planCode": opt,
                "pricingMode": prod["pricingMode"],
                "quantity": 1,
                "duration": prod["duration"],
            }
            for opt in options
            if opt
        ]
    return f"{host}/{loc}/express/#/express/review?products={_jsurl_encode([prod])}"


def decode_short_order_url(url: str) -> Dict[str, Any]:
    """Decode an @ovheco / PoorVPS /o/<region>/<token> short order link locally (zero external fetch)."""
    parsed = urllib.parse.urlsplit(url.strip())
    parts = [p for p in parsed.path.split("/") if p]
    if parsed.scheme not in ("http", "https") or not parsed.hostname or len(parts) < 3 or parts[-3] != "o":
        raise ValueError("链接格式不正确，需要包含 /o/<region>/<token>")
    link_region, token = parts[-2].lower(), parts[-1]
    if link_region not in SHORT_LINK_REGION_TO_ENDPOINT:
        raise ValueError(f"不支持的区域短码: {link_region} (仅支持 e/a/u)")
    if not token or len(token) > _SHORT_LINK_MAX_BYTES:
        raise ValueError("短链 Token 长度异常")
    try:
        padded = token + "=" * (-len(token) % 4)
        compressed = base64.b64decode(padded, altchars=b"-_", validate=True)
        decompressor = zlib.decompressobj(wbits=-zlib.MAX_WBITS)
        raw = decompressor.decompress(compressed, _SHORT_LINK_MAX_BYTES + 1)
        if len(raw) > _SHORT_LINK_MAX_BYTES or decompressor.unconsumed_tail or decompressor.unused_data:
            raise ValueError("短链载荷过大")
        raw += decompressor.flush()
        if len(raw) > _SHORT_LINK_MAX_BYTES or not decompressor.eof or decompressor.unused_data:
            raise ValueError("短链载荷不完整")
        values = raw.decode("utf-8").split("\x1f")
    except (binascii.Error, zlib.error, UnicodeDecodeError) as exc:
        raise ValueError(f"无法解码短链 Token: {exc}") from exc

    if len(values) != len(SHORT_LINK_FIELDS):
        raise ValueError("短链字段数量不合法")
    if any(not v for v in values[:4]):
        raise ValueError("短链缺少必填字段 (plan_code/datacenter/memory/storage)")
    if any(v and not _SHORT_LINK_CODE_RE.fullmatch(v) for v in values):
        raise ValueError("短链包含非法产品代码字符")

    data = dict(zip(SHORT_LINK_FIELDS, values))
    options = [
        data[k]
        for k in ("memory", "storage", "bandwidth", "system_storage", "vrack")
        if data.get(k)
    ]
    return {
        "link_region": link_region,
        "endpoint": SHORT_LINK_REGION_TO_ENDPOINT[link_region],
        "plan_code": data["plan_code"],
        "datacenter": normalize_datacenter(data["datacenter"]),
        "memory": data["memory"],
        "storage": data["storage"],
        "bandwidth": data["bandwidth"],
        "system_storage": data["system_storage"],
        "vrack": data["vrack"],
        "options": options,
    }


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def get_server_config() -> Dict[str, object]:
    """Read runtime server configuration from environment variables."""
    return {
        "host": os.environ.get("HOST", "0.0.0.0"),
        "port": env_int("PORT", 8888),
        "panel_password": os.environ.get("PANEL_PASSWORD") or os.environ.get("API_SECRET_KEY", ""),
        "headless": env_bool("HEADLESS", False),
        "auto_start": env_bool("AUTO_START", True),
        "timezone": os.environ.get("TZ", "Asia/Shanghai"),
    }
