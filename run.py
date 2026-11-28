#!/usr/bin/env python3
"""OVH Sniper — Main Entrypoint (Web Panel, Headless Daemon, and CLI Tools)."""

import argparse
import os
import signal
import sys
import time
from pathlib import Path

from ovh_buyer import __app_name__, __version__
from ovh_buyer.auth import PanelAuth
from ovh_buyer.catalog import CatalogManager
from ovh_buyer.config import format_datacenter_label, get_server_config, load_dotenv_file
from ovh_buyer.database import Database
from ovh_buyer.notifier import Notifier
from ovh_buyer.order_engine import OrderEngine
from ovh_buyer.ovh_client import OVHClient, parse_proxy_list
from ovh_buyer.sniper import SniperEngine
from ovh_buyer.web_server import AppContext, create_http_server


def _cli_proxy() -> str:
    for env_key in ("OVH_MONITOR_PROXIES", "OVH_MONITOR_PROXIES_POOL", "OVH_HTTP_PROXY", "HTTPS_PROXY", "HTTP_PROXY"):
        raw = os.environ.get(env_key, "").strip()
        if raw:
            items = parse_proxy_list(raw)
            return items[0] if items else raw
    return ""


def _build_services(seed_file: str = ""):
    db = Database()
    db.seed_from_env_or_file(Path(seed_file) if seed_file else None)
    catalog_mgr = CatalogManager()
    order_engine = OrderEngine(catalog_mgr, log_fn=db.add_log)
    notifier = Notifier(db)
    engine = SniperEngine(db=db, catalog_manager=catalog_mgr, order_engine=order_engine, notifier=notifier)
    auth = PanelAuth(db)
    return db, catalog_mgr, order_engine, notifier, engine, auth


def cmd_serve(args: argparse.Namespace) -> int:
    cfg = get_server_config()
    host = args.host or str(cfg["host"])
    port = args.port or int(cfg["port"])
    headless = bool(args.headless or cfg["headless"])

    db, catalog_mgr, order_engine, notifier, engine, auth = _build_services(args.config or "")

    stop_requested = False
    httpd = None

    def _handle_signal(signum, _frame):
        nonlocal stop_requested
        stop_requested = True
        print(f"\n[OVH Sniper] 收到信号 {signum}，正在优雅关闭...", flush=True)
        engine.stop()
        if httpd is not None:
            import threading

            threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    if not args.no_engine:
        engine.start()

    if headless:
        print(f"🎯 {__app_name__} v{__version__} 已以无头模式 (Headless) 启动", flush=True)
        print("   按 Ctrl+C 停止服务。可使用 Telegram Bot 指令交互控制。", flush=True)
        try:
            while not stop_requested:
                time.sleep(1.0)
        finally:
            engine.stop()
            db.close()
        return 0

    ctx = AppContext(
        db=db,
        catalog_manager=catalog_mgr,
        order_engine=order_engine,
        notifier=notifier,
        engine=engine,
        auth=auth,
    )
    httpd = create_http_server(host, port, ctx)
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print("=" * 60, flush=True)
    print(f"🎯 {__app_name__} v{__version__} 启动成功!", flush=True)
    print(f"🌐 Web 控制台:  http://{display_host}:{port}", flush=True)
    print(f"🔒 访问门禁:    {'已开启 (需密码登录)' if auth.is_auth_enabled() else '未设置密码 (仅建议本地使用)'}", flush=True)
    print(f"💾 数据目录:    {db.db_path.parent}", flush=True)
    print("=" * 60, flush=True)

    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        engine.stop()
        db.close()
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    proxy = _cli_proxy()
    client = OVHClient(endpoint=args.endpoint, proxy_url=proxy)
    items = client.get_availabilities(args.plan_code)
    if not items:
        print(f"未查到 {args.plan_code} ({args.endpoint}) 的任何库存记录。")
        return 1
    print(f"\n🔍 实时库存: {args.plan_code} ({args.endpoint})\n")
    for entry in items:
        print(f"FQN: {entry.get('fqn')}  |  RAM: {entry.get('memory')}  |  Disk: {entry.get('storage')}")
        for dc in entry.get("datacenters") or []:
            code = dc.get("datacenter", "")
            avail = dc.get("availability", "unknown")
            mark = "✅" if avail not in ("unavailable", "unknown") else "❌"
            print(f"  {mark} {format_datacenter_label(code):<28} {avail}")
        print()
    return 0


