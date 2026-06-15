"""Tests for the swappable estimate-smoothing trackers (pure stdlib; numpy only for the
array-smoothing case)."""
import pytest

from conf_pipeline_control.tracking import AlphaBetaTracker, ExponentialTracker, Tracker


# --------------------------------------------------------------------------- #
# ExponentialTracker (one-pole EMA) — the grid's selection smoother
# --------------------------------------------------------------------------- #
def test_exponential_passthrough_and_halfway():
    pt = ExponentialTracker(1.0)                       # alpha=1 → no smoothing
    assert pt.update(3.0) == 3.0
    assert pt.update(7.0) == 7.0
    sm = ExponentialTracker(0.5)
    assert sm.update(0.0) == 0.0                       # first obs initializes
    assert sm.update(10.0) == 5.0                      # 0.5*10 + 0.5*0


def test_exponential_reset_reacquires():
    sm = ExponentialTracker(0.3)
    sm.update(5.0)
    assert sm.value == 5.0
    sm.reset()
    assert sm.value is None
    assert sm.update(2.0) == 2.0                        # next obs re-acquires, no blend with stale


def test_exponential_rejects_bad_alpha():
    for bad in (-0.1, 1.5):
        with pytest.raises(ValueError):
            ExponentialTracker(bad)


def test_exponential_smooths_numpy_array():
    np = pytest.importorskip("numpy")
    t = ExponentialTracker(0.5)
    a = np.array([10.0, 1.0])
    b = np.array([1.0, 10.0])
    assert np.allclose(t.update(a), a)                  # init
    assert np.allclose(t.update(b), [5.5, 5.5])         # elementwise EMA, one call for the vector


# --------------------------------------------------------------------------- #
# AlphaBetaTracker (constant-velocity / steady-state Kalman hook)
# --------------------------------------------------------------------------- #
def test_alpha_beta_tracks_ramp_with_less_lag_than_ema():
    ab = AlphaBetaTracker(alpha=0.5, beta=0.1)
    ema = ExponentialTracker(0.5)
    truth = [float(i) for i in range(80)]               # ramp, slope 1 per step
    y_ab = y_ema = 0.0
    for z in truth:
        y_ab, y_ema = ab.update(z), ema.update(z)
    # CV model carries velocity → near-zero steady-state lag; EMA lags a ramp by (1-a)/a = 1.
    assert abs(y_ab - truth[-1]) < abs(y_ema - truth[-1])
    assert abs(y_ab - truth[-1]) < 0.2


def test_alpha_beta_reduces_noise_around_constant():
    seq = [1.0, -1.0, 1.0, -1.0, 1.0, -1.0, 1.0, -1.0]  # zero-mean jitter about 0
    ab = AlphaBetaTracker(alpha=0.4, beta=0.05)
    ab.update(0.0)                                       # acquire at the mean
    outs = [ab.update(z) for z in seq]
    assert max(abs(o) for o in outs) < 1.0              # smoothed below the ±1 input swing


def test_alpha_beta_reset_and_validation():
    ab = AlphaBetaTracker()
    ab.update(4.0)
    assert ab.value == 4.0
    ab.reset()
    assert ab.value is None
    with pytest.raises(ValueError):
        AlphaBetaTracker(dt=0.0)
    with pytest.raises(ValueError):
        AlphaBetaTracker(alpha=0.0)


def test_both_are_trackers():
    assert isinstance(ExponentialTracker(0.5), Tracker)
    assert isinstance(AlphaBetaTracker(), Tracker)
    with pytest.raises(TypeError):
        Tracker()                                       # abstract — reset() unimplemented
