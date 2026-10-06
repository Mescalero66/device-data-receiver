# Device Data Receiver

PC software for the MQ gas sensor board's UART broadcast (`$PMQB` sentences,
see [https://github.com/Mescalero66/mq-board-micropython/blob/main/INTERFACE.md](INTERFACE.md)). It shows the six sensors live and appends
every new reading to a daily CSV file.

## Setup

Requires Python 3.10+ with Tk (included in the python.org Windows installer).

    pip install -r requirements.txt

## Wiring

| Board (ESP32-S3)  | USB-UART adapter |
|-------------------|------------------|
| UART TX           | RX               |
| GND               | GND              |

The board only transmits, so the adapter's TX can be left unconnected. Use a
3.3 V logic adapter, or at least never connect a 5 V adapter's TX to the board.

The adapter appears as a COM port. 

## Running

Double-click `run.bat`, or:

    python mqb_receiver.py                      # window; pick the port and press Connect
    python mqb_receiver.py --port COM7          # window, connect straight away
    python mqb_receiver.py --cli --port COM7    # console only (e.g. for long unattended logging)
    python mqb_receiver.py --sim                # simulated board, no hardware needed
    python mqb_receiver.py --sim --sim-faults   # simulated faults, bad checksums and a restart
    python mqb_receiver.py data\mqb_2026-10-04.csv   # window showing an earlier log on the charts

Other options: `--raw` also logs every received line verbatim; `--out DIR`
changes the log folder (default `data/`); `--sim-speed N` speeds up the simulator.

The window shows:

- **Table**: the latest sentence per sensor. *vs clean air* is ppm divided by the
  sensor's datasheet clean-air floor; *Since new reading* should never exceed
  about 150 s. Rows turn red for a read fault or failed boot calibration, amber
  for a stale sensor or a link down.
- **Charts**: ppm per sensor over the chosen window. Hollow circles are
  provisional readings (boot-calibration R0, `tracking=0`), red crosses are
  readings with no ppm, dotted lines are board restarts. Hover for values.
  *Import CSV...* adds logs from earlier runs to the charts (see below);
  *Clear charts* empties them without touching the log files.
- **Events**: restarts, rejected lines (with the reason, e.g. checksum
  mismatch), deviations from the spec, stale sensors and link loss.
- **Status line**: counts of lines, valid sentences, rejects and readings, plus
  the estimated board uptime.

If the port drops (e.g. the adapter is unplugged), the receiver keeps retrying
until it comes back.

### Viewing earlier logs

*Import CSV...* (or CSV paths on the command line, or CSV files dropped onto
`run.bat`) loads `mqb_*.csv` logs onto the charts, with or without a board
connected. You can select several files at once. Imported readings are merged
with whatever is already shown:

- A reading already on the charts (e.g. importing today's file while logging)
  is not added twice, and neither is one logged twice because the program was
  restarted mid-cycle.
- Board restarts are found from the `t_s` values, including between files
  imported together, and drawn as dotted lines.
- The chart window widens if needed to fit the imported data. When no board
  is connected, the window ends at the newest reading rather than the present.

Imported data only goes on the charts; it is never written back to the logs.

## Log files

`data/mqb_YYYY-MM-DD.csv` gets one row per **new** reading. The board repeats
each reading about 15 times, and only the first copy is logged. Files roll over
at local midnight. Restarting the program appends to the same day's file.
Simulator runs log to `data/sim/` so they don't mix with real data.

| Column           | Meaning                                                                 |
|------------------|-------------------------------------------------------------------------|
| `pc_time`        | PC time the reading was first received                                  |
| `board_time_est` | estimated time the board took it (boot-time estimate + `t_s`)           |
| `sensor`, `gas`  | e.g. `MQ4`, `CH4`                                                       |
| `t_s`            | board uptime at the reading; goes back to small values after a restart  |
| `ppm`            | empty = no valid reading (never written as 0)                           |
| `temp_c`         | empty = no temperature compensation                                     |
| `tracking`       | 1 = tracked baseline, 0 = provisional boot calibration                  |
| `r0_kohm`        | R0 used for this ppm (lets you recompute ppm against another R0)        |
| `flags`          | space-separated: `no_ppm`, `provisional`, `placeholder_r0`, `uncompensated`, `temp_out_of_range` |

With `--raw` (or the *Also log raw lines* box), `data/mqb_raw_YYYY-MM-DD.log`
records every line with a millisecond timestamp and a verdict (`ok`, `WARN ...`
or `REJECT ...`). This is useful for checking the firmware against the spec.

## Files

| File               | Contents                                                        |
|--------------------|-----------------------------------------------------------------|
| `mqb_receiver.py`  | entry point: GUI and console mode                               |
| `mqb_core.py`      | serial port, line framing, CSV/raw logs and import, background receiver |
| `mqb_protocol.py`  | sentence parsing and validation, new-reading/restart tracking   |
| `mqb_sim.py`       | simulated board (`python mqb_sim.py` prints a sample stream)    |
| `test_mqb.py`      | tests, including every example in INTERFACE.md: `python -m unittest -v` |
