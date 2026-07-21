# Wifi-Emulator

A fixed-node Wi-Fi simulation written in Python

## Features

- One fixed access point and four fixed stations
- 802.11aa-oriented EDCA traffic classes:
  - Voice
  - Video
  - Best effort
  - Background
- Deterministic contention behavior using a configurable random seed
- Fixed one-way medium delay of exactly **5 ms**

The 5 ms value is modeled as a fixed one-way propagation delay

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

The simulation reports transmissions, packet delivery, collisions, latency,
and per-station statistics.
