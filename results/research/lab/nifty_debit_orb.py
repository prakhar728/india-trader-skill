"""The losing ORB option buy with its cost capped: buy the ATM, sell a strike `wing` points further.

Run by lab/_spreads.py. `signals` is a stub so research.py can log it.
"""

META = {
    "family": "options-debit",
    "kind": "intraday",
    "side": "long",
    "rationale": (
        "Buying the ATM Nifty weekly on a 15-minute opening-range break lost 4.2 % of premium at mid prices "
        "before costs (16 setups, a year), the worst on expiry days: most of that is the time value paid for "
        "a move that rarely comes, and the -25 % premium stop being hit by decay and noise. Selling an option "
        "100-200 points further out against the long cuts the debit, and the short leg's own decay offsets "
        "part of the long's, so if the loss was mostly decay and spread the sign may flip; if the loss was the "
        "direction being wrong, a spread cannot fix it. The counterpart is the seller of the ATM option."),
    "rules": (
        "Opening range = 09:15-09:30 high and low. The first 5-minute close above the high (by 14:30) buys the "
        "ATM call and sells the call `wing` points higher; below the low buys the ATM put and sells the put "
        "`wing` lower. Nearest expiry, expiry day included. Net debit sized at 20 % of Rs 1,00,000, whole lots. "
        "exit=stop_target: close when the spread's modelled value is 25 % under or 50 % over the debit; "
        "exit=flat: hold to 15:05 (15:00 on expiry day). Fills at modelled mid -/+ 0.5 % a leg. One trade a day."),
    "default": {"or_minutes": 15, "wing": 100, "stop_pct": 25, "target_pct": 50, "exit": "stop_target"},
    "grid": {"wing": [100, 200], "exit": ["stop_target", "flat"]},
    "max_trades_per_day": 1,
}

SPREAD = {"structure": "debit_orb"}


def signals(day, ctx):
    """Not an equity strategy: see lab/_spreads.py."""
    return []
