"""Scene scheduler — recall scenes on the config's weekly schedule (stdlib).

Executes the ``SceneSchedule`` entries stored in ``config.control.schedules``:
at each entry's local ``"HH:MM"`` on its weekdays, the scene is recalled
through the same ``get_config`` / ``apply`` pair the control API uses, so the
GUI, the HTTP API, and the scheduler all mutate one consistent config.

Deterministic by construction: the clock is injectable (``now_fn``) and
:meth:`SceneScheduler.run_pending` is a manual tick, so tests (or a GUI timer)
can drive it without threads or sleeps. :meth:`start` adds a small daemon
polling thread for headless use. An entry fires at most once per scheduled
minute; a schedule whose scene has vanished is skipped (validation flags it
as ``SCHEDULE_INVALID``).
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from functools import partial
from typing import Callable, Optional

from .api import recall_scene
from .model import WEEKDAYS, SceneSchedule, SystemConfig, parse_hhmm

Transform = Callable[[SystemConfig], SystemConfig]


def _matches(schedule: SceneSchedule, now: datetime) -> bool:
    hm = parse_hhmm(schedule.time)
    if hm is None or not schedule.enabled:
        return False
    return WEEKDAYS[now.weekday()] in schedule.days and (now.hour, now.minute) == hm


def next_fire(schedules: list[SceneSchedule], now: datetime) -> Optional[datetime]:
    """The next moment any enabled entry is due, scanning a week ahead."""
    best: Optional[datetime] = None
    for s in schedules:
        hm = parse_hhmm(s.time)
        if hm is None or not s.enabled or not s.days:
            continue
        for ahead in range(8):
            day = now + timedelta(days=ahead)
            if WEEKDAYS[day.weekday()] not in s.days:
                continue
            candidate = day.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
            if candidate < now.replace(second=0, microsecond=0):
                continue
            if best is None or candidate < best:
                best = candidate
            break
    return best


class SceneScheduler:
    """Drives scene recalls from the config's schedule entries."""

    def __init__(
        self,
        get_config: Callable[[], SystemConfig],
        apply: Callable[[Transform], SystemConfig],
        *,
        now_fn: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._get = get_config
        self._apply = apply
        self._now = now_fn if now_fn is not None else datetime.now
        self._fired: dict[str, str] = {}     # schedule id → minute stamp last fired
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- manual tick (tests / GUI timers) ----
    def run_pending(self) -> list[str]:
        """Recall every entry due *now* (once per scheduled minute). Returns the
        scene ids recalled this tick."""
        now = self._now()
        stamp = now.strftime("%Y-%m-%d %H:%M")
        config = self._get()
        schedules = config.control.schedules if config.control is not None else []
        recalled: list[str] = []
        for s in schedules:
            if not _matches(s, now) or self._fired.get(s.id) == stamp:
                continue
            self._fired[s.id] = stamp        # at most once per scheduled minute
            try:
                self._apply(partial(recall_scene, scene_id=s.scene_id))
            except ValueError:
                continue                     # scene vanished — validation's problem
            recalled.append(s.scene_id)
        # drop stale dedup marks so the map can't grow without bound
        self._fired = {k: v for k, v in self._fired.items() if v == stamp}
        return recalled

    def next_fire(self) -> Optional[datetime]:
        config = self._get()
        schedules = config.control.schedules if config.control is not None else []
        return next_fire(list(schedules), self._now())

    # ---- background polling (headless) ----
    @property
    def running(self) -> bool:
        return self._thread is not None

    def start(self, poll_seconds: float = 15.0) -> None:
        """Poll ``run_pending`` on a daemon thread. Sub-minute polling is enough
        — firing is deduplicated per scheduled minute."""
        if self._thread is not None:
            return
        self._stop.clear()

        def _loop() -> None:
            while not self._stop.wait(poll_seconds):
                self.run_pending()

        self._thread = threading.Thread(target=_loop, name="scene-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=5)
        self._thread = None

    def __enter__(self) -> "SceneScheduler":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
