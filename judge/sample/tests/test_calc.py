import pytest

from calc import add, mean


def test_add():
    assert add(2, 3) == 5


def test_mean():
    assert mean([2, 4, 6]) == 4


def test_mean_empty():
    with pytest.raises(ValueError):
        mean([])
