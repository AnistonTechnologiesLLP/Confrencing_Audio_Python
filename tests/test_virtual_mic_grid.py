"""Hardware-free tests for the virtual-microphone-grid selection beamformer.

Covers the pure grid math (build, near-field delays, vectorized delay-and-sum, band-energy
scoring), the EMA selection, the public API/geometry/mask, reused device-validation errors,
output-queue backpressure, and lifecycle parity — all without an audio device. numpy required.
"""
import pytest

np = pytest.importorskip("numpy")

import conf_pipeline_control as cc
from conf_pipeline_control import doa
from conf_pipeline_control.audio import InputDevice
import conf_pipeline_control.virtual_mic_grid as vmgmod
from conf_pipeline_control.virtual_mic_grid import (
    DeviceConfigError,
    VirtualMicGrid,
    build_grid,
    build_grid_delays,
    delay_and_sum_grid,
    score_grid,
)


# --------------------------------------------------------------------------- #
# Grid construction + near-field delays
# --------------------------------------------------------------------------- #
def test_build_grid_shape_and_span():
    pts = build_grid(4.0, 3.0, 5, 4, focus_height_m=1.2)
    assert len(pts) == 20
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    assert min(xs) == -2.0 and max(xs) == 2.0
    assert min(ys) == -1.5 and max(ys) == 1.5
    assert all(p[2] == 1.2 for p in pts)
    shifted = build_grid(4.0, 3.0, 5, 4, array_origin_xy=(10.0, 5.0))
    assert min(p[0] for p in shifted) == 8.0 and max(p[0] for p in shifted) == 12.0


def test_build_grid_delays_shape_dtype_nonneg():
    geom = cc.sensibel_8(radius_m=0.040)
    pts = build_grid(2.0, 2.0, 7, 7)
    delays, maxd = build_grid_delays(geom, pts, 44100.0, 343.0)
    assert delays.shape == (49, 8)
    assert np.issubdtype(delays.dtype, np.integer)
    assert (delays >= 0).all()
    assert (delays.min(axis=1) == 0).all()         # farthest mic per point → delay 0
    assert maxd == int(delays.max())


# --------------------------------------------------------------------------- #
# Near-field selection correctness (the key novel behavior)
# --------------------------------------------------------------------------- #
def _matched_ext(geom, delays, maxd, t, B, sr=44100.0, tones=(1200.0, 1800.0, 2600.0)):
    """An 8-ch buffer for a source physically AT grid point ``t``: mic m carries the source
    delayed by its propagation delay A_m = maxd - delays[t,m], so applying point t's focus
    delays realigns every mic exactly (delays[t,m] + A_m == maxd)."""
    M = geom.n_channels
    A = (maxd - delays[t]).astype(int)
    L = maxd + B
    tt = np.arange(L) / sr
    s = sum(np.sin(2 * np.pi * f * tt) for f in tones)
    ext = np.zeros((L, M), dtype=float)
    for m in range(M):
        a = int(A[m])
        ext[a:, m] = s[: L - a] if a else s
    return ext


def test_nearfield_selection_picks_matched_point():
    # A larger radius gives distinct per-point focus patterns (the real 0.040 m array is
    # coarser by design — see module caveats); this isolates the SELECTION algorithm.
    geom = cc.sensibel_8(radius_m=0.12)
    pts = build_grid(1.0, 1.0, 9, 9)
    delays, maxd = build_grid_delays(geom, pts, 44100.0, 343.0)
    t = 60                                          # an off-centre interior point
    B = 2048
    ext = _matched_ext(geom, delays, maxd, t, B)
    monos = delay_and_sum_grid(ext, delays, geom.active_indices(), maxd, B)
    nfft = 1024
    win = np.hanning(nfft)
    band = doa.band_indices(np.fft.rfftfreq(nfft, 1 / 44100.0), 300.0, 3800.0)
    score = score_grid(monos, win, band, nfft)
    assert int(score.argmax()) == t


