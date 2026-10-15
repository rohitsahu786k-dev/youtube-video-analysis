"""Strategy rule DSL, instrument and backtest configuration models.

The DSL is a plain JSON document so it can be versioned, hashed, reviewed by a human, translated
to Pine Script and executed by the independent engine. Nothing here knows about future data.
"""
import hashlib
import json
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

TF_SECONDS = {"M1": 60, "M3": 180, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H2": 7200, "H4": 14400, "D": 86400}
TF = Literal["M1", "M3", "M5", "M15", "M30", "H1", "H2", "H4", "D"]


class SpecError(ValueError):
    """A rule document is invalid or uses an unsupported construct."""


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class SeriesDef(Strict):
    type: str
    tf: TF | None = None
    field: str | None = None
    period: int | None = Field(default=None, ge=1, le=5000)
    n: int | None = Field(default=None, ge=1, le=50)
    mult: float | None = Field(default=None, gt=0, le=50)
    fast: int | None = Field(default=None, ge=1, le=500)
    slow: int | None = Field(default=None, ge=1, le=1000)
    signal: int | None = Field(default=None, ge=1, le=500)
    dir: Literal["bull", "bear"] | None = None
    ref: str | None = None
    name: str | None = None
    start: str | None = None
    end: str | None = None
    tz: str | None = None
    min_size: float | None = Field(default=None, ge=0)
    max_age: int | None = Field(default=None, ge=1, le=5000)
    pattern: str | None = None
    source: str | None = None
    min_penetration: float | None = Field(default=None, ge=0)
    exclude_current: bool | None = None
    close_back: bool | None = None


class StopSpec(Strict):
    type: Literal["atr", "points", "percent", "level", "signal_candle", "swing"]
    mult: float | None = Field(default=None, gt=0, le=100)
    series: str | None = None
    value: float | None = Field(default=None, gt=0)
    ref: Any = None
    buffer: float = Field(default=0, ge=0)


class TargetSpec(Strict):
    type: Literal["rr", "points", "atr", "level"]
    value: float | None = Field(default=None, gt=0)
    mult: float | None = Field(default=None, gt=0, le=100)
    series: str | None = None
    ref: Any = None
    portion: float = Field(default=1.0, gt=0, le=1)


class BreakEvenSpec(Strict):
    trigger_r: float | None = Field(default=None, gt=0, le=50)
    after_target: int | None = Field(default=None, ge=1, le=10)
    offset_points: float = 0.0


class TrailingSpec(Strict):
    type: Literal["points", "atr", "candle", "swing"]
    start_r: float = Field(default=0.0, ge=0, le=50)
    value: float | None = Field(default=None, gt=0)
    mult: float | None = Field(default=None, gt=0, le=100)
    series: str | None = None
    n: int | None = Field(default=None, ge=1, le=50)
    buffer: float = Field(default=0, ge=0)


class ManagementSpec(Strict):
    breakeven: BreakEvenSpec | None = None
    trailing: TrailingSpec | None = None
    max_bars: int | None = Field(default=None, ge=1, le=100000)


class OrderSpec(Strict):
    type: Literal["market", "limit", "stop"] = "market"
    price: Any = None
    offset_points: float = 0.0
    expire_bars: int = Field(default=3, ge=1, le=1000)


class EntrySpec(Strict):
    long: Any = None
    short: Any = None
    order: OrderSpec = Field(default_factory=OrderSpec)
    invalidation: Any = None
    cooldown_bars: int = Field(default=0, ge=0, le=10000)


class ExitRule(Strict):
    cond: Any
    directions: Literal["LONG", "SHORT", "BOTH"] = "BOTH"


class Window(Strict):
    name: str | None = None
    start: str
    end: str
    tz: str = "UTC"
    days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])


class FlatSpec(Strict):
    time: str
    tz: str = "UTC"


class SessionSpec(Strict):
    windows: list[Window] = Field(default_factory=list)
    avoid: list[Window] = Field(default_factory=list)
    weekdays: list[int] | None = None
    force_flat: FlatSpec | None = None


