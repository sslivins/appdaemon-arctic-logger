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

try:
    import paho.mqtt.client as mqtt
except ImportError:  # MQTT publishing is optional; app still logs to SQLite.
    mqtt = None


# ---------------------------------------------------------------------------
# MQTT-published entities (Home Assistant discovery).
#
# Each tuple: (object_id, friendly, device_class, state_class, unit, source_col)
# where source_col is the row/build_row key the value is read from. Sensors get
# discovery topics under homeassistant/sensor/<node>/<object_id>/config and all
# read from a single retained JSON state topic. Keep this list HP-agnostic —
# the per-HP node/unique_id prefix is applied at publish time so adding hp2 is
# just another app instance, never a code change.
# ---------------------------------------------------------------------------

# device_class of None => plain numeric/text sensor.
_MQTT_SENSORS = [
    # object_id             friendly              device_class   state_class     unit    source_col
    ("power",              "Power",              "power",       "measurement",  "W",    "realtime_power"),
    ("thermal_power",      "Thermal Output",     "power",       "measurement",  "W",    "thermal_power_w"),
    ("cop",                "COP",                 None,         "measurement",  None,   "cop"),
    ("compressor_freq",    "Compressor Freq",    "frequency",   "measurement",  "Hz",   "compressor_freq"),
    ("water_tank_temp",    "Water Tank Temp",    "temperature", "measurement",  "\u00b0C", "water_tank_temp"),
    ("outlet_water_temp",  "Outlet Water Temp",  "temperature", "measurement",  "\u00b0C", "outlet_water_temp"),
    ("inlet_water_temp",   "Inlet Water Temp",   "temperature", "measurement",  "\u00b0C", "inlet_water_temp"),
    ("outdoor_ambient_temp", "Outdoor Air Temp", "temperature", "measurement",  "\u00b0C", "outdoor_ambient_temp"),
    ("discharge_temp",     "Discharge Temp",     "temperature", "measurement",  "\u00b0C", "discharge_temp"),
    ("suction_temp",       "Suction Temp",       "temperature", "measurement",  "\u00b0C", "suction_temp"),
    ("hot_water_setpoint", "Hot Water Setpoint", "temperature", "measurement",  "\u00b0C", "hot_water_setpoint"),
    ("mode",               "Mode",                None,          None,          None,   "mode"),
]

# Binary sensors: (object_id, friendly, device_class, source_col)
# NOTE: no "fault/problem" sensor here — the existing Arctic integration
# already provides binary_sensor.arctic_heat_pump_<n>_problem. We only add the
# run-state bits the sniffer decodes that aren't otherwise exposed.
_MQTT_BINARY = [
    ("compressor", "Compressor", "running", "compressor_on"),
    ("waterpump",  "Water Pump", "running", "waterpump_on"),
]


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

# Propylene-glycol solution properties near the loop operating temp (~40 C),
# by volume %: (pct, density kg/L, cp J/(kg.K)). Linearly interpolated. These
# are approximations (the COP estimate is dominated by the flow assumption);
# for ethylene glycol or exact figures, set fluid_cp_j_kgk / fluid_density_kg_l
# explicitly instead of glycol_pct.
_PG_TABLE = [
    (0,  0.992, 4179),
    (10, 1.001, 4068),
    (20, 1.010, 3956),
    (30, 1.019, 3844),
    (40, 1.027, 3660),
    (50, 1.034, 3470),
]


