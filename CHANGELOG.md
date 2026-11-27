# Changelog

All notable changes to **OVH Buyer (OVH Sniper)** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/).

---

## [2.1.4] - 2026-11-27

### Added

- **Two-Tier Monitoring Proxy Pool (`ProxyPoolManager`: Master Pool + Auto-Selected Active Pool)**:
  - Automatically maintains up to `proxy_max_active` (default `5`) lowest-latency healthy proxies in the Active Pool (`<= proxy_max_latency_ms`) from a larger candidate Master Pool (`monitor_proxies_pool` / `OVH_MONITOR_PROXIES_POOL`);
  - **Staggered 1-by-1 Silent Background Probing**: probes candidates strictly sequentially with randomized jitter delays (`1.5s–4.0s`) rather than concurrent bursts, preventing proxy-gateway concurrency throttling and anti-bot burst signatures;
  - **Instant Hot-Swap Failover & Cooldown Resurrection**: immediately quarantines proxies hitting `403 / 407 / 429` or `proxy_fail_threshold` consecutive errors for `proxy_cooldown_sec`, hot-swaps in the fastest pre-measured standby proxy with zero wait, and re-admits cooled proxies in subsequent probe passes;
  - **Live Active Pool Dashboard & APIs**: added `GET /api/proxy/pool`, `POST /api/proxy/pool/probe`, and `POST /api/proxy/pool/evict` (one-click manual eviction/rotation using credential-safe hashed IDs).

### Changed

- **Modular Configuration Navigation**: refactored the monolithic Settings view under the `Configuration` sidebar section into four focused tabs — **Notification Settings** (unifying Telegram, multi-channel Webhooks, and Push Event switches), **Network Settings**, **Feature Switches**, and **Panel Security**.

### Fixed

- **Strict No-Local-IP-Leak Guard**: when all monitoring pool proxies are in failure cooldown and `monitor_proxy_fallback_direct` is disabled, monitoring requests now strictly abort instead of falling back to direct host connections.
- **Proxy Parser & Tester Enhancements**: added inline per-proxy test results and loading states in Network Settings; supported scheme-prefixed shorthand (`socks5://host:port:user:pass` / `http://host:port:user:pass`), preserved `,` and `;` inside proxy passwords, and omitted `:443` from `Host` headers over SOCKS5/HTTPS tunnels.

---

## [2.1.3] - 2026-11-27

### Added

- **监控专用代理与多代理池轮询系统（零第三方依赖原生支持 `HTTP` / `HTTPS` / `SOCKS5`）**：
  - 在 **抢购任务、黑五雷达、VPS 监控、目录与查货、总览** 各监控页面顶栏新增一键 **「🌐 监控代理」** 配置与 **「⚡ 一键测速验证 OVH 连通性」** 弹窗（`POST /api/proxy/test`，并发实测每条代理到 OVH API 的延迟 ms）；
  - 原生实现纯标准库 **SOCKS5 / SOCKS5H（RFC 1928 + 账号密码认证 RFC 1929，远端 DNS 解析）** 与带认证的 **HTTP / HTTPS 隧道代理**，并支持商业代理常见的 `ip:port:user:pass` 简写格式；
  - 支持 **多行代理池 Round-Robin 自动轮询 + 429/403/断连 30 秒自动熔断切换**，且按单代理独立计算 QPS 限流；
  - **监控与下单默认隔离防风控**：默认仅高频查货/雷达/VPS 监控走代理池，账号建车与结账保持直连（可按需开启「下单也走监控代理池」，或在「OVH 账号」中为单个账号绑定专属固定代理）；
  - **抢购任务、黑五雷达规则、VPS 监控项** 均支持配置单条任务的 `独立监控代理`，并补齐雷达规则与 VPS 监控项的在线 **「编辑」** 功能；导出配置备份时自动对所有代理密码脱敏。

---

## [2.1.2] - 2026-11-27

### Added

- **支持推送到 Telegram 超级群组 / 讨论群组的指定 Topic（话题 `message_thread_id`）**：
  - Web 面板「通知与设置」新增 **「默认话题 Topic ID」**（`TELEGRAM_TOPIC_ID`）与 **「群组授权用户 ID」**（`TELEGRAM_ADMIN_IDS`，在群内授权点击抢购按钮而无需把个人私聊加进重复推送列表）；
  - `Chat ID` 同时支持直接粘贴 Telegram 话题链接（如 `https://t.me/c/2134567890/45`、`https://t.me/group/45`）或简写 `-1002134567890/45`（兼容 `/`、`:`、`#` 分隔），可同时向多个不同群组的不同话题推送，且售罄自动划删除线（`editMessageText`）完全兼容话题消息；
  - Telegram Bot 新增 `/id`（`/topic`）指令，一键查看当前群组 `Chat ID`、话题 `Topic ID` 与个人 `User ID`，并在话题内原位回复所有指令与按钮操作；未授权用户在群内点击按钮时会弹窗提示其个人 ID。

