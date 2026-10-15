"""Pine Script v6 generation from an approved strategy spec, a static lookahead/repainting audit, and
TradingView Strategy Tester trade-list import + comparison.

Nothing here calls TradingView. The generated script is meant to be pasted into the Pine Editor and tested with the
official Strategy Tester; the audit refuses constructs that could leak future data.
"""
import csv
import hashlib
import io
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from pyapp.engine import ENGINE_VERSION
from pyapp.engine.instruments import resolve_instrument
from pyapp.engine.series import parse_hhmm
from pyapp.engine.spec import BacktestConfig, SpecError, StrategySpec, TF_SECONDS, digest

PINE_TF = {"M1": "1", "M3": "3", "M5": "5", "M15": "15", "M30": "30", "H1": "60", "H2": "120", "H4": "240", "D": "D"}


def short_hash(*parts):
    return hashlib.sha1(repr(parts).encode()).hexdigest()[:8]


class PineGen:
    def __init__(self, spec: StrategySpec, cfg: BacktestConfig | None):
        self.spec, self.cfg = spec, cfg or BacktestConfig(dataset_id="pine-export")
        self.inst = resolve_instrument(self.cfg.symbol or (self.cfg.instrument.symbol if self.cfg.instrument else "EURUSD"), self.cfg.instrument)
        self.lines, self.warnings, self.unsupported = [], [], []
        self.seq_count = 0
        self.struct_seen = set()

    # -- utilities ---------------------------------------------------------------------------------
    def emit(self, text=""):
        self.lines.append(text)

    def warn(self, text):
        if text not in self.warnings:
            self.warnings.append(text)

    def no(self, text):
        self.unsupported.append(text)
        self.emit(f"// UNSUPPORTED: {text}")

    @staticmethod
    def v(name):
        return "s_" + re.sub(r"[^A-Za-z0-9_]", "_", name)

    # -- operands & conditions ---------------------------------------------------------------------
    def operand(self, node):
        if isinstance(node, bool):
            return "1.0" if node else "0.0"
        if isinstance(node, (int, float)):
            return repr(float(node))
        if isinstance(node, str):
            return self.series_ref(node, 0)
        if "const" in node:
            return self.operand(node["const"])
        if "s" in node:
            return self.series_ref(node["s"], node.get("shift", 0))
        for op in ("add", "sub", "mul", "div", "min", "max", "abs", "neg"):
            if op in node:
                args = node[op] if isinstance(node[op], list) else [node[op]]
                parts = [self.operand(a) for a in args]
                if op == "add": return "(" + " + ".join(parts) + ")"
                if op == "sub": return f"({parts[0]} - {parts[1]})"
                if op == "mul": return "(" + " * ".join(parts) + ")"
                if op == "div": return f"({parts[0]} / {parts[1]})"
                if op == "min": return "math.min(" + ", ".join(parts) + ")"
                if op == "max": return "math.max(" + ", ".join(parts) + ")"
                if op == "abs": return f"math.abs({parts[0]})"
                return f"(-{parts[0]})"
        raise SpecError(f"Cannot translate operand {node!r}")

    def series_ref(self, name, shift):
        builtin = {"open": "open", "high": "high", "low": "low", "close": "close", "hl2": "hl2", "hlc3": "hlc3", "ohlc4": "ohlc4", "volume": "volume",
                   "range": "(high - low)", "body": "math.abs(close - open)", "upper_wick": "(high - math.max(open, close))", "lower_wick": "(math.min(open, close) - low)"}
        if name in builtin and name not in self.spec.series:
            base = builtin[name]
            if shift == 0:
                return base
            if base.startswith("("):
                self.warn(f"Shifted derived price '{name}[{shift}]' is evaluated with Pine history operator on an expression; verify manually.")
            return f"{base}[{shift}]"
        if name not in self.spec.series:
            raise SpecError(f"Unknown series {name}")
        ref = self.v(name)
        ref = ref if shift == 0 else f"{ref}[{shift}]"
        return f"({ref} ? 1.0 : 0.0)" if name in self.bool_series() else ref

    def cond(self, node):
        if "all" in node:
            return "(" + " and ".join(self.cond(c) for c in node["all"]) + ")"
        if "any" in node:
            return "(" + " or ".join(self.cond(c) for c in node["any"]) + ")"
        if "not" in node:
            return f"(not {self.cond(node['not'])})"
        if "is" in node:
            within = node.get("within", 1)
            shift = node.get("shift", 0)
            if isinstance(node["is"], str):
                name = node["is"]
                ref = self.series_ref(name, shift) if name not in self.bool_series() else (self.v(name) if shift == 0 else f"{self.v(name)}[{shift}]")
                is_bool = name in self.bool_series()
            else:
                ref, is_bool = self.operand(node["is"]), False
            truth = ref if is_bool else f"({ref} != 0 and not na({ref}))"
            if within == 1:
                return f"({truth})"
            return f"(nz(ta.barssince({truth}), 99999) < {within})"
        if "seq" in node:
            return self.seq(node)
        if "left" in node:
            a, b, op = self.operand(node["left"]), self.operand(node["right"]), node["op"]
            if op == "crosses_above":
                return f"ta.crossover({a}, {b})"
            if op == "crosses_below":
                return f"ta.crossunder({a}, {b})"
            return f"({a} {op} {b})"
        raise SpecError(f"Cannot translate condition {node!r}")

    def bool_series(self):
        return {name for name, d in self.spec.series.items() if d.type in ("bos", "choch", "sweep", "candle") or (d.type in ("fvg", "order_block") and d.field == "formed")}

    def seq(self, node):
        self.seq_count += 1
        k = self.seq_count
        stages = node["seq"]
        within = node.get("within")
        count = len(stages) - 1
        windows = [None] * count if within is None else [within] * count if isinstance(within, int) else within
        pre = []
        for n, st in enumerate(stages):
            pre.append(f"bool seq{k}_c{n} = {self.cond(st)}")
        inv = f"bool seq{k}_inv = {self.cond(node['invalidate'])}" if node.get("invalidate") else f"bool seq{k}_inv = false"
        restart = "true" if node.get("restart", True) else "false"
        block = [f"var int seq{k}_stage = 0", f"var int seq{k}_last = na", f"bool seq{k}_fire = false"] + pre + [inv]
        block += [f"if seq{k}_stage > 0 and seq{k}_inv", f"    seq{k}_stage := 0"]
        for n, w in enumerate(windows):
            if w is not None:
                block += [f"if seq{k}_stage == {n + 1} and bar_index - seq{k}_last > {w}", f"    seq{k}_stage := 0"]
        if len(stages) == 1:
            block += [f"if seq{k}_c0", f"    seq{k}_fire := true"]
        else:
            block += [f"bool seq{k}_advanced = false",
                      f"if seq{k}_stage > 0 and {restart} and seq{k}_c0", f"    seq{k}_stage := 1", f"    seq{k}_last := bar_index", f"    seq{k}_advanced := true"]
            for n in range(1, len(stages)):
                block += [f"if not seq{k}_advanced and seq{k}_stage == {n} and seq{k}_c{n}", f"    seq{k}_advanced := true",
                          f"    seq{k}_last := bar_index", f"    seq{k}_stage := {n + 1}" if n + 1 < len(stages) else f"    seq{k}_stage := 0",
                          *([f"    seq{k}_fire := true"] if n + 1 == len(stages) else [])]
            block += [f"if not seq{k}_advanced and seq{k}_stage == 0 and seq{k}_c0", f"    seq{k}_stage := 1", f"    seq{k}_last := bar_index"]
        self.seq_blocks.append("\n".join(block))
        return f"seq{k}_fire"

    # -- series ------------------------------------------------------------------------------------
    def series_expr(self, name, d):
        t = d.type
        src = {"close": "close", "open": "open", "high": "high", "low": "low", "hl2": "hl2", "hlc3": "hlc3", "ohlc4": "ohlc4", None: "close"}.get(d.source, "close")
        if t == "price":
            if d.field == "spread":
                self.warn("Spread series is not available in Pine; the translated value is na.")
            return {"range": "(high - low)", "body": "math.abs(close - open)", "upper_wick": "(high - math.max(open, close))", "lower_wick": "(math.min(open, close) - low)",
                    "spread": "na"}.get(d.field, d.field)
        if t in ("sma", "ema", "wma", "rsi", "stdev", "roc"):
            fn = {"sma": "ta.sma", "ema": "ta.ema", "wma": "ta.wma", "rsi": "ta.rsi", "stdev": "ta.stdev", "roc": "ta.roc"}[t]
            return f"{fn}({src}, {d.period})"
        if t == "atr":
            return f"ta.atr({d.period})"
        if t in ("highest", "lowest"):
            field = d.source or ("high" if t == "highest" else "low")
            expr = f"ta.{t}({field}, {d.period})"
            return f"{expr}[1]" if d.exclude_current else expr
        if t == "bbands":
            basis = f"ta.sma({src}, {d.period})"
            dev = f"{d.mult} * ta.stdev({src}, {d.period})"
            return {"upper": f"({basis} + {dev})", "middle": basis, "lower": f"({basis} - {dev})"}[d.field]
        if t == "macd":
            key = f"macd_{short_hash(src, d.fast, d.slow, d.signal)}"
            if key not in self.pre_defs:
                self.pre_defs[key] = (f"float {key}_line = ta.ema({src}, {d.fast}) - ta.ema({src}, {d.slow})\n"
                                      f"float {key}_sig = ta.ema({key}_line, {d.signal})")
            return {"line": f"{key}_line", "signal": f"{key}_sig", "hist": f"({key}_line - {key}_sig)"}[d.field]
        if t == "er":
            return f"(math.sum(math.abs({src} - {src}[1]), {d.period}) == 0 ? 0.0 : math.abs({src} - {src}[{d.period}]) / math.sum(math.abs({src} - {src}[1]), {d.period}))"
        if t in ("swing_high", "swing_low"):
            self.struct(d.n)
            level = f"sh_{d.n}" if t == "swing_high" else f"sl_{d.n}"
            return level if d.field != "age" else f"(bar_index - {'shb' if t == 'swing_high' else 'slb'}_{d.n})"
        if t == "trend":
            self.struct(d.n)
            return f"float(st_{d.n})"
        if t in ("bos", "choch"):
            self.struct(d.n)
            return f"{t}_{d.dir}_{d.n}"
        if t == "sweep":
            return self.sweep(d)
        if t == "fvg":
            return self.fvg(d)
        if t == "order_block":
            self.struct(d.n)
            return self.order_block(d)
        if t == "candle":
            return self.candle(d)
        if t == "prior_day":
            self.warn(f"prior_day uses TradingView's daily session; the strategy's day rollover ({self.spec.day.tz} {self.spec.day.rollover_hour}:00) can differ for the instrument's exchange.")
            return f'request.security(syminfo.tickerid, "D", {d.field}[1], lookahead=barmerge.lookahead_on)'
        if t == "session_range":
            return self.session_range(d)
        if t == "clock":
            tz = d.tz or "UTC"
            return {"hour": f'float(hour(time, "{tz}"))', "minute": f'float(minute(time, "{tz}"))', "weekday": f'float((dayofweek(time, "{tz}") + 5) % 7)',
                    "minute_of_day": f'float(hour(time, "{tz}") * 60 + minute(time, "{tz}"))'}[d.field]
        if t == "vwap":
            self.warn("VWAP uses TradingView's session anchor; it may differ from the strategy's daily rollover.")
            return "ta.vwap(hlc3)"
        raise SpecError(f"Series type {t} cannot be translated")

    def struct(self, n):
        if n in self.struct_seen:
            return
        self.struct_seen.add(n)
        self.struct_code.append(f"""// market structure (swing width {n}); pivots are known only {n} bars after they form
var float sh_{n} = na
var float sl_{n} = na
var int shb_{n} = na
var int slb_{n} = na
var bool brkH_{n} = false
var bool brkL_{n} = false
var int st_{n} = 0
float ph_{n} = ta.pivothigh(high, {n}, {n})
float pl_{n} = ta.pivotlow(low, {n}, {n})
if not na(ph_{n})
    sh_{n} := ph_{n}
    shb_{n} := bar_index
    brkH_{n} := false
if not na(pl_{n})
    sl_{n} := pl_{n}
    slb_{n} := bar_index
    brkL_{n} := false
bool bos_bull_{n} = false
bool bos_bear_{n} = false
bool choch_bull_{n} = false
bool choch_bear_{n} = false
if not na(sh_{n}) and not brkH_{n} and close > sh_{n}
    brkH_{n} := true
    if st_{n} == -1
        choch_bull_{n} := true
    else
        bos_bull_{n} := true
    st_{n} := 1
if not na(sl_{n}) and not brkL_{n} and close < sl_{n}
    brkL_{n} := true
    if st_{n} == 1
        choch_bear_{n} := true
    else
        bos_bear_{n} := true
    st_{n} := -1""")

    def sweep(self, d):
        pen = d.min_penetration or 0.0
        back = True if d.close_back is None else d.close_back
        if d.ref == "swing":
            self.struct(d.n)
            ref = f"sl_{d.n}" if d.dir == "bull" else f"sh_{d.n}"
        elif d.ref == "prior_day":
            self.warn("prior_day uses TradingView's daily session.")
            ref = f'request.security(syminfo.tickerid, "D", {"low" if d.dir == "bull" else "high"}[1], lookahead=barmerge.lookahead_on)'
        elif d.ref == "range":
            ref = f"ta.lowest(low, {d.period})[1]" if d.dir == "bull" else f"ta.highest(high, {d.period})[1]"
        else:
            ref = self.session_range(SeriesDefLike(d, "low" if d.dir == "bull" else "high"))
        if d.dir == "bull":
            return f"(not na({ref}) and low < {ref} - {pen}" + (f" and close > {ref})" if back else ")")
        return f"(not na({ref}) and high > {ref} + {pen}" + (f" and close < {ref})" if back else ")")

    def session_range(self, d):
        key = f"sr_{short_hash(d.start, d.end, d.tz)}"
        if key not in self.range_defs:
            tz = d.tz or "UTC"
            s, e = parse_hhmm(d.start), parse_hhmm(d.end)
            inside = f"(m_{key} >= {s} and m_{key} < {e})" if s < e else f"(m_{key} >= {s} or m_{key} < {e})"
            self.range_defs[key] = f"""float {key}_hi = na
float {key}_lo = na
int m_{key} = hour(time, "{tz}") * 60 + minute(time, "{tz}")
bool in_{key} = {inside}
var float {key}_chi = na
var float {key}_clo = na
var float {key}_ahi = na
var float {key}_alo = na
var bool {key}_act = false
if in_{key}
    {key}_ahi := {key}_act ? math.max({key}_ahi, high) : high
    {key}_alo := {key}_act ? math.min({key}_alo, low) : low
    {key}_act := true
else if {key}_act
    {key}_chi := {key}_ahi
    {key}_clo := {key}_alo
    {key}_act := false"""
        return f"{key}_chi" if d.field == "high" else f"{key}_clo"

    def fvg(self, d):
        key = f"fvg_{d.dir}_{short_hash(d.min_size, d.max_age)}"
        if key not in self.zone_defs:
            ms, age = d.min_size or 0.0, d.max_age or 100
            if d.dir == "bull":
                form = f"low > high[2] and low - high[2] > {ms}"
                set_ = ["    {k}_top := low", "    {k}_bot := high[2]"]
                dead = f"close < {key}_bot"
            else:
                form = f"low[2] > high and low[2] - high > {ms}"
                set_ = ["    {k}_top := low[2]", "    {k}_bot := high"]
                dead = f"close > {key}_top"
            body = "\n".join(x.format(k=key) for x in set_)
            self.zone_defs[key] = f"""var float {key}_top = na
var float {key}_bot = na
var int {key}_born = na
bool {key}_formed = bar_index >= 2 and ({form})
if {key}_formed
{body}
    {key}_born := bar_index
else if not na({key}_top) and (bar_index - {key}_born > {age} or {dead})
    {key}_top := na
    {key}_bot := na"""
        return {"formed": f"{key}_formed", "top": f"{key}_top", "bottom": f"{key}_bot", "mid": f"(({key}_top + {key}_bot) / 2)"}[d.field]

    def order_block(self, d):
        key = f"ob_{d.dir}_{d.n}_{d.max_age or 50}"
        if key not in self.zone_defs:
            event = f"(bos_{d.dir}_{d.n} or choch_{d.dir}_{d.n})"
            cond = "close[k] < open[k]" if d.dir == "bull" else "close[k] > open[k]"
            dead = f"close < {key}_bot" if d.dir == "bull" else f"close > {key}_top"
            self.zone_defs[key] = f"""var float {key}_top = na
var float {key}_bot = na
var int {key}_born = na
bool {key}_formed = false
if {event}
    for k = 1 to 20
        if {cond}
            {key}_top := high[k]
            {key}_bot := low[k]
            {key}_born := bar_index
            {key}_formed := true
            break
else if not na({key}_top) and (bar_index - {key}_born > {d.max_age or 50} or {dead})
    {key}_top := na
    {key}_bot := na"""
        return {"formed": f"{key}_formed", "top": f"{key}_top", "bottom": f"{key}_bot", "mid": f"(({key}_top + {key}_bot) / 2)"}[d.field]

    def candle(self, d):
        p, bull = d.pattern, d.dir == "bull"
        if p == "engulfing":
            return "(close > open and close[1] < open[1] and close >= open[1] and open <= close[1])" if bull else "(close < open and close[1] > open[1] and close <= open[1] and open >= close[1])"
        if p == "pinbar":
            if bull:
                return "((high - low) > 0 and (math.min(open, close) - low) >= 2 * math.abs(close - open) and (math.min(open, close) - low) >= 0.6 * (high - low) and (high - math.max(open, close)) <= 0.25 * (high - low))"
            return "((high - low) > 0 and (high - math.max(open, close)) >= 2 * math.abs(close - open) and (high - math.max(open, close)) >= 0.6 * (high - low) and (math.min(open, close) - low) <= 0.25 * (high - low))"
        if p == "inside":
            return "(high < high[1] and low > low[1])"
        return "((high - low) > 0 and math.abs(close - open) <= 0.1 * (high - low))"

    # -- generation --------------------------------------------------------------------------------
    def generate(self):
        spec, cfg, inst = self.spec, self.cfg, self.inst
        self.seq_blocks, self.struct_code, self.range_defs, self.zone_defs, self.pre_defs = [], [], {}, {}, {}
        series_lines = []
        for name, d in spec.series.items():
            expr = self.series_expr(name, d)
            if d.tf and TF_SECONDS[d.tf] != TF_SECONDS[spec.timeframe]:
                if d.type in ("bos", "choch", "sweep", "candle", "swing_high", "swing_low", "trend", "fvg", "order_block"):
                    self.no(f"Series '{name}' ({d.type}) on a higher timeframe needs a stateful security() call; replicate it by hand on the {d.tf} chart.")
                    expr = "na"
                else:
                    self.warn(f"HTF series '{name}': Pine exposes the previous COMPLETED {d.tf} bar one chart bar later than the independent engine does, so equivalent signals can be one bar later.")
                    expr = f'request.security(syminfo.tickerid, "{PINE_TF[d.tf]}", ({expr})[1], lookahead=barmerge.lookahead_on)'
            kind = "bool" if name in self.bool_series() else "float"
            series_lines.append((name, f"{kind} {self.v(name)} = {expr}"))
        long_expr = self.cond(spec.entry.long) if spec.entry.long else "false"
        short_expr = self.cond(spec.entry.short) if spec.entry.short else "false"
        inval = self.cond(spec.entry.invalidation) if spec.entry.invalidation else "false"
        bias_bull = self.cond(spec.bias.bull) if spec.bias and spec.bias.bull else "false"
        bias_bear = self.cond(spec.bias.bear) if spec.bias and spec.bias.bear else "false"
        exit_rules = [(self.cond(r.cond), r.directions) for r in spec.exits]
        tgt_exprs = []
        for k, t in enumerate(spec.targets):
            if t.type == "atr":
                tgt_exprs.append(f"{t.mult} * {self.v(t.series)}")
            elif t.type == "level":
                tgt_exprs.append(self.operand(t.ref))
            else:
                tgt_exprs.append("na")
        tick = inst.tick_size
        slip_ticks = max(0, round(((cfg.costs.slippage) + cfg.costs.spread / 2) / tick))
        if cfg.costs.commission_model == "per_lot_side":
            comm = f"commission_type=strategy.commission.cash_per_contract, commission_value={cfg.costs.commission_value / inst.contract_size:.8f}"
        elif cfg.costs.commission_model == "percent_notional_side":
            comm = f"commission_type=strategy.commission.percent, commission_value={cfg.costs.commission_value}"
        elif cfg.costs.commission_model == "fixed_per_trade":
            comm = f"commission_type=strategy.commission.cash_per_order, commission_value={cfg.costs.commission_value / 2}"
        else:
            comm = "commission_type=strategy.commission.percent, commission_value=0.0"
        if spec.risk.max_concurrent > 1:
            self.no("max_concurrent > 1 is not translated; the script trades one position at a time.")
        if spec.entry.order.type != "market" and cfg.limit_fill == "through":
            self.warn("TradingView fills limit orders when price touches the level; the engine's default requires trading through it by one tick, so fills can differ.")
        self.warn("Spread is modelled in Pine as extra slippage ticks on every fill (spread/2 + slippage per side); the engine models bid/ask explicitly.")
        self.warn("Quantities are expressed in units: lots x contract size.")
        lines = self.lines
        emit = self.emit
        emit("//@version=6")
        emit(f"// Generated by Trader Video Intelligence engine {ENGINE_VERSION} - strategy '{spec.name}' - spec hash {digest(spec.dump())[:16]}")
        emit("// Approved-rule translation. Review every UNSUPPORTED / WARNING note before trusting results.")
        emit(f'strategy("{spec.name[:60]}", overlay=true, initial_capital={cfg.initial_capital}, currency=currency.{cfg.account_currency if cfg.account_currency in ("USD","EUR","GBP","JPY","AUD","CAD","CHF","INR") else "USD"}, default_qty_type=strategy.fixed, default_qty_value=1, {comm}, slippage={slip_ticks}, pyramiding=1, calc_on_every_tick=false, process_orders_on_close=false, max_bars_back=5000)')
        emit("")
        r = spec.risk
        emit(f'riskModel = "{r.model}"')
        emit(f'riskValue = input.float({r.value}, "Risk value (percent or cash or lots)", minval=0.0)')
        emit(f'contractSize = input.float({inst.contract_size}, "Units per lot/contract")')
        emit(f'qtyStep = input.float({inst.qty_step}, "Lot step")')
        emit(f'minQty = input.float({inst.min_qty}, "Minimum lots")')
        emit(f'maxQty = input.float({inst.max_qty}, "Maximum lots")')
        emit(f'spreadAtSignal = input.float({cfg.costs.spread}, "Assumed spread (price units)")')
        quote, account = inst.quote_currency.upper(), cfg.account_currency.upper()
        if quote == account:
            emit("convRate = 1.0")
        elif inst.base_currency and inst.base_currency.upper() == account:
            emit("convRate = 1.0 / close")
        else:
            emit(f'convRate = input.float({cfg.conversion_rate or 1.0}, "Account currency per 1 {quote}")')
            self.warn(f"{quote} P&L is converted with the input rate; set it for your account.")
        emit("")
        emit("// ---- indicators and structure (every value uses only bars up to the current one) ----")
        for code in self.struct_code:
            emit(code)
        for code in self.range_defs.values():
            emit(code)
        for code in self.zone_defs.values():
            emit(code)
        for code in self.pre_defs.values():
            emit(code)
        for name, line in series_lines:
            emit(line)
        emit("")
        emit("// ---- signals (evaluated on the close of the bar; orders execute from the next bar) ----")
        emit(f"bool longSignal = {long_expr}")
        emit(f"bool shortSignal = {short_expr}")
        # sequences were registered while compiling the conditions above, so emit them BEFORE their use
        self._insert_sequences()
        emit(f"bool biasBull = {bias_bull}")
        emit(f"bool biasBear = {bias_bear}")
        emit(f"bool invalidated = {inval}")
        for k, (expr, directions) in enumerate(exit_rules):
            emit(f"bool exitRule{k} = {expr}")
        emit("")
        emit("// ---- session, risk and day filters evaluated at the decision time (this bar's close) ----")
        self._emit_filters()
        emit("")
        self._emit_trading(tgt_exprs, exit_rules)
        text = "\n".join(self.lines)
        return {"script": text, "warnings": self.warnings, "unsupported": self.unsupported,
                "equivalence": "NOT_EQUIVALENT_UNSUPPORTED_PARTS" if self.unsupported else ("APPROXIMATE" if self.warnings else "FULL"),
                "specHash": digest(spec.dump()), "engineVersion": ENGINE_VERSION}

    def _insert_sequences(self):
        # Sequence state blocks reference stage conditions, so they must precede longSignal/shortSignal.
        if not self.seq_blocks:
            return
        sig_index = max(i for i, line in enumerate(self.lines) if line.startswith("bool longSignal"))
        blocks = ["// sequence state machines (ordered stages within their windows)"] + self.seq_blocks
        self.lines[sig_index:sig_index] = blocks
        self.seq_blocks = []

    def _emit_filters(self):
        s, r, e = self.spec.sessions, self.spec.risk, self.emit
        tzr = r.day_tz
        e(f'int dowDecision = (dayofweek(time_close, "UTC") + 5) % 7')
        conds = ["true"]
        if s.weekdays is not None:
            conds.append("(" + " or ".join(f"dowDecision == {d}" for d in s.weekdays) + ")")
        def window(w, name):
            e(f'int {name}_min = hour(time_close, "{w.tz}") * 60 + minute(time_close, "{w.tz}")')
            e(f'int {name}_dow = (dayofweek(time_close, "{w.tz}") + 5) % 7')
            start, end = parse_hhmm(w.start), parse_hhmm(w.end)
            days = " or ".join(f"{name}_dow == {d}" for d in w.days) or "false"
            if start < end:
                return f"(({name}_min >= {start} and {name}_min < {end}) and ({days}))"
            e(f'int {name}_prev = ({name}_dow + 6) % 7')
            prev_days = " or ".join(f"{name}_prev == {d}" for d in w.days) or "false"
            return f"(({name}_min >= {start} and ({days})) or ({name}_min < {end} and ({prev_days})))"
        if s.windows:
            conds.append("(" + " or ".join(window(w, f"win{k}") for k, w in enumerate(s.windows)) + ")")
        for k, w in enumerate(s.avoid):
            conds.append("(not " + window(w, f"avoid{k}") + ")")
        for k, b in enumerate(self.cfg.news_blackouts):
            a, z = int(datetime.fromisoformat(b.start.replace("Z", "+00:00")).timestamp() * 1000), int(datetime.fromisoformat(b.end.replace("Z", "+00:00")).timestamp() * 1000)
            conds.append(f"(not (time_close >= {a} and time_close < {z}))")
        e("bool sessionOk = " + " and ".join(conds))
        e(f'int dayKey = year(time_close, "{tzr}") * 10000 + month(time_close, "{tzr}") * 100 + dayofmonth(time_close, "{tzr}")')
        e("var int curDay = na")
        e("var int tradesToday = 0")
        e("var float dayStartEquity = na")
        e("var float dayR = 0.0")
        e("var float dayPnl = 0.0")
        e("var int consecLosses = 0")
        e("var int closedSeen = 0")
        e("var float lastRisk = na")
        e("var float tradePnl = 0.0")
        e("if na(curDay) or dayKey != curDay")
        e("    curDay := dayKey")
        e("    tradesToday := 0")
        e("    dayStartEquity := strategy.equity")
        e("    dayR := 0.0")
        e("    dayPnl := 0.0")
        e("    consecLosses := 0")
        e("if strategy.closedtrades > closedSeen")
        e("    for q = closedSeen to strategy.closedtrades - 1")
        e("        p = strategy.closedtrades.profit(q)")
        e("        dayPnl += p")
        e("        tradePnl += p")
        e("        dayR += na(lastRisk) or lastRisk == 0 ? 0.0 : p / lastRisk")
        e("    closedSeen := strategy.closedtrades")
        e("if strategy.position_size == 0 and tradePnl != 0")
        e("    consecLosses := tradePnl < 0 ? consecLosses + 1 : 0")
        e("    tradePnl := 0.0")
        gates = ["sessionOk", "strategy.position_size == 0", "na(pendingBar)"]
        if r.max_trades_per_day:
            gates.append(f"tradesToday < {r.max_trades_per_day}")
        if r.max_daily_loss_r:
            gates.append(f"dayR > -{r.max_daily_loss_r}")
        if r.max_daily_loss_pct:
            gates.append(f"dayPnl > -{r.max_daily_loss_pct / 100.0} * dayStartEquity")
        if r.max_consecutive_losses:
            gates.append(f"consecLosses < {r.max_consecutive_losses}")
        if r.max_spread:
            gates.append(f"spreadAtSignal <= {r.max_spread}")
        if self.spec.entry.cooldown_bars:
            gates.append(f"(na(lastExitBar) or bar_index - lastExitBar > {self.spec.entry.cooldown_bars})")
        e("var int pendingBar = na")
        e("var int lastExitBar = na")
        e("bool canTrade = " + " and ".join(gates))

    def _emit_trading(self, tgt_exprs, exit_rules):
        spec, r, e = self.spec, self.spec.risk, self.emit
        st = spec.stop
        order = spec.entry.order
        e("// ---- order placement ----")
        e("var float pStop = na")
        e("var float pQty = na")
        e("var float pRisk = na")
        e("var int pDir = 0")
        e("var float stop0 = na")
        e("var float curStop = na")
        e("var float entryPx = na")
        e("var float qty0 = na")
        e("var bool beDone = false")
        e("var int barsHeld = 0")
        e("var float bestPx = na")
        for k in range(len(spec.targets)):
            e(f"var float tpX{k} = na")
            e(f"var float tpOff{k} = na")
        e("float sigDir = 0")
        e("if longSignal and shortSignal")
        e("    sigDir := 0")
        e("else if longSignal")
        e("    sigDir := 1")
        e("else if shortSignal")
        e("    sigDir := -1")
        e("if sigDir != 0 and canTrade")
        e("    float ref = " + ("close + sigDir * spreadAtSignal / 2" if order.type == "market" else f"{self.operand(order.price)} + {order.offset_points}"))
        if st.type == "atr":
            e(f"    float stopLvl = ref - sigDir * {st.mult} * {self.v(st.series)}")
        elif st.type == "points":
            e(f"    float stopLvl = ref - sigDir * {st.value}")
        elif st.type == "percent":
            e(f"    float stopLvl = ref * (1 - sigDir * {st.value} / 100.0)")
        elif st.type == "signal_candle":
            e(f"    float stopLvl = sigDir == 1 ? low - {st.buffer} : high + {st.buffer}")
        else:
            e(f"    float stopLvl = {self.operand(st.ref)} - sigDir * {st.buffer}")
        e("    float dist = sigDir * (ref - stopLvl)")
        e(f"    if not na(dist) and dist > {self.inst.tick_size}")
        if r.model == "fixed_qty":
            e("        float lots = math.floor(riskValue / qtyStep) * qtyStep")
        else:
            amount = {"percent_equity": "strategy.equity * riskValue / 100.0", "percent_initial": "strategy.initial_capital * riskValue / 100.0", "fixed_cash": "riskValue"}[r.model]
            e(f"        float riskAmt = {amount}")
            e("        float lots = math.floor((riskAmt / (dist * contractSize * convRate)) / qtyStep + 0.000000001) * qtyStep")
        e("        if lots >= minQty")
        e("            lots := math.min(lots, maxQty)")
        e("            pStop := stopLvl")
        e("            pQty := lots * contractSize")
        e("            pRisk := lots * dist * contractSize * convRate")
        e("            pDir := int(sigDir)")
        e("            pendingBar := bar_index")
        e("            tradesToday += 1")
        e("            lastRisk := pRisk")
        for k, t in enumerate(spec.targets):
            if t.type in ("atr", "level"):
                e(f"            tpOff{k} := {tgt_exprs[k]}")
        idname = '"L"' if True else ""
        entry = {"market": "", "limit": ", limit=ref", "stop": ", stop=ref"}[order.type]
        e(f'            if sigDir == 1')
        e(f'                strategy.entry("L", strategy.long, qty=pQty{entry})')
        e("            else")
        e(f'                strategy.entry("S", strategy.short, qty=pQty{entry})')
        e('            string fromNow = sigDir == 1 ? "L" : "S"')
        if spec.targets:
            for k, t in enumerate(spec.targets):
                if t.type == "rr":
                    price = f"ref + sigDir * {t.value} * dist"
                elif t.type == "points":
                    price = f"ref + sigDir * {t.value}"
                elif t.type == "atr":
                    price = f"ref + sigDir * tpOff{k}"
                else:
                    price = f"tpOff{k}"
                e(f'            strategy.exit("X{k + 1}", fromNow, qty_percent={round(t.portion * 100, 4)}, stop=stopLvl, limit={price})')
        else:
            e('            strategy.exit("X1", fromNow, stop=stopLvl)')
        e("")
        e("// a pending order that has not filled within its validity (or was invalidated) is cancelled")
        if order.type == "market":
            e("if not na(pendingBar) and bar_index > pendingBar + 1 and strategy.position_size == 0")
        else:
            e(f"if not na(pendingBar) and (bar_index > pendingBar + {order.expire_bars} or invalidated) and strategy.position_size == 0")
        e('    strategy.cancel("L")')
        e('    strategy.cancel("S")')
        e("    pendingBar := na")
        e("")
        e("// ---- position management (decided at the close, effective from the next bar) ----")
        e("if strategy.position_size != 0")
        e("    int sgn = strategy.position_size > 0 ? 1 : -1")
        e('    string fromId = sgn == 1 ? "L" : "S"')
        e("    if na(entryPx) or not na(pendingBar)")
        e("        entryPx := strategy.position_avg_price")
        e("        qty0 := math.abs(strategy.position_size)")
        e("        stop0 := pStop")
        e("        curStop := pStop")
        e("        beDone := false")
        e("        barsHeld := 0")
        e("        bestPx := entryPx")
        e("        pendingBar := na")
        e("    barsHeld += 1")
        e("    bestPx := sgn == 1 ? math.max(bestPx, high) : math.min(bestPx, low)")
        e("    float riskPx = math.abs(entryPx - stop0)")
        mg = spec.management
        be = mg.breakeven
        if be:
            if be.trigger_r:
                e(f"    bool trigHit = sgn * (bestPx - entryPx) >= {be.trigger_r} * riskPx")
            else:
                e("    bool trigHit = false")
            if be.after_target:
                e("    bool tpHit = math.abs(strategy.position_size) < qty0 - 0.000000001")
            else:
                e("    bool tpHit = false")
            e("    if not beDone and (trigHit or tpHit)")
            e(f"        float beLvl = entryPx + sgn * {be.offset_points}")
            e("        if sgn * (close - beLvl) > 0")
            e("            curStop := beLvl")
            e("            beDone := true")
        tr = mg.trailing
        if tr:
            e(f"    if sgn * (bestPx - entryPx) >= {tr.start_r} * riskPx")
            if tr.type == "points":
                e(f"        float cand = bestPx - sgn * {tr.value}")
            elif tr.type == "atr":
                e(f"        float cand = bestPx - sgn * {tr.mult} * {self.v(tr.series)}")
            elif tr.type == "candle":
                e(f"        float cand = sgn == 1 ? low - {tr.buffer} : high + {tr.buffer}")
            else:
                self.struct(tr.n)
                e(f"        float cand = sgn == 1 ? sl_{tr.n} - {tr.buffer} : sh_{tr.n} + {tr.buffer}")
            e("        if not na(cand) and sgn * (cand - curStop) > 0 and sgn * (close - cand) > 0")
            e("            curStop := cand")
        # exits
        if spec.targets:
            for k, t in enumerate(spec.targets):
                if t.type == "rr":
                    price = f"entryPx + sgn * {t.value} * riskPx"
                elif t.type == "points":
                    price = f"entryPx + sgn * {t.value}"
                elif t.type == "atr":
                    price = f"entryPx + sgn * tpOff{k}"
                else:
                    price = f"tpOff{k}"
                pct = round(t.portion * 100, 4)
                e(f'    strategy.exit("X{k + 1}", fromId, qty_percent={pct}, stop=curStop, limit={price})')
        else:
            e('    strategy.exit("X1", fromId, stop=curStop)')
        flat_due = []
        if mg.max_bars:
            e(f"    if barsHeld >= {mg.max_bars}")
            e("        strategy.close(fromId, comment=\"TIME_STOP\")")
        for k, (expr, directions) in enumerate(exit_rules):
            side = "" if directions == "BOTH" else (" and sgn == 1" if directions == "LONG" else " and sgn == -1")
            e(f"    if exitRule{k}{side}")
            e('        strategy.close(fromId, comment="EXIT_RULE")')
        if spec.sessions.force_flat:
            f = spec.sessions.force_flat
            minute = parse_hhmm(f.time)
            e(f'    int flatMin = hour(time_close, "{f.tz}") * 60 + minute(time_close, "{f.tz}")')
            e(f"    if flatMin >= {minute}")
            e('        strategy.close(fromId, comment="SESSION_END")')
        e("else")
        e("    if not na(entryPx)")
        e("        lastExitBar := bar_index")
        e("    entryPx := na")
        e("")
        e('plotshape(longSignal and canTrade, "Long signal", shape.triangleup, location.belowbar, color=color.green, size=size.tiny)')
        e('plotshape(shortSignal and canTrade, "Short signal", shape.triangledown, location.abovebar, color=color.red, size=size.tiny)')