def test_matched_point_beats_a_distant_point():
    geom = cc.sensibel_8(radius_m=0.040)            # the real array
    pts = build_grid(2.0, 2.0, 9, 9)
    delays, maxd = build_grid_delays(geom, pts, 44100.0, 343.0)
    t, far = 60, 4
    B = 2048
    ext = _matched_ext(geom, delays, maxd, t, B)
    monos = delay_and_sum_grid(ext, delays, geom.active_indices(), maxd, B)
    nfft = 1024
    score = score_grid(monos, np.hanning(nfft),
                        doa.band_indices(np.fft.rfftfreq(nfft, 1 / 44100.0), 300.0, 3800.0), nfft)
    assert score[t] > score[far]


def test_grid_processor_output_shape_and_dead_capsule():
    geom = cc.with_active_channels(cc.sensibel_8(radius_m=0.040), [i != 5 for i in range(8)])
    pts = build_grid(2.0, 2.0, 6, 5)
    delays, maxd = build_grid_delays(geom, pts, 44100.0, 343.0)
    B = 512
    ext = np.concatenate([np.zeros((maxd, 8)), np.random.default_rng(0).standard_normal((B, 8))])
    monos = delay_and_sum_grid(ext, delays, geom.active_indices(), maxd, B)
    assert monos.shape == (30, B) and np.isfinite(monos).all()


# --------------------------------------------------------------------------- #
# Scoring + selection
# --------------------------------------------------------------------------- #
def test_score_band_limit_rejects_out_of_band():
    fs, nfft = 44100.0, 1024
    tt = np.arange(nfft) / fs
    monos = np.stack([np.sin(2 * np.pi * 1000.0 * tt),    # in band
                      np.sin(2 * np.pi * 9000.0 * tt)])   # above the band + aliasing cutoff
    band = doa.band_indices(np.fft.rfftfreq(nfft, 1 / fs), 300.0, 3800.0)
    score = score_grid(monos, np.hanning(nfft), band, nfft)
    assert score[0] > 50.0 * score[1]


def test_selection_smoothing_reduces_flicker():
    s_a = np.array([10.0, 1.0])
    s_b = np.array([1.0, 10.0])
    # a=0.3 → ema after one opposing block = 0.3*[1,10] + 0.7*[10,1] = [7.3, 3.7] (still favors 0)
    smoothed = VirtualMicGrid(device=None, grid_cols=2, grid_rows=1, selection_smoothing=0.3)
    assert smoothed._update_selection(s_a)[0] == 0
    assert smoothed._update_selection(s_b)[0] == 0       # one opposing block: EMA holds
    raw = VirtualMicGrid(device=None, grid_cols=2, grid_rows=1, selection_smoothing=1.0)
    raw._update_selection(s_a)
    assert raw._update_selection(s_b)[0] == 1            # no smoothing → flips immediately


def test_topk_blend_and_selected_xy():
    vmg = VirtualMicGrid(device=None, grid_cols=3, grid_rows=1, top_k=2)
    monos = np.array([[1.0, 1.0, 1.0], [2.0, 2.0, 2.0], [0.0, 0.0, 0.0]])
    sel, order, ema = vmg._update_selection(np.array([5.0, 9.0, 1.0]))
    assert sel == 1
    mono = vmg._mix_output(monos, order, ema)
    assert mono.shape == (3,)
    assert np.allclose(mono, (9 * 2 + 5 * 1) / 14.0, atol=1e-5)   # score-weighted top-2 blend
    assert vmg.selected_xy == vmg.grid_points()[1]               # selected_xy = top-1


