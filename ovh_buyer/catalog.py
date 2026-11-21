"""OVH Catalog parser, hardware spec normalizer, and config-matching utilities."""

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import CACHE_DIR, NON_HARDWARE_OPTION_TERMS
from .ovh_client import OVHClient


# Patterns for stripping model suffixes from OVH memory/storage addon codes
_MODEL_SUFFIX_PATTERNS = [
    re.compile(r"-\d{2}skl[a-z]\d{1,3}(-v\d+)?$", re.I),   # -24sklea01, -25skleb01, -26skle01
    re.compile(r"-\d{2}sk[a-z]*\d+[a-z]*(-v\d+)?$", re.I), # -24sk502, -25skb01, -24ska01, -24sk60b
    re.compile(r"-\d{2}sys[a-z]*\d*(-v\d+)?$", re.I),      # -25sysle012, -24sysgame01, -24sysstor
    re.compile(r"-\d{2}rise[a-z]*\d*(-v\d+)?$", re.I),     # -24rise, -24risegame01, -25rise01
    re.compile(r"-\d{2}adv[a-z]*\d*(-v\d+)?$", re.I),      # -24adv01, -25advle01
    re.compile(r"-\d{2}game[a-z]*\d*(-v\d+)?$", re.I),
    re.compile(r"-\d{2}stor[a-z]*\d*(-v\d+)?$", re.I),
    re.compile(r"-ks[a-z0-9-]*$", re.I),
    re.compile(r"-rise[0-9]*$", re.I),
    re.compile(r"-game\d*$", re.I),
    re.compile(r"-stor\d*$", re.I),
    re.compile(r"-v\d+$", re.I),
    re.compile(r"-[a-z]{3}$", re.I),                        # -gra, -sgp, -bhs
]


def standardize_config(config_str: str) -> str:
    """Normalize OVH memory/storage code to its core hardware fingerprint.

    Examples:
        'ram-64g-ecc-2400-25skleb01' -> 'ram-64g'
        'ram-64g-noecc-2133'         -> 'ram-64g'
        'softraid-2x450nvme-25skle'  -> 'softraid-2x450nvme'
        'hybridsoftraid-4x4000sa-1x500nvme-24skstor' -> 'hybridsoftraid-4x4000sa-1x500nvme'
    """
    if not config_str:
        return ""
    norm = str(config_str).lower().strip()
    # Apply suffix stripping iteratively (handles chained suffixes like -24sklea01-v1)
    for _ in range(2):
        for pat in _MODEL_SUFFIX_PATTERNS:
            norm = pat.sub("", norm)
    # Remove RAM DDR generation, ECC & frequency modifiers
    norm = re.sub(r"-ddr\d+[a-z]*", "", norm)
    norm = re.sub(r"-(no)?ecc(-\d+)?", "", norm)
    norm = re.sub(r"-\d{4,5}$", "", norm)
    # Trim trailing hyphen if any
    return norm.strip("-")


def extract_ram_gb(text: str) -> Optional[int]:
    """Extract numeric RAM size in GB from strings like 'ram-64g-ecc-2400' or '64GB'."""
    if not text:
        return None
    m = re.search(r"(?:ram-)?(\d+)\s*g(?:b)?\b", text.lower())
    if m:
        return int(m.group(1))
    m2 = re.search(r"ram-(\d+)g", text.lower())
    if m2:
        return int(m2.group(1))
    return None


def format_memory_display(code: str) -> str:
    gb = extract_ram_gb(code)
    if gb:
        return f"{gb} GB RAM"
    return code or "默认内存"


def format_storage_display(code: str) -> str:
    if not code:
        return "默认存储"
    m = re.search(r"(\d+)x(\d+)(nvme|ssd|sa|sas|hdd)", code.lower())
    if m:
        cnt, size, kind = m.group(1), m.group(2), m.group(3).upper()
        if kind == "SA":
            kind = "HDD"
        return f"{cnt}x {size}GB {kind}"
    return code


def is_hardware_option(addon_code: str) -> bool:
    """Return True if addon code is a hardware/bandwidth addon rather than a paid OS/license."""
    low = (addon_code or "").lower()
    if not low:
        return False
    return not any(term in low for term in NON_HARDWARE_OPTION_TERMS)


