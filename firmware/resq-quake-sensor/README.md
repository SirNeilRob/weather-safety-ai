# ResQ Quake Sensor (Arduino App Lab app for the UNO Q)

ADXL345 accelerometer on the UNO Q header → earthquake alerts in the ResQ pipeline.

```
ADXL345 --I2C--> sketch (MCU, 100 Hz)          python/main.py (Linux, App Lab container)
                 gravity removed, one event  ──Bridge──►  sensor_bridge gates ──► POST /v1/alerts
                 = (peak g, duration s)       "shake_event"  MMI -> severity         (spooled if down)
```

## Wiring (header pins)
| ADXL345 | UNO Q |
|---|---|
| VCC | 3.3V (not 5V) |
| GND | GND |
| SDA | SDA |
| SCL | SCL |

The sketch probes `Wire` (header) then `Wire1` (Qwiic), at 0x53 and 0x1D, and checks
the chip ID (0xE5).

## What the halves do
- **Sketch** only measures: it opens an event when dynamic acceleration passes 0.02 g and
  closes it after 0.4 s of quiet, then sends `shake_event(peak_g, duration_s)`.
  Status goes out as `sensor_status(code, value)`: 1 = found, 2 = not found, 3 = heartbeat.
- **Python** decides, using `scripts/sensor_bridge.py` unchanged: 0.03 g floor, 1.0 s knock
  gate, 120 s cooldown (a stronger quake overrides), Worden 2012 PGA → MMI → severity.

## Deploy to the board
`sensor_bridge.py` must sit next to `main.py` (the container only sees the app folder):
```bash
# from the repo root on the Mac
COPYFILE_DISABLE=1 tar -C firmware -cf - resq-quake-sensor | ssh arduino@resq.local 'tar -xf - -C ~/ArduinoApps'
scp scripts/sensor_bridge.py arduino@resq.local:ArduinoApps/resq-quake-sensor/python/
ssh arduino@resq.local 'arduino-app-cli app start ~/ArduinoApps/resq-quake-sensor'
ssh arduino@resq.local 'arduino-app-cli app logs ~/ArduinoApps/resq-quake-sensor'
```
Only one App Lab app runs at a time: starting this one stops `resq-led-weather`.

## Settings (environment)
| Variable | Default |
|---|---|
| `RESQ_BACKEND` | `http://msgpack-rpc-router:8000` (App Lab's alias for the board itself) |
| `RESQ_LOCATION` | `ResQ node - Hackathon venue` |
| `RESQ_NODE` | `resq-q1` |