### Fixed

- **保留并展示 Telegram Bot API 在 HTTP 4xx/5xx 时的完整 JSON 错误描述**，当误将单向广播频道当作多话题群组或填错 Topic ID 时，运行日志会给出明确的中文排障提示；对部分旧群组 `General (Topic #1)` 自动兼容回退。

---

## [2.1.1] - 2026-11-27

### Fixed

- **修复 Docker 挂载 `./data:/app/data` 启动报 `sqlite3.OperationalError: unable to open database file` 无限重启的问题**：
  - 原因：旧版 `Dockerfile` 在构建期静态切换到系统用户 `USER appuser`（UID 100），而宿主机首次自动创建的 `./data` 目录归属为 `root:root (0755)`，挂载后覆盖镜像内目录权限导致容器无写入权限。
  - 修复：引入 `scripts/docker-entrypoint.sh` + `su-exec`，容器启动时自动创建并修正 `/app/data` 目录归属（支持通过 `PUID` / `PGID` 自定义宿主机 UID/GID，默认 `1000:1000`），实测可写后再降权运行主进程；若挂载在禁止 `chown` 的特殊文件系统上则自动回退保证正常启动。
- **修复 `HEADLESS=true` 模式下 Docker 健康检查误判 `unhealthy` 的问题**，并支持自定义容器内 `PORT`。
- **修复 `.env` 中配置 `OVH_DATA_DIR` 加载顺序问题**，并在 SQLite 打开失败时输出包含路径、UID/GID 与修复命令的明确诊断提示；在不支持共享内存锁的 NFS/SMB 挂载上自动回退 `journal_mode=DELETE`；`CatalogManager` 在缓存目录只读时自动回退纯内存缓存。
- **CI 新增宿主机 `root:root (0755)` Bind Mount 冒烟测试**，确保挂载卷读写与非 root 降权行为在每次构建中自动验证。
- **加固 Cloudflare Tunnel (`cloudflared`) 与反向代理兼容性，并补充完整排障文档**：
  - 当请求来自本机回环或内网反代时自动识别 `CF-Connecting-IP` / `X-Forwarded-For` 真实访客 IP，防止外部暴力试密把反代网关 IP（如 `127.0.0.1`）误锁死；
  - 登录与登出 Cookie 同时兼容 `X-Forwarded-Proto: https` 与 Cloudflare `CF-Visitor` 头；
  - `/api/logs/stream` SSE 实时日志流新增 `Cache-Control: no-cache, no-transform`、`X-Accel-Buffering: no` 与标准流式连接关闭语义，避免 Cloudflare 边缘压缩缓冲导致日志流卡顿；
  - 在 `README.md` 补充 Cloudflare Tunnel 部署范例以及 `Error 1033`（隧道离线 / UDP 7844 受限加 `--protocol http2` / 旧 DNS CNAME 残留）、`502 Bad Gateway`（Docker 容器内 `127.0.0.1` 网络隔离）与 `403 WAF 拦截` 的排查方案。

---

## [2.1.0] - 2026-11-24

