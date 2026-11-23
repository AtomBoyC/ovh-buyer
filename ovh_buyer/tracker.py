"""Order Delivery Tracker & OVH Hardware 'Lottery' (中奖) Detector."""

from datetime import datetime, timedelta, timezone
import time
from typing import Any, Callable, Dict, Optional, Tuple

from .catalog import CatalogManager
from .config import (
    OVH_CANCELLED_STATUSES,
    OVH_ORDER_STATUS_LABELS,
    OVH_PAID_STATUSES,
    format_datacenter_label,
)
from .database import Database
from .notifier import Notifier
from .ovh_client import OVHClient
from .specs import (
    GLOBAL_FX,
    compare_specs,
    extract_cpu_scores_from_catalog,
    parse_actual_hardware,
    parse_official_spec,
)


TRACK_FAST_WINDOW_SEC = 2 * 3600
TRACK_FAST_INTERVAL_SEC = 120
TRACK_SLOW_INTERVAL_SEC = 600
TRACK_TIMEOUT_SEC = 14 * 86400
DELIVERED_LINK_GRACE_SEC = 2 * 3600


def _parse_iso_ts(val: str) -> float:
    if not val:
        return time.time()
    try:
        dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
        return dt.timestamp()
    except Exception:
        return time.time()


def _parse_date_utc(val: Any) -> Optional[datetime]:
    if not val:
        return None
    s = str(val)
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d.astimezone(timezone.utc) if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        try:
            return datetime.fromisoformat(s[:10]).replace(tzinfo=timezone.utc)
        except Exception:
            return None


def _has_event(order: Dict[str, Any], event: str) -> bool:
    return event in [x for x in str(order.get("notified_events") or "").split(",") if x]


def _add_event(order: Dict[str, Any], event: str) -> str:
    ev = {x for x in str(order.get("notified_events") or "").split(",") if x}
    ev.add(event)
    return ",".join(sorted(ev))


