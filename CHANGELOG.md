# Changelog

All notable changes to **OVH Buyer (OVH Sniper)** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/).

---

## [2.2.0] - 2026-11-28

### Added

- **Two-Tier Scheduled Sniping & Monitoring (`ovh_buyer/scheduler.py`, `schedule_rules`, Web UI & Telegram Bot)**:
  - **Item-Level Schedules** for Sniper Tasks, Black Friday Radar rules, and VPS Watches: supports `24/7 Always Active`, `Daily Active Windows` (multiple comma-separated ranges and cross-midnight spans such as `22:00-02:00` with ISO weekday selection), and `Date/Time Range` (`start_at` ~ `end_at` flash-sale countdowns with optional `auto_pause_on_end`).
  - **⚡ Fast Lane Pre-Warmup**: scheduled Fast Lane tasks automatically prebuild and price-verify shopping carts `warmup_minutes` (default `5` min) ahead of the scheduled window/start time so single-request `POST /checkout` is warm at the exact second the sale opens, while maintaining zero carts and zero API polls outside active windows.
  - **Global Schedule Rules (`Schedules` Sidebar Tab & `/api/schedules` CRUD)**: supports **Edge-Triggered Window Guard** (enables target modules/tasks and optional peak `start_qps` on window entry, pauses and restores `end_qps` on window exit without overriding manual mid-window toggles; strictly preserves `pending_confirm` and `completed` task states), **Daily Trigger (`daily_at`)**, and **One-Shot Countdown (`once`)** across configurable IANA timezones (`schedule_timezone` with zero-dependency `zoneinfo` + fixed-offset fallback).
  - **Telegram `/schedule` (`/schedules`) Command**: view live scheduler clock, global schedule rules, and scheduled tasks, or remotely toggle rules via `/schedule on <ID>` and `/schedule off <ID>`.
- **Universal Proxy Protocol Support Across Notifications, FX & CLI**:
  - Telegram Long Polling Bot, multi-channel Webhooks (Bark, ServerChan, DingTalk, Feishu, Discord, Custom Webhook), live CNY exchange-rate fetcher (`specs.py`), and CLI subcommands (`run.py check/catalog/vps`) now share `proxy_http_request` and natively support `http://`, `https://` (nested TLS), `socks5://`, `socks5h://`, and `[scheme://]host:port:user:pass` shorthand.
- **Sub-Second Polling & Preemptive Proxy Probe Controls**:
  - Removed artificial minimum poll-interval floors (`poll_interval >= 0` with `0.05s` loop cadence for flash sales) while throttling unchanged SQLite `last_check` disk writes during `<1s` tight-loop polling.
  - Added `POST /api/proxy/pool/stop`, live progress bar, and preemptive restart (`restart=True`) for staggered background proxy pool probing.
- **Bilingual Documentation**:
  - English default [README.md](README.md) with language switcher to full Simplified Chinese [README.zh-CN.md](README.zh-CN.md), plus an all-English [CHANGELOG.md](CHANGELOG.md).

### Fixed & Security

- **Genuine Nested TLS-in-TLS (`https://` Proxy -> `https://` Target) & Socket Leak Prevention**: replaced stacked `ssl_ctx.wrap_socket()` calls (which detached the raw OS file descriptor and stripped outer proxy TLS encryption) with a stdlib `ssl.MemoryBIO` tunnel (`_TLSTunnelOverBIO`), and guaranteed deterministic socket closure across all SOCKS5/HTTP/HTTPS handshake failure paths.
- **Immediate Proxy Pool Quarantine on `403 / 407 / 429` Tunnel Errors**: propagated HTTP `CONNECT` tunnel status codes and SOCKS5 authentication rejections (`407`) into `OVHAPIError.status_code` so `ProxyPoolManager` immediately quarantines bad-credential or rate-limited proxies on the first failure.
- **At-Rest Proxy/Webhook Encryption & Masked UI Round-Tripping**: extended `DATA_ENCRYPTION_KEY` at-rest encryption to proxy settings, per-account/task/radar/VPS `proxy_url`, and notification tokens/webhooks; masked proxy passwords (`:****@`) in `GET /api/settings`, `/api/accounts`, `/api/tasks`, `/api/radar`, and `/api/vps` while seamlessly preserving unchanged passwords on save and speed-test (`merge_masked_proxy_input`).
- **Fast Lane Exit-IP Fingerprint Binding**: included effective `proxy_url` in `task_cart_fingerprint()` and reused the slot's bound client at checkout so changing account/order proxy settings immediately invalidates stale prebuilt carts and prevents build-vs-checkout IP drift.
- **Bounded Rate-Limiter Cache & Probe Isolation**: capped `_GLOBAL_RATE_LIMITERS` via LRU eviction (`MAX_RATE_LIMITERS = 256`) and isolated speed-test probes (`bypass_rate_limit=True`) so probing never mutates live monitoring QPS.
- **Frontend XSS, Theme & Cache-Busting Hardening**: escaped `priceWithTax.text` in the official OVH orders list (`app.js`), replaced hardcoded dark-mode text colors with theme-aware CSS variables, isolated option-card CSS (`.ovh-opt-stack` / `.ovh-opt-grid`) against browser extensions, added content-hash cache-busting on `/style.css` and `/app.js`, and expanded GitHub Actions CI to Python `3.9–3.13`.

