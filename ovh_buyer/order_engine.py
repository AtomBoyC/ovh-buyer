"""Fast, safe OVH Cart & Checkout Engine v2.0:
- Parallel configuration + addon attachment on slow path
- Prebuilt Cart Fast Lane (CartSlotPool): 1 single POST /checkout on stock hit
- Single-cart live price-ceiling guard & Dry-Run simulation
- Uncertain checkout protection (pending_confirm on timeout/5xx; never double-orders)
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .catalog import (
    CatalogManager,
    intersect_common_terms,
    is_hardware_option,
    standardize_config,
)
from .config import infer_region_for_datacenter, normalize_datacenter
from .ovh_client import OVHAPIError, OVHClient


PRODUCT_CART_PATH = {
    "eco": "eco",
    "baremetal": "baremetalServers",
    "baremetalservers": "baremetalServers",
    "vps": "vps",
}

SLOT_REVALIDATE_INTERVAL = 1800  # 30 min
SLOT_REBUILD_BEFORE_EXPIRY = 1200  # 20 min
SLOT_MIN_TTL_AT_HIT = 120  # 2 min


def _parse_expire_ts(expire_raw: Any) -> float:
    if not expire_raw:
        return time.time() + 3600
    try:
        dt = datetime.fromisoformat(str(expire_raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return time.time() + 3600


def _extract_prices_from_summary(summary: Any) -> Dict[str, Any]:
    """Safely extract numeric prices and currency from OVH cart summary or checkout preview."""
    out: Dict[str, Any] = {
        "withTax": None,
        "withoutTax": None,
        "tax": None,
        "currencyCode": "EUR",
    }
    if not isinstance(summary, dict):
        return out
    prices = summary.get("prices")
    if not isinstance(prices, dict):
        return out

    def _val(obj: Any) -> Optional[float]:
        if isinstance(obj, dict) and obj.get("value") is not None:
            return float(obj["value"])
        if isinstance(obj, (int, float)):
            return float(obj)
        return None

    out["withTax"] = _val(prices.get("withTax"))
    out["withoutTax"] = _val(prices.get("withoutTax"))
    out["tax"] = _val(prices.get("tax"))

    wt = prices.get("withTax")
    wot = prices.get("withoutTax")
    if isinstance(wt, dict) and wt.get("currencyCode"):
        out["currencyCode"] = str(wt["currencyCode"])
    elif isinstance(wot, dict) and wot.get("currencyCode"):
        out["currencyCode"] = str(wot["currencyCode"])
    elif prices.get("currencyCode"):
        out["currencyCode"] = str(prices["currencyCode"])
    return out


def _pick_cart_plan_pricing(
    plan_offers: Any,
    plan_code: str,
    requested_duration: str = "auto",
    requested_mode: str = "auto",
) -> Tuple[str, str]:
    """Resolve (duration, pricingMode) from /order/cart/{cartId}/{product} offers."""
    dur = requested_duration if (requested_duration and requested_duration != "auto") else "P1M"
    mode = requested_mode if (requested_mode and requested_mode != "auto") else "default"

    if not isinstance(plan_offers, list):
        return dur, mode

    matched = [o for o in plan_offers if isinstance(o, dict) and o.get("planCode") == plan_code]
    if not matched:
        return dur, mode

    prices = matched[0].get("prices") or []
    if not isinstance(prices, list) or not prices:
        return dur, mode

    candidates = []
    for p in prices:
        if not isinstance(p, dict):
            continue
        p_dur = p.get("duration")
        p_mode = p.get("pricingMode")
        if p_dur and p_mode:
            candidates.append((str(p_dur), str(p_mode), (p.get("price") or {}).get("value", 1e9)))

    if not candidates:
        return dur, mode

    if requested_duration not in ("", "auto") and requested_mode not in ("", "auto"):
        return requested_duration, requested_mode

    filtered = candidates
    if requested_duration not in ("", "auto"):
        sub = [c for c in filtered if c[0] == requested_duration]
        if sub:
            filtered = sub
    if requested_mode not in ("", "auto"):
        sub = [c for c in filtered if c[1] == requested_mode]
        if sub:
            filtered = sub

    for c in filtered:
        if c[0] == "P1M" and c[1] == "default":
            return c[0], c[1]
    filtered.sort(key=lambda x: x[2])
    return filtered[0][0], filtered[0][1]


def _pick_option_pricing(avail_opt: Dict[str, Any], preferred_duration: str, preferred_mode: str) -> Tuple[str, str]:
    """Choose compatible (duration, pricingMode) for a cart addon option."""
    if avail_opt.get("duration") and avail_opt.get("pricingMode"):
        return str(avail_opt["duration"]), str(avail_opt["pricingMode"])
    prices = avail_opt.get("prices") or []
    if isinstance(prices, list) and prices:
        for p in prices:
            if p.get("duration") == preferred_duration and p.get("pricingMode") == preferred_mode:
                return preferred_duration, preferred_mode
        for p in prices:
            if p.get("duration") == preferred_duration:
                return preferred_duration, str(p.get("pricingMode") or preferred_mode)
        first = prices[0]
        return str(first.get("duration") or preferred_duration), str(first.get("pricingMode") or preferred_mode)
    return preferred_duration, preferred_mode


def safe_delete_cart(client: OVHClient, cart_id: Optional[str]) -> None:
    if not cart_id:
        return
    try:
        client.delete(f"/order/cart/{cart_id}", need_auth=True)
    except Exception:
        pass


def _is_core_hardware_option(code: str) -> bool:
    """RAM / disk addons: missing them would silently downgrade the server you pay for."""
    c = str(code or "").lower()
    return c.startswith("ram-") or "softraid" in c or "hybridsoftraid" in c or c.startswith("raid") or "-raid-" in c or "nvme" in c


def _price_within_ceiling(prices: Dict[str, Any], max_price: float, price_includes_tax: bool) -> Tuple[bool, Optional[float]]:
    """Return (ok, checked_price). With a ceiling set, an UNKNOWN price is never ok."""
    check = prices.get("withTax") if price_includes_tax else prices.get("withoutTax")
    if check is None:
        check = prices.get("withTax") if prices.get("withTax") is not None else prices.get("withoutTax")
    if max_price and max_price > 0:
        return (check is not None and float(check) <= float(max_price)), check
    return True, check


def task_cart_fingerprint(task: Dict[str, Any], options: List[str]) -> str:
    """Everything that changes WHAT a prebuilt cart would buy; a slot is only reusable on an exact match."""
    parts = [
        str(task.get("plan_code") or ""),
        str(task.get("account_id") or ""),
        str(task.get("product_line") or ""),
        str(task.get("duration") or ""),
        str(task.get("pricing_mode") or ""),
        str(int(task.get("quantity") or 1)),
        str(task.get("os_code") or ""),
        ",".join(sorted(str(c).strip().lower() for c in (task.get("coupons") or []) if c)),
        "|".join(sorted({str(o).strip().lower() for o in options if o})),
    ]
    return "\x1f".join(parts)


class OrderEngine:
    """Executes OVH cart creation, parallel configuration, Fast Lane slots, price guard, and safe checkout."""

    def __init__(self, catalog_manager: CatalogManager, log_fn=None, uncertain_wait_seconds: float = 4.0):
        self.catalog_manager = catalog_manager
        self._log = log_fn or (lambda level, msg, source="order": None)
        self.uncertain_wait_seconds = float(uncertain_wait_seconds)

    def _resolve_term(
        self,
        client: OVHClient,
        cart_id: str,
        subsidiary: str,
        plan_code: str,
        product_line: str,
        cart_product: str,
        options: List[str],
        duration: str,
        pricing_mode: str,
    ) -> Tuple[str, str, Optional[Dict[str, Any]]]:
        """Resolve (duration, pricingMode, plan_summary) preferring common plan∩addons terms."""
        plan_summary = None
        raw_catalog: Dict[str, Any] = {}
        try:
            raw_catalog = self.catalog_manager.get_raw_catalog(
                client, subsidiary=subsidiary, product_line=product_line
            )
            plan_summary = self.catalog_manager.find_plan(
                client, plan_code, subsidiary=subsidiary, product_line=product_line
            )
        except Exception:
            plan_summary = None

        resolved_dur = duration
        resolved_mode = pricing_mode

        if resolved_dur in ("", "auto") or resolved_mode in ("", "auto"):
            # Try exact common term intersection across plan + addons first
            common = intersect_common_terms(raw_catalog, plan_code, options) if raw_catalog else []
            if common:
                pick = common[0]
                if resolved_dur not in ("", "auto"):
                    pick = next((t for t in common if t["duration"] == resolved_dur), pick)
                if resolved_mode not in ("", "auto"):
                    pick = next((t for t in common if t["pricingMode"] == resolved_mode), pick)
                if resolved_dur in ("", "auto"):
                    resolved_dur = pick["duration"]
                if resolved_mode in ("", "auto"):
                    resolved_mode = pick["pricingMode"]
            elif plan_summary:
                if resolved_dur in ("", "auto"):
                    resolved_dur = plan_summary.get("defaultDuration") or "P1M"
                if resolved_mode in ("", "auto"):
                    resolved_mode = plan_summary.get("defaultPricingMode") or "default"
            else:
                try:
                    offers = client.get(
                        f"/order/cart/{cart_id}/{cart_product}",
                        params={"planCode": plan_code},
                        need_auth=True,
                    )
                    resolved_dur, resolved_mode = _pick_cart_plan_pricing(offers, plan_code, resolved_dur, resolved_mode)
                except Exception:
                    resolved_dur = "P1M" if resolved_dur in ("", "auto") else resolved_dur
                    resolved_mode = "default" if resolved_mode in ("", "auto") else resolved_mode

        return resolved_dur, resolved_mode, plan_summary

    def build_cart(
        self,
        client: OVHClient,
        subsidiary: str,
        plan_code: str,
        datacenter: str,
        product_line: str = "eco",
        options: Optional[List[str]] = None,
        duration: str = "auto",
        pricing_mode: str = "auto",
        os_code: str = "none_64.en",
        coupons: Optional[List[str]] = None,
        quantity: int = 1,
        parallel: bool = True,
    ) -> Dict[str, Any]:
        """Build & configure a cart up to live pricing without executing checkout.

        Caller owns the returned `cart_id` and must checkout or delete it.
        """
        cart_product = PRODUCT_CART_PATH.get((product_line or "eco").lower(), "eco")
        api_dc = normalize_datacenter(datacenter)
        wanted_options = [o for o in (options or []) if isinstance(o, str) and o.strip()]
        cart_id: Optional[str] = None

        try:
            cart = client.post("/order/cart", body={"ovhSubsidiary": subsidiary.upper()}, need_auth=True)
            cart_id = str(cart["cartId"])
            expire_ts = _parse_expire_ts(cart.get("expire"))

            client.post(f"/order/cart/{cart_id}/assign", need_auth=True)

            resolved_dur, resolved_mode, plan_summary = self._resolve_term(
                client=client,
                cart_id=cart_id,
                subsidiary=subsidiary,
                plan_code=plan_code,
                product_line=product_line,
                cart_product=cart_product,
                options=wanted_options,
                duration=duration,
                pricing_mode=pricing_mode,
            )

            allowed_regions = (plan_summary or {}).get("allowedRegions") or None
            region = infer_region_for_datacenter(api_dc, allowed=allowed_regions)

            item = client.post(
                f"/order/cart/{cart_id}/{cart_product}",
                body={
                    "planCode": plan_code,
                    "pricingMode": resolved_mode,
                    "duration": resolved_dur,
                    "quantity": max(1, int(quantity or 1)),
                },
                need_auth=True,
            )
            item_id = item["itemId"]

            applied_options: List[str] = []
            if cart_product == "vps":
                self._configure_vps_item(client, cart_id, item_id, api_dc, os_code)
            else:
                applied_options = self._configure_and_attach_parallel(
                    client=client,
                    cart_id=cart_id,
                    item_id=item_id,
                    cart_product=cart_product,
                    plan_code=plan_code,
                    api_dc=api_dc,
                    region=region,
                    os_code=os_code,
                    wanted_options=wanted_options,
                    preferred_duration=resolved_dur,
                    preferred_mode=resolved_mode,
                    quantity=max(1, int(quantity or 1)),
                    parallel=parallel,
                )

            for code in coupons or []:
                clean_code = str(code).strip()
                if not clean_code:
                    continue
                try:
                    client.post(f"/order/cart/{cart_id}/coupon", body={"coupon": clean_code}, need_auth=True)
                    self._log("INFO", f"已应用优惠码: {clean_code}", "order")
                except OVHAPIError as c_err:
                    self._log("WARNING", f"优惠码 {clean_code} 无效或不可用: {c_err}", "order")

            summary = None
            try:
                summary = client.get(f"/order/cart/{cart_id}/summary", need_auth=True)
            except Exception:
                try:
                    summary = client.get(f"/order/cart/{cart_id}/checkout", need_auth=True)
                except Exception:
                    summary = None

            prices_info = _extract_prices_from_summary(summary)
            return {
                "cart_id": cart_id,
                "item_id": item_id,
                "expire_ts": expire_ts,
                "duration": resolved_dur,
                "pricing_mode": resolved_mode,
                "applied_options": applied_options,
                "prices": prices_info,
            }
        except Exception:
            safe_delete_cart(client, cart_id)
            raise

    def checkout_cart(
        self,
        client: OVHClient,
        cart_id: str,
        plan_code: str,
        datacenter: str,
        prices_info: Dict[str, Any],
        applied_options: List[str],
        auto_pay: bool = False,
        waive_retractation: bool = True,
        fast_lane: bool = False,
    ) -> Dict[str, Any]:
        """Fire POST /order/cart/{cartId}/checkout with definite-rejection vs uncertain-timeout safety."""
        api_dc = normalize_datacenter(datacenter)
        since_iso = (datetime.now(timezone.utc) - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        t0 = time.perf_counter()
        try:
            checkout = client.post(
                f"/order/cart/{cart_id}/checkout",
                body={
                    "autoPayWithPreferredPaymentMethod": bool(auto_pay),
                    "waiveRetractationPeriod": bool(waive_retractation),
                },
                need_auth=True,
            )
            checkout_ms = int((time.perf_counter() - t0) * 1000)
            order_id = str((checkout or {}).get("orderId") or "")
            order_url = str((checkout or {}).get("url") or "")
            if prices_info.get("withTax") is None and isinstance(checkout, dict):
                cp = _extract_prices_from_summary(checkout)
                if cp["withTax"] is not None or cp["withoutTax"] is not None:
                    prices_info = cp

            lane_tag = "⚡快速通道" if fast_lane else "直通下单"
            self._log(
                "SUCCESS",
                f"🎉 [{lane_tag} {checkout_ms}ms] 抢购成功! {plan_code}@{api_dc} 订单号={order_id} "
                f"价格={prices_info['withoutTax']}/{prices_info['withTax']} {prices_info['currencyCode']}",
                "order",
            )
            return {
                "success": True,
                "status": "success",
                "order_id": order_id,
                "order_url": order_url,
                "price_without_tax": prices_info["withoutTax"],
                "price_with_tax": prices_info["withTax"],
                "currency": prices_info["currencyCode"],
                "applied_options": applied_options,
                "checkout_ms": checkout_ms,
                "fast_lane": fast_lane,
                "error": "",
            }
        except OVHAPIError as exc:
            if exc.is_definite_rejection:
                safe_delete_cart(client, cart_id)
                self._log("ERROR", f"下单被 OVH 明确拒绝 {plan_code}@{api_dc}: {exc}", "order")
                return {
                    "success": False,
                    "status": "failed",
                    "order_id": "",
                    "order_url": "",
                    "price_without_tax": prices_info["withoutTax"],
                    "price_with_tax": prices_info["withTax"],
                    "currency": prices_info["currencyCode"],
                    "applied_options": applied_options,
                    "error": str(exc),
                }
            # Uncertain outcome (5xx / 429 / network timeout): NEVER retry and NEVER delete cart!
            return self._handle_uncertain_checkout(
                client, cart_id, plan_code, api_dc, prices_info, applied_options, since_iso, str(exc)
            )
        except Exception as exc:
            return self._handle_uncertain_checkout(
                client, cart_id, plan_code, api_dc, prices_info, applied_options, since_iso, str(exc)
            )

    def _handle_uncertain_checkout(
        self,
        client: OVHClient,
        cart_id: str,
        plan_code: str,
        api_dc: str,
        prices_info: Dict[str, Any],
        applied_options: List[str],
        since_iso: str,
        err_msg: str,
    ) -> Dict[str, Any]:
        self._log(
            "WARNING",
            f"⚠️ 结账请求结果不确定 ({plan_code}@{api_dc}: {err_msg})！为防重复扣单，已禁止自动重试并正在核对 /me/order...",
            "order",
        )
        if self.uncertain_wait_seconds > 0:
            time.sleep(self.uncertain_wait_seconds)
        recent = []
        try:
            recent = client.get_recent_orders(limit=5, since_iso=since_iso)
        except Exception:
            pass
        hint = ""
        if recent:
            ids = ", ".join(f"#{r.get('orderId')}({r.get('status')})" for r in recent)
            hint = f" | 近期账号订单（请人工确认是否为本次）: {ids}"
        # Do NOT auto-link recent[0]: it may be another task's or a manual order.
        return {
            "success": False,
            "status": "pending_confirm",
            "order_id": "",
            "order_url": "",
            "price_without_tax": prices_info["withoutTax"],
            "price_with_tax": prices_info["withTax"],
            "currency": prices_info["currencyCode"],
            "applied_options": applied_options,
            "recent_orders": recent,
            "error": f"结账超时/异常状态待人工核对 ({err_msg}){hint}",
        }

    def place_order(
        self,
        client: OVHClient,
        subsidiary: str,
        plan_code: str,
        datacenter: str,
        product_line: str = "eco",
        options: Optional[List[str]] = None,
        duration: str = "auto",
        pricing_mode: str = "auto",
        os_code: str = "none_64.en",
        coupons: Optional[List[str]] = None,
        quantity: int = 1,
        max_price: float = 0.0,
        price_includes_tax: bool = False,
        auto_pay: bool = False,
        waive_retractation: bool = True,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Execute end-to-end OVH cart & checkout flow on a SINGLE cart."""
        api_dc = normalize_datacenter(datacenter)
        cart_id: Optional[str] = None
        prices_info: Dict[str, Any] = {"withTax": None, "withoutTax": None, "tax": None, "currencyCode": "EUR"}
        applied_options: List[str] = []

        try:
            self._log(
                "INFO",
                f"[{'演练' if dry_run else '下单'}] 开始建车: {plan_code}@{api_dc} ({product_line}, {subsidiary})",
                "order",
            )
            built = self.build_cart(
                client=client,
                subsidiary=subsidiary,
                plan_code=plan_code,
                datacenter=api_dc,
                product_line=product_line,
                options=options,
                duration=duration,
                pricing_mode=pricing_mode,
                os_code=os_code,
                coupons=coupons,
                quantity=quantity,
                parallel=True,
            )
            cart_id = built["cart_id"]
            prices_info = built["prices"]
            applied_options = built["applied_options"]

            # Price Ceiling Guard (on the SAME cart!)
            check_price = prices_info["withTax"] if price_includes_tax else prices_info["withoutTax"]
            if check_price is None:
                check_price = prices_info["withTax"] if prices_info["withTax"] is not None else prices_info["withoutTax"]

            if max_price and max_price > 0 and (check_price is None or check_price > max_price):
                safe_delete_cart(client, cart_id)
                tax_label = "含税" if price_includes_tax else "不含税"
                if check_price is None:
                    msg = f"无法获取购物车实时价格，已设置价格上限 {max_price}，为安全起见取消下单并删除购物车"
                else:
                    msg = (
                        f"触发价格封顶保护: 实际{tax_label}价 {check_price} {prices_info['currencyCode']} "
                        f"> 上限 {max_price}，已自动取消购物车"
                    )
                self._log("WARNING", f"{plan_code}@{api_dc} {msg}", "order")
                return {
                    "success": False,
                    "status": "budget_exceeded",
                    "order_id": "",
                    "order_url": "",
                    "price_without_tax": prices_info["withoutTax"],
                    "price_with_tax": prices_info["withTax"],
                    "currency": prices_info["currencyCode"],
                    "applied_options": applied_options,
                    "error": msg,
                }

            if dry_run:
                safe_delete_cart(client, cart_id)
                self._log(
                    "SUCCESS",
                    f"[演练通过] {plan_code}@{api_dc} 可下单! 不含税={prices_info['withoutTax']} / 含税={prices_info['withTax']} {prices_info['currencyCode']}",
                    "order",
                )
                return {
                    "success": True,
                    "status": "dry_run",
                    "order_id": "DRY-RUN",
                    "order_url": "",
                    "price_without_tax": prices_info["withoutTax"],
                    "price_with_tax": prices_info["withTax"],
                    "currency": prices_info["currencyCode"],
                    "applied_options": applied_options,
                    "error": "",
                }

            return self.checkout_cart(
                client=client,
                cart_id=cart_id,
                plan_code=plan_code,
                datacenter=api_dc,
                prices_info=prices_info,
                applied_options=applied_options,
                auto_pay=auto_pay,
                waive_retractation=waive_retractation,
                fast_lane=False,
            )

        except Exception as exc:
            safe_delete_cart(client, cart_id)
            err_msg = str(exc)
            self._log("ERROR", f"下单失败 {plan_code}@{api_dc}: {err_msg}", "order")
            return {
                "success": False,
                "status": "failed",
                "order_id": "",
                "order_url": "",
                "price_without_tax": prices_info["withoutTax"],
                "price_with_tax": prices_info["withTax"],
                "currency": prices_info["currencyCode"],
                "applied_options": applied_options,
                "error": err_msg,
            }

    def _configure_vps_item(
        self,
        client: OVHClient,
        cart_id: str,
        item_id: Any,
        api_dc: str,
        os_code: str,
    ) -> None:
        required = client.get(f"/order/cart/{cart_id}/item/{item_id}/requiredConfiguration", need_auth=True)
        for cfg in required or []:
            label = cfg.get("label")
            allowed = cfg.get("allowedValues") or []
            value = None
            if label == "vps_datacenter":
                for av in allowed:
                    if str(av).lower().startswith(api_dc.lower()):
                        value = str(av)
                        break
                value = value or api_dc.upper()
            elif label == "vps_os":
                if os_code in allowed:
                    value = os_code
                else:
                    ubuntu = next((str(a) for a in allowed if "ubuntu" in str(a).lower()), None)
                    debian = next((str(a) for a in allowed if "debian" in str(a).lower()), None)
                    value = ubuntu or debian or (str(allowed[0]) if allowed else os_code)
            elif allowed:
                value = str(allowed[0])
            if label and value is not None:
                client.post(
                    f"/order/cart/{cart_id}/item/{item_id}/configuration",
                    body={"label": label, "value": value},
                    need_auth=True,
                )

    def _resolve_options_to_post(
        self,
        client: OVHClient,
        cart_id: str,
        cart_product: str,
        plan_code: str,
        wanted_options: List[str],
        preferred_duration: str,
        preferred_mode: str,
    ) -> List[Tuple[str, str, str]]:
        """Match wanted options against cart's available options list -> [(planCode, duration, pricingMode)]."""
        filtered = [opt for opt in wanted_options if is_hardware_option(opt)]
        if not filtered:
            return []
        avail_opts = client.get(
            f"/order/cart/{cart_id}/{cart_product}/options",
            params={"planCode": plan_code},
            need_auth=True,
        )
        if not isinstance(avail_opts, list):
            return []

        to_post: List[Tuple[str, str, str]] = []
        seen = set()
        for wanted in filtered:
            match = next((o for o in avail_opts if o.get("planCode") == wanted), None)
            if match is None:
                wanted_std = standardize_config(wanted)
                if wanted_std:
                    match = next(
                        (o for o in avail_opts if standardize_config(str(o.get("planCode") or "")) == wanted_std),
                        None,
                    )
            if not match:
                if _is_core_hardware_option(wanted):
                    # Never silently downgrade RAM / disk: abort so the cart gets deleted.
                    raise OVHAPIError(f"所需硬件配件 {wanted} 不在 {plan_code} 可选列表中，拒绝降级下单", status_code=409)
                self._log("WARNING", f"硬件选项 {wanted} 未在 {plan_code} 可选列表中匹配到", "order")
                continue
            opt_code = str(match["planCode"])
            if opt_code in seen:
                continue
            seen.add(opt_code)
            opt_dur, opt_mode = _pick_option_pricing(match, preferred_duration, preferred_mode)
            to_post.append((opt_code, opt_dur, opt_mode))
        return to_post

    def _configure_and_attach_parallel(
        self,
        client: OVHClient,
        cart_id: str,
        item_id: Any,
        cart_product: str,
        plan_code: str,
        api_dc: str,
        region: Optional[str],
        os_code: str,
        wanted_options: List[str],
        preferred_duration: str,
        preferred_mode: str,
        quantity: int,
        parallel: bool = True,
    ) -> List[str]:
        """Fire item configurations + hardware options concurrently (with sequential retry & dynamic fallback)."""
        configs: List[Tuple[str, str]] = [("dedicated_datacenter", api_dc)]
        if region:
            configs.append(("region", region))
        configs.append(("dedicated_os", os_code or "none_64.en"))

        options_plan = (
            self._resolve_options_to_post(
                client, cart_id, cart_product, plan_code, wanted_options, preferred_duration, preferred_mode
            )
            if wanted_options
            else []
        )

        calls: List[Tuple[str, Any]] = [("cfg", pair) for pair in configs] + [("opt", triple) for triple in options_plan]

        def _exec(call_item: Tuple[str, Any]) -> Optional[str]:
            kind, data = call_item
            if kind == "cfg":
                label, value = data
                client.post(
                    f"/order/cart/{cart_id}/item/{item_id}/configuration",
                    body={"label": label, "value": value},
                    need_auth=True,
                )
                return None
            opt_code, opt_dur, opt_mode = data
            client.post(
                f"/order/cart/{cart_id}/{cart_product}/options",
                body={
                    "itemId": item_id,
                    "planCode": opt_code,
                    "duration": opt_dur,
                    "pricingMode": opt_mode,
                    "quantity": quantity,
                },
                need_auth=True,
            )
            return opt_code

        failed_calls: List[Tuple[str, Any, Exception]] = []
        added_options: List[str] = []

        if parallel and len(calls) > 1:
            with ThreadPoolExecutor(max_workers=min(4, len(calls))) as pool:
                futs = [(c, pool.submit(_exec, c)) for c in calls]
                for c, f in futs:
                    try:
                        res = f.result()
                        if res:
                            added_options.append(res)
                    except Exception as exc:
                        failed_calls.append((c[0], c[1], exc))
        else:
            for c in calls:
                try:
                    res = _exec(c)
                    if res:
                        added_options.append(res)
                except Exception as exc:
                    failed_calls.append((c[0], c[1], exc))

        # Sequential retry / fallback for failed calls
        need_dynamic_fallback = False
        for kind, data, _orig_exc in failed_calls:
            try:
                res = _exec((kind, data))
                if res:
                    added_options.append(res)
            except OVHAPIError as exc:
                if kind == "cfg":
                    label, _ = data
                    if label in ("dedicated_os", "region"):
                        need_dynamic_fallback = True
                    else:
                        raise exc
                else:
                    if _is_core_hardware_option(str(data[0])):
                        raise OVHAPIError(f"挂载硬件配件 {data[0]} 失败，拒绝降级下单: {exc}", status_code=getattr(exc, "status_code", 0))
                    self._log("WARNING", f"添加选项 {data[0]} 失败: {exc}", "order")

        if need_dynamic_fallback:
            self._dynamic_configuration_fallback(client, cart_id, item_id, region, os_code)

        return added_options

    def _dynamic_configuration_fallback(
        self,
        client: OVHClient,
        cart_id: str,
        item_id: Any,
        region: Optional[str],
        os_code: str,
    ) -> None:
        try:
            required = client.get(f"/order/cart/{cart_id}/item/{item_id}/requiredConfiguration", need_auth=True)
            existing = client.get(f"/order/cart/{cart_id}/item/{item_id}/configuration", need_auth=True) or []
            existing_labels = set()
            if isinstance(existing, list):
                for cid in existing:
                    if isinstance(cid, dict) and cid.get("label"):
                        existing_labels.add(cid["label"])
            for cfg in required or []:
                label = cfg.get("label")
                if not label or label in existing_labels or label == "dedicated_datacenter":
                    continue
                allowed = [str(v) for v in (cfg.get("allowedValues") or [])]
                val: Optional[str] = None
                if label == "dedicated_os":
                    for cand in (os_code, "none_64.en", "none_64", "none_32"):
                        if cand in allowed:
                            val = cand
                            break
                    val = val or (allowed[0] if allowed else "none_64.en")
                elif label == "region":
                    if region and region in allowed:
                        val = region
                    elif allowed:
                        val = allowed[0]
                elif cfg.get("required") and allowed:
                    val = allowed[0]
                if val is not None:
                    try:
                        client.post(
                            f"/order/cart/{cart_id}/item/{item_id}/configuration",
                            body={"label": label, "value": val},
                            need_auth=True,
                        )
                    except OVHAPIError:
                        pass
        except Exception:
            pass


