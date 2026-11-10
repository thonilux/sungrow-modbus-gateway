"""Read-only Flask gateway for a Sungrow Modbus TCP inverter."""

from __future__ import annotations

import os
import socket
import struct
import threading
import time
import json
from typing import Any

from flask import Flask, jsonify, render_template_string
import paho.mqtt.client as mqtt
from dotenv import load_dotenv

load_dotenv()


INVERTER_IP = os.getenv("SUNGROW_IP", "192.168.1.100")
INVERTER_PORT = int(os.getenv("SUNGROW_PORT", "502"))
UNIT_ID = int(os.getenv("SUNGROW_UNIT_ID", "1"))
MODBUS_TIMEOUT = float(os.getenv("SUNGROW_TIMEOUT", "3"))
CACHE_SECONDS = float(os.getenv("SUNGROW_CACHE_SECONDS", "5"))
REQUEST_DELAY_SECONDS = float(os.getenv("SUNGROW_REQUEST_DELAY", "1.05"))
MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER", "")
MQTT_PASS = os.getenv("MQTT_PASS", "")
MQTT_BOARD_ID = os.getenv("MQTT_BOARD_ID", "RB000000000000")
MQTT_FW = os.getenv("MQTT_FW", "0.1.0")
MQTT_TELEMETRY_TOPIC = f"ress/{MQTT_BOARD_ID}/telemetry"
MQTT_STATUS_TOPIC = f"ress/{MQTT_BOARD_ID}/status"

app = Flask(__name__)
_cache: dict[str, Any] = {"timestamp": 0.0, "data": None}
_cache_lock = threading.Lock()
_transaction_lock = threading.Lock()
_transaction_id = 0
_request_lock = threading.Lock()
_last_request_at = 0.0
_poller_started = False
_poll_state: dict[str, Any] = {
    "state": "starting",
    "started_at": None,
    "last_success_at": None,
    "last_error": None,
    "success_count": 0,
    "error_count": 0,
    "duration_seconds": None,
}
_mqtt_client: mqtt.Client | None = None
_mqtt_state: dict[str, Any] = {
    "state": "disabled",
    "broker": f"{MQTT_HOST}:{MQTT_PORT}",
    "last_connect_at": None,
    "last_error": None,
    "logs": [],
}


def _mqtt_log(message: str, level: str = "INFO") -> None:
    entry = f"{time.strftime('%H:%M:%S')} [{level}] {message}"
    with _cache_lock:
        logs = _mqtt_state.setdefault("logs", [])
        logs.append(entry)
        del logs[:-100]


def _next_transaction_id() -> int:
    global _transaction_id
    with _transaction_lock:
        _transaction_id = (_transaction_id + 1) & 0xFFFF
        return _transaction_id


def _read_input_registers(register: int, count: int) -> list[int]:
    """Read using Sungrow document register numbers (e.g. 13023).

    Modbus wire addresses are zero-based, therefore document register N is sent
    as N - 1. The response is validated against the MBAP header and FC 0x04.
    """
    if register < 1 or count < 1 or count > 125:
        raise ValueError("invalid Modbus register range")

    global _last_request_at
    with _request_lock:
        wait = REQUEST_DELAY_SECONDS - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()

    transaction = _next_transaction_id()
    pdu = struct.pack(">BHH", 0x04, register - 1, count)
    # MBAP: transaction, protocol=0, length, unit id
    request = struct.pack(">HHHB", transaction, 0, len(pdu) + 1, UNIT_ID) + pdu

    with socket.create_connection((INVERTER_IP, INVERTER_PORT), MODBUS_TIMEOUT) as sock:
        sock.settimeout(MODBUS_TIMEOUT)
        sock.sendall(request)
        header = _recv_exact(sock, 7)
        rx_transaction, protocol, length, rx_unit = struct.unpack(">HHHB", header)
        if rx_transaction != transaction or protocol != 0 or rx_unit != UNIT_ID:
            raise IOError("invalid Modbus TCP response header")
        if length < 2 or length > 253:
            raise IOError("invalid Modbus TCP response length")
        body = _recv_exact(sock, length - 1)

    function = body[0]
    if function & 0x80:
        code = body[1] if len(body) > 1 else -1
        raise IOError(f"Modbus exception response: code {code}")
    if function != 0x04 or len(body) < 2 or body[1] != count * 2:
        raise IOError("invalid Read Input Registers response")

    return list(struct.unpack(f">{count}H", body[2:]))


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise ConnectionError("inverter closed the Modbus TCP connection")
        chunks.extend(chunk)
    return bytes(chunks)