---

## [2.1.4] - 2026-11-27

### Added

- **Two-Tier Monitoring Proxy Pool (`ProxyPoolManager`: Master Pool + Auto-Selected Active Pool)**:
  - Automatically maintains up to `proxy_max_active` (default `5`) lowest-latency healthy proxies in the Active Pool (`<= proxy_max_latency_ms`) from a larger candidate Master Pool (`monitor_proxies_pool` / `OVH_MONITOR_PROXIES_POOL`).
  - **Staggered 1-by-1 Silent Background Probing**: probes candidates strictly sequentially with randomized jitter delays (`1.5s–4.0s`) rather than concurrent bursts, preventing proxy-gateway concurrency throttling and anti-bot burst signatures.
  - **Instant Hot-Swap Failover & Cooldown Resurrection**: immediately quarantines proxies hitting `403 / 407 / 429` or `proxy_fail_threshold` consecutive errors for `proxy_cooldown_sec`, hot-swaps in the fastest pre-measured standby proxy with zero wait, and re-admits cooled proxies in subsequent probe passes.
  - **Live Active Pool Dashboard & APIs**: added `GET /api/proxy/pool`, `POST /api/proxy/pool/probe`, and `POST /api/proxy/pool/evict` (one-click manual eviction/rotation using credential-safe hashed IDs).

### Changed

- **Modular Configuration Navigation**: refactored the monolithic Settings view under the `Configuration` sidebar section into four focused tabs — **Notification Settings**, **Network Settings**, **Feature Switches**, and **Panel Security**.

### Fixed

- **Strict No-Local-IP-Leak Guard**: when all monitoring pool proxies are in failure cooldown and `monitor_proxy_fallback_direct` is disabled, monitoring requests strictly abort instead of falling back to direct host connections.
- **Proxy Parser & Tester Enhancements**: added inline per-proxy test results and loading states in Network Settings; supported scheme-prefixed shorthand (`socks5://host:port:user:pass` / `http://host:port:user:pass`), preserved `,` and `;` inside proxy passwords, and omitted `:443` from `Host` headers over SOCKS5/HTTPS tunnels.

---

## [2.1.3] - 2026-11-27

### Added

- **Dedicated Monitoring Proxy Pool (`HTTP` / `HTTPS` / `SOCKS5`) & UI Speed Tester**:
  - Added quick proxy configuration & one-click latency speed tester (`POST /api/proxy/test`) across Overview, Catalog, Sniper Tasks, Black Friday Radar, and VPS Monitor tabs.
  - Implemented zero-dependency **SOCKS5 / SOCKS5H (RFC 1928 + RFC 1929 user/pass auth with remote DNS)** and authenticated HTTP/HTTPS proxies, including `ip:port:user:pass` shorthand parsing.
  - Added round-robin proxy rotation with automatic 30s failure cooldown and per-proxy independent QPS rate limiting.
  - Isolated monitoring proxies from authenticated account/checkout requests by default (`order_use_proxy = false`), with optional per-account fixed `proxy_url` binding and per-task/radar/VPS `proxy_url` overrides, plus inline edit modals for Radar rules and VPS watches.

---

## [2.1.2] - 2026-11-27

### Added

- **Telegram Supergroup Forum Topic (`message_thread_id`) Routing**:
  - Added `TELEGRAM_TOPIC_ID` (`telegram_topic_id`) and `TELEGRAM_ADMIN_IDS` (`telegram_admin_ids`, authorizing group button operators without sending duplicate personal DM alerts).
  - Supported pasting Telegram topic URLs directly (`https://t.me/c/2134567890/45`, `https://t.me/group/45`) or shorthand `-1002134567890/45` (`/` / `:` / `#` separators) in `Chat ID` for multi-group, multi-topic routing compatible with sold-out strikethrough edits.
  - Added `/id` (`/topic`) Bot command to inspect current `Chat ID`, `Topic ID`, and personal `User ID`.

### Fixed

- Preserved full Telegram Bot API JSON error descriptions on HTTP 4xx/5xx responses with clear troubleshooting hints when `General (Topic #1)` or broadcast channels are misconfigured.

---

## [2.1.1] - 2026-11-27

### Fixed

- **Auto-Fix Docker Bind-Mount Permissions (`./data:/app/data`)**:
  - Added `scripts/docker-entrypoint.sh` + `su-exec` with configurable `PUID` / `PGID` (default `1000:1000`) to automatically initialize `/app/data` ownership before dropping root privileges, with safe fallback when `chown` is disallowed by network filesystems.
  - Fixed Docker `HEALTHCHECK` in `HEADLESS=true` mode, respected custom `OVH_DATA_DIR` from `.env`, added `WAL -> DELETE` SQLite journal fallback for NFS/SMB mounts, and added a root-owned bind-mount CI smoke test.