def match_filter_expression(filter_expr: str, value: str, is_memory: bool = False) -> bool:
    """Flexible user hardware filter matcher.

    Supports:
    - Empty filter -> always True
    - Threshold for RAM: '>=32', '>=64g', '32g'
    - Regex or substring: 'nvme', '2x450nvme', 'ram-64g', 'nvme|ssd'
    - Standardized config equality
    """
    if not filter_expr or not filter_expr.strip():
        return True
    expr = filter_expr.strip().lower()
    val = (value or "").lower()
    if not val:
        return False

    if is_memory:
        cmp_match = re.match(r"^(>=|<=|>|<|=)\s*(\d+)\s*g?b?$", expr)
        if cmp_match:
            op, threshold_str = cmp_match.group(1), cmp_match.group(2)
            threshold = int(threshold_str)
            actual = extract_ram_gb(val)
            if actual is None:
                return False
            if op == ">=":
                return actual >= threshold
            if op == "<=":
                return actual <= threshold
            if op == ">":
                return actual > threshold
            if op == "<":
                return actual < threshold
            return actual == threshold
        plain_gb = re.match(r"^(\d+)\s*g(?:b)?$", expr)
        if plain_gb:
            actual = extract_ram_gb(val)
            if actual is not None:
                return actual == int(plain_gb.group(1))

    if standardize_config(expr) and standardize_config(expr) == standardize_config(val):
        return True
    if expr in val:
        return True
    try:
        return re.search(expr, val, re.I) is not None
    except re.error:
        return False


def _price_micro_to_float(raw_price: Any) -> Optional[float]:
    """OVH public catalog prices are in 1e-8 units (micro-cents * 100)."""
    if isinstance(raw_price, (int, float)):
        return round(float(raw_price) / 100_000_000.0, 2)
    return None


def pick_best_pricing(pricings: List[Dict[str, Any]], preferred_mode: str = "auto", preferred_duration: str = "auto") -> Tuple[str, str, Optional[float], Optional[float]]:
    """Select the best (pricingMode, duration, monthly_price, setup_fee) from catalog pricings."""
    if not pricings:
        return ("default", "P1M", None, None)

    renew_items = [p for p in pricings if "renew" in (p.get("capacities") or [])]
    install_items = [p for p in pricings if "installation" in (p.get("capacities") or [])]
    candidates = renew_items or pricings

    chosen = None
    if preferred_mode and preferred_mode != "auto":
        for p in candidates:
            if p.get("mode") == preferred_mode:
                chosen = p
                break
    if chosen is None:
        # Prefer 'default' monthly mode if available, else lowest price
        defaults = [p for p in candidates if p.get("mode") == "default"]
        pool = defaults or candidates
        pool.sort(key=lambda x: (x.get("interval") or 1, x.get("price") or 10**18))
        chosen = pool[0]

    mode = str(chosen.get("mode") or "default")
    interval = int(chosen.get("interval") or 1)
    unit = str(chosen.get("intervalUnit") or "month").lower()
    if preferred_duration and preferred_duration != "auto":
        duration = preferred_duration
    elif unit.startswith("year"):
        duration = f"P{interval}Y"
    else:
        duration = f"P{interval}M"

    price_val = _price_micro_to_float(chosen.get("price"))
    setup_val = None
    for inst in install_items:
        if inst.get("mode") == mode:
            setup_val = _price_micro_to_float(inst.get("price"))
            break
    return (mode, duration, price_val, setup_val)