class OrderTracker:
    """Read-only order lifecycle tracker: notPaid -> paid -> delivered + hardware lottery comparison."""

    def __init__(
        self,
        db: Database,
        catalog_manager: CatalogManager,
        notifier: Notifier,
        client_builder: Callable[..., OVHClient],
    ):
        self.db = db
        self.catalog_manager = catalog_manager
        self.notifier = notifier
        self._build_client = client_builder

    def tick(self) -> int:
        due = self.db.list_due_tracking_orders(limit=6)
        count = 0
        for order in due:
            try:
                self.check_order(order["id"])
                count += 1
            except Exception as exc:
                self.db.add_log("WARNING", f"订单 #{order.get('order_id')} 跟踪异常: {exc}", "tracker")
        return count

    def _find_linked_server(
        self,
        client: OVHClient,
        order: Dict[str, Any],
        taken_names: set,
    ) -> Tuple[Optional[str], str]:
        """Link an OVH order to its provisioned dedicated server serviceName."""
        all_names = client.list_dedicated_servers()
        free = [n for n in all_names if n not in taken_names]
        if not free:
            return None, ""

        # 1. Preferred: order detail rows include `domain` once provisioned
        for detail in client.get_order_details(order["order_id"]):
            dom = str(detail.get("domain") or "").strip()
            if dom and dom != "*" and dom in free:
                return dom, "订单明细关联"

        # 2. Fallback: server created around order timestamp in matching datacenter
        order_dt = datetime.fromtimestamp(_parse_iso_ts(order.get("created_at") or ""), tz=timezone.utc)
        since_day = (order_dt - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        dc_prefix = (order.get("datacenter") or "").lower()[:3]
        best = None
        for srv_name in free[:40]:
            try:
                si = client.get_dedicated_service_infos(srv_name)
                created = _parse_date_utc(si.get("creation"))
                if not created or created < since_day:
                    continue
                srv = client.get_dedicated_server(srv_name)
                dc_ok = bool(dc_prefix) and str(srv.get("datacenter") or "").lower().startswith(dc_prefix)
                rank = (not dc_ok, abs((created - order_dt).total_seconds()))
                if best is None or rank < best[0]:
                    best = (rank, srv_name)
            except Exception:
                continue
        return (best[1], "开通时间匹配") if best else (None, "")

    def check_order(self, local_order_id: str) -> Dict[str, Any]:
        """Execute one tracking check for a single local order record."""
        order = self.db.get_order(local_order_id)
        if not order or not order.get("order_id") or order.get("dry_run"):
            return {"skipped": True}

        acc = self.db.get_account(order.get("account_id") or "", mask=False) or self.db.get_default_account(mask=False)
        if not acc or not acc.get("app_key"):
            self.db.update_order(local_order_id, {"next_check_at": time.time() + 1800})
            return {"error": "未找到关联 OVH 账号"}

        now_ts = time.time()
        age_sec = max(0.0, now_ts - _parse_iso_ts(order.get("created_at") or ""))
        client = self._build_client(account=acc)
        settings = self.db.get_settings()

        try:
            ovh_status = client.get_order_status(order["order_id"])
        except Exception as exc:
            self.db.update_order(local_order_id, {"next_check_at": now_ts + TRACK_SLOW_INTERVAL_SEC})
            return {"error": str(exc)}

        status_cn = OVH_ORDER_STATUS_LABELS.get(ovh_status, ovh_status)
        notified_events = str(order.get("notified_events") or "")
        patch: Dict[str, Any] = {"ovh_status": ovh_status}

        price_line = ""
        if order.get("price_with_tax") is not None:
            price_line = GLOBAL_FX.format_price_with_cny(order["price_with_tax"], order.get("currency") or "EUR")

        # 1. Cancelled / Expired
        if ovh_status in OVH_CANCELLED_STATUSES:
            patch["track_state"] = "cancelled"
            self.db.update_order(local_order_id, patch)
            self.db.add_log("INFO", f"订单 #{order['order_id']} 状态变更为 {status_cn}，停止跟踪", "tracker")
            return {"status": ovh_status, "track_state": "cancelled"}

        # 2. Paid notification
        if ovh_status in OVH_PAID_STATUSES and not _has_event(order, "paid"):
            notified_events = _add_event({"notified_events": notified_events}, "paid")
            patch["notified_events"] = notified_events
            if settings.get("notify_on_paid", True):
                body_lines = [
                    f"订单号: #{order['order_id']}",
                    f"型号: {order['plan_code']} @ {format_datacenter_label(order['datacenter'])}",
                    f"当前状态: {status_cn}",
                ]
                if price_line:
                    body_lines.append(f"含税金额: {price_line}")
                self.notifier.notify(
                    title=f"💳 OVH 订单 #{order['order_id']} 已付款 ({status_cn})",
                    body="\n".join(body_lines),
                    event="order_paid",
                    url_link=order.get("order_url") or "",
                )

        # 3. Link dedicated server & inspect hardware lottery when paid / delivering / delivered
        spec_payload: Optional[Dict[str, Any]] = None
        server_name = str(order.get("server_name") or "")
        server_ip = str(order.get("server_ip") or "")

        if order.get("product_line") != "vps" and ovh_status in OVH_PAID_STATUSES:
            try:
                taken = self.db.list_linked_server_names(acc["id"], exclude_order_id=local_order_id)
                found_name, linked_by = (server_name, "已关联") if server_name else self._find_linked_server(client, order, taken)
                if found_name:
                    server_name = found_name
                    srv = client.get_dedicated_server(server_name)
                    hw = client.get_dedicated_hardware(server_name)
                    server_ip = str(srv.get("ip") or "")
                    actual_spec = parse_actual_hardware(hw, srv)

                    raw_cat: Dict[str, Any] = {}
                    plan_sum = None
                    try:
                        raw_cat = self.catalog_manager.get_raw_catalog(
                            client, subsidiary=acc["subsidiary"], product_line=order.get("product_line") or "eco"
                        )
                        plan_sum = self.catalog_manager.find_plan(
                            client,
                            order["plan_code"],
                            subsidiary=acc["subsidiary"],
                            product_line=order.get("product_line") or "eco",
                        )
                    except Exception:
                        pass

                    official_spec = (
                        parse_official_spec(
                            raw_cat,
                            plan_sum,
                            order["plan_code"],
                            memory=order.get("memory") or "",
                            storage=order.get("storage") or "",
                        )
                        if raw_cat
                        else None
                    )
                    cpu_scores = extract_cpu_scores_from_catalog(raw_cat) if raw_cat else {}
                    cmp_result = compare_specs(official_spec, actual_spec, cpu_scores=cpu_scores)

                    spec_payload = {
                        "server": server_name,
                        "ip": server_ip,
                        "serverState": srv.get("state", ""),
                        "linkedBy": linked_by,
                        "official": official_spec,
                        "actual": actual_spec,
                        **cmp_result,
                    }
                    patch["server_name"] = server_name
                    patch["server_ip"] = server_ip
                    patch["spec"] = spec_payload
            except Exception as exc:
                self.db.add_log("INFO", f"订单 #{order['order_id']} 硬件读取待重试: {exc}", "tracker")

        is_delivered = (
            (spec_payload is not None and (ovh_status == "delivered" or spec_payload.get("serverState") == "ok"))
            or (ovh_status == "delivered" and order.get("product_line") == "vps")
            or (ovh_status == "delivered" and age_sec > DELIVERED_LINK_GRACE_SEC)
        )

        if is_delivered:
            patch["track_state"] = "delivered"
            if not _has_event({"notified_events": notified_events}, "delivered"):
                notified_events = _add_event({"notified_events": notified_events}, "delivered")
                patch["notified_events"] = notified_events
                if settings.get("notify_on_delivered", True):
                    is_lottery = bool(spec_payload and spec_payload.get("lottery"))
                    title = (
                        f"🎰🎉 恭喜中奖！服务器已交付 ({server_name})"
                        if is_lottery
                        else f"📦 服务器已交付: {server_name or order['plan_code']}"
                    )
                    lines = [
                        f"订单号: #{order['order_id']}",
                        f"型号: {order['plan_code']} @ {format_datacenter_label(order['datacenter'])}",
                    ]
                    if server_name:
                        lines.append(f"主机名: {server_name}")
                    if server_ip:
                        lines.append(f"IP: {server_ip}")
                    if price_line:
                        lines.append(f"含税金额: {price_line}")
                    if spec_payload:
                        lines.append(f"实际配置: {spec_payload['actual']['text']}")
                        if spec_payload.get("official"):
                            lines.append(f"官方标称: {spec_payload['official']['text']}")
                        if is_lottery:
                            lines.append("\n🎁 免费硬件升级（中奖明细）:")
                            for up in spec_payload.get("upgrades") or []:
                                lines.append(f"  • {up}")
                    self.notifier.notify(
                        title=title,
                        body="\n".join(lines),
                        event="order_delivered",
                        urgent=is_lottery,
                        extra=spec_payload or {},
                    )
            self.db.add_log(
                "SUCCESS",
                f"📦 订单 #{order['order_id']} 已交付 ({server_name or order['plan_code']})"
                + (" 🎰 中奖升级!" if (spec_payload and spec_payload.get("lottery")) else ""),
                "tracker",
            )
        elif age_sec > TRACK_TIMEOUT_SEC:
            patch["track_state"] = "timeout"
        else:
            step = TRACK_FAST_INTERVAL_SEC if age_sec < TRACK_FAST_WINDOW_SEC else TRACK_SLOW_INTERVAL_SEC
            patch["next_check_at"] = now_ts + step

        self.db.update_order(local_order_id, patch)
        return {
            "status": ovh_status,
            "status_cn": status_cn,
            "track_state": patch.get("track_state", order.get("track_state")),
            "server_name": server_name,
            "lottery": bool(spec_payload and spec_payload.get("lottery")),
            "spec": spec_payload,
        }