def _glycol_props(pct):
    """Return (density_kg_l, cp_j_kgk) for a propylene-glycol vol %."""
    pct = max(0.0, min(50.0, float(pct)))
    for i in range(1, len(_PG_TABLE)):
        p0, d0, c0 = _PG_TABLE[i - 1]
        p1, d1, c1 = _PG_TABLE[i]
        if pct <= p1:
            f = (pct - p0) / (p1 - p0) if p1 != p0 else 0.0
            return d0 + f * (d1 - d0), c0 + f * (c1 - c0)
    return _PG_TABLE[-1][1], _PG_TABLE[-1][2]

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

        # --- Per-heat-pump identity (multi-HP support) ------------------------
        # Every heat pump has its own sniffer + its own logger instance. hp_id
        # namespaces the MQTT node/unique_ids/topics; hp_name is the HA device
        # name. Defaults keep a single-unit install working with no new config.
        self.hp_id = str(self.args.get("hp_id", "hp1"))
        self.hp_name = str(self.args.get("hp_name", "Arctic HP1 (Sniffer)"))

        # --- MQTT publishing (HA discovery) -----------------------------------
        self.mqtt_enabled = bool(self.args.get("mqtt_enabled", False))
        self.mqtt_host = str(self.args.get("mqtt_host", ""))
        self.mqtt_port = int(self.args.get("mqtt_port", 1883))
        self.mqtt_user = self.args.get("mqtt_username")
        self.mqtt_pass = self.args.get("mqtt_password")
        self.mqtt_prefix = str(self.args.get("mqtt_discovery_prefix", "homeassistant"))
        self.mqtt_base = "arctic/%s" % self.hp_id            # state/availability root
        self.mqtt_avail = self.mqtt_base + "/availability"
        self._mqtt = None
        self._mqtt_discovery_sent = False

        # --- COP estimation ---------------------------------------------------
        # The Macon bus exposes no water flow (the unit only has a flow *switch*),
        # and the loop circulator (a fixed-speed Grundfos UPS26-99FC) isn't on
        # this bus. So thermal output is estimated from a constant loop flow x
        # the condenser dT (outlet - inlet). Default 11 GPM is the Arctic 040A
        # (48k BTU) manufacturer design flow; refine with a one-time clamp-on
        # measurement and just update loop_flow_gpm. Because raw temps + input
        # power are stored every row, COP is always recomputable from history.
        self.loop_flow_gpm = float(self.args.get("loop_flow_gpm", 11.0))
        # Loop fluid heat properties. Simplest is to give glycol_pct (propylene
        # glycol, vol %) and cp/density are derived at ~40 C operating temp;
        # BOTH matter since glycol's higher density partly offsets its lower
        # specific heat. Explicit fluid_cp_j_kgk / fluid_density_kg_l override
        # the glycol_pct math (e.g. for ethylene glycol or a measured value).
        glycol_pct = self.args.get("glycol_pct")
        if glycol_pct is not None:
            _dens, _cp = _glycol_props(glycol_pct)
        else:
            _dens, _cp = 1.0, 4186.0  # pure water
        self.fluid_density = float(self.args.get("fluid_density_kg_l", _dens))
        self.fluid_cp = float(self.args.get("fluid_cp_j_kgk", _cp))
        # US gal/min -> L/s -> kg/s (via fluid density).
        self.flow_kg_s = (self.loop_flow_gpm * 3.785411784 / 60.0
                          * self.fluid_density)
        self._cp = self.fluid_cp
        self.log("COP model: %.1f GPM, glycol=%s, density=%.3f kg/L, "
                 "cp=%.0f J/kgK -> %.1f W/K"
                 % (self.loop_flow_gpm,
                    ("%s%%" % glycol_pct) if glycol_pct is not None else "none",
                    self.fluid_density, self.fluid_cp,
                    self.flow_kg_s * self.fluid_cp))
        # Only trust COP when the compressor is actually drawing (W).
        self.cop_min_input_w = float(self.args.get("cop_min_input_w", 200.0))

        # --- Runtime state ----------------------------------------------------
        self._online = None      # None = unknown, True/False after first poll
        self._rows_written = 0

        self._init_db()

        if self.mqtt_enabled:
            self._mqtt_init()

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
            # Idempotent migration: add derived columns to a pre-existing DB
            # (they are not in the CREATE above so older databases lack them).
            have = {r[1] for r in db.execute("PRAGMA table_info(readings)")}
            for col, coltype in (("thermal_power_w", "REAL"),
                                 ("cop", "REAL"),
                                 ("mode", "TEXT")):
                if col not in have:
                    db.execute('ALTER TABLE readings ADD COLUMN "%s" %s'
                               % (col, coltype))
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
                self._mqtt_availability(False)
            self._online = False
            return
        if self._online is not True:
            self.log("Sniffer reachable at %s - logging resumed."
                     % self.base_url)
            self._mqtt_availability(True)
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
        if self.mqtt_enabled:
            self._mqtt_publish_state(row)

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

        # Estimated heat transfer & COP from constant loop flow x loop dT.
        # thermal_power_w is SIGNED: +ve = heat into the loop (heating mode),
        # -ve = heat pulled out of the loop (cooling mode, reversing valve
        # flipped so outlet < inlet). mode is inferred from that sign while the
        # compressor is drawing; COP uses |thermal|/power so it's valid in both
        # directions (heating COP or cooling COP/EER -- see the mode column).
        outlet = row.get("outlet_water_temp")
        inlet = row.get("inlet_water_temp")
        power = row.get("realtime_power")
        thermal = None
        cop = None
        mode = "idle"
        dt = (outlet - inlet) if (outlet is not None and inlet is not None) \
            else None
        if dt is not None:
            thermal = round(self.flow_kg_s * self._cp * dt, 1)  # W (signed)
        running = power is not None and power >= self.cop_min_input_w
        if running and dt is not None:
            if dt > 0:
                mode = "heating"
            elif dt < 0:
                mode = "cooling"
            if dt != 0:
                cop = round(abs(thermal) / power, 2)
        elif running:
            mode = None  # drawing power but temps missing -> direction unknown
        row["thermal_power_w"] = thermal
        row["cop"] = cop
        row["mode"] = mode
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

    # --------------------------------------------------------------- MQTT ---
    def _mqtt_init(self):
        """Connect to the broker and publish HA discovery configs (once)."""
        if mqtt is None:
            self.log("mqtt_enabled but paho-mqtt is not installed - skipping.",
                     level="ERROR")
            self.mqtt_enabled = False
            return
        if not self.mqtt_host:
            self.log("mqtt_enabled but no mqtt_host configured - skipping.",
                     level="ERROR")
            self.mqtt_enabled = False
            return
        try:
            client = mqtt.Client(client_id="arctic-logger-%s" % self.hp_id)
            if self.mqtt_user:
                client.username_pw_set(self.mqtt_user, self.mqtt_pass)
            # Last will: mark the device offline if the logger dies unexpectedly.
            client.will_set(self.mqtt_avail, "offline", qos=1, retain=True)
            client.on_connect = self._mqtt_on_connect
            # Assign before connect()/loop_start(): on_connect can fire on the
            # network thread before the next line runs, and
            # _mqtt_publish_discovery() guards on self._mqtt (would silently skip
            # discovery, leaving state/availability published but no entities).
            self._mqtt = client
            client.connect(self.mqtt_host, self.mqtt_port, keepalive=60)
            client.loop_start()
            self.log("MQTT: connecting to %s:%d as device '%s' (%s)"
                     % (self.mqtt_host, self.mqtt_port, self.hp_id, self.hp_name))
        except Exception as exc:  # noqa: BLE001
            self.log("MQTT connect failed: %s" % exc, level="ERROR")
            self.mqtt_enabled = False

    def _mqtt_on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            self.log("MQTT connect rc=%s (non-zero = failure)" % rc,
                     level="ERROR")
            return
        # (Re)publish discovery + availability on every (re)connect so entities
        # survive an HA/broker restart.
        self._mqtt_publish_discovery()
        client.publish(self.mqtt_avail, "online", qos=1, retain=True)
        self.log("MQTT connected; discovery published for %d sensors."
                 % (len(_MQTT_SENSORS) + len(_MQTT_BINARY)))

    def _mqtt_device(self):
        return {
            "identifiers": ["arctic_%s" % self.hp_id],
            "name": self.hp_name,
            "manufacturer": "Arctic",
            "model": "ECO-600 (Macon)",
        }

    def _mqtt_publish_discovery(self):
        if not self._mqtt:
            return
        dev = self._mqtt_device()
        state_topic = self.mqtt_base + "/state"
        for oid, friendly, dclass, sclass, unit, col in _MQTT_SENSORS:
            uid = "arctic_%s_%s" % (self.hp_id, oid)
            cfg = {
                "name": friendly,
                "unique_id": uid,
                "state_topic": state_topic,
                "value_template": "{{ value_json.%s }}" % col,
                "availability_topic": self.mqtt_avail,
                "device": dev,
            }
            if dclass:
                cfg["device_class"] = dclass
            if sclass:
                cfg["state_class"] = sclass
            if unit:
                cfg["unit_of_measurement"] = unit
            topic = "%s/sensor/arctic_%s/%s/config" % (self.mqtt_prefix,
                                                       self.hp_id, oid)
            self._mqtt.publish(topic, json.dumps(cfg), qos=1, retain=True)
        for oid, friendly, dclass, col in _MQTT_BINARY:
            uid = "arctic_%s_%s" % (self.hp_id, oid)
            cfg = {
                "name": friendly,
                "unique_id": uid,
                "state_topic": state_topic,
                "value_template": "{{ 'ON' if value_json.%s else 'OFF' }}" % col,
                "payload_on": "ON",
                "payload_off": "OFF",
                "availability_topic": self.mqtt_avail,
                "device": dev,
            }
            if dclass:
                cfg["device_class"] = dclass
            topic = "%s/binary_sensor/arctic_%s/%s/config" % (self.mqtt_prefix,
                                                              self.hp_id, oid)
            self._mqtt.publish(topic, json.dumps(cfg), qos=1, retain=True)
        self._mqtt_discovery_sent = True

    def _mqtt_publish_state(self, row):
        if not self._mqtt:
            return
        # Publish every field the sensors reference in one retained JSON blob.
        # None -> omit so the value_template yields "None"->unavailable cleanly.
        payload = {}
        for _oid, _f, _dc, _sc, _u, col in _MQTT_SENSORS:
            val = row.get(col)
            payload[col] = val
        for _oid, _f, _dc, col in _MQTT_BINARY:
            payload[col] = 1 if row.get(col) else 0
        try:
            self._mqtt.publish(self.mqtt_base + "/state",
                               json.dumps(payload), qos=0, retain=True)
        except Exception as exc:  # noqa: BLE001 - never let publishing kill poll
            self.log("MQTT state publish failed: %s" % exc, level="WARNING")

    def _mqtt_availability(self, online):
        if not self._mqtt:
            return
        try:
            self._mqtt.publish(self.mqtt_avail,
                               "online" if online else "offline",
                               qos=1, retain=True)
        except Exception:  # noqa: BLE001
            pass

    def terminate(self):
        # Clean shutdown: mark offline and disconnect so HA shows unavailable.
        if self._mqtt:
            try:
                self._mqtt.publish(self.mqtt_avail, "offline", qos=1, retain=True)
                self._mqtt.loop_stop()
                self._mqtt.disconnect()
            except Exception:  # noqa: BLE001
                pass

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
                "sensor.%s_mode" % self.sensor_prefix,
                state=row.get("mode") or "unknown",
                attributes={"friendly_name": "Arctic Mode"},
            )
            self.set_state(
                "binary_sensor.%s_fault" % self.sensor_prefix,
                state="on" if row.get("fault_active") else "off",
                attributes={"faults": row.get("faults") or "none"},
            )
        except Exception as exc:  # noqa: BLE001 - never let publishing kill the poll
            self.log("Sensor publish failed: %s" % exc, level="WARNING")
