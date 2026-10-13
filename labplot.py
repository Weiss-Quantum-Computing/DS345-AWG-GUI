"""labplot - the one plot panel shared by the Weiss lab's Tk/matplotlib GUIs.

One file, no dependencies beyond matplotlib and numpy, both optional at
import time: `import labplot` always succeeds, so a program that is loaded
by path from another program (scope_grab.py, ds345_awg_gui.py) does not
drag matplotlib into its host.  `HAVE_MPL` says whether a PlotPanel can be
built; without matplotlib the panel is a label that says so.

What every GUI gets from `PlotPanel` (the same way, with the same words):

- the stock matplotlib toolbar (Home, Back, Forward, Pan, Zoom, Save);
- the view is held across a redraw once the user has zoomed or panned
  (toolbar modes or the mouse wheel), with a blue "zoomed - Home to
  auto-scale" note; Home unlocks and redraws auto-scaled;
- mouse wheel zooms about the cursor (shift = y only, ctrl = x only),
  each step on the toolbar's Back stack;
- right-click copies "x<TAB>y" of the cursor to the clipboard;
- Save proposes a name and a folder the program chooses
  (`save_name`/`save_dir`), writes at 150 dpi, and reports the path;
- lazy drawing through `mark_dirty()` / `draw_if_visible()`, and
  `PlotBook` for a notebook of such panels addressed by key, never index.

Style helpers (`apply_style`, `legend`, `note`, `finish`, `placeholder`)
and the lab's colours (`CH_COLOURS`, `CMP_COLOURS`, `ramp`, `blend_white`)
make figures from different programs look like one set.  `thin` is the
min/max envelope every program re-invented for long records.

Canonical copy: Weiss-Quantum-Computing/lab-plot.  The other repos carry a
vendored copy next to their GUI; `tools/vendor.py --check` there reports a
stale copy.  Keep this file importable on Python 3.9.
"""

from __future__ import annotations

import datetime as _dt
import math
import os
import re
import tkinter as tk
from tkinter import ttk, filedialog

__version__ = "1.0.3"

try:
    import numpy as np
except Exception:  # pragma: no cover - numpy missing
    np = None

try:
    import matplotlib
    import matplotlib.colors as mcolors
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import (
        FigureCanvasTkAgg, NavigationToolbar2Tk)
    HAVE_MPL = True
except Exception:  # pragma: no cover - matplotlib missing
    matplotlib = mcolors = Figure = None
    FigureCanvasTkAgg = NavigationToolbar2Tk = None
    HAVE_MPL = False

NO_MPL = "matplotlib not installed - no plot"

# ---------------------------------------------------------------- style ----

FONT_SIZE = 8            # panel text; the same in every GUI
LEGEND_SIZE = 7
NOTE_SIZE = 6.5
GRID_ALPHA = 0.3
SAVE_DPI = 150
LEGEND_MAX = 12          # above this a legend hides more than it tells
LEGEND_TWO_COLS = 6      # above this it takes two columns

# CH1/CH2 are the AWG panels' blue/red (the ILC's EO1/EO2); CH3 the ILC's
# generator green; CH4 purple.  Scope channel n is CH_COLOURS[n].
CH_COLOURS = {1: "#1f77b4", 2: "#d62728", 3: "#2e7d32", 4: "#9467bd"}
# Compared runs: far from viridis, which the session's own runs use.
CMP_COLOURS = ["#ff7f0e", "#e377c2", "#8c564b", "#17becf", "#7f7f7f",
               "#bcbd22"]
CMP_ZORDER = 1.8         # under the session's own traces, above the grid
TARGET_COLOUR = "#222222"
PRED_COLOUR = "#8a8a8a"
NOTE_COLOUR = "#999999"
PLACEHOLDER_COLOUR = "#666666"
VIEW_NOTE_COLOUR = "#1c4fa8"
STATUS_COLOUR = "#666666"
WARN_COLOUR = "#c60"
MONO_FONT = ("Consolas", 9)


