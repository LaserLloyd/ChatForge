"""Safe arithmetic evaluator: an ``ast`` whitelist, never ``eval``.

Limits are enforced per node *during* evaluation, so ``9**9**9`` is refused before the
inner power runs.
"""

from __future__ import annotations

import ast
import math
import operator
from collections.abc import Callable
from typing import Any

from aichat.tools.registry import ToolResult

MAX_EXPR_CHARS = 200
MAX_EXPONENT = 100
MAX_MAGNITUDE = 1e100
MAX_ROUND_DIGITS = 100

Number = int | float


class CalcError(ValueError):
    """The expression is invalid, unsupported or exceeds a limit."""


_CONSTANTS: dict[str, float] = {"pi": math.pi, "e": math.e}
_FUNCS: dict[str, Callable[..., Number]] = {
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
    "abs": abs,
    "round": round,
    "floor": math.floor,
    "ceil": math.ceil,
}
_MAX_ARGS = {"log": 2, "round": 2}  # every other function takes exactly one argument
_BINOPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}
_UNARY: dict[type, Callable[[Any], Any]] = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _check(value: Any) -> Number:
    """Reject non-numbers, NaN/inf and anything larger than 1e100 in magnitude."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise CalcError("result is not a real number")
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        raise CalcError("result is not a finite number")
    if abs(value) > MAX_MAGNITUDE:
        raise CalcError("number too large (limit 1e100)")
    return value


def _pow(base: Number, exp: Number) -> Number:
    if abs(exp) > MAX_EXPONENT:
        raise CalcError(f"exponent too large (limit {MAX_EXPONENT})")
    if base == 0:
        if exp < 0:
            raise CalcError("division by zero")
        return base**exp
    # Estimate the magnitude first so an over-limit power is never materialised.
    if exp > 0 and abs(base) > 1 and exp * math.log10(abs(base)) > 101:
        raise CalcError("number too large (limit 1e100)")
    result = base**exp
    if isinstance(result, complex):
        raise CalcError("result is not a real number")
    return result


def _call(name: str, args: list[Number]) -> Number:
    limit = _MAX_ARGS.get(name, 1)
    if not 1 <= len(args) <= limit:
        raise CalcError(f"{name}() takes {'1 or 2' if limit == 2 else '1'} argument(s)")
    if name == "round" and len(args) == 2:
        digits = args[1]
        if not isinstance(digits, int) or abs(digits) > MAX_ROUND_DIGITS:
            raise CalcError("round() digits must be a small integer")
    return _FUNCS[name](*args)


def _eval(node: ast.AST) -> Number:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, int | float):
            raise CalcError("only numbers are allowed")
        return _check(node.value)
    if isinstance(node, ast.Name):
        if node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        raise CalcError(f"unknown name: {node.id}")
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _check(_UNARY[type(node.op)](_eval(node.operand)))
    if isinstance(node, ast.BinOp):
        op = type(node.op)
        left = _eval(node.left)
        right = _eval(node.right)
        if op is ast.Pow:
            return _check(_pow(left, right))
        if op in _BINOPS:
            return _check(_BINOPS[op](left, right))
        raise CalcError("operator not allowed")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS or node.keywords:
            raise CalcError("function not allowed")
        args = [_eval(a) for a in node.args]
        return _check(_call(node.func.id, args))
    raise CalcError("unsupported expression")


def evaluate(expression: str) -> Number:
    """Evaluate ``expression``; raise :class:`CalcError` for anything unsafe or invalid."""
    if not isinstance(expression, str) or not expression.strip():
        raise CalcError("empty expression")
    if len(expression) > MAX_EXPR_CHARS:
        raise CalcError(f"expression too long (limit {MAX_EXPR_CHARS} characters)")
    try:
        tree = ast.parse(expression.strip(), mode="eval")
        return _eval(tree)
    except CalcError:
        raise
    except ZeroDivisionError:
        raise CalcError("division by zero") from None
    except ArithmeticError:
        raise CalcError("result out of range") from None
    except (SyntaxError, ValueError, TypeError, RecursionError, MemoryError):
        raise CalcError("invalid expression or math domain error") from None


def format_number(value: Number) -> str:
    if isinstance(value, int):
        return str(value)
    return format(value, ".15g")


async def run(expression: str) -> ToolResult:
    try:
        value = evaluate(expression)
    except CalcError as exc:
        return ToolResult(False, f"Calculator error: {exc}", "calculator error")
    text = format_number(value)
    return ToolResult(True, text, f"{expression.strip()} = {text}")
