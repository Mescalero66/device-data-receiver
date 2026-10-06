# Device Data Receiver

PC software with two tabs, which can both run at the same time:

- **UART**: the MQ gas sensor board's UART broadcast (`$PMQB` sentences, see
  [INTERFACE.md](https://github.com/Mescalero66/mq-board-micropython/blob/main/INTERFACE.md)).
  It shows the six sensors live and appends every new reading to a daily CSV file.
- **Modbus**: polls registers from a Modbus TCP device (e.g. the PowerTec Pico
  environment sensor), graphs up to six of them and logs every poll to a daily CSV
  file. See [Modbus tab](#modbus-tab).

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
    python mqb_receiver.py --modbus             # also start polling the saved Modbus device
    python mqb_receiver.py --modbus-sim         # Modbus tab on a simulated sensor

Other options: `--raw` also logs every received line verbatim; `--out DIR`
changes the log folder (default `data/`); `--sim-speed N` speeds up the simulator.

The **UART** tab shows:

- **Table**: the latest sentence per sensor. *vs clean air* is ppm divided by the
  sensor's datasheet clean-air floor; *Since reading* should never exceed
  about 150 s. Rows turn red for a read fault or failed boot calibration, amber
  for a stale sensor or a link down.
- **Board temperature** (right of the table): the temperature sent with every
  reading, from all six sensors together, over the same window as the charts.
- **Charts**: ppm per sensor over the chosen window. Hollow circles are
  provisional readings (boot-calibration R0, `tracking=0`), red crosses are
  readings with no ppm, dotted lines are board restarts. Hover for values.
  *Fix axis* starts each Y axis at zero (up to the highest reading shown or the
  clean-air level), so small noise does not look like big swings; it and
  *Log scale* are alternatives, so ticking one unticks the other. Fix axis also
  applies to the temperature chart; Log scale does not.
  *Import CSV...* adds logs from earlier runs to the charts (see below);
  *Clear charts* empties them without touching the log files.
- **Events**: restarts, rejected lines (with the reason, e.g. checksum
  mismatch), deviations from the spec, stale sensors and link loss. Untick
  *Events* above the charts to hide this pane and give the charts the room;
  events are still recorded, and the box counts new ones until you show it again.
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

## Modbus tab

Polls one block of registers from a Modbus TCP device every few seconds. It
runs independently of the UART tab, so both keep receiving, logging and
charting whichever tab is showing.

1. Enter the **Device IP**, **Port** (normally 502) and **Unit ID** (often
   ignored by the device; 1 is usual), what to **Read** (holding registers,
   input registers, coils or discrete inputs), the first register (**from**)
   and how many (**count**, up to 125 registers per poll), then **Connect**.
   *Poll every* can be changed while connected.
2. The register table lists every polled register with its raw value and hex,
   live. Register numbers are protocol addresses as in the device's
   documentation (0-based); *Ref* shows the same register in the 1-based
   `40001` style.
3. For each of the six graphs, choose a **Register**, a **Name**, what it
   **Shows** (temperature, humidity, ppm or ppb), the **Format** (`uint16`,
   `int16`, or `uint32`/`int32`/`float32` over two registers, high word first)
   and a **Scale**. Choosing what it shows fills in the usual format and scale
   (temperature: `int16` x 0.1). Charts re-plot their whole history straight
   away when these change, so use the raw values in the table to check a scale.

Settings are saved to `modbus_settings.json` and restored next time. The
defaults suit the PowerTec Pico environment sensor with one sensor of each
kind: Temp 01 (register 10, x 0.1), Humi 01 (11), AQS1 eCO2 (30, ppm) and
AQS1 TVOC (31, ppb). The humidity scale is not documented; if the table shows
e.g. `455` for 45.5 %RH, set its scale to 0.1.

If the device stops answering, the tab keeps retrying and says so once in
Events. Some devices refuse to read a block that includes registers they do not
define; the tab then reads the registers one at a time (shown in the status
line) and leaves the undefined ones blank.

Tick **Simulated device** (or start with `--modbus-sim`) to try it without
hardware: a simulated environment sensor runs on this PC, and its logs go to
`data/sim/`.

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

`data/modbus_<ip>_YYYY-MM-DD.csv` gets one row per successful Modbus poll:
`pc_time`, then one column per register with its **raw** value (before format
and scale), named by type and address, e.g. `hr10` for holding register 10
(`ir` input, `co` coil, `di` discrete input). Empty = that register could not
be read. Raw values mean any register can be graphed later, whatever the graph
setup was. If you poll a different set of registers on the same day, a new
file (`..._2.csv`) is started rather than mixing columns.

## Files

| File               | Contents                                                        |
|--------------------|-----------------------------------------------------------------|
| `mqb_receiver.py`  | entry point: window (UART tab) and console mode                 |
| `mqb_core.py`      | serial port, line framing, CSV/raw logs and import, background receiver |
| `mqb_protocol.py`  | sentence parsing and validation, new-reading/restart tracking   |
| `mqb_sim.py`       | simulated board (`python mqb_sim.py` prints a sample stream)    |
| `gui_common.py`    | window parts both tabs share: theme, Events pane, chart styling, hover |
| `modbus_tab.py`    | the Modbus tab                                                  |
| `modbus_core.py`   | Modbus TCP client, value decoding, polling thread, CSV log, settings |
| `modbus_sim.py`    | simulated environment sensor (`python modbus_sim.py` serves it on port 5020) |
| `test_mqb.py`      | tests, including every example in INTERFACE.md: `python -m unittest -v` |
| `test_modbus.py`   | Modbus tests, run against the simulated sensor over real sockets |
