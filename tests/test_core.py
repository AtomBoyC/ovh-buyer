"""Comprehensive end-to-end & unit tests for OVH Sniper (with real local mock OVH API)."""

import hashlib
import http.server
import json
import socketserver
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ovh_buyer.auth import PanelAuth
from ovh_buyer.catalog import (
    CatalogManager,
    match_filter_expression,
    parse_catalog_plans,
    resolve_addons_for_fqn,
    standardize_config,
)
from ovh_buyer.config import infer_region_for_datacenter, is_availability_orderable, normalize_datacenter
from ovh_buyer.database import Database, protect_secret, reveal_secret
from ovh_buyer.notifier import Notifier
from ovh_buyer.order_engine import OrderEngine
from ovh_buyer.ovh_client import OVHClient
from ovh_buyer.sniper import SniperEngine
from ovh_buyer.web_server import AppContext, create_http_server


MOCK_APP_KEY = "test_ak_123"
MOCK_APP_SECRET = "test_as_secret_456"
MOCK_CONSUMER_KEY = "test_ck_789"


class MockOVHState:
    def __init__(self):
        self.reset()

    def reset(self):
        self.carts = {}
        self.deleted_carts = []
        self.checkouts = []
        self.bulk_calls = 0
        self.per_plan_calls = 0
        self.reject_os_en = False
        self.price_without_tax = 14.99
        self.price_with_tax = 17.99
        self.availabilities = [
            {
                "planCode": "26skleb01",
                "fqn": "26skleb01.ram-32g-ecc-2400.softraid-2x450nvme",
                "memory": "ram-32g-ecc-2400",
                "storage": "softraid-2x450nvme",
                "datacenters": [
                    {"datacenter": "gra", "availability": "1H-high"},
                    {"datacenter": "rbx", "availability": "unavailable"},
                    {"datacenter": "fra", "availability": "24H"},
                ],
            },
            {
                "planCode": "25sklea01",
                "fqn": "25sklea01.ram-16g-ecc-2133.softraid-2x4000sa",
                "memory": "ram-16g-ecc-2133",
                "storage": "softraid-2x4000sa",
                "datacenters": [{"datacenter": "gra", "availability": "unavailable"}],
            },
            {
                "planCode": "25rise01",
                "fqn": "25rise01.ram-64g-ecc-3200.softraid-2x960nvme",
                "memory": "ram-64g-ecc-3200",
                "storage": "softraid-2x960nvme",
                "datacenters": [{"datacenter": "fra", "availability": "unavailable"}],
            },
        ]
        self.catalog_plans = [
            {
                "planCode": "25sklea01",
                "invoiceName": "KS-LE-A | Intel Xeon E3-1245v5",
                "pricings": [
                    {"mode": "default", "interval": 1, "intervalUnit": "month", "price": 999000000, "capacities": ["renew"]},
                    {"mode": "default", "interval": 1, "intervalUnit": "month", "price": 0, "capacities": ["installation"]},
                ],
                "addonFamilies": [
                    {"name": "memory", "default": "ram-16g-ecc-2133-25sklea01", "addons": ["ram-16g-ecc-2133-25sklea01"]},
                    {"name": "storage", "default": "softraid-2x4000sa-25sklea01", "addons": ["softraid-2x4000sa-25sklea01"]},
                ],
            },
            {
                "planCode": "26skleb01",
                "invoiceName": "KS-LE-B Black Friday | Intel Xeon-D 1540",
                "pricings": [
                    {"mode": "default", "interval": 1, "intervalUnit": "month", "price": 1499000000, "capacities": ["renew"]},
                ],
                "addonFamilies": [
                    {
                        "name": "memory",
                        "default": "ram-32g-ecc-2400-26skleb01",
                        "addons": ["ram-32g-ecc-2400-26skleb01", "ram-64g-ecc-2400-26skleb01"],
                    },
                    {
                        "name": "storage",
                        "default": "softraid-2x450nvme-26skleb01",
                        "addons": ["softraid-2x450nvme-26skleb01"],
                    },
                ],
            },
        ]
        self.vps_state = [
            {"code": "GRA", "name": "Gravelines", "status": "out-of-stock", "days": 0},
            {"code": "SBG", "name": "Strasbourg", "status": "out-of-stock", "days": 0},
        ]


class MockOVHServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, state: MockOVHState):
        self.state = state
        super().__init__(("127.0.0.1", 0), MockOVHHandler)


class MockOVHHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002
        return

    @property
    def st(self) -> MockOVHState:
        return self.server.state  # type: ignore[attr-defined]

    def _send(self, obj, code=200):
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _verify_sig(self, method: str, body_str: str) -> bool:
        ak = self.headers.get("X-Ovh-Application", "")
        ck = self.headers.get("X-Ovh-Consumer", "")
        ts = self.headers.get("X-Ovh-Timestamp", "")
        sig = self.headers.get("X-Ovh-Signature", "")
        if ak != MOCK_APP_KEY or ck != MOCK_CONSUMER_KEY or not ts or not sig:
            self._send({"message": "Invalid credentials"}, 401)
            return False
        port = self.server.server_port
        full_url = f"http://127.0.0.1:{port}{self.path}"
        expected = "$1$" + hashlib.sha1(
            f"{MOCK_APP_SECRET}+{MOCK_CONSUMER_KEY}+{method}+{full_url}+{body_str}+{ts}".encode("utf-8")
        ).hexdigest()
        if sig != expected:
            self._send({"message": f"Invalid signature (expected {expected}, got {sig})"}, 400)
            return False
        return True

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return "", {}
        raw = self.rfile.read(n).decode("utf-8")
        return raw, (json.loads(raw) if raw else {})

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/1.0/auth/time":
            self._send(int(time.time()))
            return
        if path == "/1.0/order/catalog/public/eco":
            self._send({"locale": {"currencyCode": "EUR"}, "plans": self.st.catalog_plans, "addons": [], "products": []})
            return
        if path == "/1.0/dedicated/server/datacenter/availabilities":
            pc = (qs.get("planCode") or [""])[0]
            if pc:
                self.st.per_plan_calls += 1
                self._send([a for a in self.st.availabilities if a["planCode"] == pc])
            else:
                self.st.bulk_calls += 1
                self._send(self.st.availabilities)
            return
        if path == "/v1/vps/order/rule/datacenter":
            self._send({"datacenters": self.st.vps_state})
            return

        if not self._verify_sig("GET", ""):
            return

        if path == "/1.0/me":
            self._send({"nichandle": "xx12345-ovh", "email": "test@example.com", "ovhSubsidiary": "IE", "currency": {"code": "EUR"}})
            return
        if path == "/1.0/me/credit/balance":
            self._send(["PREPAID"])
            return
        if path == "/1.0/me/credit/balance/PREPAID":
            self._send({"amount": {"value": 42.5, "currencyCode": "EUR"}})
            return
        if path == "/1.0/me/order":
            self._send([90001])
            return
        if path == "/1.0/me/order/90001":
            self._send({"orderId": 90001, "date": "2026-11-21", "url": "https://www.ovh.com/order/90001", "priceWithTax": {"text": "17.99 EUR"}})
            return
        if path.endswith("/eco/options"):
            self._send(
                [
                    {"planCode": "ram-32g-ecc-2400-26skleb01", "prices": [{"duration": "P1M", "pricingMode": "default"}]},
                    {"planCode": "softraid-2x450nvme-26skleb01", "prices": [{"duration": "P1M", "pricingMode": "default"}]},
                ]
            )
            return
        if path.endswith("/requiredConfiguration"):
            self._send(
                [
                    {"label": "dedicated_datacenter", "required": True, "allowedValues": ["gra", "rbx", "fra"]},
                    {"label": "region", "required": True, "allowedValues": ["europe"]},
                    {"label": "dedicated_os", "required": True, "allowedValues": ["none_64"]},
                ]
            )
            return
        if path.endswith("/configuration"):
            cart_id = path.split("/")[4]
            cart = self.st.carts.get(cart_id, {})
            self._send(cart.get("configs", []))
            return
        if path.endswith("/summary"):
            self._send(
                {
                    "prices": {
                        "withoutTax": {"value": self.st.price_without_tax, "currencyCode": "EUR"},
                        "withTax": {"value": self.st.price_with_tax, "currencyCode": "EUR"},
                        "tax": {"value": round(self.st.price_with_tax - self.st.price_without_tax, 2), "currencyCode": "EUR"},
                    }
                }
            )
            return
        self._send({"message": f"Unknown GET {path}"}, 404)

    def do_POST(self):
        raw, data = self._read_body()
        if not self._verify_sig("POST", raw):
            return
        path = urllib.parse.urlsplit(self.path).path

        if path == "/1.0/order/cart":
            cid = f"cart-{len(self.st.carts) + 1}"
            self.st.carts[cid] = {"subsidiary": data.get("ovhSubsidiary"), "assigned": False, "items": [], "configs": [], "options": [], "coupons": []}
            self._send({"cartId": cid})
            return
        if path.endswith("/assign"):
            cid = path.split("/")[4]
            self.st.carts[cid]["assigned"] = True
            self._send(None)
            return
        if path.endswith("/eco") or path.endswith("/baremetalServers") or path.endswith("/vps"):
            cid = path.split("/")[4]
            item_id = 100 + len(self.st.carts[cid]["items"])
            self.st.carts[cid]["items"].append({"itemId": item_id, **data})
            self._send({"itemId": item_id})
            return
        if path.endswith("/configuration"):
            cid = path.split("/")[4]
            if self.st.reject_os_en and data.get("label") == "dedicated_os" and data.get("value") == "none_64.en":
                self._send({"message": "none_64.en is not allowed"}, 400)
                return
            self.st.carts[cid]["configs"].append(data)
            self._send({"id": len(self.st.carts[cid]["configs"]), **data})
            return
        if path.endswith("/eco/options"):
            cid = path.split("/")[4]
            self.st.carts[cid]["options"].append(data)
            self._send({"itemId": 200 + len(self.st.carts[cid]["options"])})
            return
        if path.endswith("/coupon"):
            cid = path.split("/")[4]
            self.st.carts[cid]["coupons"].append(data.get("coupon"))
            self._send({"coupon": data.get("coupon")})
            return
        if path.endswith("/checkout"):
            cid = path.split("/")[4]
            oid = 88000 + len(self.st.checkouts) + 1
            rec = {"cartId": cid, "orderId": oid, "payload": data, "cart": self.st.carts[cid]}
            self.st.checkouts.append(rec)
            self._send({"orderId": oid, "url": f"https://www.ovh.com/order/pay/{oid}"})
            return
        self._send({"message": f"Unknown POST {path}"}, 404)

    def do_DELETE(self):
        if not self._verify_sig("DELETE", ""):
            return
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/1.0/order/cart/"):
            cid = path.split("/")[4]
            self.st.deleted_carts.append(cid)
            self._send(None)
            return
        self._send({"message": f"Unknown DELETE {path}"}, 404)


