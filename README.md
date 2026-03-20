# USDT Pay SDK 使用文档

> 文件位置：`usdt_pay.py`
> 适用支付网关：epusdt（TRC20 USDT 收款）

---

## 目录

1. [SDK 结构总览](#1-sdk-结构总览)
2. [配置说明与配置位置](#2-配置说明与配置位置)
3. [快速开始（最简示例）](#3-快速开始最简示例)
4. [核心 API 详解](#4-核心-api-详解)
5. [集成到现有 bot.py 项目](#5-集成到现有-botpy-项目)
6. [Webhook 主动回调接入](#6-webhook-主动回调接入)
7. [常见问题](#7-常见问题)

---

## 1. SDK 结构总览

```
module/usdt_pay.py
├── PaymentConfig        # 配置数据类（所有参数只填这里）
├── PaymentOrder         # 已创建订单的快照（只读）
├── PaymentStatus        # 枚举：PENDING / SUCCESS / TIMEOUT / FAILED
├── PaymentResult        # 监控/回调的结果对象
├── PaymentError         # 订单创建失败时的专用异常
│
├── USDTPayClient        # 核心客户端
│   ├── generate_signature()        # MD5 签名
│   ├── verify_notify_signature()   # 验证 Webhook 回调签名
│   ├── create_order()              # 异步下单
│   ├── watch_payment()             # 异步轮询 TronGrid 确认收款
│   └── generate_qr_code()         # 生成钱包地址二维码
│
├── TelegramPaymentHelper           # Telegram 业务助手（可选）
│   ├── send_payment_info()         # 发送订单信息 + 二维码给用户
│   └── start_payment_flow()        # 完整流程一键触发
│
└── WebhookHandler                  # Webhook 主动回调处理器（可选）
    └── handle()                    # 验签 + 分发成功/失败事件
```

---

## 2. 配置说明与配置位置

### 2.1 配置参数说明

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `api_url` | str | ✅ | epusdt 下单接口地址 |
| `api_token` | str | ✅ | API 鉴权 token，用于 MD5 签名 |
| `notify_url` | str | ✅ | 支付成功后网关回调你服务器的地址（需公网可访问） |
| `redirect_url` | str | ✅ | 支付完成后的页面跳转地址 |
| `tron_api_url_template` | str | | TronGrid 查询地址模板，`{wallet}` 自动替换。默认已填好 |
| `payment_timeout_minutes` | int | | 订单超时时间（分钟），默认 `10` |
| `poll_interval_seconds` | int | | 轮询间隔（秒），默认 `3` |
| `max_api_retries` | int | | TronGrid 最大重试次数，默认 `10` |
| `amount_tolerance` | float | | USDT 金额匹配误差容忍，默认 `0.01` |

---

### 2.2 ✅ 推荐：配置写入 `bot.yaml`

项目本身通过根目录的 `bot.yaml` 管理配置，**直接在那里追加支付相关字段即可**，不需要新建文件。

打开根目录的 `bot.yaml`（没有就新建），添加以下内容：

```yaml
# ── 原有配置（示例，保持不动）──────────────────
download_filter: []

# ── 新增：USDT 支付配置 ─────────────────────────
payment:
  api_url: "https://pay.你的域名.com/api/v1/order/create-transaction"
  api_token: "你的API_TOKEN"          # 在 epusdt 后台获取
  notify_url: "https://你的域名/pay/notify"  # 必须公网可访问
  redirect_url: "https://你的域名/pay/success"
  vip_price_cny: 73                   # VIP 月费（人民币）
  payment_timeout_minutes: 10
  poll_interval_seconds: 3
```

然后在 `bot.py` 的 `assign_config()` 方法里读取并构建 `PaymentConfig`：

```python
# module/bot.py — assign_config 方法内追加
from module.usdt_pay import PaymentConfig, USDTPayClient

def assign_config(self, _config: dict):
    self.download_filter = _config.get("download_filter", self.download_filter)

    # 读取支付配置
    pay_cfg = _config.get("payment", {})
    if pay_cfg:
        self.pay_config = PaymentConfig(
            api_url=pay_cfg.get("api_url", self.api_url),
            api_token=pay_cfg.get("api_token", self.api_token),
            notify_url=pay_cfg.get("notify_url", self.notify_url),
            redirect_url=pay_cfg.get("redirect_url", self.redirect_url),
            payment_timeout_minutes=pay_cfg.get("payment_timeout_minutes", 10),
            poll_interval_seconds=pay_cfg.get("poll_interval_seconds", 3),
        )
        self.pay_client = USDTPayClient(self.pay_config)

    return True
```

---

### 2.3 备选：代码内直接硬编码（快速测试用）

不适合生产环境，但调试方便：

```python
from module.usdt_pay import PaymentConfig, USDTPayClient

config = PaymentConfig(
    api_url="https://pay.你的域名.com/api/v1/order/create-transaction",
    api_token="your_token",
    notify_url="http://pay.你的域名.com",
    redirect_url="http://pay.你的域名.com",
)
client = USDTPayClient(config)
```

---

## 3. 快速开始（最简示例）

### 场景：用户点击「购买 VIP」后触发完整支付流程

```python
from module.usdt_pay import PaymentConfig, USDTPayClient, TelegramPaymentHelper, PaymentResult

# 1. 初始化（一般在 bot 启动时做一次）
config = PaymentConfig(
    api_url="https://pay.你的域名.com/api/v1/order/create-transaction",
    api_token="your_token",
    notify_url="https://your-server.com/pay/notify",
    redirect_url="https://your-server.com/pay/success",
)
pay_client = USDTPayClient(config)
helper = TelegramPaymentHelper(pay_client)


# 2. 定义回调
async def on_payment_success(result: PaymentResult):
    user_id = result.order.order_id  # 你自己传入的 order_id 可以带 user_id
    # 在这里开通 VIP、写数据库、发通知等……
    await bot.send_message(
        chat_id=YOUR_CHAT_ID,
        text=f"🎉 支付成功！交易ID: {result.transaction_id}"
    )

async def on_payment_timeout(result: PaymentResult):
    await bot.send_message(chat_id=YOUR_CHAT_ID, text="⚠️ 订单已超时，请重新发起支付")


# 3. 在 Telegram 回调中一键触发（不阻塞，后台轮询）
await helper.start_payment_flow(
    tg_client=bot,           # pyrogram.Client 或 python-telegram-bot 的 Bot
    chat_id=user_id,
    amount_cny=73.0,
    on_success=on_payment_success,
    on_timeout=on_payment_timeout,
)
```

运行效果：
1. 自动下单，金额自动 +0.01 确保唯一化
2. 向用户发送订单信息文本
3. 向用户发送钱包地址二维码
4. 后台启动 asyncio 任务轮询 TronGrid，10 分钟内每 3 秒检查一次
5. 收到转账 → 调用 `on_payment_success`；超时 → 调用 `on_payment_timeout`

---

## 4. 核心 API 详解

### 4.1 `USDTPayClient.create_order()`

```python
order = await pay_client.create_order(
    amount_cny=73.0,          # 人民币金额
    order_id="MY_ORDER_001",  # 可选，不填自动生成
)

print(order.order_id)         # "MY_ORDER_001"
print(order.amount_cny)       # 73.01（自动加 0.01）
print(order.amount_usdt)      # 10.23（网关换算后的 USDT 金额）
print(order.wallet_address)   # "TXXX..."（本次订单专属收款地址）
print(order.payment_url)      # "https://pay.xxx.com/..."（网关支付页）
print(order.expires_at)       # datetime，10 分钟后到期
```

失败时抛出 `PaymentError`：

```python
try:
    order = await pay_client.create_order(73.0)
except PaymentError as e:
    print(f"下单失败: {e}")
    print(f"原始响应: {e.raw_response}")
```

---

### 4.2 `USDTPayClient.watch_payment()`

不依赖 Telegram，纯粹的支付监控，适合有自己通知逻辑的场景：

```python
result = await pay_client.watch_payment(
    order=order,
    on_success=my_success_handler,
    on_timeout=my_timeout_handler,
    on_failure=my_failure_handler,  # 网络彻底不通时
)

if result.is_success:
    print(f"✅ 链上交易ID: {result.transaction_id}")
else:
    print(f"状态: {result.status.value}")  # "timeout" / "failed"
```

> **注意**：`watch_payment` 是阻塞协程，在 Telegram 场景下应用 `asyncio.create_task()` 包裹（`start_payment_flow` 内部已自动处理）。

---

### 4.3 `USDTPayClient.verify_notify_signature()`

在 Webhook 接口中验证网关回调是否合法：

```python
is_valid = pay_client.verify_notify_signature(payload)
if not is_valid:
    return Response(status_code=400)
```

---

### 4.4 `USDTPayClient.generate_qr_code()`

独立生成二维码，返回 `io.BytesIO`，可直接发送给 Telegram 或保存为文件：

```python
qr = pay_client.generate_qr_code(order.wallet_address, box_size=10, border=4)

# pyrogram
await bot.send_photo(chat_id=user_id, photo=qr)

# 保存为文件
with open("qr.png", "wb") as f:
    f.write(qr.read())
```

---

## 5. 集成到现有 bot.py 项目

现有 `bot.py` 中的 `create_payment` / `check_payment` 可以**逐步替换**为 SDK，不必一次性全改。

### 第一步：在 `DownloadBot.__init__` 里初始化 SDK 客户端

```python
# module/bot.py — __init__ 末尾添加
from module.usdt_pay import PaymentConfig, USDTPayClient, TelegramPaymentHelper

self.pay_config = PaymentConfig(
    api_url=self.api_url,
    api_token=self.api_token,
    notify_url=self.notify_url,
    redirect_url=self.redirect_url,
    tron_api_url_template=(
        "https://api.trongrid.io/v1/accounts/{wallet}/transactions/trc20"
    ),
)
self.pay_client = USDTPayClient(self.pay_config)
self.pay_helper = TelegramPaymentHelper(self.pay_client)
```

### 第二步：替换触发支付的入口

在 `on_query_handler` 或 `handle_button_press` 中，把原来的 `self.create_payment(user_id, amount)` 换成：

```python
async def on_buy_vip(user_id: int):
    async def on_success(result):
        await self.update_user_vip(user_id)         # 原有 VIP 开通逻辑
        await self.get_user_info(user_id, show_message=True)

    async def on_timeout(result):
        await self.bot.send_message(
            chat_id=user_id,
            text="⚠️ 订单超时，请重新点击购买"
        )

    await self.pay_helper.start_payment_flow(
        tg_client=self.bot,
        chat_id=user_id,
        amount_cny=self.vip_price,
        order_id=f"VIP_{user_id}_{int(datetime.now().timestamp())}",
        on_success=on_success,
        on_timeout=on_timeout,
    )
```

---

## 6. Webhook 主动回调接入

如果你的服务器运行了 Web 服务（推荐 FastAPI），可以用 `WebhookHandler` 处理网关主动 POST 回调，**无需轮询 TronGrid**，更可靠。

### FastAPI 示例

```python
# module/web.py 或单独的 webhook_server.py
from fastapi import FastAPI, Request, HTTPException
from module.usdt_pay import PaymentConfig, USDTPayClient, WebhookHandler

app = FastAPI()

config = PaymentConfig(
    api_url="https://pay.你的域名.com/api/v1/order/create-transaction",
    api_token="your_token",
    notify_url="https://your-server.com/pay/notify",
    redirect_url="https://your-server.com/pay/success",
)
pay_client = USDTPayClient(config)
webhook = WebhookHandler(pay_client)


@app.post("/pay/notify")
async def pay_notify(request: Request):
    payload = await request.json()

    async def on_success(data: dict):
        order_id = data.get("order_id")
        trade_id = data.get("trade_id")
        # 根据 order_id 找到对应用户，开通 VIP
        user_id = int(order_id.split("_")[1])  # 如果 order_id 是 "VIP_用户ID_时间戳"
        from module.bot import _bot
        await _bot.update_user_vip(user_id)

    is_valid = await webhook.handle(payload, on_success=on_success)
    if not is_valid:
        raise HTTPException(status_code=400, detail="invalid signature")

    return {"status": "ok"}
```

> **epusdt 回调字段说明**：
> | 字段 | 说明 |
> |------|------|
> | `order_id` | 你创建订单时传入的 order_id |
> | `trade_id` | 网关内部交易 ID |
> | `status` | `1` = 支付成功，其他 = 失败/处理中 |
> | `actual_amount` | 实际收到的 USDT 金额 |
> | `signature` | MD5 签名，用于验签 |

---

## 7. 常见问题

**Q：`notify_url` 填什么？**
A：必须是公网可访问的 HTTPS 地址，epusdt 网关会 POST 请求这个地址通知你支付结果。本地开发可用 [ngrok](https://ngrok.com/) 临时暴露端口。

**Q：轮询和 Webhook 哪个更好？**
A：Webhook 更可靠（网关主动推送），轮询作为兜底或在没有公网 IP 时使用。两者可以同时开启，`watch_payment` 和 `WebhookHandler` 互不干扰。

**Q：如何区分不同用户的订单？**
A：在调用 `create_order()` 时传入自定义 `order_id`，建议格式：`VIP_{user_id}_{timestamp}`，这样在回调中可以直接解析出用户 ID。

**Q：金额为什么会自动 +0.01？**
A：epusdt 要求每笔订单的 USDT 金额唯一（精确到分），SDK 自动加 0.01 避免同一时间多个用户产生相同金额订单导致冲突。

**Q：`PaymentError` 和普通 Exception 有什么区别？**
A：`PaymentError` 仅在下单阶段（`create_order`）失败时抛出，携带 `raw_response` 字段方便调试。支付监控阶段的错误通过 `on_failure` 回调传递，不抛异常。
