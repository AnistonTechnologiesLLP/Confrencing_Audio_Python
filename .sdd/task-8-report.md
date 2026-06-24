# Task 8 Report — Docs (README + CHANGELOG)

## Status: DONE

## Commit hash
TBD — commit pending (see below)

## What changed per file

- **CHANGELOG.md** — Added a new `### Added` subsection under `## [Unreleased]` (inserted before the existing `### Fixed`), containing the verbatim RTF-MVDR entry from the brief.

- **README.md** — Two changes:
  1. In the "Real-time array beamforming (1.17.0)" section, updated the beam-mode list from "Four selectable strategies" to "Five selectable strategies" and appended `rtf_mvdr` with a one-line description (GEVD/max-SNR, real-reverb/per-capsule capture, fallback to plane-wave MVDR on warmup/cross-check failure, opt-in, PolarisBeamformer / A-B-engine path only).
  2. In the "IntelliMix ↔ this pipeline" comparison table, added a new row — "Data-estimated steering (real-room covariance)" — describing `rtf_mvdr` as the data-driven steering option with accurate scope constraints.

## Concerns

None. No dB figure was invented (the brief specified not to); no multi-sector or auto-steer support was claimed. All accuracy constraints honoured: opt-in, default-off, PolarisBeamformer/A-B-engine only, SRP-PHAT still does DOA/gating/cross-check, fallback described. The CHANGELOG entry is verbatim from the brief.
