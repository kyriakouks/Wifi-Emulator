# RTS/CTS Implementation Explanation

This document explains the RTS/CTS (Request To Send / Clear To Send) protocol implementation in `RTS-CTS.py` and how it differs from the basic simulation in `main.py`.

## What is RTS/CTS?

RTS/CTS is a collision avoidance mechanism used in Wi-Fi networks to reduce packet collisions, especially for large data frames.

### Protocol Flow

Without RTS/CTS (`main.py`):
```
Station → [DATA packet] → AP
         (5 ms delay)
```

With RTS/CTS (`RTS-CTS.py`):
```
1. Station → [RTS frame] → AP       (5 ms delay)
2. AP → [CTS frame] → Station        (5 ms delay)
3. Station → [DATA packet] → AP      (5 ms delay)
```

Total: **3 × 5 ms = 15 ms minimum** for successful delivery (plus contention time).

## Key Differences

### 1. Frame Types

**`main.py`**: Only DATA packets
**`RTS-CTS.py`**: Three frame types:
- `RTS` (Request To Send) - small control frame
- `CTS` (Clear To Send) - small control frame
- `DATA` - actual packet payload

### 2. Station States

**`main.py`**: Stations are either contending or idle

**`RTS-CTS.py`**: Stations have multiple states:
```python
class StationState(Enum):
    IDLE = "idle"
    CONTENDING = "contending"
    WAITING_FOR_CTS = "waiting_for_cts"
    READY_TO_SEND_DATA = "ready_to_send_data"
```

### 3. Event Types

**`main.py`** events:
- `generate` - create packet
- `attempt` - try to transmit
- `receive` - packet delivered

**`RTS-CTS.py`** events:
- `generate` - create packet
- `attempt_rts` - try to send RTS
- `receive_rts` - AP receives RTS
- `receive_cts` - Station receives CTS
- `attempt_data` - send DATA
- `receive_data` - AP receives DATA
- `cts_timeout` - CTS not received in time

### 4. Collision Detection

**`main.py`**: Collisions only occur when multiple stations attempt DATA at the same time.

**`RTS-CTS.py`**: Collisions occur during RTS phase. If multiple stations send RTS simultaneously, they collide and retry. Once CTS is received, DATA transmission is protected.

### 5. AP Behavior

**`main.py`**: AP is passive - only receives packets

**`RTS-CTS.py`**: AP is active:
- Receives RTS frames
- Immediately sends CTS responses
- Then receives DATA packets

### 6. Timeout Handling

**`main.py`**: No timeouts

**`RTS-CTS.py`**: CTS timeout mechanism:
- Station waits up to 50 ms for CTS
- If no CTS arrives, packet is retried
- Timeout counter tracked per station

## Code Locations in RTS-CTS.py

### RTS Transmission
Lines 436-473: `handle_rts_attempts()`
- Station wins contention
- Sends RTS frame
- Waits for CTS

### CTS Response
Lines 475-493: `handle_receive_rts()`
- AP receives RTS
- Immediately sends CTS back

### Data Transmission
Lines 499-522: `handle_receive_cts()`
- Station receives CTS
- Schedules DATA transmission

Lines 548-586: `handle_data_attempt()`
- Station sends DATA frame
- After receiving CTS permission

### Delivery
Lines 588-603: `handle_receive_data()`
- AP receives DATA
- Packet successfully delivered

## Statistics Tracked

**Additional statistics in `RTS-CTS.py`**:
- `rts_sent` - RTS frames transmitted
- `cts_received` - CTS frames received
- `cts_timeouts` - CTS not received in time
- `rts_collisions` - Collisions during RTS phase

## Usage

Both simulators use the same command-line interface:

```bash
# Basic simulation (no RTS/CTS)
python main.py --duration 1 --interval 0.02 --seed 7

# RTS/CTS simulation
python RTS-CTS.py --duration 1 --interval 0.02 --seed 7
```

## Performance Impact

RTS/CTS adds overhead:
- **Minimum latency**: 15 ms (3 × 5 ms) vs 5 ms in basic simulation
- **Benefit**: Reduces collisions for large data frames
- **Collision protection**: Only RTS frames can collide; DATA is protected

## When to Use Each Simulation

**Use `main.py` when:**
- You want basic EDCA contention behavior
- Studying queue priorities
- Minimal protocol overhead matters

**Use `RTS-CTS.py` when:**
- You need collision avoidance mechanism
- Studying hidden node scenarios
- Analyzing control frame overhead
- Simulating networks with large data frames
