"""Smoke test for the DS345 AWG GUI's preview on the shared plot panel.

Hardware-free: the panel is built withdrawn and off screen, Connect is
stubbed out before the App exists (auto-connect fires 300 ms after start,
and a real one would walk the GPIB bus while the bench may be in use), and
CONFIG_PATH is pointed at a temp folder first - the real
%APPDATA%\\DS345-AWG-GUI\\config.json carries the remembered address and
must not be touched.

Run:  python tests\\smoke_test.py
"""

import os
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import tkinter as tk                                    # noqa: E402

import ds345_awg_gui as gui                             # noqa: E402

PASSED = 0


def check(cond, what):
    global PASSED
    if not cond:
        raise AssertionError(what)
    PASSED += 1
    print("ok  " + what)


# --- sandbox, then stub, then build ----------------------------------------
tmp = tempfile.mkdtemp(prefix="ds345_smoke_")
gui.CONFIG_PATH = os.path.join(tmp, "config.json")
check(not os.path.exists(gui.CONFIG_PATH), "CONFIG_PATH sandboxed to a temp dir")
gui.App.do_connect = lambda self: None
check(gui.labplot is not None, "labplot imported from beside the GUI")
check(gui.labplot.HAVE_MPL, "matplotlib available to labplot")

root = tk.Tk()
root.withdraw()
app = gui.App(root)
root.geometry("+6000+200")
root.update()
check(app.preview is not None and app.preview.fig is not None,
      "preview is a labplot.PlotPanel with a figure")
check(app.preview.toolbar is not None, "the panel has the toolbar")
check(app.warn_note.master is app.preview.controls,
      "switches row lives in panel.controls")
check(gui.OUT_COLOUR == gui.labplot.CH_COLOURS[1] == "#1f77b4",
      "OUT_COLOUR is labplot's CH1 blue")
check(gui.PENDING_COLOUR == gui._lighten(gui.OUT_COLOUR, 0.42),
      "PENDING_COLOUR from labplot.lighten matches the old mix")

# --- a sine on the output and a built pending record ----------------------
app.vars["FUNC"].set("SINE")
app.on_function()
app.vars["FREQ"].set("1000")
app.vars["AMPL"].set("1")
app.vars["AMPL_UNIT"].set("VP")
app.vars["OFFS"].set("0")
app.vars["PHSE"].set("0")
app.vars["FSMP"].set("1e6")
pulse = np.concatenate([np.ones(100), np.zeros(200)])
app.arb_samples = pulse
app.arb_element = pulse
app.show_out.set(True)
app.show_pending.set(True)
app.redraw_preview()
root.update()
fig = app.preview.fig
check(len(fig.axes) == 1, "one axes drawn")
ax = fig.axes[0]
check(len(ax.lines) == 2, "output and pending traces both drawn")
labels = [ln.get_label() for ln in ax.lines]
check(labels[0].startswith("SINE") and labels[1].startswith("pending (300 pts"),
      "traces labelled: %s" % labels)
check(ax.get_xlabel() == "time (ms)" and ax.get_ylabel() == "volts",
      "axes labelled through labplot.finish")
check(ax.get_legend() is not None, "legend through labplot.legend")
check(app.warn_note.cget("text") == "", "no limit warning for a legal setup")

# --- zoom, hold across a redraw, Home releases ------------------------------
xl0, yl0 = ax.get_xlim(), ax.get_ylim()
cx, cy = sum(xl0) / 2, sum(yl0) / 2
app.preview.zoom_at(ax, cx, cy, 0.8)
xl1, yl1 = ax.get_xlim(), ax.get_ylim()
check(xl1 != xl0 and (xl1[1] - xl1[0]) < (xl0[1] - xl0[0]),
      "zoom_at narrows the view")
check(app.preview.view_locked, "zoom locks the view")
check(app.preview.view_note.cget("text") == "zoomed - Home to auto-scale",
      "blue note says the view is held")
app.vars["AMPL"].set("2")        # a keystroke: redraws through on_edit
root.update()
ax = fig.axes[0]
check(np.allclose(ax.get_xlim(), xl1) and np.allclose(ax.get_ylim(), yl1),
      "view held across a redraw from a panel edit")
check(abs(max(ax.lines[0].get_ydata())) > 0.9,
      "and the trace under it changed (2 Vpp sine)")
app.preview.home()
root.update()
ax = fig.axes[0]
check(not app.preview.view_locked and app.preview.view_note.cget("text") == "",
      "Home releases the hold and clears the note")
check(not np.allclose(ax.get_ylim(), yl1), "Home redraws auto-scaled")

# --- save ------------------------------------------------------------------
check(app.preview.proposed_name() == "ds345_preview", "Save proposes ds345_preview")
app.outdir.set(tmp)
check(app.preview.proposed_dir() == tmp, "Save proposes the output folder")
path = os.path.join(tmp, "smoke.png")
out = app.preview.save_figure(path)
check(out == path and os.path.getsize(path) > 1000, "save_figure wrote a PNG")
root.update()
check("saved figure: " + path in app.logbox.get("1.0", "end"),
      "the save was logged through on_status")

# --- the empty state: placeholder, limits still shown -------------------------
app.vars["FREQ"].set("1e8")      # past the 30.2 MHz sine ceiling: a limit
app.show_out.set(False)
app.show_pending.set(False)
app.redraw_preview()
root.update()
check(len(fig.axes) == 0 and any("nothing selected" in t.get_text()
                                 for t in fig.texts),
      "no traces ticked: labplot.placeholder")
check("past the SINE ceiling" in app.warn_note.cget("text"),
      "limit warning still shown on the placeholder: %r"
      % app.warn_note.cget("text"))
app.show_out.set(True)
app.vars["FREQ"].set("1000")

# --- the ARB-phase rule -------------------------------------------------------
app.known_waves["smoke"] = pulse
app.device_wave = "smoke"
app.vars["FUNC"].set("ARB")
app.on_function()
app.vars["PHSE"].set("180")      # left in the box; the cell is greyed under ARB
root.update()
check(str(app.widgets["PHSE"].cget("state")) == "disabled",
      "PHSE cell greyed under ARB")
check(app.panel_vals()["PHSE"] == "", "a greyed cell reads empty in panel_vals")
app.redraw_preview()
root.update()
ax = fig.axes[0]
y = ax.lines[0].get_ydata()
check(ax.lines[0].get_label().startswith("ARB 'smoke'"), "ARB trace drawn")
check(y[0] > 0.9 and y[0] == max(y),
      "ARB preview starts at the record's first sample, not rotated by PHSE")
vals = dict(app.panel_vals(), PHSE="180")
t, v, _ = gui.preview_curve("ARB", vals, arb=pulse, span=600e-6)
check(v[0] > 0.9, "preview_curve itself ignores a PHSE given for an ARB")
check(abs(gui.idle_volts("ARB", vals, 180.0, arb=pulse) - v[0]) < 1e-9,
      "idle_volts rests an ARB at its first sample too")

# --- teardown -----------------------------------------------------------------
check(not os.path.exists(gui.CONFIG_PATH), "nothing written to config yet")
app.save_config()
check(os.path.exists(gui.CONFIG_PATH), "save_config writes the sandboxed path")
app.awg.close()
root.destroy()
print("All %d checks passed." % PASSED)
