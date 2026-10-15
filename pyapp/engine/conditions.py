"""Compile DSL condition trees into causal evaluators.

A node's `eval(i)` may read series values at indices <= i only (the series themselves are causal).
Sequence nodes are stateful and must be evaluated for EVERY bar in order; composite nodes therefore
never short-circuit.
"""
from datetime import datetime, timezone

from pyapp.engine.series import Bars, PRICE_FIELDS
from pyapp.engine.spec import SpecError

COMPARE = {">", ">=", "<", "<=", "==", "!=", "crosses_above", "crosses_below"}
ARITH = {"add", "sub", "mul", "div", "min", "max", "abs", "neg"}


class Ctx:
    def __init__(self, bars: Bars, series: dict):
        self.bars, self.series = bars, series

    def has(self, name):
        return name in self.series or (name in PRICE_FIELDS and (name != "spread" or self.bars.spread is not None))

    def values(self, name):
        if name in self.series:
            return self.series[name]
        if name in PRICE_FIELDS:
            return self.bars.field(name)
        raise SpecError(f"Unknown series or price field '{name}'.")

    def stamp(self, index):
        return datetime.fromtimestamp(self.bars.t[index], timezone.utc).strftime("%Y-%m-%d %H:%M")


def _fmt(value):
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "true" if value else "false"
    return f"{value:.6g}"


# ---- operands -------------------------------------------------------------------------------------

class Operand:
    def __init__(self, fn, text):
        self.fn, self.text = fn, text

    def __call__(self, i):
        return self.fn(i)


def compile_operand(node, ctx: Ctx, path="operand") -> Operand:
    if isinstance(node, bool):
        return Operand(lambda i, v=float(node): v, str(node).lower())
    if isinstance(node, (int, float)):
        return Operand(lambda i, v=float(node): v, _fmt(float(node)))
    if isinstance(node, str):
        return _series_operand(node, 0, ctx, path)
    if isinstance(node, dict):
        if "const" in node:
            return compile_operand(node["const"], ctx, path)
        if "s" in node:
            shift = node.get("shift", 0)
            if not isinstance(shift, int) or shift < 0 or shift > 5000:
                raise SpecError(f"{path}: shift must be a non-negative integer (looking forward in time is not allowed).")
            return _series_operand(node["s"], shift, ctx, path)
        keys = [k for k in node if k in ARITH]
        if len(keys) == 1:
            op = keys[0]
            args = node[op] if isinstance(node[op], list) else [node[op]]
            parts = [compile_operand(a, ctx, f"{path}.{op}") for a in args]
            if op in ("abs", "neg") and len(parts) != 1:
                raise SpecError(f"{path}: {op} takes exactly one operand.")
            if op in ("sub", "div") and len(parts) != 2:
                raise SpecError(f"{path}: {op} takes exactly two operands.")
            def compute(i, parts=parts, op=op):
                values = [p(i) for p in parts]
                if any(v is None for v in values):
                    return None
                if op == "add": return sum(values)
                if op == "sub": return values[0] - values[1]
                if op == "mul":
                    out = 1.0
                    for v in values: out *= v
                    return out
                if op == "div": return None if values[1] == 0 else values[0] / values[1]
                if op == "min": return min(values)
                if op == "max": return max(values)
                if op == "abs": return abs(values[0])
                return -values[0]
            return Operand(compute, f"{op}(" + ", ".join(p.text for p in parts) + ")")
    raise SpecError(f"{path}: invalid operand {node!r}.")


def _series_operand(name, shift, ctx, path):
    if not ctx.has(name):
        raise SpecError(f"{path}: unknown series or price field '{name}'. Define it under 'series'.")
    values = ctx.values(name)
    def fetch(i, values=values, shift=shift):
        j = i - shift
        if j < 0:
            return None
        v = values[j]
        return float(v) if isinstance(v, bool) else v
    return Operand(fetch, name if shift == 0 else f"{name}[{shift}]")


# ---- conditions -----------------------------------------------------------------------------------

class Node:
    def eval(self, i): raise NotImplementedError
    def describe(self, i): return []


class Leaf(Node):
    def __init__(self, left, op, right):
        self.left, self.op, self.right = left, op, right

    def eval(self, i):
        a, b = self.left(i), self.right(i)
        if a is None or b is None:
            return False
        op = self.op
        if op == ">": return a > b
        if op == ">=": return a >= b
        if op == "<": return a < b
        if op == "<=": return a <= b
        if op == "==": return abs(a - b) <= 1e-12 * max(1.0, abs(a), abs(b))
        if op == "!=": return abs(a - b) > 1e-12 * max(1.0, abs(a), abs(b))
        pa, pb = self.left(i - 1) if i > 0 else None, self.right(i - 1) if i > 0 else None
        if pa is None or pb is None:
            return False
        return (a > b and pa <= pb) if op == "crosses_above" else (a < b and pa >= pb)

    def describe(self, i):
        return [f"{self.left.text} ({_fmt(self.left(i))}) {self.op.replace('_', ' ')} {self.right.text} ({_fmt(self.right(i))})"] if self.eval(i) else []


class Truthy(Node):
    def __init__(self, operand, within):
        self.operand, self.within = operand, within

    def eval(self, i):
        for k in range(self.within):
            if i - k < 0:
                break
            v = self.operand(i - k)
            if v is not None and v != 0:
                return True
        return False

    def describe(self, i):
        if not self.eval(i):
            return []
        suffix = "" if self.within == 1 else f" within last {self.within} bars"
        return [f"{self.operand.text} is true{suffix}"]