def parse_catalog_plans(raw_catalog: Dict[str, Any], product_line: str = "eco") -> List[Dict[str, Any]]:
    """Parse OVH public catalog JSON into clean structured server plan summaries."""
    currency = ((raw_catalog.get("locale") or {}).get("currencyCode")) or "EUR"
    products_by_name = {
        p.get("name"): p
        for p in (raw_catalog.get("products") or [])
        if isinstance(p, dict) and p.get("name")
    }
    addons_by_code = {
        a.get("planCode"): a
        for a in (raw_catalog.get("addons") or [])
        if isinstance(a, dict) and a.get("planCode")
    }

    parsed: List[Dict[str, Any]] = []
    for plan in raw_catalog.get("plans") or []:
        if not isinstance(plan, dict):
            continue
        plan_code = plan.get("planCode")
        if not plan_code:
            continue
        invoice_name = plan.get("invoiceName") or plan.get("displayName") or plan_code
        product_ref = products_by_name.get(plan.get("product") or plan_code, {})
        blobs = ((product_ref.get("blobs") or {}).get("technical")) or {}

        # Extract CPU from technical blobs (nested under server.cpu or cpu), product description, or invoice name
        cpu_str = "N/A"
        server_blob = blobs.get("server") if isinstance(blobs.get("server"), dict) else {}
        cpu_blob = server_blob.get("cpu") or blobs.get("cpu") or {}
        if isinstance(cpu_blob, dict) and (cpu_blob.get("brand") or cpu_blob.get("model")):
            parts = [str(cpu_blob.get("brand") or ""), str(cpu_blob.get("model") or "")]
            cores = cpu_blob.get("cores")
            threads = cpu_blob.get("threads")
            cpu_str = " ".join(p for p in parts if p).strip()
            if cores:
                cpu_str += f" ({cores}c/{threads or cores}t)"
        elif product_ref.get("description"):
            cpu_str = str(product_ref["description"]).strip()
        elif "|" in invoice_name:
            cpu_str = invoice_name.split("|", 1)[1].strip()

        # Parse addon families (memory, storage, bandwidth, vrack)
        addon_families = plan.get("addonFamilies") or []
        memory_default = ""
        storage_default = ""
        bandwidth_default = ""
        memory_addons: List[str] = []
        storage_addons: List[str] = []
        bandwidth_addons: List[str] = []
        default_addons: List[str] = []
        all_hw_addons: List[Dict[str, Any]] = []

        for fam in addon_families:
            if not isinstance(fam, dict):
                continue
            fname = str(fam.get("name") or "").lower()
            default_code = fam.get("default") or ""
            mandatory = bool(fam.get("mandatory"))
            codes = [c for c in (fam.get("addons") or []) if isinstance(c, str) and is_hardware_option(c)]
            if default_code and is_hardware_option(default_code):
                default_addons.append(default_code)
            if "memory" in fname or "ram" in fname:
                memory_addons.extend(codes)
                memory_default = default_code or (codes[0] if codes else "")
            elif "storage" in fname or "disk" in fname:
                storage_addons.extend(codes)
                storage_default = default_code or (codes[0] if codes else "")
            elif "bandwidth" in fname and "vrack" not in fname:
                bandwidth_addons.extend(codes)
                bandwidth_default = default_code or (codes[0] if codes else "")

            for code in codes:
                addon_meta = addons_by_code.get(code, {})
                _, _, addon_price, _ = pick_best_pricing(addon_meta.get("pricings") or [])
                all_hw_addons.append(
                    {
                        "planCode": code,
                        "family": fname,
                        "mandatory": mandatory,
                        "isDefault": code == default_code,
                        "invoiceName": addon_meta.get("invoiceName") or code,
                        "price": addon_price,
                    }
                )

        # Fallback RAM/storage from blobs if addonFamilies was sparse
        if not memory_default and isinstance(blobs.get("memory"), dict):
            mem_size = blobs["memory"].get("size")
            if mem_size:
                memory_default = f"{mem_size} GB"
        if not storage_default and isinstance(blobs.get("storage"), dict):
            disks = blobs["storage"].get("disks") or []
            if disks and isinstance(disks[0], dict):
                d0 = disks[0]
                storage_default = f"{d0.get('number', 1)}x{d0.get('capacity', '')}{d0.get('technology', '')}"

        pricing_mode, duration, base_price, setup_fee = pick_best_pricing(plan.get("pricings") or [])
        pricing_modes = sorted({str(p.get("mode")) for p in (plan.get("pricings") or []) if p.get("mode")})

        # Available datacenters listed in configurations (if present)
        config_dcs: List[str] = []
        for cfg in plan.get("configurations") or []:
            if isinstance(cfg, dict) and cfg.get("name") in ("dedicated_datacenter", "vps_datacenter"):
                config_dcs = [str(v).lower() for v in (cfg.get("values") or []) if v]

        parsed.append(
            {
                "planCode": plan_code,
                "name": invoice_name,
                "productLine": product_line,
                "cpu": cpu_str,
                "memory": format_memory_display(memory_default),
                "memoryCode": memory_default,
                "storage": format_storage_display(storage_default),
                "storageCode": storage_default,
                "bandwidth": bandwidth_default or "默认带宽",
                "price": base_price,
                "setupFee": setup_fee,
                "currency": currency,
                "defaultPricingMode": pricing_mode,
                "defaultDuration": duration,
                "pricingModes": pricing_modes,
                "datacenters": config_dcs,
                "defaultAddons": default_addons,
                "memoryAddons": memory_addons,
                "storageAddons": storage_addons,
                "bandwidthAddons": bandwidth_addons,
                "addons": all_hw_addons,
            }
        )

    parsed.sort(key=lambda x: (x["price"] is None, x["price"] or 999999.0, x["planCode"]))
    return parsed