def apply_style(font_size=FONT_SIZE):
    """Set the rcParams the panels rely on.  Call once at start-up.

    Programs that render files off-screen at a different size wrap that
    in `matplotlib.rc_context(...)` so the panel style is untouched.
    """
    if not HAVE_MPL:
        return
    rc = matplotlib.rcParams
    rc["font.size"] = font_size
    rc["axes.titlesize"] = font_size
    rc["axes.labelsize"] = font_size
    rc["xtick.labelsize"] = font_size
    rc["ytick.labelsize"] = font_size
    rc["legend.fontsize"] = LEGEND_SIZE
    rc["grid.alpha"] = GRID_ALPHA
    rc["savefig.dpi"] = SAVE_DPI
    rc["axes.formatter.useoffset"] = False   # no "+1.234e3" tick offsets
    rc["axes.formatter.use_mathtext"] = True


def blend_white(colour, frac):
    """Fade a colour toward white: frac 0 = the colour, 1 = white."""
    if not HAVE_MPL:
        return colour
    r, g, b = mcolors.to_rgb(colour)
    f = min(max(float(frac), 0.0), 1.0)
    return mcolors.to_hex((r + (1 - r) * f, g + (1 - g) * f, b + (1 - b) * f))


def lighten(colour, frac=0.42):
    """The AWG panels' "pending" shade of a channel colour."""
    return blend_white(colour, frac)


def ramp(n, cmap="viridis", lo=0.1, hi=0.85):
    """n colours along a colormap, oldest to newest (the ILC's session ramp)."""
    if n <= 0:
        return []
    if not HAVE_MPL:
        return ["#1f77b4"] * n
    cm = matplotlib.colormaps[cmap]
    if n == 1:
        return [mcolors.to_hex(cm(lo))]   # a lone run: the dark end reads
    return [mcolors.to_hex(cm(lo + (hi - lo) * i / (n - 1)))
            for i in range(n)]


def cmp_colour(i):
    """Colour for the i-th compared key."""
    return CMP_COLOURS[i % len(CMP_COLOURS)]


def ch_colour(n):
    return CH_COLOURS.get(int(n), "#333333")


# -------------------------------------------------------------- helpers ----

def time_unit(span_s):
    """(scale, unit) so that t*scale reads well: 1.5e-3 s -> (1e3, 'ms')."""
    span = abs(float(span_s)) if span_s else 0.0
    if span >= 1.0 or span == 0.0:
        return 1.0, "s"
    if span >= 1e-3:
        return 1e3, "ms"
    if span >= 1e-6:
        return 1e6, "us"
    return 1e9, "ns"


def fmt_num(x, digits=6):
    """A number for a readout: %.6g, with '-' for None/NaN."""
    try:
        if x is None or x != x:
            return "-"
        return "%.*g" % (digits, x)
    except Exception:
        return str(x)


def thin(x, y, max_pts=10000):
    """Min/max envelope of (x, y) when y is longer than max_pts.

    Returns (x2, y2, step): step 1 means nothing was dropped.  Every
    block of 2*step samples contributes its minimum and maximum, in
    time order, so peaks survive and the plot has <= ~max_pts points.
    """
    if np is None:
        return x, y, 1
    x = np.asarray(x)
    y = np.asarray(y)
    n = len(y)
    if max_pts is None or max_pts <= 0 or n <= max_pts:
        return x, y, 1
    step = int(math.ceil(n / float(max_pts)))
    if step <= 1:
        return x, y, 1
    blk = 2 * step
    nb = n // blk
    parts = [np.array([0, n - 1]), np.arange(nb * blk, n)]
    if nb:
        core = y[:nb * blk].reshape(nb, blk)
        base = np.arange(nb) * blk
        if np.issubdtype(core.dtype, np.floating) and np.isnan(core).any():
            # NaN gaps: argmin/argmax would land on the NaN and hide the
            # block; look past them (an all-NaN block keeps its first
            # sample, so the gap still shows).
            lo = np.where(np.isnan(core), np.inf, core).argmin(axis=1)
            hi = np.where(np.isnan(core), -np.inf, core).argmax(axis=1)
        else:
            lo = core.argmin(axis=1)
            hi = core.argmax(axis=1)
        parts.append(lo + base)
        parts.append(hi + base)
    idx = np.unique(np.concatenate(parts))
    return x[idx], y[idx], step