class RiskSpec(Strict):
    model: Literal["percent_equity", "percent_initial", "fixed_cash", "fixed_qty"] = "percent_equity"
    value: float = Field(default=1.0, gt=0)
    max_trades_per_day: int | None = Field(default=None, ge=1)
    max_daily_loss_r: float | None = Field(default=None, gt=0)
    max_daily_loss_pct: float | None = Field(default=None, gt=0, le=100)
    max_consecutive_losses: int | None = Field(default=None, ge=1)
    max_concurrent: int = Field(default=1, ge=1, le=20)
    min_rr: float | None = Field(default=None, gt=0)
    max_spread: float | None = Field(default=None, gt=0)
    day_tz: str = "UTC"


class DaySpec(Strict):
    tz: str = "UTC"
    rollover_hour: int = Field(default=0, ge=0, le=23)


class BiasSpec(Strict):
    bull: Any = None
    bear: Any = None


class StrategySpec(Strict):
    schema_version: Literal[1] = 1
    name: str = Field(min_length=1, max_length=200)
    setup_name: str | None = None
    timeframe: TF
    day: DaySpec = Field(default_factory=DaySpec)
    series: dict[str, SeriesDef] = Field(default_factory=dict)
    bias: BiasSpec | None = None
    entry: EntrySpec
    stop: StopSpec
    targets: list[TargetSpec] = Field(default_factory=list)
    management: ManagementSpec = Field(default_factory=ManagementSpec)
    exits: list[ExitRule] = Field(default_factory=list)
    sessions: SessionSpec = Field(default_factory=SessionSpec)
    risk: RiskSpec = Field(default_factory=RiskSpec)

    def dump(self) -> dict:
        """Canonical JSON form used for storage, hashing and display (unset optional fields are omitted)."""
        return self.model_dump(mode="json", exclude_none=True)

    @field_validator("targets")
    @classmethod
    def portions_fit(cls, targets):
        if sum(t.portion for t in targets) > 1.0000001:
            raise ValueError("Target portions must not exceed 1.0 in total.")
        return targets


class InstrumentSpec(Strict):
    symbol: str
    asset_class: Literal["forex", "metal", "crypto", "index", "future", "equity", "other"] = "forex"
    contract_size: float = Field(default=100000, gt=0)
    tick_size: float = Field(default=0.00001, gt=0)
    pip_size: float | None = Field(default=None, gt=0)
    base_currency: str | None = None
    quote_currency: str = "USD"
    min_qty: float = Field(default=0.01, gt=0)
    qty_step: float = Field(default=0.01, gt=0)
    max_qty: float = Field(default=1000, gt=0)
    margin_rate: float = Field(default=0.02, gt=0, le=1)


class CostConfig(Strict):
    spread: float = Field(default=0.0, ge=0)
    slippage: float = Field(default=0.0, ge=0)
    commission_model: Literal["none", "per_lot_side", "percent_notional_side", "fixed_per_trade"] = "none"
    commission_value: float = Field(default=0.0, ge=0)
    use_dataset_spread: bool = True


class NewsBlackout(Strict):
    start: str
    end: str
    label: str | None = None


class BacktestConfig(Strict):
    dataset_id: str
    fine_dataset_id: str | None = None
    symbol: str | None = None
    instrument: InstrumentSpec | None = None
    start: str | None = None
    end: str | None = None
    initial_capital: float = Field(default=10000, gt=0)
    account_currency: str = "USD"
    costs: CostConfig = Field(default_factory=CostConfig)
    same_bar_policy: Literal["conservative", "ambiguous", "lower_tf"] = "conservative"
    limit_fill: Literal["through", "touch"] = "through"
    leverage: float | None = Field(default=None, gt=0, le=1000)
    conversion_rate: float | None = Field(default=None, gt=0)
    news_blackouts: list[NewsBlackout] = Field(default_factory=list)
    calendar: Literal["forex", "24x7", "equity"] = "forex"
    display_tz: str = "Asia/Kolkata"
    in_sample_end: str | None = None
    strategy_published_at: str | None = None
    causality_samples: int = Field(default=6, ge=0, le=50)


def canonical(document) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def digest(document) -> str:
    return hashlib.sha256(canonical(document).encode()).hexdigest()
