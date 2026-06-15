"""Run the POLARIS live delay-and-sum beam + dominant-talker DOA demo.

Thin wrapper around :func:`conf_pipeline_control.polaris_beamformer._demo` (the
same code is the module's own ``__main__``). Lists input devices when run without
``--device``; otherwise streams the beam and prints the live DOA.

    pip install -e ".[control]"          # numpy + sounddevice
    python scripts/polaris_beam_demo.py                 # list devices
    python scripts/polaris_beam_demo.py --device 7 --monitor   # run (HEADPHONES)
    python scripts/polaris_beam_demo.py --device 7 --dead 5     # real board: mask dead capsule 5
"""
from conf_pipeline_control.polaris_beamformer import _demo

if __name__ == "__main__":
    raise SystemExit(_demo())
