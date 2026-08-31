#!/usr/bin/env python3
"""
DS345 AWG GUI - panel control for a Stanford Research Systems DS345
synthesized function / arbitrary waveform generator, over GPIB.

The DS345 sibling of BK4063B AWG GUI, and the mirror image of Scope Grab:
instead of pulling a capture off an instrument, this pushes a setup onto one.
Edit the panel, press Apply, and the generator follows; press Save setup and
the whole instrument state lands in a timestamped JSON you can recall later or
keep with the data it produced.

One thing is fundamentally different from the 4063B: the DS345 has no output
switch. The BNC is always driving whatever is connected, at whatever the
current settings say. The MUTE button here is the nearest thing this
instrument offers - it sets 0 Vpp and 0 V offset (which the manual defines as
a DC-only output), remembering what it displaced so LIVE can put it back.
Apply therefore always changes what is being output, and by default it asks
first.

Requires: NI-488.2 (or any VISA with GPIB) + `pip install pyvisa numpy
          matplotlib` (matplotlib only draws the waveform preview - everything
          else works without it)
Run with:  pythonw ds345_awg_gui.py    (pythonw = no console window)
"""

import datetime
import json
import os
import queue
import re
import struct
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np
import pyvisa

try:
    import matplotlib
    matplotlib.use("TkAgg")
    matplotlib.rcParams["font.size"] = 8
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.figure import Figure
except ImportError:                       # everything but the preview still works
    Figure = None

# Where the generator was last found, so later sessions connect without the
# minute-long walk of the whole bus. A typed address always wins over this.
CONFIG_PATH = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                           "DS345-AWG-GUI", "config.json")

# A copy of every waveform this app uploads, kept because the generator cannot
# read its waveform back out - the DS345 holds exactly one arbitrary waveform,
# anonymously, and LDWF? only goes one way. Without the copy, what is playing
# can never be drawn again. It lives beside the program rather than in
# %APPDATA% so the samples are somewhere you can actually get at them, which
# also makes the folder work in both directions - drop a .csv in here and
# the local list and the sequence builder can use it.
WAVE_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Waveforms")

# One output, so one colour - the same blue CH1 wears in the sibling tools.
OUT_COLOUR = "#1f77b4"


def _lighten(colour, amount):
    """A paler version of a hex colour, mixed towards white."""
    red, green, blue = (int(colour[i:i + 2], 16) for i in (1, 3, 5))
    mix = [int(round(c + (255 - c) * amount)) for c in (red, green, blue)]
    return "#%02x%02x%02x" % tuple(mix)


# The pending trace is aimed at the output, but not on it yet: a paler shade
# of the output's colour says both at once.
PENDING_COLOUR = _lighten(OUT_COLOUR, 0.42)

# The output button, coloured by what the BNC is actually doing. Red is
# driving. There is no OFF on this instrument, so the green state is MUTED -
# 0 Vpp and 0 V offset, the quietest the DS345 can be told to sit. An
# amplitude of zero with an offset that is not is a deliberate DC source, and
# gets its own colour rather than pretending to be either.
OUT_LOOK = {
    "LIVE":  ("#c62828", "LIVE"),
    "MUTED": ("#2e7d32", "MUTED"),
    "DC":    ("#c68000", "DC"),
    None:    ("#8a8a8a", "?"),
}

# A value the panel worked out rather than one that was set. Light enough to
# read black text through, since it sits behind an entry box.
COMPUTED_BG = "#fdf3b4"

# The functions the FUNC command offers, in its own 0..5 order.
FUNCS = ("SINE", "SQUARE", "TRIANGLE", "RAMP", "NOISE", "ARB")
FUNC_CODE = {name: i for i, name in enumerate(FUNCS)}

# AMPL takes a value plus one of these stuck to it: 1.5VP, 0.5VR, 3DB.
# For ARB the manual allows Vpp only, so the unit box is pinned there.
AMPL_UNITS = ("VP", "VR", "DB")

# Functions that have a frequency for FREQ to set. NOISE errors on FREQ, and
# an arb's timing is FSMP over its point count - FREQ does not touch it.
OSCILLATING = {"SINE", "SQUARE", "TRIANGLE", "RAMP"}

# Frequency ceilings per function, from the specifications page. The
# generator enforces these itself; the panel knows them so the preview's
# warnings can say which limit is about to be hit rather than waiting for
# the instrument to refuse.
FREQ_MAX = {"SINE": 30.2e6, "SQUARE": 30.2e6, "TRIANGLE": 100e3, "RAMP": 100e3}

# The arbitrary waveform: 12-bit DAC, -2047..+2047, played one point per
# 1/FSMP with FSMP = 40 MHz / N. Both ends of the length are the manual's.
DAC_FULL_SCALE = 2047
MAX_ARB_POINTS = 16300
MIN_ARB_POINTS = 8
FSMP_MAX = 40e6

# (panel key, label, functions it applies to). The panel shows them all and
# greys out the ones the selected function has no use for, so the layout does
# not jump around when the function changes. PERI is panel arithmetic - the
# generator has no period command, but which of the two you would rather type
# depends on what you are setting up, so both boxes exist and either drives
# the other. Only FREQ is ever sent.
OUT_PARAMS = [
    ("FREQ", "Freq (Hz)",   OSCILLATING),
    ("AMPL", "Ampl",        set(FUNCS)),
    ("OFFS", "Offset (V)",  set(FUNCS)),
    ("PERI", "Period (s)",  OSCILLATING),
    ("PHSE", "Phase (deg)", OSCILLATING),
    ("FSMP", "Sa/s",        {"ARB"}),
]

# Modulation waveform (MDWF) and trigger source (TSRC) go over the wire as
# integers; the panel speaks words. Two-way tables, with the wire codes as
# strings because that is how they come back from a query.
MDWF_NAMES = {"0": "SINGLE", "1": "RAMP", "2": "TRIANGLE", "3": "SINE",
              "4": "SQUARE", "5": "ARB", "6": "NONE"}
MDWF_CODES = {name: code for code, name in MDWF_NAMES.items()}
TSRC_NAMES = {"0": "SINGLE", "1": "INT", "2": "EXT+", "3": "EXT-", "4": "LINE"}
TSRC_CODES = {name: code for code, name in TSRC_NAMES.items()}
TSRC_CHOICES = ("SINGLE", "INT", "EXT+", "EXT-", "LINE")

# Internal modulation shapes for AM/FM/PM. The instrument would also take
# ARB here, but that needs a pattern downloaded with AMOD? first, which this
# panel does not do yet - offering the word without the pattern would just
# manufacture an error at MENA time.
MOD_SHAPES = ("SINE", "SQUARE", "TRIANGLE", "RAMP")
# A sweep's "shape" is what the swept frequency does with time.
SWEEP_SHAPES = ("SINGLE", "RAMP", "TRIANGLE")

# Modulation, sweep and burst are one panel row because the instrument treats
# them as one setting: MTYP holds exactly one of them and MENA is a single
# switch. Each entry is (label, command, choices or None); choices make the
# cell a fixed dropdown.
MODES = ("Off", "AM", "FM", "PM", "Lin sweep", "Log sweep", "Burst")
MTYP_OF = {"Lin sweep": "0", "Log sweep": "1", "AM": "2", "FM": "3",
           "PM": "4", "Burst": "5"}
MODE_OF_MTYP = {code: mode for mode, code in MTYP_OF.items()}

_SWEEP_PARAMS = [("Start (Hz)", "STFR", None), ("Stop (Hz)", "SPFR", None),
                 ("Rate (Hz)", "RATE", None), ("Shape", "MDWF", SWEEP_SHAPES),
                 ("Trigger", "TSRC", TSRC_CHOICES),
                 ("Trig rate (Hz)", "TRAT", None)]
MODE_PARAMS = {
    "Off":       [],
    "AM":        [("Depth (%)", "DPTH", None), ("Rate (Hz)", "RATE", None),
                  ("Shape", "MDWF", MOD_SHAPES)],
    "FM":        [("Span (Hz)", "FDEV", None), ("Rate (Hz)", "RATE", None),
                  ("Shape", "MDWF", MOD_SHAPES)],
    "PM":        [("Span (deg)", "PDEV", None), ("Rate (Hz)", "RATE", None),
                  ("Shape", "MDWF", MOD_SHAPES)],
    "Lin sweep": list(_SWEEP_PARAMS),
    "Log sweep": list(_SWEEP_PARAMS),
    "Burst":     [("Cycles", "BCNT", None), ("Trigger", "TSRC", TSRC_CHOICES),
                  ("Trig rate (Hz)", "TRAT", None)],
}
MODE_SLOTS = max(len(p) for p in MODE_PARAMS.values())

# Standard event status register bits worth reporting after a write. Bit 6
# (URQ) is deliberately absent: it latches on any front-panel key press, so
# somebody touching the instrument between two commands would otherwise read
# as our command failing.
ESR_BITS = {5: "command error", 4: "execution error", 2: "query error"}

BAD_NAME_CHARS = r'<>:"/\|?*'


# ---------------------------------------------------------------------------
# Waveform library
#
# Every shape is a pure function of a point count and a parameter lookup, so it
# can be built, previewed and tested without an instrument attached. Envelopes
# come out unipolar (0..1) because that is what an intensity control wants; the
# oscillating shapes come out bipolar (-1..+1). Either way the numbers are a
# shape, and the volts come from Ampl/Offset on the output.
#
# Shared verbatim with the 4063B tool, so a record built in one previews the
# same in the other - the generators differ only in how the numbers get to
# them and how many bits survive.
# ---------------------------------------------------------------------------

ENV_CHOICES = ("None", "Blackman-Harris", "Gaussian", "Hann", "Tukey")


class _Params:
    """Panel strings read as numbers, with a fallback when a box is empty."""

    def __init__(self, values):
        self.values = values

    def num(self, key, default):
        try:
            text = str(self.values.get(key, "")).strip()
            return float(text) if text else default
        except ValueError:
            return default

    def txt(self, key, default=""):
        return str(self.values.get(key, "")).strip() or default

    def tones(self, key, default=(10.0,)):
        out = []
        for token in str(self.values.get(key, "")).replace(";", ",").split(","):
            token = token.strip()
            if token:
                try:
                    out.append(float(token))
                except ValueError:
                    pass
        return out or list(default)


def _unit(n):
    """0 .. 1 across the record."""
    return np.linspace(0.0, 1.0, n)


def _centred(n):
    """-1 .. +1 across the record."""
    return np.linspace(-1.0, 1.0, n)


def _gaussian(n, trunc):
    """Truncated Gaussian. trunc is the half-width of the record in sigma, so 3
    puts the ends at exp(-4.5) ~ 1% rather than cutting a visible step."""
    return np.exp(-0.5 * (_centred(n) * max(trunc, 1e-6)) ** 2)


def _tukey(n, flat):
    """Flat top with raised-cosine shoulders. flat=0 is a Hann, flat=1 a square."""
    flat = min(max(flat, 0.0), 1.0)
    x = _unit(n)
    taper = (1.0 - flat) / 2.0
    w = np.ones(n)
    if taper > 0:
        left, right = x < taper, x > 1.0 - taper
        w[left] = 0.5 * (1 - np.cos(np.pi * x[left] / taper))
        w[right] = 0.5 * (1 - np.cos(np.pi * (1.0 - x[right]) / taper))
    return w


def _trapezoid(n, rise, fall):
    x = _unit(n)
    rise, fall = max(rise, 0.0), max(fall, 0.0)
    if rise + fall > 1.0:                       # keep a sane shape if over-specified
        rise, fall = rise / (rise + fall), fall / (rise + fall)
    w = np.ones(n)
    if rise > 0:
        m = x < rise
        w[m] = x[m] / rise
    if fall > 0:
        m = x > 1.0 - fall
        w[m] = (1.0 - x[m]) / fall
    return w


def _tanh_top(n, edge, flat):
    """Flat top with tanh shoulders - a smooth switch-on with no corner, which is
    what an AOM or EOM intensity ramp usually wants."""
    x = _centred(n)
    a = min(max(flat, 0.0), 1.0)
    w = max(edge, 1e-4)
    y = 0.5 * (np.tanh((x + a) / w) - np.tanh((x - a) / w))
    peak = float(y.max())
    return y / peak if peak > 0 else y


def _envelope(name, n, trunc=3.0, flat=0.5):
    if name in ("Blackman-Harris", "Blackman"):
        return _blackman_harris(n)
    if name == "Gaussian":
        return _gaussian(n, trunc)
    if name == "Hann":
        return np.hanning(n)
    if name == "Tukey":
        return _tukey(n, flat)
    return np.ones(n)


def _normalise(y):
    peak = float(np.max(np.abs(y)))
    return y / peak if peak > 0 else y


def _build_gaussian(n, p):
    return _gaussian(n, p.num("trunc", 3.0))


def _blackman_harris(n):
    """The four-term Blackman-Harris window.

    Sidelobes at about -92 dB where the classic three-term Blackman manages
    -58, which is the whole reason to reach for a window here: the sidelobes
    are what drives the line you are trying not to drive.
    """
    x = 2 * np.pi * _unit(n)
    return (0.35875 - 0.48829 * np.cos(x) + 0.14128 * np.cos(2 * x)
            - 0.01168 * np.cos(3 * x))


def _build_blackman(n, p):
    return _blackman_harris(n)


def _build_hann(n, p):
    return np.hanning(n)


def _build_tukey(n, p):
    return _tukey(n, p.num("flat", 0.5))


def _build_sech(n, p):
    """Hyperbolic secant - the amplitude profile for adiabatic rapid passage,
    and analytically solvable as the Rosen-Zener model."""
    return 1.0 / np.cosh(_centred(n) * max(p.num("trunc", 4.0), 1e-6))


def _build_sinc(n, p):
    """Bipolar. A sinc in time is a rectangle in frequency, so this is the
    starting point for a flat-topped spectral profile."""
    return np.sinc(_centred(n) * max(p.num("lobes", 4.0), 1e-6))


def _build_square(n, p):
    width = min(max(p.num("width", 0.5), 0.0), 1.0)
    return (np.abs(_centred(n)) <= width).astype(float)


def _build_trapezoid(n, p):
    return _trapezoid(n, p.num("rise", 0.1), p.num("fall", 0.1))


def _build_tanh_top(n, p):
    return _tanh_top(n, p.num("edge", 0.1), p.num("flat", 0.6))


def _build_dc(n, p):
    """A flat level. On its own it is the hold between two ramps; with a
    carrier it is a rectangular-envelope burst."""
    return np.full(n, p.num("level", 1.0))


def _build_linear(n, p):
    start, end = p.num("start", 0.0), p.num("end", 1.0)
    return start + (end - start) * _unit(n)


def _build_exp(n, p):
    """Exponential approach from start to end. The usual evaporative-cooling
    ramp is start 1, end 0, with tau setting how hard the knee is."""
    start, end = p.num("start", 1.0), p.num("end", 0.0)
    tau = max(p.num("tau", 0.3), 1e-4)
    t = _unit(n)
    k = (1.0 - np.exp(-t / tau)) / (1.0 - np.exp(-1.0 / tau))
    return start + (end - start) * k


def _build_smoothstep(n, p):
    """Minimum-jerk ramp: zero slope and zero curvature at both ends, which is
    what keeps a transport or a trap handover adiabatic."""
    start, end = p.num("start", 0.0), p.num("end", 1.0)
    t = _unit(n)
    return start + (end - start) * (t ** 3) * (10 - 15 * t + 6 * t * t)


def _build_chirp(n, p):
    """Linear frequency sweep across the record, in cycles. Pair a chirp with a
    sech envelope for adiabatic rapid passage."""
    c0, c1 = p.num("c0", 10.0), p.num("c1", 100.0)
    t = _unit(n)
    y = np.sin(2 * np.pi * (c0 * t + 0.5 * (c1 - c0) * t * t))
    return y * _envelope(p.txt("env", "None"), n)


def _build_multitone(n, p):
    """Sum of sines, each given as a whole number of cycles across the record so
    every tone closes cleanly when the waveform repeats."""
    t = _unit(n)
    y = np.zeros(n)
    for cycles in p.tones("tones"):
        y += np.sin(2 * np.pi * cycles * t)
    return _normalise(y) * _envelope(p.txt("env", "None"), n)


def _build_dgauss(n, p):
    """Derivative of a Gaussian: the quadrature half of a DRAG pulse."""
    x = _centred(n)
    trunc = max(p.num("trunc", 3.0), 1e-6)
    y = -x * trunc ** 2 * np.exp(-0.5 * (x * trunc) ** 2)
    return _normalise(y) * p.num("beta", 1.0)


# name -> (builder, [(label, key, default, choices or None)], takes a carrier)
_CARRIER = [("Carrier cycles", "cycles", "0", None),
            ("Carrier phase (deg)", "cphase", "0", None)]

BUILD_SHAPES = {
    "Gaussian":        (_build_gaussian,
                        [("Truncate (+/-sigma)", "trunc", "3", None)] + _CARRIER),
    "Blackman-Harris": (_build_blackman, list(_CARRIER)),
    "Hann":            (_build_hann, list(_CARRIER)),
    "Tukey flat-top":  (_build_tukey,
                        [("Flat fraction", "flat", "0.5", None)] + _CARRIER),
    "Sech (ARP)":      (_build_sech,
                        [("Truncate (+/-units)", "trunc", "4", None)] + _CARRIER),
    "Sinc":            (_build_sinc,
                        [("Zero crossings", "lobes", "4", None)] + _CARRIER),
    "Square pulse":    (_build_square,
                        [("Width fraction", "width", "0.5", None)] + _CARRIER),
    "Trapezoid":       (_build_trapezoid,
                        [("Rise fraction", "rise", "0.1", None),
                         ("Fall fraction", "fall", "0.1", None)] + _CARRIER),
    "Tanh flat-top":   (_build_tanh_top,
                        [("Edge fraction", "edge", "0.1", None),
                         ("Flat fraction", "flat", "0.6", None)] + _CARRIER),
    "Hold (DC)":       (_build_dc,
                        [("Level", "level", "1", None)] + _CARRIER),
    "Linear ramp":     (_build_linear,
                        [("Start", "start", "0", None),
                         ("End", "end", "1", None)] + _CARRIER),
    "Exponential ramp": (_build_exp,
                         [("Start", "start", "1", None), ("End", "end", "0", None),
                          ("Time constant", "tau", "0.3", None)] + _CARRIER),
    "Smoothstep ramp": (_build_smoothstep,
                        [("Start", "start", "0", None),
                         ("End", "end", "1", None)] + _CARRIER),
    "Chirp":           (_build_chirp,
                        [("Start cycles", "c0", "10", None),
                         ("End cycles", "c1", "100", None),
                         ("Envelope", "env", "Blackman-Harris", ENV_CHOICES)]),
    "Multitone":       (_build_multitone,
                        [("Cycles (comma list)", "tones", "10, 20, 35", None),
                         ("Envelope", "env", "None", ENV_CHOICES)]),
    "Gaussian deriv":  (_build_dgauss,
                        [("Truncate (+/-sigma)", "trunc", "3", None),
                         ("Beta", "beta", "1", None)]),
}
BUILD_SLOTS = max(len(spec) for _, spec in BUILD_SHAPES.values())


def build_waveform(shape, n_points, values):
    """Make the samples for one shape. Pure - no instrument, no widgets."""
    if shape not in BUILD_SHAPES:
        raise ValueError(f"unknown shape {shape!r}")
    n = int(n_points)
    if n < 2:
        raise ValueError("need at least 2 points")
    builder, _ = BUILD_SHAPES[shape]
    p = _Params(values)
    y = np.asarray(builder(n, p), dtype=np.float64)

    cycles = p.num("cycles", 0.0)
    if cycles > 0:
        # An envelope times a carrier: the shape becomes the burst outline and
        # the carrier fills it, which is how a Raman or Rabi pulse is specified.
        phase = np.deg2rad(p.num("cphase", 0.0))
        y = y * np.sin(2 * np.pi * cycles * _unit(n) + phase)
    return y


def build_warnings(shape, n_points, values, rate=0.0, carrier_hz=None):
    """Ways a record of `n_points` is too coarse for what is being asked of it.

    A built record is a list of numbers with no time in it until a clock is
    picked, so most of this is about the point count alone: how many points
    each carrier cycle gets, and whether a shape's own detail survives. `rate`
    only matters where the answer is in hertz - the carrier against Nyquist,
    and what the record comes to in seconds.
    """
    lines = []
    n = int(n_points) if n_points else 0
    if n < 2:
        return ["a record needs at least 2 points"]
    p = _Params(values)

    if n > MAX_ARB_POINTS:
        lines.append(f"{n:,} points will not fit - the DS345 tops out at "
                     f"{MAX_ARB_POINTS:,}")

    cycles = p.num("cycles", 0.0)
    if cycles > 0:
        per_cycle = n / cycles
        if per_cycle < ALIAS_LIMIT:
            lines.append(
                f"{cycles:g} carrier cycles across {n:,} points is "
                f"{per_cycle:.2g} points each - below two the carrier is "
                f"aliased, and what comes out is not the tone you asked for")
        elif per_cycle < DENSITY_LIMITS["cycle"]:
            lines.append(
                f"{cycles:g} carrier cycles across {n:,} points is "
                f"{per_cycle:.2g} points each - the carrier is only just "
                f"resolved, so the peaks will read low and the shape will be "
                f"visibly stepped")
        if rate > 0 and carrier_hz and carrier_hz > rate / 2.0:
            lines.append(
                f"a {_hz(carrier_hz)} carrier is above the Nyquist limit of "
                f"{_hz(rate / 2.0)} for {_hz(rate)} - it will come out as a "
                f"lower frequency, not as itself")

    # A shape's own detail is a fraction of the record, so it is the point
    # count that decides whether it survives - the clock never enters into it.
    for key, name, floor in (("rise", "rise", 0.0), ("fall", "fall", 0.0),
                             ("edge", "edge", 0.0), ("flat", "flat top", 0.0),
                             ("width", "width", 0.0)):
        fraction = p.num(key, floor)
        if 0 < fraction < 1 and fraction * n < 8:
            lines.append(f"the {name} is {fraction:g} of the record, which is "
                         f"{fraction * n:.2g} points - too few to shape it")
    return lines


# A sequence is built in real time against a sample clock, not in fractions of
# a record: the whole point is that its segments differ in length, and "2 us at
# 5 MHz" is how a pulse is specified at the bench. That makes the clock part of
# the specification rather than something chosen afterwards.
# How many points will go into the Type/paste box. A Tk text widget holding a
# line per sample is fine at ten thousand and unusable at a million, and the
# box is for reading and nudging a record rather than for holding a long one.
PASTE_MAX_LINES = 20000