class SeriesDefLike:
    """Adapter so session_range can be reused for a sweep reference."""
    def __init__(self, d, field):
        self.start, self.end, self.tz, self.field = d.start, d.end, d.tz, field


def generate_pine(spec: StrategySpec, cfg: BacktestConfig | None = None) -> dict:
    gen = PineGen(spec, cfg)
    result = gen.generate()
    audit = audit_pine(result["script"])
    result["audit"] = audit
    return result


# ---- static audit -----------------------------------------------------------------------------------

def audit_pine(script: str) -> dict:
    """Static review of lookahead, repainting, order timing and fill assumptions (SRS 11.3)."""
    findings = []
    def add(level, line, message):
        findings.append({"severity": level, "line": line, "message": message})
    lines = script.splitlines()
    text = script
    for n, line in enumerate(lines, start=1):
        code = line.split("//")[0]
        if "request.security" in code:
            call = code
            lookahead_on = "lookahead_on" in call or "lookahead=true" in call.replace(" ", "")
            lookahead_off = "lookahead_off" in call
            offset = re.search(r"\)\s*\[\s*([1-9]\d*)\s*\]|\w\s*\[\s*([1-9]\d*)\s*\]\s*,\s*lookahead", call) is not None or "[1]" in call
            if lookahead_on and not offset:
                add("FAIL", n, "request.security with lookahead_on but no historical offset ([1]): the unfinished higher-timeframe bar leaks future values into earlier bars.")
            elif lookahead_on:
                add("PASS", n, "request.security uses lookahead_on with a [1] offset: the previous COMPLETED higher-timeframe bar only (non-repainting pattern).")
            elif lookahead_off:
                add("WARN", n, "request.security with lookahead_off repaints on realtime bars and, on history, reflects the final value of a forming bar.")
            else:
                add("WARN", n, "request.security without an explicit lookahead argument; set it explicitly.")
        if re.search(r"\w\s*\[\s*-\d", code):
            add("FAIL", n, "Negative history index reads the future.")
        if "timenow" in code:
            add("FAIL", n, "timenow exposes the real-world present to historical bars.")
        if re.search(r"barstate\.(isrealtime|islast|isconfirmed|isnew)", code):
            add("WARN", n, "barstate.* conditions behave differently on history and realtime and can repaint.")
        if re.search(r"calc_on_every_tick\s*=\s*true", code):
            add("FAIL", n, "calc_on_every_tick=true makes realtime signals differ from history.")
        if re.search(r"process_orders_on_close\s*=\s*true", code):
            add("WARN", n, "process_orders_on_close=true fills at the signal bar's close, an optimistic assumption.")
        if re.search(r"use_bar_magnifier\s*=\s*true", code):
            add("INFO", n, "Bar Magnifier needs a TradingView plan that supports it and intrabar data; fills are more precise when available.")
        if re.search(r"request\.(security_lower_tf|financial|earnings|dividends|splits|economic)", code):
            add("WARN", n, "Fundamental / lower-timeframe requests must be checked for announcement-time versus period-time leakage.")
        if re.search(r"ta\.(pivothigh|pivotlow)", code):
            add("INFO", n, "ta.pivot* confirms only after its right-hand bars; the generated logic stores the level on the confirmation bar.")
    if "strategy(" not in text:
        add("FAIL", 0, "No strategy() declaration: an indicator cannot be backtested.")
    if not re.search(r"strategy\.(entry|order)\(", text):
        add("FAIL", 0, "No strategy.entry/order call found.")
    if "strategy.exit" not in text and "strategy.close" not in text:
        add("WARN", 0, "No exit logic found.")
    verdict = "FAIL" if any(f["severity"] == "FAIL" for f in findings) else "WARN" if any(f["severity"] == "WARN" for f in findings) else "PASS"
    return {"verdict": verdict, "findings": findings, "checked": ["request.security lookahead/offset", "negative history index", "timenow", "barstate.*", "calc_on_every_tick",
                                                              "process_orders_on_close", "bar magnifier", "fundamental requests", "order timing"]}


