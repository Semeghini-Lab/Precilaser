# Precilaser Amplifier GUI

Live status and control for Precilaser amplifiers (`GUI.py`). Configure ports in `devices.json`, then run:

```powershell
py -3 GUI.py
```

Requires Python 3, `pyserial`, and Tkinter.

Each card shows interlocks (PD/T), stage currents, photodiodes, temperatures, and controls (enable/disable drivers, actual current, target current). Enter a target current and press **Enter** to ramp.

## Safety features

### Connection
- No status for **3 s** → card marked disconnected; controls disabled
- Failed ports retry automatically; cards stay visible but greyed out

### Current changes (always ramp)
Current is never jumped in one step. Enter a target and press **Enter** to ramp:

- **0.5 A** steps, **1 s** apart
- Blocked if any **PD/T interlock** is faulted
- Re-checked **before every step**; ramp aborts if an interlock fault appears
- Only one ramp per amplifier at a time

### Disable Driver
- If current ≈ 0 → disable immediately
- If current > 0 → **ramp to 0 A** first (same rules as above), then disable only when current is near zero
- If the ramp-to-zero fails (interlock fault), drivers stay enabled

### Enable Driver
- Enables all three stages together; does not change current

### Other
- Commands spaced ≥ **~250 ms** (protocol requires ≥ 200 ms)
- Interlocks, PD trips, and stage colors show faults from live status (not button state)
- Does **not** replace hardware interlocks or manufacturer startup/shutdown procedure
