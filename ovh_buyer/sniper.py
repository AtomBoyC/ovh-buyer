"""Background Sniper v2.0, Prebuilt Cart Fast Lane, Delivery Tracker, BF Radar, and VPS Monitor."""

import os
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .catalog import (
    CatalogManager,
    is_config_orderable,
    is_default_fqn_config,
    match_filter_expression,
    resolve_cart_addons,
    standardize_config,
)
from .config import (
    OVH_ORDER_STATUS_LABELS,
    build_express_order_url,
    decode_short_order_url,
    format_datacenter_label,
    is_availability_orderable,
    normalize_datacenter,
)
from .database import Database, now_iso
from .notifier import Notifier
from .order_engine import CartSlotPool, OrderEngine, safe_delete_cart
from .ovh_client import OVHClient
from .specs import GLOBAL_FX
from .tracker import OrderTracker


class SniperEngine:
    """Orchestrates shared/bulk polling, Fast Lane prebuilt carts, delivery/lottery tracking, BF radar & VPS."""

    def __init__(
        self,
        db: Database,
        catalog_manager: CatalogManager,
        order_engine: OrderEngine,
        notifier: Notifier,
        client_factory=None,
    ):
        self.db = db
        self.catalog_manager = catalog_manager
        self.order_engine = order_engine
        self.notifier = notifier
        self._custom_client_factory = client_factory

        self.slot_pool = CartSlotPool(order_engine, log_fn=db.add_log)
        self.tracker = OrderTracker(
            db=db,
            catalog_manager=catalog_manager,
            notifier=notifier,
            client_builder=self.build_client,
        )

        self._stop_event = threading.Event()
        self._threads: List[threading.Thread] = []
        self._oneshot_threads: List[threading.Thread] = []
        self._lock = threading.RLock()

        # In-memory cooldown & short-link cart cache
        self._task_last_poll: Dict[str, float] = {}
        self._stock_notify_cooldown: Dict[str, float] = {}
        self._order_fail_cooldown: Dict[str, float] = {}
        self._radar_last_poll: float = 0.0
        self._vps_last_poll: float = 0.0
        self._fastlane_last_tick: float = 0.0
        self._tracker_last_tick: float = 0.0
        self._short_link_lock = threading.Lock()
        self._short_link_carts: Dict[str, Dict[str, Any]] = {}
        # Tasks currently being evaluated/ordered (guards against concurrent double-orders)
        self._inflight_tasks: set = set()
        self._vps_tick_lock = threading.Lock()

        # Telemetry
        self.started_at: Optional[float] = None
        self.total_polls: int = 0
        self.stock_hits: int = 0

        self.notifier.register_bot_handlers(
            {
                "status": self._tg_cmd_status,
                "tasks": self._tg_cmd_tasks,
                "check": self._tg_cmd_check,
                "order": self._tg_cmd_short_order,
                "orders": self._tg_cmd_orders,
                "servers": self._tg_cmd_servers,
                "pause": self._tg_cmd_pause,
                "resume": self._tg_cmd_resume,
                "quick_buy": self._tg_action_quick_buy,
                "short_confirm": self._tg_action_short_confirm,
                "short_cancel": self._tg_action_short_cancel,
            }
        )

    # ---------- Client Construction ----------

    def build_client(self, account: Optional[Dict[str, Any]] = None, endpoint: str = "ovh-eu") -> OVHClient:
        settings = self.db.get_settings()
        qps = float(settings.get("global_qps") or 4.0)
        proxy = str(
            settings.get("http_proxy")
            or os.environ.get("OVH_HTTP_PROXY")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("HTTP_PROXY")
            or ""
        ).strip()
        if self._custom_client_factory:
            return self._custom_client_factory(account=account, endpoint=endpoint, qps=qps, proxy=proxy)
        if account:
            return OVHClient(
                endpoint=account.get("endpoint") or endpoint,
                app_key=account.get("app_key") or "",
                app_secret=account.get("app_secret") or "",
                consumer_key=account.get("consumer_key") or "",
                proxy_url=proxy,
                qps=qps,
            )
        return OVHClient(endpoint=endpoint, proxy_url=proxy, qps=qps)

    def get_account_for_id(self, account_id: str) -> Optional[Dict[str, Any]]:
        """Resolve an account. Falls back to the default account ONLY when no account was specified,
        so a task bound to a deleted account never silently buys on another account."""
        if account_id:
            return self.db.get_account(account_id, mask=False)
        return self.db.get_default_account(mask=False)

    def _try_acquire_task(self, task_id: str) -> bool:
        with self._lock:
            if task_id in self._inflight_tasks:
                return False
            self._inflight_tasks.add(task_id)
            return True

    def _release_task(self, task_id: str) -> None:
        with self._lock:
            self._inflight_tasks.discard(task_id)

    # ---------- Lifecycle ----------

    def start(self) -> None:
        with self._lock:
            if self._threads and any(t.is_alive() for t in self._threads):
                return
            self._stop_event.clear()
            self.started_at = time.time()
            self._threads = [
                threading.Thread(target=self._sniper_loop, name="sniper-loop", daemon=True),
                threading.Thread(target=self._fastlane_loop, name="fastlane-loop", daemon=True),
                threading.Thread(target=self._tracker_loop, name="tracker-loop", daemon=True),
                threading.Thread(target=self._radar_loop, name="radar-loop", daemon=True),
                threading.Thread(target=self._vps_loop, name="vps-loop", daemon=True),
            ]
            for t in self._threads:
                t.start()
            self.notifier.start_telegram_polling()
            self.db.add_log("INFO", "OVH Sniper v2.0 引擎已启动（含快速通道 & 交付中奖跟踪）", "engine")

    def stop(self) -> None:
        self._stop_event.set()
        self.notifier.stop()
        with self._lock:
            to_join = list(self._threads) + list(self._oneshot_threads)
            self._oneshot_threads.clear()
        for t in to_join:
            if t.is_alive() and t is not threading.current_thread():
                t.join(timeout=3.0)
        try:
            self.slot_pool.cleanup_all()
        except Exception:
            pass
        try:
            self.db.add_log("INFO", "OVH Sniper 引擎已优雅停止", "engine")
        except Exception:
            pass

    def _spawn_immediate_tick(self) -> None:
        def _runner() -> None:
            if self._stop_event.is_set():
                return
            try:
                self.run_sniper_tick()
            except Exception:
                pass

        th = threading.Thread(target=_runner, name="sniper-oneshot", daemon=True)
        with self._lock:
            self._oneshot_threads = [t for t in self._oneshot_threads if t.is_alive()]
            self._oneshot_threads.append(th)
        th.start()

    def get_status(self) -> Dict[str, Any]:
        settings = self.db.get_settings()
        uptime = int(time.time() - self.started_at) if self.started_at else 0
        return {
            "running": not self._stop_event.is_set() and any(t.is_alive() for t in self._threads),
            "sniper_enabled": bool(settings.get("sniper_enabled", True)),
            "fast_lane_enabled": bool(settings.get("fast_lane_enabled", True)),
            "tracker_enabled": bool(settings.get("tracker_enabled", True)),
            "radar_enabled": bool(settings.get("radar_enabled", False)),
            "vps_monitor_enabled": bool(settings.get("vps_monitor_enabled", False)),
            "uptime_seconds": uptime,
            "total_polls": self.total_polls,
            "stock_hits": self.stock_hits,
            "stats": self.db.get_stats(),
        }

    # ---------- Fast Lane Prebuilt Cart Maintenance Loop ----------

    def _fastlane_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                settings = self.db.get_settings()
                if settings.get("sniper_enabled", True) and settings.get("fast_lane_enabled", True):
                    if time.time() - self._fastlane_last_tick >= 15.0:
                        self._fastlane_last_tick = time.time()
                        self.run_fastlane_maintenance()
            except Exception as exc:
                self.db.add_log("WARNING", f"快速通道维护循环异常: {exc}", "fastlane")
            self._stop_event.wait(3.0)

    def run_fastlane_maintenance(self) -> int:
        """Maintain prebuilt carts for all running buy-mode non-dry-run tasks with fast_lane=True."""
        tasks = self.db.list_tasks()
        active_fl_ids = set()
        built = 0
        for task in tasks:
            eligible = (
                task["status"] == "running"
                and task["mode"] == "buy"
                and not task["dry_run"]
                and task.get("fast_lane")
                and task["product_line"] != "vps"
                and int(task["ordered_count"]) < int(task["max_orders"])
            )
            if not eligible:
                self.slot_pool.cleanup_task(task["id"])
                continue
            active_fl_ids.add(task["id"])
            acc = self.get_account_for_id(task["account_id"])
            if not acc or not acc.get("app_key"):
                continue
            target_dcs = task.get("datacenters") or []
            if not target_dcs:
                continue
            client = self.build_client(account=acc)
            plan_summary = None
            try:
                plan_summary = self.catalog_manager.find_plan(
                    client, task["plan_code"], subsidiary=acc["subsidiary"], product_line=task["product_line"]
                )
            except Exception:
                plan_summary = None
            try:
                resolved_opts = resolve_cart_addons(
                    plan_summary,
                    fqn_memory=task.get("memory_filter") or "",
                    fqn_storage=task.get("storage_filter") or "",
                    explicit_options=task.get("options") or [],
                )
            except ValueError:
                # Filter expression (e.g. '>=32g') isn't an exact addon code; use catalog defaults for prebuilt slot
                resolved_opts = resolve_cart_addons(plan_summary, explicit_options=task.get("options") or [])
            built += self.slot_pool.maintain_task(
                task=task,
                client=client,
                subsidiary=acc["subsidiary"],
                target_dcs=target_dcs,
                resolved_options=resolved_opts,
                stop_event=self._stop_event,
            )
        return built

    # ---------- Order Delivery Tracker Loop ----------

    def _tracker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                settings = self.db.get_settings()
                if settings.get("tracker_enabled", True) and (time.time() - self._tracker_last_tick >= 30.0):
                    self._tracker_last_tick = time.time()
                    self.tracker.tick()
            except Exception as exc:
                self.db.add_log("WARNING", f"交付跟踪循环异常: {exc}", "tracker")
            self._stop_event.wait(5.0)

    # ---------- Sniper Queue Loop ----------

    def _sniper_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                settings = self.db.get_settings()
                if not settings.get("sniper_enabled", True):
                    self._stop_event.wait(1.5)
                    continue
                self.run_sniper_tick()
            except Exception as exc:
                self.db.add_log("ERROR", f"抢购主循环异常: {exc}", "sniper")
            self._stop_event.wait(1.0)

    def run_sniper_tick(self) -> int:
        """Execute one polling cycle over all due running Eco/Baremetal sniper tasks."""
        now = time.time()
        all_tasks = self.db.list_tasks()
        due_tasks = []
        for task in all_tasks:
            if task["status"] != "running":
                continue
            if task["product_line"] == "vps":
                continue
            if int(task["ordered_count"]) >= int(task["max_orders"]) and task["mode"] == "buy":
                self.db.update_task(task["id"], {"status": "completed", "last_status_msg": "已达到最大购买数量"})
                self.slot_pool.cleanup_task(task["id"])
                continue
            last_ts = self._task_last_poll.get(task["id"], 0.0)
            if now - last_ts >= max(2, int(task["poll_interval"] or 5)):
                due_tasks.append(task)

        if not due_tasks:
            return 0

        by_endpoint: Dict[str, List[Dict[str, Any]]] = {}
        for task in due_tasks:
            acc = self.get_account_for_id(task["account_id"])
            ep = (acc or {}).get("endpoint") or "ovh-eu"
            by_endpoint.setdefault(ep, []).append(task)

        settings = self.db.get_settings()
        bulk_threshold = int(settings.get("bulk_availability_threshold") or 3)
        processed = 0

        for ep, ep_tasks in by_endpoint.items():
            unique_plans = sorted({t["plan_code"] for t in ep_tasks if t["plan_code"]})
            avail_by_plan: Dict[str, List[Dict[str, Any]]] = {}
            pub_client = self.build_client(account=None, endpoint=ep)

            if len(unique_plans) >= bulk_threshold:
                try:
                    bulk_list = pub_client.get_availabilities("")
                    self.total_polls += 1
                    plan_set = set(unique_plans)
                    for item in bulk_list:
                        pc = item.get("planCode")
                        if pc in plan_set:
                            avail_by_plan.setdefault(pc, []).append(item)
                except Exception as exc:
                    self.db.add_log("WARNING", f"批量查询库存失败({ep})，回退逐型号查询: {exc}", "sniper")

            for pc in unique_plans:
                if pc not in avail_by_plan:
                    try:
                        avail_by_plan[pc] = pub_client.get_availabilities(pc)
                        self.total_polls += 1
                    except Exception as exc:
                        self.db.add_log("WARNING", f"查询 {pc} 库存失败: {exc}", "sniper")
                        avail_by_plan[pc] = []

            for task in ep_tasks:
                if not self._try_acquire_task(task["id"]):
                    continue  # another thread is already evaluating/ordering this task
                try:
                    self._task_last_poll[task["id"]] = time.time()
                    fresh = self.db.get_task(task["id"])
                    if not fresh or fresh["status"] != "running":
                        continue
                    self._evaluate_task_availability(fresh, ep, avail_by_plan.get(fresh["plan_code"], []))
                    processed += 1
                finally:
                    self._release_task(task["id"])

        return processed

    def _evaluate_task_availability(
        self,
        task: Dict[str, Any],
        endpoint: str,
        avail_items: List[Dict[str, Any]],
    ) -> None:
        plan_code = task["plan_code"]
        target_dcs = [normalize_datacenter(d) for d in (task.get("datacenters") or [])]
        only_instant = bool(task.get("only_instant"))
        allow_non_default = bool(task.get("allow_non_default", True))
        mem_filter = task.get("memory_filter") or ""
        stor_filter = task.get("storage_filter") or ""
        explicit_options = task.get("options") or []

        if explicit_options and not mem_filter and not stor_filter:
            for opt in explicit_options:
                ol = opt.lower()
                if "ram-" in ol and not mem_filter:
                    mem_filter = standardize_config(opt)
                elif any(k in ol for k in ("softraid", "raid", "nvme", "ssd", "hybrid")) and not stor_filter:
                    stor_filter = standardize_config(opt)

        acc = self.get_account_for_id(task["account_id"])
        subsidiary = (acc or {}).get("subsidiary") or "IE"
        plan_summary = None
        try:
            pub_client = self.build_client(account=None, endpoint=endpoint)
            plan_summary = self.catalog_manager.find_plan(
                pub_client, plan_code, subsidiary=subsidiary, product_line=task["product_line"]
            )
        except Exception:
            plan_summary = None

        matched_hits: List[Dict[str, Any]] = []
        skip_reasons: List[str] = []
        summary_map: Dict[str, str] = {}

        for entry in avail_items:
            entry_mem = str(entry.get("memory") or "")
            entry_stor = str(entry.get("storage") or "")
            entry_fqn = str(entry.get("fqn") or plan_code)

            orderable_dcs = []
            for dc_obj in entry.get("datacenters") or []:
                dc = normalize_datacenter(str(dc_obj.get("datacenter") or ""))
                status = str(dc_obj.get("availability") or "unknown")
                if not dc:
                    continue
                if target_dcs and dc not in target_dcs:
                    continue
                prev = summary_map.get(dc)
                if prev is None or (not is_availability_orderable(prev) and is_availability_orderable(status)):
                    summary_map[dc] = status
                if is_availability_orderable(status, instant_only=only_instant):
                    orderable_dcs.append((dc, status))

            if not orderable_dcs:
                continue

            # 1. Phantom stock guard (e.g. KS-LE-3 64GB listed in /availabilities but absent from catalog)
            cfg_ok, phantom_reason = is_config_orderable(plan_summary, entry_mem, entry_stor)
            if not cfg_ok:
                skip_reasons.append(f"{entry_mem}+{entry_stor}: {phantom_reason}")
                continue

            # 2. User hardware filters
            if not match_filter_expression(mem_filter, entry_mem, is_memory=True):
                skip_reasons.append(f"内存不匹配({entry_mem})")
                continue
            if not match_filter_expression(stor_filter, entry_stor, is_memory=False):
                skip_reasons.append(f"硬盘不匹配({entry_stor})")
                continue

            # 3. Optional base-config-only guard when user didn't pin RAM/disk
            is_def = is_default_fqn_config(plan_summary, entry_mem, entry_stor)
            if not is_def and not allow_non_default and not mem_filter and not stor_filter:
                skip_reasons.append(f"非默认配置({entry_mem}+{entry_stor})")
                continue

            for dc, status in orderable_dcs:
                matched_hits.append(
                    {
                        "dc": dc,
                        "status": status,
                        "fqn": entry_fqn,
                        "memory": entry_mem,
                        "storage": entry_stor,
                        "is_default": is_def,
                    }
                )

        # Prefer default config first (unless pinned) and respect target DC priority order
        if target_dcs and matched_hits:
            dc_rank = {d: idx for idx, d in enumerate(target_dcs)}
            matched_hits.sort(
                key=lambda h: (0 if h["is_default"] else 1, dc_rank.get(h["dc"], 999))
            )
        elif matched_hits:
            matched_hits.sort(key=lambda h: 0 if h["is_default"] else 1)

        attempts = int(task.get("attempts") or 0) + 1
        prev_avail = task.get("last_availability") or {}
        card_key = f"task:{task['id']}"

        # Detect sold-out transition to strikethrough previous Telegram restock card
        prev_had_stock = any(is_availability_orderable(str(v), instant_only=only_instant) for v in prev_avail.values())
        if not matched_hits:
            if prev_had_stock:
                self.notifier.mark_stock_card_sold_out(card_key)
            if skip_reasons:
                msg = f"有货但不匹配: {'；'.join(skip_reasons[:2])}"
            else:
                msg = f"暂无符合条件的库存 (已检 {len(avail_items)} 组配置)"
            self.db.update_task(
                task["id"],
                {
                    "attempts": attempts,
                    "last_check_at": now_iso(),
                    "last_status_msg": msg,
                    "last_availability": summary_map,
                },
            )
            return

        # Stock detected!
        self.stock_hits += 1
        hit_summary = ", ".join(f"{h['dc'].upper()}({h['status']})" for h in matched_hits[:5])
        self.db.add_log("SUCCESS", f"🎯 发现有货! [{task['name']}] {plan_code}: {hit_summary}", "sniper")

        settings = self.db.get_settings()
        if settings.get("notify_on_stock", True):
            fresh_dcs = []
            now_ts = time.time()
            for h in matched_hits:
                ckey = f"{task['id']}:{h['dc']}"
                was_avail = is_availability_orderable(str(prev_avail.get(h["dc"], "")), instant_only=only_instant)
                last_notif = self._stock_notify_cooldown.get(ckey, 0.0)
                if (not was_avail) or (now_ts - last_notif >= 300):
                    self._stock_notify_cooldown[ckey] = now_ts
                    fresh_dcs.append(h)
            if fresh_dcs:
                top0 = fresh_dcs[0]
                self.notifier.notify_stock_with_buttons(
                    plan_code=plan_code,
                    available_dcs=fresh_dcs,
                    account_id=task["account_id"],
                    endpoint=endpoint,
                    subsidiary=subsidiary,
                    product_line=task["product_line"],
                    server_name=(plan_summary or {}).get("name") or "",
                    cpu=(plan_summary or {}).get("cpu") or "",
                    memory=top0["memory"],
                    storage=top0["storage"],
                    options=explicit_options,
                    price_val=(plan_summary or {}).get("price"),
                    setup_val=(plan_summary or {}).get("setupFee"),
                    currency=(plan_summary or {}).get("currency") or "EUR",
                    card_key=card_key,
                )

        if task["mode"] == "notify_only":
            self.db.update_task(
                task["id"],
                {
                    "attempts": attempts,
                    "last_check_at": now_iso(),
                    "last_status_msg": f"仅通知模式: 发现有货 {hit_summary}",
                    "last_availability": summary_map,
                },
            )
            return

        self.db.update_task(
            task["id"],
            {
                "attempts": attempts,
                "last_check_at": now_iso(),
                "last_status_msg": f"发现库存 {hit_summary}，正在下单...",
                "last_availability": summary_map,
            },
        )

        if not acc or not acc.get("app_key"):
            msg = "未配置有效 OVH 账号，无法自动下单"
            self.db.add_log("ERROR", f"[{task['name']}] {msg}", "sniper")
            self.db.update_task(task["id"], {"last_status_msg": msg})
            return

        client = self.build_client(account=acc)

        for hit in matched_hits:
            fresh_task = self.db.get_task(task["id"])
            if not fresh_task or fresh_task["status"] != "running":
                break
            if int(fresh_task["ordered_count"]) >= int(fresh_task["max_orders"]):
                break

            fail_key = f"{task['id']}:{hit['dc']}:{hit['fqn']}"
            if time.time() - self._order_fail_cooldown.get(fail_key, 0.0) < 20.0:
                continue

            try:
                options_to_use = resolve_cart_addons(
                    plan_summary,
                    fqn_memory=hit["memory"],
                    fqn_storage=hit["storage"],
                    explicit_options=explicit_options,
                )
            except ValueError as opt_err:
                self.db.add_log("WARNING", f"[{task['name']}] 跳过不可下单配置: {opt_err}", "sniper")
                continue

            # ⚡ Fast Lane: try prebuilt verified cart first (1 single POST /checkout!)
            result = None
            if not task["dry_run"] and task.get("fast_lane"):
                slot = self.slot_pool.take_ready_slot(task["id"], hit["dc"], task=fresh_task, options=options_to_use)
                if slot:
                    self.db.add_log(
                        "INFO",
                        f"⚡ [快速通道命中] {plan_code}@{hit['dc'].upper()} 使用预建购物车 {slot['cart_id']} 直接 Checkout!",
                        "order",
                    )
                    result = self.order_engine.checkout_cart(
                        client=client,
                        cart_id=slot["cart_id"],
                        plan_code=plan_code,
                        datacenter=hit["dc"],
                        prices_info=slot["prices"],
                        applied_options=slot["options"],
                        auto_pay=bool(task["auto_pay"]),
                        waive_retractation=bool(task["waive_retractation"]),
                        fast_lane=True,
                    )

            # Standard single-cart parallel path if no prebuilt slot was used
            if result is None:
                result = self.order_engine.place_order(
                    client=client,
                    subsidiary=acc["subsidiary"],
                    plan_code=plan_code,
                    datacenter=hit["dc"],
                    product_line=task["product_line"],
                    options=options_to_use,
                    duration=task["duration"],
                    pricing_mode=task["pricing_mode"],
                    os_code=task["os_code"],
                    coupons=task["coupons"],
                    quantity=task["quantity"],
                    max_price=float(task["max_price"] or 0),
                    price_includes_tax=bool(task["price_includes_tax"]),
                    auto_pay=bool(task["auto_pay"]),
                    waive_retractation=bool(task["waive_retractation"]),
                    dry_run=bool(task["dry_run"]),
                )

            self.db.add_order(
                {
                    "task_id": task["id"],
                    "account_id": acc["id"],
                    "account_name": acc["name"],
                    "product_line": task["product_line"],
                    "plan_code": plan_code,
                    "datacenter": hit["dc"],
                    "options": result.get("applied_options") or options_to_use,
                    "memory": hit["memory"],
                    "storage": hit["storage"],
                    "status": result["status"],
                    "dry_run": bool(task["dry_run"]),
                    "order_id": result.get("order_id", ""),
                    "order_url": result.get("order_url", ""),
                    "price_without_tax": result.get("price_without_tax"),
                    "price_with_tax": result.get("price_with_tax"),
                    "currency": result.get("currency", ""),
                    "error_message": result.get("error", ""),
                }
            )

            if result["success"]:
                tag = "演练通过" if task["dry_run"] else f"下单成功 #{result['order_id']}"
                ms_str = f" {result['checkout_ms']}ms" if result.get("checkout_ms") else ""
                lane = "⚡" if result.get("fast_lane") else ""
                msg = f"{lane}{tag}{ms_str} ({hit['dc'].upper()}, {result.get('price_without_tax')}/{result.get('price_with_tax')} {result.get('currency')})"
                updated = self.db.record_task_order_success(task["id"], msg)
                if updated and updated["status"] == "completed":
                    self.slot_pool.cleanup_task(task["id"])
                if settings.get("notify_on_order", True):
                    cny_with_tax = GLOBAL_FX.format_price_with_cny(
                        result.get("price_with_tax"), result.get("currency") or "EUR"
                    )
                    body = (
                        f"任务: {task['name']}\n"
                        f"账号: {acc['name']} ({acc['subsidiary']})\n"
                        f"型号: {plan_code}\n"
                        f"机房: {format_datacenter_label(hit['dc'])}\n"
                        f"配置: {hit['memory'] or '默认'} + {hit['storage'] or '默认'}\n"
                        f"含税总价: {cny_with_tax}\n"
                        f"订单号: {result.get('order_id')}\n"
                        f"通道: {'⚡ 预建快速通道' if result.get('fast_lane') else '直通下单'}{ms_str}\n"
                        f"自动扣款: {'是' if task['auto_pay'] else '否 (请尽快付款)'}"
                    )
                    self.notifier.notify(
                        title=f"{'🧪 演练成功' if task['dry_run'] else '🎉 OVH 抢购成功!'}: {plan_code}@{hit['dc'].upper()}",
                        body=body,
                        event="order_success",
                        urgent=not bool(task["dry_run"]),
                        url_link=result.get("order_url", ""),
                        extra=result,
                    )
                break

            # 🛡️ Uncertain checkout: HALT task immediately, NEVER auto-retry!
            if result["status"] == "pending_confirm":
                self.slot_pool.cleanup_task(task["id"])
                self.db.update_task(
                    task["id"],
                    {
                        "status": "pending_confirm",
                        "last_status_msg": f"⚠️ {result['error']}（已自动暂停防重单，请人工核对 OVH 后台）",
                    },
                )
                self.notifier.notify(
                    title=f"⚠️ 结账结果待核对（任务已自动停止防重单）: {plan_code}@{hit['dc'].upper()}",
                    body=(
                        f"任务: {task['name']}\n"
                        f"账号: {acc['name']} ({acc['subsidiary']})\n"
                        f"详情: {result['error']}\n\n"
                        "请登录 OVH 后台检查是否已生成订单，确认无误后再决定是否恢复任务。"
                    ),
                    event="pending_confirm",
                    urgent=True,
                    url_link=result.get("order_url", ""),
                )
                break

            self._order_fail_cooldown[fail_key] = time.time()
            self.db.update_task(task["id"], {"last_status_msg": f"尝试下单失败({hit['dc'].upper()}): {result['error']}"})
            if settings.get("notify_on_error", False) or result["status"] == "budget_exceeded":
                self.notifier.notify(
                    title=f"⚠️ OVH 下单未完成: {plan_code}@{hit['dc'].upper()}",
                    body=f"任务: {task['name']}\n原因: {result['error']}",
                    event="order_error",
                )

    # ---------- @ovheco / PoorVPS Short Order Link Support ----------

    def preview_short_order_link(self, url: str, account_id: str = "") -> Dict[str, Any]:
        """Decode /o/<region>/<token> locally, build cart for exact live quote, return confirm token."""
        cfg = decode_short_order_url(url)
        acc = self.get_account_for_id(account_id)
        if not acc or not acc.get("app_key"):
            raise ValueError("未配置有效 OVH 账号，请先在「多账号管理」添加账号")
        if acc["endpoint"] != cfg["endpoint"]:
            # Try to find an account matching the short-link region
            for cand in self.db.list_accounts(mask=False):
                if cand["endpoint"] == cfg["endpoint"]:
                    acc = cand
                    break
        if acc["endpoint"] != cfg["endpoint"]:
            raise ValueError(f"该短链属于区域 {cfg['endpoint']}，但当前未配置该区域的 OVH 账号")

        client = self.build_client(account=acc)
        built = self.order_engine.build_cart(
            client=client,
            subsidiary=acc["subsidiary"],
            plan_code=cfg["plan_code"],
            datacenter=cfg["datacenter"],
            product_line="eco",
            options=cfg["options"],
            duration="auto",
            pricing_mode="auto",
            parallel=True,
        )
        token = uuid.uuid4().hex[:12]
        expired: List[Dict[str, Any]] = []
        with self._short_link_lock:
            # Drop previews older than 30 min (OVH carts expire anyway) so the cache can't grow forever
            cutoff = time.time() - 1800
            for k in [k for k, v in self._short_link_carts.items() if v.get("created_ts", 0) < cutoff]:
                rec = self._short_link_carts.pop(k)
                if rec.get("state") == "pending":
                    expired.append(rec)
            self._short_link_carts[token] = {
                "token": token,
                "state": "pending",
                "cart_id": built["cart_id"],
                "account_id": acc["id"],
                "account_name": acc["name"],
                "endpoint": acc["endpoint"],
                "subsidiary": acc["subsidiary"],
                "plan_code": cfg["plan_code"],
                "datacenter": cfg["datacenter"],
                "memory": cfg["memory"],
                "storage": cfg["storage"],
                "options": built["applied_options"] or cfg["options"],
                "prices": built["prices"],
                "duration": built["duration"],
                "pricing_mode": built["pricing_mode"],
                "created_ts": time.time(),
            }
        for rec in expired:
            old_acc = self.db.get_account(rec["account_id"], mask=False)
            if old_acc:
                safe_delete_cart(self.build_client(account=old_acc), rec["cart_id"])
        return {
            "token": token,
            "account_name": acc["name"],
            "subsidiary": acc["subsidiary"],
            "plan_code": cfg["plan_code"],
            "datacenter": cfg["datacenter"],
            "options": built["applied_options"] or cfg["options"],
            "duration": built["duration"],
            "pricing_mode": built["pricing_mode"],
            "prices": built["prices"],
            "price_with_tax_cny": GLOBAL_FX.format_price_with_cny(
                built["prices"].get("withTax"), built["prices"].get("currencyCode") or "EUR"
            ),
        }

    def confirm_short_order_link(self, token: str, auto_pay: bool = False) -> Dict[str, Any]:
        """Atomically confirm & checkout a previewed short-link cart (unpaid by default, never double-clicks)."""
        with self._short_link_lock:
            rec = self._short_link_carts.get(token)
            if not rec:
                return {"success": False, "error": "该预览购物车已过期或不存在，请重新解析短链"}
            if rec["state"] != "pending":
                return {"success": False, "error": f"该订单已处理（当前状态: {rec['state']}），禁止重复提交"}
            rec["state"] = "submitting"

        acc = self.db.get_account(rec["account_id"], mask=False)
        if not acc:
            return {"success": False, "error": "账号不存在"}
        client = self.build_client(account=acc)
        res = self.order_engine.checkout_cart(
            client=client,
            cart_id=rec["cart_id"],
            plan_code=rec["plan_code"],
            datacenter=rec["datacenter"],
            prices_info=rec["prices"],
            applied_options=rec["options"],
            auto_pay=auto_pay,
            waive_retractation=False,
            fast_lane=False,
        )
        with self._short_link_lock:
            rec["state"] = res["status"]

        self.db.add_order(
            {
                "task_id": f"short:{token}",
                "account_id": acc["id"],
                "account_name": acc["name"],
                "product_line": "eco",
                "plan_code": rec["plan_code"],
                "datacenter": rec["datacenter"],
                "options": rec["options"],
                "memory": rec["memory"],
                "storage": rec["storage"],
                "status": res["status"],
                "dry_run": False,
                "order_id": res.get("order_id", ""),
                "order_url": res.get("order_url", ""),
                "price_without_tax": res.get("price_without_tax"),
                "price_with_tax": res.get("price_with_tax"),
                "currency": res.get("currency", ""),
                "error_message": res.get("error", ""),
            }
        )
        return res

    def cancel_short_order_link(self, token: str) -> bool:
        with self._short_link_lock:
            rec = self._short_link_carts.pop(token, None)
        if not rec:
            return False
        acc = self.db.get_account(rec["account_id"], mask=False)
        if acc:
            safe_delete_cart(self.build_client(account=acc), rec["cart_id"])
        return True

    # ---------- Black Friday Radar Loop (New SKU + Config Fingerprint) ----------

    def _radar_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                settings = self.db.get_settings()
                interval = max(10, int(settings.get("radar_poll_interval") or 30))
                if settings.get("radar_enabled", False) and (time.time() - self._radar_last_poll >= interval):
                    self._radar_last_poll = time.time()
                    self.run_radar_tick()
            except Exception as exc:
                self.db.add_log("ERROR", f"黑五雷达循环异常: {exc}", "radar")
            self._stop_event.wait(2.0)

    def run_radar_tick(self) -> int:
        rules = [r for r in self.db.list_radar_rules() if r["enabled"]]
        if not rules:
            return 0
        by_market: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for rule in rules:
            by_market.setdefault((rule["endpoint"], rule["subsidiary"]), []).append(rule)

        triggered = 0
        for (ep, sub), market_rules in by_market.items():
            try:
                client = self.build_client(account=None, endpoint=ep)
                plans = self.catalog_manager.get_catalog_plans(client, subsidiary=sub, product_line="eco", force_refresh=True)
            except Exception as exc:
                self.db.add_log("WARNING", f"雷达拉取目录失败({ep}/{sub}): {exc}", "radar")
                continue

            all_codes = [p["planCode"] for p in plans]
            for rule in market_rules:
                triggered += self._evaluate_radar_rule(rule, plans, all_codes)
        return triggered

    def _evaluate_radar_rule(self, rule: Dict[str, Any], plans: List[Dict[str, Any]], all_codes: List[str]) -> int:
        known = set(rule.get("known_plan_codes") or [])
        matched_prev = set(rule.get("matched_plan_codes") or [])
        kw_pat = rule.get("keyword_pattern") or ""
        mem_pat = rule.get("memory_pattern") or ""
        stor_pat = rule.get("storage_pattern") or ""
        max_price = float(rule.get("max_price") or 0)

        if rule["rule_type"] == "new_sku" and not known:
            self.db.update_radar_rule(
                rule["id"],
                {"known_plan_codes": all_codes, "last_check_at": now_iso()},
            )
            self.db.add_log("INFO", f"雷达 [{rule['name']}] 初始化基线完成，记录 {len(all_codes)} 个已知型号", "radar")
            return 0

        discoveries: List[Dict[str, Any]] = []
        for plan in plans:
            pc = plan["planCode"]
            if rule["rule_type"] == "new_sku" and pc in known:
                continue
            if pc in matched_prev:
                continue

            if kw_pat:
                haystack = f"{pc} {plan.get('name', '')} {plan.get('cpu', '')}"
                try:
                    if not re.search(kw_pat, haystack, re.I):
                        continue
                except re.error:
                    if kw_pat.lower() not in haystack.lower():
                        continue

            if max_price > 0 and plan.get("price") is not None and float(plan["price"]) > max_price:
                continue

            if mem_pat:
                mem_candidates = list(plan.get("memoryAddons") or []) + [plan.get("memoryCode") or "", plan.get("memory") or ""]
                if not any(match_filter_expression(mem_pat, m, is_memory=True) for m in mem_candidates):
                    continue
            if stor_pat:
                stor_candidates = list(plan.get("storageAddons") or []) + [plan.get("storageCode") or "", plan.get("storage") or ""]
                if not any(match_filter_expression(stor_pat, s, is_memory=False) for s in stor_candidates):
                    continue

            discoveries.append(plan)

        if not discoveries:
            self.db.update_radar_rule(rule["id"], {"last_check_at": now_iso()})
            return 0

        new_codes = [p["planCode"] for p in discoveries]
        updated_known = sorted(known | set(all_codes)) if rule["rule_type"] == "new_sku" else sorted(known)
        updated_matched = sorted(matched_prev | set(new_codes))
        self.db.update_radar_rule(
            rule["id"],
            {
                "known_plan_codes": updated_known,
                "matched_plan_codes": updated_matched,
                "last_check_at": now_iso(),
            },
        )

        lines = [f"规则: {rule['name']} ({rule['endpoint']}/{rule['subsidiary']})"]
        for p in discoveries[:10]:
            p_str = GLOBAL_FX.format_price_with_cny(p.get("price"), p.get("currency") or "EUR") if p.get("price") is not None else "未知价格"
            lines.append(f"  • {p['planCode']} | {p['cpu']} | {p['memory']} | {p['storage']} | {p_str}")

            if rule["auto_snipe"]:
                self.db.create_task(
                    {
                        "name": f"[雷达] {p['planCode']}",
                        "account_id": rule["account_id"],
                        "product_line": "eco",
                        "plan_code": p["planCode"],
                        "datacenters": rule["datacenters"],
                        "memory_filter": mem_pat,
                        "storage_filter": stor_pat,
                        "max_price": max_price,
                        "auto_pay": rule["auto_pay"],
                        "dry_run": rule["dry_run"],
                        "mode": "buy",
                        "priority": 50,
                        "poll_interval": 3,
                        "source": f"radar:{rule['id']}",
                    }
                )

        if rule["auto_snipe"]:
            lines.append("\n🚀 已自动加入高优先级抢购队列!")
            self._spawn_immediate_tick()

        self.db.add_log("SUCCESS", f"🆕 黑五雷达 [{rule['name']}] 发现 {len(discoveries)} 个目标: {', '.join(new_codes)}", "radar")
        self.notifier.notify(
            title=f"🚨 黑五新品雷达命中 ({len(discoveries)} 个)",
            body="\n".join(lines),
            event="radar",
            urgent=True,
            extra={"rule_id": rule["id"], "plan_codes": new_codes},
        )
        return len(discoveries)

    # ---------- VPS Monitor Loop ----------

    def _vps_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                settings = self.db.get_settings()
                interval = max(5, int(settings.get("vps_poll_interval") or 15))
                if settings.get("vps_monitor_enabled", False) and (time.time() - self._vps_last_poll >= interval):
                    self._vps_last_poll = time.time()
                    self.run_vps_tick()
            except Exception as exc:
                self.db.add_log("ERROR", f"VPS 监控循环异常: {exc}", "vps")
            self._stop_event.wait(2.0)

    def run_vps_tick(self) -> int:
        # Serialize VPS ticks (loop thread vs. /api/vps/check) so auto_order can't fire twice
        if not self._vps_tick_lock.acquire(blocking=False):
            return 0
        try:
            return self._run_vps_tick_locked()
        finally:
            self._vps_tick_lock.release()

    def _run_vps_tick_locked(self) -> int:
        watches = [w for w in self.db.list_vps_watches() if w["enabled"]]
        if not watches:
            return 0
        checked = 0
        for w in watches:
            try:
                pub_client = self.build_client(account=None, endpoint=w["endpoint"])
                data = pub_client.get_vps_datacenter_availability(w["plan_code"], subsidiary=w["subsidiary"])
                dcs_raw = data.get("datacenters") or []
                target_dcs = [d.upper() for d in (w.get("datacenters") or [])]
                prev_state = w.get("last_state") or {}
                new_state: Dict[str, Any] = {}
                newly_avail: List[Dict[str, Any]] = []

                for dc_item in dcs_raw:
                    code = str(dc_item.get("code") or dc_item.get("datacenter") or "").upper()
                    status = str(dc_item.get("status") or "unknown").lower()
                    name = str(dc_item.get("name") or code)
                    if not code:
                        continue
                    if target_dcs and code not in target_dcs:
                        continue
                    new_state[code] = {"name": name, "status": status, "days": dc_item.get("days", 0)}
                    prev_st = str((prev_state.get(code) or {}).get("status") or "")
                    if status == "available" and prev_st != "available":
                        newly_avail.append({"code": code, "name": name, "status": status})

                self.db.update_vps_watch(w["id"], {"last_state": new_state, "last_check_at": now_iso()})
                checked += 1

                if newly_avail:
                    lines = [f"VPS 型号: {w['plan_code']} ({w['subsidiary']})"]
                    for item in newly_avail:
                        lines.append(f"  • {item['name']} ({item['code']}) -> 现货")
                    self.db.add_log("SUCCESS", f"🎉 VPS 补货: {w['plan_code']} -> {[x['code'] for x in newly_avail]}", "vps")
                    self.notifier.notify(
                        title=f"🔥 OVH VPS 补货: {w['plan_code']}",
                        body="\n".join(lines),
                        event="stock",
                        urgent=True,
                    )
                    if w["auto_order"] and int(w["ordered_count"]) < int(w["max_orders"]):
                        acc = self.get_account_for_id(w["account_id"])
                        if acc and acc.get("app_key"):
                            auth_client = self.build_client(account=acc)
                            res = self.order_engine.place_order(
                                client=auth_client,
                                subsidiary=acc["subsidiary"],
                                plan_code=w["plan_code"],
                                datacenter=newly_avail[0]["code"],
                                product_line="vps",
                                auto_pay=bool(w["auto_pay"]),
                                dry_run=bool(w["dry_run"]),
                            )
                            self.db.add_order(
                                {
                                    "task_id": f"vps:{w['id']}",
                                    "account_id": acc["id"],
                                    "account_name": acc["name"],
                                    "product_line": "vps",
                                    "plan_code": w["plan_code"],
                                    "datacenter": newly_avail[0]["code"],
                                    "status": res["status"],
                                    "dry_run": bool(w["dry_run"]),
                                    "order_id": res.get("order_id", ""),
                                    "order_url": res.get("order_url", ""),
                                    "price_without_tax": res.get("price_without_tax"),
                                    "price_with_tax": res.get("price_with_tax"),
                                    "currency": res.get("currency", ""),
                                    "error_message": res.get("error", ""),
                                }
                            )
                            if res["success"]:
                                new_cnt = int(w["ordered_count"]) + 1
                                self.db.update_vps_watch(
                                    w["id"],
                                    {
                                        "ordered_count": new_cnt,
                                        "enabled": new_cnt < int(w["max_orders"]),
                                    },
                                )
            except Exception as exc:
                self.db.add_log("WARNING", f"VPS 监控 {w['plan_code']} 失败: {exc}", "vps")
        return checked

    # ---------- Synchronous On-Demand Operations ----------

    def check_plan_live(self, plan_code: str, endpoint: str = "ovh-eu", subsidiary: str = "IE") -> List[Dict[str, Any]]:
        """Query live datacenter availabilities + phantom-stock status + Express URLs for a planCode."""
        client = self.build_client(account=None, endpoint=endpoint)
        items = client.get_availabilities(plan_code)
        plan_summary = None
        try:
            plan_summary = self.catalog_manager.find_plan(client, plan_code, subsidiary=subsidiary, product_line="eco")
        except Exception:
            plan_summary = None
        out = []
        for entry in items:
            mem = str(entry.get("memory") or "")
            stor = str(entry.get("storage") or "")
            cfg_ok, cfg_reason = is_config_orderable(plan_summary, mem, stor)
            dcs = []
            for dc_obj in entry.get("datacenters") or []:
                dc = normalize_datacenter(str(dc_obj.get("datacenter") or ""))
                status = str(dc_obj.get("availability") or "unknown")
                dcs.append(
                    {
                        "datacenter": dc,
                        "label": format_datacenter_label(dc),
                        "availability": status,
                        "orderable": is_availability_orderable(status) and cfg_ok,
                        "express_url": build_express_order_url(
                            endpoint=endpoint,
                            subsidiary=subsidiary,
                            plan_code=plan_code,
                            datacenter=dc,
                        ),
                    }
                )
            out.append(
                {
                    "planCode": entry.get("planCode") or plan_code,
                    "fqn": entry.get("fqn") or plan_code,
                    "memory": mem,
                    "storage": stor,
                    "configOrderable": cfg_ok,
                    "unorderableReason": cfg_reason,
                    "isDefault": is_default_fqn_config(plan_summary, mem, stor),
                    "datacenters": dcs,
                }
            )
        return out

    def execute_task_now(self, task_id: str, force_dry_run: bool = False) -> Dict[str, Any]:
        task = self.db.get_task(task_id)
        if not task:
            return {"success": False, "error": "任务不存在"}
        acc = self.get_account_for_id(task["account_id"])
        if not acc or not acc.get("app_key"):
            return {"success": False, "error": "未配置有效的 OVH 账号"}

        client = self.build_client(account=acc)
        if force_dry_run:
            dcs = task.get("datacenters") or []
            dc = normalize_datacenter(dcs[0]) if dcs else "gra"
            plan_summary = None
            try:
                plan_summary = self.catalog_manager.find_plan(
                    client, task["plan_code"], subsidiary=acc["subsidiary"], product_line=task["product_line"]
                )
            except Exception:
                pass
            try:
                resolved_opts = resolve_cart_addons(
                    plan_summary,
                    fqn_memory=task.get("memory_filter") or "",
                    fqn_storage=task.get("storage_filter") or "",
                    explicit_options=task.get("options") or [],
                )
            except ValueError:
                resolved_opts = list(task.get("options") or [])
            res = self.order_engine.place_order(
                client=client,
                subsidiary=acc["subsidiary"],
                plan_code=task["plan_code"],
                datacenter=dc,
                product_line=task["product_line"],
                options=resolved_opts,
                duration=task["duration"],
                pricing_mode=task["pricing_mode"],
                os_code=task["os_code"],
                coupons=task.get("coupons") or [],
                quantity=task["quantity"],
                max_price=float(task["max_price"] or 0),
                price_includes_tax=bool(task["price_includes_tax"]),
                auto_pay=False,
                waive_retractation=True,
                dry_run=True,
            )
            self.db.add_order(
                {
                    "task_id": task["id"],
                    "account_id": acc["id"],
                    "account_name": acc["name"],
                    "product_line": task["product_line"],
                    "plan_code": task["plan_code"],
                    "datacenter": dc,
                    "options": res.get("applied_options") or resolved_opts,
                    "status": res["status"],
                    "dry_run": True,
                    "order_id": res.get("order_id", ""),
                    "order_url": "",
                    "price_without_tax": res.get("price_without_tax"),
                    "price_with_tax": res.get("price_with_tax"),
                    "currency": res.get("currency", ""),
                    "error_message": res.get("error", ""),
                }
            )
            return res

        if task["status"] != "running":
            return {"success": False, "error": f"任务当前状态为 {task['status']}，请先启动任务"}
        if not self._try_acquire_task(task_id):
            return {"success": False, "error": "该任务正在检测/下单中，请稍后再试"}
        try:
            avail = client.get_availabilities(task["plan_code"])
            self._task_last_poll[task["id"]] = time.time()
            self._evaluate_task_availability(task, acc["endpoint"], avail)
        finally:
            self._release_task(task_id)
        return {"success": True, "task": self.db.get_task(task_id)}

    def list_account_servers(self, account_id: str) -> List[Dict[str, Any]]:
        """Fetch read-only summary of delivered dedicated servers on an OVH account."""
        acc = self.db.get_account(account_id, mask=False) or self.db.get_default_account(mask=False)
        if not acc or not acc.get("app_key"):
            return []
        client = self.build_client(account=acc)
        names = client.list_dedicated_servers()
        out = []
        for name in names[:30]:
            try:
                srv = client.get_dedicated_server(name)
                hw = {}
                try:
                    hw = client.get_dedicated_hardware(name)
                except Exception:
                    pass
                si = {}
                try:
                    si = client.get_dedicated_service_infos(name)
                except Exception:
                    pass
                out.append(
                    {
                        "name": name,
                        "commercialRange": srv.get("commercialRange") or "",
                        "datacenter": srv.get("datacenter") or "",
                        "ip": srv.get("ip") or "",
                        "state": srv.get("state") or "",
                        "os": srv.get("os") or "",
                        "cpu": hw.get("processorName") or "",
                        "memory": hw.get("memorySize"),
                        "expiration": si.get("expiration") or "",
                        "creation": si.get("creation") or "",
                    }
                )
            except Exception:
                continue
        return out

    # ---------- Telegram Bot Command Handlers ----------

    def _tg_cmd_status(self, _args: List[str]) -> str:
        st = self.get_status()
        s = st["stats"]
        return (
            f"📊 OVH Sniper v2.0 运行状态\n\n"
            f"抢购引擎: {'🟢 运行中' if st['sniper_enabled'] else '⏸ 已暂停'}\n"
            f"预建快速通道: {'⚡ 开启' if st['fast_lane_enabled'] else '⚪ 关闭'}\n"
            f"交付/中奖跟踪: {'🎰 开启' if st['tracker_enabled'] else '⚪ 关闭'}\n"
            f"黑五雷达: {'🟢 开启' if st['radar_enabled'] else '⚪ 关闭'}\n"
            f"VPS 监控: {'🟢 开启' if st['vps_monitor_enabled'] else '⚪ 关闭'}\n"
            f"账号数: {s['accounts_count']} | 运行中任务: {s['tasks_running']}/{s['tasks_total']}\n"
            f"累计轮询: {st['total_polls']} | 命中库存: {st['stock_hits']}\n"
            f"成功订单: {s['orders_success']} | 演练: {s['orders_dry_run']}"
        )

    def _tg_cmd_tasks(self, _args: List[str]) -> str:
        tasks = self.db.list_tasks()
        if not tasks:
            return "当前没有抢购任务。"
        lines = ["🎯 抢购任务列表:"]
        for t in tasks[:15]:
            icon = (
                "🟢"
                if t["status"] == "running"
                else ("⚠️" if t["status"] == "pending_confirm" else ("✅" if t["status"] == "completed" else "⏸"))
            )
            dcs = ",".join(d.upper() for d in t["datacenters"]) if t["datacenters"] else "ALL"
            fl = " ⚡" if t.get("fast_lane") else ""
            dry = " [演练]" if t["dry_run"] else ""
            lines.append(f"{icon} {t['plan_code']}@{dcs}{fl}{dry} ({t['ordered_count']}/{t['max_orders']})")
        return "\n".join(lines)

    def _tg_cmd_check(self, args: List[str]) -> str:
        if not args:
            return "用法: /check <planCode> [ovh-eu|ovh-ca|ovh-us]"
        plan_code = args[0].strip()
        ep = args[1].strip() if len(args) > 1 else "ovh-eu"
        items = self.check_plan_live(plan_code, endpoint=ep)
        if not items:
            return f"未查到 {plan_code} ({ep}) 的库存数据。"
        lines = [f"🔍 实时库存 {plan_code} ({ep}):"]
        for entry in items[:4]:
            tag = " [⚠️目录不可订]" if not entry.get("configOrderable", True) else ""
            avail_dcs = [d for d in entry["datacenters"] if d["orderable"]]
            dc_txt = ", ".join(f"{d['datacenter'].upper()}({d['availability']})" for d in avail_dcs) if avail_dcs else "全无货"
            lines.append(f"• {entry['memory'] or '默认'} + {entry['storage'] or '默认'}{tag} -> {dc_txt}")
        return "\n".join(lines)

    def _tg_cmd_short_order(self, args: List[str]) -> Any:
        if not args:
            return "用法: /order <https://.../o/e/SHORT_TOKEN>\n支持直接粘贴 @ovheco 频道的补货直达链接！"
        preview = self.preview_short_order_link(args[0])
        p = preview["prices"]
        text = (
            f"🧾 短链购物车报价预览\n\n"
            f"账号: {preview['account_name']} ({preview['subsidiary']})\n"
            f"型号: {preview['plan_code']} @ {format_datacenter_label(preview['datacenter'])}\n"
            f"配件: {', '.join(preview['options']) or '默认'}\n"
            f"周期: {preview['duration']} / {preview['pricing_mode']}\n"
            f"不含税: {p.get('withoutTax')} {p.get('currencyCode')}\n"
            f"含税总计: {preview['price_with_tax_cny']}\n\n"
            f"⚠️ 仅生成未付款正式订单（不会自动扣款），请确认："
        )
        markup = {
            "inline_keyboard": [
                [
                    {"text": "✅ 确认生成未付款订单", "callback_data": f"sc:{preview['token']}"},
                    {"text": "❌ 取消并删车", "callback_data": f"sx:{preview['token']}"},
                ]
            ]
        }
        return {"text": text, "reply_markup": markup}

    def _tg_action_short_confirm(self, token: str) -> str:
        res = self.confirm_short_order_link(token, auto_pay=False)
        if res.get("success"):
            return (
                f"🎉 订单生成成功！\n"
                f"订单号: #{res.get('order_id')}\n"
                f"付款链接: {res.get('order_url') or '请登录 OVH 后台付款'}"
            )
        return f"⚠️ 下单未完成 ({res.get('status')}): {res.get('error')}"

    def _tg_action_short_cancel(self, token: str) -> str:
        ok = self.cancel_short_order_link(token)
        return "✅ 已取消并清理临时购物车。" if ok else "该预览购物车已清理或过期。"

    def _tg_cmd_orders(self, _args: List[str]) -> str:
        orders = self.db.list_orders(limit=8)
        if not orders:
            return "暂无订单记录。"
        lines = ["🧾 最近订单:"]
        for o in orders:
            icon = (
                "🎰"
                if (o.get("spec") or {}).get("lottery")
                else ("✅" if o["status"] == "success" else ("⚠️" if o["status"] == "pending_confirm" else ("🧪" if o["status"] == "dry_run" else "❌")))
            )
            ovh_st = f" [{OVH_ORDER_STATUS_LABELS.get(o['ovh_status'], o['ovh_status'])}]" if o.get("ovh_status") else ""
            srv_str = f" -> {o['server_name']}" if o.get("server_name") else ""
            url_part = f"\n  付款: {o['order_url']}" if o.get("order_url") else ""
            lines.append(f"{icon} {o['plan_code']}@{o['datacenter'].upper()} #{o['order_id'] or '-'}{ovh_st}{srv_str}{url_part}")
        return "\n".join(lines)

    def _tg_cmd_servers(self, _args: List[str]) -> str:
        accs = self.db.list_accounts(mask=True)
        if not accs:
            return "尚未配置 OVH 账号。"
        servers = self.list_account_servers(accs[0]["id"])
        if not servers:
            return f"账号 {accs[0]['name']} 下暂无独服（或当前 Token 无 GET /dedicated/server 权限）。"
        lines = [f"🖥️ {accs[0]['name']} 独服列表 ({len(servers)}):"]
        for s in servers[:15]:
            lines.append(f"• {s['name']} | {s['datacenter'].upper()} | {s['ip']} | {s['cpu']} | 到期 {str(s['expiration'])[:10]}")
        return "\n".join(lines)

    def _tg_cmd_pause(self, _args: List[str]) -> str:
        self.db.update_settings({"sniper_enabled": False})
        return "⏸ 已暂停全局抢购引擎。"

    def _tg_cmd_resume(self, _args: List[str]) -> str:
        self.db.update_settings({"sniper_enabled": True})
        return "🟢 已恢复全局抢购引擎。"

    def _tg_action_quick_buy(self, meta: Dict[str, Any]) -> str:
        plan_code = meta.get("plan_code", "")
        dc = meta.get("datacenter", "")
        if not plan_code or not dc:
            return "❌ 参数不完整"
        price_val = meta.get("price_val")
        try:
            price_val = float(price_val) if price_val is not None else None
        except (TypeError, ValueError):
            price_val = None
        if not price_val or price_val <= 0:
            return "⚠️ 该补货未获取到参考价，为防止高价误下单已拒绝一键抢购，请在面板创建带价格上限的任务。"
        settings = self.db.get_settings()
        margin = float(settings.get("quick_buy_price_margin") or 0.10)
        max_price = round(price_val * (1.0 + max(0.0, margin)), 2)
        task = self.db.create_task(
            {
                "name": f"[TG一键] {plan_code}@{dc.upper()}",
                "account_id": meta.get("account_id", ""),
                "product_line": meta.get("product_line", "eco"),
                "plan_code": plan_code,
                "datacenters": [dc],
                "options": meta.get("options") or [],
                "max_price": max_price,
                "price_includes_tax": False,
                "max_orders": 1,
                "poll_interval": 2,
                "priority": 100,
                "mode": "buy",
                "dry_run": False,
                "source": "telegram",
            }
        )
        self._spawn_immediate_tick()
        return f"🚀 已创建高优先级抢购任务 #{task['id']}: {plan_code}@{dc.upper()}（不含税上限 {max_price}），正在立即尝试下单!"
