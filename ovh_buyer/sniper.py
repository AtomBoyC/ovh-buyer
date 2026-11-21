"""Background Sniper, Black Friday Catalog Radar, and VPS Monitor Engines."""

import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .catalog import (
    CatalogManager,
    match_filter_expression,
    resolve_addons_for_fqn,
    standardize_config,
)
from .config import (
    format_datacenter_label,
    is_availability_orderable,
    normalize_datacenter,
)
from .database import Database, now_iso
from .notifier import Notifier
from .order_engine import OrderEngine
from .ovh_client import OVHAPIError, OVHClient


class SniperEngine:
    """Orchestrates shared availability polling, auto-ordering, Black Friday radar, and VPS watching."""

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

        self._stop_event = threading.Event()
        self._threads: List[threading.Thread] = []
        self._oneshot_threads: List[threading.Thread] = []
        self._lock = threading.RLock()

        # In-memory cooldown maps to avoid spam & runaway retries
        self._task_last_poll: Dict[str, float] = {}
        self._stock_notify_cooldown: Dict[str, float] = {}
        self._order_fail_cooldown: Dict[str, float] = {}
        self._radar_last_poll: float = 0.0
        self._vps_last_poll: float = 0.0

        # Telemetry
        self.started_at: Optional[float] = None
        self.total_polls: int = 0
        self.stock_hits: int = 0
        self._clients_cache: Dict[str, OVHClient] = {}

        # Wire Telegram Bot commands
        self.notifier.register_bot_handlers(
            {
                "status": self._tg_cmd_status,
                "tasks": self._tg_cmd_tasks,
                "check": self._tg_cmd_check,
                "orders": self._tg_cmd_orders,
                "pause": self._tg_cmd_pause,
                "resume": self._tg_cmd_resume,
                "quick_buy": self._tg_action_quick_buy,
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
        return self.db.get_account(account_id, mask=False) or self.db.get_default_account(mask=False)

    # ---------- Lifecycle ----------

    def start(self) -> None:
        with self._lock:
            if self._threads and any(t.is_alive() for t in self._threads):
                return
            self._stop_event.clear()
            self.started_at = time.time()
            self._threads = [
                threading.Thread(target=self._sniper_loop, name="sniper-loop", daemon=True),
                threading.Thread(target=self._radar_loop, name="radar-loop", daemon=True),
                threading.Thread(target=self._vps_loop, name="vps-loop", daemon=True),
            ]
            for t in self._threads:
                t.start()
            self.notifier.start_telegram_polling()
            self.db.add_log("INFO", "OVH 抢购与监控引擎已启动", "engine")

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
            self.db.add_log("INFO", "OVH 抢购与监控引擎已停止", "engine")
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
            "radar_enabled": bool(settings.get("radar_enabled", False)),
            "vps_monitor_enabled": bool(settings.get("vps_monitor_enabled", False)),
            "uptime_seconds": uptime,
            "total_polls": self.total_polls,
            "stock_hits": self.stock_hits,
            "stats": self.db.get_stats(),
        }

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
                continue
            last_ts = self._task_last_poll.get(task["id"], 0.0)
            if now - last_ts >= max(2, int(task["poll_interval"] or 5)):
                due_tasks.append(task)

        if not due_tasks:
            return 0

        # Group due tasks by endpoint to share & batch availability requests
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

            # Use 1 bulk request when monitoring >= threshold distinct plans on the same endpoint
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
                self._task_last_poll[task["id"]] = time.time()
                self._evaluate_task_availability(task, ep, avail_by_plan.get(task["plan_code"], []))
                processed += 1

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
        mem_filter = task.get("memory_filter") or ""
        stor_filter = task.get("storage_filter") or ""
        explicit_options = task.get("options") or []

        # Also extract explicit memory/storage filter from explicit options if filters are empty
        if explicit_options and not mem_filter and not stor_filter:
            for opt in explicit_options:
                ol = opt.lower()
                if "ram-" in ol and not mem_filter:
                    mem_filter = standardize_config(opt)
                elif any(k in ol for k in ("softraid", "raid", "nvme", "ssd", "hybrid")) and not stor_filter:
                    stor_filter = standardize_config(opt)

        matched_hits: List[Dict[str, Any]] = []
        summary_map: Dict[str, str] = {}

        for entry in avail_items:
            entry_mem = str(entry.get("memory") or "")
            entry_stor = str(entry.get("storage") or "")
            entry_fqn = str(entry.get("fqn") or plan_code)

            if not match_filter_expression(mem_filter, entry_mem, is_memory=True):
                continue
            if not match_filter_expression(stor_filter, entry_stor, is_memory=False):
                continue

            for dc_obj in entry.get("datacenters") or []:
                dc = normalize_datacenter(str(dc_obj.get("datacenter") or ""))
                status = str(dc_obj.get("availability") or "unknown")
                if not dc:
                    continue
                if target_dcs and dc not in target_dcs:
                    continue
                # Preserve best status per DC in summary_map
                prev = summary_map.get(dc)
                if prev is None or (not is_availability_orderable(prev) and is_availability_orderable(status)):
                    summary_map[dc] = status
                if is_availability_orderable(status, instant_only=only_instant):
                    matched_hits.append(
                        {
                            "dc": dc,
                            "status": status,
                            "fqn": entry_fqn,
                            "memory": entry_mem,
                            "storage": entry_stor,
                        }
                    )

        # Preserve DC priority order if user specified datacenters
        if target_dcs and matched_hits:
            dc_rank = {d: idx for idx, d in enumerate(target_dcs)}
            matched_hits.sort(key=lambda h: dc_rank.get(h["dc"], 999))

        attempts = int(task.get("attempts") or 0) + 1
        prev_avail = task.get("last_availability") or {}

        if not matched_hits:
            self.db.update_task(
                task["id"],
                {
                    "attempts": attempts,
                    "last_check_at": now_iso(),
                    "last_status_msg": f"暂无符合条件的库存 (已检 {len(avail_items)} 组配置)",
                    "last_availability": summary_map,
                },
            )
            return

        # Stock detected!
        self.stock_hits += 1
        hit_summary = ", ".join(f"{h['dc'].upper()}({h['status']})" for h in matched_hits[:5])
        self.db.add_log("SUCCESS", f"🎯 发现有货! [{task['name']}] {plan_code}: {hit_summary}", "sniper")

        # Notify on newly available DCs (with 5-minute cooldown per task+dc)
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
                    product_line=task["product_line"],
                    memory=top0["memory"],
                    storage=top0["storage"],
                    options=explicit_options,
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

        # Attempt order on the highest-priority available hit
        self.db.update_task(
            task["id"],
            {
                "attempts": attempts,
                "last_check_at": now_iso(),
                "last_status_msg": f"发现库存 {hit_summary}，正在下单...",
                "last_availability": summary_map,
            },
        )

        acc = self.get_account_for_id(task["account_id"])
        if not acc or not acc.get("app_key"):
            msg = "未配置有效 OVH 账号，无法自动下单"
            self.db.add_log("ERROR", f"[{task['name']}] {msg}", "sniper")
            self.db.update_task(task["id"], {"last_status_msg": msg})
            return

        client = self.build_client(account=acc)
        plan_summary = None
        if not explicit_options:
            try:
                plan_summary = self.catalog_manager.find_plan(
                    client, plan_code, subsidiary=acc["subsidiary"], product_line=task["product_line"]
                )
            except Exception:
                plan_summary = None

        for hit in matched_hits:
            # Refresh task quota check before each attempt
            fresh_task = self.db.get_task(task["id"])
            if not fresh_task or fresh_task["status"] != "running":
                break
            if int(fresh_task["ordered_count"]) >= int(fresh_task["max_orders"]):
                break

            fail_key = f"{task['id']}:{hit['dc']}:{hit['fqn']}"
            if time.time() - self._order_fail_cooldown.get(fail_key, 0.0) < 20.0:
                continue

            options_to_use = list(explicit_options)
            if not options_to_use and plan_summary:
                options_to_use = resolve_addons_for_fqn(plan_summary, hit["memory"], hit["storage"])

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
                msg = f"{tag} ({hit['dc'].upper()}, {result.get('price_without_tax')}/{result.get('price_with_tax')} {result.get('currency')})"
                self.db.record_task_order_success(task["id"], msg)
                if settings.get("notify_on_order", True):
                    price_line = (
                        f"不含税 {result.get('price_without_tax')} / 含税 {result.get('price_with_tax')} {result.get('currency')}"
                        if result.get("price_without_tax") is not None or result.get("price_with_tax") is not None
                        else "待确认"
                    )
                    body = (
                        f"任务: {task['name']}\n"
                        f"账号: {acc['name']} ({acc['subsidiary']})\n"
                        f"型号: {plan_code}\n"
                        f"机房: {format_datacenter_label(hit['dc'])}\n"
                        f"配置: {hit['memory'] or '默认'} + {hit['storage'] or '默认'}\n"
                        f"价格: {price_line}\n"
                        f"订单号: {result.get('order_id')}\n"
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
            else:
                self._order_fail_cooldown[fail_key] = time.time()
                self.db.update_task(task["id"], {"last_status_msg": f"尝试下单失败({hit['dc'].upper()}): {result['error']}"})
                if settings.get("notify_on_error", False) or result["status"] == "budget_exceeded":
                    self.notifier.notify(
                        title=f"⚠️ OVH 下单未完成: {plan_code}@{hit['dc'].upper()}",
                        body=f"任务: {task['name']}\n原因: {result['error']}",
                        event="order_error",
                    )

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

        # For new_sku rules, first run establishes the baseline catalog snapshot
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

            # Keyword / regex filter on planCode + name + cpu
            if kw_pat:
                haystack = f"{pc} {plan.get('name', '')} {plan.get('cpu', '')}"
                try:
                    if not re.search(kw_pat, haystack, re.I):
                        continue
                except re.error:
                    if kw_pat.lower() not in haystack.lower():
                        continue

            # Price filter (base monthly price from catalog)
            if max_price > 0 and plan.get("price") is not None and float(plan["price"]) > max_price:
                continue

            # Hardware filter (for config_match or optional constraints)
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
            p_str = f"{p['price']} {p['currency']}" if p.get("price") is not None else "未知价格"
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
            # Trigger immediate sniper tick in background
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

    def check_plan_live(self, plan_code: str, endpoint: str = "ovh-eu") -> List[Dict[str, Any]]:
        """Query live datacenter availabilities for a single planCode."""
        client = self.build_client(account=None, endpoint=endpoint)
        items = client.get_availabilities(plan_code)
        out = []
        for entry in items:
            dcs = []
            for dc_obj in entry.get("datacenters") or []:
                dc = normalize_datacenter(str(dc_obj.get("datacenter") or ""))
                status = str(dc_obj.get("availability") or "unknown")
                dcs.append(
                    {
                        "datacenter": dc,
                        "label": format_datacenter_label(dc),
                        "availability": status,
                        "orderable": is_availability_orderable(status),
                    }
                )
            out.append(
                {
                    "planCode": entry.get("planCode") or plan_code,
                    "fqn": entry.get("fqn") or plan_code,
                    "memory": entry.get("memory") or "",
                    "storage": entry.get("storage") or "",
                    "datacenters": dcs,
                }
            )
        return out

    def execute_task_now(self, task_id: str, force_dry_run: bool = False) -> Dict[str, Any]:
        """Manually trigger one immediate availability check + order/dry-run for a task."""
        task = self.db.get_task(task_id)
        if not task:
            return {"success": False, "error": "任务不存在"}
        acc = self.get_account_for_id(task["account_id"])
        if not acc or not acc.get("app_key"):
            return {"success": False, "error": "未配置有效的 OVH 账号"}

        client = self.build_client(account=acc)
        if force_dry_run:
            # Test checkout pipeline directly using first target DC (or 'gra')
            dcs = task.get("datacenters") or []
            dc = normalize_datacenter(dcs[0]) if dcs else "gra"
            res = self.order_engine.place_order(
                client=client,
                subsidiary=acc["subsidiary"],
                plan_code=task["plan_code"],
                datacenter=dc,
                product_line=task["product_line"],
                options=task.get("options") or [],
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
                    "options": res.get("applied_options") or task.get("options") or [],
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

        avail = client.get_availabilities(task["plan_code"])
        self._task_last_poll[task["id"]] = time.time()
        self._evaluate_task_availability(task, acc["endpoint"], avail)
        return {"success": True, "task": self.db.get_task(task_id)}

    # ---------- Telegram Bot Command Handlers ----------

    def _tg_cmd_status(self, _args: List[str]) -> str:
        st = self.get_status()
        s = st["stats"]
        return (
            f"📊 OVH Sniper 运行状态\n\n"
            f"抢购引擎: {'🟢 运行中' if st['sniper_enabled'] else '⏸ 已暂停'}\n"
            f"黑五雷达: {'🟢 开启' if st['radar_enabled'] else '⚪ 关闭'}\n"
            f"VPS 监控: {'🟢 开启' if st['vps_monitor_enabled'] else '⚪ 关闭'}\n"
            f"账号数: {s['accounts_count']}\n"
            f"运行中任务: {s['tasks_running']} / 总任务 {s['tasks_total']}\n"
            f"累计轮询: {st['total_polls']} | 命中库存: {st['stock_hits']}\n"
            f"成功订单: {s['orders_success']} | 演练: {s['orders_dry_run']}"
        )

    def _tg_cmd_tasks(self, _args: List[str]) -> str:
        tasks = self.db.list_tasks()
        if not tasks:
            return "当前没有抢购任务。"
        lines = ["🎯 抢购任务列表:"]
        for t in tasks[:15]:
            icon = "🟢" if t["status"] == "running" else ("✅" if t["status"] == "completed" else "⏸")
            dcs = ",".join(d.upper() for d in t["datacenters"]) if t["datacenters"] else "ALL"
            dry = " [演练]" if t["dry_run"] else ""
            lines.append(f"{icon} {t['plan_code']}@{dcs}{dry} ({t['ordered_count']}/{t['max_orders']})")
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
            avail_dcs = [d for d in entry["datacenters"] if d["orderable"]]
            dc_txt = ", ".join(f"{d['datacenter'].upper()}({d['availability']})" for d in avail_dcs) if avail_dcs else "全无货"
            lines.append(f"• {entry['memory'] or '默认'} + {entry['storage'] or '默认'} -> {dc_txt}")
        return "\n".join(lines)

    def _tg_cmd_orders(self, _args: List[str]) -> str:
        orders = self.db.list_orders(limit=8)
        if not orders:
            return "暂无订单记录。"
        lines = ["🧾 最近订单:"]
        for o in orders:
            icon = "✅" if o["status"] == "success" else ("🧪" if o["status"] == "dry_run" else "❌")
            url_part = f"\n  付款: {o['order_url']}" if o.get("order_url") else ""
            lines.append(f"{icon} {o['plan_code']}@{o['datacenter'].upper()} #{o['order_id'] or '-'} ({o['created_at']}){url_part}")
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
        task = self.db.create_task(
            {
                "name": f"[TG一键] {plan_code}@{dc.upper()}",
                "account_id": meta.get("account_id", ""),
                "product_line": meta.get("product_line", "eco"),
                "plan_code": plan_code,
                "datacenters": [dc],
                "options": meta.get("options") or [],
                "poll_interval": 2,
                "priority": 100,
                "mode": "buy",
                "dry_run": False,
                "source": "telegram",
            }
        )
        self._spawn_immediate_tick()
        return f"🚀 已创建高优先级抢购任务 #{task['id']}: {plan_code}@{dc.upper()}，正在立即尝试下单!"
