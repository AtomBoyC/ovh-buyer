# Changelog

All notable changes to **OVH Buyer (OVH Sniper)** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/).

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
