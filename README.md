# IEEE 802.11aa Reliable Multicast Analysis

A dependency-free Python implementation of the analytical saturation model from:

> *Performance Analysis and Algorithm Selection for Reliable Multicast in
> IEEE 802.11aa Wireless LAN*

The program calculates the paper's main outputs as **tables**, not figures:

- Multicast reliability, `ηm` (the average per-receiver delivery probability,
  not the probability that every group member receives a frame)
- Multicast saturation throughput, `Sm`
- Aggregate unicast saturation throughput, `Su`
- Reliability-constrained LogRD utility
- Selected multicast mechanism for a given number of receivers and unicast
  contenders

The repository includes the referenced PDF and a detailed implementation guide
in [PAPER-MODEL-GUIDE.md](PAPER-MODEL-GUIDE.md).

## Important model change

The earlier fixed-distance/fixed-delay event simulation has been replaced.
There is no longer a 5 ms propagation delay or periodic packet-generation
interval. The paper studies **saturation**, so:

- the AP always has multicast traffic available;
- every unicast station always has unicast traffic available;
- DCF attempt and collision probabilities are solved analytically; and
- DATA/control frame airtime comes from frame sizes and selected bit rates.

No path loss, SNR, fading, modulation waveform, or detailed PHY is simulated.
PHY behavior is reduced to the paper's rates and BER-to-FER calculation.

## Implemented mechanisms

| CLI name | Paper mechanism |
| --- | --- |
| `legacy` | No-Ack/No-Retry legacy multicast |
| `gcr-ur` | GCR Unsolicited Retry |
| `dms` | Directed Multicast Service |
| `immediate-ba` | GCR Immediate Block Ack |
| `delayed-ba` | GCR Delayed Block Ack analytical comparison |

The paper notes that Delayed Block Ack is an analytical extension of delayed
unicast Block Ack rather than an explicit 802.11aa GCR service.

## Paper parameters

The default `ModelParameters` values reproduce Table I where the PDF provides
numerical values:

| Parameter | Default |
| --- | ---: |
| Multicast payload | 1500 bytes |
| Unicast payload | 1500 bytes |
| Legacy multicast rate | 6 Mb/s |
| GCR/DMS/Block Ack DATA rate | 54 Mb/s |
| Unicast DATA rate | 54 Mb/s |
| Control rate | 6 Mb/s |
| Legacy BER | `1e-6` |
| GCR/DMS/Block Ack BER | `1e-5` |
| Unicast BER | `0` |
| GCR-UR retries | 2 (`R + 1 = 3` copies) |
| DMS/unicast retry limit | 8 |
| Block Ack MSDU lifetime | 400 ms |
| Block Ack burst size | 10 frames |
| Multicast `{CWmin, max stage}` | `{16, 0}` |
| Unicast `{CWmin, max stage}` | `{32, 5}` |
| LogRD reliability threshold | 90% |

### Timing constants omitted by the paper

The PDF does not give numerical values for the empty slot, SIFS, DIFS, PLCP,
MAC header/FCS, ACK, or Block Ack control lengths. The implementation therefore
uses explicit common 802.11a defaults:

| Parameter | Default used by this project |
| --- | ---: |
| Empty slot | 9 µs |
| SIFS | 16 µs |
| DIFS | 34 µs |
| PLCP preamble/header time | 20 µs |
| MAC header + FCS | 34 bytes |
| ACK | 14 bytes |
| Common BAR/BA control length (`BACK`) | 32 bytes |

These values are visible in `ModelParameters` and can be changed in code. They
must not be interpreted as values explicitly stated by the paper. Exact
numerical reproduction of the original OMNeT++ output requires the authors'
missing simulator configuration.

## Requirements

- Python 3.10 or later
- No third-party packages

## Basic comparison table

Run the paper's common baseline of ten multicast receivers and ten saturated
unicast stations:

```bash
python main.py
```

Equivalent explicit command:

```bash
python main.py --receivers 10 --unicast-stations 10
```

The output contains one row per mechanism:

```text
Mechanism        Reliab.  Sm Mbps  Su Mbps  Utility  tau_m  p_cm  BA R
```

`BA R` is the effective retry count obtained from the 400 ms Block Ack MSDU
lifetime approximation.

