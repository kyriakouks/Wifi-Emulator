# Wifi-Emulator

Two fixed-node Wi-Fi simulations written in Python:

- **`main.py`**: Basic EDCA simulation with direct packet transmission
- **`RTS-CTS.py`**: Full RTS/CTS protocol implementation

## Features

- One access point `AP` and four stations (`STA-1` through `STA-4`)
- Four EDCA access categories: `voice`, `video`, `best-effort` and `background`
- Every generated packet is assigned one category randomly and independently
  of its source station
- Packets are placed in, selected from and requeued into queues using their
  own `access_category`; invalid category identifiers are rejected
- At least five packets are generated and delivered for each station by
  default, even for short simulations
- Deterministic contention behavior using a configurable random seed
- Fixed one-way medium delay of exactly **5 ms**

The 5 ms value is a fixed one-way propagation delay for every
successful station-to-access-point transmission.

This is a lightweight MAC/QoS simulation inspired by 802.11aa. It is **not** a full PHY-level implementation of every 802.11aa feature.

## Usage

### Basic Simulation (main.py)

Run the basic simulation with default settings:

```bash
python main.py
```

This uses direct packet transmission without RTS/CTS handshake.

Optional parameters:

```bash
python main.py --duration 5 --interval 0.01 --seed 42
```

`--duration` and `--interval` control normal duration-based generation. Short
runs are extended only as needed to meet the default minimum of five packets
per station. The minimum can be increased with
`--minimum-packets-per-station` (or its alias `--packets-per-station`):

```bash
python main.py --duration 0.001 --interval 0.02 --packets-per-station 5
```

The simulation reports transmissions, packet delivery, collisions, latency,
and per-station statistics.

### RTS/CTS Simulation (RTS-CTS.py)

Run the RTS/CTS simulation:

```bash
python RTS-CTS.py --duration 1 --interval 0.02 --seed 7
```

This implements the full RTS/CTS/DATA handshake:
1. Station sends RTS after winning contention (5 ms delay)
2. AP responds with CTS (5 ms delay)
3. Station sends DATA packet (5 ms delay)

Minimum delivery time: **15 ms** (3 × 5 ms) vs 5 ms in basic simulation.

The RTS/CTS version provides:
- Collision avoidance for DATA frames
- CTS timeout handling
- Additional statistics (RTS sent, CTS received, timeouts)

See `RTS-CTS-EXPLANATION.md` for detailed protocol documentation.
