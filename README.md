# Wifi-Emulator

A fixed-node Wi-Fi simulation written in Python

## Features

- One fixed access point and four fixed stations (`STA-1` through `STA-4`)
- Four generic EDCA access categories: `AC-1`, `AC-2`, `AC-3`, and `AC-4`
- Category-specific AIFS, contention-window, and priority settings are
  preserved without assigning application meanings to the generic categories
- Every generated packet is assigned one category randomly and independently
  of its source station
- Packets are placed in, selected from, and requeued into queues using their
  own `access_category`; invalid category identifiers are rejected
- At least five packets are generated and delivered for each station by
  default, including for short simulations
- Deterministic contention behavior using a configurable random seed
- Fixed one-way medium delay of exactly **5 ms**

The 5 ms value is modeled as a fixed one-way propagation delay for every
successful station-to-access-point transmission.

This is a lightweight MAC/QoS simulation inspired by 802.11aa. It is **not** a full PHY-level implementation of every 802.11aa feature.

## Usage

Run the simulation with default settings:

```bash
python main.py
```

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
