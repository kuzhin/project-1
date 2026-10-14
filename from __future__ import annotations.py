import requests
import ccxt
import pandas as pd
import time

def fetch_hyperliquid_funding():
    url = "https://api.hyperliquid.xyz/info"
    payload = {"type": "metaAndAssetCtxs"}
    response = requests.post(url, json=payload)
    data = response.json()
    meta = data[0]
    asset_ctxs = data[1]
    universe = meta["universe"]
    rows = []
    for asset, ctx in zip(universe, asset_ctxs):
        name = asset["name"]
        funding = float(ctx["funding"])
        # Hyperliquid платит фандинг каждый час, ставка в API — почасовая.
        annual = funding * 24 * 365
        rows.append({
            "symbol": name,
            "hyper_funding_hourly": funding,
            "hyper_funding_annual": annual,
            "open_interest": float(ctx.get("openInterest", 0)),
            "mark_price": float(ctx.get("markPx", 0)),
        })
    return pd.DataFrame(rows)

def fetch_cex_funding(exchange_id, quote="USDT"):
    exchange_class = getattr(ccxt, exchange_id)
    exchange = exchange_class({'enableRateLimit': True})
    markets = exchange.load_markets()
    funding_rates = exchange.fetch_funding_rates()
    rows = []
    for symbol, info in funding_rates.items():
        market = markets.get(symbol)
        if not market or market.get('type') != 'swap':
            continue
        if market.get('quote') != quote or not market.get('linear'):
            continue
        base = market.get('base')
        funding = info.get('fundingRate')
        if funding is None:
            continue
        # По умолчанию считаем, что на CEX фандинг каждые 8 часов (3 раза в день).
        # Если в info есть interval, можно уточнить.
        interval = info.get('interval', '8h')
        if interval == '8h':
            periods_per_year = 3 * 365
        elif interval == '4h':
            periods_per_year = 6 * 365
        elif interval == '1h': 
            periods_per_year = 24 * 365
        else:
            periods_per_year = 3 * 365
        annual = funding * periods_per_year
        rows.append({
            "symbol": base,
            "cex": exchange_id,
            "cex_funding": funding,
            "cex_funding_annual": annual,
            "cex_symbol": symbol,
        })
    return pd.DataFrame(rows)

def main():
    print("Загружаем фандинг с Hyperliquid...")
    hl_df = fetch_hyperliquid_funding()
    print(f"Получено {len(hl_df)} рынков на Hyperliquid.")

    cex_list = ['binance', 'bybit']  # можно добавить 'okx', 'gate' и т.д.
    cex_dfs = []
    for cex in cex_list:
        try:
            print(f"Загружаем фандинг с {cex}...")
            df = fetch_cex_funding(cex)
            cex_dfs.append(df)
        except Exception as e:
            print(f"Ошибка при загрузке {cex}: {e}")
    if not cex_dfs:
        print("Нет данных с CEX. Выход.")
        return
    cex_df = pd.concat(cex_dfs, ignore_index=True)

    merged = pd.merge(hl_df, cex_df, on="symbol", how="inner")
    if merged.empty:
        print("Нет общих символов.")
        return

    merged['spread_annual'] = merged['hyper_funding_annual'] - merged['cex_funding_annual']
    merged = merged.sort_values('spread_annual', ascending=False)

    pd.set_option('display.max_rows', 50)
    pd.set_option('display.width', 200)
    print("\nТоп возможностей: ШОРТ Hyperliquid + ЛОНГ CEX (положительный spread):")
    print(merged[['symbol', 'cex', 'hyper_funding_hourly', 'cex_funding', 'spread_annual', 'mark_price', 'open_interest']].head(20))

    merged_rev = merged.sort_values('spread_annual', ascending=True)
    print("\nТоп обратных возможностей: ЛОНГ Hyperliquid + ШОРТ CEX (отрицательный spread):")
    print(merged_rev[['symbol', 'cex', 'hyper_funding_hourly', 'cex_funding', 'spread_annual', 'mark_price', 'open_interest']].head(20))

if __name__ == "__main__":
    main()

    