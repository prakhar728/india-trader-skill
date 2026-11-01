"""Sell a defined-risk NIFTY vertical on expiry day, hold to the real cash settlement.

Run by lab/_spreads.py (options spreads; prices modelled by india-backtest/options.py,
settlement real). `signals` is a stub so research.py can log it.
"""

META = {
    "family": "options-credit",
    "kind": "intraday",
    "side": "short",
    "rationale": (
        "On expiry day the whole time value of a Nifty weekly option goes to zero by 15:30. The buyers of cheap "
        "out-of-the-money expiry-day options are mostly individuals (SEBI's 2024 study: 91 % of individual F&O "
        "traders lose, most of them option buyers) paying a lottery premium for a large move in a few hours; "
        "the earlier backtest of that very trade (buy ATM on an ORB) lost 4 % of premium at mid prices on "
        "expiry days before costs. The mirror image, hedged: sell a vertical k x sigma away (sigma = India VIX "
        "scaled to the trading time left), long wing 100 points further, and let the exchange settle it. The "
        "09:30 put spread harvests the index put skew (institutional hedging demand keeps puts dear); the ORB "
        "variant sells the side the first 15 minutes moved away from, so the seller takes the ORB buyer's side."),
    "rules": (
        "Expiry day only, one trade. Entry at the close of the 09:30 bar (sell the PUT spread), or 'orb': at the "
        "first 5-minute close outside the 09:15-09:30 range by 14:30 (break up: sell the put spread; break down: "
        "sell the call spread). Short strike = the nearest 50-point strike at k_sigma x S x VIX/100 x "
        "sqrt(trading minutes to 15:30 / trading minutes in a year) from the spot; long wing 100 points further "
        "out. Fills at modelled mid -/+ 0.5 % (at least Rs 0.05) a leg; no stop; every leg held to the real "
        "settlement (bhavcopy intrinsic). One lot when 2 % exposure + 2 % expiry-day ELM per short leg + the "
        "maximum loss fits 90 % of Rs 1,00,000; else the day is skipped and counted."),
    "default": {"k_sigma": 1.0, "wing": 100, "entry": "09:30", "dte": 0, "exit": "settle", "stop_mult": 0,
                "vix_filter": 0, "or_minutes": 15},
    "grid": {"entry": ["09:30", "orb"], "k_sigma": [0.5, 1.0]},
    "max_trades_per_day": 1,
}

SPREAD = {"structure": "vertical_0dte"}


def signals(day, ctx):
    """Not an equity strategy: see lab/_spreads.py."""
    return []
