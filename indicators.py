"""Indicator maths and the signal rule, shared by the live app (main.py) and backtest.py.

Keeping them in one place guarantees the backtest tests exactly the rule the bot trades.
No Tk, no network.
"""


def ema(values, period):
    """Return the latest EMA, seeded with the first `period` observations."""
    if period < 1 or len(values) < period:
        return None
    value = sum(values[:period]) / period
    alpha = 2.0 / (period + 1)
    for price in values[period:]:
        value = alpha * price + (1.0 - alpha) * value
    return value


def ema_series(values, period):
    """Return EMA values beginning at the first complete period."""
    if period < 1 or len(values) < period:
        return []
    value = sum(values[:period]) / period
    series = [value]
    alpha = 2.0 / (period + 1)
    for price in values[period:]:
        value = alpha * price + (1.0 - alpha) * value
        series.append(value)
    return series


def rsi(values, period=14):
    """Calculate RSI using Wilder's smoothed average gains and losses."""
    if period < 1 or len(values) < period + 1:
        return None

    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    initial = changes[:period]
    avg_gain = sum(max(change, 0.0) for change in initial) / period
    avg_loss = sum(max(-change, 0.0) for change in initial) / period

    for change in changes[period:]:
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    if avg_gain == 0:
        return 0.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd(values, fast_period=12, slow_period=26, signal_period=9):
    """Return the latest MACD and signal values using aligned EMA series."""
    if not (1 <= fast_period < slow_period and signal_period >= 1):
        return None, None
    if len(values) < slow_period + signal_period - 1:
        return None, None

    fast_ema = ema_series(values, fast_period)
    slow_ema = ema_series(values, slow_period)
    if not fast_ema or not slow_ema:
        return None, None

    # Both series must refer to the same source-price positions.
    fast_ema = fast_ema[slow_period - fast_period:]
    macd_line = [fast - slow for fast, slow in zip(fast_ema, slow_ema)]
    signal_line = ema_series(macd_line, signal_period)
    if not signal_line:
        return None, None

    return macd_line[-1], signal_line[-1]


def evaluate_signal(vals, min_history=100):
    """The live trading rule. Returns (signal, explanation); signal is HIGHER, LOWER or WAIT."""
    if len(vals) < min_history:
        return "WAIT", (
            f"Collecting at least {min_history} ticks before evaluating "
            "EMA, MACD (12/26/9), and RSI (14)."
        )

    fast, slow = ema(vals, 9), ema(vals, 21)
    macd_line, macd_signal = macd(vals)
    rsi_value = rsi(vals, 14)
    if any(value is None for value in (fast, slow, macd_line, macd_signal, rsi_value)):
        return "WAIT", "Not enough valid history to calculate all indicators."

    # These are experimental research rules, not a guarantee of future movement.
    if fast > slow and macd_line > macd_signal and 50 <= rsi_value < 70:
        return "HIGHER", f"EMA bullish; MACD above signal; RSI {rsi_value:.1f}. Research only."
    if fast < slow and macd_line < macd_signal and 30 < rsi_value <= 50:
        return "LOWER", f"EMA bearish; MACD below signal; RSI {rsi_value:.1f}. Research only."
    return "WAIT", f"Indicators do not fully agree or RSI is at a threshold (RSI {rsi_value:.1f})."
