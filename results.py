"""results.py — Stats computation for backtest/paper/live trades."""

from __future__ import annotations

import pandas as pd


def compute_stats(trades: list[dict], capital: float = 100_000) -> dict:
    """Compute comprehensive stats from trade list."""
    if not trades:
        return {"n": 0}

    total_pp = 0
    pc = pw = 0
    peak = max_dd = equity = 0.0
    streak = max_streak = 0
    reasons: dict[str, dict] = {}
    entry_types: dict[str, dict] = {}

    for t in trades:
        if t["prem_pnl"] is None:
            continue
        pnl = t["prem_pnl"]
        total_pp += pnl
        pc += 1
        equity += pnl
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
        if pnl > 0:
            pw += 1
            streak = 0
        else:
            streak += 1
            max_streak = max(max_streak, streak)

        # Exit reason breakdown
        r = t["reason"]
        if r not in reasons:
            reasons[r] = {"n": 0, "pnl": 0, "w": 0}
        reasons[r]["n"] += 1
        reasons[r]["pnl"] += pnl
        if pnl > 0:
            reasons[r]["w"] += 1

        # Entry type breakdown
        et = t.get("entry_type", "crossover")
        if et not in entry_types:
            entry_types[et] = {"n": 0, "pnl": 0, "w": 0}
        entry_types[et]["n"] += 1
        entry_types[et]["pnl"] += pnl
        if pnl > 0:
            entry_types[et]["w"] += 1

    if pc == 0:
        return {"n": 0}

    gw = sum(t["prem_pnl"] for t in trades if t["prem_pnl"] and t["prem_pnl"] > 0)
    gl = abs(sum(t["prem_pnl"] for t in trades if t["prem_pnl"] and t["prem_pnl"] < 0))
    pf = gw / gl if gl > 0 else float("inf")
    avg_win = gw / pw if pw > 0 else 0
    avg_loss = gl / (pc - pw) if (pc - pw) > 0 else 0

    # Monthly
    monthly: dict[str, float] = {}
    for t in trades:
        if t["prem_pnl"] is not None:
            m = pd.Timestamp(t["entry"]).strftime("%Y-%m")
            monthly[m] = monthly.get(m, 0) + t["prem_pnl"]
    green = sum(1 for v in monthly.values() if v >= 0)
    red = sum(1 for v in monthly.values() if v < 0)

    # Yearly
    yearly: dict[int, dict] = {}
    for t in trades:
        if t["prem_pnl"] is not None:
            y = pd.Timestamp(t["entry"]).year
            if y not in yearly:
                yearly[y] = {"pnl": 0, "n": 0, "w": 0}
            yearly[y]["pnl"] += t["prem_pnl"]
            yearly[y]["n"] += 1
            if t["prem_pnl"] > 0:
                yearly[y]["w"] += 1

    # Direction
    dir_stats: dict[str, dict] = {}
    for d in ("CE", "PE"):
        dt = [t for t in trades if t["dir"] == d and t["prem_pnl"] is not None]
        if dt:
            dpnl = sum(t["prem_pnl"] for t in dt)
            dw = sum(1 for t in dt if t["prem_pnl"] > 0)
            dir_stats[d] = {"n": len(dt), "pnl": dpnl, "w": dw}

    return {
        "n": pc, "winners": pw, "wr": pw / pc * 100, "pnl": total_pp,
        "pf": pf, "avg_win": avg_win, "avg_loss": avg_loss,
        "max_dd": max_dd, "max_streak": max_streak,
        "green_months": green, "red_months": red,
        "return_pct": total_pp / capital * 100,
        "monthly": monthly, "yearly": yearly,
        "exit_breakdown": reasons, "entry_type_breakdown": entry_types,
        "direction": dir_stats,
    }


def compute_equity_curve(trades: list[dict], capital: float = 100_000) -> list[tuple]:
    """Return list of (timestamp, equity) points."""
    curve = [(None, capital)]
    equity = capital
    for t in trades:
        if t["prem_pnl"] is not None:
            equity += t["prem_pnl"]
            curve.append((t["exit"], equity))
    return curve


def print_stats(trades: list[dict], label: str) -> None:
    """Print formatted stats to terminal."""
    s = compute_stats(trades)
    if s["n"] == 0:
        print(f"  {label}: No trades!")
        return

    print(f"\n{'=' * 90}")
    print(f"  {label}")
    print(f"{'=' * 90}")
    print(f"  Trades: {s['n']}  |  Winners: {s['winners']}/{s['n']} ({s['wr']:.0f}%)  |  Net P&L: Rs.{s['pnl']:>10,.0f}")
    if s["avg_loss"] > 0:
        print(f"  Profit Factor: {s['pf']:.2f}  |  Avg Win: Rs.{s['avg_win']:,.0f}  |  Avg Loss: Rs.{s['avg_loss']:,.0f}")
    print(f"  Max Drawdown: Rs.{s['max_dd']:>8,.0f}  |  Max Loss Streak: {s['max_streak']}  |  Green Months: {s['green_months']}/{s['green_months'] + s['red_months']}")

    print(f"\n  Exit breakdown:")
    for r in sorted(s["exit_breakdown"]):
        d = s["exit_breakdown"][r]
        wr = d["w"] / d["n"] * 100 if d["n"] else 0
        print(f"    {r:>12}: {d['n']:>3} trades, WR {wr:.0f}%, P&L: Rs.{d['pnl']:>10,.0f}")

    if s["entry_type_breakdown"]:
        print(f"\n  Entry type breakdown:")
        for et in sorted(s["entry_type_breakdown"]):
            d = s["entry_type_breakdown"][et]
            wr = d["w"] / d["n"] * 100 if d["n"] else 0
            print(f"    {et:>12}: {d['n']:>3} trades, WR {wr:.0f}%, P&L: Rs.{d['pnl']:>10,.0f}")

    print(f"\n  Yearly:")
    for y in sorted(s["yearly"]):
        d = s["yearly"][y]
        wr = d["w"] / d["n"] * 100 if d["n"] else 0
        print(f"    {y}: Rs.{d['pnl']:>10,.0f}  ({d['n']:>3} trades, {wr:.0f}% WR)")

    for d_name in ("CE", "PE"):
        if d_name in s["direction"]:
            d = s["direction"][d_name]
            print(f"\n  {d_name}: {d['n']} trades, Rs.{d['pnl']:>9,.0f}, WR {d['w']}/{d['n']} ({d['w'] / d['n'] * 100:.0f}%)")
