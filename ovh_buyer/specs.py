"""Hardware specification parser, OVH 'Lottery' (中奖) comparator, and lightweight FX CNY converter."""

import json
import re
import threading
import time
import urllib.request
from typing import Any, Dict, List, Optional

from .catalog import resolve_addons_for_fqn
from .ovh_client import proxy_http_request


DISK_RANK = {"nvme": 3, "ssd": 2, "sas": 1, "sata": 0, "hdd": 0}
DISK_LABEL = {3: "NVMe", 2: "SSD", 1: "SAS", 0: "HDD"}

# Fallback conservative CNY exchange rates when offline
DEFAULT_CNY_RATES: Dict[str, float] = {
    "EUR": 7.85,
    "USD": 7.25,
    "CAD": 5.20,
    "GBP": 9.20,
    "PLN": 1.82,
    "AUD": 4.75,
    "SGD": 5.45,
    "CNY": 1.0,
}

CURRENCY_SYMBOLS: Dict[str, str] = {
    "EUR": "€",
    "USD": "$",
    "CAD": "CA$",
    "GBP": "£",
    "PLN": "zł",
    "AUD": "A$",
    "SGD": "S$",
    "CNY": "¥",
}


class FXConverter:
    """Cached zero-dependency currency-to-CNY converter with safe offline fallback."""

    def __init__(self, ttl_seconds: int = 21600):
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._rates: Dict[str, float] = dict(DEFAULT_CNY_RATES)
        self._fetched_at: float = 0.0

    def get_rates(self, proxy_url: str = "") -> Dict[str, float]:
        now = time.time()
        with self._lock:
            if self._fetched_at > 0 and (now - self._fetched_at < self.ttl_seconds):
                return dict(self._rates)
        # Fetch live ECB/Frankfurter rates with base CNY (supports HTTP/HTTPS/SOCKS5/shorthand proxies)
        try:
            url = "https://api.frankfurter.app/latest?from=CNY&to=EUR,USD,CAD,GBP,PLN,AUD,SGD"
            status, _hdrs, raw_bytes = proxy_http_request("GET", url, proxy_url=proxy_url, timeout=6.0)
            if 200 <= status < 300:
                data = json.loads(raw_bytes.decode("utf-8"))
                rates = data.get("rates") or {}
                updated = dict(DEFAULT_CNY_RATES)
                for code, per_cny in rates.items():
                    if isinstance(per_cny, (int, float)) and per_cny > 0:
                        updated[code.upper()] = round(1.0 / float(per_cny), 4)
                with self._lock:
                    self._rates = updated
                    self._fetched_at = time.time()
                return dict(updated)
            raise OSError(f"HTTP {status}")
        except Exception:
            with self._lock:
                # Avoid retrying on every request when offline
                self._fetched_at = time.time() - self.ttl_seconds + 600
                return dict(self._rates)

    def to_cny(self, amount: Optional[float], currency: str = "EUR", proxy_url: str = "") -> Optional[float]:
        if amount is None:
            return None
        rates = self.get_rates(proxy_url=proxy_url)
        rate = rates.get((currency or "EUR").upper())
        if not rate:
            return None
        return round(float(amount) * rate, 2)

    def format_price_with_cny(self, amount: Optional[float], currency: str = "EUR", proxy_url: str = "") -> str:
        if amount is None:
            return "-"
        cur = (currency or "EUR").upper()
        sym = CURRENCY_SYMBOLS.get(cur, cur + " ")
        base = f"{sym}{float(amount):.2f}"
        if cur == "CNY":
            return base
        cny = self.to_cny(amount, cur, proxy_url=proxy_url)
        return f"{base} (≈ ¥{cny:.2f})" if cny is not None else base


GLOBAL_FX = FXConverter()


def _norm_cpu(name: str) -> str:
    s = (name or "").lower()
    s = re.sub(r"\(r\)|\(tm\)|cpu|processor|@.*$", "", s)
    return re.sub(r"[^a-z0-9]", "", s)


def _disk_rank(kind: str) -> int:
    k = (kind or "").lower()
    for token, rank in DISK_RANK.items():
        if token in k:
            return rank
    return 0


def _size_to_gb(val: Any) -> Optional[float]:
    if isinstance(val, dict):
        if val.get("value") is None:
            return None
        v = float(val["value"])
        unit = str(val.get("unit") or "GB").upper()
        if unit == "MB":
            return v / 1024.0
        if unit == "TB":
            return v * 1000.0
        if unit == "B":
            return v / (1024.0 ** 3)
        return v
    if isinstance(val, (int, float)):
        return float(val)
    return None


def _format_disks(disks: List[Dict[str, Any]]) -> str:
    parts = []
    for d in disks:
        gb = float(d.get("gb") or 0)
        size_s = f"{gb / 1000.0:g} TB" if gb >= 1000 else f"{gb:g} GB"
        kind = DISK_LABEL.get(int(d.get("rank") or 0), "")
        parts.append(f"{d.get('n') or 1}×{size_s} {kind}".strip())
    return " + ".join(parts) or "-"


