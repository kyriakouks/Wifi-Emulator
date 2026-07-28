from __future__ import annotations

import argparse
import heapq
import random
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque


# All times are stored in seconds. The required fixed medium delay is
# 5 ms = 0.005 seconds for every successful transmission (RTS, CTS, or DATA).
MEDIUM_DELAY = 0.005

# A station waits an AIFS period plus a random number of Wi-Fi slots before
# attempting transmission. Frame airtimes are simplified.
SLOT_TIME = 9e-6
RTS_AIRTIME = 0.0002  # RTS is a small control frame
CTS_AIRTIME = 0.0002  # CTS is a small control frame
DATA_AIRTIME = 0.001  # DATA frame airtime
CTS_TIMEOUT = 0.050   # Station waits up to 50ms for CTS response
MAX_RETRIES = 4


class FrameType(Enum):
    """Types of frames in the RTS/CTS/DATA exchange."""
    RTS = "RTS"
    CTS = "CTS"
    DATA = "DATA"


class StationState(Enum):
    """Station contention states for RTS/CTS protocol."""
    IDLE = "idle"
    CONTENDING = "contending"
    WAITING_FOR_CTS = "waiting_for_cts"
    READY_TO_SEND_DATA = "ready_to_send_data"


@dataclass(frozen=True)
class AccessCategory:
    name: str
    aifs_slots: int  # waiting time before attempting transmission
    cw_min: int  # minimum contention window size
    cw_max: int  # maximum contention window size
    priority: int


# Standard 802.11e/802.11aa EDCA access categories. Each category has its
# own AIFS, contention window, and priority value. Stations are generic;
# the category is chosen randomly per packet, not locked to a station.
ACCESS_CATEGORIES = {
    "voice":       AccessCategory("voice",       2, 3,  7,    4),
    "video":       AccessCategory("video",       2, 7,  15,   3),
    "best-effort": AccessCategory("best-effort", 3, 15, 1023, 2),
    "background":  AccessCategory("background",  7, 15, 1023, 1),
}
ACCESS_CATEGORY_POOL = tuple(ACCESS_CATEGORIES)
DEFAULT_MINIMUM_PACKETS_PER_STATION = 5


def validate_access_category(category_name: str) -> AccessCategory:
    """Return a configured category or reject an invalid packet category."""
    try:
        return ACCESS_CATEGORIES[category_name]
    except (KeyError, TypeError) as error:
        raise ValueError(f"Invalid access category: {category_name!r}") from error


@dataclass
class Packet:
    """A data packet that will be transmitted using RTS/CTS protocol."""
    sequence: int
    source: str
    destination: str
    access_category: str
    created_at: float
    size_bytes: int
    attempts: int = 0
    backoff: int = 0

    def __post_init__(self) -> None:
        # Reject invalid categories at packet creation instead of allowing a
        # malformed packet to enter a station queue.
        _ = validate_access_category(self.access_category)


@dataclass
class Frame:
    """
    A frame that can be transmitted on the medium.
    RTS and CTS are control frames; DATA carries the actual packet.
    """
    frame_type: FrameType
    source: str
    destination: str
    packet: Packet | None = None  # Only DATA frames carry a packet
    created_at: float = 0.0


@dataclass(order=True)
class Event:
    # Events are kept in a min-heap, so the next event in simulation time is
    # always processed first. sequence makes same-time events deterministic.
    time: float
    sequence: int
    kind: str = field(compare=False)
    node_name: str | None = field(default=None, compare=False)
    packet: Packet | None = field(default=None, compare=False)
    frame: Frame | None = field(default=None, compare=False)


@dataclass
class Node:
    name: str
    role: str
    # Each station keeps one FIFO queue per EDCA category. Only one packet
    # can be contending for the medium at a time.
    queues: dict[str, Deque[Packet]] = field(
        default_factory=lambda: {
            category: deque() for category in ACCESS_CATEGORIES
        }
    )
    state: StationState = StationState.IDLE
    contention_packet: Packet | None = None
    cts_timeout_event_seq: int | None = None  # Track CTS timeout events
    generated: int = 0
    delivered: int = 0
    dropped: int = 0
    collisions: int = 0
    rts_sent: int = 0
    cts_received: int = 0
    cts_timeouts: int = 0
    # Counts how many packets were generated in each EDCA category. Filled
    # during the simulation and printed in the report for each station.
    category_counts: dict[str, int] = field(
        default_factory=lambda: {cat: 0 for cat in ACCESS_CATEGORIES}
    )


