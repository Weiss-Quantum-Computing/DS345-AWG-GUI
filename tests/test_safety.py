"""Hardware-free checks of the DS345 panel's and instrument layer's safety rules.

Run:  python tests\\test_safety.py

pyvisa.ResourceManager inside the GUI module is swapped for FakeRM, so the
real Ds345 class runs its own GPIB exchanges against FakeInst objects (a
DS345 and an SR760 on the bus) and every write is recorded. Worker threads
run inline and every dialog is scripted: an unexpected dialog fails the test.

As in smoke_test.py, CONFIG_PATH and WAVE_CACHE go to a temp folder and
App.do_connect is a no-op BEFORE the App is built. Nothing here opens VISA.
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

CHECKS = 0


def ok(cond, what):
    global CHECKS
    CHECKS += 1
    if not cond:
        raise AssertionError("FAILED: " + what)
    print("  ok  " + what)


def raises(exc_type, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc_type as exc:
        return str(exc) or True
    return False


# ------------------------------------------------------------------ the fake
DS345_IDN = "StanfordResearchSystems,DS345,12345,1.04"
SR760_IDN = "Stanford_Research_Systems,SR760,s/n 999,ver 1.0"


class FakeInst:
    def __init__(self, res, idn, bus):
        self.res, self.idn, self.bus = res, idn, bus
        self.writes, self.queries, self.raw = [], [], []
        self.timeout = 0
        self.read_termination = self.write_termination = "\n"
        self.state = {"FUNC": "0", "FREQ": "1000.", "FSMP": "40000000.",
                      "AMPL": "1.00VP", "OFFS": "0.", "PHSE": "0.",
                      "INVT": "0", "MENA": "0", "MTYP": "1", "MDWF": "0",
                      "DPTH": "50", "FDEV": "10.", "PDEV": "45.",
                      "RATE": "1.", "BCNT": "1", "STFR": "1.", "SPFR": "10.",
                      "TSRC": "0", "TRAT": "1.", "AMRT": "1."}

    def query(self, cmd):
        self.queries.append(cmd)
        self.bus.append((self.res, cmd))
        if cmd == "*IDN?":
            return self.idn
        if cmd == "*ESR?":
            return "0"
        if cmd.startswith("LDWF?"):
            return "1"
        return self.state.get(cmd.rstrip("?"), "")

    def write(self, cmd):
        self.writes.append(cmd)
        self.bus.append((self.res, cmd))
        head, _, val = cmd.partition(" ")
        if head in self.state and val:
            self.state[head] = val

    def write_raw(self, data):
        self.raw.append(bytes(data))

    def clear(self):
        pass

    def close(self):
        pass


class FakeRM:
    """A GPIB bus: the DS345 at 19, the SR760 at 10, nothing elsewhere."""
    devices = {}
    bus = []
    listed = ()

    def __init__(self, *a, **k):
        pass

    def list_resources(self):
        return tuple(self.listed)

    def open_resource(self, res):
        FakeRM.bus.append((res, "open"))
        if res not in FakeRM.devices:
            raise OSError("VI_ERROR_TMO")
        return FakeRM.devices[res]

    def close(self):
        pass


def fresh_bus():
    FakeRM.bus = []
    FakeRM.devices = {
        "GPIB0::19::INSTR": FakeInst("GPIB0::19::INSTR", DS345_IDN, FakeRM.bus),
        "GPIB0::10::INSTR": FakeInst("GPIB0::10::INSTR", SR760_IDN, FakeRM.bus),
    }
    FakeRM.listed = ()
    return FakeRM.devices


gui.pyvisa.ResourceManager = FakeRM


def connected_ds345():
    fresh_bus()
    dev = gui.Ds345()
    dev.connect("GPIB0::19::INSTR")
    return dev


# ------------------------------------------------------------ library checks
def library_checks():
    print("Ds345 instrument layer")
    dev = connected_ds345()
    ok(dev.addr == "GPIB0::19::INSTR", "fake DS345 connects at a typed address")

    # DS-2: NaN/inf is refused before LDWF, not uploaded as a zero record.
    gap = np.sin(np.linspace(0, 6, 64))
    gap[[5, 9]] = [np.nan, np.inf]
    for norm in (True, False):
        ok(raises(ValueError, gui.dac_samples, gap, normalize=norm),
           f"dac_samples refuses NaN/inf (normalize={norm})")
    dev.inst.queries.clear()
    msg = raises(ValueError, dev.upload_arb, gap)
    ok(msg and "2 of 64" in msg and dev.inst.raw == []
       and not any(q.startswith("LDWF") for q in dev.inst.queries),
       "upload_arb refuses it before LDWF?, sending nothing")
    ok(dev.upload_arb(np.nan_to_num(gap, posinf=0.0)) == 64
       and len(dev.inst.raw) == 1, "the same record without the gap uploads")


# ---------------------------------------------------------------- GUI checks
class SyncThread:
    def __init__(self, target=None, daemon=None, args=(), kwargs=None):
        self.target, self.args, self.kwargs = target, args, kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


class Dialogs:
    """Scripted answers; each call is recorded as (kind, title, text)."""

    def __init__(self):
        self.calls = []
        self.answers = []

    def make(self, kind):
        def dialog(title="", message="", **kw):
            self.calls.append((kind, title, message))
            if not self.answers:
                raise AssertionError(f"unexpected {kind}: {title!r}")
            return self.answers.pop(0)
        return dialog

    def expect(self, *answers):
        self.calls.clear()
        self.answers = list(answers)


def build_app(tmp):
    gui.CONFIG_PATH = os.path.join(tmp, "config.json")   # before App
    gui.WAVE_CACHE = os.path.join(tmp, "Waveforms")
    gui.App.do_connect = lambda self: None                # before App
    gui.threading.Thread = SyncThread
    dlg = Dialogs()
    for kind in ("showerror", "showwarning", "showinfo", "askyesno",
                 "askokcancel", "askyesnocancel"):
        setattr(gui.messagebox, kind, dlg.make(kind))
    root = tk.Tk()
    root.withdraw()
    app = gui.App(root)
    root.geometry("+6000+200")
    root.update()
    return root, app, dlg


def read_all(app, root):
    app.after_read(app.awg.read_state(), overwrite=True)
    root.update()


def gui_checks():
    print("panel")
    tmp = tempfile.mkdtemp(prefix="ds345_safety_")
    root, app, dlg = build_app(tmp)
    app.awg = connected_ds345()
    inst = app.awg.inst
    app.set_busy(False)

    # DS-1: the live-upload check reads the generator's FUNC, not the cell.
    inst.state["FUNC"] = "5"                      # playing ARB
    read_all(app, root)
    ok(app.inst_vals["FUNC"] == "ARB" and app.out_state != "MUTED",
       "fake read: the generator plays ARB, not muted")
    app.vars["FUNC"].set("SINE")                  # typed, not applied
    app.on_function()
    root.update()
    app.arb_samples = np.sin(np.linspace(0, 2 * np.pi, 64))
    app.arb_element = app.arb_samples
    dlg.expect(False)
    app.do_upload()
    root.update()
    ok(len(dlg.calls) == 1 and "live output" in dlg.calls[0][1]
       and "playing ARB" in dlg.calls[0][2] and inst.raw == [],
       "Upload asks while the generator plays ARB, cell says SINE; "
       "Cancel sends nothing")
    app.vars["FUNC"].set("ARB")
    inst.state["FUNC"] = "0"                      # generator on SINE
    read_all(app, root)
    app.vars["FUNC"].set("ARB")                   # typed ARB, not applied
    app.on_function()
    app.arb_samples = np.sin(np.linspace(0, 2 * np.pi, 64))
    app.arb_element = app.arb_samples
    dlg.expect()
    app.do_upload()
    root.update()
    ok(dlg.calls == [] and len(inst.raw) == 1,
       "generator on SINE (cell says ARB): uploads without asking")

    # DS-2: take_table / pick_column refuse a NaN in the column used.
    good = np.column_stack([np.arange(40.0), np.linspace(-1, 1, 40),
                            np.linspace(-1, 1, 40)])
    app.take_table(good, None, "good.csv", "good")
    before = app.arb_samples.copy()
    bad = good.copy()
    bad[6, 1] = np.nan
    dlg.expect(None)
    app.take_table(bad, None, "gap.csv", "gap")
    ok(len(dlg.calls) == 1 and dlg.calls[0][0] == "showerror"
       and "data row 7" in dlg.calls[0][2]
       and np.array_equal(app.arb_samples, before),
       "take_table refuses a NaN in the sample column, naming row 7")
    bad = good.copy()
    bad[0, 2] = np.inf
    app.take_table(bad, None, "third.csv", "third")
    dlg.expect(None)
    app.arb_col.set(app.arb_col_box.cget("values")[2])
    app.pick_column()
    ok(len(dlg.calls) == 1 and app.arb_index == 1
       and np.all(np.isfinite(app.arb_samples)),
       "picking the inf column is refused and column 2 kept")

    root.update()
    root.destroy()


def main():
    library_checks()
    gui_checks()
    print("All %d checks passed." % CHECKS)


if __name__ == "__main__":
    main()