# --------------------------------------------------------------------------- #
# Constructor / geometry / mask
# --------------------------------------------------------------------------- #
def test_constructor_geometry_and_mask():
    vmg = VirtualMicGrid(device=None, grid_cols=5, grid_rows=4)
    assert vmg.geometry.n_channels == 8 and vmg.geometry.n_active == 8
    assert vmg.backend == "vmic-grid"
    assert vmg.grid_size == 20 and len(vmg.grid_points()) == 20
    assert abs(vmg.geometry.aperture_m() - 0.080) < 1e-6

    dead = VirtualMicGrid(device=None, dead_capsule=5)
    assert dead.geometry.n_active == 7
    assert dead.geometry.active_indices() == (0, 1, 2, 3, 4, 6, 7)
    masked = VirtualMicGrid(device=None, active_mask=[i != 2 for i in range(8)], dead_capsule=5)
    assert masked.geometry.active_indices() == (0, 1, 3, 4, 5, 6, 7)   # active_mask wins


def test_invalid_args_raise():
    with pytest.raises(ValueError):
        VirtualMicGrid(device=None, active_mask=[True] * 7)
    with pytest.raises(ValueError):
        VirtualMicGrid(device=None, active_mask=[False] * 8)
    with pytest.raises(ValueError):
        VirtualMicGrid(device=None, top_k=0)
    with pytest.raises(ValueError):
        VirtualMicGrid(device=None, selection_smoothing=1.5)
    with pytest.raises(ValueError):
        VirtualMicGrid(device=None, score_band=(3800.0, 300.0))


def test_scores_and_selected_xy_none_initially():
    vmg = VirtualMicGrid(device=None)
    assert vmg.selected_xy is None and vmg.scores() is None and vmg.streaming is False


# --------------------------------------------------------------------------- #
# Device-validation errors (reused logic; monkeypatched device list)
# --------------------------------------------------------------------------- #
def test_device_not_found_raises(monkeypatch):
    monkeypatch.setattr(vmgmod, "controls_available", lambda: True)
    monkeypatch.setattr(vmgmod, "list_input_devices", lambda: [InputDevice(7, "POLARIS", 8, 44100.0)])
    with pytest.raises(ValueError, match="not found"):
        VirtualMicGrid(device=99).connect()


def test_too_few_channels_raises(monkeypatch):
    monkeypatch.setattr(vmgmod, "controls_available", lambda: True)
    monkeypatch.setattr(vmgmod, "list_input_devices", lambda: [InputDevice(3, "Stereo Mic", 2, 44100.0)])
    with pytest.raises(DeviceConfigError, match="needs 8"):
        VirtualMicGrid(device=3).connect()


def test_missing_extra_raises_install_hint(monkeypatch):
    monkeypatch.setattr(vmgmod, "controls_available", lambda: False)
    with pytest.raises(RuntimeError, match=r"\[control\]"):
        VirtualMicGrid(device=None).connect()


# --------------------------------------------------------------------------- #
# Output delivery + lifecycle parity
# --------------------------------------------------------------------------- #
def test_output_queue_drop_oldest_and_callback():
    seen = []
    vmg = VirtualMicGrid(device=None, output_queue_size=2, output_callback=seen.append)
    for i in range(4):
        vmg._emit(np.full(3, float(i), dtype=np.float32))
    q = vmg.output_queue
    got = [q.get_nowait() for _ in range(q.qsize())]
    assert len(got) == 2
    assert got[0][0] == 2.0 and got[1][0] == 3.0
    assert [b[0] for b in seen] == [0.0, 1.0, 2.0, 3.0]


def test_lifecycle_parity(monkeypatch):
    vmg = VirtualMicGrid(device=None)
    assert vmg.read_level() == 0.0
    monkeypatch.setattr(vmg, "_open", lambda: None)
    monkeypatch.setattr(vmg, "_close", lambda: None)

    vmg.stop()                          # safe before start
    vmg.start()
    assert vmg.connected
    vmg._level = 0.5
    assert abs(vmg.read_level() - 0.5) < 1e-6
    vmg.set_mute(True)
    assert vmg.read_level() == 0.0
    vmg.set_mute(False)
    vmg.set_gain_db(6.0)
    assert vmg.read_level() > 0.5
    assert vmg.state().backend == "vmic-grid"
    assert vmg.state().active_channels == 8
    vmg.stop()
    assert not vmg.connected
