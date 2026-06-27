"""Auto-steering controller: DOA detection → sector gate → live extraction.

Ties the pieces together for the "listen only to people in this coverage area"
feature. A slow control thread (a few Hz — NOT the audio hot loop) repeatedly:

1. snapshots the array's spatial covariance from the live runtime,
2. runs :func:`conf_pipeline_control.doa.detect` to find who is talking and where,
3. gates the detections to the coverage **sector** (center ± half-width),
4. rebuilds a multi-look beam with
   :func:`conf_pipeline_control.beamformer.design_multi_bearings` — one look per
   in-sector talker, nulling the out-of-sector talkers — and re-applies it.

The audio thread keeps beamforming with the latest weights the whole time, so
re-steering is seamless. Hysteresis (hold + re-select deadband) stops the beams
flickering during normal turn-taking. Optionally mutes the output when nobody is
in the sector, so only in-area voices are ever heard.

numpy/sounddevice are pulled in lazily by the live runtime + DOA module, so
importing this module without the ``[control]`` extra is fine.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from . import doa


def _apply_zone_cut(config: Any, array_id: str, in_az: list, out_az: list) -> tuple:
    """Zone-cut policy (pure): keep only looks pointing INTO a pickup zone; null the rest plus every
    exclusion (no-pickup / door) zone. Returns ``(kept_looks, nulls)``. The ``margin_deg`` inside
    :func:`azimuth_in_pickup_zone` is the spatial hysteresis so a boundary-jittering bearing doesn't flap."""
    from conf_pipeline.seat_mapper import azimuth_in_pickup_zone, exclusion_zone_azimuths

    keep: list = []
    dropped: list = []
    for az in in_az:
        (keep if azimuth_in_pickup_zone(config, array_id, az) else dropped).append(az)
    nulls = list(out_az) + dropped + list(exclusion_zone_azimuths(config, array_id))
    return keep, nulls
from .beamformer import MODE_SUPERDIRECTIVE, design_multi_bearings
from .geometry import ArrayGeometry
from .live import LiveBeamController
from .model import DEFAULT_DESIGN_FREQ_HZ
from .polaris_beamformer import (
    DEFAULT_DEREVERB_BETA,
    DEFAULT_DEREVERB_GMIN_DB,
    DEFAULT_DEREVERB_T60,
    DEFAULT_POST_NR_AMOUNT,
    DEFAULT_POST_NR_ENGINE,
    DEFAULT_POST_NR_FLOOR_DB,
    DEFAULT_POST_NR_OVERSUB,
    DEFAULT_POST_NR_PRESERVE_LEVEL,
)


@dataclass(frozen=True)
class SectorConfig:
    """The coverage area as an angular arc (the "radius" expressed in degrees)."""

    center_deg: float = 0.0          # bearing of the arc centre (after front_offset)
    half_width_deg: float = 60.0     # arc half-width; full sector = 2× this
    front_offset_deg: float = 0.0    # aligns azimuth-0 to the room/desk "front"