> 界面全新设计 + 一批涉及真实扣款的安全修复，**强烈建议所有用户升级**。数据库结构不变，直接 `docker compose pull && docker compose up -d` 即可。
> v2.0.0 旧版本代码已存档到 [`legacy-v2.0`](https://github.com/AtomBoyC/ovh-buyer/tree/legacy-v2.0) 分支（不再更新，不含以下安全修复）。

### Breaking / 行为变化

- 设了价格上限但取不到购物车价格时，不再下单（之前会跳过检查直接结账）。
- Telegram 群组中，只有 ID 在 `Chat ID` 列表里的**用户**才能点按钮或发命令；请把自己的用户 ID 一并填入。
- Telegram「API 抢购」按钮改为一次性，且必须有参考价，自动建的任务上限为参考价 +10%（可用设置项 `quick_buy_price_margin` 调整）。
- 任务绑定的账号被删除后，任务不再改用默认账号下单。
- 修改面板密码会让所有旧登录会话失效。

### Changed

- **Web 控制台全新设计**：参考 StockHawk 的简洁 SaaS 风格重做界面，支持浅色 / 深色 / 跟随系统三种主题；SVG 图标替换 emoji，导航分组，统计条带，开关式顶栏，选项卡片式表单，标题固定的弹窗，适配移动端。

### Fixed

- **结账请求绝不重发**：`POST /checkout` 遇到超时、断连或 5xx 时不再自动重试或在连接池中重发，避免一次结账生成重复订单。
- **价格未知时的封顶保护**：设置了价格上限但取不到购物车价格时，直接取消下单（普通路径与快速通道都生效）。
- **同一任务并发下单**：任务加了「进行中」锁，主循环、即时检测、TG 一键抢购、手动执行不会同时为同一任务下单；`update_task` 的读改写整体加锁，`pending_confirm` 和已购数量不会被旧数据覆盖。
- **快速通道预建车复用**：现在要求完整配置指纹精确匹配（套餐 / 账号 / 周期 / 定价模式 / 数量 / 系统 / 优惠码 / 配件），并在建车、复检和命中三个时刻都按任务价格上限复核价格；任务暂停或编辑时不会再漏删购物车。
- **硬件不降级**：所需内存 / 硬盘配件匹配不到或挂载失败时，放弃下单，不再按基础配置结账。
- **账号不串用**：任务绑定的账号被删除后，不会自动改用默认账号下单。
- **不确定结账不误关联**：`pending_confirm` 订单不再自动关联账号里最近的一笔订单（那笔可能是别的任务的）。
- **Telegram 一键抢购**：按钮改为一次性；必须有参考价才能创建任务，价格上限自动设为参考价 +10%；群组中只有已授权的用户能点按钮或发命令。
- **安全**：`/api/export` 会给通知密钥和 Webhook 打码；改密码后旧会话全部失效；登录失败多次后暂时锁定；HTTPS 反代下 Cookie 加上 `Secure`；前端转义单引号，服务端忽略客户端传入的 id（修复 XSS）；静态文件路径校验改为严格的目录包含判断。
- **其他**：VPS 监控轮询改为串行执行；短链预览缓存 30 分钟后过期并删除购物车；分页参数非法时不再返回 500。

---

## [2.0.0] - 2026-11-23

> 🔥 **2026 黑五终极进化版**：吸收 NodeSeek 社区最新实战经验（`stockhawk-ovh`、`ovh-eco-telegram-order-bot` 等），全面引入 **预建购物车快速通道**、**结账超时防重单熔断**、**幽灵库存过滤**、**订单交付与硬件中奖自动检测**、**Telegram 售罄自动划删除线 + Express 直达**、**`@ovheco` 短链本地解码下单** 及 **最小权限 Token 体系**，同时保持 **零第三方依赖（纯 Python 3.9+ 标准库）** 与 **v1.0 数据库自动无痛升级**。

### Added

- **⚡ 预建购物车快速通道 (`CartSlotPool`)**：
  - 为启用 `fast_lane` 的实单任务提前为前 6 个目标机房建好并绑定购物车、写入配置/配件与实时校验价格，后台自动保活与到期前无缝重建。
  - 一旦检测到补货，直接跳过建车/配参/挂配件，**1 次 API 请求（`POST /checkout`）秒速结账**，并记录毫秒级耗时 (`checkout_ms`)。
  - 普通慢路径建车也升级为 `ThreadPoolExecutor` 并发提交 `configuration` 与 `options`，大幅缩短建车延迟。
- **🛡️ 结账超时/5xx 防重单熔断 (`pending_confirm`)**：
  - 区分明确拒绝（4xx，安全释放购物车）与不确定异常（超时、断连、5xx）。
  - 遇到不确定结账异常时：**严禁自动重试、不删除购物车**，自动反查 `/me/order` 近期订单记录，立即将任务暂停为 `pending_confirm`（待核对）状态并发送高优先级告警，彻底杜绝网络抖动导致的重复下单扣款。
- **👻 幽灵库存识别过滤 & 必选配件护航**：
  - 新增 `is_config_orderable()` 校验库存 API 返回的内存/硬盘 FQN 是否真实存在于 OVH 商品目录，过滤 2026 KS-LE-3（如 `27skle03-v1`）等假库存，并拒绝静默降级到默认配件。
  - 新增 `allow_non_default` 开关（可限制仅抢默认标配，避免高价加配）。
  - `resolve_cart_addons()` 自动补齐所有 `mandatory` 必选配件家族（如 `bandwidth-*`），新增 `intersect_common_terms()` 计算主商品与全部配件的公共 `(duration, pricingMode)` 交集。
- **🎰 订单交付跟踪 & OVH 硬件「中奖」自动检测 (`tracker.py` + `specs.py`)**：
  - 后台只读跟踪器自动轮询未完成订单状态（`/me/order/{id}/status`），在「已付款」「已交付」关键节点主动推送通知。
  - 服务器交付后自动关联主机 `serviceName`（通过订单明细 `domain` 或开通时间+机房匹配），拉取实机硬件 `/dedicated/server/{name}/specifications/hardware`，对比官方目录标配自动判定 **内存扩容 / 硬盘升级（如 HDD→SSD→NVMe）/ CPU 升级中奖**，推送 `🎰🎉 恭喜中奖` 喜报并在订单页高亮展示。
  - 新增实时汇率转换（`GET /api/fx`，ECB/Frankfurter + 离线兜底），在目录、订单、短链预览、Telegram 卡片中自动标注人民币估算价（`≈ ¥xxx`）。
- **📣 Telegram 2.0 HTML 补货卡片 + 售罄自动划删除线 + Express 直达**：
  - 升级补货推送为 HTML 结构化卡片，附带实时人民币估价、API 一键抢购按钮与 **OVH 官网 Express 直达结账按钮**（JSURL 编码）。
  - 补货售罄后自动通过 `editMessageText` 将原消息划上删除线 `<s>...</s>` 并标记「已被抢完」，频道/群组不再被过期补货误导。
- **🔗 `@ovheco` 短链本地解码下单 + 我的独服列表**：
  - 支持直接解析 `@ovheco` / `PoorVPS` 格式 `/o/<region>/<token>` 短链（纯本地 Raw Deflate + Base64URL 解码，不访问任何第三方服务器），先建车展示实时含税价与人民币估价，二次确认后生成官方未付款订单（带防重复点击锁）。
  - Telegram 新增 `/order <短链>` 与 `/servers` 指令；Web 控制台「订单与历史」页新增短链解析下单卡片与「🖥️ 我的独服列表」一键查询。
- **🛡️ 最小权限预填 Token 入口 + 默认支付方式体检**：
  - 升级 EU / CA / US Token 申请链接，预填最小权限清单（仅账号只读、购物车下单、独服配置只读，绝不申请删机/重启权限）。
  - 「测试连通」自动检查 `/me/payment/method`，展示已绑定的默认支付方式（如 `PAYPAL`）或在未绑定时给出 `⚠️ 无默认支付` 提醒。

### Changed

- **数据库 v1 → v2 自动无痛迁移**：保留全部 v1.0 已有账号、任务、订单与设置，启动时自动幂等补齐所有新字段与默认配置，旧历史订单不会触发重复交付通知。
- **Web 控制台全面升级至 v2.0**：顶栏新增「⚡ 快速通道」全局开关；任务表单新增快速通道与非默认配置选项，列表展示预建车就绪机房与剩余有效期；查库存弹窗标注「⚠️ 幽灵库存」并提供官网 Express 直达按钮。

---

## [1.0.0] - 2026-11-21

### Added

- 零第三方依赖（纯 Python 3.9+ 标准库）OVH 黑五独服 / Eco / VPS 自动抢购与监控系统。
- 单购物车直通下单引擎，内置 Dry-Run 演练模式、价格封顶保护 (`max_price`)、原子购买配额 (`max_orders`)、动态 `requiredConfiguration` 适配。
- 共享批量库存轮询（≥3 型号自动合并单次全局请求）、QPS 令牌桶、Keep-Alive 连接池、`/auth/time` 自动校偏。
- 黑五新品上新雷达 (`new_sku`) 与配置指纹狙击 (`config_match`)，以及 VPS 多机房补货监控。
- 多账号管理（EU / CA / US）、可选凭据静态加密、暗黑运维风 Web 控制台（密码/API-Key 门禁 + SSE 实时日志流）、无头模式与 CLI 工具。
- 零域名 Telegram Long Polling Bot（支持 `/status`、`/tasks`、`/check`、`/orders`、`/pause`、`/resume` 及内联一键抢购按钮）及 Bark / Server酱 / 钉钉 / 飞书 / Discord / 自定义 Webhook 多渠道通知。
- 双架构 Docker 镜像（`linux/amd64` + `linux/arm64`）自动发布至 GHCR 与 Docker Hub。
