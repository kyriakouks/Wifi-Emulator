from __future__ import annotations

import argparse
import heapq
import random
from collections import deque
from dataclasses import dataclass, field
from typing import Deque


# All times are stored in seconds. The required fixed medium delay is
# 5 ms = 0.005 seconds for every successful station-to-AP transmission.
MEDIUM_DELAY_S = 0.005

# A station waits an AIFS period plus a random number of Wi-Fi slots before
# attempting transmission. TX_AIRTIME_S is a simplified frame airtime.
SLOT_TIME_S = 9e-6
TX_AIRTIME_S = 0.001
MAX_RETRIES = 4


@dataclass(frozen=True)
class AccessCategory:
    name: str
    aifs_slots: int  # waiting time before attempting transmission
    cw_min: int  # minimum contention window size
    cw_max: int  # maximum contention window size
    priority: int


# Generic EDCA categories deliberately do not identify an application type.
# Their AIFS, contention windows, and priorities retain the existing
# category-specific contention behavior.
ACCESS_CATEGORIES = {
    "AC-1": AccessCategory("AC-1", 2, 3, 7, 4),
    "AC-2": AccessCategory("AC-2", 2, 7, 15, 3),
    "AC-3": AccessCategory("AC-3", 3, 15, 1023, 2),
    "AC-4": AccessCategory("AC-4", 7, 15, 1023, 1),
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
    # A packet keeps enough information to calculate delivery statistics and
    # to place it back in its access-category queue after a collision.
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


@dataclass(order=True)
class Event:
    # Events are kept in a min-heap, so the next event in simulation time is
    # always processed first. sequence makes same-time events deterministic.
    time: float
    sequence: int
    kind: str = field(compare=False)
    node_name: str | None = field(default=None, compare=False)
    packet: Packet | None = field(default=None, compare=False)


@dataclass
class Node:
    name: str
    role: str
    # Each station has one queue per generic EDCA access category. Only one
    # packet can be waiting for the medium on behalf of a node at a time.
    queues: dict[str, Deque[Packet]] = field(
        default_factory=lambda: {
            category: deque() for category in ACCESS_CATEGORIES
        }
    )
    contention_packet: Packet | None = None
    generated: int = 0
    delivered: int = 0
    dropped: int = 0
    collisions: int = 0


class FixedWifiSimulation:
    """Discrete-event simulation of a fixed-node, EDCA-style Wi-Fi network."""

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
            + MEDIUM_DELAY_S
        )

    def schedule(
        self,
        time: float,
        kind: str,
        node_name: str | None = None,
        packet: Packet | None = None,
    ) -> None:
        # Using a heap means events are processed in chronological order while
        # the sequence number gives stable ordering for equal timestamps.
        self.event_sequence += 1
        heapq.heappush(
            self.events,
            Event(time, self.event_sequence, kind, node_name, packet),
        )

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

            # Deliveries and new packets are handled before contention at the
            # same timestamp, keeping the event ordering predictable.
            for event in batch:
                if event.kind == "receive":
                    self.handle_receive(event)
                elif event.kind == "generate":
                    self.handle_generate(event)

            attempts = [
                event
                for event in batch
                if event.kind == "attempt" and event.node_name is not None
            ]
            if attempts:
                self.handle_attempts(attempts)

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
        # Generic categories are assigned randomly per packet and are
        # independent of the packet's source station.
        category_name = self.random.choice(ACCESS_CATEGORY_POOL)
        packet = Packet(
            sequence=self.packet_sequence,
            source=node.name,
            destination="AP",
            access_category=category_name,
            created_at=self.now,
            size_bytes=1200,
        )
        # Enqueue using the packet category, not a station-wide traffic label.
        self.enqueue_packet(node, packet)
        node.generated += 1
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
        if node.contention_packet is not None:
            return

        packet = self.dequeue_next_packet(node)
        if packet is None:
            return

        category = validate_access_category(packet.access_category)
        packet.attempts += 1

        # EDCA gives higher-priority traffic shorter AIFS/CW values. This is a
        # simplified backoff model: a random slot count is selected for the
        # packet's access category before its attempt event is scheduled.
        packet.backoff = self.random.randint(0, category.cw_min)
        contention_delay = (
            category.aifs_slots + packet.backoff
        ) * SLOT_TIME_S

        node.contention_packet = packet
        self.schedule(
            start_time + contention_delay,
            "attempt",
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
        # Filter at packet level so the packet's own category selects its EDCA
        # priority, even if a queue was populated by external caller code.
        best_packet: Packet | None = None
        best_queue: Deque[Packet] | None = None
        best_index = -1
        best_priority = -1
        for queue in node.queues.values():
            for index, packet in enumerate(queue):
                category = validate_access_category(packet.access_category)
                if category.priority > best_priority:
                    best_packet = packet
                    best_queue = queue
                    best_index = index
                    best_priority = category.priority

        if best_packet is None or best_queue is None:
            return None
        del best_queue[best_index]
        return best_packet

    @staticmethod
    def requeue_packet(node: Node, packet: Packet) -> None:
        # Requeue by the packet category so a collision cannot move it to a
        # different category's queue.
        FixedWifiSimulation.enqueue_packet(node, packet, front=True)

    def handle_attempts(self, attempts: list[Event]) -> None:
        # Ignore stale attempt events. A packet may have been requeued after
        # another event already changed the station's contention state.
        eligible = []
        for event in attempts:
            if event.node_name is None:
                continue
            node = self.nodes[event.node_name]
            if node.contention_packet is event.packet:
                eligible.append(event)

        if not eligible:
            return

        if self.now < self.medium_busy_until:
            # The medium became busy before these stations could transmit.
            # Put their packets back and let them contend after the medium is
            # available again.
            for event in eligible:
                node = self.nodes[event.node_name]
                packet = node.contention_packet
                node.contention_packet = None
                if packet is not None:
                    self.requeue_packet(node, packet)
                    self.begin_contention(node, self.medium_busy_until)
            return

        if len(eligible) > 1:
            # Multiple attempts in the same event batch mean that stations
            # selected the same slot, producing a shared-medium collision.
            self.collisions += 1
            for event in eligible:
                node = self.nodes[event.node_name]
                packet = node.contention_packet
                node.contention_packet = None
                node.collisions += 1
                if packet is None:
                    continue
                if packet.attempts <= MAX_RETRIES:
                    self.requeue_packet(node, packet)
                    self.begin_contention(node, self.now)
                else:
                    node.dropped += 1
            return

        event = eligible[0]
        node = self.nodes[event.node_name]
        packet = node.contention_packet
        node.contention_packet = None
        if packet is None:
            return

        # Exactly one station won the contention period. The packet is
        # delivered after the required fixed 5 ms one-way medium delay.
        self.transmitted += 1
        self.medium_busy_until = self.now + TX_AIRTIME_S + MEDIUM_DELAY_S
        self.schedule(
            self.now + MEDIUM_DELAY_S,
            "receive",
            "AP",
            packet,
        )
        self.begin_contention(node, self.medium_busy_until)

    def minimum_deliveries_reached(self) -> bool:
        return all(
            node.delivered >= self.minimum_packets_per_station
            for node in self.nodes.values()
            if node.role == "station"
        )

    def handle_receive(self, event: Event) -> None:
        if event.packet is None:
            return

        packet = event.packet
        self.delivered += 1
        self.nodes[packet.source].delivered += 1

        # Latency includes queueing, EDCA backoff, airtime, and the fixed
        # medium delay. The fixed delay itself is always exactly 5 ms.
        latency = self.now - packet.created_at
        self.total_latency += latency
        self.maximum_latency = max(self.maximum_latency, latency)

    def report(self) -> None:
        average_latency_ms = (
            self.total_latency / self.delivered * 1000
            if self.delivered
            else 0.0
        )
        print("Fixed 802.11aa-oriented Wi-Fi simulation")
        print("------------------------------------------")
        print(f"Simulation duration: {self.duration_s:.3f} s")
        print(f"Fixed nodes: {', '.join(self.nodes)}")
        print(f"Fixed medium delay: {MEDIUM_DELAY_S * 1000:.3f} ms")
        print(f"Transmissions: {self.transmitted}")
        print(f"Delivered packets: {self.delivered}")
        print(f"Collisions: {self.collisions}")
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


def parse_args() -> argparse.Namespace:
    # The topology and 5 ms medium delay remain fixed. These options only
    # control how long traffic is generated and how reproducible it is.
    parser = argparse.ArgumentParser(
        description="Run a fixed-node 802.11aa-oriented Wi-Fi simulation."
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
    simulation = FixedWifiSimulation(
        duration_s=args.duration,
        packet_interval_s=args.interval,
        seed=args.seed,
        minimum_packets_per_station=args.minimum_packets_per_station,
    )
    simulation.start()
    simulation.report()


if __name__ == "__main__":
    main()
