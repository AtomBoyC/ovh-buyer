"""Multi-channel Notification Hub + Zero-Domain Telegram Long Polling Bot v2.0.

Upgrades in v2.0:
- HTML Telegram Restock Cards with CNY price estimate + OVH Express direct checkout button
- Automatic editMessageText strikethrough ("已被抢完") when tracked stock sells out
- /order <https://.../o/e/...> command for @ovheco / PoorVPS short order links (confirm-before-checkout)
- /servers command to inspect delivered dedicated servers
"""

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import html as html_lib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import (
    build_express_order_url,
    format_availability_label,
    format_datacenter_label,
)
from .database import Database
from .specs import GLOBAL_FX


BEIJING_TZ = timezone(timedelta(hours=8))


def _now_hms() -> str:
    return datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _http_post_json(
    url: str,
    payload: Dict[str, Any],
    proxy_url: str = "",
    timeout: float = 12.0,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req_headers = {"Content-Type": "application/json; charset=utf-8", "User-Agent": "ovh-buyer/2.0"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=body, headers=req_headers, method="POST")
    if proxy_url:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    else:
        opener = urllib.request.build_opener()
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"ok": 200 <= resp.status < 300, "raw": raw}


def _tg_post_api(
    url: str,
    payload: Dict[str, Any],
    proxy_url: str = "",
    timeout: float = 12.0,
) -> Dict[str, Any]:
    """POST to Telegram Bot API, preserving Telegram's JSON error body on HTTP 4xx/5xx."""
    try:
        return _http_post_json(url, payload, proxy_url=proxy_url, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {"ok": False, "error_code": exc.code, "description": raw or str(exc)}


_TG_PRIVATE_TOPIC_URL_RE = re.compile(r"^(?:https?://)?t\.me/c/(\d+)(?:/(\d+))?(?:/\d+)?/?$", re.IGNORECASE)
_TG_PUBLIC_TOPIC_URL_RE = re.compile(r"^(?:https?://)?t\.me/([A-Za-z0-9_]{4,})(?:/(\d+))?(?:/\d+)?/?$", re.IGNORECASE)
_TG_INLINE_TOPIC_RE = re.compile(r"^(@[A-Za-z0-9_]+|-?\d+)\s*[/:_#]\s*(\d+)$")


def _parse_positive_int(val: Any) -> Optional[int]:
    raw = str(val or "").strip()
    if not raw:
        return None
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return None


def parse_telegram_target(raw_target: str, default_topic_id: Optional[int] = None) -> Optional[Tuple[str, Optional[int]]]:
    """Parse a Telegram target entry into (chat_id, message_thread_id).

    Supported formats:
    - '-1001234567890/45', '-1001234567890:45', '-1001234567890#45', '@group/45'
    - 'https://t.me/c/1234567890/45' (or message link '.../45/999') -> ('-1001234567890', 45)
    - 'https://t.me/mygroup/45' -> ('@mygroup', 45)
    - '-1001234567890' or '@mygroup' -> applies default_topic_id if provided
    - '123456789' (positive personal User ID) -> ('123456789', None)
    """
    s = str(raw_target or "").strip()
    if not s:
        return None

    m_priv = _TG_PRIVATE_TOPIC_URL_RE.match(s)
    if m_priv:
        cid = f"-100{m_priv.group(1)}"
        tid = int(m_priv.group(2)) if m_priv.group(2) else default_topic_id
        return cid, tid

    m_pub = _TG_PUBLIC_TOPIC_URL_RE.match(s)
    if m_pub and m_pub.group(1).lower() != "c":
        cid = f"@{m_pub.group(1)}"
        tid = int(m_pub.group(2)) if m_pub.group(2) else default_topic_id
        return cid, tid

    m_inline = _TG_INLINE_TOPIC_RE.match(s)
    if m_inline:
        return m_inline.group(1), int(m_inline.group(2))

    # Plain chat_id: only apply global default_topic_id to groups/supergroups/channels (-... or @...),
    # never to positive personal user DM IDs (which would fail with 'message thread not found').
    if s.startswith("-") or s.startswith("@"):
        return s, default_topic_id
    return s, None


def _http_get_json(url: str, proxy_url: str = "", timeout: float = 35.0) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "ovh-buyer/2.0"}, method="GET")
    if proxy_url:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    else:
        opener = urllib.request.build_opener()
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"ok": 200 <= resp.status < 300, "raw": raw}