SEQ_LOCAL = "Local waveform"
SEQ_UNITS = ("ns", "us", "ms", "s")
_TIME_UNITS = {"s": 1.0, "ms": 1e-3, "m": 1e-3, "us": 1e-6, "u": 1e-6,
               "ns": 1e-9, "n": 1e-9}

# (label, key, default, entry width). One row of these per segment.
SEQ_COLUMNS = [("Shape", "shape", "Blackman-Harris", 16),
               ("Time", "time", "1", 8),
               ("Ampl", "ampl", "1", 6),
               ("Carrier (Hz)", "freq", "0", 10),
               ("Phase (deg)", "phase", "0", 8),
               ("Gap after", "gap", "0", 8),
               ("Extra (key=value)", "extra", "", 20)]


def shape_extras(shape):
    """A shape's own parameters as `key=value`, the carrier aside.

    The builder gives each of them a labelled box. A sequence row has one free
    text field instead, so the defaults are written into it when the shape is
    picked - otherwise they are reachable but invisible, and you would have to
    already know that a Tukey takes `flat=` to discover that it does.
    """
    if shape == SEQ_LOCAL:
        return "name="
    if shape not in BUILD_SHAPES:
        return ""
    return " ".join(f"{key}={default}"
                    for _, key, default, _ in BUILD_SHAPES[shape][1]
                    if key not in ("cycles", "cphase"))


SEQ_DEFAULT = {key: default for _, key, default, _ in SEQ_COLUMNS}
SEQ_DEFAULT["extra"] = shape_extras(SEQ_DEFAULT["shape"])

# Settings a spec carries besides its segments. Without them a pasted sequence
# is only half of itself: the same rows at the wrong clock are a different
# waveform, and there is nothing in a row that says which.
SEQ_SETTINGS = ("rate", "unit", "baseline", "coherent", "clock")


def _as_bool(text, default=False):
    value = str(text or "").strip().lower()
    if value in ("on", "yes", "true", "1"):
        return True
    if value in ("off", "no", "false", "0"):
        return False
    return default


def _hz(value):
    for scale, suffix in ((1e6, " MHz"), (1e3, " kHz"), (1.0, " Hz")):
        if abs(value) >= scale:
            return f"{value / scale:.4g}{suffix}"
    return f"{value:.4g} Hz"


def _secs(value):
    for scale, suffix in ((1.0, " s"), (1e-3, " ms"), (1e-6, " us"),
                          (1e-9, " ns")):
        if abs(value) >= scale:
            return f"{value / scale:.4g}{suffix}"
    return f"{value * 1e12:.4g} ps"


def parse_time(text, unit="us", default=0.0):
    """Seconds from '2', '2u', '2us', '1.5ms', '200m' or '2e-6s'.

    A bare number is in `unit`, the sequence's own setting, so microsecond
    pulses are typed as 2 rather than 0.000002 - and a 200 ms hold in the
    middle of that same sequence is still typed as 200m rather than as 200000.
    """
    raw = str(text if text is not None else "").strip().lower()
    raw = raw.replace("µ", "u").replace("sec", "s")
    if not raw:
        return default
    match = re.fullmatch(r"([+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?)\s*([a-z]*)",
                         raw)
    if not match:
        raise ValueError(f"cannot read {text!r} as a time")
    value, suffix = float(match.group(1)), match.group(2) or unit
    if suffix not in _TIME_UNITS:
        raise ValueError(f"unknown time unit {suffix!r} in {text!r}")
    return value * _TIME_UNITS[suffix]


def parse_kv(text):
    """'trunc=3 flat=0.6' -> {'trunc': '3', 'flat': '0.6'}.

    Split on whitespace and semicolons but never on commas, so a value that is
    itself a list - Multitone's 'tones=10,20,35' - survives intact.
    """
    out = {}
    for token in re.split(r"[;\s]+", str(text or "").strip()):
        if not token:
            continue
        key, sep, value = token.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"extra parameter {token!r} should be key=value")
        out[key.strip()] = value.strip()
    return out


def _as_float(text, default=0.0):
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return default


def seq_shapes():
    """Every shape a segment can be, in the order the dropdown offers them."""
    return list(BUILD_SHAPES) + [SEQ_LOCAL]


# Shapes that have been renamed. Kept so a sequence spec written for the
# sibling 4063B tool still reads here without editing.
SHAPE_ALIASES = {"blackman": "Blackman-Harris"}


def resolve_shape(name):
    """Match a typed shape name case-insensitively, or say what is on offer."""
    text = str(name or "").strip()
    if text.lower() in SHAPE_ALIASES:
        return SHAPE_ALIASES[text.lower()]
    for candidate in seq_shapes():
        if candidate.lower() == text.lower():
            return candidate
    raise ValueError(f"unknown shape {text!r} - one of: "
                     + ", ".join(seq_shapes()))


def _carrier(n, cycles, phase):
    """`cycles` cycles across n samples, sampled half-open.

    Not `_unit(n)`, which runs 0..1 inclusive and so spends n samples covering
    n-1 intervals: a segment built that way carries its frequency about
    n/(n-1) high and hands the next segment a phase one sample out. Fine for a
    record that loops on itself, wrong for one that is butted against another,
    which is the whole of what a sequence is.

    The shape keeps the inclusive convention - a ramp typed 0 to 1 should
    reach 1 - because a shape's endpoints are specified where a carrier's are
    merely where the window fell.
    """
    return np.sin(2 * np.pi * cycles * (np.arange(n) / n) + np.deg2rad(phase))


def _segment_samples(shape, n, extra, cycles, phase, waves):
    """One segment's samples, unit amplitude."""
    for key in ("cycles", "cphase"):
        if key in extra:
            raise ValueError(
                f"put the carrier in the Carrier (Hz) and Phase columns, not "
                f"in {key}= - a sequence knows how long the segment is, so it "
                f"can work in hertz where the builder has to work in cycles")
    if shape != SEQ_LOCAL:
        y = build_waveform(shape, n, extra)
        return y * _carrier(n, cycles, phase) if cycles > 0 else y
    name = str(extra.get("name", "")).strip()
    src = (waves or {}).get(name)
    if src is None:
        known = ", ".join(sorted(waves or {})) or "nothing loaded"
        raise ValueError(f"no waveform called {name!r} - give the segment "
                         f"'name=<waveform>', one of: {known}")
    src = np.asarray(src, dtype=np.float64).ravel()
    if src.size < 2:
        raise ValueError(f"{name!r} has too few samples to use")
    # Stretched or squeezed into whatever time the sequence gives it, so one
    # stored record can appear at two lengths in the same sequence.
    y = np.interp(np.linspace(0.0, src.size - 1.0, n), np.arange(src.size), src)
    return y * _carrier(n, cycles, phase) if cycles > 0 else y


def build_sequence(segments, rate, unit="us", baseline=0.0, coherent=False,
                   waves=None):
    """Concatenate differently-specified segments into one record.

    Segments that need not resemble each other, laid end to end: a
    Blackman-Harris at 5 MHz, a gap, the same envelope at half the amplitude
    and ninety degrees, a slow ramp, a hold, a ramp back down. A plain train of
    identical pulses is the case where every row is the same.
    Each segment carries its own shape, duration, amplitude, carrier frequency
    and phase, and its own gap to whatever follows.

    `rate` is the sample clock the record is meant to be played at, in Sa/s -
    it is what turns a duration in seconds into a point count and a carrier in
    hertz into cycles across the segment, so a sequence is only true at the
    rate it was built for. On the DS345 that clock is FSMP.

    `coherent` references every carrier to the start of the sequence rather
    than to its own segment, which is what makes the second pulse of a Ramsey
    pair arrive with a defined phase relative to the first. Off, the typed
    phase is exactly the phase the segment starts at.

    Returns (samples, [(index, shape, points, seconds, gap seconds), ...]).
    """
    rate = _as_float(rate, 0.0)
    if rate <= 0:
        raise ValueError("sample rate must be a positive number of Sa/s")

    pieces, rows, elapsed = [], [], 0.0
    for index, seg in enumerate(segments, 1):
        if not str(seg.get("shape", "")).strip():
            continue
        shape = resolve_shape(seg.get("shape"))
        span = parse_time(seg.get("time"), unit, 0.0)
        gap = parse_time(seg.get("gap"), unit, 0.0)
        if span < 0 or gap < 0:
            raise ValueError(f"segment {index} ({shape}) has a negative time")
        n = int(round(span * rate))
        if n < 2:
            raise ValueError(
                f"segment {index} ({shape}) comes to {n} point(s) at "
                f"{rate:.10g} Sa/s - give it a longer time or a faster clock")
        freq = _as_float(seg.get("freq"), 0.0)
        phase = _as_float(seg.get("phase"), 0.0)
        if coherent:
            # As though a reference oscillator at this segment's frequency had
            # been running since the sequence began.
            phase += 360.0 * freq * elapsed
        y = _segment_samples(shape, n, parse_kv(seg.get("extra")),
                             freq * span, phase, waves)
        pieces.append(np.asarray(y, dtype=np.float64) * _as_float(
            seg.get("ampl"), 1.0))
        rows.append((index, shape, n, span, gap))
        elapsed += span

        gap_n = int(round(gap * rate))
        if gap_n > 0:
            pieces.append(np.full(gap_n, float(baseline)))
        elapsed += gap

    if not pieces:
        raise ValueError("no segments to build - add one first")
    return np.concatenate(pieces), rows


def sequence_extent(segments, rate, unit="us"):
    """(segments, seconds, points) without building the samples.

    Wanted on every keystroke to keep the running total honest, which is why it
    tolerates a half-typed number instead of raising at one.
    """
    rate = _as_float(rate, 0.0)
    count, span, points = 0, 0.0, 0
    for seg in segments:
        if not str(seg.get("shape", "")).strip():
            continue
        count += 1
        for key in ("time", "gap"):
            try:
                seconds = parse_time(seg.get(key), unit, 0.0)
            except ValueError:
                continue
            span += seconds
            points += int(round(seconds * rate)) if rate > 0 else 0
    return count, span, points


def parse_sequence_spec(text):
    """Read a typed or pasted sequence back as (segments, settings).

    One segment per line, fields in the order of SEQ_COLUMNS. Everything after
    the sixth comma is the extra field, so a value with commas of its own
    survives. Blank lines and # comments are skipped, as they are everywhere
    else numbers are pasted in - except a comment of the form `# rate: 1e6`,
    which is how the spec carries the settings that are not per-segment. They
    stay comments so that a spec written by hand without them still reads, and
    so that the whole thing is still a file somebody can follow.

    `settings` holds only the keys the text actually mentioned, and holds them
    as the strings they were written as: a rate typed 1e6 comes back 1e6 rather
    than 1000000.
    """
    keys = [key for _, key, _, _ in SEQ_COLUMNS]
    out, settings = [], {}
    for number, line in enumerate(str(text or "").splitlines(), 1):
        line = line.strip().lstrip("﻿")
        if not line:
            continue
        if line.startswith("#"):
            name, sep, value = line.lstrip("#").strip().partition(":")
            if sep and name.strip().lower() in SEQ_SETTINGS:
                settings[name.strip().lower()] = value.strip()
            continue
        fields = [f.strip() for f in line.split(",", len(keys) - 1)]
        try:
            seg = {"shape": resolve_shape(fields[0])}
        except ValueError as exc:
            raise ValueError(f"line {number}: {exc}") from None
        for key, value in zip(keys[1:], fields[1:]):
            seg[key] = value
        out.append({key: seg.get(key, "") for key in keys})
    if not out:
        raise ValueError("no segments found in that text")
    return out, settings


def format_sequence_spec(segments, rate, unit="us", baseline="0",
                         coherent=False, clock=True):
    """Segments and settings back out as the text `parse_sequence_spec` reads.

    `rate` and `baseline` are written through as the strings they were typed
    as, so a spec copied out and pasted back leaves the boxes exactly as they
    were rather than normalising 1e6 into 1000000.
    """
    keys = [key for _, key, _, _ in SEQ_COLUMNS]
    lines = ["# DS345 sequence",
             f"# rate: {rate}",
             f"# unit: {unit}",
             f"# baseline: {baseline}",
             f"# coherent: {'on' if coherent else 'off'}",
             f"# clock: {'on' if clock else 'off'}",
             "#",
             "# " + ", ".join(keys)]
    for seg in segments:
        if not str(seg.get("shape", "")).strip():
            continue
        lines.append(", ".join(str(seg.get(key, "")).strip() for key in keys)
                     .rstrip(", "))
    return "\n".join(lines) + "\n"


def _split_fields(line):
    """One line of a table into its fields, trailing empty ones dropped.

    A spreadsheet asked to save a one-column sheet writes `0,,,` - the empty
    cells of columns it was never given. Those are not columns, and reading
    them as data is what once made a rescaled waveform unloadable while the
    unrescaled one beside it was fine. An empty field *between* two real ones
    is a hole in the data rather than a stray comma, so it is left to fail
    loudly instead of silently shifting every column after it.
    """
    if any(sep in line for sep in ",;\t"):
        fields = [f.strip().strip('"') for f in re.split(r"[,;\t]", line)]
    else:
        fields = [f.strip().strip('"') for f in line.split()]
    while fields and fields[-1] == "":
        fields.pop()
    return fields


def _table_from_lines(lines, source="the data"):
    """Lines of a delimited table into (2-D array, column names or None).

    The one reader behind both a pasted block and a text file, so the two
    cannot drift apart: blank lines and # comments skipped, columns split on
    commas, semicolons, tabs or whitespace, and a first row that will not parse
    as numbers taken as the column names.
    """
    rows, names = [], None
    for raw in lines:
        line = raw.strip().lstrip("﻿")
        if not line or line.startswith("#"):
            continue
        fields = _split_fields(line)
        if not fields:
            continue
        try:
            rows.append([float(f) for f in fields])
        except ValueError:
            if not rows and names is None:
                names = fields
                continue
            raise ValueError(
                f"cannot read this line of {source} as numbers: "
                f"{line[:60]}")
    if not rows:
        raise ValueError(
            f"no numbers found in {source} - this does not look like sample "
            f"data. A saved setup is a .json; the .txt beside it is a summary "
            f"to read, not a waveform.")

    width = len(rows[0])
    if any(len(r) != width for r in rows):
        raise ValueError("rows do not all have the same number of columns")
    data = np.asarray(rows, dtype=np.float64)
    # Numbers typed across one line are a waveform, not one sample of many
    # channels.
    if data.shape[0] == 1 and data.shape[1] > 2:
        data, names = data.T, None
    return data, names


def parse_pasted(text):
    """Read typed or pasted numbers into (2-D array, column names or None).

    Same contract as read_table, so pasted data goes through the same column
    picker: rows split on newlines, columns on commas, semicolons or whitespace.
    A first row that is not numeric is taken as the column names.
    """
    return _table_from_lines(text.splitlines(), "what was pasted")


# Column headings that are an x-axis rather than samples, so a file written as
# time,volts picks the volts by default.
TIME_NAMES = {"time", "time_s", "times", "t", "t_s", "sec", "secs", "seconds",
              "s", "x", "index", "n", "sample", "samples",
              # Scaled time axes. Without these a file headed `time_us,voltage_V`
              # picks column 0 and uploads the TIME AXIS as the waveform - which
              # normalises to a clean ramp, so it looks like a plausible arb
              # rather than like a mistake.
              "time_us", "time_ms", "time_ns", "t_us", "t_ms", "t_ns",
              "us", "ms", "ns", "usec", "msec"}


def read_table(path):
    """Read a sample file into (2-D array of columns, column names or None).

    Accepts .npy, or text with any delimiter: one sample per line, time,volts
    pairs, or a full multi-column capture. A header row is detected by the
    numeric parse failing on it and skipped - Scope Grab's own CSVs are headed
    `time_s,CH1_V,...`, and replaying one of those here is the whole point.
    """
    if path.lower().endswith(".npy"):
        data = np.asarray(np.load(path), dtype=np.float64)
        if data.ndim == 1:
            data = data.reshape(-1, 1)
        return data, None

    # numpy first, because it reads a big clean file an order of magnitude
    # faster than Python can, and most files are clean. Anything it will not
    # take goes to the tolerant reader rather than to the user.
    delim = "," if path.lower().endswith(".csv") else None
    try:
        data = np.asarray(np.loadtxt(path, delimiter=delim, ndmin=2,
                                     comments="#"), dtype=np.float64)
        names = None
    except Exception:
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            return _table_from_lines(fh.readlines(), os.path.basename(path))

    # A single row of many values is a waveform written across the line, not a
    # one-sample capture of many channels.
    if data.shape[0] == 1 and data.shape[1] > 2:
        data, names = data.T, None
    return data, names


def default_column(names, ncols):
    """Which column is most likely to hold the samples."""
    if names and len(names) == ncols:
        for i, name in enumerate(names):
            if name.strip().lstrip("#").strip().lower() not in TIME_NAMES:
                return i
    # Unnamed: a lone column is the data, and a pair is time,volts.
    return 0 if ncols == 1 else 1


def dac_samples(samples, normalize=True):
    """The samples exactly as the DAC will receive them, in -1..+1.

    One definition on purpose, shared by the preview, the local copy and the
    byte-packing, so the same waveform cannot change height the moment it is
    sent. The 12-bit quantisation happens at pack time, not here: the local
    copy keeps the full precision so a record pulled back for editing has not
    been rounded twice.
    """
    data = np.asarray(samples, dtype=np.float64).ravel()
    if normalize:
        peak = float(np.max(np.abs(data)))
        if peak == 0:
            raise ValueError("all samples are zero; nothing to normalise")
        data = data / peak
    return np.clip(data, -1.0, 1.0)


# The first line of every cached waveform, and how one is told apart from any
# other CSV that happens to live in the same folder - a two-column scope capture
# read into a single stream of samples would be time and volts interleaved.
# The 4063B tool's mark is accepted too, so a file copied across from its
# Waveforms folder previews here without editing.
CACHE_MARK = "DS345-AWG-GUI waveform"
CACHE_MARKS = (CACHE_MARK, "BK4063B-AWG-GUI waveform")


def cache_file(name, ext=".csv"):
    return os.path.join(WAVE_CACHE, safe_name(name) + ext)


def cache_save(name, samples):
    """Keep a copy of what was just uploaded, so it can be previewed later.

    Text rather than .npy: the point of a local copy is being able to see what
    was sent, and a binary blob in a folder called Waveforms is not that. Seven
    significant figures is well past what a 12-bit DAC can hold, so nothing is
    lost by writing it out in a form that opens in anything.
    """
    os.makedirs(WAVE_CACHE, exist_ok=True)
    data = np.asarray(samples, dtype=np.float64).ravel()
    np.savetxt(cache_file(name), data, fmt="%.7g",
               header=f"{CACHE_MARK} '{name}', {data.size} points, "
                      "one sample per line, normalised to -1..1")