def _format_spec_summary(cpu: Dict[str, Any], ram_gb: Optional[float], disks: List[Dict[str, Any]]) -> str:
    cpu_txt = cpu.get("model") or "-"
    if cpu.get("cores"):
        cpu_txt += f" ({cpu['cores']}C/{cpu.get('threads') or cpu['cores']}T)"
    ram_txt = f"{ram_gb:g} GB" if ram_gb else "-"
    return f"{cpu_txt} · {ram_txt} · {_format_disks(disks)}"


def extract_cpu_scores_from_catalog(raw_catalog: Dict[str, Any]) -> Dict[str, float]:
    """Collect CPU benchmark scores from OVH catalog technical blobs."""
    out: Dict[str, float] = {}
    for prod in raw_catalog.get("products") or []:
        if not isinstance(prod, dict):
            continue
        blobs = (prod.get("blobs") or {}).get("technical") or {}
        srv = blobs.get("server") if isinstance(blobs.get("server"), dict) else {}
        cpu = srv.get("cpu") or blobs.get("cpu") or {}
        if isinstance(cpu, dict) and cpu.get("model") and cpu.get("score"):
            score = float(cpu["score"])
            out[_norm_cpu(f"{cpu.get('brand', '')} {cpu['model']}")] = score
            out[_norm_cpu(str(cpu["model"]))] = score
    return out


def parse_official_spec(
    raw_catalog: Dict[str, Any],
    plan_summary: Optional[Dict[str, Any]],
    plan_code: str,
    memory: str = "",
    storage: str = "",
) -> Optional[Dict[str, Any]]:
    """Build official catalog hardware specification for a plan + memory/storage."""
    plans_by_code = {p.get("planCode"): p for p in (raw_catalog.get("plans") or []) if isinstance(p, dict)}
    products_by_name = {p.get("name"): p for p in (raw_catalog.get("products") or []) if isinstance(p, dict)}
    addons_by_code = {a.get("planCode"): a for a in (raw_catalog.get("addons") or []) if isinstance(a, dict)}

    plan = plans_by_code.get(plan_code)
    if not plan:
        # Handle retired planCode -> successor like 24rise01 -> 24rise01-v2
        succ = sorted(c for c in plans_by_code if re.fullmatch(re.escape(plan_code) + r"-v\d+", c))
        if succ:
            plan = plans_by_code[succ[-1]]
    if not plan:
        return None

    prod = products_by_name.get(plan.get("product") or plan_code) or {}
    blobs = (prod.get("blobs") or {}).get("technical") or {}
    srv = blobs.get("server") if isinstance(blobs.get("server"), dict) else {}
    cpu_b = srv.get("cpu") or blobs.get("cpu") or {}
    num_cpu = int(cpu_b.get("number") or 1)
    cpu = {
        "model": " ".join(str(x) for x in (cpu_b.get("brand"), cpu_b.get("model")) if x).strip(),
        "cores": (int(cpu_b["cores"]) * num_cpu) if cpu_b.get("cores") else None,
        "threads": (int(cpu_b["threads"]) * num_cpu) if cpu_b.get("threads") else None,
        "score": float(cpu_b["score"]) if cpu_b.get("score") else None,
    }

    # Resolve memory & storage addon codes
    matched_codes = resolve_addons_for_fqn(plan_summary, memory, storage) if plan_summary else []
    mem_code = next((c for c in matched_codes if "ram-" in c.lower()), "") or (
        (plan_summary or {}).get("memoryCode") or ""
    )
    stor_code = next((c for c in matched_codes if "ram-" not in c.lower()), "") or (
        (plan_summary or {}).get("storageCode") or ""
    )

    ram_gb: Optional[float] = None
    if mem_code and mem_code in addons_by_code:
        mprod = products_by_name.get(addons_by_code[mem_code].get("product") or "") or {}
        msize = (((mprod.get("blobs") or {}).get("technical") or {}).get("memory") or {}).get("size")
        if msize:
            ram_gb = float(msize)
    if ram_gb is None:
        m = re.search(r"ram-(\d+)g", (memory or mem_code).lower())
        if m:
            ram_gb = float(m.group(1))

    disks: List[Dict[str, Any]] = []
    if stor_code and stor_code in addons_by_code:
        sprod = products_by_name.get(addons_by_code[stor_code].get("product") or "") or {}
        st = ((sprod.get("blobs") or {}).get("technical") or {}).get("storage") or {}
        for d in st.get("disks") or []:
            tech = str(d.get("interface") or d.get("technology") or "")
            disks.append(
                {
                    "n": int(d.get("number") or 1),
                    "gb": float(d.get("capacity") or 0),
                    "rank": _disk_rank(tech),
                }
            )
    if not disks and (storage or stor_code):
        for m in re.finditer(r"(\d+)x(\d+)(nvme|ssd|sas|sa|hdd)", (storage or stor_code).lower()):
            disks.append({"n": int(m.group(1)), "gb": float(m.group(2)), "rank": _disk_rank(m.group(3))})

    total_gb = sum(d["n"] * d["gb"] for d in disks)
    return {
        "cpu": cpu,
        "ramGb": ram_gb,
        "disks": disks,
        "diskTotalGb": total_gb,
        "text": _format_spec_summary(cpu, ram_gb, disks),
    }


