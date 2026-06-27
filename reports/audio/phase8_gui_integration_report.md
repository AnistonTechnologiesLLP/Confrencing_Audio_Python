# Phase 8 — Main GUI Operator Diagnostics Wiring Report

**Goal:** make the existing read-only `OperatorStatusPanel` reachable inside the running desktop app —
wiring only, no new DSP, no live controls, no default change.

Status: **COMPLETE.** 8 operator GUI tests green (offscreen), full non-GUI suite **1034 passed**
(unchanged), mypy clean; app.py wiring verified without constructing MainWindow.

---

## 1. Where the panel was wired
- **App menu** (`conf_pipeline_gui/app.py`, `_build_app_menu`): a new action **"Audio operator
  diagnostics…"**, placed right after **"Export commissioning report…"** — the existing diagnostics
  group. It calls `MainWindow._show_operator_diagnostics`, which opens an `OperatorDiagnosticsWindow`
  (a separate top-level window) built from the running engine via `MainWindow._operator_status`.
- **`conf_pipeline_gui/panels/operator.py`:** new `OperatorDiagnosticsWindow(QWidget)` = the existing
  `OperatorStatusPanel` + a **Refresh** button + an **Export JSON + Markdown** button + a status line,
  driven by a `status_provider` callable. `OperatorStatusPanel` itself is unchanged.
- **`conf_pipeline_gui/panels/live.py`:** new public `LivePanel.active_engine()` — returns the running
  flag-bearing engine (the A/B engine unwrapped to its `_steered` core, auto-steer to its inner `ctrl`,
  else the zone `LiveBeamController`) or `None`. Read-only accessor over the existing `_ab_target()`.

## 2. Why this location was chosen
**Option B (menu → separate window)** is the smallest safe change. It mirrors the existing
"Export commissioning report…" action exactly (same menu, same `panels["live"]` access pattern), does
**not** touch the ModeBar / tab / layout structure (so it can't break the fragile MainWindow), and the
heavy parts (the window, the accessor, the status build) are all testable offscreen via the established
single-panel pattern. A new tab (Option A) would have meant restructuring the main layout — higher risk
on a MainWindow that can't be exercised headless here.

## 3. What remains read-only
Everything. The window shows status and offers Refresh + Export only. `OperatorStatus` reads engine
**flags**; it never sets one. The window has **no DSP toggles**, no calibration/pre-NR/placement apply,
and the header label states "Read-only diagnostics — no live controls; suggestions are not
auto-applied." Building the status does not enable any feature (verified by test).

## 4. What was not added
No new tab, no live control toggles, no calibration-apply / placement-auto-apply buttons, no
transcription-vendor or virtual-mic setup, no DSP-default change, no placement/egress/transcription
internal change, no MainWindow restructure. The standalone panel and the CLI are untouched and still work.

## 5. Test results
```
QT_QPA_PLATFORM=offscreen pytest -q tests/test_gui_operator.py        → 8 passed
pytest -q --ignore-glob='tests/test_gui_*.py' -p no:cacheprovider     → 1034 passed (unchanged)
mypy                                                                   → clean, 71 files (unchanged)
```
New offscreen tests: `LivePanel.active_engine()` is `None` when not connected; the diagnostics window
refreshes + renders (and shows `HPF/notch` when pre-NR is on); a `None` status is safe; export writes
`operator_diagnostics_*.{json,md}`; refreshing forces no DSP option on. A `python -c` import check
confirms `app.py` imports and `MainWindow._show_operator_diagnostics` / `_operator_status` /
`LivePanel.active_engine` exist — **without constructing MainWindow** (so no headless hang).

## 6. Manual GUI verification steps
On a real Windows desktop (not offscreen):
```powershell
.\.venv\Scripts\python.exe run_gui.py
```
1. The main app opens normally.
2. Open the **app menu (☰)** → click **"Audio operator diagnostics…"**.
3. A separate **"Audio Operator Diagnostics"** window appears, showing Device / Calibration / Placement /
   Pipeline / Egress / Transcription + warnings.
4. With no beam connected it reads "No running engine…"; connect a beam (LIVE mode) and click **Refresh**
   to see live Calibration ON/OFF + the pipeline `[on]/[off]` stages (and `HPF/notch` if pre-NR is on).
5. Click **Export JSON + Markdown** → writes `reports/audio/operator_diagnostics_<stamp>.{json,md}` and
   shows the saved paths.
6. Confirm there are **no DSP toggles** in the window and nothing is auto-applied.

## 7. Known limitations
- **MainWindow itself is not tested headless** (it hangs offscreen on this box, per CLAUDE.md) — the
  3-line menu action is verified by inspection + a no-construct import check + CI; the window, accessor,
  and status build are fully tested offscreen.
- The in-app window reflects the **running** engine; when not connected it shows all-off defaults. For a
  fully-configurable view (force pre-NR / calibration / placement in), the CLI
  `scripts/operator_diagnostics.py` remains the richer tool.
- Engine-flag fidelity depends on the live path: the A/B engine is unwrapped to its `_steered` core and
  auto-steer to its `ctrl`, so calibration/pre-NR/cleaner flags are visible; an unusual wrapper without
  those would read flags as off (defensive, never crashes).
- Export writes to `reports/audio/` (matching the CLI); it does not prompt for a folder.
