"""24/7 read-only funding-arbitrage monitor for Hyperliquid.

Combines public REST market data with public WebSocket funding updates,
reconnects automatically, and optionally sends Telegram alerts. It never
places orders.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import requests
import websockets

INFO_URL = "https://api.hyperliquid.xyz/info"
WS_URL = "wss://api.hyperliquid.xyz/ws"
POLL_SECONDS = float(os.getenv("MARKET_REFRESH_SECONDS", "15"))
MIN_NET_APR = float(os.getenv("MIN_NET_APR", "0.10"))
EXPECTED_HOLDING_HOURS = float(os.getenv("EXPECTED_HOLDING_HOURS", "24"))
MIN_NOTIONAL_USDC = float(os.getenv("MIN_NOTIONAL_USDC", "0"))
ALLOW_SHORT_SPOT = os.getenv("ALLOW_SHORT_SPOT", "0") == "1"
SPOT_FEE = float(os.getenv("SPOT_TAKER_FEE", "0.00035"))
PERP_FEE = float(os.getenv("PERP_TAKER_FEE", "0.00035"))
SLIPPAGE_PER_LEG = float(os.getenv("SLIPPAGE_PER_LEG", "0.0005"))
HOURS_PER_YEAR = 24.0 * 365.0
ALERT_COOLDOWN = float(os.getenv("ALERT_COOLDOWN_SECONDS", "1800"))

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hyperliquid-funding")


@dataclass
class Market:
    pair: str
    coin: str
    funding_hour: float
    perp_price: float
    spot_price: float
    open_interest: float = 0.0


def number(value: Any) -> float | None:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def post_info(payload: dict[str, Any]) -> Any:
    response = requests.post(INFO_URL, json=payload, timeout=15)
    response.raise_for_status()
    return response.json()


def load_markets() -> dict[str, Market]:
    perp_meta, perp_ctx = post_info({"type": "metaAndAssetCtxs"})
    spot_meta, spot_ctx = post_info({"type": "spotMetaAndAssetCtxs"})
    perps: dict[str, tuple[str, dict[str, Any]]] = {}
    for meta, ctx in zip(perp_meta.get("universe", []), perp_ctx):
        coin = str(meta.get("name", ""))
        if coin and not meta.get("isDelisted", False):
            perps[coin.upper()] = (coin, ctx)
    result: dict[str, Market] = {}
    for meta, ctx in zip(spot_meta.get("universe", []), spot_ctx):
        pair = str(meta.get("name", ""))
        if meta.get("isDelisted", False) or "/" not in pair:
            continue
        base, quote = pair.split("/", 1)
        if quote.upper() not in {"USDC", "USD"} or base.upper() not in perps:
            continue
        coin, pctx = perps[base.upper()]
        funding = number(pctx.get("funding"))
        perp = number(pctx.get("markPx")) or number(pctx.get("oraclePx"))
        spot = number(ctx.get("midPx")) or number(ctx.get("markPx"))
        oi = number(pctx.get("openInterest")) or 0.0
        if funding is None or perp is None or spot is None or min(perp, spot) <= 0:
            continue
        result[coin] = Market(pair, coin, funding, perp, spot, oi)
    return result


def net_apr(funding_hour: float) -> tuple[float, float]:
    gross = abs(funding_hour) * HOURS_PER_YEAR
    round_trip = 2.0 * (SPOT_FEE + PERP_FEE + 2.0 * SLIPPAGE_PER_LEG)
    annual_cost = round_trip / max(EXPECTED_HOLDING_HOURS, 1e-9) * HOURS_PER_YEAR
    return gross, gross - annual_cost


def opportunity(market: Market) -> dict[str, Any] | None:
    if market.open_interest * market.perp_price < MIN_NOTIONAL_USDC:
        return None
    if market.funding_hour > 0:
        direction = "LONG spot + SHORT perp"
    elif market.funding_hour < 0 and ALLOW_SHORT_SPOT:
        direction = "SHORT spot + LONG perp (borrow required)"
    else:
        return None
    gross, net = net_apr(market.funding_hour)
    if net < MIN_NET_APR:
        return None
    return {"pair": market.pair, "coin": market.coin, "funding_hour": market.funding_hour,
            "gross_apr": gross, "net_apr": net,
            "basis": market.perp_price / market.spot_price - 1,
            "spot_price": market.spot_price, "perp_price": market.perp_price,
            "direction": direction}


def format_signal(row: dict[str, Any]) -> str:
    return (f"SIGNAL {row['pair']} | {row['direction']} | "
            f"funding {row['funding_hour']:+.5%}/h | "
            f"gross APR {row['gross_apr']:+.2%} | net APR {row['net_apr']:+.2%} | "
            f"basis {row['basis']:+.2%}")


def telegram_send(text: str) -> None:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": text}, timeout=10).raise_for_status()
    except requests.RequestException as exc:
        log.warning("Telegram notification failed: %s", exc)


async def run() -> None:
    markets: dict[str, Market] = {}
    last_alert: dict[str, float] = {}
    changed = asyncio.Event()

    async def refresh() -> None:
        nonlocal markets
        while True:
            try:
                markets = await asyncio.to_thread(load_markets)
                changed.set()
                log.info("loaded %d spot/perpetual markets", len(markets))
            except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
                log.warning("market refresh failed: %s", exc)
            await asyncio.sleep(POLL_SECONDS)

    async def evaluate() -> None:
        while True:
            await changed.wait()
            changed.clear()
            now = time.monotonic()
            for market in list(markets.values()):
                row = opportunity(market)
                if not row or now - last_alert.get(market.coin, 0) < ALERT_COOLDOWN:
                    continue
                last_alert[market.coin] = now
                message = format_signal(row)
                log.warning(message)
                await asyncio.to_thread(telegram_send, message)

    async def funding_stream() -> None:
        delay = 1.0
        while True:
            try:
                while not markets:
                    await asyncio.sleep(1)
                async with websockets.connect(
                    WS_URL, ping_interval=20, ping_timeout=20,
                    close_timeout=5, max_size=2**20
                ) as ws:
                    delay = 1.0
                    for coin in markets:
                        await ws.send(json.dumps({
                            "method": "subscribe",
                            "subscription": {"type": "activeAssetCtx", "coin": coin},
                        }))
                    log.info("funding WebSocket connected (%d subscriptions)", len(markets))
                    async for raw in ws:
                        message = json.loads(raw)
                        data = message.get("data", {})
                        coin, ctx = data.get("coin"), data.get("ctx", {})
                        if coin in markets and isinstance(ctx, dict):
                            funding = number(ctx.get("funding"))
                            mark = number(ctx.get("markPx"))
                            if funding is not None:
                                markets[coin].funding_hour = funding
                            if mark and mark > 0:
                                markets[coin].perp_price = mark
                            changed.set()
            except (OSError, asyncio.TimeoutError, websockets.WebSocketException,
                    json.JSONDecodeError) as exc:
                log.warning("WebSocket disconnected: %s; retrying in %.0fs", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)

    await asyncio.gather(refresh(), evaluate(), funding_stream())


def main() -> None:
    log.info("Hyperliquid funding scanner started; Ctrl+C to stop")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("stopped")


if __name__ == "__main__":
    main()