def render_telegram_stock_card(
    plan_code: str,
    available_dcs: List[Dict[str, str]],
    server_name: str = "",
    cpu: str = "",
    memory: str = "",
    storage: str = "",
    price_text: str = "",
    setup_text: str = "",
    detected_at: str = "",
    sold_out_at: Optional[str] = None,
) -> Tuple[str, str]:
    """Render (html_text, plain_text) for Telegram restock card, with strikethrough when sold out."""
    esc = lambda s: html_lib.escape(str(s or ""), quote=False)
    header = f"🔥 OVH 补货提醒: {server_name or plan_code}"
    rows: List[Tuple[str, str]] = [("型号", plan_code)]
    if cpu and cpu != "N/A":
        rows.append(("CPU", cpu))
    if memory or storage:
        rows.append(("配置", f"{memory or '默认'} · {storage or '默认'}"))
    if price_text:
        rows.append(("参考价", price_text))
    if setup_text:
        rows.append(("安装费", setup_text))

    dc_lines = [
        f"  • {format_datacenter_label(item['dc'])} [{format_availability_label(item.get('status', 'available'))}]"
        for item in available_dcs[:8]
    ]
    ts = detected_at or _now_hms()

    plain_lines = [header, ""] + [f"{k}: {v}" for k, v in rows] + ["", f"有货机房 ({len(available_dcs)}):"] + dc_lines + ["", f"检测时间: {ts}"]
    html_body_lines = (
        [f"<b>{esc(k)}</b>: {esc(v)}" for k, v in rows]
        + ["", f"<b>有货机房 ({len(available_dcs)})</b>:"]
        + [esc(line) for line in dc_lines]
        + ["", f"<b>检测时间</b>: {esc(ts)}"]
    )

    if sold_out_at:
        html_text = (
            f"<b>{esc(header)}</b>\n\n<s>"
            + "\n".join(html_body_lines)
            + f"</s>\n\n<b>⛔ 已被抢完: {esc(sold_out_at)}</b>"
        )
        plain_text = "\n".join(plain_lines + ["", f"⛔ 已被抢完: {sold_out_at}"])
    else:
        html_text = f"<b>{esc(header)}</b>\n\n" + "\n".join(html_body_lines)
        plain_text = "\n".join(plain_lines)

    if len(html_text) > 4000:
        # Never cut through an HTML tag (Telegram would reject the message): fall back to escaped plain text
        html_text = esc(plain_text[:3900])
    return html_text, plain_text


