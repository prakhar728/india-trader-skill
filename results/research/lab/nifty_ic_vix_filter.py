"""nifty_ic_pre_expiry's best explore variant (dte 2, 10:15 entry) with an India VIX regime filter.

Derived from nifty_ic_pre_expiry after its explore run (so its trials add to the count).
Run by lab/_spreads.py. `signals` is a stub so research.py can log it.
"""

META = {
    "family": "options-credit",
    "kind": "swing",
    "side": "short",
    "rationale": (
        "The volatility risk premium is widest after fear spikes: implied volatility overshoots what is then "
        "realised because hedgers buy protection at any price when India VIX jumps, and it mean-reverts over "
        "days. Selling the two-session pre-expiry condor only when India VIX at entry is above the median of the "
        "previous 20 sessions' closes should keep the fat-premium weeks and drop the quiet ones where the "
        "credit barely covers four legs of charges. Strikes scale with VIX already, so a high VIX widens them; "
        "k_sigma 1.5 trades credit for a higher probability of expiring between the shorts."),
    "rules": (
        "As nifty_ic_pre_expiry with dte 2 and the 10:15 entry, plus: enter only if India VIX's 10:15 bar close "
        "is above the median of the previous 20 sessions' India VIX closes. Short strikes at k_sigma x S x "
        "VIX/100 x sqrt(trading time to expiry / year) each side, wings 100 points. Closed at the real bhavcopy "
        "close of the session before expiry. One lot when the margin estimate fits 90 % of Rs 1,00,000."),
    "default": {"k_sigma": 1.0, "wing": 100, "entry": "10:15", "dte": 2, "exit": "prev_close", "stop_mult": 0,
                "vix_filter": 1},
    "grid": {"k_sigma": [1.0, 1.5]},
    "max_trades_per_day": 1,
}

SPREAD = {"structure": "iron_condor"}


def signals(day, ctx):
    """Not an equity strategy: see lab/_spreads.py."""
    return []
