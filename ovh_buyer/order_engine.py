"""Fast, safe OVH Cart & Checkout Engine with Price-Ceiling Guard & Dry-Run support."""

from typing import Any, Dict, List, Optional, Tuple

from .catalog import CatalogManager, is_hardware_option, standardize_config
from .config import infer_region_for_datacenter, normalize_datacenter
from .ovh_client import OVHAPIError, OVHClient


PRODUCT_CART_PATH = {
    "eco": "eco",
    "baremetal": "baremetalServers",
    "baremetalservers": "baremetalServers",
    "vps": "vps",
}


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

    # Filter entries that allow ordering/renewing
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

    # Exact match if user explicitly requested both
    if requested_duration not in ("", "auto") and requested_mode not in ("", "auto"):
        return requested_duration, requested_mode

    # Prefer matching explicit constraints, otherwise prefer ('P1M', 'default'), else cheapest
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


class OrderEngine:
    """Executes OVH cart creation, configuration, options, price guard, and checkout."""

    def __init__(self, catalog_manager: CatalogManager, log_fn=None):
        self.catalog_manager = catalog_manager
        self._log = log_fn or (lambda level, msg, source="order": None)

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
        """Execute end-to-end OVH cart & checkout flow on a SINGLE cart.

        Returns a structured result dict:
            {
                "success": bool,
                "status": "success" | "dry_run" | "budget_exceeded" | "failed",
                "order_id": str,
                "order_url": str,
                "price_without_tax": float | None,
                "price_with_tax": float | None,
                "currency": str,
                "applied_options": list[str],
                "error": str,
            }
        """
        cart_product = PRODUCT_CART_PATH.get((product_line or "eco").lower(), "eco")
        api_dc = normalize_datacenter(datacenter)
        region = infer_region_for_datacenter(api_dc)
        wanted_options = [o for o in (options or []) if isinstance(o, str) and o.strip()]
        cart_id: Optional[str] = None
        applied_options: List[str] = []
        prices_info: Dict[str, Any] = {"withTax": None, "withoutTax": None, "tax": None, "currencyCode": "EUR"}

        def _cleanup_cart() -> None:
            if cart_id:
                try:
                    client.delete(f"/order/cart/{cart_id}", need_auth=True)
                except Exception:
                    pass

        try:
            self._log(
                "INFO",
                f"[{'演练' if dry_run else '下单'}] 开始创建购物车: {plan_code}@{api_dc} ({cart_product}, {subsidiary})",
                "order",
            )
            # 1. Create cart
            cart = client.post("/order/cart", body={"ovhSubsidiary": subsidiary.upper()}, need_auth=True)
            cart_id = str(cart["cartId"])

            # 2. Assign cart to authenticated account
            client.post(f"/order/cart/{cart_id}/assign", need_auth=True)

            # 3. Resolve duration & pricingMode (from catalog cache first for speed, else cart offers)
            resolved_duration, resolved_mode = duration, pricing_mode
            if resolved_duration in ("", "auto") or resolved_mode in ("", "auto"):
                plan_summary = None
                try:
                    plan_summary = self.catalog_manager.find_plan(
                        client, plan_code, subsidiary=subsidiary, product_line=product_line
                    )
                except Exception:
                    plan_summary = None
                if plan_summary:
                    if resolved_duration in ("", "auto"):
                        resolved_duration = plan_summary.get("defaultDuration") or "P1M"
                    if resolved_mode in ("", "auto"):
                        resolved_mode = plan_summary.get("defaultPricingMode") or "default"
                else:
                    try:
                        offers = client.get(
                            f"/order/cart/{cart_id}/{cart_product}",
                            params={"planCode": plan_code},
                            need_auth=True,
                        )
                        resolved_duration, resolved_mode = _pick_cart_plan_pricing(
                            offers, plan_code, resolved_duration, resolved_mode
                        )
                    except Exception:
                        resolved_duration = "P1M" if resolved_duration in ("", "auto") else resolved_duration
                        resolved_mode = "default" if resolved_mode in ("", "auto") else resolved_mode

            # 4. Add base item to cart
            item_payload = {
                "planCode": plan_code,
                "pricingMode": resolved_mode,
                "duration": resolved_duration,
                "quantity": max(1, int(quantity or 1)),
            }
            item = client.post(f"/order/cart/{cart_id}/{cart_product}", body=item_payload, need_auth=True)
            item_id = item["itemId"]

            # 5. Apply item configurations (datacenter, region, OS)
            self._configure_item(
                client=client,
                cart_id=cart_id,
                item_id=item_id,
                cart_product=cart_product,
                api_dc=api_dc,
                region=region,
                os_code=os_code,
            )

            # 6. Attach hardware options / addons (Eco & Baremetal)
            if cart_product in ("eco", "baremetalServers") and wanted_options:
                applied_options = self._attach_options(
                    client=client,
                    cart_id=cart_id,
                    item_id=item_id,
                    cart_product=cart_product,
                    plan_code=plan_code,
                    wanted_options=wanted_options,
                    preferred_duration=resolved_duration,
                    preferred_mode=resolved_mode,
                )

            # 7. Apply coupons / vouchers if configured
            for code in coupons or []:
                clean_code = str(code).strip()
                if not clean_code:
                    continue
                try:
                    client.post(f"/order/cart/{cart_id}/coupon", body={"coupon": clean_code}, need_auth=True)
                    self._log("INFO", f"已应用优惠码: {clean_code}", "order")
                except OVHAPIError as c_err:
                    self._log("WARNING", f"优惠码 {clean_code} 无效或不可用: {c_err}", "order")

            # 8. Live price inspection & Price Ceiling Guard (on the SAME cart!)
            summary = None
            try:
                summary = client.get(f"/order/cart/{cart_id}/summary", need_auth=True)
            except Exception:
                try:
                    summary = client.get(f"/order/cart/{cart_id}/checkout", need_auth=True)
                except Exception:
                    summary = None

            prices_info = _extract_prices_from_summary(summary)
            check_price = prices_info["withTax"] if price_includes_tax else prices_info["withoutTax"]
            if check_price is None:
                check_price = prices_info["withTax"] if prices_info["withTax"] is not None else prices_info["withoutTax"]

            if max_price and max_price > 0 and check_price is not None and check_price > max_price:
                _cleanup_cart()
                tax_label = "含税" if price_includes_tax else "不含税"
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

            # 9. Dry-run exits cleanly after verifying everything up to live checkout preview
            if dry_run:
                _cleanup_cart()
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

            # 10. Real Checkout!
            checkout_payload = {
                "autoPayWithPreferredPaymentMethod": bool(auto_pay),
                "waiveRetractationPeriod": bool(waive_retractation),
            }
            checkout = client.post(f"/order/cart/{cart_id}/checkout", body=checkout_payload, need_auth=True)
            order_id = str(checkout.get("orderId") or "")
            order_url = str(checkout.get("url") or "")
            if not prices_info["withTax"] and isinstance(checkout, dict):
                checkout_prices = _extract_prices_from_summary(checkout)
                if checkout_prices["withTax"] is not None or checkout_prices["withoutTax"] is not None:
                    prices_info = checkout_prices

            self._log(
                "SUCCESS",
                f"🎉 抢购成功! {plan_code}@{api_dc} 订单号={order_id} 价格={prices_info['withoutTax']}/{prices_info['withTax']} {prices_info['currencyCode']}",
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
                "error": "",
            }

        except Exception as exc:
            _cleanup_cart()
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

    def _configure_item(
        self,
        client: OVHClient,
        cart_id: str,
        item_id: Any,
        cart_product: str,
        api_dc: str,
        region: Optional[str],
        os_code: str,
    ) -> None:
        """Apply required item configurations with dynamic fallback via /requiredConfiguration."""
        if cart_product == "vps":
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
            return

        # Fast path for Eco / Baremetal
        configs: List[Tuple[str, str]] = [("dedicated_datacenter", api_dc)]
        if region:
            configs.append(("region", region))
        configs.append(("dedicated_os", os_code or "none_64.en"))

        need_dynamic_fallback = False
        for label, value in configs:
            try:
                client.post(
                    f"/order/cart/{cart_id}/item/{item_id}/configuration",
                    body={"label": label, "value": value},
                    need_auth=True,
                )
            except OVHAPIError as exc:
                if label == "dedicated_os":
                    need_dynamic_fallback = True
                elif label == "region":
                    # Some plans don't accept 'region' or expect a different string
                    need_dynamic_fallback = True
                else:
                    raise exc

        if need_dynamic_fallback:
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

    def _attach_options(
        self,
        client: OVHClient,
        cart_id: str,
        item_id: Any,
        cart_product: str,
        plan_code: str,
        wanted_options: List[str],
        preferred_duration: str,
        preferred_mode: str,
    ) -> List[str]:
        """Resolve and add hardware options/addons to the cart item."""
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

        added: List[str] = []
        for wanted in filtered:
            match = None
            for opt in avail_opts:
                if opt.get("planCode") == wanted:
                    match = opt
                    break
            if match is None:
                wanted_std = standardize_config(wanted)
                if wanted_std:
                    for opt in avail_opts:
                        if standardize_config(str(opt.get("planCode") or "")) == wanted_std:
                            match = opt
                            break
            if not match:
                self._log("WARNING", f"硬件选项 {wanted} 未在 {plan_code} 可选列表中匹配到", "order")
                continue
            opt_code = str(match["planCode"])
            opt_dur, opt_mode = _pick_option_pricing(match, preferred_duration, preferred_mode)
            try:
                client.post(
                    f"/order/cart/{cart_id}/{cart_product}/options",
                    body={
                        "itemId": item_id,
                        "planCode": opt_code,
                        "duration": opt_dur,
                        "pricingMode": opt_mode,
                        "quantity": 1,
                    },
                    need_auth=True,
                )
                added.append(opt_code)
            except OVHAPIError as exc:
                self._log("WARNING", f"添加选项 {opt_code} 失败: {exc}", "order")
        return added