class All(Node):
    def __init__(self, children): self.children = children
    def eval(self, i):
        results = [c.eval(i) for c in self.children]
        return all(results)
    def describe(self, i):
        return [line for c in self.children for line in c.describe(i)]


class Any_(Node):
    def __init__(self, children): self.children = children
    def eval(self, i):
        results = [c.eval(i) for c in self.children]
        return any(results)
    def describe(self, i):
        return [line for c in self.children if c.eval(i) for line in c.describe(i)]


class Not(Node):
    def __init__(self, child): self.child = child
    def eval(self, i): return not self.child.eval(i)
    def describe(self, i): return ["NOT (" + ", ".join(self.child.describe(i) or ["condition absent"]) + ")"] if self.eval(i) else []


class Seq(Node):
    """Ordered stages; each later stage must occur on a strictly later bar within its window."""
    def __init__(self, stages, windows, invalidate, restart, ctx):
        self.stages, self.windows, self.invalidate, self.restart, self.ctx = stages, windows, invalidate, restart, ctx
        self.stage, self.hits, self.last_i, self.fired = 0, [], -1, None

    def eval(self, i):
        if self.last_i != -1 and i != self.last_i + 1:
            raise RuntimeError("Sequence conditions must be evaluated for every bar in order.")
        self.last_i = i
        self.fired = None
        if self.stage > 0:
            if self.invalidate is not None and self.invalidate.eval(i):
                self.stage, self.hits = 0, []
            else:
                limit = self.windows[self.stage - 1]
                if limit is not None and i - self.hits[-1] > limit:
                    self.stage, self.hits = 0, []
        if self.stage > 0 and self.restart and self.stages[0].eval(i):
            self.stage, self.hits = 1, [i]
            if len(self.stages) == 1:
                self.fired, self.stage, self.hits = [i], 0, []
                return True
            return False
        if self.stage > 0:
            if self.stages[self.stage].eval(i):
                self.hits.append(i)
                self.stage += 1
                if self.stage == len(self.stages):
                    self.fired, self.stage, self.hits = list(self.hits), 0, []
                    return True
            return False
        if self.stages[0].eval(i):
            if len(self.stages) == 1:
                self.fired = [i]
                return True
            self.stage, self.hits = 1, [i]
        return False

    def describe(self, i):
        if not self.fired or self.last_i != i:
            return []
        return [f"sequence stage {n + 1} met at {self.ctx.stamp(idx)}: " + "; ".join(self.stages[n].describe(idx) or ["condition met"]) for n, idx in enumerate(self.fired)]


def _contains_seq(node):
    if isinstance(node, dict):
        return "seq" in node or any(_contains_seq(v) for v in node.values())
    if isinstance(node, list):
        return any(_contains_seq(v) for v in node)
    return False


def compile_cond(node, ctx: Ctx, path="cond") -> Node | None:
    if node is None:
        return None
    if not isinstance(node, dict):
        raise SpecError(f"{path}: a condition must be an object.")
    if "all" in node or "any" in node:
        key = "all" if "all" in node else "any"
        children = node[key]
        if not isinstance(children, list) or not children:
            raise SpecError(f"{path}.{key}: provide a non-empty list.")
        parts = [compile_cond(c, ctx, f"{path}.{key}[{n}]") for n, c in enumerate(children)]
        return All(parts) if key == "all" else Any_(parts)
    if "not" in node:
        return Not(compile_cond(node["not"], ctx, f"{path}.not"))
    if "is" in node:
        within = node.get("within", 1)
        if not isinstance(within, int) or not 1 <= within <= 5000:
            raise SpecError(f"{path}: 'within' must be an integer between 1 and 5000.")
        operand = compile_operand({"s": node["is"], "shift": node.get("shift", 0)} if isinstance(node["is"], str) else node["is"], ctx, path)
        return Truthy(operand, within)
    if "seq" in node:
        stages_raw = node["seq"]
        if not isinstance(stages_raw, list) or not stages_raw:
            raise SpecError(f"{path}.seq: provide at least one stage.")
        if any(_contains_seq(s) for s in stages_raw) or _contains_seq(node.get("invalidate")):
            raise SpecError(f"{path}: nested sequences are not supported.")
        stages = [compile_cond(s, ctx, f"{path}.seq[{n}]") for n, s in enumerate(stages_raw)]
        within = node.get("within")
        count = len(stages) - 1
        if within is None:
            windows = [None] * count
        elif isinstance(within, int):
            windows = [within] * count
        elif isinstance(within, list) and len(within) == count:
            windows = within
        else:
            raise SpecError(f"{path}.within: use an integer or one entry per later stage.")
        if any(w is not None and (not isinstance(w, int) or w < 1) for w in windows):
            raise SpecError(f"{path}.within: windows must be positive integers.")
        return Seq(stages, windows, compile_cond(node.get("invalidate"), ctx, f"{path}.invalidate"), node.get("restart", True), ctx)
    if "left" in node and "op" in node and "right" in node:
        if node["op"] not in COMPARE:
            raise SpecError(f"{path}: operator must be one of {sorted(COMPARE)}.")
        return Leaf(compile_operand(node["left"], ctx, f"{path}.left"), node["op"], compile_operand(node["right"], ctx, f"{path}.right"))
    raise SpecError(f"{path}: unrecognised condition {sorted(node)}. Use all/any/not/is/seq or left/op/right.")
