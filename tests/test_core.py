"""Comprehensive end-to-end & unit tests for OVH Sniper v2.0 (with real local mock OVH API)."""

import base64
import hashlib
import http.server
import json
import socketserver
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ovh_buyer.auth import PanelAuth
from ovh_buyer.catalog import (
    CatalogManager,
    intersect_common_terms,
    is_config_orderable,
    is_default_fqn_config,
    match_filter_expression,
    resolve_addons_for_fqn,
    resolve_cart_addons,
    standardize_config,
)
from ovh_buyer.config import (
    build_express_order_url,
    create_token_url,
    decode_short_order_url,
    infer_region_for_datacenter,
    is_availability_orderable,
    normalize_datacenter,
)
from ovh_buyer.database import Database, protect_secret, reveal_secret
from ovh_buyer.notifier import Notifier, render_telegram_stock_card
from ovh_buyer.order_engine import OrderEngine
from ovh_buyer.ovh_client import OVHClient
from ovh_buyer.sniper import SniperEngine
from ovh_buyer.specs import FXConverter, compare_specs, parse_actual_hardware, parse_official_spec
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
        self.me_order_calls = 0
        self.checkout_attempts = 0
        self.summary_broken = False
        self.reject_os_en = False
        self.checkout_error_code = 0
        self.price_without_tax = 14.99
        self.price_with_tax = 17.99
        self.order_statuses = {90001: "delivered", 88001: "delivered"}
        self.order_details_domain = {88001: "ns300001.ip-1-2-3.eu"}
        self.dedicated_servers = {
            "ns300001.ip-1-2-3.eu": {
                "info": {
                    "name": "ns300001.ip-1-2-3.eu",
                    "commercialRange": "KS-LE-B",
                    "datacenter": "gra3",
                    "ip": "1.2.3.4",
                    "state": "ok",
                    "os": "debian12_64",
                },
                "hardware": {
                    "processorName": "Intel Xeon-D 1540",
                    "coresPerProcessor": 8,
                    "threadsPerProcessor": 16,
                    "memorySize": {"value": 65536, "unit": "MB"},
                    "diskGroups": [
                        {
                            "numberOfDisks": 2,
                            "diskType": "NVMe",
                            "diskSize": {"value": 960, "unit": "GB"},
                        }
                    ],
                },
                "serviceInfos": {
                    "creation": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    "expiration": "2027-11-23",
                },
            }
        }
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
                    {"name": "memory", "mandatory": True, "default": "ram-16g-ecc-2133-25sklea01", "addons": ["ram-16g-ecc-2133-25sklea01"]},
                    {"name": "storage", "mandatory": True, "default": "softraid-2x4000sa-25sklea01", "addons": ["softraid-2x4000sa-25sklea01"]},
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
                        "mandatory": True,
                        "default": "ram-32g-ecc-2400-26skleb01",
                        "addons": ["ram-32g-ecc-2400-26skleb01"],
                    },
                    {
                        "name": "storage",
                        "mandatory": True,
                        "default": "softraid-2x450nvme-26skleb01",
                        "addons": ["softraid-2x450nvme-26skleb01"],
                    },
                    {
                        "name": "bandwidth",
                        "mandatory": True,
                        "default": "bandwidth-500-26skleb01",
                        "addons": ["bandwidth-500-26skleb01"],
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
        if path == "/1.0/me/payment/method":
            self._send([5501])
            return
        if path == "/1.0/me/payment/method/5501":
            self._send({"paymentMethodId": 5501, "paymentType": "PAYPAL", "default": True, "status": "VALID"})
            return
        if path == "/1.0/me/order":
            self.st.me_order_calls += 1
            self._send([90001])
            return
        if path.startswith("/1.0/me/order/") and path.endswith("/status"):
            oid = int(path.split("/")[4])
            self._send(self.st.order_statuses.get(oid, "notPaid"))
            return
        if path.startswith("/1.0/me/order/") and path.endswith("/details"):
            self._send([1])
            return
        if path.startswith("/1.0/me/order/") and "/details/" in path:
            oid = int(path.split("/")[4])
            dom = self.st.order_details_domain.get(oid, "*")
            self._send({"orderDetailId": 1, "domain": dom, "description": "KS-LE-B"})
            return
        if path == "/1.0/me/order/90001":
            self._send({"orderId": 90001, "date": "2026-11-23", "url": "https://www.ovh.com/order/90001", "priceWithTax": {"text": "17.99 EUR"}})
            return
        if path == "/1.0/dedicated/server":
            self._send(list(self.st.dedicated_servers.keys()))
            return
        if path.startswith("/1.0/dedicated/server/"):
            parts = path.split("/")
            srv_name = urllib.parse.unquote(parts[4])
            srv_obj = self.st.dedicated_servers.get(srv_name)
            if not srv_obj:
                self._send({"message": "Server not found"}, 404)
                return
            if len(parts) == 5:
                self._send(srv_obj["info"])
                return
            if path.endswith("/specifications/hardware"):
                self._send(srv_obj["hardware"])
                return
            if path.endswith("/serviceInfos"):
                self._send(srv_obj["serviceInfos"])
                return
        if path.startswith("/1.0/order/cart/") and len(path.split("/")) == 5:
            cid = path.split("/")[4]
            cart = self.st.carts.get(cid)
            if not cart:
                self._send({"message": "Cart not found"}, 404)
                return
            self._send({"cartId": cid, "expire": cart["expire"]})
            return
        if path.endswith("/eco/options"):
            self._send(
                [
                    {"planCode": "ram-32g-ecc-2400-26skleb01", "prices": [{"duration": "P1M", "pricingMode": "default"}]},
                    {"planCode": "softraid-2x450nvme-26skleb01", "prices": [{"duration": "P1M", "pricingMode": "default"}]},
                    {"planCode": "bandwidth-500-26skleb01", "prices": [{"duration": "P1M", "pricingMode": "default"}]},
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
        if path.endswith("/summary") or (path.endswith("/checkout") and self.st.summary_broken):
            if self.st.summary_broken:
                self._send({"message": "summary unavailable"}, 503)
                return
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
            exp = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            self.st.carts[cid] = {
                "subsidiary": data.get("ovhSubsidiary"),
                "expire": exp,
                "assigned": False,
                "items": [],
                "configs": [],
                "options": [],
                "coupons": [],
            }
            self._send({"cartId": cid, "expire": exp})
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
            self.st.checkout_attempts += 1
            if self.st.checkout_error_code:
                self._send({"message": "Gateway timeout / server error"}, self.st.checkout_error_code)
                return
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


def make_short_link(
    region: str,
    plan_code: str,
    dc: str,
    memory: str = "",
    storage: str = "",
    bandwidth: str = "",
    system_storage: str = "",
    vrack: str = "",
) -> str:
    raw = "\x1f".join([plan_code, dc, memory, storage, bandwidth, system_storage, vrack]).encode("utf-8")
    comp = zlib.compressobj(level=9, wbits=-zlib.MAX_WBITS)
    compressed = comp.compress(raw) + comp.flush()
    tok = base64.urlsafe_b64encode(compressed).decode("ascii").rstrip("=")
    return f"https://ovheco.example/o/{region}/{tok}"


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
        self.order_engine = OrderEngine(self.catalog_mgr, log_fn=self.db.add_log, uncertain_wait_seconds=0.0)
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
        self.assertEqual(infer_region_for_datacenter("unknown-dc", allowed=["europe"]), "europe")
        self.assertTrue(is_availability_orderable("1H-high", instant_only=True))
        self.assertFalse(is_availability_orderable("24H", instant_only=True))
        self.assertTrue(is_availability_orderable("24H", instant_only=False))
        self.assertFalse(is_availability_orderable("unavailable"))
        self.assertFalse(is_availability_orderable("comingsoon"))

        self.assertEqual(standardize_config("ram-64g-ecc-2400-25skleb01"), "ram-64g")
        self.assertEqual(standardize_config("ram-64g-noecc-2133"), "ram-64g")
        self.assertEqual(standardize_config("ram-16g-ddr3-noecc-1333-27skle01-v1"), "ram-16g")
        self.assertEqual(standardize_config("softraid-2x450nvme-26skleb01"), "softraid-2x450nvme")
        self.assertEqual(standardize_config("softraid-2x480ssd-27skle01-v1"), "softraid-2x480ssd")

        self.assertTrue(match_filter_expression(">=32g", "ram-64g-ecc-3200", is_memory=True))
        self.assertFalse(match_filter_expression(">=64g", "ram-32g-ecc-2400", is_memory=True))
        self.assertTrue(match_filter_expression("nvme", "softraid-2x450nvme", is_memory=False))

    def test_v2_token_url_express_url_and_short_link_codec(self):
        tok_eu = create_token_url("ovh-eu")
        self.assertIn("https://eu.api.ovh.com/createToken/?", tok_eu)
        self.assertIn("POST=/order/*", tok_eu)
        self.assertIn("GET=/dedicated/server/*", tok_eu)
        self.assertNotIn("DELETE=/*", tok_eu)

        exp_url = build_express_order_url(
            endpoint="ovh-eu",
            subsidiary="IE",
            plan_code="26skleb01",
            datacenter="gra",
            options=["ram-32g-ecc-2400-26skleb01"],
        )
        self.assertIn("order.eu.ovhcloud.com/en-ie/express/#/express/review?products=", exp_url)
        self.assertIn("26skleb01", exp_url)
        self.assertIn("dedicated_datacenter", exp_url)

        short_url = make_short_link(
            "e", "26skleb01", "gra", "ram-32g-ecc-2400-26skleb01", "softraid-2x450nvme-26skleb01"
        )
        decoded = decode_short_order_url(short_url)
        self.assertEqual(decoded["endpoint"], "ovh-eu")
        self.assertEqual(decoded["plan_code"], "26skleb01")
        self.assertEqual(decoded["datacenter"], "gra")
        self.assertIn("ram-32g-ecc-2400-26skleb01", decoded["options"])

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
        self.assertEqual(info["default_payment_type"], "PAYPAL")
        self.assertEqual(info["payment_methods_count"], 1)

        plans = self.catalog_mgr.get_catalog_plans(client, subsidiary="IE", product_line="eco", force_refresh=True)
        self.assertEqual(len(plans), 2)
        p_b = next(p for p in plans if p["planCode"] == "26skleb01")
        self.assertEqual(p_b["price"], 14.99)
        addons = resolve_addons_for_fqn(p_b, "ram-32g-ecc-2400", "softraid-2x450nvme")
        self.assertEqual(addons, ["ram-32g-ecc-2400-26skleb01", "softraid-2x450nvme-26skleb01"])

    def test_v2_phantom_stock_guard_and_mandatory_addons(self):
        acc = self._create_test_account()
        client = self._make_client(self.db.get_account(acc["id"], mask=False))
        plans = self.catalog_mgr.get_catalog_plans(client, subsidiary="IE", product_line="eco", force_refresh=True)
        p_b = next(p for p in plans if p["planCode"] == "26skleb01")

        # Valid catalog config
        ok, reason = is_config_orderable(p_b, "ram-32g-ecc-2400", "softraid-2x450nvme")
        self.assertTrue(ok)
        self.assertEqual(reason, "")
        self.assertTrue(is_default_fqn_config(p_b, "ram-32g-ecc-2400", "softraid-2x450nvme"))

        # Phantom stock: availability advertises 64G RAM while catalog only sells 32G!
        ok_ph, reason_ph = is_config_orderable(p_b, "ram-64g-ddr4-ecc-2666", "softraid-2x450nvme")
        self.assertFalse(ok_ph)
        self.assertIn("幽灵库存", reason_ph)
        self.assertIn("目录无此内存配件", reason_ph)

        # resolve_cart_addons must REFUSE silent downgrade to 32G
        with self.assertRaises(ValueError):
            resolve_cart_addons(p_b, fqn_memory="ram-64g-ddr4-ecc-2666", fqn_storage="softraid-2x450nvme")

        # Mandatory bandwidth addon is automatically included
        resolved = resolve_cart_addons(p_b, fqn_memory="ram-32g-ecc-2400", fqn_storage="softraid-2x450nvme")
        self.assertIn("bandwidth-500-26skleb01", resolved)

        # intersect_common_terms finds overlapping (pricingMode, duration) across plan & addons
        raw_cat = {
            "locale": {"currencyCode": "EUR"},
            "plans": [
                {
                    "planCode": "26skleb01",
                    "pricings": [
                        {"mode": "default", "interval": 1, "intervalUnit": "month", "price": 1499000000, "capacities": ["renew"]},
                        {"mode": "upfront12", "interval": 12, "intervalUnit": "month", "price": 15000000000, "capacities": ["renew"]},
                    ],
                }
            ],
            "addons": [
                {
                    "planCode": "ram-32g-ecc-2400-26skleb01",
                    "pricings": [
                        {"mode": "upfront12", "interval": 12, "intervalUnit": "month", "price": 0, "capacities": ["renew"]},
                    ],
                }
            ],
        }
        common = intersect_common_terms(raw_cat, "26skleb01", ["ram-32g-ecc-2400-26skleb01"])
        self.assertEqual(len(common), 1)
        self.assertEqual(common[0]["pricingMode"], "upfront12")
        self.assertEqual(common[0]["duration"], "P12M")

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

    def test_v2_fast_lane_prebuilt_cart_and_1call_checkout(self):
        acc = self._create_test_account()
        task = self.db.create_task(
            {
                "name": "FastLane KS-LE-B",
                "account_id": acc["id"],
                "plan_code": "26skleb01",
                "datacenters": ["gra"],
                "max_price": 20.0,
                "max_orders": 1,
                "dry_run": False,
                "fast_lane": True,
                "poll_interval": 2,
            }
        )
        built = self.engine.run_fastlane_maintenance()
        self.assertEqual(built, 1)
        slots = self.engine.slot_pool.get_task_slots_summary(task["id"])
        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0]["dc"], "gra")
        carts_before_hit = len(self.mock_state.carts)

        # Stock hits -> should consume prebuilt cart with 0 new carts created!
        self.engine.run_sniper_tick()
        self.assertEqual(len(self.mock_state.carts), carts_before_hit)
        self.assertEqual(len(self.mock_state.checkouts), 1)
        self.assertEqual(self.db.get_task(task["id"])["status"], "completed")
        self.assertIn("⚡", self.db.get_task(task["id"])["last_status_msg"])

    def test_v2_checkout_uncertain_500_pending_confirm_no_double_order(self):
        acc = self._create_test_account()
        self.mock_state.checkout_error_code = 500
        task = self.db.create_task(
            {
                "name": "Uncertain Guard Test",
                "account_id": acc["id"],
                "plan_code": "26skleb01",
                "datacenters": ["gra"],
                "max_price": 20.0,
                "max_orders": 1,
                "dry_run": False,
                "poll_interval": 2,
            }
        )
        self.engine.run_sniper_tick()
        # Task must be halted to pending_confirm, cart NOT deleted, /me/order checked!
        t_after = self.db.get_task(task["id"])
        self.assertEqual(t_after["status"], "pending_confirm")
        self.assertEqual(len(self.mock_state.deleted_carts), 0)
        self.assertGreaterEqual(self.mock_state.me_order_calls, 1)

        orders = self.db.list_orders()
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["status"], "pending_confirm")

        # Subsequent tick must NOT re-attempt checkout while pending_confirm!
        carts_before = len(self.mock_state.carts)
        self.engine.run_sniper_tick()
        self.assertEqual(len(self.mock_state.carts), carts_before)
        # The money-moving POST /checkout must have been sent exactly ONCE (no transport/5xx retries)
        self.assertEqual(self.mock_state.checkout_attempts, 1)
        # Uncertain checkout is never auto-linked to an arbitrary recent account order
        self.assertEqual(orders[0]["order_id"], "")

    def test_review_unknown_price_with_ceiling_never_checks_out(self):
        acc = self._create_test_account()
        client = self._make_client(self.db.get_account(acc["id"], mask=False))
        self.mock_state.summary_broken = True
        res = self.order_engine.place_order(
            client=client,
            subsidiary="IE",
            plan_code="26skleb01",
            datacenter="gra",
            max_price=20.0,
            dry_run=False,
        )
        self.assertFalse(res["success"])
        self.assertEqual(res["status"], "budget_exceeded")
        self.assertEqual(self.mock_state.checkout_attempts, 0)
        self.assertEqual(len(self.mock_state.deleted_carts), 1)

    def test_review_export_masks_secrets_and_ignores_client_ids(self):
        self.db.update_settings({"telegram_bot_token": "123456:SECRET-TOKEN-VALUE", "dingtalk_secret": "SECabcdef123456"})
        exported = self.db.export_config(include_secrets=False)
        self.assertNotIn("SECRET-TOKEN-VALUE", json.dumps(exported, ensure_ascii=False))
        self.assertNotIn("SECabcdef123456", json.dumps(exported, ensure_ascii=False))
        self.assertNotIn("panel_password_hash", exported["settings"])
        t = self.db.create_task({"id": "x');alert(1)//", "plan_code": "26skleb01"})
        self.assertNotIn("'", t["id"])

    def test_review_deleted_account_never_falls_back_to_default(self):
        acc = self._create_test_account()
        task = self.db.create_task(
            {"name": "Orphan", "account_id": "deleted-acc", "plan_code": "26skleb01", "datacenters": ["gra"], "max_price": 20.0}
        )
        self.assertIsNone(self.engine.get_account_for_id("deleted-acc"))
        self.assertEqual(self.engine.get_account_for_id("")["id"], acc["id"])
        self.engine.run_sniper_tick()
        self.assertEqual(self.mock_state.checkout_attempts, 0)
        self.assertIn("账号", self.db.get_task(task["id"])["last_status_msg"])

    def test_v2_order_delivery_tracker_and_hardware_lottery(self):
        acc = self._create_test_account()
        # Order officially bought 32G RAM + 2x450G NVMe, while mock server delivers 64G RAM + 2x960G NVMe!
        order = self.db.add_order(
            {
                "account_id": acc["id"],
                "account_name": acc["name"],
                "product_line": "eco",
                "plan_code": "26skleb01",
                "datacenter": "gra",
                "memory": "ram-32g-ecc-2400",
                "storage": "softraid-2x450nvme",
                "status": "success",
                "dry_run": False,
                "order_id": "88001",
                "order_url": "https://www.ovh.com/order/pay/88001",
                "price_without_tax": 14.99,
                "price_with_tax": 17.99,
                "currency": "EUR",
            }
        )
        res = self.engine.tracker.check_order(order["id"])
        self.assertEqual(res.get("track_state"), "delivered")
        self.assertEqual(res.get("server_name"), "ns300001.ip-1-2-3.eu")
        self.assertTrue(res.get("lottery"))

        updated = self.db.get_order(order["id"])
        self.assertEqual(updated["ovh_status"], "delivered")
        self.assertEqual(updated["server_ip"], "1.2.3.4")
        self.assertTrue(updated["spec"]["lottery"])
        self.assertTrue(any("内存免费升级" in u for u in updated["spec"]["upgrades"]))
        self.assertTrue(any("硬盘容量升级" in u for u in updated["spec"]["upgrades"]))

        # FX offline fallback formatting
        fx = FXConverter()
        fx._rates = {"EUR": 7.85, "CNY": 1.0}
        fx._fetched_at = time.time()
        self.assertIn("≈ ¥141", fx.format_price_with_cny(17.99, "EUR"))

    def test_sniper_bulk_polling_and_quota_enforcement(self):
        acc = self._create_test_account()
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
        hits1 = self.engine.run_radar_tick()
        self.assertEqual(hits1, 0)
        self.assertEqual(len(self.db.get_radar_rule(rule["id"])["known_plan_codes"]), 2)

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

        watch = self.db.create_vps_watch({"plan_code": "vps-2025-model1", "endpoint": "ovh-eu", "subsidiary": "IE"})
        self.engine.run_vps_tick()
        self.assertEqual(self.db.get_vps_watch(watch["id"])["last_state"]["GRA"]["status"], "out-of-stock")
        self.mock_state.vps_state[0]["status"] = "available"
        self.engine.run_vps_tick()
        self.assertEqual(self.db.get_vps_watch(watch["id"])["last_state"]["GRA"]["status"], "available")

    def test_v2_telegram_html_card_commands_and_short_order(self):
        self._create_test_account()
        card_active, _ = render_telegram_stock_card(
            plan_code="26skleb01",
            available_dcs=[{"dc": "gra", "status": "1H-high"}],
            cpu="Intel Xeon-D 1540",
            memory="ram-32g-ecc-2400",
            storage="softraid-2x450nvme",
            price_text="14.99 EUR (≈ ¥118)",
        )
        self.assertIn("🔥", card_active)
        self.assertNotIn("<s>", card_active)

        card_sold, _ = render_telegram_stock_card(
            plan_code="26skleb01",
            available_dcs=[{"dc": "gra", "status": "1H-high"}],
            cpu="Intel Xeon-D 1540",
            memory="ram-32g-ecc-2400",
            storage="softraid-2x450nvme",
            price_text="14.99 EUR (≈ ¥118)",
            sold_out_at="2026-11-23 19:30:00",
        )
        self.assertIn("已被抢完", card_sold)
        self.assertIn("<s>", card_sold)

        status_reply = self.notifier._dispatch_command("/status", [])
        self.assertIn("OVH Sniper v2.1 运行状态", status_reply)
        check_reply = self.notifier._dispatch_command("/check", ["26skleb01"])
        self.assertIn("GRA(1H-high)", check_reply)
        srv_reply = self.notifier._dispatch_command("/servers", [])
        self.assertIn("ns300001.ip-1-2-3.eu", srv_reply)

        # Short-link /order preview + atomic confirm + duplicate-click block
        short_url = make_short_link("e", "26skleb01", "gra", "ram-32g-ecc-2400-26skleb01", "softraid-2x450nvme-26skleb01")
        preview_reply = self.notifier._dispatch_command("/order", [short_url])
        self.assertIsInstance(preview_reply, dict)
        text, markup = preview_reply["text"], preview_reply["reply_markup"]
        self.assertIn("短链购物车报价预览", text)
        cb_data = markup["inline_keyboard"][0][0]["callback_data"]
        self.assertTrue(cb_data.startswith("sc:"))
        confirm_msg = self.notifier._dispatch_callback(cb_data)
        self.assertIn("订单生成成功", confirm_msg)
        self.assertIn("#88001", confirm_msg)
        # Re-clicking same button must NOT double-order!
        dup_msg = self.notifier._dispatch_callback(cb_data)
        self.assertIn("禁止重复提交", dup_msg)
        self.assertEqual(len(self.mock_state.checkouts), 1)

        tok = self.notifier.create_action_button_token(
            {"action": "quick_buy", "plan_code": "26skleb01", "datacenter": "gra", "price_val": 14.99}
        )
        buy_reply = self.notifier._dispatch_callback(f"buy:{tok}")
        self.assertIn("已创建高优先级抢购任务", buy_reply)
        # Button is single-use: a second tap must NOT create another real-buy task
        again = self.notifier._dispatch_callback(f"buy:{tok}")
        self.assertIn("已使用或已过期", again)
        tg_tasks = [t for t in self.db.list_tasks() if t["source"] == "telegram"]
        self.assertEqual(len(tg_tasks), 1)
        self.assertAlmostEqual(tg_tasks[0]["max_price"], 16.49, places=2)

        # Without a known price the one-tap buy is refused (no uncapped real-buy tasks)
        tok2 = self.notifier.create_action_button_token({"action": "quick_buy", "plan_code": "26skleb01", "datacenter": "gra"})
        self.assertIn("拒绝一键抢购", self.notifier._dispatch_callback(f"buy:{tok2}"))

    def test_v2_sqlite_v1_to_v2_auto_migration(self):
        v1_path = Path(self.tmpdir.name) / "legacy_v1.db"
        conn = sqlite3.connect(str(v1_path))
        conn.executescript(
            """
            CREATE TABLE accounts (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, endpoint TEXT NOT NULL,
                subsidiary TEXT NOT NULL, app_key TEXT NOT NULL, app_secret TEXT NOT NULL,
                consumer_key TEXT NOT NULL, nichandle TEXT DEFAULT '', currency TEXT DEFAULT 'EUR',
                is_default INTEGER DEFAULT 0, last_verified_at TEXT DEFAULT '', created_at TEXT NOT NULL
            );
            CREATE TABLE sniper_tasks (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, account_id TEXT DEFAULT '',
                product_line TEXT NOT NULL DEFAULT 'eco', plan_code TEXT NOT NULL,
                datacenters TEXT NOT NULL DEFAULT '[]', memory_filter TEXT DEFAULT '',
                storage_filter TEXT DEFAULT '', options TEXT NOT NULL DEFAULT '[]',
                duration TEXT NOT NULL DEFAULT 'auto', pricing_mode TEXT NOT NULL DEFAULT 'auto',
                os_code TEXT NOT NULL DEFAULT 'none_64.en', coupons TEXT NOT NULL DEFAULT '[]',
                quantity INTEGER NOT NULL DEFAULT 1, max_price REAL NOT NULL DEFAULT 0,
                price_includes_tax INTEGER NOT NULL DEFAULT 0, mode TEXT NOT NULL DEFAULT 'buy',
                dry_run INTEGER NOT NULL DEFAULT 0, auto_pay INTEGER NOT NULL DEFAULT 0,
                waive_retractation INTEGER NOT NULL DEFAULT 1, only_instant INTEGER NOT NULL DEFAULT 0,
                max_orders INTEGER NOT NULL DEFAULT 1, ordered_count INTEGER NOT NULL DEFAULT 0,
                poll_interval INTEGER NOT NULL DEFAULT 5, status TEXT NOT NULL DEFAULT 'running',
                source TEXT NOT NULL DEFAULT 'manual', attempts INTEGER NOT NULL DEFAULT 0,
                last_check_at TEXT DEFAULT '', last_status_msg TEXT DEFAULT '',
                last_availability TEXT DEFAULT '{}',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE orders (
                id TEXT PRIMARY KEY, task_id TEXT DEFAULT '', account_id TEXT DEFAULT '',
                account_name TEXT DEFAULT '', product_line TEXT NOT NULL, plan_code TEXT NOT NULL,
                datacenter TEXT NOT NULL, options TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL, dry_run INTEGER NOT NULL DEFAULT 0,
                order_id TEXT DEFAULT '', order_url TEXT DEFAULT '',
                price_without_tax REAL, price_with_tax REAL, currency TEXT DEFAULT '',
                error_message TEXT DEFAULT '', created_at TEXT NOT NULL
            );
            INSERT INTO accounts (id, name, endpoint, subsidiary, app_key, app_secret, consumer_key, created_at)
            VALUES ('acc1', 'OldAcc', 'ovh-eu', 'IE', 'ak', 'as', 'ck', '2026-11-20 00:00:00');
            INSERT INTO sniper_tasks (id, name, account_id, plan_code, created_at, updated_at)
            VALUES ('task1', 'OldTask', 'acc1', '25skleb01', '2026-11-20 00:00:00', '2026-11-20 00:00:00');
            INSERT INTO orders (id, task_id, account_id, product_line, plan_code, datacenter, status, order_id, created_at)
            VALUES ('ord1', 'task1', 'acc1', 'eco', '25skleb01', 'gra', 'success', '77001', '2026-11-20 00:00:00');
            """
        )
        conn.commit()
        conn.close()

        migrated_db = Database(db_path=v1_path, encryption_key="test-key")
        try:
            acc = migrated_db.get_account("acc1")
            self.assertEqual(acc["name"], "OldAcc")
            self.assertEqual(acc["default_payment_type"], "")

            task = migrated_db.get_task("task1")
            self.assertEqual(task["plan_code"], "25skleb01")
            self.assertFalse(task["fast_lane"])
            self.assertTrue(task["allow_non_default"])

            order = migrated_db.get_order("ord1")
            self.assertEqual(order["order_id"], "77001")
            self.assertEqual(order["track_state"], "")
            self.assertEqual(order["server_name"], "")
            # Newly added orders get active tracking state
            new_order = migrated_db.add_order(
                {
                    "account_id": "acc1",
                    "product_line": "eco",
                    "plan_code": "26skleb01",
                    "datacenter": "gra",
                    "status": "success",
                    "order_id": "77002",
                }
            )
            self.assertEqual(new_order["track_state"], "tracking")
        finally:
            migrated_db.close()

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
            # 1. Health endpoint is public and reports v2.0.0
            with opener.open(f"{base}/api/health", timeout=5) as r:
                health = json.loads(r.read())
                self.assertEqual(health["ok"], True)
                self.assertEqual(health["version"], "2.1.0")

            # 2. Protected endpoint returns 401 without auth
            with self.assertRaises(urllib.error.HTTPError) as ctx_err:
                opener.open(f"{base}/api/status", timeout=5)
            self.assertEqual(ctx_err.exception.code, 401)

            # 3. X-API-Key authorizes requests
            req = urllib.request.Request(f"{base}/api/status", headers={"X-API-Key": "my-secret-password"})
            with opener.open(req, timeout=5) as r:
                status_data = json.loads(r.read())
                self.assertIn("fast_lane_enabled", status_data)
                self.assertIn("tracker_enabled", status_data)

            # 4. Login sets HttpOnly cookie
            login_req = urllib.request.Request(
                f"{base}/api/auth/login",
                data=json.dumps({"password": "my-secret-password"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with opener.open(login_req, timeout=5) as r:
                cookie = r.headers.get("Set-Cookie", "").split(";")[0]
            self.assertIn("ovh_sniper_session=", cookie)

            # 5. Static HTML served with v2.0 UI
            with opener.open(f"{base}/", timeout=5) as r:
                html = r.read().decode("utf-8")
                self.assertIn("OVH Sniper", html)
                self.assertIn("short-link-form", html)

            # 6. Meta has least-privilege token URL & FX endpoint responds
            meta_req = urllib.request.Request(f"{base}/api/meta", headers={"Cookie": cookie})
            with opener.open(meta_req, timeout=5) as r:
                meta = json.loads(r.read())
                self.assertIn("POST=/order/*", meta["endpoints"]["ovh-eu"]["token_url"])

            fx_req = urllib.request.Request(f"{base}/api/fx", headers={"Cookie": cookie})
            with opener.open(fx_req, timeout=5) as r:
                fx_data = json.loads(r.read())
                self.assertIn("EUR", fx_data["rates"])

            # 7. Create, test (default_payment_type), fetch servers, and short-link preview/confirm through REST API
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
                acc_id = created["id"]

            test_req = urllib.request.Request(
                f"{base}/api/accounts/{acc_id}/test",
                data=b"{}",
                headers={"Content-Type": "application/json", "Cookie": cookie},
                method="POST",
            )
            with opener.open(test_req, timeout=5) as r:
                test_res = json.loads(r.read())
                self.assertEqual(test_res["info"]["default_payment_type"], "PAYPAL")

            srv_req = urllib.request.Request(f"{base}/api/accounts/{acc_id}/servers", headers={"Cookie": cookie})
            with opener.open(srv_req, timeout=5) as r:
                srvs = json.loads(r.read())["items"]
                self.assertEqual(len(srvs), 1)
                self.assertEqual(srvs[0]["name"], "ns300001.ip-1-2-3.eu")

            short_url = make_short_link("e", "26skleb01", "gra", "ram-32g-ecc-2400-26skleb01", "softraid-2x450nvme-26skleb01")
            prev_req = urllib.request.Request(
                f"{base}/api/short-link/preview",
                data=json.dumps({"url": short_url, "account_id": acc_id}).encode("utf-8"),
                headers={"Content-Type": "application/json", "Cookie": cookie},
                method="POST",
            )
            with opener.open(prev_req, timeout=5) as r:
                preview = json.loads(r.read())["preview"]
                self.assertEqual(preview["plan_code"], "26skleb01")
                tok = preview["token"]

            conf_req = urllib.request.Request(
                f"{base}/api/short-link/confirm",
                data=json.dumps({"token": tok, "auto_pay": False}).encode("utf-8"),
                headers={"Content-Type": "application/json", "Cookie": cookie},
                method="POST",
            )
            with opener.open(conf_req, timeout=5) as r:
                conf = json.loads(r.read())
                self.assertTrue(conf["success"])
                self.assertEqual(conf["order_id"], "88001")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
