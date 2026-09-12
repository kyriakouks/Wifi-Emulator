# Wi-Fi Emulator — Lightweight IEEE 802.11aa Groupcast Model

A dependency-free, discrete-event Python simulation that demonstrates the
MAC-level reliability differences between legacy multicast and the
IEEE 802.11aa **Groupcast with Retries (GCR)** services.

The model deliberately omits PHY behavior. It is intended to make groupcast
control and retry decisions observable, not to predict real radio performance.

## What the simulator models

### Fixed topology and delay

- One AP and four fixed group members: `STA-1` through `STA-4`
- AP-originated group-addressed traffic sent to all four stations
- Every AP-to-station and station-to-AP transmission has a fixed **5 ms delay**
- No distance, SNR, modulation, coding, interference, capture effect, or rate
  adaptation calculation changes those delays

### Three group-addressed delivery modes

| Mode | Behavior | Feedback | Retry decision |
| --- | --- | --- | --- |
| `legacy` | One group DATA frame | None | Never retries |
| `gcr-ur` | GCR unsolicited retry | None | Always sends the configured number of extra DATA copies |
| `gcr-ba` | Lightweight GCR Block Ack | Per-receiver Block Ack reports aggregated at the AP | Retries only while at least one member has not acknowledged the packet |

`gcr-ur` represents the important **no-feedback** property of GCR Unsolicited
Retry. It can repair missed receivers, but it sends every configured copy even
when all receivers already got the first one.

`gcr-ba` represents the important **feedback-driven** property of GCR Block
Ack. The AP learns which receivers accepted the packet and retransmits only
when the aggregate feedback shows that a receiver is still missing it.

The full standard contains detailed GCR membership, control-frame, and Block
Ack rules. This simulator intentionally abstracts those details into a
serialized feedback window so the reliability difference is clear and no PHY
collision model is needed. See [802.11aa-GROUPCAST-GUIDE.md](802.11aa-GROUPCAST-GUIDE.md)
for the exact simplifications.

### EDCA-style traffic selection

Every group packet is assigned one pseudo-random access category:

- `voice`
- `video`
- `best-effort`
- `background`

The AP holds one FIFO queue per category and selects the highest-priority
non-empty queue. Its AIFS and `CWmin` determine a small EDCA-style delay before
the AP transmits a new group packet.

This is not a full 802.11aa EDCA implementation: it does not model
separate internal contention entities, virtual collisions, slot-by-slot
freeze/resume, or competing APs. EDCA is included only to show priority-aware
traffic scheduling around the 802.11aa groupcast comparison.

## Abstract receiver misses

A groupcast reliability comparison needs some receivers to miss initial DATA
copies. The option `--receiver-miss-probability` controls an **abstract,
deterministic MAC-level receiver miss input**:

- It is **not** calculated from the PHY, distance, signal strength, or channel
  conditions.
- It is derived only from the seed, packet number, station, and DATA copy
  number.
- Therefore the same packet/member/copy has the same outcome across `legacy`,
  `gcr-ur`, and `gcr-ba` comparisons.
- Set it to `0` for a perfect, lossless medium.

The default is `0.20`, which makes the reliability and overhead trade-offs
visible. The model does not claim that this value represents a physical packet
error rate.

## Requirements

- Python 3.10 or later
- No external packages

## Usage

Run all modes side by side (the default):

```bash
python main.py
```

Example comparison with a fixed seed:

```bash
python main.py --mode all --duration 1 --interval 0.02 --seed 7
```

Run a detailed report for one mode:

```bash
python main.py --mode gcr-ba --gcr-retries 2 --receiver-miss-probability 0.20
```

Run a perfect-medium comparison with no abstract receiver misses:

```bash
python main.py --mode all --receiver-miss-probability 0
```

Generate at least five AP group packets, even for a very short generation
window:

```bash
python main.py --duration 0.001 --minimum-group-packets 5
```

## Command-line options

| Option | Default | Meaning |
| --- | --- | --- |
| `--mode` | `all` | `legacy`, `gcr-ur`, `gcr-ba`, or an all-mode comparison |
| `--duration` | `1.0` | Normal group-packet generation window in seconds |
| `--interval` | `0.02` | Time between generated AP group packets in seconds |
| `--seed` | `7` | Seed for AP backoff and deterministic traffic/outcome generation |
| `--minimum-group-packets` | `5` | Minimum number of AP group packets to generate |
| `--gcr-retries` | `2` | Extra DATA copies available to either GCR mode |
| `--receiver-miss-probability` | `0.20` | Abstract independent per-copy receiver miss input; not a PHY model |

## Tests

The included tests use only Python's standard-library `unittest` module:

```bash
python -m unittest -v
```

They verify the five-packet minimum, the fixed 5 ms DATA delay, the
no-feedback legacy path, fixed GCR-UR copies, and feedback-driven GCR-BA
behavior.

## Interpreting the results

The all-mode table reports:

- **Complete**: group packets received by all four stations
- **Partial**: group packets with one or more stations missing the packet after
  the delivery procedure completes
- **Delivery**: unique station deliveries divided by all possible group
  deliveries
- **DATA**: group DATA frames sent by the AP
- **BA**: Block Ack feedback frames sent by group members; only GCR Block Ack
  uses these in this model
- **Retries**: extra group DATA copies sent after the initial transmission

Expected behavior with nonzero receiver misses:

1. `legacy` sends the fewest frames but gives the lowest group delivery ratio.
2. `gcr-ur` usually improves delivery, but has fixed retry overhead even when
   no receiver needs another copy.
3. `gcr-ba` spends feedback overhead and retries only when aggregate feedback
   identifies missing receivers; it can avoid unnecessary DATA retries.

## Scope and non-goals

This is a **lightweight 802.11aa-inspired GCR comparison**, not a conformance
implementation of IEEE 802.11aa. It does not implement:

- Complete GCR Block Ack control-frame semantics, membership procedures, or
  sequence bitmaps
- Stream Classification Service (SCS), TSPEC/TCLAS, admission control, HCCA,
  or intra-access-category prioritization
- Overlapping-BSS coordination
- 802.1 AVB/802.1Q stream-reservation interworking
- RTS/CTS, NAV, SIFS/DIFS timing accuracy, normal unicast ACKs, or PHY effects
- Multicast power-save delivery, aggregation, rate adaptation, or channel loss
  derived from a radio model

For the feature mapping and event sequence, see
[802.11aa-GROUPCAST-GUIDE.md](802.11aa-GROUPCAST-GUIDE.md).
