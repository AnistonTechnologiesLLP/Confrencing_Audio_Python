"""MultiTrackRecorder — per-person WAV tracks (the per-person half of "capture everyone")."""
import os
import wave

import pytest

np = pytest.importorskip("numpy")

from conf_pipeline_control.multibeam import MultiTrackRecorder


def _read_wav(path):
    with wave.open(path, "rb") as w:
        return w.getnchannels(), w.getframerate(), w.getnframes()


def test_not_recording_writes_nothing(tmp_path):
    rec = MultiTrackRecorder(2, 44100.0)
    rec.feed([np.ones(64, np.float32), np.ones(64, np.float32)])      # ignored — not started
    assert rec.write(str(tmp_path)) == []


def test_records_one_wav_per_beam_plus_mixed(tmp_path):
    rec = MultiTrackRecorder(2, 44100.0)
    rec.start()
    block = 100
    for _ in range(3):
        monos = [np.full(block, 0.2, np.float32), np.full(block, 0.1, np.float32)]
        rec.feed(monos, mixed=np.full(block, 0.25, np.float32))
    rec.stop()
    paths = rec.write(str(tmp_path), prefix="cap")
    assert len(paths) == 3                                            # 2 beams + mixed
    for p in paths:
        ch, sr, frames = _read_wav(p)
        assert ch == 1 and sr == 44100 and frames == 3 * block
    assert any(os.path.basename(p) == "cap_mixed.wav" for p in paths)


def test_labels_drive_filenames(tmp_path):
    rec = MultiTrackRecorder(2, 44100.0)
    rec.start()
    rec.set_labels(["sofa-seat1", "sofa seat 2!"])                    # second has unsafe chars
    rec.feed([np.zeros(32, np.float32), np.zeros(32, np.float32)])
    rec.stop()
    names = sorted(os.path.basename(p) for p in rec.write(str(tmp_path), prefix="cap"))
    assert names == ["cap_1_sofa-seat1.wav", "cap_2_sofa-seat-2.wav"]   # unsafe chars -> '-', trailing trimmed


def test_only_beams_with_data_are_written(tmp_path):
    rec = MultiTrackRecorder(3, 44100.0)
    rec.start()
    rec.feed([np.ones(16, np.float32), np.ones(16, np.float32)])      # only 2 of 3 beams fed
    rec.stop()
    assert len(rec.write(str(tmp_path))) == 2
