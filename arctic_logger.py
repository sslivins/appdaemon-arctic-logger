"""Arctic Logger - AppDaemon app.

Continuously polls an **Arctic (Macon) heat-pump sniffer** (an ESP32 running the
``arctic-sniffer`` firmware, which passively decodes the Macon Modbus bus) and
records every register value to a local **SQLite** database so the unit's
behaviour can be graphed and mined for reverse-engineering.

Each poll it fetches:

* ``GET /api/registers`` - a flat ``{addr: raw_byte}`` map of every register the
  sniffer has seen on the wire.
* ``GET /api/status``    - firmware version + frame/CRC/transaction counters.

and writes one timestamped row containing:

* the **complete raw register map** as JSON (``raw_json``) so nothing is ever
  lost for future decoding, plus
* **decoded columns** for the ~24 named Macon registers (temperatures, setpoint,
  electrical, compressor frequency), the compressor/pump status bits, and a
  decoded **fault** string.

The decode tables (register scale/sign + fault bit map) mirror the shared
``arctic-macon`` library so the stored decoded values match the firmware exactly.

The sniffer is briefly unreachable during its own OTA reboots; polls that fail
are skipped (logged once on the offline/online transition, not every poll).

No hosts or paths are hard-coded - everything comes from ``arctic_logger.yaml``.
"""

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import appdaemon.plugins.hass.hassapi as hass

import requests


# ---------------------------------------------------------------------------
# Macon decode tables - mirror of the shared arctic-macon library
# (src/macon_registers.cpp and src/macon_faults.cpp). Keep in sync if the
# canonical library changes.
# ---------------------------------------------------------------------------

# Named scalar registers: addr -> (column, unit, scale, signed).
# Decoded value = (to_signed(raw) if signed else raw) * scale.
_REGISTERS = {
    2000: ("ac_current",          "A",     1.0,   False),  # A4
    2001: ("dc_bus_voltage",      "V",     10.0,  False),  # A7
    2003: ("dc_motor_speed",      None,    1.0,   False),  # A10
    2008: ("water_tank_temp",     "\u00b0C", 1.0, True),   # o1
    2012: ("hot_water_setpoint",  "\u00b0C", 1.0, False),
    2101: ("ac_voltage",          "V",     10.0,  False),  # A13
    2104: ("main_eev",            "steps", 1.0,   False),  # A5
    2113: ("ipm_temp",            "\u00b0C", 1.0, True),   # A8
    2114: ("realtime_power",      "W",     100.0, False),  # A9
    2132: ("outlet_water_temp",   "\u00b0C", 1.0, True),   # o3
    2133: ("inlet_water_temp",    "\u00b0C", 1.0, True),   # o2
    2134: ("outdoor_ambient_temp", "\u00b0C", 1.0, True),  # o4
    2135: ("cool_coil_temp",      "\u00b0C", 1.0, True),   # A6
    2136: ("coil_temp",           "\u00b0C", 1.0, True),   # A2
    2137: ("suction_temp",        "\u00b0C", 1.0, True),   # A3
    2138: ("discharge_temp",      "\u00b0C", 1.0, True),   # A1
    2141: ("compressor_freq",     "Hz",    1.0,   False),  # A14
}

# Ordered decoded scalar columns (SQLite schema follows this order).
_SCALAR_COLUMNS = [v[0] for v in _REGISTERS.values()]

# Status byte reg 2130: bit -> label.
_STATUS_BITS = {2: "Compressor", 3: "WaterPump"}
_REG_STATUS = 2130

# The five Macon fault-bitfield registers.
_FAULT_REGS = (2007, 2125, 2126, 2127, 2128)

