"""Multi-channel Notification Hub + Zero-Domain Telegram Long Polling Bot."""

import base64
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable, Dict, List, Optional

from .config import format_datacenter_label
from .database import Database


def _http_post_json(
    url: str,
    payload: Dict[str, Any],
    proxy_url: str = "",
    timeout: float = 12.0,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req_headers = {"Content-Type": "application/json; charset=utf-8", "User-Agent": "ovh-buyer/1.0"}
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
    req = urllib.request.Request(url, headers={"User-Agent": "ovh-buyer/1.0"}, method="GET")
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


class Notifier:
    """Sends alerts to Telegram, Bark, ServerChan, DingTalk, Feishu, Discord, and Custom Webhooks."""

    def __init__(self, db: Database):
        self.db = db
        self._stop_event = threading.Event()
        self._tg_thread: Optional[threading.Thread] = None
        self._tg_offset = 0
        self._bot_handlers: Dict[str, Callable[..., str]] = {}

    def register_bot_handlers(self, handlers: Dict[str, Callable[..., str]]) -> None:
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

    def send_telegram(
        self,
        text: str,
        reply_markup: Optional[Dict[str, Any]] = None,
        chat_id: Optional[str] = None,
    ) -> bool:
        settings = self.db.get_settings()
        token = str(settings.get("telegram_bot_token") or "").strip()
        targets = [chat_id] if chat_id else self._authorized_chat_ids(settings)
        if not token or not targets:
            return False
        proxy = self._tg_proxy(settings)
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        ok_any = False
        for cid in targets:
            payload: Dict[str, Any] = {
                "chat_id": cid,
                "text": text,
                "disable_web_page_preview": True,
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup
            try:
                res = _http_post_json(url, payload, proxy_url=proxy, timeout=12.0)
                if res.get("ok"):
                    ok_any = True
                else:
                    self.db.add_log("WARNING", f"Telegram 发送返回异常: {res}", "notify")
            except Exception as exc:
                self.db.add_log("WARNING", f"Telegram 发送失败: {exc}", "notify")
        return ok_any

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
            # Support both full Bark push URL (https://api.day.app/<key>) and /push endpoint
            target = bark_url if bark_url.endswith("/push") else bark_url
            _http_post_json(target, payload, proxy_url=proxy, timeout=10.0)
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
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, bool]:
        """Broadcast a notification across all configured channels."""
        tg_text = f"{title}\n\n{body}"
        if url_link and url_link not in tg_text:
            tg_text += f"\n\n🔗 {url_link}"
        color = 0x10B981 if event in ("order_success", "stock") else (0xF59E0B if event == "radar" else 0xEF4444)
        results = {
            "telegram": self.send_telegram(tg_text, reply_markup=tg_reply_markup),
            "bark": self.send_bark(title, body, url_link=url_link, urgent=urgent),
            "serverchan": self.send_serverchan(title, body),
            "dingtalk": self.send_dingtalk(title, body),
            "feishu": self.send_feishu(title, body),
            "discord": self.send_discord(title, body, color=color),
            "custom_webhook": self.send_custom_webhook(event, title, body, extra=extra),
        }
        return results

    def notify_stock_with_buttons(
        self,
        plan_code: str,
        available_dcs: List[Dict[str, str]],
        account_id: str = "",
        product_line: str = "eco",
        memory: str = "",
        storage: str = "",
        options: Optional[List[str]] = None,
        price_hint: str = "",
    ) -> Dict[str, bool]:
        """Send stock alert with Telegram 1-click buy & price-check inline buttons."""
        lines = [f"型号: {plan_code}"]
        if memory or storage:
            lines.append(f"配置: {memory or '默认'} + {storage or '默认'}")
        if price_hint:
            lines.append(f"参考价: {price_hint}")
        lines.append(f"有货机房 ({len(available_dcs)}):")
        for item in available_dcs:
            dc = item["dc"]
            st = item.get("status", "available")
            lines.append(f"  • {format_datacenter_label(dc)} [{st}]")

        # Build Telegram inline buttons (2 per row)
        keyboard: List[List[Dict[str, str]]] = []
        row: List[Dict[str, str]] = []
        for item in available_dcs[:8]:
            dc = item["dc"]
            tok = self.create_action_button_token(
                {
                    "action": "quick_buy",
                    "plan_code": plan_code,
                    "datacenter": dc,
                    "account_id": account_id,
                    "product_line": product_line,
                    "options": options or [],
                }
            )
            row.append({"text": f"🛒 {dc.upper()} 一键抢购", "callback_data": f"buy:{tok}"})
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)

        markup = {"inline_keyboard": keyboard} if keyboard else None
        return self.notify(
            title=f"🔥 OVH 补货提醒: {plan_code}",
            body="\n".join(lines),
            event="stock",
            urgent=True,
            tg_reply_markup=markup,
            extra={"plan_code": plan_code, "datacenters": available_dcs, "options": options or []},
        )

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
        # 1. Inline button callback_query
        if "callback_query" in upd:
            cq = upd["callback_query"]
            cq_id = cq.get("id", "")
            msg = cq.get("message") or {}
            chat_id = str((msg.get("chat") or {}).get("id") or "")
            if chat_id not in allowed_chats:
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

        # 2. Text commands
        msg = upd.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id") or "")
        text = str(msg.get("text") or "").strip()
        if not text or chat_id not in allowed_chats:
            return
        if not text.startswith("/"):
            return
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        args = parts[1:]
        reply = self._dispatch_command(cmd, args)
        if reply:
            self.send_telegram(reply, chat_id=chat_id)

    def _dispatch_callback(self, data_str: str) -> str:
        if data_str.startswith("buy:"):
            tok = data_str[4:]
            meta = self.db.kv_get(f"tg_btn:{tok}")
            if not meta:
                return "⚠️ 按钮已过期，请在面板或使用 /check 重新查询。"
            handler = self._bot_handlers.get("quick_buy")
            if handler:
                try:
                    return handler(meta)
                except Exception as exc:
                    return f"❌ 一键下单失败: {exc}"
        return "⚠️ 未知操作"

    def _dispatch_command(self, cmd: str, args: List[str]) -> str:
        if cmd in ("/start", "/help"):
            return (
                "🎯 OVH Black Friday Sniper Bot\n\n"
                "可用命令:\n"
                "/status - 查看引擎状态与统计\n"
                "/tasks - 查看抢购任务列表\n"
                "/check <planCode> - 实时查询库存\n"
                "/orders - 查看最近订单与支付链接\n"
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
