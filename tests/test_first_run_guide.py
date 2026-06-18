"""Pure step-model for the first-run setup guide (no Qt / no QApplication)."""
from conf_pipeline_gui.panels import first_run as fr
from conf_pipeline_gui.panels.first_run import GuideSnapshot


def test_default_snapshot_starts_at_mode():
    snap = GuideSnapshot()
    assert fr.active_step(snap) == fr.STEP_MODE
    assert not fr.all_done(snap) and not fr.required_done(snap)


def test_mode_step_uses_touched_not_value():
    # the combo defaults to "table"; an untouched default must NOT count as done
    assert not fr.step_done(fr.STEP_MODE, GuideSnapshot(listening_mode="table"))
    assert fr.step_done(fr.STEP_MODE, GuideSnapshot(listening_mode="table", listening_mode_touched=True))


def test_connect_step_keys_off_busy():
    assert not fr.step_done(fr.STEP_CONNECT, GuideSnapshot())
    assert fr.step_done(fr.STEP_CONNECT, GuideSnapshot(busy=True))


def test_calibrate_is_irrelevant_for_table_mode():
    table = GuideSnapshot(listening_mode="table", listening_mode_touched=True)
    assert not fr.step_relevant(fr.STEP_CALIBRATE, table)
    assert fr.step_done(fr.STEP_CALIBRATE, table)          # auto-skipped → treated as satisfied
    follow = GuideSnapshot(listening_mode="follow", listening_mode_touched=True)
    assert fr.step_relevant(fr.STEP_CALIBRATE, follow)
    assert not fr.step_done(fr.STEP_CALIBRATE, follow)


def test_calibrate_is_never_inferred_from_offset():
    # only the dedicated flag flips it — 0° is a legitimate "already aligned" result
    follow = GuideSnapshot(listening_mode="follow", listening_mode_touched=True)
    assert not fr.step_done(fr.STEP_CALIBRATE, follow)
    assert fr.step_done(fr.STEP_CALIBRATE, GuideSnapshot(
        listening_mode="follow", listening_mode_touched=True, front_calibrated=True))


def test_hear_step_meter_or_manual_ack():
    assert not fr.step_done(fr.STEP_HEAR, GuideSnapshot(busy=True, monitor_on=True))          # meter at 0
    assert fr.step_done(fr.STEP_HEAR, GuideSnapshot(busy=True, monitor_on=True, meter_level=0.3))
    assert fr.step_done(fr.STEP_HEAR, GuideSnapshot(heard_ack=True))                          # silent-room fallback


def test_required_done_ignores_optional_steps():
    # table mode required = mode + connect + hear; detect is optional, calibrate irrelevant
    snap = GuideSnapshot(listening_mode="table", listening_mode_touched=True, busy=True, heard_ack=True)
    assert fr.required_done(snap)                          # completion reached without the optional detect
    assert not fr.step_done(fr.STEP_DETECT, snap)
    assert not fr.all_done(snap)                           # all_done still wants the optional step ticked


def test_all_done_when_every_relevant_step_ticked():
    snap = GuideSnapshot(listening_mode="follow", listening_mode_touched=True, busy=True,
                         caps_probed=True, front_calibrated=True, heard_ack=True)
    assert fr.all_done(snap) and fr.required_done(snap)
    assert fr.active_step(snap) is None


def test_progress_counts_relevant_steps_only():
    follow = GuideSnapshot(listening_mode="follow", listening_mode_touched=True)
    assert fr.progress(follow) == (1, 5)                   # all five apply
    table = GuideSnapshot(listening_mode="table", listening_mode_touched=True)
    assert fr.progress(table) == (1, 4)                    # calibrate skipped


def test_active_step_walks_in_order():
    follow = GuideSnapshot(listening_mode="follow", listening_mode_touched=True)
    assert fr.active_step(follow) == fr.STEP_CONNECT
    assert fr.active_step(GuideSnapshot(
        listening_mode="follow", listening_mode_touched=True, busy=True)) == fr.STEP_DETECT