def thin_step(n, max_pts=10000):
    """The step `thin` would use for n samples (1 = nothing dropped)."""
    if max_pts is None or max_pts <= 0 or n <= max_pts:
        return 1
    return max(1, int(math.ceil(n / float(max_pts))))


def legend(ax, n=None, max_entries=LEGEND_MAX, loc="best", **kw):
    """The lab legend: 7 pt, two columns above 6 entries, none above 12.

    Returns the legend or None.  `n` is the entry count when the caller
    knows it; otherwise it is counted from the labelled artists.
    """
    if n is None:
        handles, labels = ax.get_legend_handles_labels()
        n = len(labels)
    if n <= 0 or n > max_entries:
        return None
    opts = dict(fontsize=LEGEND_SIZE, loc=loc, framealpha=0.85)
    if n > LEGEND_TWO_COLS:
        opts["ncols"] = 2
    opts.update(kw)
    try:
        return ax.legend(**opts)
    except TypeError:          # matplotlib < 3.6 spells it ncol
        opts["ncol"] = opts.pop("ncols", 1)
        return ax.legend(**opts)


def note(ax, text, where="top-left", colour=NOTE_COLOUR, size=NOTE_SIZE):
    """A grey method note in a corner of the axes (values, settings,
    identifiers - never a verdict)."""
    x = 0.01 if "left" in where else 0.99
    y = 0.98 if "top" in where else 0.02
    return ax.text(x, y, text, transform=ax.transAxes, fontsize=size,
                   color=colour, ha="left" if "left" in where else "right",
                   va="top" if "top" in where else "bottom", zorder=5)


def finish(ax, xlabel=None, ylabel=None, title=None, grid=True):
    """Labels, a descriptive title and the lab grid, in one call."""
    if xlabel is not None:
        ax.set_xlabel(xlabel)
    if ylabel is not None:
        ax.set_ylabel(ylabel)
    if title is not None:
        ax.set_title(title, fontsize=FONT_SIZE)
    if grid:
        both = ax.get_xscale() == "log" or ax.get_yscale() == "log"
        ax.grid(True, alpha=GRID_ALPHA, which="both" if both else "major")
    return ax


def placeholder(fig, text):
    """Clear the figure and say why there is nothing to draw."""
    fig.clear()
    fig.text(0.5, 0.5, text, ha="center", va="center",
             color=PLACEHOLDER_COLOUR, fontsize=FONT_SIZE + 1)


def stamp(when=None):
    """YYYYMMDD_HHMMSS for file names."""
    return (when or _dt.datetime.now()).strftime("%Y%m%d_%H%M%S")


_BAD_NAME = re.compile(r'[<>:"/\\|?*\s]+')


def safe_name(text, fallback="figure"):
    """A file-name stem: whitespace and Windows-forbidden characters -> '_'."""
    s = _BAD_NAME.sub("_", str(text or "")).strip("_. ")
    return s or fallback


# ---------------------------------------------------------------- panel ----

class _Toolbar(NavigationToolbar2Tk if HAVE_MPL else object):
    """The stock toolbar with Home and Save routed through the panel.

    NavigationToolbar2Tk binds each button to `getattr(self, name)` when
    it is built, so overriding the methods on a subclass is enough - no
    poking at `_buttons` afterwards.
    """

    def __init__(self, canvas, window, panel):
        self._panel = panel
        super().__init__(canvas, window, pack_toolbar=False)

    def home(self, *args):
        self._panel.home()

    def save_figure(self, *args):
        self._panel.save_figure()

    def _stock_home(self, *args):
        super().home(*args)


