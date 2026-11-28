"""Zero-dependency Timezone & Two-Tier Scheduling Engine for OVH Sniper v2.2.

Supports:
1. Item-level schedules on Sniper Tasks, Black Friday Radar rules, and VPS Watches:
   - `daily_window`: recurring daily/weekly active windows (supports multiple ranges & cross-midnight, e.g. '22:00-02:00')
   - `datetime_range`: scheduled start/end timestamps (e.g. Black Friday flash-sale countdown & auto-stop)
   - Fast Lane pre-warmup (`warmup_minutes`, default 5m before window/start_at)
2. Global Schedule Rules (`schedule_rules` table):
   - `window`: edge-triggered active window (turns targets ON when entering window, OFF when leaving window)
   - `daily_at`: daily recurring trigger at HH:MM on selected weekdays
   - `once`: one-shot trigger at YYYY-MM-DDTHH:MM
"""

from datetime import date, datetime, time as dtime, timedelta, timezone, tzinfo
import os
import re
from typing import Any, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]


FIXED_OFFSET_FALLBACKS: Dict[str, int] = {
    "UTC": 0,
    "GMT": 0,
    "ETC/UTC": 0,
    "ASIA/SHANGHAI": 8 * 60,
    "ASIA/CHONGQING": 8 * 60,
    "ASIA/HONG_KONG": 8 * 60,
    "ASIA/TAIPEI": 8 * 60,
    "ASIA/SINGAPORE": 8 * 60,
    "ASIA/TOKYO": 9 * 60,
    "ASIA/SEOUL": 9 * 60,
    "EUROPE/LONDON": 0,
    "EUROPE/PARIS": 1 * 60,
    "EUROPE/BERLIN": 1 * 60,
    "EUROPE/MADRID": 1 * 60,
    "EUROPE/WARSAW": 1 * 60,
    "EUROPE/AMSTERDAM": 1 * 60,
    "AMERICA/NEW_YORK": -5 * 60,
    "AMERICA/TORONTO": -5 * 60,
    "AMERICA/MONTREAL": -5 * 60,
    "AMERICA/CHICAGO": -6 * 60,
    "AMERICA/LOS_ANGELES": -8 * 60,
    "AMERICA/VANCOUVER": -8 * 60,
    "AUSTRALIA/SYDNEY": 10 * 60,
}

SUPPORTED_TIMEZONES: List[Dict[str, str]] = [
    {"id": "Asia/Shanghai", "label": "Asia/Shanghai (UTC+8 北京/新加坡)"},
    {"id": "Europe/Paris", "label": "Europe/Paris (OVH 欧洲总部 CET/CEST)"},
    {"id": "Europe/London", "label": "Europe/London (英国 GMT/BST)"},
    {"id": "America/New_York", "label": "America/New_York (美东/加拿大东 EST/EDT)"},
    {"id": "America/Los_Angeles", "label": "America/Los_Angeles (美西 PST/PDT)"},
    {"id": "Asia/Tokyo", "label": "Asia/Tokyo (UTC+9 东京)"},
    {"id": "UTC", "label": "UTC (协调世界时)"},
]

