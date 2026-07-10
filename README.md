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
- an estimated **thermal output** (`thermal_power_w`, signed: +ve heating /
  −ve cooling), a heating/cooling/idle **`mode`** (inferred from the loop dT
  sign while the compressor runs), and a **COP** (`cop`, valid in both
  directions via `|thermal|/power`), derived from a constant loop flow
  (`loop_flow_gpm`, default 11 GPM = the Arctic 040A design flow) x the loop
  dT (outlet − inlet), corrected for the loop fluid: set `glycol_pct`
  (propylene-glycol vol %, cp/density derived automatically) or override
  `fluid_cp_j_kgk` / `fluid_density_kg_l` directly. The unit has no flow
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
  thermal_power_w REAL,      -- estimated loop heat transfer (W; +heat / -cool)
  cop REAL,                  -- estimated coefficient of performance
  mode TEXT,                 -- 'heating' / 'cooling' / 'idle'
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

## Home Assistant

The SQLite log is written regardless of any HA integration. Two optional ways to
also surface the data in Home Assistant:

### MQTT discovery (recommended)

Set `mqtt_enabled: true` and point `mqtt_host`/`mqtt_username`/`mqtt_password` at
your broker (e.g. the HA Mosquitto add-on). The app publishes proper HA entities
via [MQTT discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery),
grouped under one **device per heat pump** (named `hp_name`) with correct
`device_class` / `state_class` / units:

- `sensor.*_power`, `*_thermal_output`, `*_cop`, `*_compressor_freq`,
  `*_water_tank_temp`, `*_outlet_water_temp`, `*_inlet_water_temp`,
  `*_outdoor_air_temp`, `*_discharge_temp`, `*_suction_temp`,
  `*_hot_water_setpoint`, `*_mode`
- `binary_sensor.*_compressor`, `binary_sensor.*_water_pump`

Entity ids derive from the device name, so with the default
`hp_name: Arctic HP1 (Sniffer)` you get `sensor.arctic_hp1_sniffer_power`, etc.
There is **no fault/problem binary sensor** here on purpose — if you already run
a separate Arctic climate integration it provides one, and a duplicate just adds
noise; faults still live in the SQLite `faults` column.

Because the power sensor carries `state_class: measurement`, HA keeps **hourly
long-term statistics** for it — a `statistics-graph` card (period `hour`,
`days_to_show: 2`) gives a 48h power history in 1h buckets without any helper.

State is published as a single retained JSON blob on `arctic/<hp_id>/state`, with
availability on `arctic/<hp_id>/availability` (an MQTT last-will marks the device
offline if the logger dies). Discovery configs are retained under
`<mqtt_discovery_prefix>/<component>/arctic_<hp_id>/<object>/config`.

Requires `paho-mqtt` in the AppDaemon container. Store the broker password in
`/conf/secrets.yaml` as `arctic_mqtt_password:` and reference it with
`mqtt_password: !secret arctic_mqtt_password` — **AppDaemon reads secrets only at
startup, so restart the container after first creating that file.**

### Multiple heat pumps

Each unit has its own sniffer and its own logger instance. Add a second app block
(`arctic_logger_hp2:`) pointing at the second sniffer with its own `host`,
`db_file`, `hp_id: hp2`, and `hp_name`. `hp_id` namespaces the MQTT topics and
unique ids so the two devices never collide.

### Legacy `publish_sensors`

Set `publish_sensors: true` to instead publish a curated subset via AppDaemon
`set_state`. This path creates **ephemeral entities with no long-term
statistics** and is kept only for backwards compatibility — prefer MQTT
discovery above. Off by default.

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
The core dependency is `requests` (ships with the AppDaemon image); `sqlite3` is
in the standard library. MQTT discovery (optional) additionally needs
`paho-mqtt` — add it to the AppDaemon container's Python packages if you enable
`mqtt_enabled`.

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
| `hp_id` | Short id namespacing MQTT topics/unique-ids (default `hp1`). |
| `hp_name` | HA device name for this unit (default `Arctic HP1 (Sniffer)`). |
| `loop_flow_gpm` | Constant loop flow for the COP estimate (default 11 GPM). |
| `glycol_pct` | Propylene-glycol vol % (derives cp/density; default 25). |
| `fluid_cp_j_kgk` / `fluid_density_kg_l` | Explicit fluid override (skips `glycol_pct`). |
| `cop_min_input_w` | Ignore COP below this input power (default 200 W). |
| `db_file` | SQLite path (default `arctic_log.db` in the app dir). |
| `mqtt_enabled` | Publish HA entities via MQTT discovery (default `false`). |
| `mqtt_host` / `mqtt_port` | Broker address (default port 1883). |
| `mqtt_username` / `mqtt_password` | Broker credentials (use `!secret`). |
| `mqtt_discovery_prefix` | HA discovery topic prefix (default `homeassistant`). |
| `publish_sensors` | Legacy `set_state` sensors, no statistics (default `false`). |
| `sensor_prefix` | Prefix for the legacy sensors (default `arctic`). |

## Files

| File | Purpose |
| --- | --- |
| `arctic_logger.py` | The app logic + Macon decode tables. |
| `arctic_logger.yaml` | App configuration (edit for your setup). |
| `install/logs.yaml` | `logs:` fragment merged into `appdaemon.yaml`. |

## License

MIT