def cache_read(path):
    """One cached waveform's samples, or None if the file is not one of ours."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
            if not any(mark in first for mark in CACHE_MARKS):
                return None
        try:
            return np.asarray(np.loadtxt(path, ndmin=1),
                              dtype=np.float64).ravel()
        except Exception:
            # One of ours that has been round a spreadsheet since. The header
            # says it is a cache file, so it is read as one rather than being
            # dropped for the trailing commas Excel left behind.
            with open(path, encoding="utf-8-sig", errors="replace") as fh:
                data, _ = _table_from_lines(fh.readlines(),
                                            os.path.basename(path))
            return np.asarray(data[:, 0], dtype=np.float64).ravel()
    except Exception:
        return None                   # a damaged entry is not worth a crash


def cache_load():
    """Every waveform this app has uploaded, as {name: samples}."""
    out = {}
    try:
        entries = sorted(os.listdir(WAVE_CACHE))
    except OSError:
        return out
    for entry in entries:
        path = os.path.join(WAVE_CACHE, entry)
        if entry.endswith(".csv"):
            data = cache_read(path)
            if data is not None:
                out[entry[:-4]] = data
        elif entry.endswith(".npy") and entry[:-4] not in out:
            try:
                out[entry[:-4]] = np.asarray(np.load(path),
                                             dtype=np.float64).ravel()
            except Exception:
                pass
    return out


def cache_forget(name):
    """Delete this app's copies of a waveform, and only this app's.

    The CSV is checked for the header before it goes, so Forget cannot take
    out a file that merely shares the folder. A .npy has no header to check -
    it is read at all only because the 4063B tool once wrote them and its
    Waveforms folder gets copied across - so it is opened instead, and only a
    plain one-dimensional array of numbers is treated as ours. Anything else
    under that name is somebody's data, and this is not the tool that deletes
    it.
    """
    gone = False
    csv = cache_file(name)
    if cache_read(csv) is not None:
        try:
            os.remove(csv)
            gone = True
        except OSError:
            pass
    npy = cache_file(name, ".npy")
    try:
        data = np.load(npy, allow_pickle=False)
        ours = data.ndim == 1 and data.dtype.kind in "fiu"
    except Exception:
        ours = False
    if ours:
        try:
            os.remove(npy)
            gone = True
        except OSError:
            pass
    return gone


def safe_name(text):
    """Trim a typed name down to something legal in a filename."""
    out = "".join("_" if c in BAD_NAME_CHARS else c for c in text.strip())
    return out.strip() or "awg"


def fmt_value(raw):
    """Normalise an instrument reply into what the panel displays."""
    raw = str(raw).strip()
    try:
        return f"{float(raw):g}"
    except ValueError:
        return raw


def parse_ampl(reply):
    """'1.00VP' -> ('1', 'VP'). The unit rides on the number, per the manual."""
    text = str(reply).strip().upper()
    for unit in AMPL_UNITS:
        if text.endswith(unit):
            return fmt_value(text[:-len(unit)]), unit
    return fmt_value(text), ""


# ---------------------------------------------------------------------------
# Preview
#
# Computed here rather than read back from the generator: the point is to show
# what Apply would produce, before it is applied. Everything below is pure -
# panel strings in, volts against seconds out.
# ---------------------------------------------------------------------------

# Peak-to-peak over RMS for each function, used to turn an amplitude typed in
# Vrms or dBm into the volts the plot is drawn in. The instrument does this
# same arithmetic on its display; the numbers are the standard crest factors.
_CREST = {"SINE": 2.0 * np.sqrt(2.0), "SQUARE": 2.0,
          "TRIANGLE": 2.0 * np.sqrt(3.0), "RAMP": 2.0 * np.sqrt(3.0)}


def ampl_vpp(func, value, unit):
    """An AMPL setting as peak-to-peak volts, for drawing.

    dBm is into the DS345's 50 ohm: Vrms = sqrt(P * 50) with P in watts. For
    NOISE and ARB the crest factor is not defined, so Vrms and dBm are
    converted with the sine's factor - close enough for a picture whose exact
    height the instrument's own display is the authority on.
    """
    crest = _CREST.get(func, _CREST["SINE"])
    unit = (unit or "VP").upper()
    if unit == "VR":
        return value * crest
    if unit == "DB":
        return np.sqrt(10.0 ** (value / 10.0) * 1e-3 * 50.0) * crest
    return value


def _mod_wave(name, u):
    """The modulating signal, -1..1, from its own phase u (in cycles)."""
    u = np.asarray(u) % 1.0
    name = (name or "SINE").upper()
    if name == "SQUARE":
        return np.where(u < 0.5, 1.0, -1.0)
    if name == "TRIANGLE":
        return 1.0 - 4.0 * np.abs(((u + 0.25) % 1.0) - 0.5)
    if name == "RAMP":
        return 2.0 * u - 1.0
    return np.sin(2.0 * np.pi * u)


def _sweep_u(name, u):
    """Where a sweep is within its span, 0..1, from its own phase u (cycles).

    RAMP restarts at the bottom each cycle; TRIANGLE goes up and back down;
    SINGLE is handled by the caller, because one pass and then holding still
    is about the window, not about a repeating shape.
    """
    u = np.asarray(u) % 1.0
    if (name or "RAMP").upper() == "TRIANGLE":
        return 1.0 - np.abs(2.0 * u - 1.0)
    return u


def _shape_out(func, ph, arb):
    """Unit output shape, -1..1, from carrier phase ph (in cycles).

    TRIANGLE and RAMP are separate functions on this generator, not one shape
    with a symmetry setting: the ramp is the rising sawtooth the front panel
    calls RAMP. An arb point is held for its whole 1/FSMP - a 12-bit DAC
    stepping through the record - so the lookup floors rather than
    interpolates, and the staircase in the picture is the one on the scope.
    """
    x = np.asarray(ph) % 1.0
    if func == "SINE":
        return np.sin(2.0 * np.pi * x)
    if func == "SQUARE":
        return np.where(x < 0.5, 1.0, -1.0)
    if func == "TRIANGLE":
        return 1.0 - 4.0 * np.abs(((x + 0.25) % 1.0) - 0.5)
    if func == "RAMP":
        return 2.0 * x - 1.0
    if func == "ARB":
        if arb is None or len(arb) < 2:
            return None
        return arb[(x * len(arb)).astype(int) % len(arb)]
    return None


def _numget(source):
    """Read panel strings as numbers, falling back when a box is empty."""
    def num(key, default=0.0):
        try:
            return float(str(source.get(key, "")).strip() or default)
        except (ValueError, AttributeError):
            return default
    return num


def repeat_freq(func, vals, arb_n=None):
    """How often the output actually repeats, in hertz.

    For the standard functions that is FREQ. An arb has no FREQ: the record
    repeats at FSMP over its point count, so the length of the record being
    drawn - pending or held locally - is part of the answer.
    """
    num = _numget(vals)
    if func == "ARB":
        rate = num("FSMP", 0.0)
        if rate > 0 and arb_n:
            return rate / arb_n
        return 1000.0
    freq = num("FREQ", 1000.0)
    return freq if freq > 0 else 1000.0


def preview_period(func, vals, mode="Off", mod=None, arb_n=None):
    """The repeat time worth drawing two of.

    With a mode running that is the envelope, not the carrier: two carrier
    cycles of a 1 kHz tone under 10 Hz AM is a flat sine with no modulation
    visible. Sweeps and continuous modulation repeat at RATE; a burst repeats
    at the internal trigger rate, and never faster than the burst itself.
    """
    mnum = _numget(mod or {})
    carrier = 1.0 / repeat_freq(func, vals, arb_n)
    if mode in ("AM", "FM", "PM", "Lin sweep", "Log sweep"):
        rate = mnum("RATE", 0.0)
        return 1.0 / rate if rate > 0 else carrier
    if mode == "Burst":
        length = carrier * max(mnum("BCNT", 1.0), 1.0)
        trat = mnum("TRAT", 0.0)
        if str((mod or {}).get("TSRC", "")).strip().upper() == "INT" and trat > 0:
            return max(1.0 / trat, length)
        return length
    return carrier


# How finely the preview is sampled. The window is sized by the modulating
# envelope rather than by the carrier, so a slow modulation on a fast carrier
# can put thousands of carrier cycles inside one picture, and a fixed point
# budget then draws an alias instead of the waveform. The count is chosen per
# trace from what is actually in it, between a floor that keeps a plain tone
# smooth and a ceiling that keeps a redraw-per-keystroke responsive.
PREVIEW_MIN_POINTS = 2000
PREVIEW_MAX_POINTS = 20000
PTS_PER_CYCLE = 40          # what a smooth oscillating trace wants
PTS_PER_ARB_SAMPLE = 3      # a stored point cannot be resolved finer than itself

# Points per feature below which the preview stops being a faithful picture.
# A dozen points across a cycle already puts the drawn peak about 3% low, and
# below two the shape itself is wrong. A stored arb point is its own limit -
# one preview point each already shows every sample there is.
DENSITY_LIMITS = {"cycle": 12.0, "stored point": 1.0}

# Below this the shape is not merely coarse, it is a different shape: two
# points per cycle is where a sampled sine stops carrying its own frequency.
ALIAS_LIMIT = 2.0


def preview_features(func, vals, mode="Off", mod=None, arb=None):
    """Everything in the trace that has to be resolved, as
    (per second, what one of them is called, points wanted each).

    Separate from `preview_period`, which answers how *long* to draw. How
    *finely* is a different question with a different answer: the window
    follows the modulating envelope while the detail follows the carrier, its
    deviation, or the stored points of an arb.
    """
    mnum = _numget(mod or {})
    arb_n = len(arb) if arb is not None else None
    freq = repeat_freq(func, vals, arb_n)

    rate = freq
    if mode == "FM":
        rate = freq + abs(mnum("FDEV")) / 2.0
    elif mode in ("Lin sweep", "Log sweep"):
        rate = max(rate, mnum("STFR", freq), mnum("SPFR", freq))
    out = [(rate, "cycle", PTS_PER_CYCLE)]

    if func == "ARB" and arb is not None and len(arb) >= 2:
        out.append((freq * len(arb), "stored point", PTS_PER_ARB_SAMPLE))
    return out


def preview_points(span, func, vals, mode="Off", mod=None, arb=None):
    """How many samples to draw `span` seconds with.

    Deterministic, so the plot and the warning about the plot agree without
    having to pass the count between them.
    """
    want = max(per * span * rate for rate, _, per
               in preview_features(func, vals, mode, mod, arb))
    return max(2, int(min(max(PREVIEW_MIN_POINTS, want), PREVIEW_MAX_POINTS)))


def preview_aliasing(span, n, func, vals, mode="Off", mod=None, arb=None):
    """True when `n` points across `span` seconds cannot draw this faithfully.

    Asked of the whole trace rather than of one feature: the panel says the
    picture is not to be trusted, not which parameter of it, so anything too
    fine for the budget is enough to answer yes.
    """
    if span <= 0:
        return False
    return any(rate > 0 and n / (span * rate) < DENSITY_LIMITS.get(unit, 8.0)
               for rate, unit, _
               in preview_features(func, vals, mode, mod, arb))


# The output stage's own ceiling: whichever way the two are split, the DS345
# will not put out more than |Vpp/2| + |Vdc| = 5 V. It is the coupling that
# makes apply_settings retry an amplitude once the offset it was fighting has
# moved, and it is worth saying before Apply rather than after.
LEVEL_MAX = 5.0


def limit_warnings(func, vals, mode="Off", mod=None):
    """The instrument's own limits this panel would cross, as short lines.

    Worked out here rather than left to the generator because the DS345's
    entire error channel is one bit in *ESR: a refused setting comes back as
    "execution error", with no word on which setting it was or by how much.
    The numbers are the specifications page's, so this is the panel saying
    what the front panel would have said.

    Pure, and about what Apply would send rather than about the picture - so
    it is as true with the output trace switched off as with it on.
    """
    num, mnum = _numget(vals), _numget(mod or {})
    out = []

    ceiling = FREQ_MAX.get(func)
    if ceiling:
        freq = num("FREQ")
        if freq > ceiling:
            out.append(f"{_hz(freq)} is past the {func} ceiling of "
                       f"{_hz(ceiling)}")
        elif mode == "FM":
            # A carrier inside the limit can still be swung out of it: FDEV
            # is the whole span, so the top of the swing is half of it up.
            top = freq + abs(mnum("FDEV")) / 2.0
            if top > ceiling:
                out.append(f"FM reaches {_hz(top)}, past the {func} ceiling "
                           f"of {_hz(ceiling)}")
        elif mode in ("Lin sweep", "Log sweep"):
            top = max(mnum("STFR"), mnum("SPFR"))
            if top > ceiling:
                out.append(f"the sweep reaches {_hz(top)}, past the {func} "
                           f"ceiling of {_hz(ceiling)}")

    if func == "ARB":
        rate = num("FSMP")
        if rate > FSMP_MAX:
            out.append(f"{_hz(rate)} is past the {_hz(FSMP_MAX)} sample clock")

    # The amplitude and the offset are only ever refused as a pair, which is
    # exactly why neither cell on its own can be checked against a number.
    vpp = ampl_vpp(func, num("AMPL"), vals.get("AMPL_UNIT", "VP"))
    reach = abs(vpp) / 2.0 + abs(num("OFFS"))
    if reach > LEVEL_MAX + 1e-9:
        out.append(f"{vpp:.4g} Vpp on {num('OFFS'):.4g} V of offset reaches "
                   f"{reach:.4g} V; the DS345 allows {LEVEL_MAX:g}")
    return out


def idle_volts(func, vals, phase_deg, arb=None, invert=False):
    """The volts the output sits at when the carrier is parked at `phase_deg`.

    What a burst rests at between bursts: the DS345 holds the waveform at its
    start phase until the next trigger, and start phase 0 on an arb is its
    first stored sample. A record that begins high therefore leaves the BNC
    held high between bursts, which is worth knowing before it is wired into
    anything that would rather not be held on.
    """
    num = _numget(vals)
    ofst = num("OFFS")
    if func == "NOISE":
        return ofst
    y = _shape_out(func, np.full(1, phase_deg / 360.0), arb)
    if y is None:
        return None
    value = -float(y[0]) if invert else float(y[0])
    vpp = ampl_vpp(func, num("AMPL", 1.0), vals.get("AMPL_UNIT", "VP"))
    return ofst + vpp / 2.0 * value


def preview_curve(func, vals, arb=None, periods=2.0, n=None, span=None,
                  mode="Off", mod=None, invert=False):
    """What the panel currently describes, in volts against seconds.

    `vals` carries FREQ, AMPL, AMPL_UNIT, OFFS, PHSE and FSMP as panel
    strings. `mode` and `mod` carry the modulation/sweep/burst row.
    Everything that varies with time is folded into an instantaneous
    frequency, a phase offset and an amplitude scale, then integrated once -
    which is what lets FM and both sweeps share a path with a plain carrier
    instead of each being a special case.

    Returns (t, volts, note) or None when there is nothing meaningful to draw.
    """
    mod = mod or {}
    num, mnum = _numget(vals), _numget(mod)
    vpp = ampl_vpp(func, num("AMPL", 1.0), vals.get("AMPL_UNIT", "VP"))
    ofst = num("OFFS")
    arb_n = len(arb) if arb is not None else None
    freq = repeat_freq(func, vals, arb_n)

    if span is None:
        span = periods * preview_period(func, vals, mode, mod, arb_n)
    if n is None:
        n = preview_points(span, func, vals, mode, mod, arb)
    t = np.linspace(0.0, span, n)
    dt = t[1] - t[0] if n > 1 else 1.0

    f_inst = np.full(n, freq)                    # cycles per second
    ph_off = np.full(n, num("PHSE") / 360.0)     # cycles
    scale = np.ones(n)                           # amplitude multiplier
    bursting = None                              # None unless a burst
    burst_phase = rest_phase = None
    note = ""

    if mode == "AM":
        depth = mnum("DPTH", 100.0)
        m = _mod_wave(mod.get("MDWF"), t * mnum("RATE", 100.0))
        if depth >= 0:
            # The AMOD table pins the physics: the modulator multiplies the
            # front-panel amplitude by at most 1.0, so at depth d the envelope
            # runs from (1 - d/100) x full up to full, never above it.
            scale = 1.0 - depth / 200.0 * (1.0 - m)
        else:
            # A negative depth is DSBSC, per the DPTH command: carrier
            # suppressed, envelope crossing through zero.
            scale = (-depth / 100.0) * m
    elif mode == "FM":
        # FDEV is the span, so the deviation is +/- span/2 about FREQ.
        f_inst = freq + mnum("FDEV", 0.0) / 2.0 * _mod_wave(
            mod.get("MDWF"), t * mnum("RATE", 100.0))
    elif mode == "PM":
        ph_off = ph_off + mnum("PDEV", 0.0) / 2.0 / 360.0 * _mod_wave(
            mod.get("MDWF"), t * mnum("RATE", 100.0))
    elif mode in ("Lin sweep", "Log sweep"):
        rate = mnum("RATE", 1.0)
        sweep_t = 1.0 / rate if rate > 0 else span
        start, stop = mnum("STFR", freq), mnum("SPFR", freq)
        shape = str(mod.get("MDWF", "RAMP")).strip().upper()
        if shape == "SINGLE":
            # One pass and hold: when the next trigger comes is not ours to
            # know, so one sweep is drawn and the window rests after it.
            u = np.clip(t / max(sweep_t, 1e-12), 0.0, 1.0)
            note = "single sweep: one shown"
        else:
            u = _sweep_u(shape, t / max(sweep_t, 1e-12))
        if mode == "Log sweep" and start > 0 and stop > 0:
            f_inst = start * (stop / start) ** u
        else:
            f_inst = start + (stop - start) * u
    elif mode == "Burst":
        length = max(mnum("BCNT", 1.0), 1.0) / freq
        trigger = str(mod.get("TSRC", "INT")).strip().upper()
        trat = mnum("TRAT", 0.0)
        if trigger == "INT" and trat > 0:
            repeat = max(1.0 / trat, length)
        else:
            # A single or external trigger arrives when it arrives, so one
            # burst is drawn and the window rests after it.
            repeat = span + length
            note = f"{trigger or 'ext'} trigger: one burst shown"
        since = np.mod(t, max(repeat, 1e-12))
        bursting = since < length
        # Every burst restarts the carrier at the start phase - it is not a
        # gate over a free-running carrier - and between bursts the output
        # holds at that same phase. One expression covers both, so the two
        # cannot disagree.
        rest_phase = ph_off
        burst_phase = rest_phase + since * freq

    if func == "NOISE":
        rng = np.random.default_rng(0)           # fixed seed: a still picture
        # The DS345 specifies its noise by Vpp; the picture clips a Gaussian
        # at 3 sigma to span roughly that. The exact statistics are the
        # instrument's own business.
        y = np.clip(rng.standard_normal(n) / 3.0, -1.0, 1.0) * scale
        return t, ofst + vpp / 2.0 * (-y if invert else y), note

    # One phase accumulator for the lot: a plain carrier is the constant case,
    # and FM and the sweeps are the same integral with f varying.
    ph = np.cumsum(f_inst) * dt + ph_off
    if bursting is not None:
        ph = np.where(bursting, burst_phase, rest_phase)
    y = _shape_out(func, ph, arb)
    if y is None:
        return None
    if bursting is None:
        y = y * scale
    if invert:
        y = -y                                   # inversion flips the AC part only
    return t, ofst + (vpp / 2.0) * y, note


# ---------------------------------------------------------------------------
# Instrument layer
# ---------------------------------------------------------------------------

# Everything the panel shows, as (panel key, query). Read in one pass so a
# snapshot and a panel read cannot drift apart. PHSE? comes last of the
# output block only for tidiness - all of these are plain queries the manual
# allows at any time; it is *setting* them that has preconditions.
READ_QUERIES = [
    ("FUNC", "FUNC?"),
    ("FREQ", "FREQ?"),
    ("FSMP", "FSMP?"),
    ("AMPL", "AMPL?"),
    ("OFFS", "OFFS?"),
    ("PHSE", "PHSE?"),
    ("INVT", "INVT?"),
    ("MENA", "MENA?"),
    ("MTYP", "MTYP?"),
    ("MDWF", "MDWF?"),
    ("DPTH", "DPTH?"),
    ("FDEV", "FDEV?"),
    ("PDEV", "PDEV?"),
    ("RATE", "RATE?"),
    ("BCNT", "BCNT?"),
    ("STFR", "STFR?"),
    ("SPFR", "SPFR?"),
    ("TSRC", "TSRC?"),
    ("TRAT", "TRAT?"),
    ("AMRT", "AMRT?"),
]


class Ds345:
    """The DS345 over GPIB. One VISA session, so every exchange the GUI makes
    is serialised through the worker thread."""

    def __init__(self):
        self.rm = None
        self.inst = None
        self.idn = ""
        self.addr = ""

    def connect(self, addr=None, remembered=None, log=lambda s: None):
        """Find the generator and open it.

        A typed address is tried alone and taken at its word if something
        answers *IDN?. Otherwise: the address that worked last time, then
        whatever GPIB resources VISA already lists, then the whole primary
        address range - because an instrument that has never been opened by
        NI MAX does not show up in list_resources at all, and the DS345 on
        this bench is exactly such an instrument. The full walk takes a
        minute the first time; after that the remembered address makes
        connecting instant.
        """
        self.close()
        self.rm = pyvisa.ResourceManager()
        if addr:
            candidates = [addr]
        else:
            listed = [r for r in self.rm.list_resources()
                      if r.upper().startswith("GPIB")]
            candidates = [remembered] if remembered else []
            candidates += [r for r in listed if r not in candidates]
            candidates += [f"GPIB0::{n}::INSTR" for n in range(1, 31)
                           if f"GPIB0::{n}::INSTR" not in candidates]
        problems = []
        for res in candidates:
            dev = None
            try:
                dev = self.rm.open_resource(res)
                # Short while probing: a primary address with nothing behind
                # it costs a full timeout, and there are thirty of them.
                dev.timeout = 1200 if not addr else 4000
                dev.write_termination = "\n"
                dev.read_termination = "\n"
                idn = dev.query("*IDN?").strip()
            except Exception as exc:
                problems.append(f"  {res}: {type(exc).__name__}")
                if dev is not None:
                    try:
                        dev.close()
                    except Exception:
                        pass
                continue
            # A typed address is taken at its word; a scan only claims a DS345.
            if addr or "DS345" in idn.upper():
                dev.timeout = 8000
                self.inst, self.idn, self.addr = dev, idn, res
                # Clear a stale event register so the first Apply's error
                # checks start from zero rather than from whatever the last
                # session - or a front-panel key press - left latched.
                try:
                    dev.query("*ESR?")
                except Exception:
                    pass
                return idn
            problems.append(f"  {res}: not a DS345 ({idn})")
            dev.close()
        raise RuntimeError(
            "No DS345 found on GPIB.\n" + "\n".join(problems) + "\n\n"
            "Check that the DS345 is powered on, and on its front panel that "
            "the GPIB interface is enabled with a free address "
            "(SHIFT then GPIB shows the address; the SR760 on this bus "
            "already holds 10).")

    def close(self):
        for obj in (self.inst, self.rm):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        self.inst = self.rm = None

    # -- primitives --------------------------------------------------------

    def query(self, command):
        return self.inst.query(command).strip()

    def write(self, command):
        self.inst.write(command)

    def recover(self):
        """Flush a half-finished exchange after a timeout, so the next query
        cannot read the previous reply."""
        try:
            self.inst.clear()
        except Exception:
            pass

    def esr_errors(self):
        """What the generator objected to since the last look, as text.

        *ESR? is the DS345's whole error channel: reading it clears it, so a
        read after each write attributes an error to the command that caused
        it. Bit 6 (any front-panel key press) is masked out - somebody
        walking past the bench is not a failed command.
        """
        try:
            value = int(float(self.query("*ESR?")))
        except Exception:
            return []
        return [name for bit, name in ESR_BITS.items() if value & (1 << bit)]

    def checked_write(self, command, log=lambda s: None):
        """Send one command and ask the generator whether it objected.

        Returns the objection as text, or an empty list. The *ESR? that does
        the asking also waits out the command - the input queue is served in
        order, so the reply only comes once the write has been executed.
        """
        self.write(command)
        log(f"  {command}")
        errors = self.esr_errors()
        return [f"{command} -> {', '.join(errors)}"] if errors else []

    # -- reading -----------------------------------------------------------

    def read_state(self):
        """Every setting the panel shows, as {key: raw reply string}.

        Each query is tolerated failing on its own: one flaky exchange should
        cost one blank cell, not the whole read.
        """
        out = {}
        for key, q in READ_QUERIES:
            try:
                out[key] = self.query(q)
            except Exception:
                self.recover()
                out[key] = ""
        return out

    # -- writing -----------------------------------------------------------

    def apply_settings(self, plan, log=lambda s: None):
        """Push the edited settings, in the order the DS345 needs.

        `plan` is {key: value} in wire units, plus optionally
        plan["MODE"] = (mode name, {command: wire value}).

        The ordering is not cosmetic:

          FUNC first - the frequency limits depend on the function, and
            switching function coerces an incompatible FREQ itself, so FREQ
            has to land after it or a legal setup is refused.
          AMPL and OFFS are coupled by |Vpp/2| + |Vdc| <= 5 V. Whichever is
            sent first can transiently violate that against the *old* value
            of the other, so if the first of them is refused it is retried
            once after its partner lands - by then the pair is judged as the
            pair that was actually asked for.
          PHSE only goes when the function and mode give it meaning - the
            manual has the set (not the query) error under NOISE, ARB,
            sweeps, FM and PM.
          The mode block goes last, MENA last of all, because enabling is
            when the DS345 checks the whole arrangement for consistency.

        Returns what did not take, which is nothing on a good day.
        """
        missed = []

        def send(command):
            missed.extend(self.checked_write(command, log))

        if "FUNC" in plan:
            send(f"FUNC {plan['FUNC']}")
        if "FSMP" in plan:
            send(f"FSMP {plan['FSMP']}")
        if "FREQ" in plan:
            send(f"FREQ {plan['FREQ']}")

        ampl_cmd = f"AMPL {plan['AMPL']}" if "AMPL" in plan else None
        offs_cmd = f"OFFS {plan['OFFS']}" if "OFFS" in plan else None
        ampl_errors = []
        if ampl_cmd:
            self.write(ampl_cmd)
            log(f"  {ampl_cmd}")
            ampl_errors = self.esr_errors()
        if offs_cmd:
            send(offs_cmd)
        if ampl_errors:
            if offs_cmd:
                # Second try, now that the offset it was fighting has moved.
                retry = self.checked_write(ampl_cmd, log=lambda s: None)
                if retry:
                    missed.extend(retry)
                else:
                    log(f"  ({ampl_cmd} took on the second try, after OFFS)")
            else:
                missed.append(f"{ampl_cmd} -> {', '.join(ampl_errors)}")

        if "PHSE" in plan:
            send(f"PHSE {plan['PHSE']}")
        if "INVT" in plan:
            send(f"INVT {plan['INVT']}")

        if "MODE" in plan:
            mode, params = plan["MODE"]
            missed += self.apply_mode(mode, params, log)
        return missed

    def apply_mode(self, mode, params, log=lambda s: None):
        """Put the modulation/sweep/burst row onto the instrument.

        The manual says every modulation parameter may be set at any time, so
        a running mode whose type is unchanged just gets its parameters - no
        MENA off/on, no glitch in whatever the experiment is listening to.
        A change of type gets the full sequence: off, retype, parameters,
        on - with the enable last because that is when the DS345 checks the
        arrangement and refuses an inconsistent one.
        """
        missed = []
        if mode == "Off":
            missed.extend(self.checked_write("MENA 0", log))
            log("  modulation, sweep and burst off")
            return missed

        try:
            running = (self.query("MENA?").strip() == "1"
                       and self.query("MTYP?").strip() == MTYP_OF[mode])
        except Exception:
            running = False

        if not running:
            missed.extend(self.checked_write("MENA 0", log))
            missed.extend(self.checked_write(f"MTYP {MTYP_OF[mode]}", log))
        for cmd in ("MDWF", "DPTH", "FDEV", "PDEV", "RATE", "BCNT",
                    "STFR", "SPFR", "TSRC", "TRAT"):
            if cmd in params and params[cmd] != "":
                missed.extend(self.checked_write(f"{cmd} {params[cmd]}", log))
        if not running:
            missed.extend(self.checked_write("MENA 1", log))
            missed += self.mode_took(mode)
        return missed

    def mode_took(self, mode):
        """Empty if modulation really is on, or one line saying it is not."""
        try:
            if self.query("MENA?").strip() == "1":
                return []
        except Exception:
            return []                 # a check that cannot run is not a failure
        return [f"{mode} did not switch on - the DS345 refused the enable, "
                "which usually means a parameter is out of range for the "
                "current function"]

    def trigger(self):
        """*TRG fires a single burst or single sweep. The trigger source has
        to be SINGLE for it to mean anything; the ESR says if it was not."""
        return self.checked_write("*TRG")

    def upload_arb(self, samples, normalize=True):
        """Download a waveform into the DS345's (single, anonymous) arb slot.

        The LDWF?0,N handshake from the manual: ask, wait for the ASCII "1"
        that says the generator is ready, then send N 16-bit words least
        significant byte first, then a 16-bit checksum that is the plain sum
        of the words with the carries ignored. Values span -2047..+2047 - a
        12-bit DAC, so this generator quantises about 16x coarser than the
        4063B. If the function is already ARB the new record starts playing
        the moment the checksum lands.
        """
        # Length before height: normalising an all-zero record raises about
        # the zeros, and on a record that is also the wrong length that is
        # the less useful of the two things wrong with it.
        n_in = np.asarray(samples).size
        if n_in < MIN_ARB_POINTS:
            raise ValueError(f"need at least {MIN_ARB_POINTS} samples")
        if n_in > MAX_ARB_POINTS:
            raise ValueError(f"{n_in:,} samples will not fit - the "
                             f"DS345 holds at most {MAX_ARB_POINTS:,} points")
        data = dac_samples(samples, normalize=normalize)
        codes = np.round(data * DAC_FULL_SCALE).astype("<i2")

        reply = self.query(f"LDWF?0,{codes.size}")
        if reply.strip() != "1":
            raise RuntimeError(f"the DS345 answered {reply!r} to LDWF? "
                               "instead of 1 - it is not ready for data")
        checksum = int(codes.astype(np.int64).sum()) & 0xFFFF
        # One write_raw for data and checksum together: the instrument counts
        # bytes, and a write termination injected between them would be read
        # as waveform data and wreck the count.
        self.inst.write_raw(codes.tobytes() + struct.pack("<H", checksum))
        errors = self.esr_errors()
        if errors:
            raise RuntimeError("the DS345 rejected the waveform data ("
                               + ", ".join(errors) + ")")
        return codes.size

    def snapshot(self):
        return {
            "captured": datetime.datetime.now().isoformat(timespec="seconds"),
            "instrument": self.idn,
            "address": self.addr,
            "settings": self.read_state(),
        }


def describe(snap):
    """Human-readable form of a snapshot, for the .txt written beside it."""
    s = snap.get("settings", {})
    try:
        func = FUNCS[int(float(str(s.get("FUNC", "")).strip()))]
    except (ValueError, IndexError):
        func = "?"          # a .txt is a courtesy; it must not fail the save
    mena = str(s.get("MENA", "")).strip()
    mode = (MODE_OF_MTYP.get(str(s.get("MTYP", "")).strip(), "?")
            if mena == "1" else "Off")
    lines = [
        f"saved              : {snap.get('captured', '')}",
        f"instrument         : {snap.get('instrument', '')}",
        f"visa address       : {snap.get('address', '')}",
        "",
        f"function           : {func}",
        f"frequency          : {s.get('FREQ', '')} Hz",
        f"arb sample rate    : {s.get('FSMP', '')} Sa/s",
        f"amplitude          : {s.get('AMPL', '')}",
        f"offset             : {s.get('OFFS', '')} V",
        f"phase              : {s.get('PHSE', '')} deg",
        f"inverted           : {'yes' if str(s.get('INVT', '')).strip() == '1' else 'no'}",
        # The one thing a snapshot can know about the arb slot. The DS345
        # cannot read a waveform back, so this name - and the CSV in
        # Waveforms/ that goes with it - is the whole record of what was
        # playing when the setup was saved.
        f"arb local name     : {snap.get('arb_local_name', '') or '(unknown)'}",
        "",
        f"modulation         : {mode}",
    ]
    if mode != "Off":
        lines += [
            f"  mod waveform     : "
            f"{MDWF_NAMES.get(str(s.get('MDWF', '')).strip(), s.get('MDWF', ''))}",
            f"  AM depth         : {s.get('DPTH', '')} %",
            f"  FM span          : {s.get('FDEV', '')} Hz",
            f"  PM span          : {s.get('PDEV', '')} deg",
            f"  rate             : {s.get('RATE', '')} Hz",
            f"  burst count      : {s.get('BCNT', '')}",
            f"  sweep start      : {s.get('STFR', '')} Hz",
            f"  sweep stop       : {s.get('SPFR', '')} Hz",
            f"  trigger source   : "
            f"{TSRC_NAMES.get(str(s.get('TSRC', '')).strip(), s.get('TSRC', ''))}",
            f"  trigger rate     : {s.get('TRAT', '')} Hz",
        ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------


class App:
    def __init__(self, root):
        self.root = root
        self.awg = Ds345()
        self.msgs = queue.Queue()
        self.busy = False
        self.arb_samples = None       # samples loaded from disk, not yet uploaded
        self.arb_table = None         # every column of that file, for the picker
        self.arb_element = None       # the last built/loaded/pasted waveform,
                                      # kept so a train is never built on a train
        self.arb_index = 0            # which column of that file holds them
        self.known_waves = cache_load()   # name -> samples, from earlier sessions
        self.arb_source = ""
        self.read_stamp = ""
        # What the generator's single arb slot holds, as far as this app can
        # know. LDWF? is one-way, so the answer is only ever "what this app
        # last sent" (or what the config remembered) - never a readback.
        self.device_wave = ""
        self.wave_dir = WAVE_CACHE

        # panel key -> widgets/state, exactly one entry per editable cell
        self.vars = {}                # key -> StringVar shown in the panel
        self.marks = {}               # key -> "edited" marker label
        self.inst_vals = {}           # key -> value the generator last reported
        self.widgets = {}             # key -> the entry/combobox itself
        self.natural = {}             # key -> state to restore when re-enabled
        self.labels = {}              # key -> its caption, for greying out
        self.halos = {}               # key -> the frame behind it, for the tint
        self.halo_off = None          # whatever Tk's default frame colour is
        self.computed = set()         # keys this app worked out and filled in
        self.driver = {}              # linked group -> the cell now driving it
        self.out_state = None         # LIVE / MUTED / DC / None
        self.mute_restore = None      # (ampl value, ampl unit, offset) to put back

        # The mode row's boxes are shared by every mode, so what each one means
        # is remembered here: without it a switch of mode leaves the old values
        # sitting under the new labels.
        self.mode_shown = "Off"
        self.mode_bad = ()
        self.loading = False          # panel being filled from the generator
        self.quiet = False            # a var write that is not a user edit

        # The address the config file remembers, kept as its own field
        # rather than read back out of saved_cfg: a connect that fails leaves
        # awg.addr empty, and a config value that only lives in the snapshot
        # of the last save would be erased by the next one.
        self.remembered = ""

        self.setup_win = None
        self.outdir = tk.StringVar(value=os.path.join(
            os.path.expanduser("~"), "Desktop", "ds345_setups"))
        self.prefix = tk.StringVar(value="ds345")
        self.confirm_output = tk.BooleanVar(value=True)
        self.save_btn = self.recall_btn = self.mem_btn = None
        self.connect_btn = None
        self.mem_names = []           # list row -> waveform name, in order

        # The sequence lives on the app rather than in its window, so closing
        # the window and opening it again does not throw the sequence away.
        self.seq_win = None
        self.seq_data = [dict(SEQ_DEFAULT)]
        self.seq_vars = []
        self.seq_note = None
        self.seq_canvas = None
        self.seq_rate = tk.StringVar()
        self.seq_unit = tk.StringVar(value="us")
        self.seq_baseline = tk.StringVar(value="0")
        self.seq_coherent = tk.BooleanVar(value=False)
        self.seq_set_clock = tk.BooleanVar(value=True)
        for var in (self.seq_rate, self.seq_unit, self.seq_baseline):
            var.trace_add("write", lambda *_: self._seq_info())

        root.title("DS345 AWG GUI")
        win_w = min(1250, root.winfo_screenwidth() - 80)
        win_h = min(900, root.winfo_screenheight() - 120)
        root.geometry(f"{win_w}x{win_h}+40+20")

        pad = dict(padx=8, pady=4)

        body = ttk.Frame(root)
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body)
        left.pack(side="left", fill="y")
        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True)

        # --- connection row
        top = ttk.Frame(left)
        top.pack(fill="x", **pad)
        self.status = ttk.Label(top, text="Not connected", foreground="#a00")
        self.status.pack(side="left")
        self.connect_btn = ttk.Button(top, text="Connect",
                                      command=self.do_connect)
        self.connect_btn.pack(side="right")
        self.address = tk.StringVar()
        addr_box = ttk.Entry(top, textvariable=self.address, width=16)
        addr_box.pack(side="right", padx=(0, 4))
        ttk.Label(top, text="Address:").pack(side="right", padx=(0, 2))

        row2 = ttk.Frame(left)
        row2.pack(fill="x", padx=8)
        ttk.Label(row2, foreground="#666",
                  text="blank address = scan the bus (slow the first time; "
                       "the found address is remembered)").pack(side="left")

        # --- the one output panel
        self.build_output(left, pad)
        self.build_modes(left, pad)

        # --- read / apply bar
        bar = ttk.Frame(left)
        bar.pack(fill="x", **pad)
        self.read_btn = ttk.Button(bar, text="Read from generator",
                                   command=self.do_read, state="disabled")
        self.read_btn.pack(side="left")
        self.apply_btn = ttk.Button(bar, text="Apply changes",
                                    command=self.do_apply, state="disabled")
        self.apply_btn.pack(side="left", padx=6)
        # Not gated on an instrument being attached: putting the panel back to
        # what was last read is a thing the panel does to itself.
        ttk.Button(bar, text="Discard changes",
                   command=self.do_discard).pack(side="left", padx=(0, 6))
        self.sync = ttk.Label(bar, text="not read yet", foreground="#666")
        self.sync.pack(side="left", padx=6)

        bar2 = ttk.Frame(left)
        bar2.pack(fill="x", padx=8)
        ttk.Button(bar2, text="Load/save setups...",
                   command=self.do_setups).pack(side="left")
        hint = ttk.Frame(left)
        hint.pack(fill="x", padx=8, pady=(6, 0))
        tk.Label(hint, text="   ", bg=COMPUTED_BG, relief="solid",
                 borderwidth=1).pack(side="left")
        ttk.Label(hint, foreground="#666",
                  text=" computed from other settings, not set directly"
                  ).pack(side="left", padx=(4, 0))
        ttk.Label(left, foreground="#666", justify="left", wraplength=430,
                  text="The DS345 has no output switch - the BNC always "
                       "drives what the settings say, so Apply changes the "
                       "output immediately. MUTE is the nearest thing to "
                       "off: 0 Vpp, 0 V offset.").pack(
                           anchor="w", padx=10, pady=(6, 0))

        # The waveform tools sit under the preview rather than in the left
        # column: they are what the preview is showing.
        self.build_preview(right, pad)
        self.build_builder(right, pad)
        self.build_arb(right, pad)
        self.build_memory(right, pad)

        # --- log
        lf = ttk.LabelFrame(right, text="Log")
        lf.pack(fill="both", expand=True, **pad)
        self.logbox = tk.Text(lf, height=8, wrap="word", font=("Consolas", 9))
        # Wrapped continuations are indented, so a long message reads as one
        # entry rather than as several.
        self.logbox.tag_configure("entry", lmargin2=30)
        self.logbox.pack(fill="both", expand=True, padx=4, pady=4)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self.pump)
        self.root.after(300, self.do_connect)
        self.saved_cfg = None
        self.load_config()
        self.refresh_memory()
        self.refresh_marks()
        self.draw_preview()

    # -- panel construction ------------------------------------------------

    def cell(self, parent, key, choices, row, col, width, on_change=None):
        """One editable panel cell plus its unapplied-edit marker."""
        holder = ttk.Frame(parent)
        holder.grid(row=row, column=col, sticky="w", padx=2, pady=1)
        var = tk.StringVar()
        # A plain tk.Frame behind the widget, showing two pixels all the way
        # round. That is the tint for a computed value: the vista theme ignores
        # a ttk field background outright, and swapping in a tk.Entry for the
        # cells that need one would leave the panel with two kinds of box. The
        # frame is always there, so turning the tint on shifts no layout.
        halo = tk.Frame(holder)
        halo.pack(side="left")
        if self.halo_off is None:
            self.halo_off = halo.cget("bg")
        # None -> plain entry. A list of choices -> fixed dropdown.
        if choices is not None:
            w = ttk.Combobox(halo, textvariable=var, values=list(choices),
                             width=max(4, width - 3),
                             height=min(len(choices), 24) if choices else 10,
                             state="readonly" if choices else "normal")
        else:
            w = ttk.Entry(halo, textvariable=var, width=width)
        w.pack(padx=2, pady=2)
        mark = ttk.Label(holder, text=" ", width=1, foreground="#c60")
        mark.pack(side="left")

        self.halos[key] = halo
        self.vars[key] = var
        self.marks[key] = mark
        self.widgets[key] = w
        self.inst_vals[key] = ""
        # Remembered so re-enabling a cell restores the state it was built with:
        # a fixed-list combobox must come back readonly, not as free text.
        self.natural[key] = "readonly" if choices else "normal"
        var.trace_add("write", lambda *_: self.on_edit(key, on_change))
        return w

    def enable(self, key, on):
        self.widgets[key].configure(state=self.natural[key] if on else "disabled")

    def build_output(self, parent, pad):
        f = ttk.LabelFrame(parent, text="Output")
        f.pack(fill="x", **pad)

        # --- the mute row: the only control that changes the output live
        o = ttk.Frame(f)
        o.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(o, text="Output:").pack(side="left")
        self.out_btn = tk.Button(o, width=7, borderwidth=2,
                                 font=("Segoe UI", 9, "bold"),
                                 command=self.flip_output, state="disabled")
        self.out_btn.pack(side="left", padx=(4, 8))
        self.paint_output(self.out_btn, None)
        # Inversion is a setting like any other, so it goes through Apply.
        ttk.Label(o, text="Invert:").pack(side="left", padx=(12, 0))
        self.cell(self._grid(o), "INVT", ("OFF", "ON"), 0, 0, 7)
        # TTL and ECL are the DS345's own one-key level presets. Here they
        # fill in the cells as unapplied edits rather than going straight to
        # the instrument, so they pass through the same Apply - and the same
        # asking-first - as anything typed.
        ttk.Button(o, text="TTL", width=4,
                   command=lambda: self.preset_levels("5", "VP", "2.5", "TTL")
                   ).pack(side="left", padx=(14, 2))
        ttk.Button(o, text="ECL", width=4,
                   command=lambda: self.preset_levels("1", "VP", "-1.3", "ECL")
                   ).pack(side="left")

        # --- function + its parameters
        w = ttk.Frame(f)
        w.pack(fill="x", padx=6, pady=(2, 6))
        ttk.Label(w, text="Function:").grid(row=0, column=0, sticky="e",
                                            padx=(0, 4))
        self.cell(w, "FUNC", FUNCS, 0, 1, 11,
                  on_change=self.on_function)
        # The amplitude unit rides the top row next to the function: it
        # changes what the AMPL number means, so it lives with the settings
        # rather than as a display preference.
        label = ttk.Label(w, text="Ampl unit:")
        label.grid(row=0, column=2, sticky="e", padx=(0, 4))
        self.labels["AMPL_UNIT"] = label
        self.cell(w, "AMPL_UNIT", AMPL_UNITS, 0, 3, 11,
                  on_change=self.on_function)
        for i, (key, label_text, _) in enumerate(OUT_PARAMS):
            row, col = divmod(i, 3)
            row += 1
            lab = ttk.Label(w, text=label_text + ":")
            lab.grid(row=row, column=col * 2, sticky="e", padx=(0, 4), pady=1)
            self.labels[key] = lab
            self.cell(w, key, None, row, col * 2 + 1, 11)

    def build_modes(self, parent, pad):
        f = ttk.LabelFrame(parent, text="Modulation / sweep / burst")
        f.pack(fill="x", **pad)
        m = ttk.Frame(f)
        m.pack(fill="x", padx=6, pady=(6, 6))
        ttk.Label(m, text="Mode:").grid(row=0, column=0, sticky="e", padx=(0, 4))
        self.cell(m, "MODE", MODES, 0, 1, 11, on_change=self.on_mode)
        # *TRG only means anything with the trigger source on SINGLE, but it
        # lives here always: the button being present is how you find out
        # single triggering exists.
        self.trig_btn = ttk.Button(m, text="Trigger (*TRG)",
                                   command=self.do_trigger, state="disabled")
        self.trig_btn.grid(row=0, column=3, sticky="w", padx=(10, 0))
        for slot in range(MODE_SLOTS):
            row, col = divmod(slot, 3)
            row += 1
            lab = ttk.Label(m, text="")
            lab.grid(row=row, column=col * 2, sticky="e", padx=(0, 4), pady=1)
            setattr(self, f"modlab{slot}", lab)
            holder = ttk.Frame(m)
            holder.grid(row=row, column=col * 2 + 1, sticky="w", padx=2, pady=1)
            var = tk.StringVar()
            # A Combobox with state="normal" and no values is an entry box, so
            # one widget covers both the free-number and the fixed-list slots.
            cb = ttk.Combobox(holder, textvariable=var, width=8, state="normal")
            cb.pack(side="left")
            mark = ttk.Label(holder, text=" ", width=1, foreground="#c60")
            mark.pack(side="left")
            key = f"MODE:{slot}"
            self.vars[key] = var
            self.marks[key] = mark
            self.widgets[key] = cb
            self.inst_vals[key] = ""
            self.natural[key] = "normal"
            var.trace_add("write",
                          lambda *_, k=key: self.on_edit(
                              k, self.show_burst_idle))

        # Under the mode row because that is where a burst is chosen, and the
        # level a burst rests at is a property of the burst rather than of the
        # waveform.
        self.modnote = ttk.Label(m, text="", justify="left", wraplength=430)
        self.modnote.grid(row=(MODE_SLOTS + 2) // 3 + 1, column=0, columnspan=6,
                          sticky="w", padx=(0, 4), pady=(3, 0))

    @staticmethod
    def _grid(parent):
        """A one-cell grid container, so `cell` can grid inside a packed row."""
        holder = ttk.Frame(parent)
        holder.pack(side="left")
        return holder

    def preset_levels(self, ampl, unit, offs, name):
        """Fill the level cells the way the ATTL/AECL keys would set them.

        As unapplied edits, not as commands: the instrument's own keys switch
        the output there and then, and a level preset that bypassed Apply
        would be the one control in this panel that did.
        """
        for key, value in (("AMPL", ampl), ("AMPL_UNIT", unit),
                           ("OFFS", offs)):
            self.vars[key].set(value)
        self.log(f"{name} levels filled in ({ampl} {unit}, {offs} V offset) "
                 "- not applied yet")

    def build_builder(self, parent, pad):
        f = ttk.LabelFrame(parent, text="Build waveform")
        f.pack(fill="x", **pad)

        r = ttk.Frame(f)
        r.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(r, text="Shape:").pack(side="left")
        self.shape = tk.StringVar(value="Gaussian")
        cb = ttk.Combobox(r, textvariable=self.shape, values=list(BUILD_SHAPES),
                          width=17, height=len(BUILD_SHAPES), state="readonly")
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", lambda e: self.on_shape())
        self.build_len_label = ttk.Label(r, text="Points:")
        self.build_len_label.pack(side="left", padx=(10, 2))
        self.build_pts = tk.StringVar(value="10000")
        ttk.Entry(r, textvariable=self.build_pts, width=9).pack(side="left")
        self.build_unit = tk.StringVar(value="us")
        self.build_unit_box = ttk.Combobox(
            r, textvariable=self.build_unit, values=list(SEQ_UNITS), width=4,
            state="disabled")
        self.build_unit_box.pack(side="left", padx=(2, 0))
        self.build_unit_box.bind("<<ComboboxSelected>>",
                                 lambda e: self.on_build_change())
        ttk.Button(r, text="Build", command=self.do_build).pack(side="left",
                                                                padx=(10, 4))
        ttk.Button(r, text="Type/paste values...",
                   command=self.do_paste).pack(side="left")
        ttk.Button(r, text="Sequence...",
                   command=self.do_sequence).pack(side="left", padx=(4, 0))

        # A carrier in cycles is rate-free and survives being replayed at any
        # clock, which is why the builder works that way. But a carrier is
        # usually known in hertz, and a record has a duration the moment a
        # clock is named, so the panel will do the conversion either way.
        c = ttk.Frame(f)
        c.pack(fill="x", padx=6, pady=(0, 2))
        self.build_real = tk.BooleanVar(value=False)
        ttk.Checkbutton(c, text="Using real units with",
                        variable=self.build_real,
                        command=self.on_build_units).pack(side="left")
        self.build_rate = tk.StringVar(value="1e6")
        ttk.Entry(c, textvariable=self.build_rate, width=10).pack(side="left",
                                                                  padx=(4, 2))
        ttk.Label(c, text="Sa/s").pack(side="left")
        # Wrapped rather than clipped: a warning that runs off the edge of the
        # window is a warning nobody reads. The full text goes to the log on
        # every build too, since this line is replaced by the next one.
        self.build_note = ttk.Label(c, text="", foreground="#666",
                                    justify="left", wraplength=520)
        self.build_note.pack(side="left", padx=(10, 0))
        for var in (self.build_pts, self.build_rate):
            var.trace_add("write", lambda *_: self.on_build_change())

        g = ttk.Frame(f)
        g.pack(fill="x", padx=6, pady=(2, 6))
        self.shape_labels, self.shape_vars, self.shape_boxes = [], [], []
        for slot in range(BUILD_SLOTS):
            row, col = divmod(slot, 3)
            lab = ttk.Label(g, text="")
            lab.grid(row=row, column=col * 2, sticky="e", padx=(0, 4), pady=1)
            var = tk.StringVar()
            box = ttk.Combobox(g, textvariable=var, width=13, state="normal")
            box.grid(row=row, column=col * 2 + 1, sticky="w", padx=2, pady=1)
            var.trace_add("write", lambda *_: self.on_build_change())
            self.shape_labels.append(lab)
            self.shape_vars.append(var)
            self.shape_boxes.append(box)
        self.on_shape()

    def on_shape(self, keep=False):
        """Relabel the parameter slots for the shape now selected.

        `keep` leaves the values alone, for the case where only the carrier's
        units changed and the shape did not.
        """
        spec = BUILD_SHAPES[self.shape.get()][1]
        for slot in range(BUILD_SLOTS):
            lab = self.shape_labels[slot]
            var = self.shape_vars[slot]
            box = self.shape_boxes[slot]
            if slot < len(spec):
                label, key, default, choices = spec[slot]
                if key == "cycles" and self.build_real.get():
                    label = "Carrier (Hz)"
                lab.configure(text=label + ":", foreground="#000")
                box.configure(values=list(choices) if choices else (),
                              state="readonly" if choices else "normal")
                if not keep:
                    var.set(default)
            else:
                lab.configure(text="")
                box.configure(values=(), state="disabled")
                if not keep:
                    var.set("")
        self.on_build_change()

    def build_rate_value(self):
        rate = _as_float(self.build_rate.get(), 0.0)
        if rate <= 0:
            raise ValueError("real units need a positive sample rate")
        return rate

    def build_points(self):
        """How many samples the record will have, however it was asked for.

        In real units the length box holds a time, because that is what setting
        it actually changes: at a fixed clock a longer record is a longer pulse,
        not a finer one. The point count falls out of the clock.
        """
        if not self.build_real.get():
            return int(_as_float(self.build_pts.get(), 0.0))
        span = parse_time(self.build_pts.get(), self.build_unit.get(), 0.0)
        return int(round(span * self.build_rate_value()))

    def build_values(self):
        """The shape's parameters, with a carrier in hertz turned into cycles.

        The library only knows cycles across the record, that being the one
        description of a carrier which needs no clock. Hertz is the useful one
        to type, so the conversion happens here: cycles = frequency x points /
        rate.
        """
        spec = BUILD_SHAPES[self.shape.get()][1]
        values = {spec[i][1]: self.shape_vars[i].get()
                  for i in range(len(spec))}
        carrier = None
        if self.build_real.get() and "cycles" in values:
            freq = _as_float(values["cycles"], 0.0)
            if freq > 0:
                points = self.build_points()
                carrier = freq
                values["cycles"] = f"{freq * points / self.build_rate_value():.10g}"
        return values, carrier

    def on_build_units(self):
        """Switch the length and the carrier between points and real time.

        The record itself does not change: the same 10000 points at 1 MSa/s is
        10 ms either way, and the same 50 cycles across it is 5 kHz. Converting
        rather than clearing means the box that was right a moment ago is still
        right.
        """
        try:
            rate = self.build_rate_value()
        except ValueError as exc:
            self.build_real.set(not self.build_real.get())
            messagebox.showerror("Cannot use real units", str(exc))
            return
        spec = BUILD_SHAPES[self.shape.get()][1]
        cycles_at = next((i for i in range(len(spec))
                          if spec[i][1] == "cycles"), None)
        real = self.build_real.get()

        if real:
            points = int(_as_float(self.build_pts.get(), 0.0))
            unit = _TIME_UNITS[self.build_unit.get()]
            self.build_pts.set(f"{points / rate / unit:.10g}" if points else "")
            if cycles_at is not None and points:
                cycles = _as_float(self.shape_vars[cycles_at].get(), 0.0)
                self.shape_vars[cycles_at].set(f"{cycles * rate / points:.10g}")
        else:
            # Not build_points(): the checkbox has already flipped, so that
            # would read the box as a point count when it is still a time.
            points = int(round(parse_time(self.build_pts.get(),
                                          self.build_unit.get(), 0.0) * rate))
            if cycles_at is not None and points:
                freq = _as_float(self.shape_vars[cycles_at].get(), 0.0)
                self.shape_vars[cycles_at].set(f"{freq * points / rate:.10g}")
            self.build_pts.set(str(points))

        self.build_len_label.configure(text="Length:" if real else "Points:")
        self.build_unit_box.configure(state="readonly" if real else "disabled")
        self.on_shape(keep=True)

    def on_build_change(self):
        """Say what the record will come to, and what it cannot resolve."""
        if not hasattr(self, "build_note"):
            return                     # still building the panel
        try:
            values, carrier = self.build_values()
            points = self.build_points()
        except ValueError as exc:
            self.build_note.configure(text=str(exc), foreground="#c60")
            return
        rate = _as_float(self.build_rate.get(), 0.0)
        parts = []
        if points >= 2 and rate > 0:
            # Played straight through at this clock, a record of n points lasts
            # n/rate and repeats at rate/n. Both are worth knowing before it is
            # built rather than after it is on the generator.
            parts.append(f"{points:,} pts = {_secs(points / rate)}, "
                         f"repeats at {_hz(rate / points)}")
        warnings = build_warnings(self.shape.get(), points, values, rate, carrier)
        if warnings:
            self.build_note.configure(
                text=" | ".join(parts + warnings[:1]), foreground="#c60")
        else:
            self.build_note.configure(text=" | ".join(parts), foreground="#666")

    def do_build(self):
        shape = self.shape.get()
        spec = BUILD_SHAPES[shape][1]
        try:
            values, carrier = self.build_values()
            points = self.build_points()
            data = build_waveform(shape, points, values)
        except Exception as exc:
            self.log(f"Could not build {shape}: {exc}")
            messagebox.showerror("Cannot build waveform", str(exc))
            return
        detail = ", ".join(f"{spec[i][0]}={self.shape_vars[i].get()}"
                           for i in range(len(spec)) if self.shape_vars[i].get())
        self.log(f"Built {shape}: {data.size} pts"
                 + (f" ({detail})" if detail else ""))
        rate = _as_float(self.build_rate.get(), 0.0)
        if carrier:
            self.log(f"  carrier {_hz(carrier)} at {rate:.10g} Sa/s "
                     f"= {values['cycles']} cycles across the record")
        # Logged as well as shown, because the label is one line and goes away
        # the moment the next thing is built.
        for line in build_warnings(shape, points, values, rate, carrier):
            self.log(f"  warning: {line}")
        self.take_table(data.reshape(-1, 1), None, f"built {shape}",
                        shape.split("(")[0].strip().replace(" ", "_").lower())

    def do_paste(self):
        """The pending record's numbers, to read, scroll and edit by hand.

        Opens holding whatever is pending rather than empty, which makes it the
        way to look at a waveform point by point and change one. Type into an
        empty box and it is what it always was.
        """
        pending = self.arb_samples
        dlg = tk.Toplevel(self.root)
        dlg.title("Type or paste values")
        dlg.transient(self.root)
        dlg.grab_set()

        note = ""
        if pending is not None and pending.size > PASTE_MAX_LINES:
            note = (f"\n\n{pending.size:,} points is too many to put in a text "
                    f"box, so this one is empty. Anything typed here replaces "
                    f"the lot.")
        elif pending is not None:
            note = (f"\n\nShowing the {pending.size:,} pending point(s). Edit "
                    f"and press Use, or Clear and start again.")
        ttk.Label(dlg, justify="left", text=(
            "One sample per line, a single row of numbers, or columns separated\n"
            "by commas, semicolons or spaces. A non-numeric first line is read\n"
            "as column names. Blank lines and # comments are skipped." + note)
        ).pack(anchor="w", padx=8, pady=(8, 4))

        box = ttk.Frame(dlg)
        box.pack(fill="both", expand=True, padx=8)
        txt = tk.Text(box, width=58, height=16, wrap="none", font=("Consolas", 9))
        bar = ttk.Scrollbar(box, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=bar.set)
        txt.pack(side="left", fill="both", expand=True)
        bar.pack(side="left", fill="y")
        if pending is not None and pending.size <= PASTE_MAX_LINES:
            txt.insert("1.0", "\n".join(f"{v:.7g}" for v in pending) + "\n")

        row = ttk.Frame(dlg)
        row.pack(fill="x", padx=8, pady=8)

        def use():
            try:
                table, names = parse_pasted(txt.get("1.0", "end"))
            except Exception as exc:
                messagebox.showerror("Cannot read values", str(exc), parent=dlg)
                return
            dlg.destroy()
            self.take_table(table, names, "pasted values", "pasted")

        ttk.Button(row, text="Use these values", command=use).pack(side="left")
        ttk.Button(row, text="Clear",
                   command=lambda: txt.delete("1.0", "end")).pack(side="left",
                                                                  padx=6)
        ttk.Button(row, text="Cancel", command=dlg.destroy).pack(side="left")
        txt.focus_set()

    # -- sequence editor ---------------------------------------------------

    def do_sequence(self):
        """A run of differently-specified segments, laid end to end.

        Its own window rather than another panel row, because a sequence is a
        table and the panel has nowhere to put one. Deliberately not modal: the
        main preview is right behind it, so each Build redraws the picture the
        sequence is being tuned against.
        """
        if self.seq_win is not None and self.seq_win.winfo_exists():
            self.seq_win.lift()
            self.seq_win.focus_force()
            return
        if not self.seq_rate.get().strip():
            # Whatever FSMP holds is the likeliest answer, since that is the
            # rate the record will play at.
            rate = self.vars["FSMP"].get().strip()
            self.seq_rate.set(rate if _as_float(rate, 0.0) > 0 else "1e6")

        win = self.seq_win = tk.Toplevel(self.root)
        win.title("Build a sequence")
        win.transient(self.root)
        win.protocol("WM_DELETE_WINDOW", self._seq_close)

        ttk.Label(win, justify="left", foreground="#444", text=(
            "Segments are laid end to end into one record, each with its own "
            "shape, length, amplitude and carrier.\n"
            "Times are in the unit below; a suffix overrides it for one field "
            "(2u, 200m, 1.5s). Carrier is in hertz,\n"
            "which only means anything at the sample rate given. Extra takes "
            "the shape's own parameters as\n"
            "key=value. Picking a shape fills in that shape's own defaults,\n"
            "so the row says what it will accept. name=pending reaches "
            "whatever was last built, loaded or pasted.")
        ).pack(anchor="w", padx=8, pady=(8, 4))

        top = ttk.Frame(win)
        top.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Label(top, text="Sample rate:").pack(side="left")
        ttk.Entry(top, textvariable=self.seq_rate, width=10).pack(side="left",
                                                                  padx=(3, 1))
        ttk.Label(top, text="Sa/s").pack(side="left", padx=(0, 10))
        ttk.Label(top, text="Times in:").pack(side="left")
        ttk.Combobox(top, textvariable=self.seq_unit, values=list(SEQ_UNITS),
                     width=4, state="readonly").pack(side="left", padx=(3, 10))
        ttk.Label(top, text="Baseline:").pack(side="left")
        ttk.Entry(top, textvariable=self.seq_baseline, width=6).pack(
            side="left", padx=(3, 10))

        opts = ttk.Frame(win)
        opts.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Checkbutton(
            opts, variable=self.seq_coherent,
            text="phase coherent (reference every carrier to the sequence "
                 "start, not to its own segment)").pack(anchor="w")
        ttk.Checkbutton(
            opts, variable=self.seq_set_clock,
            text="set the panel to ARB at this sample rate (FSMP) when "
                 "building").pack(anchor="w")

        holder = ttk.Frame(win)
        holder.pack(fill="both", expand=True, padx=8, pady=(2, 2))
        canvas = self.seq_canvas = tk.Canvas(holder, highlightthickness=0,
                                             height=200)
        bar = ttk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.seq_body = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=self.seq_body, anchor="nw")
        self.seq_body.bind("<Configure>", lambda e: canvas.configure(
            scrollregion=canvas.bbox("all")))
        # Bound only while the pointer is over the list, so the wheel still
        # belongs to whatever is under it everywhere else.
        canvas.bind("<Enter>", lambda e: canvas.bind_all(
            "<MouseWheel>",
            lambda ev: canvas.yview_scroll(-1 * (ev.delta // 120), "units")))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        ttk.Label(win, foreground="#666",
                  text="^ v move a segment    D duplicate    X delete").pack(
                      anchor="w", padx=10, pady=(0, 2))

        tools = ttk.Frame(win)
        tools.pack(fill="x", padx=8, pady=(0, 2))
        ttk.Button(tools, text="+ Add segment",
                   command=self._seq_add).pack(side="left")
        ttk.Button(tools, text="Paste spec...",
                   command=self._seq_paste).pack(side="left", padx=(6, 0))
        ttk.Button(tools, text="Copy spec",
                   command=self._seq_copy).pack(side="left", padx=(6, 0))
        self.seq_note = ttk.Label(tools, text="", foreground="#666")
        self.seq_note.pack(side="left", padx=12)

        buttons = ttk.Frame(win)
        buttons.pack(fill="x", padx=8, pady=(2, 8))
        ttk.Button(buttons, text="Build", command=self._seq_build).pack(
            side="left")
        ttk.Button(buttons, text="Close", command=self._seq_close).pack(
            side="left", padx=6)

        self._seq_redraw()
        win.update_idletasks()
        win.minsize(win.winfo_reqwidth(), win.winfo_reqheight())

    def _seq_close(self):
        if self.seq_win is not None:
            self.seq_win.destroy()
        self.seq_win, self.seq_note, self.seq_canvas = None, None, None
        self.seq_vars = []

    def _seq_redraw(self):
        """Rebuild the rows from the data.

        Cheaper to think about than patching widgets in place: reorder, insert
        and delete all become one list operation followed by a redraw, and the
        row numbers cannot drift out of step with the list.
        """
        for child in self.seq_body.winfo_children():
            child.destroy()
        # The variables have to be held here: a StringVar that is garbage
        # collected takes its Tcl variable with it, and the row goes blank.
        self.seq_vars = []

        heads = ["#"] + [label for label, _, _, _ in SEQ_COLUMNS]
        for col, text in enumerate(heads):
            ttk.Label(self.seq_body, text=text, foreground="#444").grid(
                row=0, column=col, sticky="w", padx=2, pady=(0, 2))

        for index, seg in enumerate(self.seq_data):
            ttk.Label(self.seq_body, text=str(index + 1),
                      foreground="#666").grid(row=index + 1, column=0,
                                              padx=(2, 4))
            row_vars = {}
            self.seq_vars.append(row_vars)
            for col, (_, key, _, width) in enumerate(SEQ_COLUMNS, start=1):
                var = tk.StringVar(value=str(seg.get(key, "")))
                var.trace_add("write", lambda *_, i=index, k=key, v=var:
                              self._seq_edit(i, k, v))
                row_vars[key] = var
                if key == "shape":
                    widget = ttk.Combobox(self.seq_body, textvariable=var,
                                          values=seq_shapes(), width=width,
                                          height=len(seq_shapes()),
                                          state="readonly")
                else:
                    widget = ttk.Entry(self.seq_body, textvariable=var,
                                       width=width)
                widget.grid(row=index + 1, column=col, sticky="w", padx=2,
                            pady=1)
            row_tools = ttk.Frame(self.seq_body)
            row_tools.grid(row=index + 1, column=len(SEQ_COLUMNS) + 1,
                           padx=(8, 2))
            for text, call in (("^", self._seq_up), ("v", self._seq_down),
                               ("D", self._seq_dup), ("X", self._seq_del)):
                ttk.Button(row_tools, text=text, width=2,
                           command=lambda f=call, i=index: f(i)).pack(
                               side="left")

        # A canvas has no opinion about how wide the frame scrolling inside it
        # is, so without this the row buttons sit past the right edge with no
        # way to reach them - the scrollbar only moves the view up and down.
        if self.seq_canvas is not None:
            self.seq_body.update_idletasks()
            self.seq_canvas.configure(width=self.seq_body.winfo_reqwidth())
        self._seq_info()

    def _seq_edit(self, index, key, var):
        if not 0 <= index < len(self.seq_data):
            return
        self.seq_data[index][key] = var.get()
        if key == "shape":
            # A new shape takes different parameters, so the ones the last
            # shape took are not just stale, they are meaningless. Filling in
            # the new defaults is also the only place the row says what the
            # shape will accept.
            #
            # Written into the box where it stands rather than by rebuilding
            # the row. Rebuilding destroyed the combobox whose callback this
            # is, and a readonly combobox holds a grab while its dropdown
            # closes - destroy it first and the grab is never released, which
            # left every box in the window deaf to the keyboard until it was
            # clicked away from and back.
            row = self.seq_vars[index] if index < len(self.seq_vars) else {}
            defaults = shape_extras(var.get())
            if "extra" in row:
                row["extra"].set(defaults)
            else:
                self.seq_data[index]["extra"] = defaults
        self._seq_info()

    def _seq_add(self):
        self.seq_data.append(dict(SEQ_DEFAULT))
        self._seq_redraw()

    def _seq_dup(self, index):
        self.seq_data.insert(index + 1, dict(self.seq_data[index]))
        self._seq_redraw()

    def _seq_del(self, index):
        self.seq_data.pop(index)
        if not self.seq_data:
            self.seq_data.append(dict(SEQ_DEFAULT))
        self._seq_redraw()

    def _seq_up(self, index):
        if index > 0:
            self.seq_data[index - 1:index + 1] = [self.seq_data[index],
                                                  self.seq_data[index - 1]]
            self._seq_redraw()

    def _seq_down(self, index):
        if index < len(self.seq_data) - 1:
            self.seq_data[index:index + 2] = [self.seq_data[index + 1],
                                              self.seq_data[index]]
            self._seq_redraw()

    def _seq_info(self):
        """The running total, recomputed on every keystroke."""
        if self.seq_note is None:
            return
        count, span, points = sequence_extent(self.seq_data,
                                              self.seq_rate.get(),
                                              self.seq_unit.get())
        text = (f"{count} segment{'' if count == 1 else 's'}, "
                f"{_secs(span)}, {points} pts")
        colour = "#666"
        if points > MAX_ARB_POINTS:
            text += (f" - past the DS345's {MAX_ARB_POINTS:,}-point memory; "
                     "lower the rate or shorten the sequence")
            colour = "#c60"
        self.seq_note.configure(text=text, foreground=colour)

    def _seq_build(self):
        """Make the record and hand it to the pending waveform."""
        try:
            # `pending` names whatever was last built, loaded or pasted, so a
            # record that has never been uploaded can still be a segment.
            waves = dict(self.known_waves)
            if self.arb_element is not None:
                waves["pending"] = self.arb_element
            out, rows = build_sequence(
                self.seq_data, self.seq_rate.get(), unit=self.seq_unit.get(),
                baseline=_as_float(self.seq_baseline.get(), 0.0),
                coherent=bool(self.seq_coherent.get()), waves=waves)
        except Exception as exc:
            self.log(f"Could not build the sequence: {exc}")
            messagebox.showerror("Cannot build sequence", str(exc),
                                 parent=self.seq_win)
            return

        rate = _as_float(self.seq_rate.get(), 0.0)
        self.log(f"Sequence: {len(rows)} segments, {out.size} pts, "
                 f"{_secs(out.size / rate)} at {rate:.10g} Sa/s"
                 + (" (phase coherent)" if self.seq_coherent.get() else ""))
        for index, shape, n, span, gap in rows:
            self.log(f"  {index}. {shape}, {_secs(span)} ({n} pts)"
                     + (f", then {_secs(gap)} gap" if gap > 0 else ""))
        element = self.arb_element
        self.take_table(out.reshape(-1, 1), None, "sequence", "sequence")
        # A sequence must never become its own source. Left alone, a segment
        # naming `pending` would fold the last result into the next one and the
        # record would double every time Build was pressed.
        self.arb_element = element
        if self.seq_set_clock.get():
            self._seq_set_clock(rate)

    def _seq_set_clock(self, rate):
        """Put the clock the sequence was designed for into the panel.

        A sequence is specified in seconds, and seconds only exist on this
        generator through FSMP: the record plays one point per 1/FSMP, so the
        same rows at a different clock are a different waveform. FSMP itself
        only takes exact divisors of 40 MHz and rounds anything else, so the
        nearest divisor is what goes in the box - with a log line when that
        moved the rate, because a sequence built for 3 MHz playing at
        40/13 MHz is exactly the silent kind of wrong this panel exists to
        say out loud. Left as unapplied edits rather than sent, so it carries
        the same asterisks as anything typed and Apply is still the thing
        that changes the instrument.
        """
        actual = rate
        if 0 < rate <= FSMP_MAX:
            divisor = max(1, int(round(FSMP_MAX / rate)))
            actual = FSMP_MAX / divisor
        cells = (("FUNC", "ARB"), ("FSMP", f"{actual:.10g}"))
        for key, value in cells:
            self.vars[key].set(value)
        # Marked afterwards, not as each one is written: setting a var runs
        # on_edit, which clears the mark on the assumption a write is somebody
        # typing.
        self.computed.update(key for key, _ in cells)
        self.refresh_marks()
        note = ""
        if abs(actual - rate) > 1e-6 * max(rate, 1.0):
            note = (f" (FSMP only takes 40 MHz / N, so {rate:.10g} became "
                    f"{actual:.10g} - times will scale by "
                    f"{rate / actual:.6g})")
        self.log(f"  panel set to ARB at {actual:.10g} Sa/s - not applied yet"
                 + note)

    def _seq_spec(self):
        """The sequence as text: the rows, and the settings that frame them."""
        return format_sequence_spec(
            self.seq_data, self.seq_rate.get().strip(), self.seq_unit.get(),
            baseline=self.seq_baseline.get().strip(),
            coherent=bool(self.seq_coherent.get()),
            clock=bool(self.seq_set_clock.get()))

    def _seq_apply_settings(self, settings):
        """Put a pasted spec's settings into the boxes that hold them.

        Only what the text actually mentioned: a spec written by hand with
        nothing but rows leaves the window as it was, rather than resetting it
        to defaults nobody asked for.
        """
        for name, var in (("rate", self.seq_rate),
                          ("baseline", self.seq_baseline)):
            if settings.get(name):
                var.set(settings[name])
        unit = settings.get("unit", "").strip().lower()
        if unit in SEQ_UNITS:
            self.seq_unit.set(unit)
        for name, var in (("coherent", self.seq_coherent),
                          ("clock", self.seq_set_clock)):
            if name in settings:
                var.set(_as_bool(settings[name], bool(var.get())))

    def _seq_copy(self):
        text = self._seq_spec()
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.log(f"Sequence spec copied to the clipboard "
                 f"({len(self.seq_data)} segments)")

    def _seq_paste(self):
        """Type or paste a whole sequence at once.

        Opens holding the sequence that is there now, so the same box is both
        how a sequence gets in and how it gets read out to keep beside the data
        it produced.
        """
        dlg = tk.Toplevel(self.seq_win)
        dlg.title("Sequence spec")
        dlg.transient(self.seq_win)
        dlg.grab_set()
        ttk.Label(dlg, justify="left", text=(
            "One segment per line: " + ", ".join(
                key for _, key, _, _ in SEQ_COLUMNS) + ".\n"
            "Everything after the sixth comma is the extra field, so a value "
            "with commas of its own survives.\n"
            "The `# rate:` lines carry the settings above the list and are "
            "read back with the rows; other\n"
            "# comments and blank lines are skipped. Using this replaces the "
            "whole sequence.")
        ).pack(anchor="w", padx=8, pady=(8, 4))

        box = ttk.Frame(dlg)
        box.pack(fill="both", expand=True, padx=8)
        txt = tk.Text(box, width=76, height=16, wrap="none",
                      font=("Consolas", 9))
        scroll = ttk.Scrollbar(box, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=scroll.set)
        txt.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")
        txt.insert("1.0", self._seq_spec())

        row = ttk.Frame(dlg)
        row.pack(fill="x", padx=8, pady=8)

        def use():
            try:
                data, settings = parse_sequence_spec(txt.get("1.0", "end"))
            except Exception as exc:
                messagebox.showerror("Cannot read sequence", str(exc),
                                     parent=dlg)
                return
            dlg.destroy()
            self.seq_data = data
            # Settings before the redraw: the running total under the list is
            # computed against the rate and the unit, so landing the rows first
            # would show a length that was never true.
            self._seq_apply_settings(settings)
            self._seq_redraw()
            named = ", ".join(sorted(settings)) if settings else "no settings"
            self.log(f"Sequence spec read: {len(data)} segments ({named})")

        ttk.Button(row, text="Use this sequence", command=use).pack(side="left")
        ttk.Button(row, text="Cancel", command=dlg.destroy).pack(side="left",
                                                                 padx=6)
        txt.focus_set()

    def build_arb(self, parent, pad):
        f = ttk.LabelFrame(parent, text="Upload arbitrary waveform")
        f.pack(fill="x", **pad)
        r1 = ttk.Frame(f)
        r1.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Button(r1, text="Load file...", command=self.pick_arb).pack(
            side="left")
        ttk.Label(r1, text="Column:").pack(side="left", padx=(10, 2))
        # Which column holds the samples. A multi-channel scope capture has
        # several plausible answers, so it is shown rather than guessed at
        # silently - picking the wrong one uploads the trigger trace.
        self.arb_col = tk.StringVar()
        self.arb_col_box = ttk.Combobox(r1, textvariable=self.arb_col, width=16,
                                        state="disabled")
        self.arb_col_box.pack(side="left")
        self.arb_col_box.bind("<<ComboboxSelected>>",
                              lambda e: self.pick_column())
        self.arb_info = ttk.Label(r1, text="no file loaded", foreground="#666")
        self.arb_info.pack(side="left", padx=8)

        r2 = ttk.Frame(f)
        r2.pack(fill="x", padx=6, pady=(2, 6))
        # The DS345's arb slot is anonymous, so the name is purely for the
        # local copy that lets the preview draw this record again later.
        ttk.Label(r2, text="Local name:").pack(side="left")
        self.arb_name = tk.StringVar(value="wave1")
        ttk.Entry(r2, textvariable=self.arb_name, width=18).pack(side="left",
                                                                 padx=4)
        self.norm = tk.BooleanVar(value=True)
        ttk.Checkbutton(r2, text="normalise to full scale", variable=self.norm,
                        command=self.on_norm).pack(side="left", padx=8)
        self.upload_btn = ttk.Button(r2, text="Upload", command=self.do_upload,
                                     state="disabled")
        self.upload_btn.pack(side="left", padx=4)
        ttk.Button(r2, text="Clear pending",
                   command=self.do_clear_pending).pack(side="left")

    def build_memory(self, parent, pad):
        f = ttk.LabelFrame(parent, text="Local waveform copies")
        f.pack(fill="both", expand=True, **pad)

        # Controls first, from the bottom up: the list is the part that should
        # give up space when the column runs short, not the buttons.
        ttk.Label(f, foreground="#666", justify="left", text=(
            "The DS345 holds one arbitrary waveform, anonymously, and cannot\n"
            "read it back out - so these local copies are the only record of\n"
            "what has been sent. Uploading anything replaces the one on the\n"
            "generator.")
        ).pack(side="bottom", anchor="w", padx=6, pady=(0, 6))

        row = ttk.Frame(f)
        row.pack(side="bottom", fill="x", padx=6, pady=2)
        self.mem_btn = ttk.Button(row, text="Refresh",
                                  command=self.do_refresh_memory)
        self.mem_btn.pack(side="left")
        ttk.Button(row, text="Forget local copy",
                   command=self.do_forget).pack(side="left", padx=6)
        ttk.Button(row, text="Load to pending",
                   command=self.do_use_wave).pack(side="left")

        box = ttk.Frame(f)
        box.pack(fill="both", expand=True, padx=6, pady=(6, 0))
        self.mem_list = tk.Listbox(box, height=4, font=("Consolas", 9),
                                   exportselection=False)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.mem_list.yview)
        self.mem_list.configure(yscrollcommand=sb.set)
        self.mem_list.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")

    def build_preview(self, parent, pad):
        f = ttk.LabelFrame(parent, text="Preview (what Apply would produce)")
        f.pack(fill="x", **pad)
        if Figure is None:
            ttk.Label(f, text="matplotlib not installed - no preview",
                      foreground="#666").pack(padx=8, pady=8)
            self.canvas = None
            return
        self.fig = Figure(figsize=(5.6, 3.0), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.fig.subplots_adjust(left=0.14, right=0.96, top=0.90, bottom=0.18)
        self.canvas = FigureCanvasTkAgg(self.fig, master=f)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=4,
                                         pady=4)

        r = ttk.Frame(f)
        r.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Label(r, text="Show:").pack(side="left")
        self.show_out = tk.BooleanVar(value=True)
        tk.Checkbutton(r, text="output", variable=self.show_out,
                       command=self.draw_preview, foreground=OUT_COLOUR,
                       activeforeground=OUT_COLOUR,
                       selectcolor="white").pack(side="left", padx=(4, 0))
        self.show_pending = tk.BooleanVar(value=True)
        ttk.Checkbutton(r, text="pending", variable=self.show_pending,
                        command=self.draw_preview).pack(side="left", padx=(8, 0))
        # Sits with the trace switches because that is where it can change on
        # every keystroke without a log filling up behind it. Two kinds of
        # warning share the line: a limit the generator will refuse, and a
        # caveat on the drawing itself. The refusals go first - one of them
        # means Apply will not do what the panel says, and the other only
        # means the picture of it is coarse.
        self.warn_note = ttk.Label(r, text="", foreground="#c60",
                                   wraplength=520, justify="left")
        self.warn_note.pack(side="left", padx=(14, 4))

    # -- helpers -----------------------------------------------------------

    def log(self, text):
        self.msgs.put(text)

    def pump(self):
        while not self.msgs.empty():
            self.logbox.insert("end", self.msgs.get() + "\n", "entry")
            self.logbox.see("end")
        self.root.after(100, self.pump)

    def pick_dir(self):
        d = filedialog.askdirectory(initialdir=self.outdir.get() or ".")
        if d:
            self.outdir.set(d)
            self.save_config()

    def on_edit(self, key, on_change):
        if on_change:
            on_change()
        # A rewrite the panel did to itself (moving the mode row's values to
        # where the new mode keeps them) is not an edit, and redrawing halfway
        # through one would show a row that is part old mode and part new.
        if self.quiet:
            return
        # A value the panel worked out stops being one the moment it is typed
        # over, so the tint comes off at the first keystroke in the box.
        self.computed.discard(key)
        if not self.loading:
            # The cell just typed in is the setting, and everything describing
            # the same thing another way becomes the arithmetic. Not while a
            # read is landing: the generator reporting every cell at once is
            # not a choice between them.
            for group in self.groups_for(key):
                self.driver[group] = key
            if key in ("FREQ", "PERI", "FUNC"):
                self.recompute()
        self.show_burst_idle()
        self.refresh_marks()
        self.draw_preview()

    def edited(self, key):
        """True if the panel value differs from what the generator last said."""
        return self.vars[key].get().strip() != self.inst_vals[key]

    def linked_groups(self):
        """Cells that are one setting in different guises.

        The DS345 has exactly one such pair on its panel: frequency and
        period are reciprocals, and which you would rather type depends on
        what you are setting up. Only FREQ is ever sent - the generator has
        no period command - so the period side is always the arithmetic.
        Computed rather than a constant because the pair is only real for the
        functions that have a frequency at all.
        """
        func = self.vars["FUNC"].get().strip().upper()
        if func in OSCILLATING:
            return [(("FREQ",), ("PERI",))]
        return []

    def groups_for(self, key):
        return [group for group in self.linked_groups()
                if any(key in side for side in group)]

    def driving_side(self, group):
        """The side of a group that is currently the setting."""
        driver = self.driver.get(group)
        for side in group:
            if driver in side:
                return side
        return group[0]

    def derived(self, key):
        """True when some other cell is the setting and this one is its result.

        Never sent - the generator only speaks FREQ, and the panel's period
        box is the arithmetic beside it.
        """
        return any(key not in self.driving_side(group)
                   for group in self.groups_for(key))

    def is_computed(self, key):
        """True when this cell shows arithmetic rather than a setting.

        Two ways a number lands in a box without being typed there. It is the
        far side of the frequency/period pair. Or this app worked it out,
        which is what the sequence builder does with FSMP; that one is sent,
        so it is not `derived`.
        """
        return key in self.computed or self.derived(key)

    def recompute(self):
        """Bring the derived side of the frequency/period pair into step.

        Showing a period beside a frequency only earns its place if setting
        either fills in the other; two boxes left to disagree would be worse
        than one box.
        """
        def num(key):
            try:
                return float(self.vars[key].get().strip())
            except ValueError:
                return None

        drives = {key for group in self.linked_groups()
                  for key in self.driving_side(group)}
        new = {}
        if "FREQ" in drives:
            freq = num("FREQ")
            new["PERI"] = 1.0 / freq if freq else None
        elif "PERI" in drives:
            peri = num("PERI")
            new["FREQ"] = 1.0 / peri if peri else None

        self.quiet = True
        try:
            for key, value in new.items():
                var = self.vars[key]
                text = "" if value is None else fmt_value(value)
                if var.get().strip() != text:
                    var.set(text)
        finally:
            self.quiet = False

    def unapplied(self):
        """The cells holding an edit that has not been sent.

        The same rule the asterisks use: a greyed cell is not part of the
        current settings and a derived one moved because something else was
        typed, so neither is an edit waiting to go anywhere.
        """
        return [key for key in self.marks
                if str(self.widgets[key].cget("state")) != "disabled"
                and not self.derived(key) and self.edited(key)]

    def refresh_marks(self):
        if not hasattr(self, "sync"):
            return          # still building the panel
        pending = 0
        for key, mark in self.marks.items():
            live = str(self.widgets[key].cget("state")) != "disabled"
            halo = self.halos.get(key)
            if halo is not None:
                # A greyed cell is not part of the current settings, so it is
                # neither an edit nor a computed value - just what was left
                # behind by the previous function.
                halo.configure(bg=COMPUTED_BG if live and self.is_computed(key)
                               else self.halo_off)
            if not live or self.derived(key):
                mark.configure(text=" ")
                continue
            if self.edited(key):
                pending += 1
                mark.configure(text="*")
            else:
                mark.configure(text=" ")
        if not self.read_stamp:
            self.sync.configure(text="not read yet", foreground="#666")
        elif pending:
            self.sync.configure(
                text=f"{pending} edit(s) not applied - press Apply changes",
                foreground="#c60")
        else:
            self.sync.configure(text=f"in sync ({self.read_stamp})",
                                foreground="#060")

    def on_function(self):
        """Grey out every cell the current function gives no meaning to.

        FREQ, the period beside it and the phase belong to the four standard
        functions; NOISE has no frequency to set and an arb's timing is FSMP
        over its length. FSMP is the other way round. The amplitude unit is
        pinned to Vpp under ARB because the manual only allows Vpp there.
        """
        func = self.vars["FUNC"].get().strip().upper()
        mode = self.vars["MODE"].get().strip() or "Off"
        # Phase additionally needs a mode that leaves the phase alone: under
        # a sweep, FM or PM the instrument owns the phase and refuses PHSE.
        phase_ok = func in OSCILLATING and mode in ("Off", "AM", "Burst")
        for key, _, applies in OUT_PARAMS:
            on = func in applies
            if key == "PHSE":
                on = phase_ok
            self.enable(key, on)
            self.labels[key].configure(foreground="#000" if on else "#aaa")

        arb = func == "ARB"
        if arb and not self.loading and \
                self.vars["AMPL_UNIT"].get().strip() != "VP":
            self.vars["AMPL_UNIT"].set("VP")
            self.log("Amplitude unit set to VP - an arb's amplitude can only "
                     "be given peak-to-peak")
        self.enable("AMPL_UNIT", not arb)
        self.labels["AMPL_UNIT"].configure(
            foreground="#000" if not arb else "#aaa")

        self.check_mode()
        self.show_burst_idle()
        if not self.loading:
            # A different function is a different set of pairs - NOISE loses
            # its period - so the derived cells are worked out again rather
            # than left holding the last function's answers.
            self.recompute()

    def on_mode(self):
        """Relabel the mode parameter slots for the mode now selected, and move
        their values to wherever the new mode keeps them."""
        mode = self.vars["MODE"].get().strip() or "Off"
        spec = MODE_PARAMS.get(mode, [])
        # Not while the panel is being filled from the generator: there the
        # fresh values arrive straight after and carrying the old ones over
        # would fight with them.
        if not self.loading and self.mode_shown != mode:
            self.remap_mode_slots(self.mode_shown, mode)
        self.mode_shown = mode
        for slot in range(MODE_SLOTS):
            lab = getattr(self, f"modlab{slot}")
            w = self.widgets[f"MODE:{slot}"]
            if slot < len(spec):
                label, _, choices = spec[slot]
                lab.configure(text=label + ":", foreground="#000")
                w.configure(values=list(choices) if choices else (),
                            state="readonly" if choices else "normal")
            else:
                lab.configure(text="")
                w.configure(values=(), state="disabled")
        # The mode decides whether the phase cell means anything, so the
        # same greying pass the function runs has to run here too.
        self.on_function_phase_only()
        self.show_burst_idle()
        self.check_mode()

    def on_function_phase_only(self):
        """Just the phase cell's greying, for when only the mode moved."""
        func = self.vars["FUNC"].get().strip().upper()
        mode = self.vars["MODE"].get().strip() or "Off"
        on = func in OSCILLATING and mode in ("Off", "AM", "Burst")
        self.enable("PHSE", on)
        self.labels["PHSE"].configure(foreground="#000" if on else "#aaa")

    def remap_mode_slots(self, old, new):
        """Carry the mode row across a change of mode, matched on meaning.

        The row is one set of boxes reused by every mode, and the modes do not
        agree on the order: AM is (depth, rate, shape) while a sweep is
        (start, stop, rate, ...). Kept by position, a depth of 80 lands under
        the Start (Hz) label and reads as a frequency somebody chose. So a
        value survives only when the new mode has the same command behind it,
        and every other box starts empty rather than holding a number typed
        for something else.
        """
        carried = {}
        for slot, (_, cmd, _) in enumerate(MODE_PARAMS.get(old or "Off", [])):
            value = self.vars[f"MODE:{slot}"].get().strip()
            if value:
                carried[cmd] = value
        spec = MODE_PARAMS.get(new, [])
        self.quiet = True
        try:
            for slot in range(MODE_SLOTS):
                cmd = spec[slot][1] if slot < len(spec) else None
                self.vars[f"MODE:{slot}"].set(
                    carried.get(cmd, "") if cmd else "")
        finally:
            self.quiet = False

    def mode_problem(self):
        """Why this mode row will not take, as (title, message).

        Both rules follow from what the modes act on: a sweep, FM or PM
        moves the output frequency, and NOISE and ARB do not have one -
        noise is noise, and an arb's timing is FSMP. The generator would
        refuse at MENA time with a bare error bit; the panel knows why.
        """
        mode = self.vars["MODE"].get().strip() or "Off"
        func = self.vars["FUNC"].get().strip().upper()
        if func == "NOISE" and mode not in ("Off", "AM"):
            return ("NOISE carrier",
                    f"{mode} needs a carrier with a frequency, and NOISE "
                    f"has none.\n\nOnly AM works on noise.")
        if func == "ARB" and mode in ("FM", "PM", "Lin sweep", "Log sweep"):
            return ("ARB carrier",
                    f"{mode} moves the output frequency, and an arbitrary "
                    f"waveform's timing is the FSMP sample clock instead.\n\n"
                    f"Burst and AM still work with an arb.")
        return ()

    def check_mode(self):
        """Say so, once, each time the panel newly asks for a mode this
        function cannot do.

        Called from both sides of it, since the mode or the function can each
        be the thing that just changed, and only on the way into the bad
        state - otherwise every later keystroke would raise the same box
        again. The box itself is put off to the idle loop: a modal dialog
        runs a nested event loop, and raising one from inside a variable
        trace lets the combobox's own selection land while it is up.
        """
        problem = self.mode_problem()
        fresh = problem and problem != self.mode_bad and not self.loading
        self.mode_bad = problem
        if fresh:
            title, message = problem
            self.log(message.splitlines()[0])
            self.root.after_idle(
                lambda: messagebox.showerror(title, message))

    def burst_idle(self):
        """What the output will sit at between bursts, in volts, or None.

        Not a quirk of this panel - between bursts the DS345 holds the
        carrier at its start phase, and start phase 0 on an arb is its first
        stored sample. So a record that begins high leaves the BNC held high
        until something triggers it, which is worth knowing before that BNC
        is wired into anything that would rather not be held on.
        """
        if self.vars["MODE"].get().strip() != "Burst":
            return None
        func = self.vars["FUNC"].get().strip().upper()
        vals = self.panel_vals()
        arb = self.known_waves.get(self.device_wave) if func == "ARB" else None
        return idle_volts(func, vals, _as_float(vals.get("PHSE"), 0.0),
                          arb=arb,
                          invert=self.vars["INVT"].get().strip() == "ON")

    def show_burst_idle(self):
        """Put that level under the mode row, or clear the line."""
        if getattr(self, "modnote", None) is None:
            return
        volts = self.burst_idle()
        if volts is None:
            self.modnote.configure(text="")
            return
        vals = self.panel_vals()
        vpp = ampl_vpp(self.vars["FUNC"].get().strip().upper(),
                       _as_float(vals.get("AMPL"), 0.0),
                       vals.get("AMPL_UNIT", "VP"))
        # A tenth of a percent of the swing is the DAC's own noise floor, not
        # a level anything downstream will notice.
        if abs(volts) <= max(abs(vpp) * 0.001, 1e-4):
            self.modnote.configure(
                text=f"Between bursts the output rests at {volts:+.3g} V.",
                foreground="#666")
        else:
            self.modnote.configure(
                text=f"Between bursts the output RESTS AT {volts:+.3g} V, not "
                     f"0 V - it holds there until triggered.",
                foreground="#c60")

    def mode_cmd(self, slot):
        """The command the given mode slot currently stands for, or None."""
        mode = self.vars["MODE"].get().strip() or "Off"
        spec = MODE_PARAMS.get(mode, [])
        return spec[slot][1] if slot < len(spec) else None

    def panel_vals(self):
        """The output cells as one {key: string} for the preview helpers."""
        return {key: self.vars[key].get().strip()
                for key in ("FREQ", "AMPL", "AMPL_UNIT", "OFFS", "PHSE",
                            "FSMP")}

    def set_busy(self, busy):
        self.busy = busy
        live = bool(self.awg.inst) and not busy
        state = "normal" if live else "disabled"
        buttons = [self.read_btn, self.apply_btn, self.trig_btn,
                   self.save_btn, self.recall_btn]
        for btn in buttons:
            # Save and Recall live in a window that is usually not open.
            if btn is not None and btn.winfo_exists():
                btn.configure(state=state)
        self.out_btn.configure(state=state)
        # These two are about the bus rather than about a connection: Refresh
        # reads the local Waveforms folder, which works with nothing attached,
        # and Connect is how a connection gets made in the first place. Both
        # are live unless something is already talking to the instrument.
        for btn in (self.mem_btn, self.connect_btn):
            if btn is not None:
                btn.configure(state="disabled" if busy else "normal")
        self.paint_output(self.out_btn, self.out_state)
        self.upload_btn.configure(
            state="normal" if live and self.arb_samples is not None
            else "disabled")

    def paint_output(self, btn, state):
        """Colour the output button by the state it is showing.

        A disabled button is washed out rather than recoloured, so a panel
        with no generator attached still says which way the output was left
        without looking like it could be clicked.
        """
        colour, word = OUT_LOOK.get(state, OUT_LOOK[None])
        if str(btn.cget("state")) == "disabled":
            colour = _lighten(colour, 0.55)
        btn.configure(text=word, bg=colour, fg="#ffffff",
                      activebackground=_lighten(colour, 0.18),
                      activeforeground="#ffffff", disabledforeground="#ffffff")

    def output_state_from(self, ampl_value, ampl_unit, offs):
        """LIVE, MUTED or DC from the generator's own amplitude and offset.

        The DS345 has no output relay to ask about, so the state is read off
        the settings: zero amplitude is the manual's DC-only output, and with
        the offset also zero the BNC is as quiet as this instrument gets. A
        dBm amplitude is never zero output, whatever its number.
        """
        try:
            value = float(ampl_value)
        except (TypeError, ValueError):
            return None
        if str(ampl_unit).upper() == "DB" or value > 0:
            return "LIVE"
        return "DC" if abs(_as_float(offs, 0.0)) > 1e-9 else "MUTED"

    def show_output_state(self, state):
        self.out_state = state
        self.paint_output(self.out_btn, state)

    def flip_output(self):
        """One button, so the click means the other direction.

        LIVE (or DC) -> mute: remember what the generator holds and write
        0 Vpp, 0 V. That is the harmless direction, so it does not ask.
        MUTED -> restore what mute displaced, which puts a voltage into
        whatever is connected - that is the direction that asks first.
        """
        if self.busy or not self.awg.inst:
            return
        if self.out_state in ("LIVE", "DC"):
            restore = (self.inst_vals.get("AMPL", ""),
                       self.inst_vals.get("AMPL_UNIT", "VP") or "VP",
                       self.inst_vals.get("OFFS", "0") or "0")
            self.set_busy(True)

            def work():
                try:
                    missed = []
                    missed += self.awg.checked_write("AMPL 0VP", self.log)
                    missed += self.awg.checked_write("OFFS 0", self.log)
                    for line in missed:
                        self.log(f"  {line}")
                    if not missed:
                        self.mute_restore = restore
                        self.log(f"Output muted (was {restore[0]} {restore[1]}"
                                 f", offset {restore[2]} V)")
                    state = self.awg.read_state()
                    self.root.after(0, lambda: self.after_read(state,
                                                               overwrite=True))
                    self.root.after(0, self.save_config)
                except Exception as exc:
                    self.log(f"ERROR: {exc}")
                    self.root.after(0, lambda: self.set_busy(False))
            threading.Thread(target=work, daemon=True).start()
            return

        # MUTED -> LIVE
        restore = self.mute_restore
        if not restore or not str(restore[0]).strip():
            messagebox.showinfo(
                "Nothing to restore",
                "The output is at 0 Vpp, but this app did not mute it, so it "
                "has nothing to put back.\n\nType an amplitude in the Ampl "
                "cell and press Apply changes instead.")
            return
        ampl, unit, offs = restore
        if self.confirm_output.get() and not messagebox.askokcancel(
                "Restore the output?",
                f"The output will go back to driving whatever is "
                f"connected:\n\n    {self.vars['FUNC'].get().strip() or '?'}"
                f"   {ampl} {unit}   offset {offs} V\n\n"
                "These are the settings MUTE displaced."):
            return
        self.set_busy(True)

        def work():
            try:
                missed = []
                missed += self.awg.checked_write(f"OFFS {offs}", self.log)
                missed += self.awg.checked_write(f"AMPL {ampl}{unit}",
                                                 self.log)
                for line in missed:
                    self.log(f"  {line}")
                if not missed:
                    self.mute_restore = None
                    self.log(f"Output restored: {ampl} {unit}, "
                             f"offset {offs} V")
                state = self.awg.read_state()
                self.root.after(0, lambda: self.after_read(state,
                                                           overwrite=True))
                self.root.after(0, self.save_config)
            except Exception as exc:
                self.log(f"ERROR: {exc}")
                self.root.after(0, lambda: self.set_busy(False))
        threading.Thread(target=work, daemon=True).start()

    # -- panel <-> instrument ---------------------------------------------

    def flatten(self, raw):
        """The generator's raw replies -> the flat {panel key: value} the
        panel uses.

        Wire codes become the panel's words here - FUNC 5 is ARB, INVT 1 is
        ON - and the amplitude's unit is split off its number. Mode slots the
        current mode has no use for come back empty rather than missing, so a
        cell that does not apply reads blank instead of holding a stale
        number.
        """
        out = {}
        func_code = str(raw.get("FUNC", "")).strip()
        try:
            func = FUNCS[int(float(func_code))]
        except (ValueError, IndexError):
            func = ""
        out["FUNC"] = func
        out["FREQ"] = fmt_value(raw.get("FREQ", "")) if raw.get("FREQ") else ""
        out["FSMP"] = fmt_value(raw.get("FSMP", "")) if raw.get("FSMP") else ""
        ampl, unit = parse_ampl(raw.get("AMPL", ""))
        out["AMPL"] = ampl
        out["AMPL_UNIT"] = unit or "VP"
        out["OFFS"] = fmt_value(raw.get("OFFS", "")) if raw.get("OFFS") else ""
        out["PHSE"] = fmt_value(raw.get("PHSE", "")) if raw.get("PHSE") else ""
        out["PERI"] = ""              # panel arithmetic, filled by recompute
        invt = str(raw.get("INVT", "")).strip()
        out["INVT"] = "ON" if invt == "1" else ("OFF" if invt == "0" else "")

        mena = str(raw.get("MENA", "")).strip()
        mode = (MODE_OF_MTYP.get(str(raw.get("MTYP", "")).strip(), "Off")
                if mena == "1" else "Off")
        out["MODE"] = mode
        spec = MODE_PARAMS.get(mode, [])
        for slot in range(MODE_SLOTS):
            if slot >= len(spec):
                out[f"MODE:{slot}"] = ""
                continue
            cmd = spec[slot][1]
            value = str(raw.get(cmd, "")).strip()
            if cmd == "MDWF":
                out[f"MODE:{slot}"] = MDWF_NAMES.get(value, value)
            elif cmd == "TSRC":
                out[f"MODE:{slot}"] = TSRC_NAMES.get(value, value)
            else:
                out[f"MODE:{slot}"] = fmt_value(value) if value else ""
        return out

    def show_state(self, raw, overwrite=False):
        """Main thread only. Puts instrument values in the panel, keeping any
        edit not applied yet - unless overwrite is set, which is the case
        after an Apply, when the generator is the authority on what took
        effect."""
        values = self.flatten(raw)
        kept = 0
        self.loading = True
        try:
            # The mode row is relabelled by the mode itself, so that has to be
            # set before its slots or the slots land under the wrong labels.
            for key in ("FUNC", "MODE"):
                if overwrite or not self.edited(key):
                    self.vars[key].set(values[key])
                self.inst_vals[key] = values[key]
            self.on_function()
            self.on_mode()

            done = ("FUNC", "MODE")
            for key, value in values.items():
                if key in done:
                    continue
                was_edited = self.edited(key)
                self.inst_vals[key] = value
                if overwrite or not was_edited:
                    self.vars[key].set(value)
                elif self.vars[key].get().strip() != value:
                    kept += 1
            # The period is arithmetic off the frequency the generator just
            # reported, so it lands after everything else.
            self.driver.clear()
            self.recompute()
            self.inst_vals["PERI"] = self.vars["PERI"].get().strip()
        finally:
            self.loading = False
        # Whatever the generator turned out to be set to is the new baseline
        # for the complaint, so a panel read back in a bad state says so once.
        self.mode_bad = self.mode_problem()

        self.show_output_state(self.output_state_from(
            values.get("AMPL"), values.get("AMPL_UNIT"), values.get("OFFS")))
        if kept:
            self.log(f"  (kept {kept} unapplied edit(s))")

    def collect(self):
        """Everything edited, as the wire-unit plan apply_settings wants.

        Only edited cells are sent. That keeps an Apply from rewriting
        settings nobody touched - which matters on an output that is always
        driving something.
        """
        plan = {}

        def want(key):
            return (self.edited(key)
                    and str(self.widgets[key].cget("state")) != "disabled"
                    and not self.derived(key))

        if want("FUNC"):
            func = self.vars["FUNC"].get().strip().upper()
            if func in FUNC_CODE:
                plan["FUNC"] = str(FUNC_CODE[func])
        for key in ("FSMP", "FREQ", "OFFS", "PHSE"):
            if want(key):
                plan[key] = self.vars[key].get().strip()
        # The amplitude and its unit are one setting on the wire: AMPL
        # 1.5VP. Editing either sends both.
        if want("AMPL") or want("AMPL_UNIT"):
            value = self.vars["AMPL"].get().strip()
            unit = self.vars["AMPL_UNIT"].get().strip().upper() or "VP"
            if value != "":
                plan["AMPL"] = f"{value}{unit}"
        if want("INVT"):
            plan["INVT"] = "1" if self.vars["INVT"].get().strip() == "ON" \
                else "0"

        mode = self.vars["MODE"].get().strip() or "Off"
        slots = [f"MODE:{s}" for s in range(MODE_SLOTS)]
        touched = self.edited("MODE") or any(
            self.edited(k) and str(self.widgets[k].cget("state")) != "disabled"
            for k in slots)
        if touched:
            params = {}
            for slot in range(MODE_SLOTS):
                cmd = self.mode_cmd(slot)
                value = self.vars[f"MODE:{slot}"].get().strip()
                if not cmd or value == "":
                    continue
                if cmd == "MDWF":
                    value = MDWF_CODES.get(value.upper(), value)
                elif cmd == "TSRC":
                    value = TSRC_CODES.get(value.upper(), value)
                params[cmd] = value
            plan["MODE"] = (mode, params)
        return plan

    # -- actions -----------------------------------------------------------

    def do_connect(self):
        # A blank-address scan walks thirty primary addresses and takes about
        # a minute, which is long enough that Connect gets pressed again. The
        # second press used to start a second walk that closed the first
        # one's VISA session out from under it.
        if self.busy:
            self.log("Already talking to the bus - wait for that to finish.")
            return
        addr = self.address.get().strip()
        remembered = self.remembered
        self.set_busy(True)

        def work():
            try:
                if not addr:
                    self.log("Looking for the DS345 (typed address is blank, "
                             "so scanning GPIB - the first scan of a quiet "
                             "bus takes a minute)...")
                idn = self.awg.connect(addr or None, remembered or None,
                                       log=self.log)
                self.root.after(0, lambda: self.status.configure(
                    text=idn[:70], foreground="#060"))
                self.log(f"Connected: {idn}")
                self.log(f"Address:   {self.awg.addr}")
                self.remembered = self.awg.addr
                self.root.after(0, lambda: self.address.set(self.awg.addr))
                state = self.awg.read_state()
                self.root.after(0, lambda: self.after_read(state))
                self.root.after(0, self.save_config)
            except Exception as exc:
                self.root.after(0, lambda: self.status.configure(
                    text="Not connected", foreground="#a00"))
                self.log(f"ERROR: {exc}")
                self.root.after(0, lambda: self.set_busy(False))
        threading.Thread(target=work, daemon=True).start()

    def after_read(self, raw, overwrite=False):
        """Main thread. Land a fresh read into the panel."""
        self.show_state(raw, overwrite=overwrite)
        self.read_stamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.set_busy(False)
        self.refresh_marks()
        self.refresh_memory()
        self.draw_preview()

    def do_discard(self):
        """Put every cell back to what the generator last reported.

        The counterpart to Apply: an edit that could only ever be sent or
        quietly carried, never taken back, is a trap.
        """
        pending = self.unapplied()
        if not pending:
            self.log("Nothing to discard - the panel matches the generator.")
            return
        note = ("" if self.read_stamp else
                "\n\nNothing has been read from the generator yet, so this "
                "will leave the panel empty.")
        if not messagebox.askokcancel(
                "Discard changes",
                f"Put {len(pending)} unapplied change(s) back to what the "
                f"generator last reported?{note}"):
            return
        # Loading, because none of this is the user typing: no mode row
        # carry-over, no complaint, no driver moving to whatever was written
        # last.
        self.loading = True
        try:
            for key, var in self.vars.items():
                value = self.inst_vals.get(key, "")
                if var.get().strip() != value:
                    var.set(value)
            self.computed.clear()
            self.on_function()
            self.on_mode()
        finally:
            self.loading = False
        self.refresh_marks()
        self.draw_preview()
        self.log(f"Discarded {len(pending)} unapplied change(s).")

    def do_read(self):
        if self.busy or not self.awg.inst:
            return
        pending = self.unapplied()
        if pending and not messagebox.askyesno(
                "Unapplied changes",
                f"{len(pending)} change(s) have not been applied.\n\n"
                "Reading will overwrite them with what the generator actually "
                "holds.\n\nRead anyway?"):
            self.log(f"Read cancelled - {len(pending)} unapplied change(s) "
                     f"kept.")
            return
        self.set_busy(True)

        def work():
            try:
                state = self.awg.read_state()
                self.log("Read back from generator."
                         + (f" {len(pending)} unapplied change(s) overwritten."
                            if pending else ""))
                self.root.after(0, lambda: self.after_read(state,
                                                           overwrite=True))
            except Exception as exc:
                self.log(f"ERROR: {exc}")
                self.root.after(0, lambda: self.set_busy(False))
        threading.Thread(target=work, daemon=True).start()

    def do_apply(self):
        if self.busy or not self.awg.inst:
            return
        plan = self.collect()
        if not plan:
            self.log("No changes to apply.")
            return
        # The generator would take the enable and answer with a bare error
        # bit; the refusal here says why.
        problem = self.mode_problem() if "MODE" in plan else ()
        if problem:
            title, message = problem
            messagebox.showerror(title, message)
            self.log(f"Apply cancelled: {message.splitlines()[0]}")
            return
        # There is no output switch on this instrument, so applying always
        # changes what is coming out of the BNC - which is a real change to
        # the experiment, not just to the panel.
        if self.out_state != "MUTED" and self.confirm_output.get():
            if not messagebox.askokcancel(
                    "Apply to the live output?",
                    "The DS345's output is always on, so applying will "
                    "change what is being output straight away.\n\n"
                    "Continue?"):
                self.log("Apply cancelled.")
                return
        self.set_busy(True)

        def work():
            try:
                self.log("Applying:")
                missed = self.awg.apply_settings(plan, log=self.log)
                for line in missed:
                    self.log(f"  {line}")
                if missed:
                    # A box rather than a log line: this is exactly the case
                    # where the read-back below makes an edit look as though
                    # it was silently thrown away.
                    self.root.after(0, lambda m=list(missed):
                                    messagebox.showwarning(
                        "Not everything took",
                        "\n\n".join(m)
                        + "\n\nThe panel is about to show what the generator "
                          "actually holds."))
                if "FSMP" in plan or plan.get("FUNC") == str(FUNC_CODE["ARB"]):
                    self.report_arb_timing()
                fresh = self.awg.read_state()
                self.root.after(0, lambda: self.after_read(fresh,
                                                           overwrite=True))
            except Exception as exc:
                self.log(f"ERROR: {exc}")
                self.root.after(0, lambda: self.set_busy(False))
        threading.Thread(target=work, daemon=True).start()

    def do_trigger(self):
        """*TRG - fire a single burst or single sweep."""
        if self.busy or not self.awg.inst:
            return

        # *TRG is one exchange, but it is an exchange: without the flag it
        # could go down the same VISA session an Apply was already halfway
        # through, and the reply either command got would be the other's.
        self.set_busy(True)

        def work():
            try:
                missed = self.awg.trigger()
                if missed:
                    self.log(f"*TRG: {missed[0]} (the trigger source has to "
                             f"be SINGLE for *TRG to mean anything)")
                else:
                    self.log("*TRG sent")
            except Exception as exc:
                self.log(f"ERROR: {exc}")
            finally:
                self.root.after(0, lambda: self.set_busy(False))
        threading.Thread(target=work, daemon=True).start()

    def report_arb_timing(self):
        """Say out loud what the sample clock does to the arb's repeat rate.

        FSMP only takes exact divisors of 40 MHz and quietly rounds anything
        else, and the repeat rate is that rounded clock over the record's
        length - two silent pieces of arithmetic that decide what frequency
        actually comes out. Instrument thread only.
        """
        try:
            rate = float(self.awg.query("FSMP?"))
            samples = self.known_waves.get(self.device_wave)
            if samples is not None and rate > 0:
                n = len(samples)
                self.log(f"  ARB timing: {rate:,.6g} Sa/s over {n:,} points "
                         f"-> repeats at {_hz(rate / n)}")
            elif rate > 0:
                self.log(f"  ARB clock: {rate:,.6g} Sa/s (record length "
                         f"unknown, so the repeat rate is rate/points)")
        except Exception:
            pass                      # a log line is never worth failing over

    # -- arbitrary waveform upload ----------------------------------------

    def pick_arb(self):
        start = self.wave_dir if os.path.isdir(self.wave_dir) else "."
        path = filedialog.askopenfilename(
            title="Waveform samples", initialdir=start,
            filetypes=[("Sample data", "*.csv *.txt *.dat *.npy"),
                       ("All files", "*.*")])
        if not path:
            return
        self.wave_dir = os.path.dirname(path) or self.wave_dir
        try:
            table, names = read_table(path)
            if table.shape[0] < 2:
                raise ValueError(f"only {table.shape[0]} sample(s) in the file")
        except Exception as exc:
            self.log(f"Could not read {os.path.basename(path)}: {exc}")
            messagebox.showerror("Cannot read file", str(exc))
            return

        self.log(f"Loaded {path}")
        self.take_table(table, names, path,
                        os.path.splitext(os.path.basename(path))[0])

    def take_table(self, table, names, source, suggested):
        """Adopt a set of samples, wherever they came from - a file, a built
        shape or pasted text. All three land here so they share the column
        picker, the preview and the upload button."""
        self.arb_table = np.asarray(table, dtype=np.float64)
        self.arb_source = source
        ncols = self.arb_table.shape[1]
        labels = [f"{i + 1}: {names[i]}" if names and i < len(names)
                  else f"column {i + 1}" for i in range(ncols)]
        self.arb_col_box.configure(values=labels,
                                   state="readonly" if ncols > 1 else "disabled")
        self.arb_col.set(labels[default_column(names, ncols)])
        self.arb_name.set(safe_name(suggested) or "wave1")
        if ncols > 1:
            self.log(f"  {self.arb_table.shape[0]} rows x {ncols} columns"
                     + (f": {', '.join(names)}" if names else ""))
        if self.canvas is not None:
            self.show_pending.set(True)        # show what is about to go up
        self.pick_column()

    def pick_column(self):
        """Take the selected column of the loaded file as the samples."""
        if self.arb_table is None:
            return
        try:
            index = self.arb_col_box.cget("values").index(self.arb_col.get())
        except ValueError:
            index = 0
        data = np.asarray(self.arb_table[:, index], dtype=np.float64).ravel()
        self.arb_samples = data
        self.arb_element = data
        self.arb_index = index
        self.show_arb_info()
        self.log(f"  using column {index + 1}: {data.size} pts, "
                 f"{data.min():g} to {data.max():g}")
        self.set_busy(self.busy)
        self.draw_preview()

    def on_norm(self):
        """The normalise switch changes what will be sent, so say so at once."""
        self.show_arb_info()
        self.draw_preview()

    def show_arb_info(self):
        """The line beside Load file..., including the warnings that matter
        before Upload rather than after: full-scale clipping, and the DS345's
        own hard limits on how long a record can be.
        """
        if self.arb_samples is None:
            self.arb_info.configure(text="no file loaded", foreground="#666")
            return
        data = self.arb_samples
        note = f"{os.path.basename(self.arb_source)} - {data.size} pts"
        if self.arb_table is not None and self.arb_table.shape[1] > 1:
            note += f", col {self.arb_index + 1}"
        peak = float(np.max(np.abs(data))) if data.size else 0.0
        if data.size > MAX_ARB_POINTS:
            self.arb_info.configure(
                text=f"{note} - TOO LONG, the DS345 holds "
                     f"{MAX_ARB_POINTS:,} points at most", foreground="#c60")
        elif data.size < MIN_ARB_POINTS:
            self.arb_info.configure(
                text=f"{note} - too short, the DS345 wants at least "
                     f"{MIN_ARB_POINTS}", foreground="#c60")
        elif not self.norm.get() and peak > 1.0:
            over = int(np.count_nonzero(np.abs(data) > 1.0))
            self.arb_info.configure(
                text=f"{note} - peak {peak:g}, {over:,} sample(s) would be "
                     f"CLIPPED at full scale", foreground="#c60")
        else:
            self.arb_info.configure(text=note, foreground="#000")

    def do_clear_pending(self):
        """Drop the waveform waiting to be uploaded.

        Nothing on the generator is touched - this is the panel forgetting,
        not the instrument. Worth having because the pending record is
        otherwise only replaced, never let go: build something by mistake and
        it stays on the preview, and one wrong press of Upload puts it on the
        output.
        """
        if self.arb_samples is None and self.arb_table is None:
            return
        was = self.arb_source or "pending waveform"
        self.arb_samples = self.arb_table = self.arb_element = None
        self.arb_source = ""
        self.arb_col_box.configure(values=(), state="disabled")
        self.arb_col.set("")
        self.show_arb_info()
        self.log(f"Pending waveform cleared ({was}). Nothing was sent.")
        self.set_busy(self.busy)
        self.draw_preview()

    def do_upload(self):
        if self.busy or not self.awg.inst or self.arb_samples is None:
            return
        name = safe_name(self.arb_name.get())
        samples = np.asarray(self.arb_samples, dtype=np.float64).ravel()
        if samples.size > MAX_ARB_POINTS:
            messagebox.showerror(
                "Too many points",
                f"{samples.size:,} samples will not fit - the DS345 holds at "
                f"most {MAX_ARB_POINTS:,} points.\n\nLower the sample rate or "
                f"shorten the record.")
            return
        if samples.size < MIN_ARB_POINTS:
            messagebox.showerror(
                "Too few points",
                f"The DS345 wants at least {MIN_ARB_POINTS} points and this "
                f"record has {samples.size}.")
            return
        norm = self.norm.get()
        # Clipping must never happen silently: a record that peaks at 1.14
        # would come out flat-topped, and the local copy written afterwards
        # would hold the clipped numbers with nothing anywhere saying so.
        peak = float(np.max(np.abs(samples)))
        if not norm and peak > 1.0:
            over = int(np.count_nonzero(np.abs(samples) > 1.0))
            answer = messagebox.askyesnocancel(
                "Samples past full scale",
                f"{over:,} of {samples.size:,} samples are outside -1..+1 "
                f"(peak {peak:g}), and 'normalise to full scale' is off.\n\n"
                f"The DAC has no headroom above full scale, so every one of "
                f"them would come out at the same flat top. Set the height "
                f"with Ampl on the output instead.\n\n"
                f"Yes - divide by {peak:g} so the whole shape fits\n"
                f"No - upload it flat-topped anyway\n"
                f"Cancel - go back")
            if answer is None:
                return
            if answer:
                norm = True
                self.log(f"Normalising on upload: peak was {peak:g}, so "
                         f"{over:,} sample(s) would have been clipped.")
            else:
                self.log(f"WARNING: uploading '{name}' with {over:,} "
                         f"sample(s) clipped flat at full scale.")
        # The one arb slot is the one being played whenever the function is
        # ARB, so this upload changes the output the moment it lands - and
        # there is no output switch to hide behind.
        live_now = (self.vars["FUNC"].get().strip().upper() == "ARB"
                    and self.out_state != "MUTED")
        if live_now and self.confirm_output.get():
            if not messagebox.askokcancel(
                    "Upload to the live output?",
                    "The function is ARB and the output is not muted, so the "
                    "new waveform starts playing the moment it arrives - it "
                    "replaces the one playing now.\n\nContinue?"):
                return
        self.set_busy(True)

        def work():
            try:
                n = self.awg.upload_arb(samples, normalize=norm)
                self.log(f"Uploaded '{name}' ({n} pts) into the DS345's arb "
                         f"slot" + ("" if live_now else
                                    " - set Function to ARB to play it"))
                # Keep the samples so the preview can draw this waveform from
                # now on. The generator cannot read it back out, so if we do
                # not remember it here, nothing can ever show it.
                self.known_waves[name] = dac_samples(samples, normalize=norm)
                cache_save(name, self.known_waves[name])
                self.device_wave = name
                self.report_arb_timing()
                state = self.awg.read_state()
                self.root.after(0, lambda: self.after_read(state,
                                                           overwrite=True))
                self.root.after(0, self.save_config)
            except Exception as exc:
                self.log(f"ERROR uploading: {exc}")
                self.root.after(0, lambda: self.set_busy(False))
        threading.Thread(target=work, daemon=True).start()

    # -- local waveform list ----------------------------------------------

    def do_refresh_memory(self):
        """Re-read the Waveforms folder.

        Local copies used to be picked up only at startup elsewhere, so a
        waveform dropped into Waveforms/ - or written there by a script -
        stayed invisible until a restart. The folder needs nothing but the
        disk, so this works with no generator attached.
        """
        if self.busy:
            return
        before = set(self.known_waves)
        self.known_waves = cache_load()
        added = sorted(set(self.known_waves) - before)
        gone = sorted(before - set(self.known_waves))
        if added:
            self.log(f"Local copies picked up from {WAVE_CACHE}: "
                     + ", ".join(added))
        if gone:
            self.log("Local copies no longer in the folder: " + ", ".join(gone))
        if not added and not gone:
            self.log(f"Local copies unchanged ({len(self.known_waves)} in "
                     f"{WAVE_CACHE}).")
        self.refresh_memory()
        self.draw_preview()

    def refresh_memory(self):
        """Show the local copies, and which of them the generator holds.

        The names are kept in mem_names alongside the rows rather than read
        back off them: a row is a name padded out to a column and then some
        text, and picking the first word out of that is only the name again
        while no name has a space in it. A file called "pi pulse 300ns.csv"
        is a perfectly ordinary thing to find in Waveforms/, and it made both
        Forget and Load to pending answer about a waveform called "pi".
        """
        self.mem_list.delete(0, "end")
        self.mem_names = []
        for name in sorted(self.known_waves):
            samples = self.known_waves[name]
            note = f"{samples.size:>9,} pts"
            if name == self.device_wave:
                note += "   <- on the generator"
            self.mem_list.insert("end", f"{name:<18}{note}")
            self.mem_names.append(name)
        if self.device_wave and self.device_wave not in self.known_waves:
            # The record the generator is playing has lost its local copy, so
            # nothing can draw it any more. Say so rather than showing an
            # empty list that implies the generator is empty too.
            self.mem_list.insert("end", f"{self.device_wave:<18} on the "
                                        "generator, no local copy")
            self.mem_names.append(self.device_wave)

    def selected_wave(self):
        picked = self.mem_list.curselection()
        if not picked or picked[0] >= len(self.mem_names):
            return None
        return self.mem_names[picked[0]]

    def do_forget(self):
        """Drop this app's local copy. The generator keeps playing its own."""
        name = self.selected_wave()
        if not name:
            self.log("Pick a waveform in the list first.")
            return
        if name not in self.known_waves:
            self.log(f"No local copy of '{name}' to forget.")
            return
        note = ""
        if name == self.device_wave:
            note = ("\n\nThis is the record the generator is holding, and "
                    "the DS345 cannot read it back - forget the copy and "
                    "nothing can ever draw it again.")
        if not messagebox.askokcancel(
                "Forget local copy?",
                f"Delete this app's local copy of '{name}'?{note}"):
            return
        self.known_waves.pop(name, None)
        cache_forget(name)
        self.log(f"Forgot the local copy of '{name}'")
        self.refresh_memory()
        self.draw_preview()

    def do_use_wave(self):
        """Load the selected waveform's local copy into the pending slot.

        Pending draws it on the preview first and puts its numbers where
        Type/paste can show them - choosing a waveform without having seen it
        is what this list exists to prevent.
        """
        name = self.selected_wave()
        if not name:
            self.log("Pick a waveform in the list first.")
            return
        samples = self.known_waves.get(name)
        if samples is None:
            self.log(f"No local copy of '{name}' - the generator may hold "
                     "it, but it will not read samples back over GPIB, so "
                     "there is nothing here to load.")
            return
        samples = np.asarray(samples, dtype=np.float64).ravel()
        self.take_table(samples.reshape(-1, 1), None,
                        f"local copy of {name}", name)
        self.log(f"'{name}' loaded to pending, {samples.size} pts. "
                 "Nothing was sent to the generator.")

    # -- setups ------------------------------------------------------------

    def do_setups(self):
        """Saving and recalling a whole instrument state, in its own window.

        A window rather than a panel row because it is not something touched
        while tuning - it is what happens once at the start and once at the
        end. Not modal: Save and Recall both go off to the instrument on a
        thread, and a grab here would freeze the panel that reports what they
        did.
        """
        if self.setup_win is not None and self.setup_win.winfo_exists():
            self.setup_win.lift()
            self.setup_win.focus_force()
            return
        win = self.setup_win = tk.Toplevel(self.root)
        win.title("Load / save setups")
        win.transient(self.root)
        win.protocol("WM_DELETE_WINDOW", self._setups_close)

        ttk.Label(win, justify="left", foreground="#444", text=(
            "Save setup writes the full instrument state to a timestamped "
            "JSON and a readable .txt beside it.\n"
            "Recall setup applies one back. The arbitrary waveform itself is "
            "not in the file - the DS345 cannot\nread it back out - so a "
            "setup that uses ARB needs its record re-uploaded from the local "
            "copies list.")
        ).pack(anchor="w", padx=8, pady=(8, 4))

        of = ttk.Frame(win)
        of.pack(fill="x", padx=8, pady=(0, 2))
        ttk.Label(of, text="Folder:").pack(side="left", padx=(0, 4))
        ttk.Entry(of, textvariable=self.outdir, width=52).pack(
            side="left", fill="x", expand=True)
        ttk.Button(of, text="...", width=3,
                   command=self.pick_dir).pack(side="left", padx=6)

        pf = ttk.Frame(win)
        pf.pack(fill="x", padx=8, pady=2)
        ttk.Label(pf, text="Prefix:").pack(side="left")
        ttk.Entry(pf, textvariable=self.prefix, width=16).pack(side="left",
                                                               padx=4)
        self.save_btn = ttk.Button(pf, text="Save setup",
                                   command=self.do_save_setup,
                                   state="disabled")
        self.save_btn.pack(side="left", padx=(8, 4))
        self.recall_btn = ttk.Button(pf, text="Recall setup...",
                                     command=self.do_recall_setup,
                                     state="disabled")
        self.recall_btn.pack(side="left")

        gf = ttk.Frame(win)
        gf.pack(fill="x", padx=8, pady=(6, 4))
        # Defaults on. With no output switch on this instrument, asking
        # before anything that changes the live output is the whole of the
        # safety story.
        ttk.Checkbutton(gf, text="confirm before changing the live output",
                        variable=self.confirm_output).pack(side="left")

        ttk.Button(win, text="Close", command=self._setups_close).pack(
            anchor="w", padx=8, pady=(4, 8))
        # The two buttons are only live with an instrument attached, and this
        # window may well have been opened before one was.
        self.set_busy(self.busy)

    def _setups_close(self):
        if self.setup_win is not None:
            self.setup_win.destroy()
        self.setup_win = None
        self.save_btn = self.recall_btn = None

    def do_save_setup(self):
        if self.busy or not self.awg.inst:
            return
        self.set_busy(True)
        outdir = self.outdir.get().strip()
        prefix = safe_name(self.prefix.get())

        def work():
            try:
                snap = self.awg.snapshot()
                # The arb slot is write-only, so the setup can only carry
                # what this app knows about it: the local name, which recall
                # uses to say what to re-upload.
                snap["arb_local_name"] = self.device_wave
                os.makedirs(outdir, exist_ok=True)
                stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                base = os.path.join(outdir, f"{prefix}_{stamp}")
                with open(base + ".json", "w", encoding="utf-8") as fh:
                    json.dump(snap, fh, indent=2)
                with open(base + ".txt", "w", encoding="utf-8") as fh:
                    fh.write(describe(snap))
                self.log(f"Saved setup: {base}.json (+ .txt)")
            except Exception as exc:
                self.log(f"ERROR saving setup: {exc}")
            finally:
                self.root.after(0, lambda: self.set_busy(False))
                self.root.after(0, self.save_config)
        threading.Thread(target=work, daemon=True).start()

    def do_recall_setup(self):
        if self.busy or not self.awg.inst:
            return
        path = filedialog.askopenfilename(
            title="Recall setup", initialdir=self.outdir.get() or ".",
            filetypes=[("Setup files", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as fh:
                snap = json.load(fh)
            settings = snap["settings"]
        except Exception as exc:
            messagebox.showerror("Cannot read setup", str(exc))
            self.log(f"Could not read {path}: {exc}")
            return

        note = ("\n\nThe output is always on, so this changes what is being "
                "output straight away.")
        func_code = str(settings.get("FUNC", "")).strip()
        arb_name = str(snap.get("arb_local_name", "") or "")
        if func_code == str(FUNC_CODE["ARB"]):
            held = ("the local copies list holds it"
                    if arb_name and arb_name in self.known_waves
                    else "no local copy of it is known")
            note += (f"\n\nThe setup uses ARB. The waveform itself is not in "
                     f"the file - it was "
                     + (f"'{arb_name}', and {held}. " if arb_name
                        else "never named. ")
                     + "Whatever the generator holds right now is what will "
                       "play; re-upload from Load to pending if that is not "
                       "it.")
        if not messagebox.askokcancel("Recall setup",
                                      f"Apply {os.path.basename(path)} to the "
                                      f"generator?{note}"):
            return
        # Recalling is the last thing this window is for, and what it does
        # next is all reported to the log and the panel behind it.
        self._setups_close()
        self.set_busy(True)

        def work():
            try:
                self.log(f"Recalling {os.path.basename(path)}:")
                missed = self.awg.apply_settings(
                    self.plan_from_snapshot(settings), log=self.log)
                for line in missed:
                    self.log(f"  {line}")
                fresh = self.awg.read_state()
                self.log(f"Recalled {os.path.basename(path)}")
                self.root.after(0, lambda: self.after_read(fresh,
                                                           overwrite=True))
            except Exception as exc:
                self.log(f"ERROR recalling: {exc}")
                self.root.after(0, lambda: self.set_busy(False))
        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def plan_from_snapshot(settings):
        """A saved state's raw replies -> the write plan apply_settings wants.

        The one judgement call is the phase: PHSE cannot be set under NOISE,
        ARB, a sweep, FM or PM, so a snapshot taken in any of those states
        leaves it out rather than manufacturing a refusal on recall.
        """
        def val(key):
            return str(settings.get(key, "")).strip()

        plan = {}
        if val("FUNC"):
            plan["FUNC"] = val("FUNC")
        for key in ("FSMP", "FREQ", "OFFS"):
            if val(key):
                plan[key] = val(key)
        if val("AMPL"):
            plan["AMPL"] = val("AMPL")     # unit already rides on the number
        if val("INVT"):
            plan["INVT"] = val("INVT")

        mena = val("MENA") == "1"
        mode = MODE_OF_MTYP.get(val("MTYP"), "Off") if mena else "Off"
        try:
            func = FUNCS[int(float(val("FUNC")))]
        except (ValueError, IndexError):
            func = ""
        if val("PHSE") and func in OSCILLATING and mode in ("Off", "AM",
                                                            "Burst"):
            plan["PHSE"] = val("PHSE")

        params = {}
        if mode != "Off":
            for cmd in ("MDWF", "DPTH", "FDEV", "PDEV", "RATE", "BCNT",
                        "STFR", "SPFR", "TSRC", "TRAT"):
                if val(cmd):
                    params[cmd] = val(cmd)
        plan["MODE"] = (mode, params)
        return plan

    # -- preview -----------------------------------------------------------

    def panel_mode(self):
        """The modulation/sweep/burst row as (mode, {command: value})."""
        mode = self.vars["MODE"].get().strip() or "Off"
        params = {}
        for slot in range(MODE_SLOTS):
            cmd = self.mode_cmd(slot)
            value = self.vars[f"MODE:{slot}"].get().strip()
            if cmd and value:
                params[cmd] = value
        return mode, params

    def trace_for(self, span):
        """The output's curve over the given window, or (None, why)."""
        func = self.vars["FUNC"].get().strip().upper()
        vals = self.panel_vals()
        arb = self.known_waves.get(self.device_wave) if func == "ARB" else None
        mode, mod = self.panel_mode()
        curve = preview_curve(func, vals, arb=arb, span=span, mode=mode,
                              mod=mod,
                              invert=self.vars["INVT"].get().strip() == "ON")
        if curve is None:
            why = (f"ARB '{self.device_wave}' has no local copy to draw"
                   if func == "ARB" and self.device_wave
                   else "ARB: nothing known to be on the generator"
                   if func == "ARB" else func or "?")
            return None, why
        label = func
        if mode != "Off":
            label += f" +{mode}"
        if arb is not None:
            label += f" '{self.device_wave}' ({len(arb)} pts held)"
        if curve[2]:
            label += f" [{curve[2]}]"
        return curve[:2], label

    def pending_fsmp(self):
        """The clock the pending record would play at: the panel's FSMP if it
        holds one, else the builder's rate box - the two places a rate can
        have been stated."""
        rate = _as_float(self.vars["FSMP"].get(), 0.0)
        if rate <= 0:
            rate = _as_float(self.build_rate.get(), 0.0)
        return rate

    def draw_preview(self):
        """The output's trace, and the pending record as it would play.

        The pending waveform is drawn as ARB at the panel's sample clock,
        dashed and in a paler shade - one instrument, one axis, so the two
        traces share time and the picture says whether the pending record is
        longer or shorter than what is playing now.
        """
        if self.canvas is None:
            return
        show_out = self.show_out.get()
        pending = None
        if self.show_pending.get() and self.arb_samples is not None:
            try:
                # Draw what will be stored, not what came out of the file: the
                # upload normalises, so raw values here would put the same
                # waveform at a different height before and after sending.
                pending = dac_samples(self.arb_samples,
                                      normalize=self.norm.get())
            except ValueError:
                pending = None

        func = self.vars["FUNC"].get().strip().upper()
        vals = self.panel_vals()
        mode, mod = self.panel_mode()
        limits = limit_warnings(func, vals, mode, mod)

        self.ax.clear()
        if not show_out and pending is None:
            self.ax.text(0.5, 0.5, "nothing selected\ntick output or pending",
                         ha="center", va="center",
                         transform=self.ax.transAxes, color="#666")
            self.ax.set_xticks([])
            self.ax.set_yticks([])
            # Switching the trace off does not make a setting legal, so the
            # limits stay up; only the caveat about the drawing goes.
            self.warn_note.configure(text="; ".join(limits))
            self.canvas.draw_idle()
            return

        arb = self.known_waves.get(self.device_wave) if func == "ARB" else None

        periods = []
        if show_out:
            periods.append(preview_period(
                func, vals, mode, mod, len(arb) if arb is not None else None))
        pending_vals = None
        if pending is not None:
            # The pending record is judged through the panel's own settings -
            # amplitude, offset, mode - but at its own length and clock,
            # which is what it would actually play at.
            pending_vals = dict(vals)
            rate = self.pending_fsmp()
            if rate > 0:
                pending_vals["FSMP"] = f"{rate:.10g}"
            periods.append(preview_period("ARB", pending_vals, mode, mod,
                                          pending.size))
        span = 2.0 * max(periods) if periods else 1e-3

        handles, notes, bad = [], [], []
        if show_out:
            curve, label = self.trace_for(span)
            if curve is None:
                notes.append(label)
            else:
                t, v = curve
                handles += self.ax.plot(t * 1e3, v, lw=1.1, color=OUT_COLOUR,
                                        label=label)
                n = preview_points(span, func, vals, mode, mod, arb)
                if preview_aliasing(span, n, func, vals, mode, mod, arb):
                    bad.append("output")

        if pending is not None:
            curve = preview_curve(
                "ARB", pending_vals, arb=pending, span=span, mode=mode,
                mod=mod, invert=self.vars["INVT"].get().strip() == "ON")
            if curve is not None:
                t, v = curve[:2]
                rate = self.pending_fsmp()
                at = f" at {_hz(rate)}" if rate > 0 else ""
                handles += self.ax.plot(
                    t * 1e3, v, lw=1.4, ls="--", color=PENDING_COLOUR,
                    label=f"pending ({pending.size} pts{at})")
                n = preview_points(span, "ARB", pending_vals, mode, mod,
                                   pending)
                if preview_aliasing(span, n, "ARB", pending_vals, mode, mod,
                                    pending):
                    bad.append("pending")

        self.ax.set_xlabel("time (ms)")
        self.ax.set_ylabel("volts")
        self.ax.grid(alpha=0.3)
        if handles:
            self.ax.legend(handles, [h.get_label() for h in handles],
                           fontsize=7, loc="upper right", framealpha=0.85)
        self.ax.set_title(" | ".join(notes) if notes else "",
                          fontsize=8, color="#666")
        if pending is not None and self.pending_fsmp() > FSMP_MAX:
            limits.append(f"the pending record's "
                          f"{_hz(self.pending_fsmp())} is past the "
                          f"{_hz(FSMP_MAX)} sample clock")
        if bad:
            named = bad[0] if len(bad) == 1 else " and ".join(bad)
            limits.append(f"{named} may be aliasing")
        self.warn_note.configure(text="; ".join(limits))
        self.canvas.draw_idle()

    # -- config ------------------------------------------------------------

    def current_cfg(self):
        return {
            "address": self.awg.addr or self.remembered,
            "outdir": self.outdir.get(),
            "prefix": self.prefix.get(),
            "arb_name": self.arb_name.get(),
            "normalise": bool(self.norm.get()),
            "confirm_output": bool(self.confirm_output.get()),
            "device_wave": self.device_wave,
            "mute_restore": list(self.mute_restore) if self.mute_restore
            else None,
        }

    def load_config(self):
        """Restore what the last session was using. Anything missing, malformed
        or of the wrong type is ignored and leaves the default in place."""
        try:
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                cfg = json.load(fh)
            if not isinstance(cfg, dict):
                raise ValueError("not a JSON object")
        except FileNotFoundError:
            self.saved_cfg = self.current_cfg()
            return
        except Exception as exc:
            self.log(f"Ignoring unreadable {CONFIG_PATH}: {exc}")
            self.saved_cfg = self.current_cfg()
            return

        for key, var in (("outdir", self.outdir), ("prefix", self.prefix),
                         ("arb_name", self.arb_name)):
            value = cfg.get(key)
            if isinstance(value, str) and value.strip():
                var.set(value)
        for key, var in (("normalise", self.norm),
                         ("confirm_output", self.confirm_output)):
            value = cfg.get(key)
            if isinstance(value, (bool, int)):
                var.set(bool(value))
        if isinstance(cfg.get("device_wave"), str):
            # What the generator was left holding last session. Only a
            # memory, and said as one: the DS345 keeps its arb over GPIB
            # sessions, but anyone at the front panel since could have
            # replaced it.
            self.device_wave = cfg["device_wave"]
        restore = cfg.get("mute_restore")
        if isinstance(restore, list) and len(restore) == 3:
            self.mute_restore = tuple(str(v) for v in restore)
        address = cfg.get("address")
        if isinstance(address, str) and address.strip():
            # The whole point of writing the address down. It is not put in
            # the address box: a typed address is a deliberate override that
            # is taken at its word without checking the reply is a DS345, and
            # a remembered one should not arrive looking like one. It is tried
            # first all the same, which is what makes the second and every
            # later session connect in a moment instead of in a minute.
            self.remembered = address.strip()

        self.saved_cfg = self.current_cfg()
        self.log(f"Restored last session from {CONFIG_PATH}")

    def save_config(self):
        """Called after connecting, uploading, muting and on close. Writes
        only when something actually changed."""
        cfg = self.current_cfg()
        if cfg == self.saved_cfg:
            return
        try:
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, indent=2)
            self.saved_cfg = cfg
        except Exception as exc:
            self.log(f"Could not save {CONFIG_PATH}: {exc}")

    def on_close(self):
        # The output is deliberately left exactly as it is: closing a control
        # panel should never interrupt something the bench is in the middle
        # of - and on this instrument there is no off to switch to anyway.
        self.save_config()
        self.awg.close()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