def cmd_catalog(args: argparse.Namespace) -> int:
    proxy = _cli_proxy()
    client = OVHClient(endpoint=args.endpoint, proxy_url=proxy)
    cm = CatalogManager()
    plans = cm.get_catalog_plans(
        client, subsidiary=args.subsidiary, product_line=args.product, force_refresh=args.refresh
    )
    if args.search:
        q = args.search.lower()
        plans = [
            p
            for p in plans
            if q in p["planCode"].lower()
            or q in p["name"].lower()
            or q in p["cpu"].lower()
            or q in p["memory"].lower()
            or q in p["storage"].lower()
        ]
    print(f"\n🗂️  OVH Catalog ({args.endpoint} / {args.subsidiary} / {args.product}) — 共 {len(plans)} 项\n")
    print(f"{'PLAN CODE':<22} {'PRICE':<14} {'CPU':<30} {'RAM / STORAGE'}")
    print("-" * 100)
    for p in plans[: args.limit]:
        price_str = f"{p['price']} {p['currency']}" if p["price"] is not None else "-"
        print(f"{p['planCode']:<22} {price_str:<14} {p['cpu'][:28]:<30} {p['memory']} | {p['storage']}")
    return 0


def cmd_vps(args: argparse.Namespace) -> int:
    proxy = _cli_proxy()
    client = OVHClient(endpoint=args.endpoint, proxy_url=proxy)
    data = client.get_vps_datacenter_availability(args.plan_code, subsidiary=args.subsidiary)
    dcs = data.get("datacenters") or []
    print(f"\n🖥️  VPS 库存: {args.plan_code} ({args.endpoint} / {args.subsidiary})\n")
    for d in dcs:
        st = d.get("status", "unknown")
        mark = "✅" if st == "available" else "❌"
        print(f"  {mark} {d.get('code', ''):<6} {d.get('name', ''):<24} {st}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"{__app_name__} v{__version__} — OVH 黑五自动抢购与监控套件")
    parser.add_argument("--env", help="指定 .env 文件路径")
    parser.add_argument("--host", help="监听地址 (默认 0.0.0.0)")
    parser.add_argument("--port", type=int, help="监听端口 (默认 8888)")
    parser.add_argument("--headless", action="store_true", help="无头模式（仅后台引擎 + Telegram Bot，不启 Web 端口）")
    parser.add_argument("--no-engine", action="store_true", help="仅启动 Web 控制台，不自动启动后台轮询")
    parser.add_argument("--config", help="启动时导入的 JSON 配置文件路径")

    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="启动 Web 控制台与抢购引擎（默认）")
    p_serve.add_argument("--host", help="监听地址")
    p_serve.add_argument("--port", type=int, help="监听端口")
    p_serve.add_argument("--headless", action="store_true", help="无头模式")
    p_serve.add_argument("--no-engine", action="store_true", help="不自动启动引擎")
    p_serve.add_argument("--config", help="初始 JSON 配置文件")

    p_check = sub.add_parser("check", help="命令行秒查指定 planCode 的实时库存")
    p_check.add_argument("plan_code", help="如 25skleb01 / 24ska01")
    p_check.add_argument("--endpoint", default="ovh-eu", choices=["ovh-eu", "ovh-ca", "ovh-us"])

    p_cat = sub.add_parser("catalog", help="命令行浏览/搜索 OVH 公开商品目录")
    p_cat.add_argument("--endpoint", default="ovh-eu", choices=["ovh-eu", "ovh-ca", "ovh-us"])
    p_cat.add_argument("--subsidiary", default="IE")
    p_cat.add_argument("--product", default="eco", choices=["eco", "baremetal", "vps"])
    p_cat.add_argument("--search", default="", help="过滤关键词")
    p_cat.add_argument("--limit", type=int, default=40)
    p_cat.add_argument("--refresh", action="store_true")

    p_vps = sub.add_parser("vps", help="命令行查询 VPS 机房库存")
    p_vps.add_argument("plan_code", help="如 vps-2025-model1")
    p_vps.add_argument("--endpoint", default="ovh-eu", choices=["ovh-eu", "ovh-ca", "ovh-us"])
    p_vps.add_argument("--subsidiary", default="IE")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.env:
        load_dotenv_file(Path(args.env))

    if args.command == "check":
        return cmd_check(args)
    if args.command == "catalog":
        return cmd_catalog(args)
    if args.command == "vps":
        return cmd_vps(args)
    return cmd_serve(args)


if __name__ == "__main__":
    sys.exit(main())
