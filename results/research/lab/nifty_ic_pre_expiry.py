"""Iron condor on NIFTY weeklies 1-2 sessions before expiry, closed at the real close the session
before expiry (never carried into expiry day).

Run by lab/_spreads.py. `signals` is a stub so research.py can log it.
"""

META = {
    "family": "options-credit",
    "kind": "swing",
    "side": "short",
    "rationale": (
        "Volatility risk premium: Nifty weekly implied volatility sits above the volatility that is then "
        "realised, because hedgers roll weekly protection and directional retail buyers pay for convexity "
        "they rarely collect (SEBI 2024: 91 % of individual F&O traders lose). Theta is steepest in the last "
        "two sessions before expiry, so a condor opened then collects most of the week's premium in the least "
        "time. Closing at the real bhavcopy close of the session BEFORE expiry avoids expiry-day gamma, the "
        "unvalidated expiry-day model and SEBI's extra 2 % expiry-day ELM, which is what lets ONE condor lot "
        "fit a Rs 1,00,000 account. The 10:15 entry waits for the opening imbalance (overnight gap hedging) to "
        "settle before the strikes are chosen."),
    "rules": (
        "On the session with dte trading days to the nearest NIFTY expiry (dte 2 = two sessions before; dte 1 = "
        "the session before), at the close of the 09:30 bar or of the 10:15 bar: sell the put spread and the "
        "call spread with short strikes at the nearest 50-point strike k_sigma x S x VIX/100 x sqrt(trading "
        "time to expiry / year) each side of the spot, long wings 100 points further out. Fills at modelled mid "
        "-/+ 0.5 % a leg (at least Rs 0.05). No stop. Marked daily at the real bhavcopy settle; closed at the "
        "real bhavcopy close of the session before expiry, each leg -/+ 0.5 % slippage. One position at a "
        "time, one lot when 2 % exposure per short leg + the maximum loss fits 90 % of Rs 1,00,000."),
    "default": {"k_sigma": 1.0, "wing": 100, "entry": "09:30", "dte": 2, "exit": "prev_close", "stop_mult": 0,
                "vix_filter": 0},
    "grid": {"dte": [1, 2], "entry": ["09:30", "10:15"]},
    "max_trades_per_day": 1,
}

SPREAD = {"structure": "iron_condor"}


def signals(day, ctx):
    """Not an equity strategy: see lab/_spreads.py."""
    return []
