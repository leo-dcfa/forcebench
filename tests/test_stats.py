import math

from forcebench.stats import bootstrap_ci, pass_at_k, stratified_bootstrap_ci


def test_pass_at_k():
    assert pass_at_k(10, 0, 1) == 0.0
    assert pass_at_k(10, 10, 1) == 1.0
    assert math.isclose(pass_at_k(10, 3, 1), 0.3)
    assert math.isclose(pass_at_k(4, 1, 2), 0.5)


def test_bootstrap_bounds():
    xs = [1, 0, 1, 1, 0, 1, 0, 1, 1, 1]
    lo, hi = bootstrap_ci(xs, n_boot=2000)
    assert 0 <= lo < 0.7 < hi <= 1
    assert bootstrap_ci([1, 1, 1], n_boot=100) == (1.0, 1.0)


def test_stratified():
    lo, hi = stratified_bootstrap_ci({"a": [1, 1, 0, 1], "b": [0, 0, 1, 0]}, n_boot=2000)
    assert lo < 0.5 < hi