_OFFSET_RE = re.compile(r"^(?:UTC|GMT)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)
_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_WINDOW_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")


def default_timezone_name() -> str:
    env_tz = str(os.environ.get("TZ") or "").strip()
    return env_tz or "Asia/Shanghai"


def resolve_timezone(tz_name: str = "") -> tzinfo:
    """Resolve an IANA timezone name or UTC±HH:MM offset into a stdlib tzinfo (never raises)."""
    raw = str(tz_name or "").strip() or default_timezone_name()
    if raw.upper() in ("UTC", "GMT", "Z"):
        return timezone.utc

    m = _OFFSET_RE.match(raw)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        hours = min(14, int(m.group(2)))
        mins = min(59, int(m.group(3) or 0))
        return timezone(timedelta(minutes=sign * (hours * 60 + mins)), name=raw)

    if ZoneInfo is not None:
        try:
            return ZoneInfo(raw)
        except Exception:
            pass

    fallback_mins = FIXED_OFFSET_FALLBACKS.get(raw.upper(), 8 * 60)
    return timezone(timedelta(minutes=fallback_mins), name=raw)


def now_in_tz(tz_name: str = "", now_ts: Optional[float] = None) -> datetime:
    tz = resolve_timezone(tz_name)
    if now_ts is not None:
        return datetime.fromtimestamp(float(now_ts), tz=tz)
    return datetime.now(tz)


def normalize_weekdays(raw: Any) -> List[int]:
    """Normalize weekday list into sorted unique ISO weekdays [1..7] (1=Mon .. 7=Sun)."""
    if raw is None or raw == "":
        return [1, 2, 3, 4, 5, 6, 7]
    if isinstance(raw, str):
        items = [x.strip() for x in raw.split(",") if x.strip()]
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        return [1, 2, 3, 4, 5, 6, 7]
    out = sorted({int(x) for x in items if str(x).isdigit() and 1 <= int(x) <= 7})
    return out or [1, 2, 3, 4, 5, 6, 7]


def parse_hhmm(raw: str) -> Optional[int]:
    """Parse 'HH:MM' into minutes since 00:00 (0..1440, allowing 24:00 as end-of-day)."""
    m = _HHMM_RE.match(str(raw or "").strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h == 24 and mi == 0:
        return 1440
    if 0 <= h <= 23 and 0 <= mi <= 59:
        return h * 60 + mi
    return None


def parse_time_windows(raw: str) -> List[Tuple[int, int]]:
    """Parse comma/space-separated time ranges like '15:00-18:00, 22:00-02:00' into [(start_min, end_min)]."""
    out: List[Tuple[int, int]] = []
    for part in re.split(r"[,;\n]+", str(raw or "")):
        tok = part.strip()
        if not tok:
            continue
        m = _WINDOW_RE.match(tok)
        if not m:
            continue
        s_min = parse_hhmm(f"{m.group(1)}:{m.group(2)}")
        e_min = parse_hhmm(f"{m.group(3)}:{m.group(4)}")
        if s_min is not None and e_min is not None:
            out.append((s_min, e_min))
    return out


def parse_local_datetime(raw: str, tz: tzinfo) -> Optional[datetime]:
    """Parse 'YYYY-MM-DDTHH:MM(:SS)' or 'YYYY-MM-DD HH:MM(:SS)' in the target timezone."""
    s = str(raw or "").strip()
    if not s:
        return None
    norm = s.replace(" ", "T")
    if norm.endswith("Z"):
        norm = norm[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(norm)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=tz)
        return dt.astimezone(tz)
    except ValueError:
        return None


def _fmt_hhmm(mins: int) -> str:
    mins = max(0, min(1440, int(mins)))
    return f"{mins // 60:02d}:{mins % 60:02d}"


def _fmt_weekdays_cn(days: List[int]) -> str:
    if set(days) == {1, 2, 3, 4, 5, 6, 7}:
        return "每天"
    if days == [1, 2, 3, 4, 5]:
        return "工作日"
    if days == [6, 7]:
        return "周末"
    names = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "日"}
    return "周" + "/".join(names.get(d, str(d)) for d in days)


def normalize_item_schedule(raw: Any) -> Dict[str, Any]:
    """Normalize an item-level `schedule` dict for tasks, radar rules, and VPS watches."""
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            raw = {}
        else:
            import json

            try:
                raw = json.loads(raw)
            except Exception:
                raw = {}
    if not isinstance(raw, dict):
        raw = {}

    enabled = bool(raw.get("enabled", False))
    mode = str(raw.get("mode") or "daily_window").strip().lower()
    if mode not in ("daily_window", "datetime_range"):
        mode = "daily_window"
    windows = str(raw.get("windows") or "").strip()
    weekdays = normalize_weekdays(raw.get("weekdays"))
    start_at = str(raw.get("start_at") or "").strip()
    end_at = str(raw.get("end_at") or "").strip()
    try:
        warmup = max(0, min(60, int(raw.get("warmup_minutes", 5))))
    except (TypeError, ValueError):
        warmup = 5
    auto_pause = bool(raw.get("auto_pause_on_end", True))

    return {
        "enabled": enabled,
        "mode": mode,
        "windows": windows,
        "weekdays": weekdays,
        "start_at": start_at,
        "end_at": end_at,
        "warmup_minutes": warmup,
        "auto_pause_on_end": auto_pause,
    }


def is_in_daily_windows(now_dt: datetime, windows: List[Tuple[int, int]], weekdays: List[int]) -> bool:
    """Check whether `now_dt` falls inside any `(start_min, end_min)` window (including overnight spans)."""
    if not windows:
        return True
    day_set = set(normalize_weekdays(weekdays))
    cur_min = now_dt.hour * 60 + now_dt.minute + (now_dt.second / 60.0)
    today_iso = now_dt.isoweekday()
    yesterday_iso = (now_dt - timedelta(days=1)).isoweekday()

    for s_min, e_min in windows:
        if s_min == e_min:
            # 24h full day on matching weekdays
            if today_iso in day_set:
                return True
        elif s_min < e_min:
            if today_iso in day_set and s_min <= cur_min < e_min:
                return True
        else:
            # Cross-midnight window, e.g. 22:00-02:00
            if today_iso in day_set and cur_min >= s_min:
                return True
            if yesterday_iso in day_set and cur_min < e_min:
                return True
    return False


def next_daily_window_start(now_dt: datetime, windows: List[Tuple[int, int]], weekdays: List[int]) -> Optional[datetime]:
    """Return the earliest upcoming window start strictly after `now_dt`."""
    if not windows:
        return None
    day_set = set(normalize_weekdays(weekdays))
    tz = now_dt.tzinfo or timezone.utc
    candidates: List[datetime] = []
    base_date: date = now_dt.date()
    for day_offset in range(0, 8):
        cand_date = base_date + timedelta(days=day_offset)
        if cand_date.isoweekday() not in day_set:
            continue
        for s_min, _ in windows:
            h, m = divmod(min(1439, s_min), 60)
            cand_dt = datetime.combine(cand_date, dtime(hour=h, minute=m, second=0), tzinfo=tz)
            if cand_dt > now_dt:
                candidates.append(cand_dt)
    return min(candidates) if candidates else None


def evaluate_item_schedule(
    raw_schedule: Any,
    tz_name: str = "",
    now_ts: Optional[float] = None,
) -> Dict[str, Any]:
    """Evaluate an item's schedule at `now_ts` in `tz_name`."""
    sched = normalize_item_schedule(raw_schedule)
    if not sched["enabled"]:
        return {
            "scheduled": False,
            "active": True,
            "in_warmup": False,
            "expired": False,
            "state": "always",
            "summary": "全天候",
            "status_hint": "",
            "next_start": "",
        }

    now_dt = now_in_tz(tz_name, now_ts)
    tz = now_dt.tzinfo or timezone.utc
    warmup_sec = int(sched["warmup_minutes"]) * 60

    if sched["mode"] == "datetime_range":
        start_dt = parse_local_datetime(sched["start_at"], tz)
        end_dt = parse_local_datetime(sched["end_at"], tz)
        s_label = start_dt.strftime("%m-%d %H:%M") if start_dt else "立即"
        e_label = end_dt.strftime("%m-%d %H:%M") if end_dt else "不限"
        summary = f"{s_label} ~ {e_label}"

        if end_dt is not None and now_dt >= end_dt:
            return {
                "scheduled": True,
                "active": False,
                "in_warmup": False,
                "expired": True,
                "state": "expired",
                "summary": summary,
                "status_hint": f"⏹ 定时已结束 ({e_label})",
                "next_start": "",
            }
        if start_dt is not None and now_dt < start_dt:
            rem_sec = (start_dt - now_dt).total_seconds()
            in_warm = warmup_sec > 0 and rem_sec <= warmup_sec
            start_disp = start_dt.strftime("%m-%d %H:%M") if start_dt.date() != now_dt.date() else start_dt.strftime("%H:%M")
            return {
                "scheduled": True,
                "active": False,
                "in_warmup": in_warm,
                "expired": False,
                "state": "warmup" if in_warm else "waiting",
                "summary": summary,
                "status_hint": f"🔥 预热中 ({start_disp} 开抢)" if in_warm else f"⏰ 待命 ({start_disp} 启动)",
                "next_start": start_disp,
            }
        return {
            "scheduled": True,
            "active": True,
            "in_warmup": False,
            "expired": False,
            "state": "active",
            "summary": summary,
            "status_hint": f"⏰ 定时运行中 (至 {e_label})" if end_dt else "⏰ 定时运行中",
            "next_start": "",
        }

    # Mode: daily_window
    windows = parse_time_windows(sched["windows"])
    weekdays = sched["weekdays"]
    day_txt = _fmt_weekdays_cn(weekdays)
    win_txt = ", ".join(f"{_fmt_hhmm(s)}-{_fmt_hhmm(e)}" for s, e in windows) if windows else "00:00-24:00"
    summary = f"{day_txt} {win_txt}"

    if is_in_daily_windows(now_dt, windows, weekdays):
        return {
            "scheduled": True,
            "active": True,
            "in_warmup": False,
            "expired": False,
            "state": "active",
            "summary": summary,
            "status_hint": f"⏰ 时段运行中 ({win_txt})",
            "next_start": "",
        }

    nxt = next_daily_window_start(now_dt, windows, weekdays)
    in_warm = False
    nxt_disp = ""
    if nxt is not None:
        rem_sec = (nxt - now_dt).total_seconds()
        in_warm = warmup_sec > 0 and 0 < rem_sec <= warmup_sec
        nxt_disp = nxt.strftime("%H:%M") if nxt.date() == now_dt.date() else nxt.strftime("%m-%d %H:%M")

    return {
        "scheduled": True,
        "active": False,
        "in_warmup": in_warm,
        "expired": False,
        "state": "warmup" if in_warm else "waiting",
        "summary": summary,
        "status_hint": (
            f"🔥 预热中 ({nxt_disp} 开抢)"
            if in_warm
            else (f"⏰ 待命 (下次 {nxt_disp})" if nxt_disp else f"⏰ 非活跃时段 ({summary})")
        ),
        "next_start": nxt_disp,
    }


def describe_global_rule(rule: Dict[str, Any], tz_name: str = "", now_ts: Optional[float] = None) -> Dict[str, Any]:
    """Return live display metadata (`active_now`, `schedule_label`, `next_hint`) for a global `schedule_rule`."""
    rtype = str(rule.get("rule_type") or "window").lower()
    now_dt = now_in_tz(tz_name, now_ts)
    tz = now_dt.tzinfo or timezone.utc
    weekdays = normalize_weekdays(rule.get("weekdays"))
    day_txt = _fmt_weekdays_cn(weekdays)

    if rtype == "window":
        wins = parse_time_windows(str(rule.get("windows") or ""))
        win_txt = ", ".join(f"{_fmt_hhmm(s)}-{_fmt_hhmm(e)}" for s, e in wins) if wins else "未设置时段"
        in_win = bool(rule.get("enabled")) and bool(wins) and is_in_daily_windows(now_dt, wins, weekdays)
        nxt = next_daily_window_start(now_dt, wins, weekdays) if wins and not in_win else None
        nxt_str = (nxt.strftime("%H:%M") if nxt.date() == now_dt.date() else nxt.strftime("%m-%d %H:%M")) if nxt else ""
        return {
            "active_now": in_win,
            "schedule_label": f"时段守护 · {day_txt} {win_txt}",
            "next_hint": "🟢 当前处于开启时段内" if in_win else (f"下次开启: {nxt_str}" if nxt_str else ""),
        }
    if rtype == "daily_at":
        at_str = str(rule.get("at_time") or "22:00").strip()
        act_cn = "开启" if rule.get("action") == "enable" else "暂停"
        return {
            "active_now": False,
            "schedule_label": f"每日定点 · {day_txt} {at_str} {act_cn}",
            "next_hint": f"最近触发: {rule.get('last_triggered_at') or '尚未触发'}",
        }
    # once
    run_dt = parse_local_datetime(str(rule.get("run_at") or ""), tz)
    run_txt = run_dt.strftime("%Y-%m-%d %H:%M") if run_dt else str(rule.get("run_at") or "-")
    act_cn = "开启" if rule.get("action") == "enable" else "暂停"
    return {
        "active_now": False,
        "schedule_label": f"单次定时 · {run_txt} {act_cn}",
        "next_hint": f"最近触发: {rule.get('last_triggered_at') or '等待到点触发'}",
    }