def parse_actual_hardware(hw: Dict[str, Any], server: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Parse /dedicated/server/{serviceName}/specifications/hardware into normalized spec dict."""
    num_cpu = int(hw.get("numberOfProcessors") or 1)
    cores_per = hw.get("coresPerProcessor")
    threads_per = hw.get("threadsPerProcessor")
    cpu = {
        "model": str(hw.get("processorName") or "").strip(),
        "cores": int(cores_per) * num_cpu if cores_per else None,
        "threads": int(threads_per) * num_cpu if threads_per else None,
    }
    ram_gb = _size_to_gb(hw.get("memorySize"))
    if ram_gb is not None:
        ram_gb = round(ram_gb)

    disks: List[Dict[str, Any]] = []
    for g in hw.get("diskGroups") or []:
        gb = _size_to_gb(g.get("diskSize")) or 0.0
        disks.append(
            {
                "n": int(g.get("numberOfDisks") or 1),
                "gb": round(gb, 1),
                "rank": _disk_rank(str(g.get("diskType") or "")),
                "type": str(g.get("diskType") or ""),
            }
        )
    total_gb = sum(d["n"] * d["gb"] for d in disks)
    return {
        "cpu": cpu,
        "ramGb": ram_gb,
        "disks": disks,
        "diskTotalGb": total_gb,
        "datacenter": str((server or {}).get("datacenter") or ""),
        "os": str((server or {}).get("os") or ""),
        "ip": str((server or {}).get("ip") or ""),
        "state": str((server or {}).get("state") or ""),
        "text": _format_spec_summary(cpu, ram_gb, disks),
    }


def compare_specs(
    official: Optional[Dict[str, Any]],
    actual: Optional[Dict[str, Any]],
    cpu_scores: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """Compare official catalog spec against delivered hardware; flags OVH 'Lottery' upgrades."""
    upgrades: List[str] = []
    downgrades: List[str] = []
    if not official or not actual:
        return {"upgrades": upgrades, "downgrades": downgrades, "lottery": False}

    scores = cpu_scores or {}

    # 1. RAM comparison
    if official.get("ramGb") and actual.get("ramGb"):
        if actual["ramGb"] > official["ramGb"] + 1:
            upgrades.append(f"内存免费升级: {official['ramGb']:g} GB → {actual['ramGb']:g} GB")
        elif actual["ramGb"] < official["ramGb"] - 1:
            downgrades.append(f"内存低于标称: {official['ramGb']:g} GB → {actual['ramGb']:g} GB")

    # 2. Disk capacity & medium comparison (NVMe > SSD > SAS > HDD)
    if official.get("disks") and actual.get("disks"):
        ot, at = float(official["diskTotalGb"]), float(actual["diskTotalGb"])
        orank = max(d["rank"] for d in official["disks"])
        arank = max(d["rank"] for d in actual["disks"])
        if arank > orank and at >= ot * 0.95:
            upgrades.append(f"硬盘介质升级: {_format_disks(official['disks'])} → {_format_disks(actual['disks'])}")
        elif at > ot * 1.05 and arank >= orank:
            upgrades.append(f"硬盘容量升级: {_format_disks(official['disks'])} → {_format_disks(actual['disks'])}")
        elif at < ot * 0.95 or arank < orank:
            downgrades.append(f"硬盘低于标称: {_format_disks(official['disks'])} → {_format_disks(actual['disks'])}")

    # 3. CPU comparison (by catalog benchmark score or core count)
    oc, ac = official.get("cpu") or {}, actual.get("cpu") or {}
    om, am = _norm_cpu(str(oc.get("model") or "")), _norm_cpu(str(ac.get("model") or ""))
    if om and am and om not in am and am not in om:
        os_score = oc.get("score") or scores.get(om)
        as_score = scores.get(am) or next(
            (v for k, v in scores.items() if k and (k in am or am in k)), None
        )
        if os_score and as_score and as_score > os_score * 1.03:
            upgrades.append(f"CPU 免费升级: {oc['model']} → {ac['model']} (跑分 {os_score:g} → {as_score:g})")
        elif (ac.get("cores") or 0) > (oc.get("cores") or 0) > 0:
            upgrades.append(f"CPU 核心升级: {oc['model']} ({oc['cores']}C) → {ac['model']} ({ac['cores']}C)")
        elif os_score and as_score and as_score < os_score * 0.97:
            downgrades.append(f"CPU 低于标称: {oc['model']} → {ac['model']}")
    elif (ac.get("cores") or 0) > (oc.get("cores") or 0) > 0:
        upgrades.append(f"CPU 核心升级: {oc['cores']}C → {ac['cores']}C")

    return {"upgrades": upgrades, "downgrades": downgrades, "lottery": bool(upgrades)}
