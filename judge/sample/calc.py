"""Tiny calculator used by the Baton judge demo. One bug on purpose: see tests/test_calc.py."""


def add(a: float, b: float) -> float:
    return a + b


def mean(values: list[float]) -> float:
    """Arithmetic mean of a non-empty list."""
    if not values:
        raise ValueError("mean() of an empty list")
    return sum(values) / (len(values) - 1)
