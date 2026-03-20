"""
usdt_pay.py — USDT (TRC20) Payment SDK

基于 epusdt 支付网关封装的 SDK，提供：
  - 订单创建与 MD5 签名
  - TronGrid 链上交易轮询监控
  - 主动回调通知（webhook）验签处理
  - 二维码生成
  - Telegram 集成助手（框架无关，支持 pyrogram / python-telegram-bot）

快速开始::

    from module.usdt_pay import PaymentConfig, USDTPayClient, TelegramPaymentHelper

    config = PaymentConfig(
        api_url="https://pay.example.com/api/v1/order/create-transaction",
        api_token="your_api_token",
        notify_url="https://your-server.com/pay/notify",
        redirect_url="https://your-server.com/pay/success",
    )

    client = USDTPayClient(config)
    helper = TelegramPaymentHelper(client)

    # 在 Telegram 回调中触发完整支付流程
    await helper.start_payment_flow(
        tg_client=bot,
        chat_id=user_id,
        amount_cny=73.0,
        on_success=my_success_handler,
        on_timeout=my_timeout_handler,
    )
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Awaitable, Callable, Optional

import aiohttp
import qrcode
import requests

logger = logging.getLogger(__name__)


# ─── 数据模型 ─────────────────────────────────────────────────────────────────


@dataclass
class PaymentConfig:
    """支付网关配置。"""

    # epusdt 下单接口
    api_url: str
    # API 鉴权 token（用于 MD5 签名）
    api_token: str
    # 异步回调地址（网关 POST 通知）
    notify_url: str
    # 支付完成后跳转地址
    redirect_url: str
    # TronGrid 查询模板，{wallet} 会被替换为实际钱包地址
    tron_api_url_template: str = (
        "https://api.trongrid.io/v1/accounts/{wallet}/transactions/trc20"
    )
    # 订单超时时间（分钟）
    payment_timeout_minutes: int = 10
    # 轮询间隔（秒）
    poll_interval_seconds: int = 3
    # TronGrid API 最大重试次数
    max_api_retries: int = 10
    # USDT 金额匹配容差
    amount_tolerance: float = 0.01


@dataclass
class PaymentOrder:
    """已创建的支付订单。"""

    order_id: str
    amount_cny: float
    amount_usdt: float
    wallet_address: str
    payment_url: str
    created_at: datetime = field(default_factory=datetime.now)
    expires_at: datetime = field(init=False)

    # expires_at 由 USDTPayClient.create_order 注入
    def __post_init__(self) -> None:
        if not hasattr(self, "_expires_set"):
            self.expires_at = self.created_at  # 占位，会被 create_order 覆盖


class PaymentStatus(Enum):
    PENDING = "pending"
    SUCCESS = "success"
    TIMEOUT = "timeout"
    FAILED = "failed"


@dataclass
class PaymentResult:
    """支付检查结果。"""

    status: PaymentStatus
    order: PaymentOrder
    transaction_id: Optional[str] = None
    error: Optional[str] = None

    @property
    def is_success(self) -> bool:
        return self.status == PaymentStatus.SUCCESS


# 回调类型别名
OnSuccessCallback = Callable[[PaymentResult], Awaitable[None]]
OnTimeoutCallback = Callable[[PaymentResult], Awaitable[None]]
OnFailureCallback = Callable[[PaymentResult], Awaitable[None]]


# ─── 异常 ──────────────────────────────────────────────────────────────────────


class PaymentError(Exception):
    """订单创建或支付处理失败时抛出。"""

    def __init__(self, message: str, raw_response: Optional[dict] = None) -> None:
        super().__init__(message)
        self.raw_response = raw_response


# ─── 核心 SDK ─────────────────────────────────────────────────────────────────


class USDTPayClient:
    """
    USDT (TRC20) 支付客户端。

    职责：
      1. 生成 MD5 签名并向 epusdt 网关下单
      2. 通过 TronGrid 轮询链上交易确认支付
      3. 处理主动 webhook 通知的签名验证
      4. 生成二维码图片

    与任何 Telegram 框架、Web 框架解耦，所有回调由调用方注入。
    """

    def __init__(self, config: PaymentConfig) -> None:
        self.config = config

    # ── 签名 ──────────────────────────────────────────────────────────────────

    def generate_signature(self, params: dict) -> str:
        """
        按照 epusdt 规范生成 MD5 签名。

        规则：过滤空值 → 按参数名 ASCII 升序排列 → 拼接 token → MD5。
        """
        filtered = {k: v for k, v in params.items() if v not in (None, "", 0)}
        sorted_pairs = sorted(filtered.items(), key=lambda x: x[0])
        query_string = "&".join(f"{k}={v}" for k, v in sorted_pairs)
        sign_str = query_string + self.config.api_token
        return hashlib.md5(sign_str.encode("utf-8")).hexdigest().lower()

    def verify_notify_signature(self, payload: dict) -> bool:
        """
        验证网关主动回调（webhook）的签名合法性。

        Args:
            payload: 网关 POST 过来的原始参数字典（包含 signature 字段）。

        Returns:
            True 表示签名合法，False 表示可能被篡改。
        """
        received_sig = payload.get("signature", "")
        params_without_sig = {k: v for k, v in payload.items() if k != "signature"}
        expected_sig = self.generate_signature(params_without_sig)
        return received_sig.lower() == expected_sig.lower()

    # ── 下单 ──────────────────────────────────────────────────────────────────

    async def create_order(
        self,
        amount_cny: float,
        order_id: Optional[str] = None,
    ) -> PaymentOrder:
        """
        向 epusdt 网关创建支付订单。

        Args:
            amount_cny: 订单金额（人民币），网关会自动加 0.01 以唯一化金额。
            order_id:   自定义订单号；留空则自动生成（时间戳 + 随机数）。

        Returns:
            PaymentOrder 包含钱包地址、USDT 金额、支付链接等信息。

        Raises:
            PaymentError: 网关返回错误或网络异常时。
        """
        amount_float = round(float(amount_cny) + 0.01, 2)

        if order_id is None:
            order_id = (
                datetime.now().strftime("%Y%m%d%H%M%S")
                + str(random.randint(1_000_000, 9_999_999))
            )

        params: dict = {
            "order_id": order_id,
            "amount": amount_float,
            "notify_url": self.config.notify_url,
            "redirect_url": self.config.redirect_url,
        }
        params["signature"] = self.generate_signature(params)

        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: requests.post(
                    self.config.api_url, json=params, timeout=15
                ),
            )
            data: dict = response.json()
        except Exception as exc:
            raise PaymentError(f"网关请求失败: {exc}") from exc

        if data.get("status_code") != 200:
            raise PaymentError(
                f"订单创建失败: {data.get('message')} (code={data.get('status_code')})",
                raw_response=data,
            )

        pd = data["data"]
        order = PaymentOrder(
            order_id=order_id,
            amount_cny=amount_float,
            amount_usdt=pd["actual_amount"],
            wallet_address=pd["token"],
            payment_url=pd["payment_url"],
        )
        order.expires_at = order.created_at + timedelta(
            minutes=self.config.payment_timeout_minutes
        )

        logger.info(
            "订单已创建 | id=%s cny=%.2f usdt=%s wallet=%s",
            order_id,
            amount_float,
            order.amount_usdt,
            order.wallet_address,
        )
        return order

    # ── 链上轮询监控 ──────────────────────────────────────────────────────────

    async def watch_payment(
        self,
        order: PaymentOrder,
        on_success: Optional[OnSuccessCallback] = None,
        on_timeout: Optional[OnTimeoutCallback] = None,
        on_failure: Optional[OnFailureCallback] = None,
    ) -> PaymentResult:
        """
        轮询 TronGrid 监控链上 TRC20 交易，直到匹配成功或超时。

        - 匹配成功 → 调用 on_success
        - 超时     → 调用 on_timeout
        - 达到最大重试次数 → 调用 on_failure

        Args:
            order:      要监控的 PaymentOrder。
            on_success: 支付成功时的异步回调。
            on_timeout: 订单超时时的异步回调。
            on_failure: 网络/API 彻底失败时的异步回调。

        Returns:
            PaymentResult 含最终状态。
        """
        api_url = self.config.tron_api_url_template.format(
            wallet=order.wallet_address
        )
        retry_count = 0

        logger.info(
            "开始监控 | id=%s usdt=%s wallet=%s",
            order.order_id,
            order.amount_usdt,
            order.wallet_address,
        )

        while datetime.now() < order.expires_at:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        api_url,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status != 200:
                            logger.warning("TronGrid 返回 %s", resp.status)
                        else:
                            data = await resp.json()
                            for tx in data.get("data", []):
                                result = self._match_transaction(tx, order)
                                if result:
                                    logger.info(
                                        "✅ 有效交易 | tx=%s", result.transaction_id
                                    )
                                    if on_success:
                                        await on_success(result)
                                    return result

                remaining = (order.expires_at - datetime.now()).total_seconds()
                logger.debug("未匹配，剩余 %.0fs | id=%s", remaining, order.order_id)

            except aiohttp.ClientError as exc:
                retry_count += 1
                logger.error(
                    "API 请求错误 (%d/%d): %s",
                    retry_count,
                    self.config.max_api_retries,
                    exc,
                )
                if retry_count >= self.config.max_api_retries:
                    result = PaymentResult(
                        status=PaymentStatus.FAILED,
                        order=order,
                        error=f"超过最大重试次数: {exc}",
                    )
                    if on_failure:
                        await on_failure(result)
                    return result
                await asyncio.sleep(10)
                continue

            except Exception as exc:  # noqa: BLE001
                logger.error("监控异常: %s", exc)

            await asyncio.sleep(self.config.poll_interval_seconds)

        # 超时
        result = PaymentResult(status=PaymentStatus.TIMEOUT, order=order)
        logger.warning("⚠️ 支付超时 | id=%s", order.order_id)
        if on_timeout:
            await on_timeout(result)
        return result

    def _match_transaction(
        self, tx: dict, order: PaymentOrder
    ) -> Optional[PaymentResult]:
        """
        判断单条 TronGrid 交易记录是否与订单匹配。

        Returns:
            匹配成功返回 PaymentResult(SUCCESS)，否则返回 None。
        """
        # 1. 确认状态（部分 API 版本无此字段，有则检查）
        if "confirmed" in tx and not tx["confirmed"]:
            return None

        # 2. 交易时间在订单有效期内
        tx_time = datetime.fromtimestamp(tx.get("block_timestamp", 0) / 1000)
        if not (order.created_at <= tx_time <= order.expires_at):
            return None

        # 3. 金额匹配（微 USDT → USDT）
        tx_value = float(tx.get("value", "0")) / 1e6
        if abs(tx_value - float(order.amount_usdt)) > self.config.amount_tolerance:
            return None

        # 4. 收款地址匹配
        if tx.get("to", "").lower() != order.wallet_address.lower():
            return None

        return PaymentResult(
            status=PaymentStatus.SUCCESS,
            order=order,
            transaction_id=tx.get("transaction_id"),
        )

    # ── 工具 ──────────────────────────────────────────────────────────────────

    def generate_qr_code(
        self,
        data: str,
        box_size: int = 8,
        border: int = 2,
    ) -> io.BytesIO:
        """
        生成二维码 PNG 图片。

        Args:
            data:     要编码的字符串（如钱包地址、支付链接）。
            box_size: 每个格子的像素大小。
            border:   边框宽度（格子数）。

        Returns:
            包含 PNG 图片数据的 BytesIO，指针已重置至起始位置。
        """
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=box_size,
            border=border,
        )
        qr.add_data(data)
        qr.make(fit=True)

        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf


# ─── Telegram 集成助手 ────────────────────────────────────────────────────────


class TelegramPaymentHelper:
    """
    Telegram 支付流程助手。

    封装了「发送支付信息 → 发送二维码 → 后台监控交易」的完整流程。
    框架无关：tg_client 只需支持 send_message / send_photo 两个异步方法，
    可以是 pyrogram.Client、python-telegram-bot 的 Bot，或任何鸭子类型对象。

    使用示例（pyrogram）::

        helper = TelegramPaymentHelper(client)

        async def on_success(result: PaymentResult):
            await bot.send_message(result.order.order_id[:10], "支付成功！")

        await helper.start_payment_flow(
            tg_client=bot,
            chat_id=user_id,
            amount_cny=73.0,
            on_success=on_success,
        )
    """

    def __init__(self, pay_client: USDTPayClient) -> None:
        self.pay_client = pay_client

    async def send_payment_info(
        self,
        tg_client,
        chat_id: int,
        order: PaymentOrder,
    ) -> None:
        """向 Telegram 用户发送订单信息和二维码。"""
        text = (
            f"✅ 订单创建成功!\n\n"
            f"💰 充值金额: **{order.amount_cny} CNY**\n"
            f"💲 USDT金额: **{order.amount_usdt} USDT**\n"
            f"📝 订单号: `{order.order_id}`\n\n"
            f"请向以下钱包地址转账 USDT (TRC20):\n"
            f"`{order.wallet_address}`\n\n"
            f"⚠️ 请确保转账金额与上方 **USDT 金额完全一致**\n"
            f"⏱ 支付有效期: {self.pay_client.config.payment_timeout_minutes} 分钟"
        )
        await tg_client.send_message(chat_id=chat_id, text=text)

        qr_img = self.pay_client.generate_qr_code(order.wallet_address)
        await tg_client.send_photo(
            chat_id=chat_id,
            photo=qr_img,
            caption="📲 扫码向上方地址转账 USDT (TRC20)",
        )

    async def start_payment_flow(
        self,
        tg_client,
        chat_id: int,
        amount_cny: float,
        order_id: Optional[str] = None,
        on_success: Optional[OnSuccessCallback] = None,
        on_timeout: Optional[OnTimeoutCallback] = None,
        on_failure: Optional[OnFailureCallback] = None,
    ) -> None:
        """
        完整支付流程入口：下单 → 通知用户 → 后台监控。

        监控任务以 asyncio.create_task 在后台运行，不阻塞当前协程。

        Args:
            tg_client:  Telegram 客户端 / Bot 对象。
            chat_id:    用户的 Telegram chat_id。
            amount_cny: 订单金额（人民币）。
            order_id:   可选，自定义订单号。
            on_success / on_timeout / on_failure: 异步回调函数。
        """
        try:
            order = await self.pay_client.create_order(
                amount_cny=amount_cny,
                order_id=order_id,
            )
        except PaymentError as exc:
            logger.error("创建订单失败: %s", exc)
            await tg_client.send_message(
                chat_id=chat_id,
                text=f"❌ 订单创建失败，请稍后重试。\n原因：{exc}",
            )
            return

        await self.send_payment_info(tg_client, chat_id, order)

        asyncio.create_task(
            self.pay_client.watch_payment(
                order=order,
                on_success=on_success,
                on_timeout=on_timeout,
                on_failure=on_failure,
            )
        )
        logger.info("后台监控任务已启动 | id=%s chat_id=%s", order.order_id, chat_id)


# ─── Webhook 处理器（主动回调） ───────────────────────────────────────────────


class WebhookHandler:
    """
    处理 epusdt 网关主动 POST 过来的支付回调（notify_url）。

    适配任何 Web 框架（FastAPI、aiohttp、Flask 等），
    调用方只需把请求体解析为 dict 传入 handle() 即可。

    示例（FastAPI）::

        handler = WebhookHandler(client)

        @app.post("/pay/notify")
        async def pay_notify(request: Request):
            payload = await request.json()
            result = await handler.handle(payload, on_success=my_vip_upgrade)
            return {"status": "ok" if result else "sig_error"}
    """

    def __init__(self, pay_client: USDTPayClient) -> None:
        self.pay_client = pay_client

    async def handle(
        self,
        payload: dict,
        on_success: Optional[Callable[[dict], Awaitable[None]]] = None,
        on_failure: Optional[Callable[[dict], Awaitable[None]]] = None,
    ) -> bool:
        """
        验签并分发 webhook 通知。

        Args:
            payload:    网关发来的原始参数字典。
            on_success: 签名合法且状态为成功时调用，参数为原始 payload。
            on_failure: 签名合法但状态非成功时调用，参数为原始 payload。

        Returns:
            True 表示签名验证通过，False 表示签名非法（应返回 400）。
        """
        if not self.pay_client.verify_notify_signature(payload):
            logger.warning("Webhook 签名验证失败 | payload=%s", payload)
            return False

        status = payload.get("status")
        order_id = payload.get("order_id")
        trade_id = payload.get("trade_id")

        logger.info(
            "Webhook 已验证 | order_id=%s trade_id=%s status=%s",
            order_id,
            trade_id,
            status,
        )

        # epusdt 以 status=1 表示支付成功
        if str(status) == "1":
            if on_success:
                await on_success(payload)
        else:
            if on_failure:
                await on_failure(payload)

        return True
