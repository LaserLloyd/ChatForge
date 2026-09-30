"""Calculator: valid math works, everything unsafe or over-limit is rejected."""

import math

import pytest

from aichat.tools import calculator
from aichat.tools.calculator import CalcError, evaluate


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("1+2*3", 7),
        ("(1+2)*3", 9),
        ("2**10", 1024),
        ("7//2", 3),
        ("7%4", 3),
        ("-3+abs(-4)", 1),
        ("+5", 5),
        ("10/4", 2.5),
        ("sqrt(2)*10", math.sqrt(2) * 10),
        ("sin(0)+cos(0)", 1.0),
        ("log10(1000)", 3.0),
        ("log(e)", 1.0),
        ("log(8, 2)", 3.0),
        ("exp(0)", 1.0),
        ("round(2.567, 1)", 2.6),
        ("round(2.5)", 2),
        ("floor(2.9)+ceil(2.1)", 5),
        ("pi", math.pi),
        ("2*pi*3", 2 * math.pi * 3),
        ("10**100", 10**100),
        ("2**-2", 0.25),
        ("  1 + 1  ", 2),
    ],
)
def test_valid_expressions(expr, expected):
    assert evaluate(expr) == pytest.approx(expected)


@pytest.mark.parametrize(
    "expr",
    [
        "__import__('os')",
        "__import__('os').system('echo hi')",
        "9**9**9",
        "().__class__",
        "(1).__class__",
        "(1).__class__.__bases__",
        "10**101",
        "1e100*10",
        "1/0",
        "1//0",
        "1%0",
        "0**-1",
        "2**101",
        "2**-101",
        "1e308*1e308",
        "(-8)**0.5",
        "sqrt(-1)",
        "log(0)",
        "exp(1000)",
        "tan(",
        "",
        "   ",
        "1 +",
        "a+1",
        "x",
        "'a'*3",
        "'abc'",
        "True",
        "None",
        "1j",
        "[1,2]",
        "(1,2)",
        "{1:2}",
        "lambda: 1",
        "[x for x in range(3)]",
        "abs.__call__(1)",
        "print(1)",
        "eval('1')",
        "open('x')",
        "sqrt(x=4)",
        "sqrt()",
        "sqrt(1, 2)",
        "round(1, 2, 3)",
        "round(1.5, 1000)",
        "round(1.5, 1.5)",
        "1 if 1 else 2",
        "1 < 2",
        "not 1",
        "~1",
        "1 << 3",
        "1 & 1",
        "(1).real",
        "pi.real",
        "math.pi",
        "sqrt(2)(3)",
        "abs",
        "1;2",
        "x = 1",
        "0x" + "f" * 90,  # 16**90 > 1e100
        "9" * 101,
        "1" * 201,  # over the 200 character limit
        "1+" * 150 + "1",  # 301 characters
        "\x00",
    ],
)
def test_rejected(expr):
    with pytest.raises(CalcError):
        evaluate(expr)


def test_deep_nesting_within_limit_is_fine():
    assert evaluate("(" * 90 + "1" + ")" * 90) == 1


def test_non_string_rejected():
    with pytest.raises(CalcError):
        evaluate(None)  # type: ignore[arg-type]


def test_length_limit_boundary():
    assert evaluate("1" + "+0" * 99) == 1  # exactly 199 chars
    with pytest.raises(CalcError, match="too long"):
        evaluate("1" + "+0" * 100 + "0")


def test_limits_checked_during_evaluation(monkeypatch):
    """9**9**9 must fail on the outer exponent without ever computing 9**9 ** 387420489."""
    calls = []
    real_pow = calculator._pow

    def spy(base, exp):
        calls.append((base, exp))
        return real_pow(base, exp)

    monkeypatch.setattr(calculator, "_pow", spy)
    with pytest.raises(CalcError, match="exponent"):
        evaluate("9**9**9")
    assert calls[-1] == (9, 9**9)  # rejected at the outer node, before any huge int exists


def test_intermediate_magnitude_rejected():
    # The final result is small, but an intermediate node exceeds 1e100.
    with pytest.raises(CalcError):
        evaluate("(10**60 * 10**60) / 10**60")


def test_bool_arithmetic_not_reachable():
    with pytest.raises(CalcError):
        evaluate("True + 1")


async def test_run_success_and_error():
    ok = await calculator.run("sqrt(2)*10")
    assert ok.ok
    assert ok.content == "14.142135623731"
    assert "sqrt(2)*10" in ok.summary
    bad = await calculator.run("__import__('os')")
    assert not bad.ok
    assert "Calculator error" in bad.content
    zero = await calculator.run("1/0")
    assert not zero.ok
    assert "division by zero" in zero.content


def test_format_number():
    assert calculator.format_number(4.0) == "4"
    assert calculator.format_number(7) == "7"
    assert calculator.format_number(0.1 + 0.2) == "0.3"
