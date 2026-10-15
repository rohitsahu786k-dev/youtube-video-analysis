"""Instrument contract specifications, position sizing and currency conversion.

P&L (quote currency) = price_difference * quantity * contract_size.
Conversion to account currency is explicit; if it cannot be derived the run is refused instead of guessed.
"""
import math
import re
from pyapp.engine.spec import InstrumentSpec, SpecError

_FOREX = re.compile(r"^[A-Z]{6}$")

PRESETS = {
    "XAUUSD": dict(asset_class="metal", contract_size=100, tick_size=0.01, base_currency="XAU", quote_currency="USD", min_qty=0.01, qty_step=0.01, max_qty=100, margin_rate=0.05),
    "XAGUSD": dict(asset_class="metal", contract_size=5000, tick_size=0.001, base_currency="XAG", quote_currency="USD", min_qty=0.01, qty_step=0.01, max_qty=100, margin_rate=0.05),
    "BTCUSDT": dict(asset_class="crypto", contract_size=1, tick_size=0.01, base_currency="BTC", quote_currency="USDT", min_qty=0.0001, qty_step=0.0001, max_qty=1000, margin_rate=1.0),
    "ETHUSDT": dict(asset_class="crypto", contract_size=1, tick_size=0.01, base_currency="ETH", quote_currency="USDT", min_qty=0.001, qty_step=0.001, max_qty=10000, margin_rate=1.0),
    "BTCUSD": dict(asset_class="crypto", contract_size=1, tick_size=0.01, base_currency="BTC", quote_currency="USD", min_qty=0.0001, qty_step=0.0001, max_qty=1000, margin_rate=1.0),
    "NAS100": dict(asset_class="index", contract_size=1, tick_size=0.1, quote_currency="USD", min_qty=0.1, qty_step=0.1, max_qty=1000, margin_rate=0.05),
    "US30": dict(asset_class="index", contract_size=1, tick_size=1.0, quote_currency="USD", min_qty=0.1, qty_step=0.1, max_qty=1000, margin_rate=0.05),
    "SPX500": dict(asset_class="index", contract_size=1, tick_size=0.1, quote_currency="USD", min_qty=0.1, qty_step=0.1, max_qty=1000, margin_rate=0.05),
    "ES": dict(asset_class="future", contract_size=50, tick_size=0.25, quote_currency="USD", min_qty=1, qty_step=1, max_qty=500, margin_rate=0.06),
    "NQ": dict(asset_class="future", contract_size=20, tick_size=0.25, quote_currency="USD", min_qty=1, qty_step=1, max_qty=500, margin_rate=0.06),
    "CL": dict(asset_class="future", contract_size=1000, tick_size=0.01, quote_currency="USD", min_qty=1, qty_step=1, max_qty=500, margin_rate=0.08),
    "GC": dict(asset_class="future", contract_size=100, tick_size=0.1, quote_currency="USD", min_qty=1, qty_step=1, max_qty=500, margin_rate=0.06),
}


def normalize_symbol(symbol: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", symbol.upper().split(":")[-1])


def resolve_instrument(symbol: str, override: InstrumentSpec | None = None) -> InstrumentSpec:
    if override:
        return override
    key = normalize_symbol(symbol)
    if key in PRESETS:
        return InstrumentSpec(symbol=key, **PRESETS[key])
    if _FOREX.match(key):
        quote = key[3:]
        pip = 0.01 if quote == "JPY" else 0.0001
        return InstrumentSpec(symbol=key, asset_class="forex", contract_size=100000, tick_size=pip / 10, pip_size=pip,
                              base_currency=key[:3], quote_currency=quote, min_qty=0.01, qty_step=0.01, max_qty=100, margin_rate=0.02)
    raise SpecError(f"No contract specification is known for {symbol}. Provide an explicit instrument definition.")


def conversion_factor(instrument: InstrumentSpec, account_currency: str, price: float, fixed_rate: float | None) -> float:
    """Multiplier turning quote-currency P&L into account currency at `price` (the instrument price)."""
    quote, account = instrument.quote_currency.upper(), account_currency.upper()
    stable = {"USD", "USDT", "USDC"}
    if quote == account or (quote in stable and account in stable):
        return 1.0
    if instrument.base_currency and instrument.base_currency.upper() == account:
        return 1.0 / price
    if fixed_rate:
        return fixed_rate
    raise SpecError(f"Cannot convert {quote} P&L to {account}. Provide a conversion rate (account currency per 1 {quote}).")


def round_down_qty(qty: float, instrument: InstrumentSpec) -> float:
    step = instrument.qty_step
    units = math.floor(round(qty / step, 9))
    return round(units * step, 10)


def size_position(risk_amount: float, risk_per_unit_price: float, instrument: InstrumentSpec, conv: float):
    """Largest permitted quantity whose stop-out loss does not exceed `risk_amount`. Never rounds up."""
    per_qty_loss = risk_per_unit_price * instrument.contract_size * conv
    if per_qty_loss <= 0:
        return 0.0
    qty = round_down_qty(risk_amount / per_qty_loss, instrument)
    return min(qty, instrument.max_qty) if qty >= instrument.min_qty else 0.0
