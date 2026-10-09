"""Hyperliquid spot/perpetual funding arbitrage executor.

LIVE_TRADING is disabled unless the environment variable is exactly 1.
Use a dedicated wallet and test with a very small size first.
"""
from __future__ import annotations

import os
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

from funding_scanner import scan

LIVE_TRADING = os.getenv("LIVE_TRADING", "0") == "1"
PRIVATE_KEY = os.environ.get("HL_PRIVATE_KEY", "")
API_URL = constants.MAINNET_API_URL

POLL_SECONDS = 15
MIN_NET_APY = 0.10
MAX_NOTIONAL_USDC = 300.0
MAX_ONE_TRADE = 1
ENTRY_SLIPPAGE = 0.003
EXIT_ON_ERROR = True


def floor_to(value: float, decimals: int) -> float:
    step = Decimal("1") / (Decimal(10) ** decimals)
    return float(Decimal(str(value)).quantize(step, rounding=ROUND_DOWN))


def account_address() -> str:
    if not PRIVATE_KEY:
        raise RuntimeError("HL_PRIVATE_KEY is not set")
    return Account.from_key(PRIVATE_KEY).address


def make_clients() -> tuple[Info, Exchange]:
    address = account_address()
    info = Info(API_URL, skip_ws=True)
    exchange = Exchange(Account.from_key(PRIVATE_KEY), API_URL)
    print(f"Wallet: {address}")
    return info, exchange


def perp_size_decimals(info: Info, coin: str) -> int:
    meta = info.meta()
    for market in meta["universe"]:
        if market["name"] == coin:
            return int(market.get("szDecimals", 4))
    return 4


def user_has_perp_position(info: Info, address: str, coin: str) -> bool:
    state = info.user_state(address)
    for item in state.get("assetPositions", []):
        position = item.get("position", {})
        if position.get("coin") == coin and abs(float(position.get("szi", 0))) > 0:
            return True
    return False


def limit_ioc(exchange: Exchange, coin: str, is_buy: bool, size: float, price: float):
    # IOC avoids leaving an unfilled limit order on the book.
    return exchange.order(
        coin,
        is_buy,
        size,
        price,
        {"limit": {"tif": "Ioc"}},
        reduce_only=False,
    )


def close_perp(exchange: Exchange, coin: str, size: float, price: float):
    return exchange.order(
        coin,
        True,                         # short perp is closed by buying
        size,
        price * (1 + ENTRY_SLIPPAGE),
        {"limit": {"tif": "Ioc"}},
        reduce_only=True,
    )


def execute_one(info: Info, exchange: Exchange, address: str, row: dict[str, Any]) -> None:
    coin = row["perp"]
    pair = row["pair"]
    notional = min(float(row["max_notional_usdc"]), MAX_NOTIONAL_USDC)
    perp_decimals = perp_size_decimals(info, coin)
    size = floor_to(notional / float(row["perp_price"]), perp_decimals)
    if size <= 0:
        print(f"{pair}: size rounds to zero")
        return

    if user_has_perp_position(info, address, coin):
        print(f"{pair}: skipped, an existing perp position was found")
        return

    print(
        f"SIGNAL {pair}: funding={row['funding_hour']:.5%}/h, "
        f"net APY={row['net_apy']:.2%}, size={size}, "
        f"notional≈${size * row['perp_price']:.2f}"
    )
    if not LIVE_TRADING:
        print("DRY RUN: no orders sent (set LIVE_TRADING=1 to enable)")
        return

    spot_price = float(row["spot_price"])
    perp_price = float(row["perp_price"])
    spot_buy_price = spot_price * (1 + ENTRY_SLIPPAGE)
    perp_sell_price = perp_price * (1 - ENTRY_SLIPPAGE)

    # Spot first, then the short perp hedge.  If the hedge fails, unwind spot.
    spot_result = limit_ioc(exchange, pair, True, size, spot_buy_price)
    try:
        perp_result = limit_ioc(exchange, coin, False, size, perp_sell_price)
    except Exception:
        if EXIT_ON_ERROR:
            try:
                limit_ioc(exchange, pair, False, size, spot_price * (1 - ENTRY_SLIPPAGE))
            except Exception as unwind_error:
                raise RuntimeError(
                    f"Perp leg failed and spot unwind also failed: {unwind_error}"
                )
        raise

    print(f"OPENED {pair}: spot={spot_result} perp={perp_result}")


def main() -> None:
    if LIVE_TRADING and not PRIVATE_KEY:
        raise RuntimeError("LIVE_TRADING=1 requires HL_PRIVATE_KEY")
    if not LIVE_TRADING:
        print("DRY RUN mode: no orders will be sent")

    info, exchange = make_clients() if PRIVATE_KEY else (None, None)
    address = account_address() if PRIVATE_KEY else ""
    trades = 0

    while trades < MAX_ONE_TRADE:
        rows = [row for row in scan() if row["net_apy"] >= MIN_NET_APY]
        if not rows:
            print("No qualifying opportunity")
            time.sleep(POLL_SECONDS)
            continue

        for row in rows:
            if trades >= MAX_ONE_TRADE:
                break
            execute_one(info, exchange, address, row)
            trades += 1
        if not LIVE_TRADING:
            break

    print("Stopped after the configured trade limit")


if __name__ == "__main__":
    main()
