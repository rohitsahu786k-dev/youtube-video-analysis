"""Event-driven, strictly causal bar simulator.

Per bar i the order of work is fixed and never reads bars after i:
  1. pending orders created at earlier closes try to fill inside bar i,
  2. open positions are managed inside bar i (gap, stop, targets) with conservative same-bar rules,
  3. at the CLOSE of bar i management updates and new signals are decided; they take effect from bar i+1.
Prices are mid; longs exit on the bid and shorts on the ask (spread/2 either side).
"""
from bisect import bisect_left
from datetime import datetime, timezone

from pyapp.engine import ENGINE_VERSION
from pyapp.engine.clock import in_window, local, session_label, iso_utc
from pyapp.engine.conditions import Ctx, compile_cond, compile_operand
from pyapp.engine.data import CandleSet, parse_iso
from pyapp.engine.instruments import conversion_factor, resolve_instrument, size_position, round_down_qty
from pyapp.engine.series import Bars, Factory, parse_hhmm
from pyapp.engine.spec import BacktestConfig, SeriesDef, SpecError, StrategySpec, TF_SECONDS

EPS = 1e-12


class LookaheadError(RuntimeError):
    """The simulator tried to read a bar that has not closed yet."""


class _Guarded(list):
    """List that refuses to reveal any element beyond the bar currently being processed."""
    def __init__(self, data, cursor):
        super().__init__(data)
        self._cursor = cursor

    def __getitem__(self, key):
        limit = self._cursor[0]
        if isinstance(key, slice):
            stop = key.stop if key.stop is not None else len(self)
            if stop - 1 > limit or (key.start is not None and key.start < 0):
                raise LookaheadError(f"Slice {key} reaches beyond bar {limit}")
        elif key > limit or key < 0:
            raise LookaheadError(f"Read of bar {key} while processing bar {limit}")
        return super().__getitem__(key)


class Position:
    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.legs, self.events = [], []
        self.pending_stop = None
        self.exit_next = None
        self.armed_be = False
        self.be_done = False
        self.ambiguous = False
        self.bars_held = 0
        self.trail_on = False


