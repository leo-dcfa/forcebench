import math

from forcebench.stats import bootstrap_ci, pass_at_k, stratified_bootstrap_ci, wilson_ci


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


def test_wilson_known_values():
    lo, hi = wilson_ci(18, 18)
    assert math.isclose(lo, 0.8241, abs_tol=1e-4)
    assert hi == 1.0
    lo, hi = wilson_ci(0, 18)
    assert lo == 0.0
    assert math.isclose(hi, 0.1759, abs_tol=1e-4)
    lo, hi = wilson_ci(9, 18)
    assert math.isclose(lo, 0.2903, abs_tol=1e-4)
    assert math.isclose(hi, 0.7097, abs_tol=1e-4)


def test_wilson_never_has_zero_width():
    # A bootstrap of a suite where every task passed collapses to 100-100; Wilson doesn't.
    for n in (1, 5, 15, 20):
        for k in (0, n / 2, n):
            lo, hi = wilson_ci(k, n)
            assert 0.0 <= lo < hi <= 1.0
            assert lo <= k / n <= hi
