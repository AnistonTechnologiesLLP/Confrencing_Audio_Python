"""Hardware-free tests for the far-end reference capture (live AEC).

Covers the pure, device-independent logic: the circular ring (write/recent/wrap),
the resampler (no-op + rate change), the auto-detect candidate ordering (WASAPI
loopback → Stereo Mix → manual) with a fake sounddevice, and graceful degrade
(``recent`` returns zeros with no source). Opening real streams needs hardware and
is not exercised here. numpy is required and skipped if absent.
"""
import pytest

np = pytest.importorskip("numpy")

from conf_pipeline_control.reference_capture import ReferenceCapture, _Ring, _resample_to


# --------------------------------------------------------------------------- #
# Circular ring
# --------------------------------------------------------------------------- #
def test_ring_recent_newest_last_and_front_pads():
    r = _Ring(100)
    r.write(np.arange(10, dtype=np.float32))
    out = r.recent(4)
    assert out.tolist() == [6.0, 7.0, 8.0, 9.0]              # newest 4, newest last
    out2 = r.recent(16)                                      # more than written → zero-front-padded
    assert out2.shape == (16,) and out2[:6].tolist() == [0.0] * 6 and out2[6:].tolist() == list(range(10))


def test_ring_wraps_and_keeps_newest():
    r = _Ring(8)
    r.write(np.arange(6, dtype=np.float32))                  # 0..5
    r.write(np.arange(6, 12, dtype=np.float32))              # 6..11 → wraps; ring holds the newest 8 (4..11)
    assert r.recent(8).tolist() == [4, 5, 6, 7, 8, 9, 10, 11]
    big = np.arange(100, 120, dtype=np.float32)              # bigger than the ring → keep its tail
    r.write(big)
    assert r.recent(8).tolist() == list(range(112, 120))


def test_ring_clear_and_empty_recent_is_zeros():
    r = _Ring(50)
    assert r.recent(10).tolist() == [0.0] * 10               # nothing written yet
    r.write(np.ones(20, dtype=np.float32))
    r.clear()
    assert float(np.max(np.abs(r.recent(20)))) == 0.0


# --------------------------------------------------------------------------- #
# Resampler
# --------------------------------------------------------------------------- #
def test_resample_noop_when_same_rate():
    x = np.linspace(-1, 1, 512).astype(np.float32)
    out = _resample_to(x, 48000.0, 48000.0)
    assert out.shape == x.shape and np.allclose(out, x)


def test_resample_changes_length_by_ratio():
    x = (0.3 * np.random.default_rng(0).standard_normal(4800)).astype(np.float32)
    out = _resample_to(x, 48000.0, 44100.0)                  # ~0.91875× samples
    assert abs(out.shape[0] - int(round(4800 * 44100 / 48000))) <= 2
    assert bool(np.all(np.isfinite(out)))


# --------------------------------------------------------------------------- #
# Auto-detect candidate ordering (fake sounddevice)
# --------------------------------------------------------------------------- #
class _FakeSd:
    def __init__(self, hostapis, devices, wasapi_ok=True):
        self._hostapis, self._devices, self._wasapi_ok = hostapis, devices, wasapi_ok

    def query_hostapis(self, index=None):
        return self._hostapis if index is None else self._hostapis[index]

    def query_devices(self, idx=None, kind=None):
        return self._devices if idx is None else self._devices[idx]

    def WasapiSettings(self, loopback=False):
        if not self._wasapi_ok:
            raise RuntimeError("loopback unsupported")
        return ("WasapiSettings", loopback)


def _dev(name, ic, oc, sr=48000.0):
    return {"name": name, "max_input_channels": ic, "max_output_channels": oc, "default_samplerate": sr}


def test_candidates_prefers_wasapi_loopback():
    rc = ReferenceCapture(44100.0)
    rc._sd = _FakeSd(
        hostapis=[{"name": "Windows WASAPI", "default_output_device": 1}],
        devices=[_dev("Mic", 2, 0), _dev("Speakers", 0, 2, 48000.0), _dev("Stereo Mix", 2, 0)],
        wasapi_ok=True,
    )
    cands = list(rc._candidates())
    assert cands, "expected at least one candidate"
    dev, ch, sr, extra, label = cands[0]
    assert dev == 1 and ch == 2 and sr == 48000.0 and extra == ("WasapiSettings", True)
    assert "loopback" in label.lower()


def test_candidates_falls_back_to_stereo_mix_when_no_wasapi_loopback():
    rc = ReferenceCapture(44100.0)
    rc._sd = _FakeSd(
        hostapis=[{"name": "Windows WASAPI", "default_output_device": 1}],
        devices=[_dev("Speakers", 0, 2), _dev("Stereo Mix (Realtek)", 2, 0, 48000.0)],
        wasapi_ok=False,                                      # WasapiSettings raises → skip loopback
    )
    cands = list(rc._candidates())
    assert len(cands) == 1
    dev, ch, sr, extra, label = cands[0]
    assert dev == 1 and ch == 2 and extra is None and "stereo mix" in label.lower()


def test_candidates_manual_device_only():
    rc = ReferenceCapture(44100.0, device=7)
    rc._sd = _FakeSd(hostapis=[], devices=[_dev(f"d{i}", 2, 0) for i in range(8)])
    cands = list(rc._candidates())
    assert len(cands) == 1 and cands[0][0] == 7 and "device 7" in cands[0][4]


def test_recent_zeros_before_start():
    rc = ReferenceCapture(44100.0)                           # never started → no source
    assert not rc.available
    assert float(np.max(np.abs(rc.recent(256)))) == 0.0
