# Paper Analytical Model Guide

This document maps `main.py` to the equations and evaluation methodology in
*Performance Analysis and Algorithm Selection for Reliable Multicast in IEEE
802.11aa Wireless LAN*.

## 1. System model

The model contains:

- one AP with saturated multicast traffic.
- `Nrx` multicast receivers.
- `Nu` independent saturated unicast stations transmitting to the AP.
- one virtual multicast contender competing with the unicast stations.


The implementation is analytical. It does not execute packet events or radio
propagation. It solves transmission probabilities, collision probabilities,
reliability, and saturation throughput directly.

## 2. PHY abstraction

A complete PHY is unnecessary for the paper's analysis. The implementation
uses only:

- DATA and control bit rates.
- payload and control-frame sizes.
- PLCP time.
- BER converted into frame error rate.

For a payload of `L` bits and bit error rate `pe`, paper Eqs. (1)–(2) give:

```text
FER = 1 - (1 - pe)^L
```

With the default 1500-byte payload:

```text
legacy FER at BER 1e-6 ≈ 0.011928
GCR FER at BER 1e-5   ≈ 0.113080
unicast FER at BER 0  = 0
```

No path loss, SNR, distance, fading, capture effect, or dynamic rate adaptation
is modeled.

## 3. DCF contention probabilities

For multicast attempt probability `tau_m`, unicast attempt probability `tau_u`,
and `Nu` unicast contenders, the conditional multicast collision probability is
paper Eq. (6):

```text
p_cm = 1 - (1 - tau_u)^Nu
```

The generic slot probabilities follow paper Eqs. (9)–(14):

```text
P_sm = tau_m * (1 - tau_u)^Nu
P_su = Nu * tau_u * (1 - tau_u)^(Nu - 1) * (1 - tau_m)
P_cu = (1 - tau_m) * [1 - (1 - tau_u)^Nu
                       - Nu*tau_u*(1 - tau_u)^(Nu - 1)]
P_e  = (1 - tau_u)^Nu * (1 - tau_m)
P_cm = 1 - P_sm - P_su - P_cu - P_e
```

`p_cm` is conditional on a multicast attempt. `P_cm` is the probability that a
random model slot contains a collision involving multicast.

For mechanisms with fixed multicast contention behavior, paper Eq. (18) is:

```text
tau_m = 2 / (CWmin_m + 1)
```

The unicast fixed point follows paper Eqs. (19)–(20). The code solves it with
damping and a tolerance defined in `ModelParameters`.

Delayed Block Ack uses paper Eq. (60) for its mechanism-specific `tau_m` and is
solved jointly with the unicast fixed point.

## 4. Average slot duration

For legacy, GCR-UR, DMS, and Immediate Block Ack, paper Eq. (11) is:

```text
T_slot = P_su*T_su + P_cu*T_cu
       + P_sm*T_sm + P_cm*T_cm + P_e*T_e
```

Unicast timing follows paper Eqs. (15)–(16). Legacy/GCR-UR multicast timing
follows Eq. (17). DMS uses an acknowledged multicast-as-unicast timing in this
implementation because that is required by the protocol description, although
the paper's timing reference is ambiguous.

Delayed Block Ack uses its dedicated paper Eq. (48).

## 5. Mechanism equations

### Legacy No-Ack/No-Retry

Reliability, paper Eq. (5):

```text
eta = (1 - p_cm) * (1 - average_FER)
```

Throughput follows paper Eqs. (7)–(8). It uses the Table I legacy multicast
rate of 6 Mb/s.

### GCR Unsolicited Retry

With retry count `R = 2`, every packet is attempted three times.

Reliability, paper Eq. (21):

```text
eta = average_i[1 - (1 - (1-p_cm)*(1-FER_i))^(R+1)]
```

Multicast and unicast throughput follow Eqs. (23)–(24), using a 54 Mb/s DATA
rate and 6 Mb/s control rate.

### Directed Multicast Service

DMS converts one multicast packet into acknowledged unicast delivery to each
receiver. Reliability follows paper Eq. (25), with retry limit 8. Per-receiver
attempt probabilities use executable interpretations of Eqs. (27)–(30), and
throughput follows Eqs. (26) and (31).

DMS reliability is high, but multicast throughput decreases roughly with group
size because the AP performs work separately for every receiver.

### GCR Immediate Block Ack

The default burst contains ten DATA frames. Burst duration follows paper
Eq. (39), including receiver-dependent BAR/BA control overhead represented by
the paper's common `BACK` length.

A collision affects only the beginning of a burst. The number of affected
frames follows Eq. (33), and per-attempt receiver success follows Eqs. (34)–(35).