class Simulator:
    def __init__(self, spec: StrategySpec, candles: CandleSet, config: BacktestConfig, fine: CandleSet | None = None, guard: bool = False):
        self.spec, self.cfg, self.cs, self.fine, self.guard = spec, config, candles, fine, guard
        self.tf = TF_SECONDS[spec.timeframe]
        self.n = len(candles)
        if self.n < 2:
            raise SpecError("At least two candles are required.")
        symbol = config.symbol or candles.meta.get("symbol") or (config.instrument.symbol if config.instrument else "")
        self.symbol = symbol
        self.inst = resolve_instrument(symbol, config.instrument)
        self.margin_rate = 1.0 / config.leverage if config.leverage else self.inst.margin_rate
        conversion_factor(self.inst, config.account_currency, candles.c[0], config.conversion_rate)  # fail fast
        self.bars = Bars(candles, self.tf)
        self.factory = Factory(self.bars, spec.day)
        series = {name: self.factory.build(name, d) for name, d in spec.series.items()}
        for name, d in {"__er20": SeriesDef(type="er", period=20), "__atr14": SeriesDef(type="atr", period=14)}.items():
            series[name] = self.factory.build(name, d)
        self.ctx = Ctx(self.bars, series)
        e = spec.entry
        if e.long is None and e.short is None:
            raise SpecError("The strategy must define an entry condition for long, short or both.")
        self.long_cond = compile_cond(e.long, self.ctx, "entry.long")
        self.short_cond = compile_cond(e.short, self.ctx, "entry.short")
        self.invalidation = compile_cond(e.invalidation, self.ctx, "entry.invalidation")
        self.bias_bull = compile_cond(spec.bias.bull, self.ctx, "bias.bull") if spec.bias else None
        self.bias_bear = compile_cond(spec.bias.bear, self.ctx, "bias.bear") if spec.bias else None
        self.exit_rules = [(compile_cond(r.cond, self.ctx, f"exits[{k}]"), r.directions) for k, r in enumerate(spec.exits)]
        self.order_price = None
        if e.order.type != "market":
            if e.order.price is None:
                raise SpecError("Limit/stop entry orders need entry.order.price (a series, number or expression).")
            self.order_price = compile_operand(e.order.price, self.ctx, "entry.order.price")
        self._compile_stop_targets()
        sessions = spec.sessions
        self.windows, self.avoid = sessions.windows, sessions.avoid
        self.flat = None
        if sessions.force_flat:
            self.flat = (parse_hhmm(sessions.force_flat.time), sessions.force_flat.tz)
        self.news = [(parse_iso(b.start), parse_iso(b.end)) for b in config.news_blackouts]
        self.start_ts = parse_iso(config.start) if config.start else None
        self.fine_t = fine.t if fine else None
        self.fine_secs = None
        if fine and config.same_bar_policy == "lower_tf":
            sizes = [fine.t[k + 1] - fine.t[k] for k in range(min(len(fine) - 1, 50))]
            self.fine_secs = min(sizes) if sizes else None
        self.result = None

    # ---- setup ---------------------------------------------------------------------------------
    def _compile_stop_targets(self):
        st = self.spec.stop
        self.stop_atr = None
        if st.type == "atr":
            if st.mult is None or not st.series or st.series not in self.ctx.series:
                raise SpecError("An ATR stop requires 'mult' and 'series' naming an atr series.")
            self.stop_atr = self.ctx.series[st.series]
        elif st.type in ("points", "percent") and st.value is None:
            raise SpecError(f"A {st.type} stop requires 'value'.")
        elif st.type in ("level", "swing"):
            if st.ref is None:
                raise SpecError(f"A {st.type} stop requires 'ref'.")
            self.stop_ref = compile_operand(st.ref, self.ctx, "stop.ref")
        self.target_refs, self.target_atr = [], []
        for k, t in enumerate(self.spec.targets):
            if t.type in ("rr", "points") and t.value is None:
                raise SpecError(f"targets[{k}] ({t.type}) requires 'value'.")
            if t.type == "atr":
                if t.mult is None or not t.series or t.series not in self.ctx.series:
                    raise SpecError(f"targets[{k}] (atr) requires 'mult' and 'series'.")
            if t.type == "level":
                if t.ref is None:
                    raise SpecError(f"targets[{k}] (level) requires 'ref'.")
            self.target_refs.append(compile_operand(t.ref, self.ctx, f"targets[{k}].ref") if t.type == "level" else None)
        tr = self.spec.management.trailing
        self.trail_atr = None
        if tr:
            if tr.type == "atr":
                if tr.mult is None or not tr.series or tr.series not in self.ctx.series:
                    raise SpecError("An ATR trailing stop requires 'mult' and 'series'.")
                self.trail_atr = self.ctx.series[tr.series]
            elif tr.type == "points" and tr.value is None:
                raise SpecError("A points trailing stop requires 'value'.")
            elif tr.type == "swing" and tr.n is None:
                raise SpecError("A swing trailing stop requires 'n'.")
            if tr.type == "swing":
                from pyapp.engine.series import swings
                self.trail_swings = swings(self.bars, tr.n)
        be = self.spec.management.breakeven
        if be and be.trigger_r is None and be.after_target is None:
            raise SpecError("Break-even needs trigger_r or after_target.")

    # ---- helpers -------------------------------------------------------------------------------
    def spread(self, i):
        if self.cfg.costs.use_dataset_spread and self.cs.spread is not None:
            return self.cs.spread[i]
        return self.cfg.costs.spread

    def conv(self, price):
        return conversion_factor(self.inst, self.cfg.account_currency, price, self.cfg.conversion_rate)

    def commission(self, qty, price):
        c = self.cfg.costs
        if c.commission_model == "per_lot_side":
            return c.commission_value * qty
        if c.commission_model == "percent_notional_side":
            return c.commission_value / 100.0 * qty * self.inst.contract_size * price * self.conv(price)
        return 0.0

    def _log(self, kind, i, **detail):
        self.events.append({"type": kind, "idx": i, "time": iso_utc(self.bars.t[i]), **detail})

    def _allowed(self, decision_ts):
        s = self.spec.sessions
        when = local(decision_ts, "UTC")
        if s.weekdays is not None and when.weekday() not in s.weekdays:
            return False, "WEEKDAY_FILTER"
        if self.windows and not any(in_window(w, decision_ts) for w in self.windows):
            return False, "OUTSIDE_SESSION"
        if any(in_window(w, decision_ts) for w in self.avoid):
            return False, "AVOID_WINDOW"
        if any(a <= decision_ts < b for a, b in self.news):
            return False, "NEWS_BLACKOUT"
        return True, None

    def _day_key(self, ts):
        return local(ts, self.spec.risk.day_tz).date().toordinal()

    def _day(self, key):
        return self.days.setdefault(key, {"trades": 0, "pnl": 0.0, "r": 0.0, "consec": 0, "start_balance": self.balance})

    def _margin_used(self):
        return sum(p.qty_open * self.inst.contract_size * p.entry * self.conv(p.entry) * self.margin_rate for p in self.positions)

    # ---- run -----------------------------------------------------------------------------------
    def run(self):
        self.events, self.trades, self.orders_log = [], [], []
        self.orders, self.positions, self.days = [], [], {}
        self.balance = self.cfg.initial_capital
        self.equity, self.halted = [], False
        self.counters = {"signals": 0, "ordersPlaced": 0, "filled": 0, "expired": 0, "rejected": {}, "invalidated": 0}
        self.last_close_idx, self.next_order_id = -10**9, 1
        self.warnings = []
        if self.guard:
            cursor = self._cursor = [-1]
            raw = self.cs
            self.cs = CandleSet(_Guarded(raw.t, cursor), _Guarded(raw.o, cursor), _Guarded(raw.h, cursor), _Guarded(raw.l, cursor), _Guarded(raw.c, cursor),
                                _Guarded(raw.v, cursor), None if raw.spread is None else _Guarded(raw.spread, cursor), raw.meta)
        for i in range(self.n):
            if self.guard:
                self._cursor[0] = i
            self._fill_orders(i)
            self._process_positions(i)
            self._close_of_bar(i)
        self._finish()
        return {"engine": "independent-python", "engineVersion": ENGINE_VERSION, "symbol": self.symbol, "instrument": self.inst.model_dump(),
                "trades": self.trades, "events": self.events, "orders": self.orders_log, "equity": self.equity,
                "counters": self.counters, "warnings": self.warnings, "balanceEnd": self.balance}

    # ---- order filling -------------------------------------------------------------------------
    def _reject(self, reason, i, **detail):
        self.counters["rejected"][reason] = self.counters["rejected"].get(reason, 0) + 1
        if sum(self.counters["rejected"].values()) <= 2000:
            self._log("REJECTED", i, reason=reason, **detail)

    def _fill_orders(self, i):
        keep = []
        for od in self.orders:
            if i > od["validUntil"]:
                self.counters["expired"] += 1
                self._log("ORDER_EXPIRED", i, orderId=od["id"])
                od["status"] = "EXPIRED"
                continue
            if self._try_fill(od, i):
                continue
            keep.append(od)
        self.orders = keep

    def _try_fill(self, od, i):
        sign, s, slip = od["sign"], self.spread(i), self.cfg.costs.slippage
        o, h, l = self.cs.o[i], self.cs.h[i], self.cs.l[i]
        tick = self.inst.tick_size
        side = sign * s / 2
        fill, at_open, kind = None, False, None
        if od["type"] == "market":
            fill, at_open, kind = o + side + sign * slip, True, "open"
        elif od["type"] == "limit":
            level = od["level"]
            ref = (l + side) if sign == 1 else (h + side)
            through = (ref <= level - tick) if sign == 1 else (ref >= level + tick)
            touched = (ref <= level) if sign == 1 else (ref >= level)
            hit = touched if self.cfg.limit_fill == "touch" else through
            open_px = o + side
            if (sign == 1 and open_px <= level) or (sign == -1 and open_px >= level):
                fill, at_open, kind = open_px, True, "open"
            elif hit:
                fill, kind = level, "intrabar_limit"
        else:
            level = od["level"]
            ref = (h + side) if sign == 1 else (l + side)
            open_px = o + side
            if (sign == 1 and ref >= level) or (sign == -1 and ref <= level):
                at_open = (sign == 1 and open_px >= level) or (sign == -1 and open_px <= level)
                fill = (max(level, open_px) if sign == 1 else min(level, open_px)) + sign * slip
                kind = "open" if at_open else "intrabar_stop"
        if fill is None:
            return False
        if not sign * (fill - od["stop"]) > 0:
            od["status"] = "REJECTED_FILL_INVALID"
            self._reject("FILL_INVALIDATES_STOP", i, orderId=od["id"], fill=fill)
            return True
        targets = self._targets_at_fill(od, fill)
        if targets is None:
            od["status"] = "REJECTED_TARGET_INVALID"
            self._reject("TARGET_ON_WRONG_SIDE", i, orderId=od["id"], fill=fill)
            return True
        mid_entry = fill - side - (sign * slip if od["type"] in ("market", "stop") else 0.0)
        commission = self.commission(od["qty"], fill)
        if self.cfg.costs.commission_model == "fixed_per_trade":
            commission = self.cfg.costs.commission_value
        self.balance -= commission
        pos = Position(id=od["id"], sign=sign, direction="LONG" if sign == 1 else "SHORT", qty=od["qty"], qty_open=od["qty"], entry=fill,
                       entry_mid=mid_entry, entry_idx=i, entry_ts=self.cs.t[i], stop=od["stop"], stop0=od["stop"], targets=targets,
                       risk_per_unit=sign * (fill - od["stop"]), planned_risk=od["riskAmount"], planned_dist=od["plannedDist"],
                       order=od, kind=kind, commission=commission, best=fill, worst=fill, spread_entry=s)
        od["status"] = "FILLED"
        self.positions.append(pos)
        self.counters["filled"] += 1
        pos.events.append({"type": "ORDER_FILLED", "idx": i, "time": iso_utc(self.cs.t[i]), "price": fill, "qty": od["qty"], "detail": f"{od['type']} order filled ({kind})"})
        self._log("ORDER_FILLED", i, orderId=od["id"], price=fill, qty=od["qty"])
        return True

    def _targets_at_fill(self, od, fill):
        sign, risk = od["sign"], od["sign"] * (fill - od["stop"])
        out, qty_left = [], od["qty"]
        specs = self.spec.targets
        for k, t in enumerate(specs):
            if t.type == "rr":
                price = fill + sign * t.value * risk
            elif t.type == "points":
                price = fill + sign * t.value
            elif t.type == "atr":
                price = fill + sign * t.mult * od["atr_at_signal"][k]
            else:
                price = od["level_targets"][k]
                if price is None:
                    return None
            if not sign * (price - fill) > 0:
                return None
            qty = round_down_qty(od["qty"] * t.portion, self.inst)
            if k == len(specs) - 1 and abs(sum(t2.portion for t2 in specs) - 1.0) < 1e-9:
                qty = qty_left
            qty = min(qty, qty_left)
            if qty <= 0:
                # Too small to split at this lot step: merge into the next target / final exit.
                continue
            qty_left = round(qty_left - qty, 10)
            out.append({"idx": k + 1, "price": price, "qty": qty, "done": False, "rr": sign * (price - fill) / risk})
        return out

    # ---- position handling ---------------------------------------------------------------------
    def _sub_bars(self, i):
        if self.fine_secs and self.fine_t:
            lo = bisect_left(self.fine_t, self.cs.t[i])
            hi = bisect_left(self.fine_t, self.cs.t[i] + self.tf)
            if hi > lo:
                return [(self.fine.o[k], self.fine.h[k], self.fine.l[k], self.fine.c[k]) for k in range(lo, hi)], True
        return [(self.cs.o[i], self.cs.h[i], self.cs.l[i], self.cs.c[i])], False

    def _leg(self, pos, i, price, mid, qty, reason, kind="EXIT"):
        sign = pos.sign
        conv = self.conv(price)
        gross = (price - pos.entry) * sign * qty * self.inst.contract_size * conv
        commission = self.commission(qty, price)
        self.balance += gross - commission
        pos.legs.append({"idx": i, "ts": self.cs.t[i], "price": price, "mid": mid, "qty": qty, "reason": reason, "gross": gross, "commission": commission, "conv": conv})
        pos.qty_open = round(pos.qty_open - qty, 10)
        pos.events.append({"type": "PARTIAL_EXIT" if pos.qty_open > 0 else "EXIT", "idx": i, "time": iso_utc(self.cs.t[i]), "price": price, "qty": qty, "detail": reason})

    def _process_positions(self, i):
        for pos in list(self.positions):
            if pos.entry_idx != i:
                if pos.pending_stop is not None:
                    new_stop, why = pos.pending_stop
                    if pos.sign * (new_stop - pos.stop) > 0:
                        self._log("SL_MOVED", i, positionId=pos.id, old=pos.stop, new=new_stop, reason=why)
                        pos.events.append({"type": "SL_MOVED", "idx": i, "time": iso_utc(self.cs.t[i]), "price": new_stop, "detail": f"{why}: stop {pos.stop:.6g} -> {new_stop:.6g}"})
                        pos.stop = new_stop
                        if why == "BREAKEVEN":
                            pos.be_done = True
                    pos.pending_stop = None
                if pos.exit_next:
                    s = self.spread(i)
                    open_exit = self.cs.o[i] - pos.sign * s / 2
                    gap_stop = pos.sign * (open_exit - pos.stop) <= 0
                    price = open_exit - pos.sign * self.cfg.costs.slippage
                    self._leg(pos, i, price, self.cs.o[i], pos.qty_open, "STOP_GAP" if gap_stop else pos.exit_next)
                    self._close_position(pos, i)
                    continue
            self._scan_position(pos, i)
            if pos in self.positions and pos.qty_open > 0:
                pos.bars_held += 1

    def _scan_position(self, pos, i):
        sign, s, slip = pos.sign, self.spread(i), self.cfg.costs.slippage
        adj = -sign * s / 2
        subs, used_fine = self._sub_bars(i)
        gap_check = pos.entry_idx != i
        for k, (o, h, l, c) in enumerate(subs):
            O, H, L = o + adj, h + adj, l + adj
            if k == 0 and gap_check:
                if sign * (O - pos.stop) <= 0:
                    self._leg(pos, i, O - sign * slip, o, pos.qty_open, "STOP_GAP")
                    self._close_position(pos, i)
                    return
                for tp in [t for t in pos.targets if not t["done"]]:
                    if sign * (O - tp["price"]) >= 0:
                        tp["done"] = True
                        self._leg(pos, i, O, o, tp["qty"], f"TARGET_{tp['idx']}_GAP")
                        self._after_target(pos, tp)
                if pos.qty_open <= 1e-12:
                    self._close_position(pos, i)
                    return
            stop_hit = (L <= pos.stop) if sign == 1 else (H >= pos.stop)
            credit_targets = not (pos.entry_idx == i and pos.kind == "intrabar_limit")
            open_targets = sorted((t for t in pos.targets if not t["done"]), key=lambda t: sign * t["price"])
            hits = [t for t in open_targets if ((H >= t["price"]) if sign == 1 else (L <= t["price"]))] if credit_targets else []
            if stop_hit and hits:
                pos.ambiguous = True
                pos.amb_extra = sum((t["price"] - pos.stop) * sign * t["qty"] * self.inst.contract_size * self.conv(t["price"]) for t in hits)
                pos.events.append({"type": "AMBIGUOUS_BAR", "idx": i, "time": iso_utc(self.cs.t[i]), "detail": "Stop and target both inside one bar; conservative stop-first convention applied" + (" (even at lower-timeframe resolution)" if used_fine else "")})
                self._leg(pos, i, pos.stop - sign * slip, pos.stop - adj, pos.qty_open, "STOP_AMBIGUOUS_BAR")
                self._close_position(pos, i)
                return
            if stop_hit:
                self._leg(pos, i, pos.stop - sign * slip, pos.stop - adj, pos.qty_open, "STOP")
                self._close_position(pos, i)
                return
            for tp in hits:
                tp["done"] = True
                self._leg(pos, i, tp["price"], tp["price"] - adj, tp["qty"], f"TARGET_{tp['idx']}")
                self._after_target(pos, tp)
            if pos.qty_open <= 1e-12:
                self._close_position(pos, i)
                return
            extreme = H if sign == 1 else L
            worst = L if sign == 1 else H
            pos.best = max(pos.best, extreme) if sign == 1 else min(pos.best, extreme)
            pos.worst = min(pos.worst, worst) if sign == 1 else max(pos.worst, worst)
            be = self.spec.management.breakeven
            if be and be.trigger_r and not pos.armed_be and sign * (pos.best - pos.entry) >= be.trigger_r * pos.risk_per_unit:
                pos.armed_be = True

    def _after_target(self, pos, tp):
        be = self.spec.management.breakeven
        if be and be.after_target and tp["idx"] >= be.after_target:
            pos.armed_be = True

    def _close_position(self, pos, i):
        if pos in self.positions:
            self.positions.remove(pos)
        pos.exit_idx = i
        trade = self._trade_record(pos)
        self.trades.append(trade)
        self.last_close_idx = i
        day = self._day(self._day_key(self.cs.t[i] + self.tf))
        day["pnl"] += trade["netPnl"]
        if trade["rMultiple"] is not None:
            day["r"] += trade["rMultiple"]
        day["consec"] = day["consec"] + 1 if trade["netPnl"] < 0 else 0

    # ---- close-of-bar decisions ----------------------------------------------------------------
    def _unrealized(self, i):
        total = 0.0
        for p in self.positions:
            price = self.cs.c[i] - p.sign * self.spread(i) / 2
            total += (price - p.entry) * p.sign * p.qty_open * self.inst.contract_size * self.conv(price)
        return total

    def _close_of_bar(self, i):
        t_close = self.cs.t[i] + self.tf
        equity = self.balance + self._unrealized(i)
        self.equity.append(equity)
        if equity <= 0 and not self.halted:
            self.halted = True
            self.warnings.append(f"Account equity reached zero at {iso_utc(self.cs.t[i])}; trading halted.")
        mgmt = self.spec.management
        exit_hits = [cond.eval(i) for cond, _ in self.exit_rules]  # evaluated every bar to keep sequences in step
        tick = self.inst.tick_size
        for pos in self.positions:
            if pos.qty_open <= 0:
                continue
            sign = pos.sign
            market = self.cs.c[i] - sign * self.spread(i) / 2
            if pos.armed_be and not pos.be_done and mgmt.breakeven:
                level = pos.entry + sign * mgmt.breakeven.offset_points
                if sign * (market - level) > tick:
                    pos.pending_stop = self._tighter(pos, (level, "BREAKEVEN"))
            tr = mgmt.trailing
            if tr and sign * (pos.best - pos.entry) >= tr.start_r * pos.risk_per_unit:
                cand = self._trail_level(pos, i, tr)
                if cand is not None and sign * (market - cand) > tick:
                    pos.pending_stop = self._tighter(pos, (cand, "TRAILING"))
            for hit, (_, directions) in zip(exit_hits, self.exit_rules):
                if hit and directions in ("BOTH", pos.direction) and not pos.exit_next:
                    pos.exit_next = "EXIT_RULE"
            if mgmt.max_bars and pos.bars_held >= mgmt.max_bars and not pos.exit_next:
                pos.exit_next = "TIME_STOP"
            if self.flat and not pos.exit_next and self._flat_due(pos, t_close):
                pos.exit_next = "SESSION_END"
        invalid = self.invalidation.eval(i) if self.invalidation else False
        if invalid and self.orders:
            for od in self.orders:
                od["status"] = "INVALIDATED"
                self.counters["invalidated"] += 1
                self._log("ORDER_INVALIDATED", i, orderId=od["id"])
            self.orders = []
        self._signals(i, t_close)

    def _tighter(self, pos, proposal):
        current = pos.pending_stop
        level = proposal[0]
        base = current[0] if current else pos.stop
        if pos.sign * (level - base) > 0:
            return proposal
        return current

    def _trail_level(self, pos, i, tr):
        sign, bar = pos.sign, self.bars
        if tr.type == "points":
            return pos.best - sign * tr.value
        if tr.type == "atr":
            a = self.trail_atr[i]
            return None if a is None else pos.best - sign * tr.mult * a
        if tr.type == "candle":
            return (bar.l[i] - tr.buffer) if sign == 1 else (bar.h[i] + tr.buffer)
        level = self.trail_swings["low" if sign == 1 else "high"][i]
        return None if level is None else (level - tr.buffer if sign == 1 else level + tr.buffer)

    def _flat_due(self, pos, t_close):
        minute_flat, tz = self.flat
        when, entered = local(t_close, tz), local(pos.entry_ts, tz)
        minute = when.hour * 60 + when.minute
        if when.date() > entered.date():
            return True
        return minute >= minute_flat and entered.hour * 60 + entered.minute < minute_flat

    # ---- signals -------------------------------------------------------------------------------
    def _signals(self, i, t_close):
        spec, risk = self.spec, self.spec.risk
        long_sig = self.long_cond.eval(i) if self.long_cond else False
        short_sig = self.short_cond.eval(i) if self.short_cond else False
        bull = self.bias_bull.eval(i) if self.bias_bull else False
        bear = self.bias_bear.eval(i) if self.bias_bear else False
        if not (long_sig or short_sig) or self.halted:
            return
        if self.start_ts and t_close <= self.start_ts:
            return
        if long_sig and short_sig:
            self._reject("BOTH_DIRECTIONS_SIGNALLED", i)
            return
        self.counters["signals"] += 1
        sign = 1 if long_sig else -1
        node = self.long_cond if long_sig else self.short_cond
        explain = node.describe(i)
        ok, why = self._allowed(t_close)
        if not ok:
            self._reject(why, i)
            return
        if len(self.positions) + len(self.orders) >= risk.max_concurrent:
            self._reject("MAX_CONCURRENT", i)
            return
        if i - self.last_close_idx <= spec.entry.cooldown_bars:
            self._reject("COOLDOWN", i)
            return
        day = self._day(self._day_key(t_close))
        if risk.max_trades_per_day and day["trades"] >= risk.max_trades_per_day:
            self._reject("MAX_TRADES_PER_DAY", i)
            return
        if risk.max_daily_loss_r and day["r"] <= -risk.max_daily_loss_r:
            self._reject("DAILY_LOSS_LIMIT_R", i)
            return
        if risk.max_daily_loss_pct and day["pnl"] <= -risk.max_daily_loss_pct / 100.0 * day["start_balance"]:
            self._reject("DAILY_LOSS_LIMIT_PCT", i)
            return
        if risk.max_consecutive_losses and day["consec"] >= risk.max_consecutive_losses:
            self._reject("CONSECUTIVE_LOSS_LIMIT", i)
            return
        s = self.spread(i)
        if risk.max_spread and s > risk.max_spread:
            self._reject("SPREAD_TOO_WIDE", i, spread=s)
            return
        order_type = spec.entry.order.type
        close = self.cs.c[i]
        if order_type == "market":
            ref = close + sign * s / 2
        else:
            base = self.order_price(i)
            if base is None:
                self._reject("ORDER_PRICE_UNAVAILABLE", i)
                return
            ref = base + spec.entry.order.offset_points
        stop = self._stop_level(sign, ref, i)
        if stop is None:
            self._reject("STOP_UNAVAILABLE", i)
            return
        dist = sign * (ref - stop)
        if dist <= self.inst.tick_size:
            self._reject("STOP_TOO_CLOSE_OR_WRONG_SIDE", i, ref=ref, stop=stop)
            return
        atr_vals, level_vals = {}, {}
        for k, t in enumerate(spec.targets):
            if t.type == "atr":
                v = self.ctx.series[t.series][i]
                if v is None:
                    self._reject("TARGET_ATR_UNAVAILABLE", i)
                    return
                atr_vals[k] = v
            if t.type == "level":
                level_vals[k] = self.target_refs[k](i)
        if risk.min_rr:
            best = self._planned_rr(sign, ref, stop, atr_vals, level_vals)
            if best is None or best < risk.min_rr:
                self._reject("BELOW_MIN_RR", i, plannedRR=best)
                return
        qty, risk_amount = self._size(sign, ref, dist)
        if qty <= 0:
            self._reject("QTY_BELOW_MINIMUM", i)
            return
        equity = self.balance + self._unrealized(i)
        margin_needed = qty * self.inst.contract_size * ref * self.conv(ref) * self.margin_rate
        free = equity - self._margin_used()
        if margin_needed > free:
            affordable = round_down_qty(max(0.0, free) / (self.inst.contract_size * ref * self.conv(ref) * self.margin_rate), self.inst)
            if affordable < self.inst.min_qty:
                self._reject("INSUFFICIENT_MARGIN", i, needed=margin_needed, free=free)
                return
            qty, risk_amount = affordable, affordable * dist * self.inst.contract_size * self.conv(ref)
        regime = self._regime(i)
        bias = "BULLISH" if bull and not bear else "BEARISH" if bear and not bull else "NEUTRAL" if (self.bias_bull or self.bias_bear) else "NOT_DEFINED"
        od = {"id": self.next_order_id, "sign": sign, "direction": "LONG" if sign == 1 else "SHORT", "type": order_type, "level": ref if order_type != "market" else None,
              "stop": stop, "qty": qty, "riskAmount": risk_amount, "plannedDist": dist, "createdIdx": i, "signalTime": iso_utc(self.cs.t[i]),
              "validUntil": i + 1 if order_type == "market" else i + spec.entry.order.expire_bars, "atr_at_signal": atr_vals, "level_targets": level_vals,
              "explain": explain, "bias": bias, "regime": regime, "status": "PENDING", "refPrice": ref, "signalClose": close,
              "setup": spec.setup_name or spec.name, "spreadAtSignal": s, "atrAtSignal": self.ctx.series["__atr14"][i]}
        self.next_order_id += 1
        day["trades"] += 1
        self.orders.append(od)
        self.orders_log.append(od)
        self.counters["ordersPlaced"] += 1
        self._log("SIGNAL", i, orderId=od["id"], direction=od["direction"], conditions=explain)
        self._log("ORDER_PLACED", i, orderId=od["id"], orderType=order_type, level=od["level"], stop=stop, qty=qty)

    def _stop_level(self, sign, ref, i):
        st = self.spec.stop
        if st.type == "atr":
            a = self.stop_atr[i]
            return None if a is None else ref - sign * st.mult * a
        if st.type == "points":
            return ref - sign * st.value
        if st.type == "percent":
            return ref * (1 - sign * st.value / 100.0)
        if st.type == "signal_candle":
            return (self.cs.l[i] - st.buffer) if sign == 1 else (self.cs.h[i] + st.buffer)
        level = self.stop_ref(i)
        return None if level is None else level - sign * st.buffer

    def _planned_rr(self, sign, ref, stop, atr_vals, level_vals):
        risk = sign * (ref - stop)
        best = None
        for k, t in enumerate(self.spec.targets):
            if t.type == "rr":
                price = ref + sign * t.value * risk
            elif t.type == "points":
                price = ref + sign * t.value
            elif t.type == "atr":
                price = ref + sign * t.mult * atr_vals[k]
            else:
                price = level_vals.get(k)
            if price is None:
                continue
            rr = sign * (price - ref) / risk
            best = rr if best is None else max(best, rr)
        return best

    def _size(self, sign, ref, dist):
        r, inst = self.spec.risk, self.inst
        conv = self.conv(ref)
        if r.model == "fixed_qty":
            qty = min(round_down_qty(r.value, inst), inst.max_qty)
            return (qty if qty >= inst.min_qty else 0.0), qty * dist * inst.contract_size * conv
        amount = {"percent_equity": self.balance * r.value / 100.0, "percent_initial": self.cfg.initial_capital * r.value / 100.0, "fixed_cash": r.value}[r.model]
        qty = size_position(amount, dist, inst, conv)
        return qty, qty * dist * inst.contract_size * conv

    def _regime(self, i):
        er, atr = self.ctx.series["__er20"][i], self.ctx.series["__atr14"]
        window = [a for a in atr[max(0, i - 99):i + 1] if a is not None]
        pct = None if atr[i] is None or len(window) < 20 else sum(1 for a in window if a <= atr[i]) / len(window)
        trend = None if er is None else ("trending" if er >= 0.35 else "ranging" if er <= 0.2 else "transitional")
        vol = None if pct is None else ("high-volatility" if pct >= 0.67 else "low-volatility" if pct <= 0.33 else "normal-volatility")
        return {"trend": trend or "unknown", "volatility": vol or "unknown", "efficiencyRatio": er, "atrPercentile": pct}

    # ---- finishing -----------------------------------------------------------------------------
    def _finish(self):
        last = self.n - 1
        if self.guard:
            self._cursor[0] = last
        for od in self.orders:
            od["status"] = "UNFILLED_END_OF_DATA"
        self.orders = []
        for pos in list(self.positions):
            s = self.spread(last)
            price = self.cs.c[last] - pos.sign * s / 2
            pos.open_at_end = True
            self._leg(pos, last, price, self.cs.c[last], pos.qty_open, "END_OF_DATA")
            self._close_position(pos, last)
            self.trades[-1]["openAtEnd"] = True
        if self.equity:
            self.equity[-1] = self.balance

    def _trade_record(self, pos):
        sign, inst = pos.sign, self.inst
        legs = pos.legs
        gross = sum(g["gross"] for g in legs)
        fees = pos.commission + sum(g["commission"] for g in legs)
        net = gross - fees
        ideal = sum((g["mid"] - pos.entry_mid) * sign * g["qty"] * inst.contract_size * g["conv"] for g in legs)
        spread_cost = sum((pos.spread_entry / 2 + self.spread(g["idx"]) / 2) * g["qty"] * inst.contract_size * g["conv"] for g in legs)
        slippage_cost = max(0.0, ideal - gross - spread_cost)
        conv_entry = self.conv(pos.entry)
        last = legs[-1]
        risk_amount = pos.planned_risk
        r_multiple = net / risk_amount if risk_amount > 0 else None
        actual_risk = abs(pos.entry - pos.stop0) * pos.qty * inst.contract_size * conv_entry
        qty = pos.qty
        avg_exit = sum(g["price"] * g["qty"] for g in legs) / qty if qty else last["price"]
        order = pos.order
        weights = [(t["rr"], t["qty"] / qty) for t in pos.targets] if qty else []
        planned_rr = sum(r * w for r, w in weights) if weights else None
        far = max(pos.targets, key=lambda t: sign * t["price"]) if pos.targets else None
        mfe = sign * (pos.best - pos.entry) / pos.risk_per_unit if pos.risk_per_unit else None
        mae = sign * (pos.entry - pos.worst) / pos.risk_per_unit if pos.risk_per_unit else None
        sl_changes = [e for e in pos.events if e["type"] == "SL_MOVED"]
        entry_ts = pos.entry_ts
        result = "WIN" if net > 1e-9 else "LOSS" if net < -1e-9 else "BREAKEVEN"
        reasons = [g["reason"] for g in legs]
        events = [{"type": "SIGNAL", "idx": order["createdIdx"], "time": order["signalTime"], "detail": "; ".join(order["explain"]) or "entry condition met"},
                  {"type": "ORDER_PLACED", "idx": order["createdIdx"], "time": order["signalTime"], "price": order["level"], "detail": f"{order['type']} order, stop {order['stop']:.6g}, qty {order['qty']}"}] + pos.events
        return {
            "seq": len(self.trades) + 1, "direction": pos.direction, "symbol": self.symbol, "setup": order["setup"],
            "signalIdx": order["createdIdx"], "signalTime": order["signalTime"], "entryIdx": pos.entry_idx, "entryTs": entry_ts,
            "entryTime": iso_utc(entry_ts), "exitIdx": last["idx"], "exitTs": last["ts"], "exitTime": iso_utc(last["ts"]),
            "orderType": order["type"], "plannedEntry": order["refPrice"], "entry": pos.entry, "entryKind": pos.kind,
            "stop0": pos.stop0, "stopDistance": abs(pos.entry - pos.stop0), "qty": qty, "riskAmount": risk_amount, "actualRiskAmount": actual_risk,
            "targets": [{"idx": t["idx"], "price": t["price"], "qty": t["qty"], "rr": t["rr"], "filled": t["done"]} for t in pos.targets],
            "tpDistance": abs(far["price"] - pos.entry) if far else None, "plannedRR": planned_rr,
            "slChanges": [{"time": e["time"], "detail": e["detail"], "price": e["price"]} for e in sl_changes],
            "breakeven": any("BREAKEVEN" in e["detail"] for e in sl_changes), "trailing": any("TRAILING" in e["detail"] for e in sl_changes),
            "partialExits": [{"time": iso_utc(g["ts"]), "price": g["price"], "qty": g["qty"], "reason": g["reason"]} for g in legs[:-1]],
            "legs": [{"time": iso_utc(g["ts"]), "price": g["price"], "qty": g["qty"], "reason": g["reason"], "gross": g["gross"], "commission": g["commission"]} for g in legs],
            "exit": avg_exit, "finalExit": last["price"], "exitReason": last["reason"], "reasons": reasons,
            "grossPnl": gross, "fees": fees, "netPnl": net, "idealPnl": ideal, "spreadCost": spread_cost, "slippageCost": slippage_cost,
            "rMultiple": r_multiple, "result": result, "holdBars": last["idx"] - pos.entry_idx + 1,
            "holdSeconds": (last["idx"] - pos.entry_idx + 1) * self.tf, "ambiguous": pos.ambiguous, "openAtEnd": False,
            "bias": order["bias"], "regime": order["regime"], "session": session_label(entry_ts), "mfeR": mfe, "maeR": mae,
            "conditions": order["explain"], "events": events, "spreadAtSignal": order["spreadAtSignal"], "atrAtSignal": order["atrAtSignal"],
            "balanceAfter": self.balance,
            **({"ambiguityBestCaseNet": net + getattr(pos, "amb_extra", 0.0)} if pos.ambiguous else {}),
        }
