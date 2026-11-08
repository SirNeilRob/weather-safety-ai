#!/usr/bin/env python3
"""A fake ResQ node: emits (peak, duration) lines so the bridge can be demoed.

The real firmware is a microcontroller writing one line per detected event to a
serial port. This stands in for it when the hardware is not on the table. With
``--pty`` it writes to a pseudo-terminal and prints the device path, so the
bridge opens a real tty and its termios code is exercised rather than bypassed.

    # terminal 1: a fake node on a pty, prints the device path
    python3 scripts/sensor_sim.py --pty --scenario sequence

    # terminal 2: the bridge, against a live backend
    python3 scripts/sensor_bridge.py --port /dev/pts/7

Scenarios:

    quiet      background noise only, nothing should alert
    knock      a board knocked off a table: huge peak, no duration
    quake      the reference event, 0.21 g sustained for 2.4 s
    sequence   quiet, knock, quiet, quake, quiet (the default)
"""

from __future__ import annotations

import argparse
import os
import pty
import sys
import time

#: (name, peak in g, duration in s, pause after the line)
Event = tuple[str, float, float, float]

SCENARIOS: dict[str, list[Event]] = {
    "quiet": [
        ("quiet", 0.004, 0.10, 1.0),
        ("quiet", 0.011, 0.20, 1.0),
    ],
    "knock": [
        ("knock", 2.40, 0.04, 1.0),
    ],
    "quake": [
        ("quake", 0.21, 2.40, 0.0),
    ],
    "sequence": [
        ("quiet", 0.004, 0.10, 1.0),
        ("quiet", 0.011, 0.20, 1.0),
        ("knock", 2.40, 0.04, 1.5),
        ("quiet", 0.006, 0.10, 1.0),
        ("quake", 0.21, 2.40, 1.5),
        ("quiet", 0.005, 0.10, 1.0),
    ],
}


class SerialWriter:
    """Writes firmware lines to the master end of a pseudo-terminal."""

    def __init__(self, fd: int) -> None:
        self.fd = fd

    def __call__(self, line: str) -> None:
        os.write(self.fd, line.encode("utf-8"))


def stdout_writer(line: str) -> None:
    sys.stdout.write(line)
    sys.stdout.flush()


def emit(scenario: list[Event], write, *, interval: float, loop: bool) -> None:
    while True:
        for _name, peak, duration, pause in scenario:
            write(f"PEAK={peak:.3f} DUR={duration:.2f}\n")
            time.sleep(max(interval, pause))
        if not loop:
            return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Emit fake accelerometer readings for the sensor bridge.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="sequence")
    parser.add_argument("--pty", action="store_true", help="write to a pseudo-terminal instead of stdout")
    parser.add_argument("--interval", type=float, default=0.6, help="seconds between lines")
    parser.add_argument("--loop", action="store_true", help="repeat the scenario forever")
    args = parser.parse_args(argv)

    scenario = SCENARIOS[args.scenario]

    if not args.pty:
        try:
            emit(scenario, stdout_writer, interval=args.interval, loop=args.loop)
        except (KeyboardInterrupt, BrokenPipeError):
            return 0
        return 0

    master, slave = pty.openpty()
    path = os.ttyname(slave)
    print(path, flush=True)
    try:
        emit(scenario, SerialWriter(master), interval=args.interval, loop=args.loop)
    except (KeyboardInterrupt, OSError):
        # the bridge closed the slave end: nothing left to write to
        pass
    finally:
        os.close(master)
        os.close(slave)
    return 0


if __name__ == "__main__":
    sys.exit(main())