class PlotPanel(ttk.Frame):
    """A figure with the lab's toolbar behaviour, in one Tk frame.

    Parameters
    ----------
    master : Tk container.
    draw : callable(fig) that (re)builds the figure, or None.  Panels
        with no draw function are driven through `fig` directly and
        `redraw()`.
    figsize, dpi, constrained : figure construction.
    toolbar : whether to show the toolbar row.
    save_name : callable() -> stem, or a string, proposed by Save.
    save_dir : callable() -> folder, or a string, proposed by Save.
    on_click : callable(event) on a left click outside toolbar modes.
    on_status : callable(text) - where "saved ..." etc. also go (the log).
    wheel_zoom, hold_view, copy_coords : the behaviours, on by default.

    Attributes: `fig`, `canvas`, `toolbar`, `controls` (an empty row
    above the figure for the program's own knobs), `view_locked`,
    `dirty`, `last_saved`.
    """

    def __init__(self, master, draw=None, figsize=(6.4, 4.6), dpi=100,
                 constrained=True, toolbar=True, save_name=None,
                 save_dir=None, on_click=None, on_status=None,
                 wheel_zoom=True, hold_view=True, copy_coords=True,
                 **kw):
        super().__init__(master, **kw)
        self._draw_fn = draw
        self.save_name = save_name
        self.save_dir = save_dir
        self.on_click = on_click
        self.on_status = on_status
        self.wheel_zoom = wheel_zoom
        self.hold_view = hold_view
        self.copy_coords = copy_coords
        self.view_locked = False
        self.dirty = True
        self.last_saved = None

        self.controls = ttk.Frame(self)
        self.controls.pack(side="top", fill="x")

        if not HAVE_MPL:
            self.fig = self.canvas = self.toolbar = None
            self.view_note = self.status = None
            ttk.Label(self, text=NO_MPL, foreground=WARN_COLOUR,
                      anchor="center").pack(fill="both", expand=True)
            return

        self.fig = Figure(figsize=figsize, dpi=dpi,
                          constrained_layout=constrained)
        self.canvas = FigureCanvasTkAgg(self.fig, master=self)
        widget = self.canvas.get_tk_widget()

        if toolbar:
            row = ttk.Frame(self)
            row.pack(side="bottom", fill="x")
            self.toolbar = _Toolbar(self.canvas, row, self)
            self.toolbar.pack(side="left", fill="x", expand=True)
            self.status = ttk.Label(row, text="", foreground=STATUS_COLOUR)
            self.status.pack(side="right", padx=(0, 6))
            self.view_note = ttk.Label(row, text="",
                                       foreground=VIEW_NOTE_COLOUR)
            self.view_note.pack(side="right", padx=(0, 10))
        else:
            self.toolbar = None
            self.status = self.view_note = None

        widget.pack(side="top", fill="both", expand=True)
        self.canvas.mpl_connect("button_press_event", self._on_press)
        self.canvas.mpl_connect("button_release_event", self._on_release)
        self.canvas.mpl_connect("scroll_event", self._on_scroll)

    # -- drawing ---------------------------------------------------------

    def draw(self, fn=None):
        """Rebuild the figure with `fn(fig)` (or the panel's draw function),
        holding the view when the user has zoomed."""
        fn = fn or self._draw_fn
        if fn is not None:
            self._draw_fn = fn
        if self.fig is None:
            self.dirty = False
            return
        saved = self._remember() if (self.hold_view and self.view_locked) \
            else None
        if fn is not None:
            fn(self.fig)
        if self.toolbar is not None:
            self.toolbar.update()          # the axes are new: fresh stack
        if saved is not None and not self._restore(saved):
            self._set_locked(False)
        self.canvas.draw_idle()
        self.dirty = False

    def redraw(self):
        """Draw again with the last draw function (or just repaint)."""
        if self._draw_fn is not None:
            self.draw()
        elif self.canvas is not None:
            self.canvas.draw_idle()

    def mark_dirty(self):
        self.dirty = True

    def draw_if_visible(self):
        """Draw when dirty and on screen; the lazy-tab contract."""
        if self.dirty and self.is_visible():
            self.draw()
            return True
        return False

    def is_visible(self):
        """False only when the panel sits on a notebook tab that is not
        the one selected (so withdrawn test windows still draw)."""
        w = self
        try:
            parent = w.nametowidget(w.winfo_parent()) if w.winfo_parent()                 else None
            while parent is not None:
                if isinstance(parent, ttk.Notebook):
                    if parent.select() != str(w):
                        return False
                w = parent
                parent = w.nametowidget(w.winfo_parent())                     if w.winfo_parent() else None
        except (tk.TclError, KeyError):
            return True
        return True

    def show_placeholder(self, text):
        if self.fig is not None:
            placeholder(self.fig, text)
            self.canvas.draw_idle()

    # -- view holding ------------------------------------------------------

    def layout_key(self):
        """What makes two figure layouts "the same" for holding a view:
        the axes count, their subplot-grid cells and scales (positions
        move with the layout engine, grid cells do not).  A twinned pair
        and two stacked panels both have two axes but different keys."""
        if self.fig is None:
            return None
        key = []
        for ax in self.fig.axes:
            ss = ax.get_subplotspec() if hasattr(ax, "get_subplotspec")                 else None
            geo = tuple(ss.get_geometry()) if ss is not None else ("free",)
            key.append(geo + (ax.get_xscale(), ax.get_yscale()))
        return tuple(key)

    def _remember(self):
        if self.fig is None:
            return None
        return (self.layout_key(),
                [(ax.get_xlim(), ax.get_ylim()) for ax in self.fig.axes])

    def _restore(self, saved):
        key, limits = saved
        if key != self.layout_key():
            return False
        for ax, (xl, yl) in zip(self.fig.axes, limits):
            ax.set_xlim(xl)
            ax.set_ylim(yl)
        return True

    def _set_locked(self, locked):
        self.view_locked = bool(locked)
        if self.view_note is not None:
            self.view_note.configure(
                text="zoomed - Home to auto-scale" if locked else "")

    def lock_view(self, locked=True):
        """Hold (or release) the current view across redraws."""
        self._set_locked(locked)

    def home(self):
        """Unlock the view and redraw auto-scaled (also the toolbar Home)."""
        self._set_locked(False)
        if self.fig is None:
            return
        if self._draw_fn is not None:
            self.draw()
        else:
            for ax in self.fig.axes:
                ax.relim()
                ax.autoscale()
            if self.toolbar is not None:
                self.toolbar._stock_home()
            self.canvas.draw_idle()

    def _in_mode(self):
        return bool(self.toolbar is not None and
                    getattr(self.toolbar, "mode", ""))

    # -- mouse -------------------------------------------------------------

    def _on_release(self, event):
        if self._in_mode() and self.hold_view:
            self._set_locked(True)

    def _on_press(self, event):
        if event.button == 3 and self.copy_coords and \
                event.inaxes is not None and event.xdata is not None and \
                not self._in_mode():
            text = "%s\t%s" % (fmt_num(event.xdata), fmt_num(event.ydata))
            try:
                self.clipboard_clear()
                self.clipboard_append(text)
            except tk.TclError:
                pass
            self.set_status("copied  x = %s   y = %s" %
                            (fmt_num(event.xdata), fmt_num(event.ydata)))
            return
        if event.button == 1 and self.on_click is not None and \
                event.inaxes is not None and not self._in_mode():
            self.on_click(event)

    def _on_scroll(self, event):
        if not self.wheel_zoom or event.inaxes is None or event.xdata is None:
            return
        if getattr(event.inaxes, "name", "") == "3d":
            return                      # a 3-D view rotates; no wheel zoom
        self.zoom_at(event.inaxes, event.xdata, event.ydata,
                     1 / 1.25 if event.button == "up" else 1.25,
                     key=event.key or "")

    def zoom_at(self, ax, x, y, factor, key=""):
        """Scale the axes about (x, y): factor < 1 zooms in.  'shift' in
        key keeps x, 'control'/'ctrl' keeps y.  One Back-stack entry."""
        key = (key or "").lower()
        do_x = "shift" not in key
        do_y = not ("control" in key or "ctrl" in key)
        if self.toolbar is not None:
            self.toolbar.push_current()
        if do_x:
            ax.set_xlim(_scaled(ax.get_xlim(), x, factor, ax.get_xscale()))
        if do_y:
            ax.set_ylim(_scaled(ax.get_ylim(), y, factor, ax.get_yscale()))
        if self.hold_view:
            self._set_locked(True)
        self.canvas.draw_idle()

    # -- saving ------------------------------------------------------------

    def proposed_name(self):
        name = self.save_name() if callable(self.save_name) else self.save_name
        return safe_name(name or ("figure_" + stamp()))

    def proposed_dir(self):
        d = self.save_dir() if callable(self.save_dir) else self.save_dir
        if d and os.path.isdir(d):
            return d
        return os.path.expanduser("~")

    def save_figure(self, path=None, dpi=SAVE_DPI):
        """Save the figure; with `path` None, ask (the toolbar's Save)."""
        if self.fig is None:
            return None
        if path is None:
            path = filedialog.asksaveasfilename(
                parent=self, title="Save figure",
                initialdir=self.proposed_dir(),
                initialfile=self.proposed_name() + ".png",
                defaultextension=".png",
                filetypes=[("PNG image", "*.png"), ("PDF", "*.pdf"),
                           ("SVG", "*.svg"), ("All files", "*.*")])
            if not path:
                return None
        self.fig.savefig(path, dpi=dpi)
        self.last_saved = path
        self.set_status("saved " + os.path.basename(path))
        if self.on_status is not None:
            self.on_status("saved figure: " + path)
        return path

    def set_status(self, text):
        if self.status is not None:
            self.status.configure(text=text or "")