class CartSlotPool:
    """Fast Lane prebuilt cart pool: maintains 1 verified, budget-checked cart per (task_id, datacenter).

    Safety rules:
    - A slot is only reused on an EXACT cart fingerprint match (plan, account, duration, pricing mode,
      quantity, OS, coupons, options). Any task edit evicts stale slots.
    - Prices are re-checked against the task ceiling at build, at revalidation AND at hit time;
      an unknown price with a ceiling set is never accepted.
    - A per-task generation counter prevents carts built concurrently with cleanup_task() from leaking.
    """

    def __init__(self, order_engine: OrderEngine, log_fn=None):
        self.order_engine = order_engine
        self._log = log_fn or (lambda level, msg, source="fastlane": None)
        self._lock = threading.RLock()
        # task_id -> {dc: {cart_id, client, expire_ts, checked_ts, prices, options, cfg_fp, ok, plan_code}}
        self._slots: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._gen: Dict[str, int] = {}

    @staticmethod
    def _options_fp(options: List[str]) -> str:
        return "|".join(sorted({str(o).strip().lower() for o in options if o}))

    def _generation(self, task_id: str) -> int:
        with self._lock:
            return self._gen.get(task_id, 0)

    def maintain_task(
        self,
        task: Dict[str, Any],
        client: OVHClient,
        subsidiary: str,
        target_dcs: List[str],
        resolved_options: List[str],
        stop_event: Optional[threading.Event] = None,
    ) -> int:
        """Build or refresh prebuilt carts for up to 6 target datacenters of a Fast Lane task."""
        task_id = task["id"]
        dcs = [normalize_datacenter(d) for d in target_dcs if d][:6]
        if not dcs:
            return 0
        cfg_fp = task_cart_fingerprint(task, resolved_options)
        max_price = float(task.get("max_price") or 0)
        incl_tax = bool(task.get("price_includes_tax"))
        gen = self._generation(task_id)
        now = time.time()
        built_count = 0

        # Evict slots for removed DCs or any changed cart-affecting setting
        evicted: List[Dict[str, Any]] = []
        with self._lock:
            task_map = self._slots.setdefault(task_id, {})
            for old_dc in list(task_map.keys()):
                sl = task_map[old_dc]
                ok_price, _ = _price_within_ceiling(sl["prices"], max_price, incl_tax)
                if old_dc not in dcs or sl.get("cfg_fp") != cfg_fp or not ok_price:
                    evicted.append(task_map.pop(old_dc))
        for removed in evicted:
            safe_delete_cart(removed["client"], removed["cart_id"])

        for dc in dcs:
            if stop_event and stop_event.is_set():
                break
            with self._lock:
                slot = self._slots.get(task_id, {}).get(dc)
            need_build = slot is None or (slot["expire_ts"] - now < SLOT_REBUILD_BEFORE_EXPIRY)
            need_revalidate = slot is not None and not need_build and (now - slot["checked_ts"] > SLOT_REVALIDATE_INTERVAL)

            if need_build:
                try:
                    built = self.order_engine.build_cart(
                        client=client,
                        subsidiary=subsidiary,
                        plan_code=task["plan_code"],
                        datacenter=dc,
                        product_line=task["product_line"],
                        options=resolved_options,
                        duration=task["duration"],
                        pricing_mode=task["pricing_mode"],
                        os_code=task["os_code"],
                        coupons=task.get("coupons") or [],
                        quantity=int(task.get("quantity") or 1),
                        parallel=True,
                    )
                    prices = built["prices"]
                    ok, check_p = _price_within_ceiling(prices, max_price, incl_tax)
                    if not ok:
                        safe_delete_cart(client, built["cart_id"])
                        reason = "无法获取实时价格" if check_p is None else f"价格 {check_p} 超过上限 {max_price}"
                        self._log("WARNING", f"⚡ [快速通道] {task['plan_code']}@{dc.upper()} 预建车{reason}，跳过预建", "fastlane")
                        continue
                    new_slot = {
                        "cart_id": built["cart_id"],
                        "client": client,
                        "plan_code": task["plan_code"],
                        "datacenter": dc,
                        "expire_ts": built["expire_ts"],
                        "checked_ts": time.time(),
                        "prices": prices,
                        "options": built["applied_options"],
                        "cfg_fp": cfg_fp,
                        "ok": True,
                    }
                    stale_gen = False
                    old = None
                    with self._lock:
                        if self._gen.get(task_id, 0) != gen:
                            stale_gen = True
                        else:
                            old = self._slots.setdefault(task_id, {}).get(dc)
                            self._slots[task_id][dc] = new_slot
                    if stale_gen:
                        # Task was paused/edited/deleted while we were building: never leak the cart
                        safe_delete_cart(client, built["cart_id"])
                        break
                    if old and old["cart_id"] != built["cart_id"]:
                        safe_delete_cart(old["client"], old["cart_id"])
                    built_count += 1
                    self._log(
                        "INFO",
                        f"⚡ [快速通道] 预建购物车就绪: {task['plan_code']}@{dc.upper()} (含税={prices['withTax']} {prices['currencyCode']})",
                        "fastlane",
                    )
                except Exception as exc:
                    self._log("WARNING", f"⚡ [快速通道] 预建 {task['plan_code']}@{dc.upper()} 失败: {exc}", "fastlane")
            elif need_revalidate and slot:
                drop = False
                try:
                    info = client.get(f"/order/cart/{slot['cart_id']}", need_auth=True)
                    summary = client.get(f"/order/cart/{slot['cart_id']}/summary", need_auth=True)
                    prices = _extract_prices_from_summary(summary)
                    exp_ts = _parse_expire_ts((info or {}).get("expire"))
                    ok, _ = _price_within_ceiling(prices, max_price, incl_tax)
                    with self._lock:
                        cur = self._slots.get(task_id, {}).get(dc)
                        if cur and cur["cart_id"] == slot["cart_id"]:
                            if ok:
                                cur["expire_ts"] = exp_ts
                                cur["prices"] = prices
                                cur["checked_ts"] = time.time()
                            else:
                                self._slots[task_id].pop(dc, None)
                                drop = True
                except Exception:
                    with self._lock:
                        cur = self._slots.get(task_id, {}).get(dc)
                        if cur and cur["cart_id"] == slot["cart_id"]:
                            self._slots[task_id].pop(dc, None)
                            drop = True
                # Only delete the cart if THIS thread removed it (the sniper may have popped it for checkout)
                if drop:
                    safe_delete_cart(client, slot["cart_id"])

        return built_count

    def take_ready_slot(
        self,
        task_id: str,
        datacenter: str,
        task: Optional[Dict[str, Any]] = None,
        options: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Atomically pop a ready, non-expired, exactly-matching, within-budget slot for 1-call checkout."""
        dc = normalize_datacenter(datacenter)
        now = time.time()
        if task is None:
            return None
        want_fp = task_cart_fingerprint(task, options or [])
        max_price = float(task.get("max_price") or 0)
        incl_tax = bool(task.get("price_includes_tax"))
        with self._lock:
            task_map = self._slots.get(task_id)
            if not task_map or dc not in task_map:
                return None
            slot = task_map[dc]
            if not slot.get("ok") or (slot["expire_ts"] - now < SLOT_MIN_TTL_AT_HIT):
                return None
            if slot.get("cfg_fp") != want_fp:
                return None
            ok, _ = _price_within_ceiling(slot["prices"], max_price, incl_tax)
            if not ok:
                return None
            return task_map.pop(dc)

    def get_task_slots_summary(self, task_id: str) -> List[Dict[str, Any]]:
        now = time.time()
        with self._lock:
            task_map = self._slots.get(task_id) or {}
            out = []
            for dc, sl in task_map.items():
                ttl = max(0, int(sl["expire_ts"] - now))
                if ttl > 0:
                    out.append(
                        {
                            "dc": dc,
                            "ttl_minutes": ttl // 60,
                            "price_without_tax": sl["prices"].get("withoutTax"),
                            "price_with_tax": sl["prices"].get("withTax"),
                            "currency": sl["prices"].get("currencyCode", "EUR"),
                        }
                    )
            return out

    def cleanup_task(self, task_id: str) -> None:
        with self._lock:
            self._gen[task_id] = self._gen.get(task_id, 0) + 1
            removed = self._slots.pop(task_id, {})
        for sl in removed.values():
            safe_delete_cart(sl["client"], sl["cart_id"])

    def cleanup_all(self) -> None:
        with self._lock:
            all_tasks = list(self._slots.keys())
        for tid in all_tasks:
            self.cleanup_task(tid)
