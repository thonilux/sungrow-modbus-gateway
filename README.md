# Sungrow Modbus TCP Gateway

Read-only Flask gateway: Sungrow hybrid inverter (Modbus TCP, function `0x04`) to HTTP JSON, with MQTT telemetry publish. No write operations.

## Setup

```bash
python3 -m venv .venv
. .venv/bin/activate        # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
cp .env.example .env        # edit values
python app.py
```

## Docker

Copy `.env.example` to `.env`, edit the Sungrow and MQTT settings, then run:

```bash
docker compose up -d --build
docker compose logs -f sungrow-gateway
```

The dashboard is available at `http://127.0.0.1:5000/`. Stop it with:

```bash
docker compose down
```

The container runs as a non-root user. `.env`, vendor PDFs, and Python cache files
are excluded from the image.

Docker Compose uses the Linux host network so the gateway can reach Sungrow and
MQTT services through the server's WireGuard routes. `FLASK_PORT` is therefore
bound directly on the server and the `ports` mapping is not needed.

## Endpoints

```bash
curl http://127.0.0.1:5000/health
curl http://127.0.0.1:5000/api/data
```

## Configuration

See [.env.example](.env.example). Key vars: `SUNGROW_IP`, `SUNGROW_PORT` (502), `SUNGROW_UNIT_ID` (1), `FLASK_PORT`, `MQTT_*`.

## Docs

- [Modbus TCP notes](docs/modbus-tcp.md)
- [Modbus RTU notes](docs/modbus-rtu.md)
- Vendor PDFs: download from Sungrow, place in `docs/vendor/` (git-ignored).
- `scripts/tcp-scan.py`: find Modbus TCP devices on a subnet.