def _scaled(lim, c, factor, scale):
    lo, hi = lim
    if scale == "log" and lo > 0 and hi > 0 and c > 0:
        llo, lhi, lc = math.log10(lo), math.log10(hi), math.log10(c)
        return (10 ** (lc - (lc - llo) * factor),
                10 ** (lc + (lhi - lc) * factor))
    return (c - (c - lo) * factor, c + (hi - c) * factor)


# ----------------------------------------------------------------- book ----

class PlotBook:
    """Panels in a ttk.Notebook, addressed by key and drawn lazily.

    book = PlotBook(notebook)
    book.add("waves", "Waveforms", draw=self.draw_waves)
    ...
    book.mark_dirty()            # new data: every panel is stale
    book.draw_visible()          # only the tab on show draws now
    book.select("waves")         # by key, never by index

    Tabs that are not figures (tables) go into the same notebook with
    `notebook.add` as usual; the book only tracks its own panels.
    """

    def __init__(self, notebook):
        self.nb = notebook
        self.panels = {}
        self._tabs = {}
        self.nb.bind("<<NotebookTabChanged>>", self._changed, add="+")

    def add(self, key, title, draw=None, **panel_kw):
        frame = ttk.Frame(self.nb)
        panel = PlotPanel(frame, draw=draw, **panel_kw)
        panel.pack(fill="both", expand=True)
        panel.tab = frame
        panel.key = key
        self.nb.add(frame, text=title)
        self.panels[key] = panel
        self._tabs[str(frame)] = key
        return panel

    def panel(self, key):
        return self.panels[key]

    def __getitem__(self, key):
        return self.panels[key]

    def __iter__(self):
        return iter(self.panels.values())

    def __contains__(self, key):
        return key in self.panels

    def select(self, key):
        self.nb.select(self.panels[key].tab)

    def current_key(self):
        try:
            return self._tabs.get(self.nb.select())
        except tk.TclError:
            return None

    def current(self):
        key = self.current_key()
        return self.panels.get(key) if key else None

    def mark_dirty(self, keys=None):
        for key in (keys if keys is not None else self.panels):
            self.panels[key].mark_dirty()

    def draw_visible(self):
        panel = self.current()
        if panel is not None:
            return panel.draw_if_visible()
        return False

    def _changed(self, event=None):
        self.draw_visible()


__all__ = [
    "__version__", "HAVE_MPL", "NO_MPL", "FONT_SIZE", "LEGEND_SIZE",
    "NOTE_SIZE", "GRID_ALPHA", "SAVE_DPI", "LEGEND_MAX", "CH_COLOURS",
    "CMP_COLOURS", "CMP_ZORDER", "TARGET_COLOUR", "PRED_COLOUR",
    "NOTE_COLOUR", "MONO_FONT", "apply_style", "blend_white", "lighten",
    "ramp", "cmp_colour", "ch_colour", "time_unit", "fmt_num", "thin",
    "thin_step", "legend", "note", "finish", "placeholder", "stamp",
    "safe_name", "PlotPanel", "PlotBook",
]
