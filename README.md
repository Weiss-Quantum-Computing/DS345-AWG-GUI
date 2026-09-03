# DS345 AWG GUI

Panel control for a Stanford Research Systems DS345 synthesized function /
arbitrary waveform generator over GPIB. The DS345 sibling of
[BK4063B-AWG-GUI](https://github.com/Weiss-Quantum-Computing/BK4063B-AWG-GUI),
built to match it and the Scope Grab / Spectrum Grab bench tools: edit the
panel, press **Apply**, and the generator follows; **Save setup** writes the
whole instrument state to a timestamped JSON (plus a readable .txt) that can
be recalled later or kept with the data it produced.

## What it does

- **Function panel** — sine, square, triangle, ramp, noise, arb; frequency
  (or period - either box drives the other), amplitude in Vpp / Vrms / dBm,
  offset, phase, inversion, and the arb sample clock (FSMP). TTL and ECL
  level presets fill the cells as unapplied edits.
- **Modulation / sweep / burst** — AM (negative depth = DSBSC), FM, ΦM,
  linear and log sweeps, and burst, with a *TRG button for single triggers.
  The panel refuses combinations the instrument would silently reject
  (e.g. sweeping NOISE), and warns what a burst rests at between triggers.
- **Arbitrary waveforms** — a shape builder (Gaussian, Blackman-Harris,
  sech, chirp, multitone, ramps, ...), a multi-segment sequence builder
  specified in real time units, file loading (Scope Grab CSVs replay
  directly), and typed/pasted values. Upload uses the LDWF? binary protocol
  (12-bit, 8-16,300 points, 40 MHz/N clock).
- **Local waveform copies** — the DS345 holds one arb, anonymously, and
  cannot read it back; every upload is kept as a CSV in `Waveforms/` so the
  preview can always draw what the generator is playing.
- **Preview** — what Apply would produce, including modulation, sweeps and
  bursts, with the pending record overlaid dashed and an aliasing warning
  when the picture cannot be trusted. Beside it, the limits the generator
  would refuse: a frequency past the ceiling for that function (100 kHz on
  triangle and ramp, 30.2 MHz on sine and square), an FM or sweep that
  reaches past it, a sample clock over 40 MHz, and an amplitude and offset
  that together need more than the ±5 V the output stage has. The DS345's
  whole error channel is one bit in `*ESR`, so a refusal otherwise comes
  back as "execution error" with no word on which setting caused it.

## Zoom and pan on the preview

Matplotlib's own toolbar sits under the plot, the same one the ILC panel
uses: drag a rectangle to zoom, pan, step back and forward through views,
save the figure as a PNG. It earns its place on an arb in particular — a
16,300-point record drawn across two repeats is a few pixels per sample, and
zooming is the only way to see the shape of one edge.

The preview redraws on every keystroke, so a zoom would otherwise last until
the next character typed — and typing is exactly what you do while looking at
a zoomed edge. It does not: a drag that ends in zoom or pan mode holds that
view, and every redraw after it keeps the same window while the trace
underneath changes. **Home** gives auto-scaling back. A plain click on the
canvas freezes nothing.

A held view is worth knowing about, because it looks exactly like a broken
one — raise the amplitude with a zoom on and the trace simply leaves the
window. So the toolbar row says `zoomed - Home to auto-scale` whenever the
picture is not scaling itself.

## The one big difference from the 4063B

The DS345 has **no output switch** - the BNC always drives what the settings
say, so Apply changes the output immediately (it asks first, by default).
The MUTE button is the nearest thing the instrument offers: it sets 0 Vpp
and 0 V offset, remembering what it displaced so LIVE can put it back.

## Requirements

NI-488.2 (or any VISA with GPIB) plus:

```
pip install pyvisa numpy matplotlib
```

matplotlib only draws the preview - everything else works without it.

Run with `pythonw ds345_awg_gui.py` (no console window).

## Connecting

Leave the address box blank and press Connect to scan the bus - the first
scan of a quiet bus takes about a minute, and the found address is
remembered in `%APPDATA%\DS345-AWG-GUI\config.json` so later sessions
connect instantly. Or type the address (e.g. `GPIB0::19::INSTR`) directly.

If nothing is found: check the DS345 is powered on, and on its front panel
that GPIB is enabled with a free address (SHIFT then GPIB shows it - the
SR760 on this bench already holds address 10).