# Canonical fault bit table: (reg, bit, code, label, severity).
# severity: 0=INFO (skipped), 1=WARNING, 2=FAULT, 3=CRITICAL.
_FAULT_BITS = [
    (2007, 0, "P15", "Temp difference too large (PT)", 1),
    (2007, 1, "P16", "Outlet temp too low (PT)",       1),
    (2007, 2, "FE",  "FE protection",                  2),
    (2007, 3, "FF",  "FF protection",                  2),
    (2007, 5, "RUN", "Hot-water run indicator",        0),
    (2125, 0, "E28", "Outdoor EEPROM error",           2),
    (2125, 1, "E19", "Inlet water temp sensor",        2),
    (2125, 2, "E18", "Outlet water temp sensor",       2),
    (2125, 3, "E13", "Cool coil temp sensor",          2),
    (2125, 4, "E03", "E03 protection",                 2),
    (2125, 5, "E28", "Indoor EEPROM error",            2),
    (2125, 6, "E27", "Driver communication",           3),
    (2125, 7, "E21", "Controller communication",       3),
    (2126, 0, "r02", "Compressor start failure",       2),
    (2126, 1, "E26", "Indoor/outdoor communication",   3),
    (2126, 2, "r01", "IPM fault",                      3),
    (2126, 4, "E01", "Discharge temp sensor",          2),
    (2126, 5, "E09", "Suction temp sensor",            2),
    (2126, 6, "E05", "Coil temp sensor",               2),
    (2126, 7, "E22", "Ambient temp sensor",            2),
    (2127, 1, "P19", "AC current protection",          2),
    (2127, 2, "r06", "Compressor phase current",       2),
    (2127, 3, "r10", "AC voltage protection",          2),
    (2127, 4, "r11", "DC bus voltage protection",      2),
    (2127, 5, "r05", "IPM temperature protection",     2),
    (2127, 6, "P11", "High discharge temp",            2),
    (2127, 7, "P02", "High pressure protection",       3),
    (2128, 0, "P06", "Low pressure protection",        3),
    (2128, 1, "P27", "Coil overheat",                  2),
    (2128, 2, "PC",  "Ambient too high/low (PT)",      1),
    (2128, 3, "P10", "P10 protection",                 2),
    (2128, 4, "P30", "Antifreeze protection",          1),
    (2128, 5, "E05", "Coil temp sensor",               2),
    (2128, 7, "P01", "Water flow protection",          3),
]


def _to_signed(raw):
    """Interpret an 8-bit register byte as a signed int8."""
    return raw - 256 if raw > 127 else raw


def _decode_scalar(addr, raw):
    """Decode one named scalar register to its engineering value."""
    _col, _unit, scale, signed = _REGISTERS[addr]
    val = _to_signed(raw) if signed else raw
    val = val * scale
    return int(val) if scale == 1.0 else round(val, 2)


def _decode_faults(regs):
    """Return (fault_active, "CODE:label | ...") from the raw register map.

    Mirrors arctic::macon_decode_faults: skips the INFO (RUN) bit, sorts active
    faults by descending severity.
    """
    active = []
    for reg, bit, code, label, sev in _FAULT_BITS:
        if sev == 0:
            continue  # INFO / RUN indicator is not a fault
        raw = regs.get(reg)
        if raw is None:
            continue
        if raw & (1 << bit):
            active.append((sev, code, label))
    if not active:
        return 0, ""
    active.sort(key=lambda t: t[0], reverse=True)
    text = " | ".join("%s:%s" % (code, label) for _sev, code, label in active)
    return 1, text