def resolve_addons_for_fqn(plan_summary: Optional[Dict[str, Any]], fqn_memory: str, fqn_storage: str) -> List[str]:
    """Match availability FQN memory/storage codes to catalog addon planCodes."""
    if not plan_summary:
        return []
    resolved: List[str] = []
    mem_std = standardize_config(fqn_memory)
    stor_std = standardize_config(fqn_storage)

    if mem_std:
        for addon in plan_summary.get("memoryAddons") or []:
            if standardize_config(addon) == mem_std:
                resolved.append(addon)
                break
    if stor_std:
        for addon in plan_summary.get("storageAddons") or []:
            if standardize_config(addon) == stor_std:
                resolved.append(addon)
                break
    return resolved


class CatalogManager:
    """In-memory + on-disk cache manager for OVH public catalogs."""

    def __init__(self, ttl_seconds: int = 900, cache_dir: Optional[Path] = None):
        self.ttl_seconds = ttl_seconds
        self.cache_dir = Path(cache_dir or CACHE_DIR)
        self._lock = threading.RLock()
        self._memory_cache: Dict[Tuple[str, str, str], Tuple[float, Dict[str, Any], List[Dict[str, Any]]]] = {}
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _disk_path(self, endpoint: str, subsidiary: str, product_line: str) -> Path:
        safe = f"catalog_{endpoint}_{subsidiary.upper()}_{product_line.lower()}.json"
        return self.cache_dir / safe

    def get_catalog_plans(
        self,
        client: OVHClient,
        subsidiary: str = "IE",
        product_line: str = "eco",
        force_refresh: bool = False,
    ) -> List[Dict[str, Any]]:
        key = (client.endpoint, subsidiary.upper(), product_line.lower())
        now = time.time()
        with self._lock:
            cached = self._memory_cache.get(key)
            if cached and not force_refresh and (now - cached[0] < self.ttl_seconds):
                return cached[2]

        # Try disk cache if not forcing refresh
        disk_file = self._disk_path(*key)
        if not force_refresh and disk_file.is_file():
            try:
                mtime = disk_file.stat().st_mtime
                if now - mtime < self.ttl_seconds:
                    raw = json.loads(disk_file.read_text(encoding="utf-8"))
                    plans = parse_catalog_plans(raw, product_line=product_line)
                    with self._lock:
                        self._memory_cache[key] = (mtime, raw, plans)
                    return plans
            except Exception:
                pass

        raw = client.get_catalog(subsidiary=subsidiary, product_line=product_line)
        plans = parse_catalog_plans(raw, product_line=product_line)
        with self._lock:
            self._memory_cache[key] = (now, raw, plans)
        try:
            disk_file.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        return plans

    def find_plan(
        self,
        client: OVHClient,
        plan_code: str,
        subsidiary: str = "IE",
        product_line: str = "eco",
    ) -> Optional[Dict[str, Any]]:
        plans = self.get_catalog_plans(client, subsidiary=subsidiary, product_line=product_line)
        for p in plans:
            if p["planCode"] == plan_code:
                return p
        return None

    def clear(self) -> None:
        with self._lock:
            self._memory_cache.clear()
        for f in self.cache_dir.glob("catalog_*.json"):
            try:
                f.unlink()
            except OSError:
                pass