def _s16(value: int) -> int:
    return value - 0x10000 if value & 0x8000 else value


def _s32_little_word(words: list[int]) -> int:
    """Decode Sungrow 32-bit values: low word first, big-endian bytes."""
    raw = (words[1] << 16) | words[0]
    return raw - 0x100000000 if raw & 0x80000000 else raw


def _read_data() -> dict[str, Any]:
    # Keep requests grouped and read-only; allow at least one second between
    # calls through the cache, as recommended by the Sungrow protocol notes.
    def safe_read(register: int, count: int) -> list[int] | None:
        try:
            return _read_input_registers(register, count)
        except Exception:
            return None

    def u16(register: int) -> int | None:
        values = safe_read(register, 1)
        return values[0] if values else None

    def s16(register: int, scale: float = 1.0) -> float | None:
        value = u16(register)
        return None if value is None else _s16(value) * scale

    def s32(register: int) -> int | None:
        values = safe_read(register, 2)
        return None if not values else _s32_little_word(values)

    running = safe_read(13000, 2) or [0, 0]
    power_flow = running[1]
    data: dict[str, Any] = {
        "model": "MG6RL",
        "device_type_code": u16(5000),
        "nominal_output_power_kw": (u16(5001) / 10.0) if u16(5001) is not None else None,
        "output_type": u16(5002),
        "daily_output_energy_kwh": (u16(5003) / 10.0) if u16(5003) is not None else None,
        "total_output_energy_kwh": (s32(5004) / 10.0) if s32(5004) is not None else None,
        "daily_pv_generation_kwh": (u16(13002) / 10.0) if u16(13002) is not None else None,
        "total_pv_generation_kwh": (s32(13003) / 10.0) if s32(13003) is not None else None,
        "inside_temperature_c": s16(5008, 0.1),
        "mppt_1_voltage_v": (u16(5011) / 10.0) if u16(5011) is not None else None,
        "mppt_1_current_a": (u16(5012) / 10.0) if u16(5012) is not None else None,
        "mppt_2_voltage_v": (u16(5013) / 10.0) if u16(5013) is not None else None,
        "mppt_2_current_a": (u16(5014) / 10.0) if u16(5014) is not None else None,
        "mppt_3_voltage_v": (u16(5015) / 10.0) if u16(5015) is not None else None,
        "mppt_3_current_a": (u16(5016) / 10.0) if u16(5016) is not None else None,
        "pv_power_w": s32(5017),
        "dc_active_power_w": s32(5017),
        "dc_active_energy_total_kwh": (s32(13003) / 10.0) if s32(13003) is not None else None,
        "v_dc_mppt_1_v": (u16(5011) / 10.0) if u16(5011) is not None else None,
        "ac_voltage_a_v": (u16(5019) / 10.0) if u16(5019) is not None else None,
        "ac_current_a_a": s16(13031, 0.1),
        "reactive_power_var": s32(5033),
        "power_factor": s16(5035, 0.001),
        "pf_ac": s16(5035, 0.001),
        "grid_frequency_01hz": (u16(5036) / 10.0) if u16(5036) is not None else None,
        "battery_power_w": s32(5214),
        "battery_voltage_v": (u16(13020) / 10.0) if u16(13020) is not None else None,
        "grid_frequency_hz": (u16(5242) / 100.0) if u16(5242) is not None else None,
        "ac_active_power_w": s32(13034),
        "ac_reactive_power_var": s32(5033),
        "ac_voltage_v": (u16(5019) / 10.0) if u16(5019) is not None else None,
        "ac_current_a": s16(13031, 0.1),
        "meter_power_w": s32(5601),
        "meter_phase_a_power_w": s32(5603),
        "meter_phase_b_power_w": s32(5605),
        "meter_phase_c_power_w": s32(5607),
        "battery_current_a": s16(5631, 0.1),
        "max_charge_current_a": u16(5635),
        "max_discharge_current_a": u16(5636),
        "battery_capacity_kwh": (u16(5639) / 100.0) if u16(5639) is not None else None,
        "backup_phase_a_current_a": s16(5720, 0.1),
        "backup_phase_b_current_a": s16(5721, 0.1),
        "backup_phase_c_current_a": s16(5722, 0.1),
        "backup_phase_a_power_w": s16(5723),
        "backup_phase_b_power_w": s16(5724),
        "backup_phase_c_power_w": s16(5725),
        "backup_total_power_w": s32(5726),
        "backup_phase_a_voltage_v": (u16(5731) / 10.0) if u16(5731) is not None else None,
        "backup_phase_b_voltage_v": (u16(5732) / 10.0) if u16(5732) is not None else None,
        "backup_phase_c_voltage_v": (u16(5733) / 10.0) if u16(5733) is not None else None,
        "backup_frequency_hz": (u16(5734) / 100.0) if u16(5734) is not None else None,
        "running_state": running[0],
        "power_flow_status": power_flow,
        "battery_soc_percent": (u16(13023) / 10.0) if u16(13023) is not None else None,
        "battery_soh_percent": (u16(13024) / 10.0) if u16(13024) is not None else None,
        "battery_temperature_c": s16(13025, 0.1),
        "daily_battery_discharge_kwh": (u16(13026) / 10.0) if u16(13026) is not None else None,
        "total_battery_discharge_kwh": (s32(13027) / 10.0) if s32(13027) is not None else None,
        "self_consumption_percent": (u16(13029) / 10.0) if u16(13029) is not None else None,
        "load_power_w": s32(13008),
        "export_power_w": s32(13010),
        "total_active_power_w": s32(13034),
        "daily_import_energy_kwh": (u16(13036) / 10.0) if u16(13036) is not None else None,
        "total_import_energy_kwh": (s32(13037) / 10.0) if s32(13037) is not None else None,
        "daily_charge_energy_kwh": (u16(13040) / 10.0) if u16(13040) is not None else None,
        "total_charge_energy_kwh": (s32(13041) / 10.0) if s32(13041) is not None else None,
        "daily_export_energy_kwh": (u16(13045) / 10.0) if u16(13045) is not None else None,
        "total_export_energy_kwh": (s32(13046) / 10.0) if s32(13046) is not None else None,
        "active_export_energy_kwh": (s32(13046) / 10.0) if s32(13046) is not None else None,
        "active_import_energy_kwh": (s32(13037) / 10.0) if s32(13037) is not None else None,
        "inverter_alarm": s32(13050),
        "grid_side_fault": s32(13052),
        "system_fault_1": s32(13054),
        "system_fault_2": s32(13056),
        "dc_side_fault": s32(13058),
        "battery_fault": s32(13066),
        "battery_alarm": s32(13068),
    }

    data.update({
        "inverter_ip": INVERTER_IP,
        "unit_id": UNIT_ID,
        "power_flow_bits": {
            "pv_generating": bool(power_flow & (1 << 0)),
            "battery_charging": bool(power_flow & (1 << 1)),
            "battery_discharging": bool(power_flow & (1 << 2)),
            "load_present": bool(power_flow & (1 << 3)),
            "exporting": bool(power_flow & (1 << 4)),
            "importing": bool(power_flow & (1 << 5)),
        },
        "read_at_unix": time.time(),
    })
    return data


