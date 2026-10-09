"""Realtime funding-arbitrage scanner for Hyperliquid.

The scanner only reads public market data. It does not place orders.
"""

from __future__ import annotations
import time
from datetime import datetime, timezone
from typing import Any
import requests
INFO_URL = "https://api.hyperliquid.xyz/info" #hy
# Tune these values to your account and actual fee tier.
POLL_SECONDS = 19
MIN_NET_APY = 0.05         # 10% annualized after estimated costs
SPOT_TAKER_FEE = 0.00035    # 0.035%, example only
PERP_TAKER_FEE = 0.00035    # 0.035%, example only
SLIPPAGE_PER_LEG = 0.0050  # 0.05%, estimate per entry/exit leg
HOURS_PER_YEAR = 24 * 365   # Hyperliquid funding is hourly

session = requests.Session()


def info(payload: dict[str, Any]) -> Any:
    response = session.post(INFO_URL, json=payload, timeout=10)
    response.raise_for_status()
    return response.json()


def as_float(value: Any) -> float | None:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def scan() -> list[dict[str, Any]]:
    perp_meta, perp_contexts = info({"type": "metaAndAssetCtxs"})
    spot_meta, spot_contexts = info({"type": "spotMetaAndAssetCtxs"})

    perp_by_coin: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for market, context in zip(perp_meta["universe"], perp_contexts):
        coin = market.get("name", "")
        if coin and not market.get("isDelisted", False):
            perp_by_coin[coin.upper()] = (market, context)

    # Entry + exit for both legs: spot and perp.
    round_trip_cost = 2 * (
        SPOT_TAKER_FEE + PERP_TAKER_FEE + 2 * SLIPPAGE_PER_LEG
    )
    opportunities: list[dict[str, Any]] = []

    for spot_market, spot_context in zip(
        spot_meta["universe"], spot_contexts
    ):
        if spot_market.get("isDelisted", False):
            continue

        pair = spot_market.get("name", "")
        if "/" not in pair:
            continue
        base, quote = pair.split("/", 1)
        if quote.upper() != "USDC":
            continue

        perp_item = perp_by_coin.get(base.upper())
        if perp_item is None:
            continue
        perp_market, perp_context = perp_item

        # Positive funding pays the short perpetual leg:
        # buy spot + short perp. Negative funding would require shorting spot.
        funding_hour = as_float(perp_context.get("funding"))
        mark_price = as_float(perp_context.get("markPx"))
        oracle_price = as_float(perp_context.get("oraclePx"))
        spot_price = (
            as_float(spot_context.get("midPx"))
            or as_float(spot_context.get("markPx"))
        )
        if funding_hour is None or mark_price is None or oracle_price is None:
            continue
        if spot_price is None or min(mark_price, oracle_price, spot_price) <= 0:
            continue

        # Only this direction is generally implementable without spot borrowing.
        if funding_hour <= 0:
            continue

        gross_apy = funding_hour * HOURS_PER_YEAR
        net_apy = gross_apy - round_trip_cost
        basis = mark_price / spot_price - 1.0

        if net_apy < MIN_NET_APY:
            continue

        opportunities.append(
            {
                "pair": pair,
                "perp": perp_market["name"],
                "funding_hour": funding_hour,
                "gross_apy": gross_apy,
                "net_apy": net_apy,
                "spot_price": spot_price,
                "perp_price": mark_price,
                "basis": basis,
            }
        )

    return sorted(opportunities, key=lambda row: row["net_apy"], reverse=True)


def print_results(rows: list[dict[str, Any]]) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"\n{timestamp} | opportunities: {len(rows)}")
    print("PAIR         FUNDING/h    NET APY     BASIS       ACTION")
    print("-" * 78)
    for row in rows:
        print(
            f"{row['pair']:<12} "
            f"{row['funding_hour']:+.5%}   "
            f"{row['net_apy']:+8.2%}   "
            f"{row['basis']:+.3%}   "
            "LONG spot + SHORT perp"
        )


def main() -> None:
    print("Hyperliquid funding scanner started. Press Ctrl+C to stop.")
    while True:
        try:
            print_results(scan())
        except KeyboardInterrupt:
            print("\nStopped.")
            return
        except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
            print(f"Data error: {exc}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
