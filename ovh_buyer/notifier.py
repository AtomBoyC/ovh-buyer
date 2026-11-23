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

    def _authorized_chat_ids(self, settings: Dict[str, Any]) -> List[str]:
        raw = str(settings.get("telegram_chat_id") or "").strip()
        return [x.strip() for x in raw.split(",") if x.strip()]

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
    ) -> List[Dict[str, Any]]:
        """Send Telegram message(s) and return list of {chat_id, message_id} for later editing."""
        settings = self.db.get_settings()
        token = str(settings.get("telegram_bot_token") or "").strip()
        targets = [chat_id] if chat_id else self._authorized_chat_ids(settings)
        if not token or not targets:
            return []
        proxy = self._tg_proxy(settings)
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        sent: List[Dict[str, Any]] = []
        for cid in targets:
            payload: Dict[str, Any] = {
                "chat_id": cid,
                "text": text,
                "disable_web_page_preview": True,
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode
            if reply_markup:
                payload["reply_markup"] = reply_markup
            try:
                res = _http_post_json(url, payload, proxy_url=proxy, timeout=12.0)
                if res.get("ok"):
                    msg_id = (res.get("result") or {}).get("message_id")
                    sent.append({"chat_id": cid, "message_id": msg_id})
                else:
                    self.db.add_log("WARNING", f"Telegram 发送返回异常: {res}", "notify")
            except Exception as exc:
                self.db.add_log("WARNING", f"Telegram 发送失败: {exc}", "notify")
        return sent

    def send_telegram(
        self,
        text: str,
        reply_markup: Optional[Dict[str, Any]] = None,
        chat_id: Optional[str] = None,
        parse_mode: Optional[str] = None,
    ) -> bool:
        return bool(
            self.send_telegram_messages(text, reply_markup=reply_markup, chat_id=chat_id, parse_mode=parse_mode)
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
        if not token or not chat_id or not message_id:
            return False
        proxy = self._tg_proxy(settings)
        url = f"https://api.telegram.org/bot{token}/editMessageText"
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            res = _http_post_json(url, payload, proxy_url=proxy, timeout=10.0)
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

    def _tg_poll_loop(self) -> None:
        while not self._stop_event.is_set():
            settings = self.db.get_settings()
            token = str(settings.get("telegram_bot_token") or "").strip()
            enabled = bool(settings.get("telegram_polling_enabled", True))
            allowed_chats = set(self._authorized_chat_ids(settings))
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
                    self._handle_tg_update(token, proxy, allowed_chats, upd)
            except Exception as exc:
                self.db.add_log("WARNING", f"Telegram Long Polling 异常（将在10秒后重试）: {exc}", "telegram")
                self._stop_event.wait(10.0)

    def _handle_tg_update(self, token: str, proxy: str, allowed_chats: set, upd: Dict[str, Any]) -> None:
        if "callback_query" in upd:
            cq = upd["callback_query"]
            cq_id = cq.get("id", "")
            msg = cq.get("message") or {}
            chat = msg.get("chat") or {}
            chat_id = str(chat.get("id") or "")
            if chat_id not in allowed_chats:
                return
            # In groups, buttons can move real money: the pressing USER must be authorized too
            from_id = str((cq.get("from") or {}).get("id") or "")
            if chat.get("type") != "private" and from_id not in allowed_chats:
                self.db.add_log("WARNING", f"拒绝未授权 Telegram 用户 {from_id} 在群组 {chat_id} 中的按钮操作", "telegram")
                return
            data_str = str(cq.get("data") or "")
            reply_text = self._dispatch_callback(data_str)
            try:
                _http_post_json(
                    f"https://api.telegram.org/bot{token}/answerCallbackQuery",
                    {"callback_query_id": cq_id, "text": reply_text[:180], "show_alert": False},
                    proxy_url=proxy,
                    timeout=8.0,
                )
            except Exception:
                pass
            self.send_telegram(reply_text, chat_id=chat_id)
            return

        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        text = str(msg.get("text") or "").strip()
        if not text or chat_id not in allowed_chats:
            return
        from_id = str((msg.get("from") or {}).get("id") or "")
        if chat.get("type") not in (None, "private") and from_id not in allowed_chats:
            return
        if not text.startswith("/"):
            return
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        args = parts[1:]
        result = self._dispatch_command(cmd, args)
        if isinstance(result, dict):
            self.send_telegram(
                result.get("text", ""),
                reply_markup=result.get("reply_markup"),
                chat_id=chat_id,
                parse_mode=result.get("parse_mode"),
            )
        elif result:
            self.send_telegram(str(result), chat_id=chat_id)

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
                "🎯 OVH Sniper v2.0 Bot\n\n"
                "可用命令:\n"
                "/status - 查看引擎、快速通道与统计状态\n"
                "/tasks - 查看抢购任务列表\n"
                "/check <planCode> [endpoint] - 实时查询库存\n"
                "/order <https://.../o/e/...> - 解析 @ovheco 短链并预览/确认下单\n"
                "/orders - 查看最近订单与交付/中奖状态\n"
                "/servers - 查看账号已有独服列表\n"
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
