# Sungrow Hybrid Inverter - Modbus TCP

Referensi: `Communication Protocol of Residential and Small Industrial Hybrid Inverter.pdf`

## Konfigurasi

- Protokol: Modbus TCP
- Interface: Ethernet melalui WiNet-S, WiNet-S2, atau Logger1000
- IP: sesuai konfigurasi DHCP/static inverter
- Port: `502`
- Slave/Unit ID: `1` (default)
- Data monitoring: Function Code `0x04` (Read Input Registers)
- Modbus TCP tidak menggunakan CRC
- Jeda minimum antar-request: sekitar `1000 ms`

Alamat register pada dokumen Sungrow dimulai dari 1. Saat dikirim melalui Modbus, gunakan alamat register dikurangi 1.

## Register data utama

| Data | Register dokumen | Alamat Modbus | Format |
|---|---:|---:|---|
| Running state | 13000 | 12999 | U16 |
| Power flow status | 13001 | 13000 | U16 |
| Total DC/PV power | 5017-5018 | 5016-5017 | U32, W |
| Battery power | 5214-5215 | 5213-5214 | S32, W |
| Battery SOC | 13023 | 13022 | U16, skala 0,1% |
| Battery temperature | 13025 | 13024 | S16, skala 0,1 degC |
| Load power | 13008-13009 | 13007-13008 | S32, W |
| Export power | 13010-13011 | 13009-13010 | S32, W |
| Meter active power | 5601-5602 | 5600-5601 | S32, W |
| Grid frequency | 5242 | 5241 | U16, skala 0,01 Hz |
| Serial number | 4990-4999 | 4989-4998 | UTF-8 |

Untuk tipe `S32/U32`, Sungrow menggunakan little-endian untuk urutan double-word. Nilai desimal dikirim sebagai integer yang sudah dikalikan skala.

## Contoh Python

Install library:

```powershell
pip install pymodbus
```

Kode pembacaan:

```python
from pymodbus.client import ModbusTcpClient

IP_INVERTER = "192.168.1.100"
PORT = 502
SLAVE_ID = 1

client = ModbusTcpClient(IP_INVERTER, port=PORT, timeout=3)

if not client.connect():
    raise ConnectionError("Tidak bisa terhubung ke inverter")


def read_u16(register):
    result = client.read_input_registers(
        address=register - 1,
        count=1,
        slave=SLAVE_ID,
    )
    if result.isError():
        raise RuntimeError(result)
    return result.registers[0]


def read_s16(register):
    value = read_u16(register)
    return value - 65536 if value >= 32768 else value


def read_s32(register):
    result = client.read_input_registers(
        address=register - 1,
        count=2,
        slave=SLAVE_ID,
    )
    if result.isError():
        raise RuntimeError(result)

    # Sungrow: little-endian untuk double-word
    first_word, second_word = result.registers
    raw = (second_word << 16) | first_word

    if raw >= 0x80000000:
        raw -= 0x100000000

    return raw


try:
    data = {
        "running_state": hex(read_u16(13000)),
        "power_flow": hex(read_u16(13001)),
        "pv_power_w": read_s32(5017),
        "battery_power_w": read_s32(5214),
        "battery_soc_percent": read_u16(13023) / 10.0,
        "battery_temperature_c": read_s16(13025) / 10.0,
        "load_power_w": read_s32(13008),
        "export_power_w": read_s32(13010),
        "meter_power_w": read_s32(5601),
        "grid_frequency_hz": read_u16(5242) / 100.0,
    }

    print(data)
finally:
    client.close()
```

Jika `pymodbus` yang digunakan tidak menerima parameter `slave`, ganti menjadi `unit=SLAVE_ID`.

## Power Flow Status

Nilai register `13001` adalah bit field:

- Bit 0: PV menghasilkan daya
- Bit 1: baterai charging
- Bit 2: baterai discharging
- Bit 3: ada beban
- Bit 4: ekspor ke grid
- Bit 5: impor dari grid

Contoh `0x000D` berarti PV aktif, baterai sedang discharge, dan ada beban.

## Contoh frame Modbus TCP

Untuk membaca dua register mulai dari register dokumen `13000`, alamat Modbus-nya adalah `12999` (`0x32C7`). Request PDU-nya menggunakan:

```text
Function: 04
Start address: 0x32C7
Quantity: 0x0002
```

Berbeda dari Modbus RTU, frame TCP memiliki MBAP header dan tidak memiliki CRC.