- **Cloudflare Tunnel (`cloudflared`) & Reverse-Proxy Hardening**:
  - Extracted real client IPs from `CF-Connecting-IP` / `X-Forwarded-For` when requests originate from loopback/private reverse proxies, preventing brute-force lockouts from blocking the proxy gateway IP.
  - Respected `CF-Visitor` HTTPS headers on session cookies and added `Cache-Control: no-cache, no-transform` + `X-Accel-Buffering: no` to `/api/logs/stream` SSE responses.

---

## [2.1.0] - 2026-11-24

> Redesigned SaaS Web Console + critical real-money checkout safety fixes. Legacy v2.0 UI archived on branch [`legacy-v2.0`](https://github.com/AtomBoyC/ovh-buyer/tree/legacy-v2.0).

### Changed

- **Redesigned Web Console**: clean StockHawk-inspired SaaS interface with Light / Dark / System themes, Lucide SVG icons, grouped sidebar navigation, topbar quick switches, KPI strip, selectable option cards, sticky-header modals, and mobile layout.

### Fixed

- **Never Resend Non-Idempotent Checkouts**: `POST /checkout` is never automatically retried or resent across pooled connections on timeout, disconnect, or 5xx, preventing duplicate orders.
- **Strict Unknown-Price Ceiling Guard**: when `max_price > 0` and live cart price cannot be retrieved, checkout aborts immediately on both standard and Fast Lane paths.
- **Per-Task In-Flight Concurrency Lock & Atomic Task Updates**: prevented concurrent double-orders across main loop, manual run, and Telegram quick-buy; locked read-modify-write in `update_task`.
- **Exact Fast Lane Cart Fingerprint**: required exact match across plan, account, duration, pricing mode, quantity, OS, coupons, and options, re-verifying price ceilings at build, revalidation, and hit time.
- **Hardware Non-Downgrade & Account Isolation**: aborted checkout if required RAM/storage addons fail to attach; prevented tasks bound to deleted accounts from silently falling back to the default account.
- **Telegram Single-Use Buy Tokens & Panel Security**: made quick-buy buttons atomic single-use with `+10%` ceiling over catalog price, restricted group button presses to authorized user IDs, redacted secrets on `/api/export`, rotated session signing keys on password change, added login brute-force lockout, and fixed XSS/static-path checks.

---

## [2.0.0] - 2026-11-23

### Added

- **⚡ Prebuilt Cart Fast Lane (`CartSlotPool`)**: prebuilds, configures, and price-checks carts across up to 6 target datacenters with automatic renewal so stock hits execute a single `POST /checkout` call; parallelized `ThreadPoolExecutor` addon attachment on the standard path.
- **🛡️ Uncertain Checkout Fuse (`pending_confirm`)**: distinguishes definite 4xx rejections from ambiguous timeouts/5xx, halting the task in `pending_confirm` without deleting the cart and cross-checking `/me/order`.
- **👻 Phantom Stock Guard & Mandatory Addon Resolution**: validates FQN hardware combinations against official OVH catalogs (`is_config_orderable`), supports `allow_non_default`, and auto-attaches mandatory addon families (`bandwidth-*`).
- **🎰 Order Delivery Tracker & Hardware Lottery Detector (`tracker.py` + `specs.py`)**: tracks `notPaid -> paid -> delivered`, links provisioned dedicated servers, compares actual hardware specs against catalog baselines, alerts on free RAM/disk/CPU upgrades, and adds live CNY currency estimates (`GET /api/fx`).
- **📣 Telegram 2.0 HTML Restock Cards, Sold-Out Strikethrough & `@ovheco` Short-Link Ordering**: HTML cards with OVH Express JSURL checkout links, automatic `editMessageText` strikethrough on sell-out, local deflate `/o/<region>/<token>` decoder (`/order`), `/servers` command, and least-privilege OVH Token creation links with default payment method verification.

---

## [1.0.0] - 2026-11-21

### Added

- Zero-dependency (100% Python 3.9+ standard library) OVHcloud Eco / Baremetal / VPS auto-buyer and stock monitor.
- Single-cart checkout engine with Dry-Run mode, `max_price` budget guard, atomic `max_orders` quota, and dynamic `requiredConfiguration` resolution.
- Shared bulk availability polling, QPS rate limiter, HTTP Keep-Alive pool, and automatic `/auth/time` clock-skew sync.
- Black Friday `new_sku` & `config_match` Radar, multi-datacenter VPS stock monitor, multi-account EU/CA/US credential management, optional at-rest secret encryption, Web Console with SSE live logs, Headless mode, CLI tools, zero-domain Telegram Long Polling Bot, multi-channel Webhooks, and multi-arch (`linux/amd64`, `linux/arm64`) Docker images on GHCR & Docker Hub.