# ---- TradingView Strategy Tester import / comparison ------------------------------------------------------

def _norm(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def import_tv_trades(content: bytes, tz: str = "UTC") -> list[dict]:
    """Parse the 'List of trades' export (CSV). Entry/exit rows share a trade number; pieces of partial exits are separate trade numbers."""
    text = content.decode("utf-8-sig")
    sample = text[:2048]
    delimiter = ";" if sample.count(";") > sample.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if not reader.fieldnames:
        raise ValueError("The file has no header row.")
    cols = {_norm(c): c for c in reader.fieldnames}
    def find(*options, prefix=False):
        for o in options:
            if o in cols:
                return cols[o]
        if prefix:
            return next((cols[k] for k in cols if any(k.startswith(o) for o in options)), None)
        return None
    c_no = find("trade", "trade#", "tradeno")
    c_type, c_time = find("type"), find("datetime", "dateandtime", "datetimeutc", "date")
    c_price = find("price", prefix=True)
    c_qty = find("positionsizeqty", "sizeqty", "positionsize", "qty", prefix=True)
    pnl_candidates = sorted((k for k in cols if k.startswith("netpl")), key=len, reverse=True)
    c_pnl = cols[pnl_candidates[0]] if pnl_candidates else find("profit", prefix=True)
    c_signal = find("signal")
    if not all((c_no, c_type, c_time, c_price)):
        raise ValueError("This does not look like a Strategy Tester 'List of trades' export (needs Trade #, Type, Date/Time, Price).")
    zone = ZoneInfo(tz)
    def parse_time(value):
        value = value.strip()
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%d-%m-%Y %H:%M", "%m/%d/%Y %H:%M", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                parsed = datetime.strptime(value, fmt)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=zone)
                return int(parsed.timestamp())
            except ValueError:
                continue
        raise ValueError(f"Unrecognised date/time '{value}'.")
    def number(value):
        cleaned = re.sub(r"[^0-9eE+\-.]", "", (value or "").replace("−", "-").replace(",", ""))
        return float(cleaned) if cleaned not in ("", "-", "+") else None
    grouped = {}
    for row in reader:
        kind = row[c_type].strip().lower()
        no = row[c_no].strip()
        item = grouped.setdefault(no, {})
        direction = "LONG" if "long" in kind else "SHORT"
        record = {"ts": parse_time(row[c_time]), "price": number(row[c_price]), "qty": number(row[c_qty]) if c_qty else None, "pnl": number(row[c_pnl]) if c_pnl else None,
                  "signal": row[c_signal].strip() if c_signal else ""}
        if kind.startswith("entry"):
            item["entry"], item["direction"] = record, direction
        elif kind.startswith("exit"):
            item["exit"], item["direction"] = record, direction
    trades = []
    for no, item in grouped.items():
        if "entry" not in item or "exit" not in item:
            continue
        trades.append({"tvTradeNo": no, "direction": item["direction"], "entryTs": item["entry"]["ts"], "entry": item["entry"]["price"], "exitTs": item["exit"]["ts"],
                       "exit": item["exit"]["price"], "qty": item["entry"]["qty"], "netPnl": item["exit"]["pnl"], "signal": item["entry"]["signal"], "exitSignal": item["exit"]["signal"]})
    trades.sort(key=lambda t: t["entryTs"])
    return trades


def compare_trades(ours: list[dict], tv: list[dict], tf_seconds: int, price_tol: float, contract_size: float, time_tol_bars: int = 1, pnl_tol_pct: float = 2.0) -> dict:
    """Pair equivalent trades and report every material difference. Never declares equivalence on partial agreement."""
    unused = list(range(len(tv)))
    pairs, only_ours = [], []
    for t in ours:
        best, gap = None, None
        for k in unused:
            c = tv[k]
            if c["direction"] != t["direction"]:
                continue
            delta = abs(c["entryTs"] - t["entryTs"])
            if delta <= time_tol_bars * tf_seconds and (gap is None or delta < gap):
                best, gap = k, delta
        if best is None:
            only_ours.append(t)
            continue
        unused.remove(best)
        c = tv[best]
        qty_ours = t["qty"] * contract_size
        diffs = {"entryTimeBars": (c["entryTs"] - t["entryTs"]) / tf_seconds, "entryPrice": (c["entry"] - t["entry"]) if c["entry"] is not None else None,
                 "exitPrice": (c["exit"] - t["finalExit"]) if c["exit"] is not None else None, "exitTimeBars": (c["exitTs"] - t["exitTs"]) / tf_seconds,
                 "qtyUnits": (c["qty"] - qty_ours) if c["qty"] is not None else None, "netPnl": (c["netPnl"] - t["netPnl"]) if c["netPnl"] is not None else None}
        reasons = []
        if abs(diffs["entryTimeBars"]) > 0:
            reasons.append("entry bar differs (HTF one-bar delay, limit touch/through rule or different data)")
        if diffs["entryPrice"] is not None and abs(diffs["entryPrice"]) > price_tol:
            reasons.append("entry price differs (spread modelling, slippage or data feed)")
        if diffs["exitPrice"] is not None and abs(diffs["exitPrice"]) > price_tol:
            reasons.append("exit price differs (same-bar stop/target convention, spread or data feed)")
        if abs(diffs["exitTimeBars"]) > time_tol_bars:
            reasons.append("exit time differs")
        if diffs["qtyUnits"] is not None and abs(diffs["qtyUnits"]) > max(1e-9, 0.01 * qty_ours):
            reasons.append("position size differs (equity basis or rounding)")
        if diffs["netPnl"] is not None and abs(diffs["netPnl"]) > max(1.0, pnl_tol_pct / 100.0 * abs(t["netPnl"])):
            reasons.append("net P&L differs")
        pairs.append({"ourTrade": t["id"], "tvTrade": c["tvTradeNo"], "differences": diffs, "reasons": reasons, "equivalent": not reasons})
    only_tv = [tv[k] for k in unused]
    equivalent = bool(pairs) and not only_ours and not only_tv and all(p["equivalent"] for p in pairs)
    return {"matched": len(pairs), "onlyInEngine": [t["id"] for t in only_ours], "onlyInTradingView": [t["tvTradeNo"] for t in only_tv], "pairs": pairs,
            "verdict": "EQUIVALENT_WITHIN_TOLERANCE" if equivalent else "NOT_EQUIVALENT",
            "explanation": "All trades matched within tolerance." if equivalent else "The two engines disagree; the differences above must be explained before either run is relied upon. Runs are never described as equivalent when they are not.",
            "tolerances": {"entryTimeBars": time_tol_bars, "priceUnits": price_tol, "pnlPct": pnl_tol_pct}}