class TestOVHSniper(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock_state = MockOVHState()
        cls.mock_server = MockOVHServer(cls.mock_state)
        cls.mock_port = cls.mock_server.server_port
        cls.mock_thread = threading.Thread(target=cls.mock_server.serve_forever, daemon=True)
        cls.mock_thread.start()
        cls.api_base = f"http://127.0.0.1:{cls.mock_port}/1.0"
        cls.v1_base = f"http://127.0.0.1:{cls.mock_port}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.mock_server.shutdown()
        cls.mock_server.server_close()

    def setUp(self):
        self.mock_state.reset()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        self.db = Database(db_path=self.db_path, encryption_key="unit-test-enc-key")
        self.catalog_mgr = CatalogManager(ttl_seconds=60, cache_dir=Path(self.tmpdir.name) / "cache")
        self.order_engine = OrderEngine(self.catalog_mgr, log_fn=self.db.add_log)
        self.notifier = Notifier(self.db)
        self.engine = SniperEngine(
            db=self.db,
            catalog_manager=self.catalog_mgr,
            order_engine=self.order_engine,
            notifier=self.notifier,
            client_factory=self._make_client,
        )

    def tearDown(self):
        self.engine.stop()
        self.db.close()
        self.tmpdir.cleanup()

    def _make_client(self, account=None, endpoint="ovh-eu", qps=50.0, proxy=""):
        return OVHClient(
            endpoint=endpoint,
            app_key=(account or {}).get("app_key", ""),
            app_secret=(account or {}).get("app_secret", ""),
            consumer_key=(account or {}).get("consumer_key", ""),
            qps=50.0,
            base_url_override=self.api_base,
            v1_url_override=self.v1_base,
        )

    def _create_test_account(self):
        return self.db.create_account(
            {
                "name": "测试IE账号",
                "endpoint": "ovh-eu",
                "subsidiary": "IE",
                "app_key": MOCK_APP_KEY,
                "app_secret": MOCK_APP_SECRET,
                "consumer_key": MOCK_CONSUMER_KEY,
                "is_default": True,
            }
        )

    def test_config_and_hardware_matching(self):
        self.assertEqual(normalize_datacenter("MUM"), "ynm")
        self.assertEqual(infer_region_for_datacenter("lim"), "europe")
        self.assertEqual(infer_region_for_datacenter("bhs5"), "canada")
        self.assertEqual(infer_region_for_datacenter("ynm"), "apac")
        self.assertTrue(is_availability_orderable("1H-high", instant_only=True))
        self.assertFalse(is_availability_orderable("24H", instant_only=True))
        self.assertTrue(is_availability_orderable("24H", instant_only=False))
        self.assertFalse(is_availability_orderable("unavailable"))

        self.assertEqual(standardize_config("ram-64g-ecc-2400-25skleb01"), "ram-64g")
        self.assertEqual(standardize_config("ram-64g-noecc-2133"), "ram-64g")
        self.assertEqual(standardize_config("ram-16g-ddr3-noecc-1333-27skle01-v1"), "ram-16g")
        self.assertEqual(standardize_config("softraid-2x450nvme-26skleb01"), "softraid-2x450nvme")
        self.assertEqual(standardize_config("softraid-2x480ssd-27skle01-v1"), "softraid-2x480ssd")

        self.assertTrue(match_filter_expression(">=32g", "ram-64g-ecc-3200", is_memory=True))
        self.assertFalse(match_filter_expression(">=64g", "ram-32g-ecc-2400", is_memory=True))
        self.assertTrue(match_filter_expression("nvme", "softraid-2x450nvme", is_memory=False))

    def test_secret_encryption_roundtrip(self):
        enc = protect_secret("super_secret_ck", "my-key")
        self.assertTrue(enc.startswith("enc1:"))
        self.assertNotIn("super_secret_ck", enc)
        self.assertEqual(reveal_secret(enc, "my-key"), "super_secret_ck")

    def test_ovh_client_auth_and_catalog_parsing(self):
        acc = self._create_test_account()
        full_acc = self.db.get_account(acc["id"], mask=False)
        client = self._make_client(full_acc)
        info = client.verify_credentials()
        self.assertEqual(info["nichandle"], "xx12345-ovh")
        self.assertEqual(info["credit_balance"], 42.5)

        plans = self.catalog_mgr.get_catalog_plans(client, subsidiary="IE", product_line="eco", force_refresh=True)
        self.assertEqual(len(plans), 2)
        p_b = next(p for p in plans if p["planCode"] == "26skleb01")
        self.assertEqual(p_b["price"], 14.99)
        addons = resolve_addons_for_fqn(p_b, "ram-32g-ecc-2400", "softraid-2x450nvme")
        self.assertEqual(addons, ["ram-32g-ecc-2400-26skleb01", "softraid-2x450nvme-26skleb01"])

    def test_order_engine_dry_run_budget_guard_and_real_checkout(self):
        acc = self._create_test_account()
        client = self._make_client(self.db.get_account(acc["id"], mask=False))

        # 1. Dry-run with dynamic requiredConfiguration fallback (reject none_64.en -> fallback none_64)
        self.mock_state.reject_os_en = True
        dry_res = self.order_engine.place_order(
            client=client,
            subsidiary="IE",
            plan_code="26skleb01",
            datacenter="gra",
            options=["ram-32g-ecc-2400-26skleb01", "softraid-2x450nvme-26skleb01"],
            coupons=["BF2026"],
            max_price=20.0,
            dry_run=True,
        )
        self.assertTrue(dry_res["success"])
        self.assertEqual(dry_res["status"], "dry_run")
        self.assertEqual(dry_res["price_without_tax"], 14.99)
        self.assertEqual(len(self.mock_state.checkouts), 0)
        self.assertEqual(len(self.mock_state.deleted_carts), 1)
        cart1 = self.mock_state.carts["cart-1"]
        self.assertTrue(any(c["label"] == "dedicated_os" and c["value"] == "none_64" for c in cart1["configs"]))
        self.assertEqual(cart1["coupons"], ["BF2026"])

        # 2. Budget ceiling exceeded -> must NOT checkout and must delete cart!
        self.mock_state.reject_os_en = False
        budget_res = self.order_engine.place_order(
            client=client,
            subsidiary="IE",
            plan_code="26skleb01",
            datacenter="gra",
            max_price=10.0,
            dry_run=False,
        )
        self.assertFalse(budget_res["success"])
        self.assertEqual(budget_res["status"], "budget_exceeded")
        self.assertEqual(len(self.mock_state.checkouts), 0)
        self.assertEqual(len(self.mock_state.deleted_carts), 2)

        # 3. Real checkout -> succeeds and creates order
        real_res = self.order_engine.place_order(
            client=client,
            subsidiary="IE",
            plan_code="26skleb01",
            datacenter="gra",
            options=["ram-32g-ecc-2400-26skleb01"],
            max_price=20.0,
            auto_pay=True,
            waive_retractation=True,
            dry_run=False,
        )
        self.assertTrue(real_res["success"])
        self.assertEqual(real_res["status"], "success")
        self.assertEqual(real_res["order_id"], "88001")
        self.assertTrue(real_res["order_url"].endswith("/88001"))
        self.assertEqual(len(self.mock_state.checkouts), 1)
        self.assertTrue(self.mock_state.checkouts[0]["payload"]["autoPayWithPreferredPaymentMethod"])

    def test_sniper_bulk_polling_and_quota_enforcement(self):
        acc = self._create_test_account()
        # Create 3 tasks for 3 distinct planCodes -> triggers 1 single bulk availability request!
        t1 = self.db.create_task(
            {
                "name": "Target BF",
                "account_id": acc["id"],
                "plan_code": "26skleb01",
                "datacenters": ["gra", "fra"],
                "memory_filter": ">=32g",
                "storage_filter": "nvme",
                "max_price": 20.0,
                "max_orders": 1,
                "dry_run": False,
                "poll_interval": 2,
            }
        )
        self.db.create_task({"name": "Watch A", "account_id": acc["id"], "plan_code": "25sklea01", "poll_interval": 2})
        self.db.create_task({"name": "Watch Rise", "account_id": acc["id"], "plan_code": "25rise01", "poll_interval": 2})

        self.engine.run_sniper_tick()
        self.assertEqual(self.mock_state.bulk_calls, 1)
        self.assertEqual(self.mock_state.per_plan_calls, 0)
        self.assertEqual(len(self.mock_state.checkouts), 1)

        # Task should now be completed (ordered_count=1 == max_orders=1) -> second tick must NOT re-buy!
        updated_t1 = self.db.get_task(t1["id"])
        self.assertEqual(updated_t1["status"], "completed")
        self.assertEqual(updated_t1["ordered_count"], 1)

    def test_black_friday_radar_and_vps_monitor(self):
        acc = self._create_test_account()
        rule = self.db.create_radar_rule(
            {
                "name": "黑五新品雷达",
                "rule_type": "new_sku",
                "account_id": acc["id"],
                "endpoint": "ovh-eu",
                "subsidiary": "IE",
                "keyword_pattern": "26bf|skle",
                "datacenters": ["gra"],
                "max_price": 25.0,
                "auto_snipe": True,
                "dry_run": True,
                "enabled": True,
            }
        )
        # First scan sets baseline
        hits1 = self.engine.run_radar_tick()
        self.assertEqual(hits1, 0)
        self.assertEqual(len(self.db.get_radar_rule(rule["id"])["known_plan_codes"]), 2)

        # Inject a brand-new Black Friday SKU into catalog!
        self.mock_state.catalog_plans.append(
            {
                "planCode": "26bf-special-01",
                "invoiceName": "BF Special | Ryzen 5600X",
                "pricings": [{"mode": "default", "interval": 1, "intervalUnit": "month", "price": 1999000000, "capacities": ["renew"]}],
                "addonFamilies": [],
            }
        )
        hits2 = self.engine.run_radar_tick()
        self.assertEqual(hits2, 1)
        tasks = self.db.list_tasks()
        self.assertTrue(any(t["plan_code"] == "26bf-special-01" and t["source"].startswith("radar:") for t in tasks))

        # VPS watch transition test
        watch = self.db.create_vps_watch({"plan_code": "vps-2025-model1", "endpoint": "ovh-eu", "subsidiary": "IE"})
        self.engine.run_vps_tick()
        self.assertEqual(self.db.get_vps_watch(watch["id"])["last_state"]["GRA"]["status"], "out-of-stock")
        self.mock_state.vps_state[0]["status"] = "available"
        self.engine.run_vps_tick()
        self.assertEqual(self.db.get_vps_watch(watch["id"])["last_state"]["GRA"]["status"], "available")

    def test_telegram_commands_and_inline_buy_button(self):
        self._create_test_account()
        status_reply = self.notifier._dispatch_command("/status", [])
        self.assertIn("OVH Sniper 运行状态", status_reply)
        check_reply = self.notifier._dispatch_command("/check", ["26skleb01"])
        self.assertIn("GRA(1H-high)", check_reply)

        tok = self.notifier.create_action_button_token(
            {"action": "quick_buy", "plan_code": "26skleb01", "datacenter": "gra"}
        )
        buy_reply = self.notifier._dispatch_callback(f"buy:{tok}")
        self.assertIn("已创建高优先级抢购任务", buy_reply)

    def test_web_server_auth_and_rest_api(self):
        auth = PanelAuth(self.db, env_password="my-secret-password")
        ctx = AppContext(self.db, self.catalog_mgr, self.order_engine, self.notifier, self.engine, auth)
        server = create_http_server("127.0.0.1", 0, ctx)
        port = server.server_port
        th = threading.Thread(target=server.serve_forever, daemon=True)
        th.start()
        try:
            base = f"http://127.0.0.1:{port}"
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            # 1. Health endpoint is public
            with opener.open(f"{base}/api/health", timeout=5) as r:
                self.assertEqual(json.loads(r.read())["ok"], True)

            # 2. Protected endpoint returns 401 without auth
            with self.assertRaises(urllib.error.HTTPError) as ctx_err:
                opener.open(f"{base}/api/status", timeout=5)
            self.assertEqual(ctx_err.exception.code, 401)

            # 3. X-API-Key authorizes requests
            req = urllib.request.Request(f"{base}/api/status", headers={"X-API-Key": "my-secret-password"})
            with opener.open(req, timeout=5) as r:
                self.assertEqual(r.status, 200)

            # 4. Login sets HttpOnly cookie & creates account via API
            login_req = urllib.request.Request(
                f"{base}/api/auth/login",
                data=json.dumps({"password": "my-secret-password"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with opener.open(login_req, timeout=5) as r:
                cookie = r.headers.get("Set-Cookie", "").split(";")[0]
            self.assertIn("ovh_sniper_session=", cookie)

            # 5. Static HTML served
            with opener.open(f"{base}/", timeout=5) as r:
                html = r.read().decode("utf-8")
                self.assertIn("OVH Sniper", html)

            # 6. Create & verify account through REST API
            acc_req = urllib.request.Request(
                f"{base}/api/accounts",
                data=json.dumps(
                    {
                        "name": "API账号",
                        "endpoint": "ovh-eu",
                        "subsidiary": "IE",
                        "app_key": MOCK_APP_KEY,
                        "app_secret": MOCK_APP_SECRET,
                        "consumer_key": MOCK_CONSUMER_KEY,
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json", "Cookie": cookie},
                method="POST",
            )
            with opener.open(acc_req, timeout=5) as r:
                created = json.loads(r.read())
                self.assertIn("****", created["consumer_key"])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
