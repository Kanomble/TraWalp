import sqlite3
from pathlib import Path

DB_PATH = Path("data/trading_system.sqlite3")

if not DB_PATH.exists():
    raise SystemExit(f"Database not found: {DB_PATH.resolve()}")

db = sqlite3.connect(
    f"file:{DB_PATH.resolve().as_posix()}?mode=ro",
    uri=True,
)

try:
    print("=" * 80)
    print("TraWalp Database Coverage")
    print("=" * 80)
    print(f"Database: {DB_PATH.resolve()}")
    print(f"Size:     {DB_PATH.stat().st_size / 1024 / 1024:.1f} MB")
    print()

    # ------------------------------------------------------------------
    # 1. Overall bar coverage by timeframe
    # ------------------------------------------------------------------
    print("BAR COVERAGE BY TIMEFRAME")
    print("-" * 80)

    rows = db.execute(
        """
        SELECT
            timeframe,
            MIN(timestamp) AS earliest,
            MAX(timestamp) AS latest,
            COUNT(*) AS bars,
            COUNT(DISTINCT symbol) AS symbols
        FROM bars
        GROUP BY timeframe
        ORDER BY timeframe
        """
    ).fetchall()

    for timeframe, earliest, latest, bars, symbols in rows:
        print(
            f"{timeframe:>5} | "
            f"{earliest or '-':25} -> {latest or '-':25} | "
            f"{bars:>10,} bars | {symbols:>5,} symbols"
        )

    print()

    # ------------------------------------------------------------------
    # 2. Daily coverage
    # ------------------------------------------------------------------
    print("DAILY DATA")
    print("-" * 80)

    earliest, latest, bars, symbols = db.execute(
        """
        SELECT
            MIN(timestamp),
            MAX(timestamp),
            COUNT(*),
            COUNT(DISTINCT symbol)
        FROM bars
        WHERE timeframe = '1d'
        """
    ).fetchone()

    print(f"Earliest Daily Bar : {earliest}")
    print(f"Latest Daily Bar   : {latest}")
    print(f"Daily Bars         : {bars:,}")
    print(f"Daily Symbols      : {symbols:,}")
    print()

    # ------------------------------------------------------------------
    # 3. How many symbols reach back to each year?
    # ------------------------------------------------------------------
    print("HOW FAR BACK DO INDIVIDUAL SYMBOLS GO?")
    print("-" * 80)

    first_dates = db.execute(
        """
        WITH coverage AS (
            SELECT
                symbol,
                MIN(date(timestamp)) AS first_date,
                MAX(date(timestamp)) AS last_date,
                COUNT(*) AS bar_count
            FROM bars
            WHERE timeframe = '1d'
            GROUP BY symbol
        )
        SELECT
            substr(first_date, 1, 4) AS first_year,
            COUNT(*) AS symbol_count
        FROM coverage
        GROUP BY first_year
        ORDER BY first_year
        """
    ).fetchall()

    for year, count in first_dates:
        print(f"First data in {year}: {count:>5} symbols")

    print()

    # ------------------------------------------------------------------
    # 4. Coverage relevant for historical tests
    # ------------------------------------------------------------------
    print("SYMBOLS WITH DATA AVAILABLE BY DATE")
    print("-" * 80)

    for cutoff in (
        "2020-01-01",
        "2021-01-01",
        "2022-01-01",
        "2023-01-01",
        "2024-01-01",
        "2025-01-01",
        "2026-01-01",
    ):
        count = db.execute(
            """
            SELECT COUNT(*)
            FROM (
                SELECT symbol
                FROM bars
                WHERE timeframe = '1d'
                GROUP BY symbol
                HAVING MIN(date(timestamp)) <= ?
            )
            """,
            (cutoff,),
        ).fetchone()[0]

        print(f"Data starts on/before {cutoff}: {count:>5} symbols")

    print()

    # ------------------------------------------------------------------
    # 5. Specifically: 2022 coverage
    # ------------------------------------------------------------------
    print("2022 DAILY COVERAGE")
    print("-" * 80)

    symbols_2022, bars_2022, first_2022, last_2022 = db.execute(
        """
        SELECT
            COUNT(DISTINCT symbol),
            COUNT(*),
            MIN(date(timestamp)),
            MAX(date(timestamp))
        FROM bars
        WHERE timeframe = '1d'
          AND date(timestamp) >= '2022-01-01'
          AND date(timestamp) <= '2022-12-31'
        """
    ).fetchone()

    print(f"Symbols represented in 2022 : {symbols_2022:,}")
    print(f"Daily bars in 2022          : {bars_2022:,}")
    print(f"First stored 2022 date      : {first_2022}")
    print(f"Last stored 2022 date       : {last_2022}")
    print()

    # ------------------------------------------------------------------
    # 6. Per-symbol extremes
    # ------------------------------------------------------------------
    print("10 SYMBOLS WITH EARLIEST DAILY HISTORY")
    print("-" * 80)

    rows = db.execute(
        """
        SELECT
            symbol,
            MIN(date(timestamp)) AS first_date,
            MAX(date(timestamp)) AS last_date,
            COUNT(*) AS bars
        FROM bars
        WHERE timeframe = '1d'
        GROUP BY symbol
        ORDER BY first_date ASC, symbol ASC
        LIMIT 10
        """
    ).fetchall()

    for symbol, first_date, last_date, count in rows:
        print(
            f"{symbol:<8} {first_date} -> {last_date} "
            f"({count:,} bars)"
        )

    print()

    # ------------------------------------------------------------------
    # 7. Fundamental data coverage
    # ------------------------------------------------------------------
    print("FUNDAMENTAL / SEC COVERAGE")
    print("-" * 80)

    facts, fact_symbols, earliest_period, earliest_filed, latest_filed = db.execute(
        """
        SELECT
            COUNT(*),
            COUNT(DISTINCT symbol),
            MIN(period_end),
            MIN(filed),
            MAX(filed)
        FROM fundamental_facts
        """
    ).fetchone()

    print(f"Fundamental facts         : {facts:,}")
    print(f"Symbols with fundamentals : {fact_symbols:,}")
    print(f"Earliest period end       : {earliest_period}")
    print(f"Earliest filing date      : {earliest_filed}")
    print(f"Latest filing date        : {latest_filed}")

finally:
    db.close()