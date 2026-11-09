#!/usr/bin/env python3
"""Read-only Modbus TCP register scanner for Sungrow inverters.

The scanner sends only function codes 03 and 04. It never sends write commands.
Register addresses are zero-based PDU addresses. Use --address-offset 1 if the
register numbers in a protocol document need to be converted to zero-based
addresses before transmission.
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import time


def read_response(sock: socket.socket, transaction_id: int) -> bytes:
    header = read_exact(sock, 7)
    response_transaction, protocol_id, length, unit_id = struct.unpack(">HHHB", header)
    if response_transaction != transaction_id:
        raise ValueError(f"unexpected transaction ID: {response_transaction}")
    if protocol_id != 0:
        raise ValueError(f"unexpected protocol ID: {protocol_id}")
    if length < 2:
        raise ValueError(f"invalid Modbus TCP length: {length}")
    return bytes([unit_id]) + read_exact(sock, length - 1)


def read_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("connection closed while receiving response")
        data.extend(chunk)
    return bytes(data)


def read_registers(host: str, port: int, unit_id: int, function: int,
                  address: int, quantity: int, timeout: float,
                  transaction_id: int) -> list[int]:
    if not 0 <= address <= 0xFFFF:
        raise ValueError("address must be between 0 and 65535")
    if not 1 <= quantity <= 125:
        raise ValueError("quantity must be between 1 and 125")

    pdu = struct.pack(">BBHH", unit_id, function, address, quantity)
    mbap = struct.pack(">HHHB", transaction_id, 0, len(pdu), 0)
    # The unit ID belongs in the PDU and MBAP header's final byte must be unit ID.
    mbap = struct.pack(">HHHB", transaction_id, 0, len(pdu) - 1, unit_id)

    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(mbap + pdu[1:])
        response = read_response(sock, transaction_id)

    response_unit, response_function = response[:2]
    if response_unit != unit_id:
        raise ValueError(f"unexpected unit ID: {response_unit}")
    if response_function & 0x80:
        code = response[2] if len(response) > 2 else None
        raise RuntimeError(f"Modbus exception 0x{code:02X}" if code is not None else "Modbus exception")
    if response_function != function:
        raise ValueError(f"unexpected function code: 0x{response_function:02X}")
    byte_count = response[2]
    payload = response[3:3 + byte_count]
    if byte_count != quantity * 2 or len(payload) != byte_count:
        raise ValueError(f"invalid byte count: {byte_count}")
    return list(struct.unpack(f">{quantity}H", payload))


def scan(args: argparse.Namespace) -> int:
    functions = [args.function] if args.function else [3, 4]
    transaction_id = 1
    found = 0
    for function in functions:
        print(f"Function 0x{function:02X}, addresses {args.start}..{args.end}")
        for address in range(args.start, args.end + 1, args.quantity):
            quantity = min(args.quantity, args.end - address + 1)
            try:
                values = read_registers(
                    args.host, args.port, args.unit, function, address,
                    quantity, args.timeout, transaction_id,
                )
            except (OSError, ValueError, RuntimeError) as exc:
                if args.verbose:
                    print(f"  {address:5d}: error: {exc}", file=sys.stderr)
            else:
                found += len(values)
                for offset, value in enumerate(values):
                    register = address + offset + args.address_offset
                    print(f"  register={register:5d} raw=0x{value:04X} unsigned={value:5d} signed={value - 65536 if value >= 32768 else value}")
            transaction_id = (transaction_id + 1) & 0xFFFF
            if args.delay:
                time.sleep(args.delay)
    print(f"Completed: {found} register values returned.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", help="inverter IP address or hostname")
    parser.add_argument("--port", type=int, default=502)
    parser.add_argument("--unit", type=int, default=1, help="Modbus unit/slave ID")
    parser.add_argument("--start", type=int, default=0, help="first zero-based address")
    parser.add_argument("--end", type=int, default=100, help="last zero-based address")
    parser.add_argument("--quantity", type=int, default=20, help="registers per request, max 125")
    parser.add_argument("--function", type=int, choices=(3, 4), help="scan only FC03 or FC04")
    parser.add_argument("--address-offset", type=int, default=0, help="display offset only; does not change requests")
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--delay", type=float, default=0.05, help="delay between requests in seconds")
    parser.add_argument("--verbose", action="store_true", help="print failed requests")
    args = parser.parse_args()
    if args.start < 0 or args.end < args.start or args.end > 0xFFFF:
        parser.error("require 0 <= start <= end <= 65535")
    if args.unit < 0 or args.unit > 247:
        parser.error("unit must be between 0 and 247")
    try:
        return scan(args)
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
