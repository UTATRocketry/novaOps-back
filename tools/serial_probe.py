"""Probe a FAS serial port the way fas_bridge does: open, write, read.

The bridge retires its handle on a *write* failure as readily as on a read
failure, so a port that opens and reads fine but rejects writes shows up in the
UI as a connect/disconnect cycle -- and a read-only terminal on the same port
looks perfectly stable. This tells the two apart.

    python tools/serial_probe.py COM6 --baud 115200
    python tools/serial_probe.py            # just list what exists
"""
from __future__ import annotations

import argparse
import struct
import sys
import time

import serial
import serial.tools.list_ports

RS422_MAGIC = 0xAA


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def discovery_frame() -> bytes:
    """The same DISCOVERY_REQ the bridge writes every 2 s."""
    can_id = (0x02 << 23)                      # msg=DISCOVERY_REQ, kind=GS, seq=0
    payload = struct.pack("<I", can_id) + b"\x00" * 8
    header = struct.pack("<H", len(payload))
    return bytes([RS422_MAGIC]) + header + payload + struct.pack("<H", crc16(header + payload))


def list_ports() -> None:
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        print("no serial ports present on this machine")
        return
    print("available ports:")
    for p in ports:
        print(f"  {p.device:8} {p.description}")
        print(f"           hwid={p.hwid}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("port", nargs="?", help="e.g. COM6 (omit to just list ports)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--seconds", type=float, default=6.0)
    args = ap.parse_args()

    list_ports()
    if not args.port:
        return 0

    print(f"\nopening {args.port} @ {args.baud} ...")
    try:
        ser = serial.Serial(args.port, args.baud, timeout=1, write_timeout=2)
    except Exception as e:
        print(f"  OPEN FAILED: {type(e).__name__}: {e}")
        print("  -> the bridge would log 'serial open failed' and retry every 2 s,")
        print("     never reaching a connected state.")
        return 1
    print("  open OK")

    # The discriminator: can this port actually be written to?
    print("writing a DISCOVERY_REQ frame (what the bridge does every 2 s) ...")
    write_failures = 0
    for i in range(3):
        try:
            ser.write(discovery_frame())
            ser.flush()
            print(f"  write {i + 1} OK")
        except Exception as e:
            write_failures += 1
            print(f"  write {i + 1} FAILED: {type(e).__name__}: {e}")
        time.sleep(0.3)

    print(f"\nreading for {args.seconds:.0f}s ...")
    deadline = time.monotonic() + args.seconds
    total = 0
    magic = 0
    read_failures = 0
    while time.monotonic() < deadline:
        try:
            chunk = ser.read(256)
        except Exception as e:
            read_failures += 1
            print(f"  READ FAILED: {type(e).__name__}: {e}")
            break
        total += len(chunk)
        magic += chunk.count(bytes([RS422_MAGIC]))
    ser.close()

    print(f"  {total} bytes, {magic} frame-start markers")
    print("\nverdict:")
    if write_failures:
        print("  WRITES FAIL on an openable port. This is the flap: the bridge's")
        print("  discovery write every 2.0 s kills the handle, and it reopens 2.0 s")
        print("  later. A read-only terminal on this port looks perfectly stable.")
        print("  Usually means the wrong port (e.g. a Bluetooth SPP with no peer).")
    elif read_failures:
        print("  reads fail -> the port really is going away mid-use.")
    elif total == 0:
        print("  writes fine, but nothing came back. Port is healthy and idle:")
        print("  wrong port, wrong baud, or the FMC is not transmitting.")
    elif magic == 0:
        print(f"  {total} bytes but no 0xAA frame markers -> almost certainly a")
        print("  BAUD MISMATCH (bridge default is 115200; nova.config.ps1 says 460800).")
    else:
        print("  port opens, writes, and carries framed data. Link looks healthy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
