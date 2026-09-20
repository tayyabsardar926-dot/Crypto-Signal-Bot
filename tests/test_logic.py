import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from signal_bot import add_indicators, trend_score, rolling_4h_move_potential, price_levels


def synthetic_uptrend(n=260):
    rng = np.random.default_rng(7)
    base = np.linspace(100, 135, n)
    noise = rng.normal(0, 0.25, n)
    close = base + noise
    open_ = close - rng.normal(0.12, 0.10, n)
    high = np.maximum(open_, close) + 0.45
    low = np.minimum(open_, close) - 0.45
    volume = np.linspace(1000, 1600, n) + rng.normal(0, 80, n)
    return pd.DataFrame({
        "open": open_, "high": high, "low": low, "close": close,
        "volume": volume, "quote_volume": volume * close,
    })


def test_uptrend_scores_high():
    d = add_indicators(synthetic_uptrend())
    assert trend_score(d) >= 4


def test_move_potential_positive():
    d = add_indicators(synthetic_uptrend())
    assert rolling_4h_move_potential(d) > 0


def test_price_levels_exist():
    d = add_indicators(synthetic_uptrend())
    levels = price_levels(d, 1.0, 1.8)
    assert levels is not None
    assert levels["tp1"] > levels["current_price"]
    assert levels["tp2"] > levels["tp1"]
    assert levels["stop"] < levels["current_price"]