def _mqtt_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Build the MQTT v3 payload for the Sungrow inverter."""
    state = data.get("running_state")
    status = "on_grid" if state in (0, 0x40) else "standby"
    fields = {
        "pv_power": data.get("pv_power_w"),
        "dc_active_energy": data.get("dc_active_energy_total_kwh"),
        "daily_output_energy": data.get("daily_output_energy_kwh"),
        "total_active_power": data.get("total_active_power_w"),
        "reactive_power": data.get("reactive_power_var"),
        "ac_voltage_phase_a": data.get("ac_voltage_a_v"),
        "dc_voltage_mppt_1": data.get("v_dc_mppt_1_v"),
        "mppt_1_current": data.get("mppt_1_current_a"),
        "mppt_2_voltage": data.get("mppt_2_voltage_v"),
        "mppt_2_current": data.get("mppt_2_current_a"),
        "ac_current_phase_a": data.get("ac_current_a_a"),
        "battery_power": data.get("battery_power_w"),
        "battery_soc": data.get("battery_soc_percent"),
        "battery_temperature": data.get("battery_temperature_c"),
        "inside_temperature": data.get("inside_temperature_c"),
        "load_power": data.get("load_power_w"),
        "export_power": data.get("export_power_w"),
        "meter_power": data.get("meter_power_w"),
        "grid_frequency": data.get("grid_frequency_hz"),
        "ac_power_factor": data.get("pf_ac"),
        "active_export_energy": data.get("active_export_energy_kwh"),
        "active_import_energy": data.get("active_import_energy_kwh"),
        "backup_total_power": data.get("backup_total_power_w"),
    }
    return {
        "v": 3,
        "boardId": MQTT_BOARD_ID,
        "batch": [
            {
                "d": "inv_sungrow",
                "ts": int(data.get("read_at_unix") or time.time()),
                "data": [
                    {"k": key, "v": value}
                    for key, value in {"inv_status": status, **fields}.items()
                    if value is not None
                ],
            }
        ],
    }


def _mqtt_start() -> None:
    global _mqtt_client
    if not MQTT_USER or not MQTT_PASS:
        _mqtt_log("MQTT disabled: credentials belum diisi", "WARN")
        return
    with _cache_lock:
        _mqtt_state["state"] = "connecting"
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=MQTT_BOARD_ID)
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.will_set(MQTT_STATUS_TOPIC, json.dumps({
        "v": 1, "boardId": MQTT_BOARD_ID, "online": False,
        "ts": int(time.time()), "fw": MQTT_FW,
    }), qos=1, retain=True)

    def on_connect(_client, _userdata, _flags, reason_code, _properties=None):
        if reason_code == 0:
            _mqtt_log(f"Connected ke {MQTT_HOST}:{MQTT_PORT}")
            with _cache_lock:
                _mqtt_state.update({"state": "connected", "last_connect_at": time.time(), "last_error": None})
            client.publish(MQTT_STATUS_TOPIC, json.dumps({
                "v": 1, "boardId": MQTT_BOARD_ID, "online": True,
                "ts": int(time.time()), "fw": MQTT_FW,
            }), qos=1, retain=True)
        else:
            _mqtt_log(f"Connect gagal: reason {reason_code}", "ERROR")
            with _cache_lock:
                _mqtt_state.update({"state": "error", "last_error": f"connect reason {reason_code}"})

    def on_disconnect(_client, _userdata, _disconnect_flags, reason_code, _properties=None):
        _mqtt_log(f"Disconnected: reason {reason_code}", "WARN")
        with _cache_lock:
            _mqtt_state.update({"state": "disconnected", "last_error": f"disconnect reason {reason_code}"})

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()
    _mqtt_client = client


def _mqtt_publish(data: dict[str, Any]) -> None:
    if _mqtt_client is not None:
        payload = _mqtt_payload(data)
        result = _mqtt_client.publish(MQTT_TELEMETRY_TOPIC, json.dumps(payload), qos=1)
        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            _mqtt_log(f"PUBLISH {MQTT_TELEMETRY_TOPIC} · {len(payload['batch'][0]['data'])} fields")
        else:
            _mqtt_log(f"Publish gagal: rc {result.rc}", "ERROR")


def _mqtt_schedule_loop() -> None:
    """Publish the latest telemetry exactly at every minute boundary."""
    while True:
        now = time.time()
        next_minute = (int(now) // 60 + 1) * 60
        time.sleep(max(0.01, next_minute - now))
        with _cache_lock:
            data = dict(_cache["data"]) if _cache["data"] is not None else None
        if data is not None:
            _mqtt_publish(data)
        else:
            _mqtt_log("PUBLISH dilewati: belum ada data Sungrow", "WARN")


def get_cached_data() -> dict[str, Any]:
    with _cache_lock:
        if _cache["data"] is None:
            raise ConnectionError(_cache.get("error", "data belum tersedia"))
        return dict(_cache["data"])


def _poll_loop() -> None:
    """Single background poller; HTTP requests never contact the inverter."""
    while True:
        started = time.time()
        with _cache_lock:
            _poll_state.update({"state": "polling", "started_at": started})
        try:
            data = _read_data()
            with _cache_lock:
                _cache["timestamp"] = time.time()
                _cache["data"] = data
                _cache["error"] = None
                _poll_state.update({
                    "state": "ok",
                    "last_success_at": _cache["timestamp"],
                    "last_error": None,
                    "success_count": _poll_state["success_count"] + 1,
                    "duration_seconds": round(time.time() - started, 2),
                })
        except Exception as exc:
            with _cache_lock:
                _cache["error"] = str(exc)
                _poll_state.update({
                    "state": "error",
                    "last_error": str(exc),
                    "error_count": _poll_state["error_count"] + 1,
                    "duration_seconds": round(time.time() - started, 2),
                })
        time.sleep(max(CACHE_SECONDS, 5.0))


def start_poller() -> None:
    global _poller_started
    if not _poller_started:
        _poller_started = True
        threading.Thread(target=_poll_loop, name="sungrow-poller", daemon=True).start()
        threading.Thread(target=_mqtt_schedule_loop, name="mqtt-scheduler", daemon=True).start()


@app.get("/")
def index():
    return render_template_string(
        """