class ArcticLogger(hass.Hass):

    def initialize(self):
        # --- Config -----------------------------------------------------------
        # Base URL of the sniffer's HTTP API, e.g. http://192.168.10.118
        host = self.args.get("host")
        base = self.args.get("base_url")
        if not base:
            if not host:
                self.log("No 'host' or 'base_url' configured - cannot poll.",
                         level="ERROR")
                return
            base = "http://%s" % host
        self.base_url = base.rstrip("/")
        self.interval = int(self.args.get("poll_interval_seconds", 30))
        self.http_timeout = int(self.args.get("http_timeout_seconds", 8))

        app_dir = os.path.dirname(os.path.abspath(__file__))
        self.db_path = self.args.get("db_file",
                                     os.path.join(app_dir, "arctic_log.db"))

        # Optional Home Assistant sensor publishing (a curated subset).
        self.publish_sensors = bool(self.args.get("publish_sensors", False))
        self.sensor_prefix = str(self.args.get("sensor_prefix", "arctic"))

        # --- COP estimation ---------------------------------------------------
        # The Macon bus exposes no water flow (the unit only has a flow *switch*),
        # and the loop circulator (a fixed-speed Grundfos UPS26-99FC) isn't on
        # this bus. So thermal output is estimated from a constant loop flow x
        # the condenser dT (outlet - inlet). Default 11 GPM is the Arctic 040A
        # (48k BTU) manufacturer design flow; refine with a one-time clamp-on
        # measurement and just update loop_flow_gpm. Because raw temps + input
        # power are stored every row, COP is always recomputable from history.
        self.loop_flow_gpm = float(self.args.get("loop_flow_gpm", 11.0))
        # Loop fluid heat properties. Defaults are pure water; for a glycol mix
        # use that mix's values -- BOTH matter, since glycol's higher density
        # partly offsets its lower specific heat. ~25% propylene glycol at
        # operating temp ~ cp 3950 J/(kg.K), density 1.015 kg/L.
        self.fluid_cp = float(self.args.get("fluid_cp_j_kgk", 4186.0))
        self.fluid_density = float(self.args.get("fluid_density_kg_l", 1.0))
        # US gal/min -> L/s -> kg/s (via fluid density).
        self.flow_kg_s = (self.loop_flow_gpm * 3.785411784 / 60.0
                          * self.fluid_density)
        self._cp = self.fluid_cp
        # Only trust COP when the compressor is actually drawing (W).
        self.cop_min_input_w = float(self.args.get("cop_min_input_w", 200.0))

        # --- Runtime state ----------------------------------------------------
        self._online = None      # None = unknown, True/False after first poll
        self._rows_written = 0

        self._init_db()

        # Manual "poll now" trigger for testing.
        self.listen_event(self._manual_poll, "arctic_logger_poll")

        # Periodic poll. First run a few seconds out so AppDaemon finishes init.
        start = self.datetime() + timedelta(seconds=5)
        self.run_every(self._poll, start, self.interval)

        self.log("Arctic Logger started: polling %s every %ds -> %s"
                 % (self.base_url, self.interval, self.db_path))

    # ------------------------------------------------------------------ DB ---
    def _connect(self):
        # WAL keeps reads (e.g. an external grapher) from blocking our writes.
        db = sqlite3.connect(self.db_path, timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def _init_db(self):
        cols = ", ".join('"%s" REAL' % c for c in _SCALAR_COLUMNS)
        db = self._connect()
        try:
            db.execute(
                "CREATE TABLE IF NOT EXISTS readings ("
                "ts TEXT PRIMARY KEY, "
                "epoch INTEGER, "
                "version TEXT, "
                "frames INTEGER, "
                "crc_errors INTEGER, "
                "transactions INTEGER, "
                "run_state INTEGER, "
                "compressor_on INTEGER, "
                "waterpump_on INTEGER, "
                "fault_active INTEGER, "
                "faults TEXT, "
                + cols + ", "
                "raw_json TEXT"
                ")"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_readings_epoch "
                "ON readings(epoch)"
            )
            # Idempotent migration: add COP columns to a pre-existing DB (they
            # are not in the CREATE above so older databases lack them).
            have = {r[1] for r in db.execute("PRAGMA table_info(readings)")}
            for col in ("thermal_power_w", "cop"):
                if col not in have:
                    db.execute('ALTER TABLE readings ADD COLUMN "%s" REAL' % col)
            db.commit()
        finally:
            db.close()

    # ---------------------------------------------------------------- poll ---
    def _manual_poll(self, event_name, data, kwargs):
        self.log("Manual poll requested.")
        self._poll({})

    def _poll(self, kwargs):
        regs, status = self._fetch()
        if regs is None:
            if self._online is not False:
                self.log("Sniffer unreachable at %s - skipping polls until it "
                         "responds (expected briefly during its OTA reboots)."
                         % self.base_url, level="WARNING")
            self._online = False
            return
        if self._online is not True:
            self.log("Sniffer reachable at %s - logging resumed."
                     % self.base_url)
        self._online = True

        now = datetime.now(timezone.utc)
        row = self._build_row(now, regs, status)
        try:
            self._insert(row)
            self._rows_written += 1
        except sqlite3.Error as exc:
            self.log("DB write failed: %s" % exc, level="ERROR")
            return

        if self.publish_sensors:
            self._publish(row)

    def _fetch(self):
        """Return (regs_dict[int->int], status_dict) or (None, None) on error."""
        try:
            r = requests.get(self.base_url + "/api/registers",
                             timeout=self.http_timeout)
            r.raise_for_status()
            raw = r.json()
        except (requests.RequestException, ValueError):
            return None, None
        regs = {}
        for k, v in raw.items():
            try:
                regs[int(k)] = int(v)
            except (TypeError, ValueError):
                continue

        status = {}
        try:
            s = requests.get(self.base_url + "/api/status",
                             timeout=self.http_timeout)
            if s.ok:
                status = s.json()
        except (requests.RequestException, ValueError):
            status = {}
        return regs, status

    def _build_row(self, now, regs, status):
        row = {
            "ts": now.isoformat(timespec="seconds"),
            "epoch": int(now.timestamp()),
            "version": status.get("version"),
            "frames": status.get("frames"),
            "crc_errors": status.get("crc_errors"),
            "transactions": status.get("transactions"),
            "run_state": regs.get(2007),
            "raw_json": json.dumps(regs, separators=(",", ":"), sort_keys=True),
        }

        status_byte = regs.get(_REG_STATUS, 0) or 0
        row["compressor_on"] = 1 if status_byte & (1 << 2) else 0
        row["waterpump_on"] = 1 if status_byte & (1 << 3) else 0

        fault_active, fault_text = _decode_faults(regs)
        row["fault_active"] = fault_active
        row["faults"] = fault_text

        for addr, (col, _unit, _scale, _signed) in _REGISTERS.items():
            raw = regs.get(addr)
            row[col] = _decode_scalar(addr, raw) if raw is not None else None

        # Estimated thermal output & COP from constant loop flow x condenser dT.
        outlet = row.get("outlet_water_temp")
        inlet = row.get("inlet_water_temp")
        power = row.get("realtime_power")
        thermal = None
        cop = None
        if outlet is not None and inlet is not None:
            dt = outlet - inlet
            thermal = round(self.flow_kg_s * self._cp * dt, 1)  # W (signed)
            if power is not None and power >= self.cop_min_input_w and dt > 0:
                cop = round(thermal / power, 2)
        row["thermal_power_w"] = thermal
        row["cop"] = cop
        return row

    def _insert(self, row):
        cols = list(row.keys())
        placeholders = ", ".join("?" for _ in cols)
        sql = ('INSERT OR REPLACE INTO readings ('
               + ", ".join('"%s"' % c for c in cols)
               + ") VALUES (" + placeholders + ")")
        db = self._connect()
        try:
            db.execute(sql, [row[c] for c in cols])
            db.commit()
        finally:
            db.close()

    # ------------------------------------------------------------ sensors ---
    def _publish(self, row):
        """Publish a curated subset of decoded values as HA sensors."""
        pub = {
            "water_tank_temp": ("Water Tank Temp", "\u00b0C"),
            "hot_water_setpoint": ("Hot Water Setpoint", "\u00b0C"),
            "outdoor_ambient_temp": ("Outdoor Temp", "\u00b0C"),
            "inlet_water_temp": ("Inlet Water Temp", "\u00b0C"),
            "outlet_water_temp": ("Outlet Water Temp", "\u00b0C"),
            "compressor_freq": ("Compressor Freq", "Hz"),
            "realtime_power": ("Real-time Power", "W"),
            "thermal_power_w": ("Thermal Output", "W"),
            "cop": ("COP", None),
        }
        try:
            for col, (friendly, unit) in pub.items():
                if row.get(col) is None:
                    continue
                self.set_state(
                    "sensor.%s_%s" % (self.sensor_prefix, col),
                    state=row[col],
                    attributes={
                        "friendly_name": "Arctic %s" % friendly,
                        "unit_of_measurement": unit,
                        "device_class": "temperature" if unit == "\u00b0C" else None,
                    },
                )
            self.set_state(
                "binary_sensor.%s_compressor" % self.sensor_prefix,
                state="on" if row.get("compressor_on") else "off",
            )
            self.set_state(
                "binary_sensor.%s_fault" % self.sensor_prefix,
                state="on" if row.get("fault_active") else "off",
                attributes={"faults": row.get("faults") or "none"},
            )
        except Exception as exc:  # noqa: BLE001 - never let publishing kill the poll
            self.log("Sensor publish failed: %s" % exc, level="WARNING")