class Notifier:
    """Sends alerts to Telegram, Bark, ServerChan, DingTalk, Feishu, Discord, and Custom Webhooks."""

    def __init__(self, db: Database):
        self.db = db
        self._stop_event = threading.Event()
        self._tg_thread: Optional[threading.Thread] = None
        self._tg_offset = 0
        self._bot_handlers: Dict[str, Callable[..., Any]] = {}

    def register_bot_handlers(self, handlers: Dict[str, Callable[..., Any]]) -> None:
        """Bind engine action callbacks used by Telegram interactive commands & buttons."""
        self._bot_handlers = handlers

    def _http_proxy(self, settings: Dict[str, Any]) -> str:
        return str(
            settings.get("http_proxy")
            or os.environ.get("OVH_HTTP_PROXY")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("HTTP_PROXY")
            or ""
        ).strip()

    def _tg_proxy(self, settings: Dict[str, Any]) -> str:
        return str(settings.get("telegram_proxy") or self._http_proxy(settings) or "").strip()

    def _parsed_targets(self, settings: Dict[str, Any]) -> List[Tuple[str, Optional[int]]]:
        default_tid = _parse_positive_int(settings.get("telegram_topic_id"))
        raw = str(settings.get("telegram_chat_id") or "").strip()
        out: List[Tuple[str, Optional[int]]] = []
        for item in raw.split(","):
            parsed = parse_telegram_target(item, default_topic_id=default_tid)
            if parsed:
                out.append(parsed)
        return out

    def _authorized_chat_ids(self, settings: Dict[str, Any]) -> List[str]:
        seen: List[str] = []
        for cid, _ in self._parsed_targets(settings):
            if cid not in seen:
                seen.append(cid)
        return seen

    def _authorized_user_ids(self, settings: Dict[str, Any]) -> set:
        users = {cid for cid in self._authorized_chat_ids(settings) if cid and not cid.startswith("-") and not cid.startswith("@")}
        raw_admins = str(settings.get("telegram_admin_ids") or "").strip()
        for item in raw_admins.split(","):
            s = item.strip()
            if s:
                users.add(s)
        return users

    def create_action_button_token(self, payload: Dict[str, Any], ttl_seconds: int = 86400) -> str:
        """Store rich button action metadata in SQLite kv_cache and return short 12-char token."""
        token = uuid.uuid4().hex[:12]
        self.db.kv_set(f"tg_btn:{token}", payload, ttl_seconds=ttl_seconds)
        return token

    def send_telegram_messages(
        self,
        text: str,
        reply_markup: Optional[Dict[str, Any]] = None,
        chat_id: Optional[str] = None,
        parse_mode: Optional[str] = None,
        message_thread_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Send Telegram message(s) (supporting Forum Topic message_thread_id) and return list of {chat_id, message_id}."""
        settings = self.db.get_settings()
        token = str(settings.get("telegram_bot_token") or "").strip()
        default_tid = _parse_positive_int(settings.get("telegram_topic_id"))
        if chat_id:
            parsed_single = parse_telegram_target(
                chat_id,
                default_topic_id=message_thread_id if message_thread_id is not None else None,
            )
            targets: List[Tuple[str, Optional[int]]] = []
            if parsed_single:
                base_c, parsed_t = parsed_single
                targets.append((base_c, message_thread_id if message_thread_id is not None else parsed_t))
        else:
            targets = self._parsed_targets(settings)
            if message_thread_id is not None:
                targets = [
                    (c, message_thread_id if (c.startswith("-") or c.startswith("@")) else t)
                    for c, t in targets
                ]
        if not token or not targets:
            return []
        proxy = self._tg_proxy(settings)
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        sent: List[Dict[str, Any]] = []
        for base_cid, tid in targets:
            payload: Dict[str, Any] = {
                "chat_id": base_cid,
                "text": text,
                "disable_web_page_preview": True,
            }
            if tid is not None and tid > 0:
                payload["message_thread_id"] = int(tid)
            if parse_mode:
                payload["parse_mode"] = parse_mode
            if reply_markup:
                payload["reply_markup"] = reply_markup
            try:
                res = _tg_post_api(url, payload, proxy_url=proxy, timeout=12.0)
                desc = str(res.get("description") or "")
                # Topic #1 ('General') in some legacy supergroups rejects explicit message_thread_id=1
                if not res.get("ok") and tid == 1 and "thread" in desc.lower():
                    payload.pop("message_thread_id", None)
                    res = _tg_post_api(url, payload, proxy_url=proxy, timeout=12.0)
                    desc = str(res.get("description") or "")
                if res.get("ok"):
                    result_obj = res.get("result") or {}
                    msg_id = result_obj.get("message_id")
                    actual_cid = str((result_obj.get("chat") or {}).get("id") or base_cid)
                    sent.append({"chat_id": actual_cid, "message_id": msg_id, "message_thread_id": tid})
                else:
                    if tid and "thread" in desc.lower():
                        self.db.add_log(
                            "WARNING",
                            f"Telegram 发送失败: 找不到话题 Topic ID={tid} (chat_id={base_cid}, 原始返回: {desc})。"
                            "注意：普通单向广播频道不支持多话题，请使用开启了「话题 (Topics)」的超级群组（或频道绑定的讨论群组）并确认话题 ID 正确。",
                            "notify",
                        )
                    else:
                        self.db.add_log("WARNING", f"Telegram 发送返回异常 ({base_cid}): {desc or res}", "notify")
            except Exception as exc:
                self.db.add_log("WARNING", f"Telegram 发送失败 ({base_cid}): {exc}", "notify")
        return sent

    def send_telegram(
        self,
        text: str,
        reply_markup: Optional[Dict[str, Any]] = None,
        chat_id: Optional[str] = None,
        parse_mode: Optional[str] = None,
        message_thread_id: Optional[int] = None,
    ) -> bool:
        return bool(
            self.send_telegram_messages(
                text,
                reply_markup=reply_markup,
                chat_id=chat_id,
                parse_mode=parse_mode,
                message_thread_id=message_thread_id,
            )
        )

    def edit_telegram_message(
        self,
        chat_id: str,
        message_id: Any,
        text: str,
        parse_mode: Optional[str] = "HTML",
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> bool:
        settings = self.db.get_settings()
        token = str(settings.get("telegram_bot_token") or "").strip()
        parsed = parse_telegram_target(chat_id)
        clean_cid = parsed[0] if parsed else str(chat_id or "").strip()
        if not token or not clean_cid or not message_id:
            return False
        proxy = self._tg_proxy(settings)
        url = f"https://api.telegram.org/bot{token}/editMessageText"
        payload: Dict[str, Any] = {
            "chat_id": clean_cid,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            res = _tg_post_api(url, payload, proxy_url=proxy, timeout=10.0)
            return bool(res.get("ok"))
        except Exception as exc:
            self.db.add_log("WARNING", f"Telegram 编辑消息失败: {exc}", "notify")
            return False

    def send_bark(self, title: str, body: str, url_link: str = "", urgent: bool = False) -> bool:
        settings = self.db.get_settings()
        bark_url = str(settings.get("bark_url") or "").strip().rstrip("/")
        if not bark_url:
            return False
        proxy = self._http_proxy(settings)
        payload: Dict[str, Any] = {
            "title": title,
            "body": body,
            "group": "OVH-Sniper",
        }
        if url_link:
            payload["url"] = url_link
        if urgent:
            payload["level"] = "timeSensitive"
            payload["sound"] = "alarm"
        try:
            _http_post_json(bark_url, payload, proxy_url=proxy, timeout=10.0)
            return True
        except Exception as exc:
            self.db.add_log("WARNING", f"Bark 推送失败: {exc}", "notify")
            return False

    def send_serverchan(self, title: str, body: str) -> bool:
        settings = self.db.get_settings()
        key = str(settings.get("serverchan_key") or "").strip()
        if not key:
            return False
        proxy = self._http_proxy(settings)
        url = f"https://sctapi.ftqq.com/{urllib.parse.quote(key)}.send"
        try:
            _http_post_json(url, {"title": title[:100], "desp": body}, proxy_url=proxy, timeout=10.0)
            return True
        except Exception as exc:
            self.db.add_log("WARNING", f"Server酱 推送失败: {exc}", "notify")
            return False

    def send_dingtalk(self, title: str, body: str) -> bool:
        settings = self.db.get_settings()
        webhook = str(settings.get("dingtalk_webhook") or "").strip()
        secret = str(settings.get("dingtalk_secret") or "").strip()
        if not webhook:
            return False
        proxy = self._http_proxy(settings)
        target = webhook
        if secret:
            ts = str(int(time.time() * 1000))
            sign_raw = f"{ts}\n{secret}".encode("utf-8")
            hmac_code = hmac.new(secret.encode("utf-8"), sign_raw, digestmod=hashlib.sha256).digest()
            sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
            sep = "&" if "?" in target else "?"
            target = f"{target}{sep}timestamp={ts}&sign={sign}"
        payload = {"msgtype": "markdown", "markdown": {"title": title, "text": f"### {title}\n\n{body}"}}
        try:
            _http_post_json(target, payload, proxy_url=proxy, timeout=10.0)
            return True
        except Exception as exc:
            self.db.add_log("WARNING", f"钉钉推送失败: {exc}", "notify")
            return False

    def send_feishu(self, title: str, body: str) -> bool:
        settings = self.db.get_settings()
        webhook = str(settings.get("feishu_webhook") or "").strip()
        if not webhook:
            return False
        proxy = self._http_proxy(settings)
        payload = {"msg_type": "text", "content": {"text": f"{title}\n\n{body}"}}
        try:
            _http_post_json(webhook, payload, proxy_url=proxy, timeout=10.0)
            return True
        except Exception as exc:
            self.db.add_log("WARNING", f"飞书推送失败: {exc}", "notify")
            return False

    def send_discord(self, title: str, body: str, color: int = 0x10B981) -> bool:
        settings = self.db.get_settings()
        webhook = str(settings.get("discord_webhook") or "").strip()
        if not webhook:
            return False
        proxy = self._http_proxy(settings)
        payload = {
            "embeds": [
                {
                    "title": title[:250],
                    "description": body[:4000],
                    "color": color,
                }
            ]
        }
        try:
            _http_post_json(webhook, payload, proxy_url=proxy, timeout=10.0)
            return True
        except Exception as exc:
            self.db.add_log("WARNING", f"Discord 推送失败: {exc}", "notify")
            return False

    def send_custom_webhook(self, event: str, title: str, body: str, extra: Optional[Dict[str, Any]] = None) -> bool:
        settings = self.db.get_settings()
        webhook = str(settings.get("custom_webhook_url") or "").strip()
        if not webhook:
            return False
        proxy = self._http_proxy(settings)
        payload = {"event": event, "title": title, "message": body, "data": extra or {}}
        try:
            _http_post_json(webhook, payload, proxy_url=proxy, timeout=10.0)
            return True
        except Exception as exc:
            self.db.add_log("WARNING", f"自定义 Webhook 推送失败: {exc}", "notify")
            return False

    def notify(
        self,
        title: str,
        body: str,
        event: str = "info",
        urgent: bool = False,
        url_link: str = "",
        tg_reply_markup: Optional[Dict[str, Any]] = None,
        tg_html: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, bool]:
        """Broadcast a notification across all configured channels."""
        if tg_html:
            tg_ok = self.send_telegram(tg_html, reply_markup=tg_reply_markup, parse_mode="HTML")
        else:
            tg_text = f"{title}\n\n{body}"
            if url_link and url_link not in tg_text:
                tg_text += f"\n\n🔗 {url_link}"
            tg_ok = self.send_telegram(tg_text, reply_markup=tg_reply_markup)
        color = (
            0x10B981
            if event in ("order_success", "order_paid", "order_delivered", "stock")
            else (0xF59E0B if event in ("radar", "pending_confirm") else 0xEF4444)
        )
        return {
            "telegram": tg_ok,
            "bark": self.send_bark(title, body, url_link=url_link, urgent=urgent),
            "serverchan": self.send_serverchan(title, body),
            "dingtalk": self.send_dingtalk(title, body),
            "feishu": self.send_feishu(title, body),
            "discord": self.send_discord(title, body, color=color),
            "custom_webhook": self.send_custom_webhook(event, title, body, extra=extra),
        }

    def notify_stock_with_buttons(
        self,
        plan_code: str,
        available_dcs: List[Dict[str, str]],
        account_id: str = "",
        endpoint: str = "ovh-eu",
        subsidiary: str = "IE",
        product_line: str = "eco",
        server_name: str = "",
        cpu: str = "",
        memory: str = "",
        storage: str = "",
        options: Optional[List[str]] = None,
        price_val: Optional[float] = None,
        setup_val: Optional[float] = None,
        currency: str = "EUR",
        card_key: str = "",
    ) -> Dict[str, bool]:
        """Send HTML restock card with 1-click API buy + OVH Express URL buttons & sold-out edit tracking."""
        settings = self.db.get_settings()
        proxy = self._http_proxy(settings)
        price_text = GLOBAL_FX.format_price_with_cny(price_val, currency, proxy_url=proxy) if price_val is not None else ""
        setup_text = (
            "免费"
            if setup_val == 0
            else (GLOBAL_FX.format_price_with_cny(setup_val, currency, proxy_url=proxy) if setup_val is not None else "")
        )
        detected_at = _now_hms()
        html_text, plain_text = render_telegram_stock_card(
            plan_code=plan_code,
            available_dcs=available_dcs,
            server_name=server_name,
            cpu=cpu,
            memory=memory,
            storage=storage,
            price_text=price_text,
            setup_text=setup_text,
            detected_at=detected_at,
        )

        # Inline buttons: 1-click API buy per DC + OVH Express direct review link for top DC
        keyboard: List[List[Dict[str, str]]] = []
        row: List[Dict[str, str]] = []
        for item in available_dcs[:6]:
            dc = item["dc"]
            tok = self.create_action_button_token(
                {
                    "action": "quick_buy",
                    "plan_code": plan_code,
                    "datacenter": dc,
                    "account_id": account_id,
                    "product_line": product_line,
                    "options": options or [],
                    "price_val": price_val,
                }
            )
            row.append({"text": f"🛒 {dc.upper()} API抢购", "callback_data": f"buy:{tok}"})
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)

        # Direct OVH Express Order button for the primary available DC
        top_dc = available_dcs[0]["dc"] if available_dcs else ""
        express_url = build_express_order_url(
            endpoint=endpoint,
            subsidiary=subsidiary,
            plan_code=plan_code,
            product_line=product_line,
            datacenter=top_dc,
            options=options or [],
        )
        keyboard.append([{"text": f"🌐 OVH 官网直达下单 ({subsidiary}/{top_dc.upper()})", "url": express_url}])
        markup = {"inline_keyboard": keyboard}

        sent_msgs = self.send_telegram_messages(html_text, reply_markup=markup, parse_mode="HTML")
        if sent_msgs and card_key and settings.get("edit_sold_out_telegram", True):
            self.db.kv_set(
                f"tg_card:{card_key}",
                {
                    "messages": sent_msgs,
                    "plan_code": plan_code,
                    "available_dcs": available_dcs,
                    "server_name": server_name,
                    "cpu": cpu,
                    "memory": memory,
                    "storage": storage,
                    "price_text": price_text,
                    "setup_text": setup_text,
                    "detected_at": detected_at,
                },
                ttl_seconds=48 * 3600,
            )

        # Broadcast to non-Telegram channels too
        title = f"🔥 OVH 补货提醒: {server_name or plan_code}"
        return {
            "telegram": bool(sent_msgs),
            "bark": self.send_bark(title, plain_text, url_link=express_url, urgent=True),
            "serverchan": self.send_serverchan(title, plain_text),
            "dingtalk": self.send_dingtalk(title, plain_text),
            "feishu": self.send_feishu(title, plain_text),
            "discord": self.send_discord(title, plain_text, color=0x10B981),
            "custom_webhook": self.send_custom_webhook(
                "stock",
                title,
                plain_text,
                extra={"plan_code": plan_code, "datacenters": available_dcs, "express_url": express_url},
            ),
        }

    def mark_stock_card_sold_out(self, card_key: str) -> bool:
        """Edit previously sent Telegram restock card to strikethrough '已被抢完' and remove buttons."""
        settings = self.db.get_settings()
        if not settings.get("edit_sold_out_telegram", True):
            return False
        record = self.db.kv_get(f"tg_card:{card_key}")
        if not record or record.get("edited_sold_out"):
            return False
        sold_at = _now_hms()
        html_text, _ = render_telegram_stock_card(
            plan_code=record["plan_code"],
            available_dcs=record["available_dcs"],
            server_name=record.get("server_name", ""),
            cpu=record.get("cpu", ""),
            memory=record.get("memory", ""),
            storage=record.get("storage", ""),
            price_text=record.get("price_text", ""),
            setup_text=record.get("setup_text", ""),
            detected_at=record.get("detected_at", ""),
            sold_out_at=sold_at,
        )
        ok_any = False
        for m in record.get("messages") or []:
            if self.edit_telegram_message(
                chat_id=str(m["chat_id"]),
                message_id=m["message_id"],
                text=html_text,
                parse_mode="HTML",
                reply_markup={"inline_keyboard": []},
            ):
                ok_any = True
        record["edited_sold_out"] = True
        self.db.kv_set(f"tg_card:{card_key}", record, ttl_seconds=3600)
        return ok_any

    # ---------- Telegram Long Polling Bot ----------

    def start_telegram_polling(self) -> None:
        if self._tg_thread and self._tg_thread.is_alive():
            return
        self._stop_event.clear()
        self._tg_thread = threading.Thread(target=self._tg_poll_loop, name="tg-poller", daemon=True)
        self._tg_thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def _is_chat_allowed(self, chat: Dict[str, Any], allowed_chats: set) -> bool:
        cid = str(chat.get("id") or "")
        if cid and cid in allowed_chats:
            return True
        uname = str(chat.get("username") or "").strip()
        if uname:
            allowed_lower = {c.lower() for c in allowed_chats}
            if f"@{uname}".lower() in allowed_lower:
                return True
        return False

    @staticmethod
    def _extract_msg_topic_id(msg: Dict[str, Any]) -> Optional[int]:
        if msg.get("is_topic_message") and msg.get("message_thread_id"):
            try:
                return int(msg["message_thread_id"])
            except (TypeError, ValueError):
                return None
        return None

    def _tg_poll_loop(self) -> None:
        while not self._stop_event.is_set():
            settings = self.db.get_settings()
            token = str(settings.get("telegram_bot_token") or "").strip()
            enabled = bool(settings.get("telegram_polling_enabled", True))
            allowed_chats = set(self._authorized_chat_ids(settings))
            allowed_users = self._authorized_user_ids(settings)
            if not token or not enabled or not allowed_chats:
                self._stop_event.wait(5.0)
                continue

            proxy = self._tg_proxy(settings)
            url = f"https://api.telegram.org/bot{token}/getUpdates?timeout=25&offset={self._tg_offset}"
            try:
                data = _http_get_json(url, proxy_url=proxy, timeout=35.0)
                if not data.get("ok"):
                    self._stop_event.wait(5.0)
                    continue
                for upd in data.get("result") or []:
                    upd_id = int(upd.get("update_id") or 0)
                    if upd_id >= self._tg_offset:
                        self._tg_offset = upd_id + 1
                    self._handle_tg_update(token, proxy, allowed_chats, allowed_users, upd)
            except Exception as exc:
                self.db.add_log("WARNING", f"Telegram Long Polling 异常（将在10秒后重试）: {exc}", "telegram")
                self._stop_event.wait(10.0)

    def _handle_tg_update(
        self,
        token: str,
        proxy: str,
        allowed_chats: set,
        allowed_users: set,
        upd: Dict[str, Any],
    ) -> None:
        if "callback_query" in upd:
            cq = upd["callback_query"]
            cq_id = cq.get("id", "")
            msg = cq.get("message") or {}
            chat = msg.get("chat") or {}
            chat_id = str(chat.get("id") or "")
            thread_id = self._extract_msg_topic_id(msg)
            if not self._is_chat_allowed(chat, allowed_chats):
                return
            # In groups/channels, buttons can move real money: the pressing USER must be authorized too
            from_id = str((cq.get("from") or {}).get("id") or "")
            if chat.get("type") != "private" and from_id not in allowed_users:
                self.db.add_log("WARNING", f"拒绝未授权 Telegram 用户 {from_id} 在群组 {chat_id} 中的按钮操作", "telegram")
                _tg_post_api(
                    f"https://api.telegram.org/bot{token}/answerCallbackQuery",
                    {
                        "callback_query_id": cq_id,
                        "text": f"⚠️ 你的用户 ID ({from_id}) 未授权，请将其填入面板「群组授权用户 ID」",
                        "show_alert": True,
                    },
                    proxy_url=proxy,
                    timeout=8.0,
                )
                return
            data_str = str(cq.get("data") or "")
            reply_text = self._dispatch_callback(data_str)
            _tg_post_api(
                f"https://api.telegram.org/bot{token}/answerCallbackQuery",
                {"callback_query_id": cq_id, "text": reply_text[:180], "show_alert": False},
                proxy_url=proxy,
                timeout=8.0,
            )
            self.send_telegram(reply_text, chat_id=chat_id, message_thread_id=thread_id)
            return

        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        thread_id = self._extract_msg_topic_id(msg)
        text = str(msg.get("text") or "").strip()
        if not text or not self._is_chat_allowed(chat, allowed_chats):
            return
        from_id = str((msg.get("from") or {}).get("id") or "")
        parts = text.split()
        cmd = parts[0].split("@")[0].lower() if text.startswith("/") else ""
        if cmd in ("/id", "/topic"):
            target_str = f"{chat_id}/{thread_id}" if thread_id else chat_id
            info_lines = [
                "🆔 当前会话信息:",
                f"• Chat ID: {chat_id}",
                f"• Topic ID (话题): {thread_id or '无 (主会话/General)'}",
                f"• 你的 User ID: {from_id or '未知'}",
                f"• 面板可直接填写的推送目标: {target_str}",
            ]
            self.send_telegram("\n".join(info_lines), chat_id=chat_id, message_thread_id=thread_id)
            return
        if chat.get("type") not in (None, "private") and from_id not in allowed_users:
            return
        if not text.startswith("/"):
            return
        args = parts[1:]
        result = self._dispatch_command(cmd, args)
        if isinstance(result, dict):
            self.send_telegram(
                result.get("text", ""),
                reply_markup=result.get("reply_markup"),
                chat_id=chat_id,
                parse_mode=result.get("parse_mode"),
                message_thread_id=thread_id,
            )
        elif result:
            self.send_telegram(str(result), chat_id=chat_id, message_thread_id=thread_id)

    def _dispatch_callback(self, data_str: str) -> str:
        if data_str.startswith("buy:"):
            tok = data_str[4:]
            # Single-use: consume atomically so double-taps can never create duplicate real-buy tasks
            meta = self.db.kv_pop(f"tg_btn:{tok}")
            if not meta:
                return "⚠️ 按钮已使用或已过期，请在面板或使用 /check 重新查询。"
            handler = self._bot_handlers.get("quick_buy")
            if handler:
                try:
                    return str(handler(meta))
                except Exception as exc:
                    return f"❌ 一键下单失败: {exc}"
        elif data_str.startswith("sc:"):
            # Short-link confirm order
            tok = data_str[3:]
            handler = self._bot_handlers.get("short_confirm")
            if handler:
                try:
                    return str(handler(tok))
                except Exception as exc:
                    return f"❌ 确认下单失败: {exc}"
        elif data_str.startswith("sx:"):
            tok = data_str[3:]
            handler = self._bot_handlers.get("short_cancel")
            if handler:
                return str(handler(tok))
            return "已取消该短链购物车。"
        return "⚠️ 未知操作"

    def _dispatch_command(self, cmd: str, args: List[str]) -> Any:
        if cmd in ("/start", "/help"):
            return (
                "🎯 OVH Sniper v2.1 Bot\n\n"
                "可用命令:\n"
                "/status - 查看引擎、快速通道与统计状态\n"
                "/tasks - 查看抢购任务列表\n"
                "/check <planCode> [endpoint] - 实时查询库存\n"
                "/order <https://.../o/e/...> - 解析 @ovheco 短链并预览/确认下单\n"
                "/orders - 查看最近订单与交付/中奖状态\n"
                "/servers - 查看账号已有独服列表\n"
                "/id - 查看当前群组 Chat ID、话题 Topic ID 与个人 User ID\n"
                "/pause - 暂停全局抢购引擎\n"
                "/resume - 恢复全局抢购引擎"
            )
        handler = self._bot_handlers.get(cmd.lstrip("/"))
        if handler:
            try:
                return handler(args)
            except Exception as exc:
                return f"❌ 执行命令 {cmd} 出错: {exc}"
        return "未知命令，发送 /help 查看帮助。"