<!doctype html>
<html lang="id">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sungrow Monitor</title>
  <style>
    :root { color-scheme: dark; font-family: Segoe UI, Arial, sans-serif; }
    body { margin: 0; background: #0f172a; color: #e2e8f0; }
    main { max-width: 1000px; margin: auto; padding: 28px 18px; }
    header { display: flex; justify-content: space-between; gap: 16px; align-items: center; margin-bottom: 24px; }
    h1 { margin: 0; font-size: 28px; }
    .sub { color: #94a3b8; margin-top: 6px; }
    .status { padding: 8px 12px; border-radius: 999px; background: #14532d; color: #bbf7d0; }
    .error { background: #7f1d1d; color: #fecaca; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 14px; }
    .card { background: #1e293b; border: 1px solid #334155; border-radius: 14px; padding: 18px; }
    .label { color: #94a3b8; font-size: 14px; }
    .value { font-size: 30px; font-weight: 700; margin-top: 10px; }
    .unit { color: #cbd5e1; font-size: 14px; margin-left: 4px; }
    .wide { grid-column: 1 / -1; }
    .terminal { background: #020617; border: 1px solid #334155; border-radius: 14px; padding: 14px 16px; margin-top: 14px; color: #86efac; font: 13px Consolas, monospace; min-height: 100px; max-height: 260px; overflow: auto; white-space: pre-wrap; }
    .clockbar { display: flex; gap: 18px; flex-wrap: wrap; margin: 0 0 14px; color: #cbd5e1; font: 14px Consolas, monospace; }
    footer { color: #64748b; margin-top: 22px; font-size: 13px; }
  </style>
</head>
<body>
<main>
  <header>
    <div><h1>Sungrow Monitor</h1><div class="sub">Modbus TCP · {{ target }}</div></div>
    <div>
      <div id="status" class="status">Menunggu poller...</div>
      <div id="mqtt" class="sub" style="text-align:right;margin-top:8px">MQTT: ...</div>
    </div>
  </header>
  <div class="clockbar"><span>Clock: <strong id="clock">--:--:--</strong></span><span>Next MQTT publish: <strong id="next-publish">--s</strong></span></div>
  <section class="grid">
    <div class="card"><div class="label">PV Power / Total DC</div><div class="value"><span id="pv">-</span><span class="unit">W</span></div></div>
    <div class="card"><div class="label">DC Active Energy</div><div class="value"><span id="dcenergy">-</span><span class="unit">kWh</span></div></div>
    <div class="card"><div class="label">Daily Output Energy</div><div class="value"><span id="daily">-</span><span class="unit">kWh</span></div></div>
    <div class="card"><div class="label">Total Active Power</div><div class="value"><span id="active">-</span><span class="unit">W</span></div></div>
    <div class="card"><div class="label">Reactive Power</div><div class="value"><span id="reactive">-</span><span class="unit">var</span></div></div>
    <div class="card"><div class="label">AC Voltage Phase A</div><div class="value"><span id="acv">-</span><span class="unit">V</span></div></div>
    <div class="card"><div class="label">DC Voltage MPPT 1</div><div class="value"><span id="dcv">-</span><span class="unit">V</span></div></div>
    <div class="card"><div class="label">MPPT 1 Current</div><div class="value"><span id="mppt1i">-</span><span class="unit">A</span></div></div>
    <div class="card"><div class="label">MPPT 2 Voltage</div><div class="value"><span id="mppt2v">-</span><span class="unit">V</span></div></div>
    <div class="card"><div class="label">MPPT 2 Current</div><div class="value"><span id="mppt2i">-</span><span class="unit">A</span></div></div>
    <div class="card"><div class="label">AC Current Phase A</div><div class="value"><span id="aca">-</span><span class="unit">A</span></div></div>
    <div class="card"><div class="label">Battery Power</div><div class="value"><span id="battery">-</span><span class="unit">W</span></div></div>
    <div class="card"><div class="label">Battery SOC</div><div class="value"><span id="soc">-</span><span class="unit">%</span></div></div>
    <div class="card"><div class="label">Battery Temperature</div><div class="value"><span id="temp">-</span><span class="unit">°C</span></div></div>
    <div class="card"><div class="label">Inside Temperature</div><div class="value"><span id="inside">-</span><span class="unit">°C</span></div></div>
    <div class="card"><div class="label">Load Power</div><div class="value"><span id="load">-</span><span class="unit">W</span></div></div>
    <div class="card"><div class="label">Export Power</div><div class="value"><span id="export">-</span><span class="unit">W</span></div></div>
    <div class="card"><div class="label">Meter Power</div><div class="value"><span id="meter">-</span><span class="unit">W</span></div></div>
    <div class="card"><div class="label">Grid Frequency</div><div class="value"><span id="freq">-</span><span class="unit">Hz</span></div></div>
    <div class="card"><div class="label">AC Power Factor</div><div class="value"><span id="pf">-</span></div></div>
    <div class="card"><div class="label">Active Export Energy</div><div class="value"><span id="exportenergy">-</span><span class="unit">kWh</span></div></div>
    <div class="card"><div class="label">Active Import Energy</div><div class="value"><span id="importenergy">-</span><span class="unit">kWh</span></div></div>
    <div class="card"><div class="label">Backup Total Power</div><div class="value"><span id="backup">-</span><span class="unit">W</span></div></div>
    <div class="card wide"><div class="label">Power Flow Status</div><div class="value" id="flow">-</div></div>
  </section>
  <section class="card wide"><div class="label">MQTT Terminal Log</div><div id="mqtt-log" class="terminal">Menunggu log MQTT...</div></section>
  <footer>
    <div>Read-only dashboard · refresh otomatis setiap 5 detik</div>
    <div id="progress">Poller sedang mulai...</div>
  </footer>
</main>
<script>
  const ids = {
    pv: 'pv_power_w', battery: 'battery_power_w', soc: 'battery_soc_percent',
    temp: 'battery_temperature_c', load: 'load_power_w', export: 'export_power_w',
    meter: 'meter_power_w', freq: 'grid_frequency_hz', daily: 'daily_output_energy_kwh',
    reactive: 'reactive_power_var', acv: 'ac_voltage_a_v', aca: 'ac_current_a_a', active: 'total_active_power_w',
    backup: 'backup_total_power_w',
    dcenergy: 'dc_active_energy_total_kwh', dcv: 'v_dc_mppt_1_v', inside: 'inside_temperature_c',
    pf: 'pf_ac', exportenergy: 'active_export_energy_kwh', importenergy: 'active_import_energy_kwh',
    mppt1i: 'mppt_1_current_a', mppt2v: 'mppt_2_voltage_v', mppt2i: 'mppt_2_current_a'
  };
  const fmt = v => typeof v === 'number' ? v.toLocaleString('id-ID', {maximumFractionDigits: 2}) : '-';
  function updateClock() {
    const now = new Date();
    document.getElementById('clock').textContent = now.toLocaleTimeString('id-ID', {hour12: false});
    document.getElementById('next-publish').textContent = `${60 - now.getSeconds()}s`;
  }
  async function refresh() {
    const status = document.getElementById('status');
    try {
      const response = await fetch('/api/data', {cache: 'no-store'});
      const result = await response.json();
      const ps = result.poll_status || {};
      const ms = result.mqtt_status || {};
      const progress = document.getElementById('progress');
      const log = document.getElementById('mqtt-log');
      log.textContent = (ms.logs || []).join('\n') || 'Belum ada aktivitas MQTT';
      log.scrollTop = log.scrollHeight;
      document.getElementById('mqtt').textContent = `MQTT: ${ms.state || 'unknown'} · ${ms.broker || ''}`;
      if (ps.state === 'polling') progress.textContent = 'Sedang membaca register inverter...';
      else if (ps.state === 'ok') progress.textContent = `Berhasil ${ps.success_count}x · durasi ${ps.duration_seconds}s · error ${ps.error_count}x`;
      else if (ps.state === 'error') progress.textContent = `Error ${ps.error_count}x: ${ps.last_error || result.error}`;
      else progress.textContent = 'Menunggu pembacaan pertama...';
      if (!response.ok || !result.ok) throw new Error(result.error || 'Gateway error');
      const d = result.data;
      for (const [id, key] of Object.entries(ids)) document.getElementById(id).textContent = fmt(d[key]);
      const bits = d.power_flow_bits || {};
      const active = Object.entries(bits).filter(([, value]) => value).map(([key]) => key.replaceAll('_', ' '));
      document.getElementById('flow').textContent = active.length ? active.join(' · ') : 'Tidak ada status aktif';
      status.textContent = ps.state === 'polling' ? 'Polling...' : 'Data OK'; status.classList.remove('error');
    } catch (err) {
      status.textContent = 'Offline'; status.classList.add('error');
      console.error(err);
    }
  }
  updateClock(); setInterval(updateClock, 250); refresh(); setInterval(refresh, 5000);
</script>
</body>
</html>
        """,
        target=f"{INVERTER_IP}:{INVERTER_PORT}",
    )


@app.get("/health")
def health():
    try:
        data = get_cached_data()
        return jsonify({"ok": True, "read_at_unix": data["read_at_unix"]})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503


@app.get("/api/data")
def api_data():
    try:
        with _cache_lock:
            data = _cache.get("data")
            error = _cache.get("error")
            timestamp = _cache.get("timestamp", 0.0)
            poll_status = dict(_poll_state)
            mqtt_status = dict(_mqtt_state)
        if data is None:
            return jsonify({"ok": False, "error": error or "polling belum menghasilkan data", "poll_status": poll_status, "mqtt_status": mqtt_status}), 503
        return jsonify({"ok": True, "data": data, "cache_timestamp": timestamp, "poll_error": error, "poll_status": poll_status, "mqtt_status": mqtt_status})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


if __name__ == "__main__":
    _mqtt_start()
    start_poller()
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("FLASK_PORT", "5000")),
        use_reloader=False,
    )
