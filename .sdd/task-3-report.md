# Task 3 Report — MultiKitController fence tick + _produce veto/gate + preconditions

## Status
COMPLETE — all targeted tests green, mypy clean, full suite pending.

## Targeted tests
`tests/test_multikit.py`: **41 passed** (29 pre-existing + 12 new Task-3 tests)

## Full suite
Pending (running as background job).

## What was built

### `conf_pipeline_control/multikit.py`

**Ctor changes (default-OFF / inert):**
- New params: `fence_polygon=None`, `fence_margin_m=DEFAULT_FENCE_MARGIN_M`,
  `fence_hold_ticks=DEFAULT_FENCE_HOLD_TICKS`, `fence_duck_db=-60.0`
- Fields: `_fence_polygon`, `_fence_decider` (None when no polygon), `_fence_duck_gain`,
  `_fence_last: Optional[FenceDecision] = None`
- `_fence_decider is None` ⇒ entire fence path skipped ⇒ `_produce` byte-identical to pre-fence

**Loud preconditions:**
- `fence_polygon` given with `n_kits != 2` ⇒ `ValueError("Audio fence needs exactly 2 kits; got N")`
  raised in `__init__` before the fail-open guard
- `update_fence()` with a None pose entry ⇒ `FenceConfigError(...)` raised from the pose
  validation block that runs BEFORE the `try/except` fail-open guard — so setup errors always surface

**`update_fence(t)` — control-thread tick:**
- Guards: returns immediately when `_fence_decider is None`
- Pose precondition check runs BEFORE the fail-open try/except (so `FenceConfigError` escapes)
- Reads both `kit_reading()` calls (outside controller lock per existing pattern)
- Snapshots poses under `_lock` to avoid tearing
- Calls `_fence_decider.update(...)`, atomically rebinds `self._fence_last = dec`
- Wrapped in `try/except` — on any runtime fusion error, leaves `_fence_last` at last-good / None

**`_produce` hooks (audio-thread only):**
- Reads `dec = self._fence_last` once (at the top, before selector) — no lock, no fusion call
- Selection veto: `if dec is not None and dec.veto_kit is not None: eff[dec.veto_kit] = 0.0`
  between `eff` build and `self._selector.update(eff, t)`
- Output gate: `if dec is not None and not dec.keep: mono = mono * self._fence_duck_gain`
  after AGC, before master mute/gain — scalar multiply, no glitch

**`fence_status() -> Optional[dict]`:**
- Returns `None` when `_fence_decider is None` (fence off); `status()`/`KitStatus` untouched
- Returns dict with keys: `keep`, `veto_kit`, `point` (`(x,y)` or None), `inside`,
  `confidence`, `degenerate`, `polygon`

**Import change:** imports `FenceDecider`, `FenceDecision`, `FenceConfigError`,
`DEFAULT_FENCE_HOLD_TICKS`, `DEFAULT_FENCE_MARGIN_M` from `.fence`

## Design decisions

**Where the pose precondition is raised:** inside `update_fence()`, checked BEFORE the
`try/except` fail-open guard. Rationale: the alternative was `set_fence_poses()` but
that method is called before the fence is used and also accepts `None` entries for
partial configuration (valid per Task-2). Raising in `update_fence()` means the error
surfaces the first time the operator actually tries to use the fence with bad config —
consistent with "visible, not silent-inert".

**`dec` read once before selector, reused at gate:** The same `dec` reference is used
for both the veto (before selector) and the gate (after AGC). This is safe because
`_fence_last` is Python-atomically rebound (GIL), and reading the same snapshot for
both operations within one `_produce` call is correct (coherent per-block decision).

**`update_fence` pose snapshot under lock:** `set_fence_poses` writes `_fence_poses` under
lock; `update_fence` reads it under lock to snapshot, then uses poses outside the lock.
No engine calls inside the lock (matching the existing `kit_reading` pattern).

## RT safety
- `_produce` touches: one attribute read (`self._fence_last`) + one conditional scalar
  multiply. No engine calls, no `reading()`, no new locks, no allocation beyond existing path.
- All fusion math is in `update_fence()` on the control thread.

## mypy
`Success: no issues found in 63 source files`
