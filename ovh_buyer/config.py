"""Configuration, constants, and OVH regional metadata."""

import os
from pathlib import Path
from typing import Dict, List, Optional


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


ENDPOINTS: Dict[str, Dict[str, object]] = {
    "ovh-eu": {
        "label": "欧洲区 (OVH EU / IE / FR / DE / GB)",
        "api_base": "https://eu.api.ovh.com/1.0",
        "v1_base": "https://eu.api.ovh.com/v1",
        "token_url": "https://eu.api.ovh.com/createToken/",
        "subsidiaries": ["IE", "FR", "DE", "ES", "GB", "IT", "NL", "PL", "PT", "MA", "SN", "TN", "WE", "WS"],
        "default_subsidiary": "IE",
    },
    "ovh-ca": {
        "label": "加拿大 / 亚太区 (OVH CA / SG / AU / ASIA)",
        "api_base": "https://ca.api.ovh.com/1.0",
        "v1_base": "https://ca.api.ovh.com/v1",
        "token_url": "https://ca.api.ovh.com/createToken/",
        "subsidiaries": ["CA", "QC", "ASIA", "AU", "IN", "SG", "WE", "WS"],
        "default_subsidiary": "CA",
    },
    "ovh-us": {
        "label": "美国区 (OVHcloud US)",
        "api_base": "https://api.us.ovhcloud.com/1.0",
        "v1_base": "https://api.us.ovhcloud.com/v1",
        "token_url": "https://api.us.ovhcloud.com/createToken/",
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
    "vin": {"name": "弗吉尼亚", "country": "美国", "flag": "🇺🇸", "region": "usa"},
    "hil": {"name": "俄勒冈", "country": "美国", "flag": "🇺🇸", "region": "usa"},
    "sgp": {"name": "新加坡", "country": "新加坡", "flag": "🇸🇬", "region": "apac"},
    "syd": {"name": "悉尼", "country": "澳大利亚", "flag": "🇦🇺", "region": "apac"},
    "ynm": {"name": "孟买", "country": "印度", "flag": "🇮🇳", "region": "apac"},
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

UNAVAILABLE_STATUSES = {"unavailable", "unknown", "out-of-stock", ""}
INSTANT_STATUSES = {"available", "1h-high", "1h-low", "1h"}

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


def normalize_datacenter(dc: str) -> str:
    """Convert display or alias datacenter code to OVH API datacenter code."""
    if not dc:
        return ""
    cleaned = dc.strip().lower()
    return DC_ALIASES.get(cleaned, cleaned)


def infer_region_for_datacenter(dc: str) -> Optional[str]:
    """Infer OVH API `region` configuration value from datacenter code."""
    api_dc = normalize_datacenter(dc)
    if not api_dc:
        return None
    if api_dc in DATACENTERS:
        return DATACENTERS[api_dc]["region"]
    prefix = api_dc[:3]
    if prefix in DATACENTERS:
        return DATACENTERS[prefix]["region"]
    return None


def format_datacenter_label(dc: str) -> str:
    """Human-friendly datacenter display name."""
    api_dc = normalize_datacenter(dc)
    prefix = api_dc[:3]
    info = DATACENTERS.get(api_dc) or DATACENTERS.get(prefix)
    if not info:
        return api_dc.upper()
    return f"{info['flag']} {info['country']}·{info['name']} ({api_dc.upper()})"


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