The 400 ms MSDU lifetime is converted into an effective retry count using the
paper's lifetime approximation and conservative floor rounding. The waiting
term excludes the target multicast burst before adding that burst once; this
avoids double counting it through both `Tslot/tau_m` and `Tburst`. No retry cap
absent from the paper is imposed. Reliability and throughput follow
Eqs. (36)–(38).

The publication's Eqs. (40)–(44) for expected attempts contain malformed
indices and inconsistent retry conventions. `main.py` uses an explicit
truncated expectation:

```text
E[T] = sum over k of P(group is still incomplete after k attempts)
```

The implementation preserves the paper's dependence structure: burst
collision exposure is a common event shared by all receivers, while channel
errors are receiver-specific. For each attempt count it conditions on the
number of collision-free attempts, applies receiver FER to those attempts,
and then uses the survival-sum identity for `E[T]`.

### GCR Delayed Block Ack

The burst fraction follows paper Eq. (45), the dedicated slot duration follows
Eqs. (48)–(50), and the multicast attempt probability follows Eq. (60).
Reliability is shared with the Immediate Block Ack analysis, while throughput
follows Eqs. (46)–(47). Its lifetime conversion uses the Delayed BA burst
probability `tau_m * p_b`, its DATA-burst duration, and Eq. (48)'s non-burst
slot contribution; it no longer reuses the Immediate BA exchange duration.

The paper notes that this is an analytical extension of delayed unicast Block
Ack rather than an explicitly standardized 802.11aa GCR service.

## 6. LogRD mechanism selection

For receiver count `Nrx`, paper Eqs. (61)–(63) use:

```text
multicast weight = 1 + ln(Nrx)
unicast weight   = 1

utility = min(Sm / (1 + ln(Nrx)), Su / Nu)
```

If reliability is below `eta_min`, utility is zero. The default threshold is
90%, matching the paper's evaluation. Reliability is the average probability
that an intended receiver obtains a multicast frame, not the probability that
all receivers obtain the same frame.

The selected mechanism is the feasible mechanism with maximum utility. If no
mechanism reaches the threshold, the API returns `None` and table output says
`no-feasible`; it never labels an unreliable zero-utility mechanism as the
winner. Because the paper does not specify exact-tie handling, input order is
used as a stable tie-break rule.

## 7. Table workflows instead of figures

One scenario:

```bash
python main.py --receivers 10 --unicast-stations 10
```

Receiver reliability/throughput curve data:

```bash
python main.py --sweep receivers --unicast-stations 10
```

Unicast-load curve data:

```bash
python main.py --sweep unicast --receivers 10
```

Mechanism selection at `Nu = 12`:

```bash
python main.py \
  --sweep receivers \
  --unicast-stations 12 \
  --selection-only
```

CSV for external analysis:

```bash
python main.py --sweep receivers --format csv > receiver-sweep.csv
```

No plotting dependency is included.

## 8. Paper omissions and executable choices

The following choices are documented because the PDF alone does not uniquely
define an executable reproduction:

1. Slot/SIFS/DIFS/PLCP values are common 802.11a defaults, not Table I values.
2. MAC header/FCS, ACK, and common BAR/BA (`BACK`) sizes are explicit defaults.
3. DMS timing includes its required ACK even though the paper's timing cross-
   reference is ambiguous.
4. The malformed DMS denominator is interpreted as a sum over all receivers.
5. Block Ack lifetime conversion uses floor rounding.
6. Malformed Block Ack expected-attempt equations are replaced with a stable
   truncated expectation.
7. Delayed Block Ack Eq. (60) is required to produce a finite probability in
   `(0, 1)`; singular or nonphysical results raise `ModelDomainError` rather
   than being clamped into plausible-looking output.
8. The supported station domain is the paper's tested `Nrx, Nu = 1…30` range.
9. Block Ack expected attempts preserve common collision events and independent
   per-receiver channel errors.

These choices preserve the paper's analytical structure and make every
assumption inspectable and testable. Exact agreement with the authors' OMNeT++
numeric curves requires their omitted simulator constants and intended
resolutions of the published equation ambiguities.

## 9. Known benchmark mismatch

The implementation reproduces several qualitative trends reported in the
paper, but the exact `Nu=12` LogRD winner map remains different. The paper
reports GCR-UR at `Nrx={1,4,5,28,29,30}`, Delayed BA at `Nrx={2,3}`, and
Immediate BA at `Nrx=6…27`. With the explicitly documented timing constants
and equation repairs in this project, GCR-UR remains selected through most of
the middle range.

This means the throughput/reliability tables are valid outputs of the coded
analytical assumptions, but the selection boundaries are not yet a validated
numeric reproduction of Figure 13. Calibrating them requires the authors'
omitted OMNeT++ timing/frame configuration or an authoritative resolution of
the ambiguous DMS and Block Ack equations.