The row marked `*` is selected by the paper's LogRD criterion:

```text
utility = min(Sm / (1 + ln(Nrx)), (Su / Nu))
```

A mechanism receives zero utility if its reliability is below the configured
threshold. If every mechanism is below the threshold, no winner is marked and
the program reports that no feasible mechanism exists.

## Reliability and throughput curve data as tables

### Sweep multicast receivers

Hold `Nu = 10` and evaluate `Nrx = 1…30`, corresponding to the receiver sweep
used by the paper:

```bash
python main.py \
  --sweep receivers \
  --sweep-start 1 \
  --sweep-end 30 \
  --unicast-stations 10
```

This prints one row per receiver count and mechanism with:

- reliability;
- multicast throughput;
- unicast throughput;
- LogRD utility; and
- whether the mechanism was selected.

Print only the selected mechanism at each point:

```bash
python main.py --sweep receivers --selection-only --unicast-stations 12
```

The `Nu = 12` selection sweep corresponds to the illustrative scenario in the
paper's mechanism-selection section. It is useful for comparison, but the
current executable assumptions do not reproduce Figure 13's exact winner
boundaries; see **Known benchmark gap** below.

### Sweep unicast contenders

Hold `Nrx = 10` and evaluate `Nu = 1…30`:

```bash
python main.py \
  --sweep unicast \
  --sweep-start 1 \
  --sweep-end 30 \
  --receivers 10
```

### CSV output

All sweep data can be redirected to a CSV file without generating figures:

```bash
python main.py \
  --sweep receivers \
  --format csv \
  --unicast-stations 10 \
  > receiver-sweep.csv
```

## Command-line options

| Option | Default | Meaning |
| --- | ---: | --- |
| `--receivers` | `10` | Number of multicast receivers, `Nrx` (`1…30`) |
| `--unicast-stations` | `10` | Number of saturated unicast stations, `Nu` (`1…30`) |
| `--sweep` | `none` | `none`, `receivers`, or `unicast` |
| `--sweep-start` | `1` | First sweep value |
| `--sweep-end` | `30` | Last sweep value |
| `--selection-only` | off | Show only the LogRD winner during a sweep |
| `--format` | `table` | `table` or `csv` sweep output |
| `--reliability-threshold` | `0.90` | Minimum reliability accepted by LogRD |

## Tests

Run the standard-library test suite:

```bash
python -m unittest -v
```

The tests verify:

- Table I BER-to-FER values;
- slot-probability normalization;
- baseline reliability, throughput, and selection trends;
- DMS throughput scaling with receiver count;
- finite, physical results throughout the complete `Nrx, Nu = 1…30` domain;
- parameter validation;
- no-feasible-mechanism selection;
- CSV row and line-ending correctness.

## Reproduction limits

The code follows the paper's equations and Table I parameters, but some exact
paper outputs cannot be guaranteed because the publication omits timing/frame
constants and contains several ambiguous or malformed equations. The project
uses documented executable interpretations for:

- acknowledged DMS timing;
- the DMS per-receiver attempt denominator;
- lifetime-to-retry rounding;
- Block Ack expected transmission count with collision correlation; and
- delayed Block Ack lifetime/retry timing.

Inputs are deliberately limited to the paper's evaluated `Nrx, Nu = 1…30`
range. Invalid probabilities, rates, timings, retry stages, and reversed sweep
ranges are rejected rather than converted into plausible-looking output.

### Known benchmark gap

The model reproduces important reported trends—Immediate Block Ack maximizes
multicast throughput, DMS multicast throughput falls with receiver count,
legacy reliability is lowest, and feedback methods approach 100% reliability.
However, its `Nu=12` LogRD selection map does **not** reproduce Figure 13's
published winner regions: the current assumptions select GCR-UR for much of
the middle receiver range where the paper reports Immediate Block Ack.

This is a known validation gap, not a hidden success claim. The remaining
difference is sensitive to omitted simulator timing/frame constants and the
paper's ambiguous DMS/Block Ack equations. Use the output for analytical
experimentation and trend comparison; do not cite its exact selection
boundaries as a reproduction of the published figure without calibrating the
missing original simulator configuration.