class RTSCTSWifiSimulation:
    """
    Discrete-event simulation of a fixed-node Wi-Fi network using RTS/CTS.

    Protocol flow:
    1. Station wins EDCA contention and sends RTS
    2. AP receives RTS after 5ms and immediately sends CTS
    3. Station receives CTS after 5ms and sends DATA
    4. AP receives DATA after 5ms (delivery complete)
    """

    def __init__(
        self,
        duration_s: float = 1.0,
        packet_interval_s: float = 0.02,
        seed: int = 7,
        minimum_packets_per_station: int = DEFAULT_MINIMUM_PACKETS_PER_STATION,
    ) -> None:
        if duration_s <= 0:
            raise ValueError("duration must be greater than zero")
        if packet_interval_s <= 0:
            raise ValueError("packet interval must be greater than zero")
        if minimum_packets_per_station < DEFAULT_MINIMUM_PACKETS_PER_STATION:
            raise ValueError(
                f"minimum packets per station must be at least {DEFAULT_MINIMUM_PACKETS_PER_STATION}"
            )

        self.duration_s = duration_s
        self.packet_interval_s = packet_interval_s
        self.minimum_packets_per_station = minimum_packets_per_station
        self.random = random.Random(seed)

        # Fixed topology: one AP and four fixed stations. Category selection is
        # made independently for every packet, rather than per station.
        self.nodes: dict[str, Node] = {
            "AP": Node("AP", "access point"),
            "STA-1": Node("STA-1", "station"),
            "STA-2": Node("STA-2", "station"),
            "STA-3": Node("STA-3", "station"),
            "STA-4": Node("STA-4", "station"),
        }

        # The event heap is the simulation clock: no real-time waiting occurs.
        self.events: list[Event] = []
        self.event_sequence = 0
        self.packet_sequence = 0
        self.now = 0.0

        # A transmission reserves the shared medium until its simplified
        # airtime and the fixed 5 ms propagation delay have elapsed.
        self.medium_busy_until = 0.0

        self.transmitted = 0
        self.delivered = 0
        self.collisions = 0
        self.rts_collisions = 0
        self.total_latency = 0.0
        self.maximum_latency = 0.0

        # A short run still gets the configured minimum number of packets per
        # station. Normal duration-based generation remains unchanged when it
        # already produces at least that many packets.
        self.minimum_generation_end_s = (
            self.minimum_packets_per_station - 1
        ) * self.packet_interval_s
        self.processing_horizon_s = (
            max(self.duration_s, self.minimum_generation_end_s)
            + MEDIUM_DELAY * 3 + CTS_TIMEOUT  # Allow full RTS/CTS/DATA exchange
        )

    def schedule(
        self,
        time: float,
        kind: str,
        node_name: str | None = None,
        packet: Packet | None = None,
        frame: Frame | None = None,
    ) -> int:
        # Using a heap means events are processed in chronological order while
        # the sequence number gives stable ordering for equal timestamps.
        self.event_sequence += 1
        heapq.heappush(
            self.events,
            Event(time, self.event_sequence, kind, node_name, packet, frame),
        )
        return self.event_sequence

    def start(self) -> None:
        # Start one packet-generation stream for each fixed station. The AP
        # receives traffic but does not generate traffic in this model.
        for node in self.nodes.values():
            if node.role == "station":
                self.schedule(0.0, "generate", node.name)
        self.run()

    def run(self) -> None:
        while self.events:
            # Pop the earliest event and group other events at the same time.
            # Grouping is important because simultaneous attempts represent a
            # collision on a shared Wi-Fi medium.
            first_event = heapq.heappop(self.events)
            if (
                first_event.time > self.processing_horizon_s
                and self.minimum_deliveries_reached()
            ):
                break

            self.now = first_event.time
            batch = [first_event]
            while self.events and abs(self.events[0].time - self.now) < 1e-12:
                batch.append(heapq.heappop(self.events))

            # Process events in order: receives first, then generation, then attempts
            for event in batch:
                if event.kind == "receive_rts":
                    self.handle_receive_rts(event)
                elif event.kind == "receive_cts":
                    self.handle_receive_cts(event)
                elif event.kind == "receive_data":
                    self.handle_receive_data(event)
                elif event.kind == "generate":
                    self.handle_generate(event)
                elif event.kind == "cts_timeout":
                    self.handle_cts_timeout(event)

            # RTS attempts are grouped for collision detection
            rts_attempts = [
                event
                for event in batch
                if event.kind == "attempt_rts" and event.node_name is not None
            ]
            if rts_attempts:
                self.handle_rts_attempts(rts_attempts)

            # DATA attempts happen individually after CTS is received
            data_attempts = [
                event
                for event in batch
                if event.kind == "attempt_data" and event.node_name is not None
            ]
            for event in data_attempts:
                self.handle_data_attempt(event)

    def handle_generate(self, event: Event) -> None:
        if event.node_name is None:
            return

        node = self.nodes[event.node_name]
        # Generation normally follows duration/interval. For a short run, the
        # minimum count is allowed to extend the generation stream.
        if (
            event.time > self.duration_s
            and node.generated >= self.minimum_packets_per_station
        ):
            return

        self.packet_sequence += 1
        # Each packet is independently assigned a random EDCA category so that
        # a generic station can produce any kind of traffic (voice, video,
        # best-effort, or background) across different packets.
        category_name = self.random.choice(ACCESS_CATEGORY_POOL)
        packet = Packet(
            sequence=self.packet_sequence,
            source=node.name,
            destination="AP",
            access_category=category_name,
            created_at=self.now,
            size_bytes=1200,
        )
        self.enqueue_packet(node, packet)
        node.generated += 1
        # Track how many packets of each EDCA category this station produced.
        node.category_counts[category_name] += 1
        self.begin_contention(node, self.now)

        # Preserve interval-based generation through the requested duration,
        # while extending only until the station reaches the minimum count.
        next_generation = self.now + self.packet_interval_s
        if (
            next_generation <= self.duration_s
            or node.generated < self.minimum_packets_per_station
        ):
            self.schedule(next_generation, "generate", node.name)

    def begin_contention(self, node: Node, start_time: float) -> None:
        # Do not create two simultaneous contention attempts for one station.
        if node.state != StationState.IDLE or node.contention_packet is not None:
            return

        packet = self.dequeue_next_packet(node)
        if packet is None:
            return

        category = validate_access_category(packet.access_category)
        packet.attempts += 1

        # EDCA gives higher-priority traffic shorter AIFS/CW values. This is a
        # simplified backoff model: a random slot count is selected for the
        # packet's access category before its RTS attempt event is scheduled.
        packet.backoff = self.random.randint(0, category.cw_min)
        contention_delay = (
            category.aifs_slots + packet.backoff
        ) * SLOT_TIME

        node.contention_packet = packet
        node.state = StationState.CONTENDING
        self.schedule(
            start_time + contention_delay,
            "attempt_rts",
            node.name,
            packet,
        )

    @staticmethod
    def enqueue_packet(node: Node, packet: Packet, front: bool = False) -> None:
        category = validate_access_category(packet.access_category)
        queue = node.queues[category.name]
        if front:
            queue.appendleft(packet)
        else:
            queue.append(packet)

    @staticmethod
    def dequeue_next_packet(node: Node) -> Packet | None:
        # Walk queues from highest to lowest EDCA priority and return the
        # front packet of the first non-empty one. This is standard EDCA
        # FIFO-within-priority behaviour: voice is always served before video,
        # video before best-effort, and best-effort before background.
        for category in sorted(
            ACCESS_CATEGORIES.values(),
            key=lambda c: c.priority,
            reverse=True,
        ):
            queue = node.queues[category.name]
            if queue:
                return queue.popleft()
        return None

    @staticmethod
    def requeue_packet(node: Node, packet: Packet) -> None:
        # Requeue by the packet category so a collision cannot move it to a
        # different category's queue.
        RTSCTSWifiSimulation.enqueue_packet(node, packet, front=True)

    def handle_rts_attempts(self, attempts: list[Event]) -> None:
        # Ignore stale attempt events. A packet may have been requeued after
        # another event already changed the station's contention state.
        eligible = []
        for event in attempts:
            if event.node_name is None:
                continue
            node = self.nodes[event.node_name]
            if (
                node.contention_packet is event.packet
                and node.state == StationState.CONTENDING
            ):
                eligible.append(event)

        if not eligible:
            return

        if self.now < self.medium_busy_until:
            # The medium became busy before these stations could transmit RTS.
            # Put their packets back and let them contend after the medium is
            # available again.
            for event in eligible:
                node = self.nodes[event.node_name]
                packet = node.contention_packet
                node.contention_packet = None
                node.state = StationState.IDLE
                if packet is not None:
                    self.requeue_packet(node, packet)
                    self.begin_contention(node, self.medium_busy_until)
            return

        if len(eligible) > 1:
            # Multiple RTS attempts in the same event batch mean that stations
            # selected the same slot, producing a collision on the RTS frame.
            self.collisions += 1
            self.rts_collisions += 1
            for event in eligible:
                node = self.nodes[event.node_name]
                packet = node.contention_packet
                node.contention_packet = None
                node.state = StationState.IDLE
                node.collisions += 1
                if packet is None:
                    continue
                if packet.attempts <= MAX_RETRIES:
                    self.requeue_packet(node, packet)
                    self.begin_contention(node, self.now)
                else:
                    node.dropped += 1
            return

        # Exactly one station won the contention period. Send RTS frame.
        event = eligible[0]
        node = self.nodes[event.node_name]
        packet = node.contention_packet
        if packet is None:
            node.state = StationState.IDLE
            return

        # Station sends RTS and waits for CTS response
        node.state = StationState.WAITING_FOR_CTS
        node.rts_sent += 1
        rts_frame = Frame(
            frame_type=FrameType.RTS,
            source=node.name,
            destination="AP",
            packet=packet,
            created_at=self.now,
        )

        # Reserve medium for RTS transmission
        self.medium_busy_until = self.now + RTS_AIRTIME + MEDIUM_DELAY

        # Schedule RTS arrival at AP after fixed 5ms delay
        self.schedule(
            self.now + MEDIUM_DELAY,
            "receive_rts",
            "AP",
            frame=rts_frame,
        )

        # Schedule CTS timeout in case AP doesn't respond
        timeout_seq = self.schedule(
            self.now + CTS_TIMEOUT,
            "cts_timeout",
            node.name,
            packet,
        )
        node.cts_timeout_event_seq = timeout_seq

    def handle_receive_rts(self, event: Event) -> None:
        """AP receives RTS and immediately sends CTS back."""
        if event.frame is None or event.frame.frame_type != FrameType.RTS:
            return

        rts_frame = event.frame
        source_station = rts_frame.source

        # AP immediately responds with CTS
        cts_frame = Frame(
            frame_type=FrameType.CTS,
            source="AP",
            destination=source_station,
            packet=rts_frame.packet,
            created_at=self.now,
        )

        # Reserve medium for CTS transmission
        self.medium_busy_until = self.now + CTS_AIRTIME + MEDIUM_DELAY

        # CTS arrives at station after fixed 5ms delay
        self.schedule(
            self.now + MEDIUM_DELAY,
            "receive_cts",
            source_station,
            frame=cts_frame,
        )

    def handle_receive_cts(self, event: Event) -> None:
        """Station receives CTS and can now send DATA."""
        if event.node_name is None or event.frame is None:
            return
        if event.frame.frame_type != FrameType.CTS:
            return

        node = self.nodes[event.node_name]
        cts_frame = event.frame

        # Ignore CTS if station is no longer waiting or packet doesn't match
        if (
            node.state != StationState.WAITING_FOR_CTS
            or node.contention_packet is not cts_frame.packet
        ):
            return

        # Cancel the CTS timeout event (it's no longer needed)
        node.cts_timeout_event_seq = None
        node.cts_received += 1
        node.state = StationState.READY_TO_SEND_DATA

        # Station can now send DATA frame immediately
        self.schedule(
            self.now,
            "attempt_data",
            node.name,
            node.contention_packet,
        )

    def handle_cts_timeout(self, event: Event) -> None:
        """Station didn't receive CTS in time - treat as failure and retry."""
        if event.node_name is None or event.packet is None:
            return

        node = self.nodes[event.node_name]

        # Ignore timeout if it's not the current expected timeout
        if node.cts_timeout_event_seq != event.sequence:
            return

        # Timeout occurred - station never received CTS
        node.cts_timeouts += 1
        packet = node.contention_packet
        node.contention_packet = None
        node.state = StationState.IDLE
        node.cts_timeout_event_seq = None

        if packet is None:
            return

        # Retry if attempts remain
        if packet.attempts <= MAX_RETRIES:
            self.requeue_packet(node, packet)
            self.begin_contention(node, self.now)
        else:
            node.dropped += 1

    def handle_data_attempt(self, event: Event) -> None:
        """Station sends DATA frame after receiving CTS."""
        if event.node_name is None or event.packet is None:
            return

        node = self.nodes[event.node_name]

        # Verify station is ready to send data
        if (
            node.state != StationState.READY_TO_SEND_DATA
            or node.contention_packet is not event.packet
        ):
            return

        packet = node.contention_packet
        node.contention_packet = None
        node.state = StationState.IDLE

        if packet is None:
            return

        # Station sends DATA frame
        self.transmitted += 1
        data_frame = Frame(
            frame_type=FrameType.DATA,
            source=node.name,
            destination="AP",
            packet=packet,
            created_at=self.now,
        )

        # Reserve medium for DATA transmission
        self.medium_busy_until = self.now + DATA_AIRTIME + MEDIUM_DELAY

        # DATA arrives at AP after fixed 5ms delay
        self.schedule(
            self.now + MEDIUM_DELAY,
            "receive_data",
            "AP",
            frame=data_frame,
        )

        # Station can now contend for next packet
        self.begin_contention(node, self.medium_busy_until)

    def handle_receive_data(self, event: Event) -> None:
        """AP receives DATA frame - delivery complete."""
        if event.frame is None or event.frame.frame_type != FrameType.DATA:
            return

        data_frame = event.frame
        packet = data_frame.packet
        if packet is None:
            return

        self.delivered += 1
        self.nodes[packet.source].delivered += 1

        # Latency includes queueing, EDCA backoff, RTS delay, CTS delay,
        # DATA delay. Each transmission uses the fixed 5 ms medium delay.
        latency = self.now - packet.created_at
        self.total_latency += latency
        self.maximum_latency = max(self.maximum_latency, latency)

    def minimum_deliveries_reached(self) -> bool:
        return all(
            node.delivered >= self.minimum_packets_per_station
            for node in self.nodes.values()
            if node.role == "station"
        )

    def report(self) -> None:
        average_latency_ms = (
            self.total_latency / self.delivered * 1000
            if self.delivered
            else 0.0
        )
        print("Fixed 802.11aa-oriented Wi-Fi simulation with RTS/CTS")
        print("-------------------------------------------------------")
        print(f"Simulation duration: {self.duration_s:.3f} s")
        print(f"Fixed nodes: {', '.join(self.nodes)}")
        print(f"Fixed medium delay: {MEDIUM_DELAY * 1000:.3f} ms (per frame)")
        print(f"Protocol: RTS/CTS/DATA (3 × 5ms = 15ms minimum per packet)")
        print(f"Transmissions (DATA frames): {self.transmitted}")
        print(f"Delivered packets: {self.delivered}")
        print(f"Total collisions: {self.collisions}")
        print(f"  RTS collisions: {self.rts_collisions}")
        print(f"Average latency: {average_latency_ms:.3f} ms")
        print(f"Maximum latency: {self.maximum_latency * 1000:.3f} ms")
        print("\nPer-station results:")
        for node in self.nodes.values():
            if node.role == "station":
                print(
                    f"  {node.name}: generated={node.generated}, "
                    f"delivered={node.delivered}, dropped={node.dropped}, "
                    f"collisions={node.collisions}"
                )
                print(
                    f"    RTS sent={node.rts_sent}, CTS received={node.cts_received}, "
                    f"CTS timeouts={node.cts_timeouts}"
                )
                # Show how many packets of each EDCA category this station
                # produced, confirming that traffic is spread across all four
                # discrete categories during the simulation run.
                mix = ", ".join(
                    f"{cat}={node.category_counts[cat]}"
                    for cat in ACCESS_CATEGORIES
                )
                print(f"    traffic mix: [{mix}]")


def parse_args() -> argparse.Namespace:
    # The topology and 5 ms medium delay remain fixed. These options only
    # control how long traffic is generated and how reproducible it is.
    parser = argparse.ArgumentParser(
        description="Run a fixed-node 802.11aa-oriented Wi-Fi simulation with RTS/CTS."
    )
    parser.add_argument("--duration", type=float, default=1.0)
    parser.add_argument("--interval", type=float, default=0.02)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument(
        "--minimum-packets-per-station",
        "--packets-per-station",
        dest="minimum_packets_per_station",
        type=int,
        default=DEFAULT_MINIMUM_PACKETS_PER_STATION,
        help="minimum packets generated and delivered per station (default: 5)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    simulation = RTSCTSWifiSimulation(
        duration_s=args.duration,
        packet_interval_s=args.interval,
        seed=args.seed,
        minimum_packets_per_station=args.minimum_packets_per_station,
    )
    simulation.start()
    simulation.report()


if __name__ == "__main__":
    main()
