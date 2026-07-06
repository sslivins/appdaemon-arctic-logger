# appdaemon-arctic-logger

An [AppDaemon](https://appdaemon.readthedocs.io/) app that **continuously logs an
Arctic (Macon) heat pump** to a local SQLite database. It polls an
[`arctic-sniffer`](https://github.com/sslivins/arctic-sniffer) (an ESP32 that
passively decodes the Macon Modbus bus) and records every register value over
time so the unit's behaviour can be graphed and mined for reverse-engineering.

> Personal project, shared as-is. Adapt the host / interval in
> `arctic_logger.yaml` to your own setup.

## What it does

Every `poll_interval_seconds` (default 30) it fetches:

- `GET /api/registers` — a flat `{addr: raw_byte}` map of every register the
  sniffer has seen on the wire.
- `GET /api/status` — firmware version + frame / CRC / transaction counters.

and writes **one timestamped row** to the `readings` table containing:

- the **complete raw register map** as JSON (`raw_json`), so nothing is ever
  lost for future decoding;
- **decoded columns** for the ~24 named Macon registers — temperatures
  (tank / inlet / outlet / outdoor / coils / suction / discharge / IPM), hot-water
  setpoint, electrical (AC current/voltage, DC bus, real-time power), EEV, and
  compressor frequency;
- the compressor / water-pump **status bits** (`compressor_on`, `waterpump_on`);
- a decoded **fault** string (`faults`) plus a `fault_active` flag;
- an estimated **thermal output** (`thermal_power_w`) and **COP** (`cop`),
  derived from a constant loop flow (`loop_flow_gpm`, default 11 GPM = the
  Arctic 040A design flow) x the condenser dT (outlet − inlet), using the
  loop fluid's heat properties (`fluid_cp_j_kgk` / `fluid_density_kg_l`,
  defaulting to water; set them for a glycol mix). The unit has no flow
  meter, so this is an estimate; because raw temps + input power are stored
  every row, COP can be recomputed for all history by changing these.

The decode tables (register scale/sign + the five-register fault bit map) mirror
the shared [`arctic-macon`](https://github.com/sslivins/arctic-macon) library, so
the stored decoded values match the firmware exactly. If that library's tables
change, update `_REGISTERS` / `_FAULT_BITS` in `arctic_logger.py` to match.

The sniffer is briefly unreachable during its own OTA reboots; failed polls are
skipped and logged **once** on the offline/online transition, not every poll.

## Storage

SQLite at `arctic_log.db` inside the app directory by default (gitignored);
override with `db_file`. Schema:

```
readings(
  ts TEXT PRIMARY KEY,       -- ISO-8601 UTC, seconds
  epoch INTEGER,             -- unix seconds (indexed)
  version TEXT, frames INTEGER, crc_errors INTEGER, transactions INTEGER,
  run_state INTEGER,         -- raw reg 2007
  compressor_on INTEGER, waterpump_on INTEGER,
  fault_active INTEGER, faults TEXT,
  <decoded scalar columns...>,
  thermal_power_w REAL,      -- estimated condenser heat output (W)
  cop REAL,                  -- estimated coefficient of performance
  raw_json TEXT              -- full {addr: byte} map
)
```

Example queries:

```sql
-- last 24 h of tank temperature
SELECT ts, water_tank_temp FROM readings
WHERE epoch > strftime('%s','now') - 86400 ORDER BY ts;

-- any faults ever seen
SELECT ts, faults FROM readings WHERE fault_active = 1 ORDER BY ts;
```

## Home Assistant sensors (optional)

Set `publish_sensors: true` to also publish a curated subset
(`sensor.<prefix>_water_tank_temp`, `..._hot_water_setpoint`,
`..._outdoor_ambient_temp`, `..._compressor_freq`, `..._realtime_power`, …,
`binary_sensor.<prefix>_compressor`, `binary_sensor.<prefix>_fault`) so they can
be graphed in HA. The SQLite log is written either way. Off by default.

## Installation

This app follows the [`appdaemon-base`](https://github.com/sslivins/appdaemon-base)
convention — it's a self-contained public repo cloned into `conf/apps/arctic_logger/`,
and its `install/logs.yaml` fragment is merged into the shared `appdaemon.yaml`
by `install_app.py`:

```sh
cd ~/docker/appdaemon-base
python3 install_app.py arctic_logger \
    --repo https://github.com/sslivins/appdaemon-arctic-logger \
    --conf ~/docker/appdaemon/conf \
    --restart
```

Then edit `conf/apps/arctic_logger/arctic_logger.yaml` for your sniffer's host.
The only Python dependency is `requests`, which ships with the AppDaemon image;
`sqlite3` is in the standard library.

To update: `git -C ~/docker/appdaemon/conf/apps/arctic_logger pull` then re-run
`install_app.py arctic_logger --conf ~/docker/appdaemon/conf --restart`.
To remove: add `--remove`.

## Logging

`install/logs.yaml` registers a dedicated rotating log at
`/conf/apps/arctic_logger/arctic_logger.log` under the shared `appdaemon.yaml`
`logs:` section (wired automatically by `install_app.py`).

## Manual poll (testing)

Fire the event `arctic_logger_poll` to log a row immediately without waiting for
the next interval:

```yaml
event_type: arctic_logger_poll
```

## Configuration (`arctic_logger.yaml`)

| Key | What it is |
| --- | --- |
| `host` | Sniffer IP/hostname (base URL becomes `http://<host>`). |
| `base_url` | Full base URL, if you'd rather set it explicitly. |
| `poll_interval_seconds` | Poll + store cadence (default 30). |
| `http_timeout_seconds` | Per-request HTTP timeout (default 8). |
| `db_file` | SQLite path (default `arctic_log.db` in the app dir). |
| `publish_sensors` | Publish the curated HA sensor subset (default `false`). |
| `sensor_prefix` | Prefix for published sensors (default `arctic`). |

## Files

| File | Purpose |
| --- | --- |
| `arctic_logger.py` | The app logic + Macon decode tables. |
| `arctic_logger.yaml` | App configuration (edit for your setup). |
| `install/logs.yaml` | `logs:` fragment merged into `appdaemon.yaml`. |

## License

MIT