class AutoSteerController:
    """Detect in-area talkers and steer/extract them live.

    Wraps a :class:`~conf_pipeline_control.live.LiveBeamController` with
    covariance tracking on. Call :meth:`start`, read :meth:`detections` for a live
    readout, and :meth:`stop` (or use as a context manager). Audio I/O, mute and
    gain are delegated to the wrapped controller."""

    def __init__(
        self,
        geometry: ArrayGeometry,
        sector: SectorConfig,
        *,
        device: Optional[int] = None,
        samplerate: float = 44100.0,
        off_nadir_deg: float = 90.0,
        max_talkers: int = 3,
        grid_step_deg: float = 2.0,
        min_separation_deg: float = 40.0,
        min_salience_db: float = 3.0,
        vad_floor_db: float = 3.0,
        freq_hz: float = DEFAULT_DESIGN_FREQ_HZ,
        mode: str = MODE_SUPERDIRECTIVE,
        loading: float = 0.05,
        update_hz: float = 8.0,
        hold_seconds: float = 0.6,
        reselect_deg: float = 8.0,
        gate_when_empty: bool = True,
        sectors: Optional[Sequence[SectorConfig]] = None,   # multiple coverage arcs; overrides ``sector`` when given
        monitor: bool = False,
        output_device: Optional[int] = None,
        record_path: Optional[str] = None,
        post_nr: bool = False,
        post_nr_engine: str = DEFAULT_POST_NR_ENGINE,
        post_nr_floor_db: float = DEFAULT_POST_NR_FLOOR_DB,
        post_nr_oversub: float = DEFAULT_POST_NR_OVERSUB,
        post_nr_amount: float = DEFAULT_POST_NR_AMOUNT,
        post_nr_preserve_level: bool = DEFAULT_POST_NR_PRESERVE_LEVEL,
        peq: bool = False,                      # parametric EQ (tone) on the cleaned mono
        peq_bands: Optional[Sequence[dict]] = None,
        transient_suppress: bool = False,       # duck impulsive table taps / knocks
        voice_gate: bool = False,               # mute non-speech (gaps & noise)
        dereverb: bool = False,
        dereverb_t60: float = DEFAULT_DEREVERB_T60,
        dereverb_beta: float = DEFAULT_DEREVERB_BETA,
        dereverb_gmin_db: float = DEFAULT_DEREVERB_GMIN_DB,
        aec: bool = False,
        aec_n_taps: int = 16,
        aec_mu: float = 0.3,
        aec_ref_device: Optional[int] = None,
        preamp_gain_db: float = 0.0,            # mic-INPUT preamp gain (dB); 0 = no-op
        preamp_auto: bool = False,              # auto headroom stager (analog track)
        calibration: Optional[Any] = None,      # per-capsule gain/polarity/delay align (default OFF)
        calibration_path: Optional[str] = None,  # ...or load that CalibrationProfile from a JSON file
        agc_target_db: Optional[float] = None,  # target output RMS (dBFS); None = AGC off
        config: Any = None,                     # room config — enables the zone cut (needs array bearing + zones)
        array_id: Optional[str] = None,         # which array's zones to honour
        zone_cut: bool = False,                 # cut the door + anyone outside the pickup area
        zone_gain_enabled: bool = False,        # apply the active pickup zone's gain_db live (post-AGC, default OFF)
    ):
        self.geometry = geometry
        # Canonical coverage state is a LIST of sectors. A talker is "in coverage" if it falls
        # inside ANY of them; the gaps between sectors are nulled / muted. ``sector`` (single) is
        # kept for back-compat via the properties below. Rebind this list atomically to change
        # coverage live — never mutate it in place (it's read lock-free by the control tick).
        self._sectors: list = list(sectors) if sectors else [sector]
        self._config = config
        self._array_id = array_id
        self.zone_cut = bool(zone_cut)
        self.zone_gain_enabled = bool(zone_gain_enabled)
        self.off_nadir_deg = off_nadir_deg
        self.max_talkers = max_talkers
        self.grid_step_deg = grid_step_deg
        self.min_separation_deg = min_separation_deg
        self.min_salience_db = min_salience_db
        self.vad_floor_db = vad_floor_db
        self.freq_hz = freq_hz
        self.mode = mode
        self.loading = loading
        self.update_hz = max(1.0, update_hz)
        self.hold_cycles = max(1, int(hold_seconds * update_hz))
        self.reselect_deg = reselect_deg
        self.gate_when_empty = gate_when_empty

        self.ctrl = LiveBeamController(
            geometry,
            device=device,
            samplerate=samplerate,
            monitor=monitor,
            output_device=output_device,
            record_path=record_path,
            track_covariance=True,
            post_nr=post_nr,
            post_nr_engine=post_nr_engine,
            post_nr_floor_db=post_nr_floor_db,
            post_nr_oversub=post_nr_oversub,
            post_nr_amount=post_nr_amount,
            post_nr_preserve_level=post_nr_preserve_level,
            peq=peq,
            peq_bands=peq_bands,
            transient_suppress=transient_suppress,
            voice_gate=voice_gate,
            dereverb=dereverb,
            dereverb_t60=dereverb_t60,
            dereverb_beta=dereverb_beta,
            dereverb_gmin_db=dereverb_gmin_db,
            aec=aec,
            aec_n_taps=aec_n_taps,
            aec_mu=aec_mu,
            aec_ref_device=aec_ref_device,
            preamp_gain_db=preamp_gain_db,
            preamp_auto=preamp_auto,
            calibration=calibration,                # per-capsule align (default OFF); forwarded as-is
            calibration_path=calibration_path,
            agc_target_db=agc_target_db,
            live_zone_gain=bool(zone_gain_enabled),
        )
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._detections: list = []          # last gated detections (for readout)
        self._last_looks: list = []           # held look azimuths
        self._last_sig: Optional[tuple[tuple, tuple]] = None  # quantized (looks, nulls) signature
        self._hold = 0
        self._active_nulls: list = []         # null bearings actually applied this tick (telemetry → map markers)
        self.error = ""

    def set_peq_bands(self, bands: Optional[Sequence[dict]] = None) -> None:
        """Forward live parametric-EQ band changes to the underlying live controller."""
        self.ctrl.set_peq_bands(bands)

    # ---- lifecycle ----
    def start(self) -> None:
        self.ctrl.connect()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="autosteer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self.ctrl.disconnect()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    # ---- coverage sectors (live-settable) ----
    @property
    def sectors(self) -> list:
        """The coverage arcs. A talker inside ANY of them is followed; the gaps are cut. Assign a
        NEW list to change coverage at runtime (read once per control tick) — never mutate in place."""
        return self._sectors

    @sectors.setter
    def sectors(self, value: Optional[Sequence[SectorConfig]]) -> None:
        self._sectors = list(value) if value else []

    @property
    def sector(self) -> SectorConfig:
        """Back-compat: the first coverage sector (or a neutral default if none are set)."""
        return self._sectors[0] if self._sectors else SectorConfig()

    @sector.setter
    def sector(self, value: SectorConfig) -> None:
        self._sectors = [value]

    # ---- readout ----
    def detections(self) -> list:
        """Copy of the latest gated detections (``Detection`` list, in/out flagged)."""
        with self._lock:
            return list(self._detections)

    def read_level(self) -> float:
        return self.ctrl.read_level()

    @property
    def active_nulls(self) -> list:
        """The null bearings (array-relative deg) the auto-steer zone-cut is **actually** applying this
        tick — the door + anyone outside the pickup area. Empty when not zone-cutting or while merely
        holding. Drives the live room-map null markers (Feature D)."""
        with self._lock:
            return list(self._active_nulls)

    @property
    def aec_erle_db(self) -> float:
        """Live AEC echo-return-loss-enhancement (dB); 0 when AEC is off or no echo seen."""
        return self.ctrl.aec_erle_db

    @property
    def stage_activity(self) -> Any:
        """Lock-free snapshot of what each cleaning stage did on the last block (for the live per-stage
        meter strip), forwarded from the wrapped controller."""
        return self.ctrl.stage_activity

    def set_bypass(self, on: bool) -> None:
        """Monitor the RAW (pre-cleaning) beam — a one-click A/B of the whole cleaning chain. Forwarded
        to the wrapped controller; the chain still runs (meters keep updating)."""
        self.ctrl.set_bypass(on)

    def start_ab_capture(self, seconds: float = 8.0):
        """Arm an A/B proof capture on the wrapped controller (raw beam vs cleaned)."""
        return self.ctrl.start_ab_capture(seconds)

    @property
    def ab_capture(self):
        return self.ctrl.ab_capture

    def active_cleaning_stages(self) -> str:
        return self.ctrl.active_cleaning_stages()

    @property
    def estimated_latency_ms(self) -> float:
        return self.ctrl.estimated_latency_ms

    # ---- control loop ----
    def _loop(self) -> None:  # pragma: no cover (timing/thread)
        period = 1.0 / self.update_hz
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # keep the thread alive; surface the error
                self.error = str(exc)
            self._stop.wait(period)

    def _quantize(self, azimuths) -> tuple:
        """Deadband: snap azimuths to the re-select grid so tiny jitter doesn't
        force a redesign."""
        q = sorted({round(az / self.reselect_deg) for az in azimuths})
        return tuple(q)

    def _tick(self) -> None:
        cov, freqs = self.ctrl.snapshot_covariance()
        if cov is None:
            return
        res = doa.detect(
            cov, freqs, self.geometry,
            off_nadir_deg=self.off_nadir_deg,
            grid_step_deg=self.grid_step_deg,
            max_talkers=self.max_talkers,
            min_separation_deg=self.min_separation_deg,
            min_salience_db=self.min_salience_db,
            vad_floor_db=self.vad_floor_db,
        )
        secs = self._sectors                       # read the rebindable list ONCE (consistent snapshot)
        specs = [(s.center_deg, s.half_width_deg, s.front_offset_deg) for s in secs]
        doa.sector_gate_multi(res.detections, specs)
        with self._lock:
            self._detections = res.detections

        in_az = [d.azimuth_deg for d in res.detections if d.in_sector]
        out_az = [d.azimuth_deg for d in res.detections if not d.in_sector]

        # zone cut: keep only looks inside a pickup zone; null the door + anyone outside the pickup area
        if self.zone_cut and self._config is not None and self._array_id:
            in_az, out_az = _apply_zone_cut(self._config, self._array_id, in_az, out_az)

        # per-zone gain: apply the active pickup zone's gain_db to the output (post-AGC, opt-in)
        if self.zone_gain_enabled and self._config is not None and self._array_id:
            try:
                from conf_pipeline.seat_mapper import active_zone_gain_db as _azgd
                az_for_gain = in_az[0] if in_az else None
                if az_for_gain is not None:
                    g = _azgd(self._config, self._array_id, az_for_gain)
                    self.ctrl.set_zone_gain_lin(10 ** (g / 20.0) if g is not None else None)
                else:
                    self.ctrl.set_zone_gain_lin(None)
            except Exception:
                pass   # realtime-safe: never throw into the control tick

        # hysteresis: hold the last look set briefly when detections drop out
        if in_az:
            self._hold = self.hold_cycles
            looks = in_az
        elif self._hold > 0:
            self._hold -= 1
            looks = self._last_looks
            out_az = []                        # don't null while merely holding
        else:
            looks = []

        sig = (self._quantize(looks), self._quantize(out_az))
        if sig == self._last_sig:
            return                             # nothing meaningful changed
        self._last_sig = sig
        self._last_looks = looks

        if looks:
            design = design_multi_bearings(
                self.geometry,
                [(az, self.off_nadir_deg) for az in looks],
                [(az, self.off_nadir_deg) for az in out_az],
                freq_hz=self.freq_hz,
                mode=self.mode,
                loading=self.loading,
                array_id="POLARIS",
                bands=(),   # hot loop: skip band verification — the live runtime
                            # re-derives the weights per FFT bin anyway
            )
            self.ctrl.apply_design(design)
            if self.gate_when_empty:
                self.ctrl.set_mute(False)
        elif self.gate_when_empty:
            self.ctrl.set_mute(True)           # nobody in the area → silence
        with self._lock:                       # telemetry: the nulls actually applied (door + out-of-zone)
            self._active_nulls = list(out_az) if looks else []
