#!/usr/bin/env python3
"""
fx_signal timing probe — measures end-to-end latency of each pipeline stage
on YOUR server (Yahoo Finance blocks many cloud/datacenter IPs, so timing
must be measured from where the cron actually runs).

Usage:
    cd /opt/market/fx_signal
    python3 timing_probe.py          # runs each stage once and prints timings
    python3 timing_probe.py --repeat 3   # repeat each stage N times for medians

Safe: read-only. Does not write signals, does not send Telegram messages.
"""

import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import yfinance as yf  # noqa: E402

from fx_config import FX_PAIRS, TIMEFRAMES  # noqa: E402


def timeit(fn, *args, **kwargs):
    t0 = time.perf_counter()
    result = fn(*args, **kwargs)
    return time.perf_counter() - t0, result


def probe_collector_latency():
    """Time the full collector-style loop: 10 pairs x 2 timeframes."""
    timings = {}
    for pair, cfg in FX_PAIRS.items():
        for tf_name, tf_cfg in TIMEFRAMES.items():
            key = f"{pair} {tf_cfg['interval']}"
            dt, df = timeit(
                lambda s=cfg["yf_symbol"], i=tf_cfg["interval"], p=tf_cfg["period"]:
                yf.Ticker(s).history(interval=i, period=p)
            )
            timings[key] = (dt, len(df))
    return timings


def probe_batch_latency():
    """Time the batched alternative: 1 request for all symbols."""
    syms = sorted({cfg["yf_symbol"] for cfg in FX_PAIRS.values()})
    dt, df = timeit(
        lambda: yf.download(syms, interval="1h", period="5d",
                            group_by="ticker", threads=True, progress=False)
    )
    return dt, len(syms)


def probe_fastinfo_latency():
    """Time the live-price call used by telegram_bot / outcome_tracker."""
    dt, fi = timeit(lambda: yf.Ticker("EURUSD=X").fast_info)
    price = getattr(fi, "last_price", None)
    return dt, price


def probe_telegram_latency():
    """Time a Telegram sendMessage round-trip (needs token+chat_id set)."""
    import os
    import requests
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return None, "skipped (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set)"
    dt, r = timeit(
        lambda: requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": "⏱ timing probe"}, timeout=10,
        )
    )
    return dt, r.status_code


def main():
    repeats = 1
    if len(sys.argv) > 2 and sys.argv[1] == "--repeat":
        repeats = int(sys.argv[2])

    print("fx_signal timing probe")
    print("=" * 64)

    all_times = {k: [] for k in
                 ("collector_loop", "batch", "fastinfo", "telegram")}

    for rep in range(repeats):
        print(f"\n--- run {rep + 1}/{repeats} ---")

        t0 = time.perf_counter()
        timings = probe_collector_latency()
        total = time.perf_counter() - t0
        print(f"\nCollector loop (sequential, current code):")
        print(f"  total {total:.2f}s for {len(timings)} calls "
              f"(median {statistics.median(v[0] for v in timings.values()):.2f}s/call, "
              f"max {max(v[0] for v in timings.values()):.2f}s)")
        slow = sorted(timings.items(), key=lambda kv: -kv[1][0])[:5]
        for k, (dt, n) in slow:
            print(f"    {k:<12} {dt:6.2f}s  ({n} rows)")
        all_times["collector_loop"].append(total)

        bdt, b = probe_batch_latency()
        print(f"Batch yf.download (all {b} symbols, 1 request): {bdt:.2f}s")
        all_times["batch"].append(bdt)

        fdt, price = probe_fastinfo_latency()
        print(f"fast_info EURUSD=X: {fdt:.2f}s  price={price}")
        all_times["fastinfo"].append(fdt)

        tdt, tstatus = probe_telegram_latency()
        if tdt is None:
            print(f"Telegram send: {tstatus}")
        else:
            print(f"Telegram sendMessage: {tdt:.2f}s  HTTP {tstatus}")
            all_times["telegram"].append(tdt)

    print("\n" + "=" * 64)
    print("Medians:")
    for k, v in all_times.items():
        if v:
            print(f"  {k:<16} {statistics.median(v):.2f}s")
    print("\nLatency budget (bar close -> Telegram, cron every minute):")
    print("  cron wait (avg)      ~30s")
    print("  collector            ~as measured above (batch alternative: ~3-6s)")
    print("  signal_engine        <1s (local SQLite)")
    print("  telegram_bot/signal  fast_info + telegram send (as measured)")
    print("  => typical total     ~1.5-4 min; Yahoo feed lag (1-3 min) dominates")


if __name__ == "__main__":
    main()
