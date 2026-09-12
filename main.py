from __future__ import annotations

import argparse
import hashlib
import heapq
import random
from collections import deque
from dataclasses import dataclass, field
from enum import Enum


# All times are measured in seconds. Every AP-to-station or station-to-AP
# transmission crosses the same fixed medium delay, as required by the model.
MEDIUM_DELAY_S = 0.005

# These are fixed MAC timing values used only to order simulated frames. They
# are not derived from a PHY rate, SNR, distance, or modulation scheme.
SLOT_TIME_S = 9e-6
SIFS_S = 16e-6
GROUP_DATA_AIRTIME_S = 0.001
BLOCK_ACK_AIRTIME_S = 0.0002

DEFAULT_MINIMUM_GROUP_PACKETS = 5
DEFAULT_GCR_RETRIES = 2
DEFAULT_RECEIVER_MISS_PROBABILITY = 0.20
GROUP_MEMBERS = ("STA-1", "STA-2", "STA-3", "STA-4")


class GroupcastMode(str, Enum):
    """Group-addressed delivery modes compared by this lightweight model."""

    LEGACY = "legacy"
    GCR_UNSOLICITED_RETRY = "gcr-ur"
    GCR_BLOCK_ACK = "gcr-ba"


class FrameType(str, Enum):
    """Frames that occupy the simplified shared medium."""

    GROUP_DATA = "group-data"
    BLOCK_ACK = "block-ack"


@dataclass(frozen=True)
class AccessCategory:
    """The fixed EDCA parameters used to schedule AP group traffic."""

    name: str
    aifs_slots: int
    cw_min: int
    cw_max: int
    priority: int


# These are the conventional EDCA categories. The simulator uses them only to
# prioritize AP queues; it does not claim full EDCA virtual-collision behavior.
ACCESS_CATEGORIES = {
    "voice": AccessCategory("voice", 2, 3, 7, 4),
    "video": AccessCategory("video", 2, 7, 15, 3),
    "best-effort": AccessCategory("best-effort", 3, 15, 1023, 2),
    "background": AccessCategory("background", 7, 15, 1023, 1),
}
ACCESS_CATEGORY_POOL = tuple(ACCESS_CATEGORIES)


@dataclass
class GroupPacket:
    """One AP-originated group-addressed packet and its delivery state."""

    sequence: int
    access_category: str
    created_at: float
    members: tuple[str, ...]
    data_attempts: int = 0
    received_by: set[str] = field(default_factory=set)
    acknowledged_by: set[str] = field(default_factory=set)
    finalized: bool = False
    first_transmit_at: float | None = None
    finalization_at: float | None = None


@dataclass(order=True)
class Event:
    """A time-ordered simulation event stored in a min-heap."""

    time: float
    order: int
    kind: str = field(compare=False)
    packet: GroupPacket | None = field(default=None, compare=False)
    member_name: str | None = field(default=None, compare=False)


@dataclass(frozen=True)
class FrameRecord:
    """An observable medium transmission used for reporting and tests."""

    frame_type: FrameType
    source: str
    destination: str
    packet_sequence: int
    sent_at: float
    arrived_at: float


@dataclass
class Station:
    """A generic group member with no traffic type permanently assigned."""

    name: str
    unique_packets_received: int = 0
    duplicate_data_frames_ignored: int = 0
    data_frame_misses: int = 0
    unrecovered_packets: int = 0
    block_acks_sent: int = 0
    category_deliveries: dict[str, int] = field(
        default_factory=lambda: {category: 0 for category in ACCESS_CATEGORIES}
    )


@dataclass
class AccessPoint:
    """AP queue state. The AP is the only source of group-addressed traffic."""

    queues: dict[str, deque[GroupPacket]] = field(
        default_factory=lambda: {
            category: deque() for category in ACCESS_CATEGORIES
        }
    )
    active_packet: GroupPacket | None = None


@dataclass(frozen=True)
class SimulationSummary:
    """Compact mode result used by the all-modes comparison report."""

    mode: GroupcastMode
    generated_packets: int
    complete_packets: int
    partial_packets: int
    receiver_delivery_ratio: float
    group_data_frames: int
    block_ack_frames: int
    retransmissions: int


class Lightweight80211aaSimulation:
    """
    A discrete-event comparison model for 802.11aa group-addressed delivery.

    The model intentionally isolates MAC-level reliability differences:
    - legacy multicast: one group DATA frame and no feedback
    - GCR unsolicited retry: a fixed number of extra group DATA copies
    - GCR Block Ack: receiver feedback identifies members still missing DATA

    It has no PHY calculation. Optional receiver misses are deterministic test
    inputs, independent of distance, rate, interference, or signal quality.
    """

    def __init__(
        self,
        mode: GroupcastMode | str,
        duration_s: float = 1.0,
        packet_interval_s: float = 0.02,
        seed: int = 7,
        minimum_group_packets: int = DEFAULT_MINIMUM_GROUP_PACKETS,
        gcr_retries: int = DEFAULT_GCR_RETRIES,
        receiver_miss_probability: float = DEFAULT_RECEIVER_MISS_PROBABILITY,
    ) -> None:
        self.mode = GroupcastMode(mode)

        if duration_s <= 0:
            raise ValueError("duration must be greater than zero")
        if packet_interval_s <= 0:
            raise ValueError("packet interval must be greater than zero")
        if minimum_group_packets < DEFAULT_MINIMUM_GROUP_PACKETS:
            raise ValueError(
                "minimum group packets must be at least "
                f"{DEFAULT_MINIMUM_GROUP_PACKETS}"
            )
        if gcr_retries < 0:
            raise ValueError("gcr retries cannot be negative")
        if not 0 <= receiver_miss_probability < 1:
            raise ValueError("receiver miss probability must be in [0, 1)")

        self.duration_s = duration_s
        self.packet_interval_s = packet_interval_s
        self.seed = seed
        self.minimum_group_packets = minimum_group_packets
        self.gcr_retries = gcr_retries
        self.receiver_miss_probability = receiver_miss_probability
        self.backoff_random = random.Random(seed)

        # The AP and all four stations are fixed. Every station is exactly one
        # MEDIUM_DELAY_S away from the shared medium for every transmission.
        self.ap = AccessPoint()
        self.stations = {name: Station(name) for name in GROUP_MEMBERS}

        self.events: list[Event] = []
        self.event_order = 0
        self.packet_sequence = 0
        self.now = 0.0
        self.medium_busy_until = 0.0
        self.frame_log: list[FrameRecord] = []
        self.completed_packets: list[GroupPacket] = []

        self.group_packets_generated = 0
        self.group_packets_complete = 0
        self.group_packets_partial = 0
        self.group_data_frames_sent = 0
        self.block_ack_frames_sent = 0
        self.gcr_unsolicited_retries = 0
        self.gcr_block_ack_retries = 0

    def schedule(
        self,
        time: float,
        kind: str,
        packet: GroupPacket | None = None,
        member_name: str | None = None,
    ) -> None:
        """Push an event into the chronological event queue."""
        self.event_order += 1
        heapq.heappush(
            self.events,
            Event(time, self.event_order, kind, packet, member_name),
        )

    def start(self) -> None:
        """Generate AP group traffic, then drain all queued MAC work."""
        self.schedule(0.0, "generate-group-packet")
        self.run()

    def run(self) -> None:
        """Process events until all packets and feedback windows are complete."""
        while self.events:
            event = heapq.heappop(self.events)
            self.now = event.time

            if event.kind == "generate-group-packet":
                self.handle_generate_group_packet()
            elif event.kind == "transmit-group-data":
                self.handle_transmit_group_data(event)
            elif event.kind == "receive-group-data":
                self.handle_receive_group_data(event)
            elif event.kind == "receive-block-ack":
                self.handle_receive_block_ack(event)
            elif event.kind == "block-ack-window-complete":
                self.handle_block_ack_window_complete(event)
            else:
                raise RuntimeError(f"Unknown event kind: {event.kind}")

    def handle_generate_group_packet(self) -> None:
        """Create one generic AP group packet with a deterministic random AC."""
        # The stream is extended only when necessary to satisfy the requested
        # minimum packet count; otherwise generation ends at duration_s.
        if (
            self.now > self.duration_s
            and self.group_packets_generated >= self.minimum_group_packets
        ):
            return

        self.packet_sequence += 1
        category_name = self.category_for_sequence(self.packet_sequence)
        packet = GroupPacket(
            sequence=self.packet_sequence,
            access_category=category_name,
            created_at=self.now,
            members=GROUP_MEMBERS,
        )
        self.ap.queues[category_name].append(packet)
        self.group_packets_generated += 1
        self.begin_edca_access(self.now)

        next_generation = self.now + self.packet_interval_s
        if (
            next_generation <= self.duration_s
            or self.group_packets_generated < self.minimum_group_packets
        ):
            self.schedule(next_generation, "generate-group-packet")

    def begin_edca_access(self, earliest_start_s: float) -> None:
        """Schedule the AP's next group frame using an EDCA-style queue choice."""
        if self.ap.active_packet is not None:
            return

        packet = self.dequeue_highest_priority_packet()
        if packet is None:
            return

        category = ACCESS_CATEGORIES[packet.access_category]
        # This single-AP model does not attempt to emulate full independent-AC
        # virtual collisions. AIFS and CWmin are retained to make traffic class
        # scheduling visible without introducing a PHY or competing AP model.
        backoff_slots = self.backoff_random.randint(0, category.cw_min)
        contention_delay = (category.aifs_slots + backoff_slots) * SLOT_TIME_S
        send_time = max(earliest_start_s, self.medium_busy_until) + contention_delay

        self.ap.active_packet = packet
        self.schedule(send_time, "transmit-group-data", packet)

    def dequeue_highest_priority_packet(self) -> GroupPacket | None:
        """Return the first packet from the highest-priority non-empty queue."""
        categories = sorted(
            ACCESS_CATEGORIES.values(),
            key=lambda category: category.priority,
            reverse=True,
        )
        for category in categories:
            queue = self.ap.queues[category.name]
            if queue:
                return queue.popleft()
        return None

    def handle_transmit_group_data(self, event: Event) -> None:
        """Send one group-addressed DATA copy from the AP to all members."""
        packet = event.packet
        if packet is None or packet is not self.ap.active_packet or packet.finalized:
            return

        # Retries are scheduled after the previous frame's reservation, but
        # this guard keeps the medium invariant explicit for future extensions.
        if self.now < self.medium_busy_until:
            self.schedule(self.medium_busy_until, "transmit-group-data", packet)
            return

        packet.data_attempts += 1
        if packet.first_transmit_at is None:
            packet.first_transmit_at = self.now

        self.group_data_frames_sent += 1
        arrival_time = self.now + MEDIUM_DELAY_S
        self.medium_busy_until = self.now + GROUP_DATA_AIRTIME_S + MEDIUM_DELAY_S
        self.frame_log.append(
            FrameRecord(
                frame_type=FrameType.GROUP_DATA,
                source="AP",
                destination="GROUP",
                packet_sequence=packet.sequence,
                sent_at=self.now,
                arrived_at=arrival_time,
            )
        )
        self.schedule(arrival_time, "receive-group-data", packet)

    def handle_receive_group_data(self, event: Event) -> None:
        """Apply a group DATA copy to every member after the fixed 5 ms delay."""
        packet = event.packet
        if packet is None or packet.finalized:
            return

        for member_name in packet.members:
            station = self.stations[member_name]
            if self.receiver_receives(packet, member_name):
                if member_name in packet.received_by:
                    # GCR retries are group-addressed copies. A receiver that
                    # already accepted this sequence ignores a later duplicate.
                    station.duplicate_data_frames_ignored += 1
                else:
                    packet.received_by.add(member_name)
                    station.unique_packets_received += 1
                    station.category_deliveries[packet.access_category] += 1
            else:
                station.data_frame_misses += 1

        if self.mode is GroupcastMode.LEGACY:
            # Legacy multicast does not collect receiver feedback and has no
            # group-reliability retry decision.
            self.finalize_packet(packet)
        elif self.mode is GroupcastMode.GCR_UNSOLICITED_RETRY:
            self.handle_unsolicited_retry(packet)
        else:
            self.schedule_block_ack_window(packet)

    def handle_unsolicited_retry(self, packet: GroupPacket) -> None:
        """Repeat DATA a configured number of times without receiver feedback."""
        if packet.data_attempts < self.gcr_retries + 1:
            self.gcr_unsolicited_retries += 1
            retry_time = max(self.now, self.medium_busy_until) + SIFS_S
            self.schedule(retry_time, "transmit-group-data", packet)
            return

        # Unlike GCR Block Ack, the AP does not know who missed a packet here.
        # It stops only after sending the configured number of copies.
        self.finalize_packet(packet)

    def schedule_block_ack_window(self, packet: GroupPacket) -> None:
        """
        Schedule lightweight per-member Block Ack feedback and aggregate it.

        Real 802.11aa GCR Block Ack has detailed control-frame and membership
        rules. This model represents its key property: the AP learns which
        group members received the frame, then retries only when feedback says
        that at least one receiver is still missing the sequence.
        """
        # Reserve one serialized feedback opportunity for every member that
        # has not already acknowledged this packet. A receiver that still
        # misses the DATA uses a silent slot; the AP must wait through that
        # slot rather than gaining impossible immediate knowledge of a miss.
        awaiting_feedback = sorted(set(packet.members) - packet.acknowledged_by)
        if not awaiting_feedback:
            # Defensive guard: a complete acknowledgement set normally causes
            # finalization before another feedback window is requested.
            self.schedule(
                max(self.now, self.medium_busy_until),
                "block-ack-window-complete",
                packet,
            )
            return

        next_slot_time = max(self.now, self.medium_busy_until) + SIFS_S
        # Initialized for the type checker; the non-empty guard above means
        # the loop always replaces it with the final feedback-slot end time.
        slot_end_time = next_slot_time
        for member_name in awaiting_feedback:
            if member_name in packet.received_by:
                arrival_time = next_slot_time + MEDIUM_DELAY_S
                self.block_ack_frames_sent += 1
                self.frame_log.append(
                    FrameRecord(
                        frame_type=FrameType.BLOCK_ACK,
                        source=member_name,
                        destination="AP",
                        packet_sequence=packet.sequence,
                        sent_at=next_slot_time,
                        arrived_at=arrival_time,
                    )
                )
                self.schedule(
                    arrival_time,
                    "receive-block-ack",
                    packet,
                    member_name,
                )

            # Feedback slots are serialized to avoid adding a hidden PHY
            # collision model. An actual Block Ack still crosses the fixed
            # 5 ms delay; a silent slot simply reserves the feedback window.
            slot_end_time = (
                next_slot_time + BLOCK_ACK_AIRTIME_S + MEDIUM_DELAY_S
            )
            next_slot_time = slot_end_time + SIFS_S

        # This event runs after every expected feedback opportunity, including
        # silent slots from members that did not receive the group DATA copy.
        self.medium_busy_until = max(self.medium_busy_until, slot_end_time)
        self.schedule(slot_end_time, "block-ack-window-complete", packet)

    def handle_receive_block_ack(self, event: Event) -> None:
        """Record the AP's receipt of one member's Block Ack feedback."""
        packet = event.packet
        member_name = event.member_name
        if packet is None or member_name is None or packet.finalized:
            return

        # Only stations that received the sequence can report it as received.
        if member_name in packet.received_by:
            packet.acknowledged_by.add(member_name)
            self.stations[member_name].block_acks_sent += 1

    def handle_block_ack_window_complete(self, event: Event) -> None:
        """Use aggregated Block Ack state to complete or retry a group packet."""
        packet = event.packet
        if packet is None or packet.finalized:
            return

        missing_members = set(packet.members) - packet.acknowledged_by
        if not missing_members:
            self.finalize_packet(packet)
            return

        if packet.data_attempts < self.gcr_retries + 1:
            # Feedback-driven retry is the key GCR Block Ack difference: the
            # AP retransmits only because feedback identifies missing members.
            self.gcr_block_ack_retries += 1
            retry_time = max(self.now, self.medium_busy_until) + SIFS_S
            self.schedule(retry_time, "transmit-group-data", packet)
            return

        self.finalize_packet(packet)

    def finalize_packet(self, packet: GroupPacket) -> None:
        """Close a group packet and start EDCA access for the next AP packet."""
        if packet.finalized:
            return

        packet.finalized = True
        packet.finalization_at = self.now
        self.completed_packets.append(packet)
        if set(packet.members) == packet.received_by:
            self.group_packets_complete += 1
        else:
            self.group_packets_partial += 1
            for member_name in set(packet.members) - packet.received_by:
                self.stations[member_name].unrecovered_packets += 1

        if packet is self.ap.active_packet:
            self.ap.active_packet = None
        self.begin_edca_access(max(self.now, self.medium_busy_until))

    def category_for_sequence(self, sequence: int) -> str:
        """Choose a repeatable pseudo-random category shared across modes."""
        index = self.stable_integer(f"category:{sequence}") % len(
            ACCESS_CATEGORY_POOL
        )
        return ACCESS_CATEGORY_POOL[index]

    def receiver_receives(self, packet: GroupPacket, member_name: str) -> bool:
        """
        Return an abstract deterministic receiver outcome for this DATA copy.

        This is deliberately not a PHY model. The value depends only on the
        seed, packet sequence, receiver identity, and copy number, allowing
        legacy, GCR-UR, and GCR-BA runs to be compared under equivalent input.
        """
        if self.receiver_miss_probability == 0:
            return True

        sample = self.stable_fraction(
            f"receiver:{packet.sequence}:{member_name}:{packet.data_attempts}"
        )
        return sample >= self.receiver_miss_probability

    def stable_integer(self, value: str) -> int:
        """Create a process-independent deterministic value from the seed."""
        digest = hashlib.sha256(f"{self.seed}:{value}".encode()).digest()
        return int.from_bytes(digest[:8], "big")

    def stable_fraction(self, value: str) -> float:
        """Map stable_integer to the half-open interval [0, 1)."""
        return self.stable_integer(value) / 2**64

    def receiver_delivery_ratio(self) -> float:
        """Return unique packet deliveries divided by all group destinations."""
        possible_deliveries = self.group_packets_generated * len(GROUP_MEMBERS)
        if possible_deliveries == 0:
            return 0.0
        delivered = sum(
            station.unique_packets_received for station in self.stations.values()
        )
        return delivered / possible_deliveries

    def retransmission_count(self) -> int:
        """Return retransmissions applicable to the active groupcast mode."""
        return self.gcr_unsolicited_retries + self.gcr_block_ack_retries

    def summary(self) -> SimulationSummary:
        """Produce a mode-neutral result for side-by-side comparison."""
        return SimulationSummary(
            mode=self.mode,
            generated_packets=self.group_packets_generated,
            complete_packets=self.group_packets_complete,
            partial_packets=self.group_packets_partial,
            receiver_delivery_ratio=self.receiver_delivery_ratio(),
            group_data_frames=self.group_data_frames_sent,
            block_ack_frames=self.block_ack_frames_sent,
            retransmissions=self.retransmission_count(),
        )

    def report(self) -> None:
        """Print detailed mode results, including per-station group outcomes."""
        summary = self.summary()
        print("Lightweight IEEE 802.11aa groupcast simulation")
        print("================================================")
        print(f"Mode: {mode_label(self.mode)}")
        print(f"Fixed topology: AP -> {', '.join(GROUP_MEMBERS)}")
        print(f"Fixed one-way medium delay: {MEDIUM_DELAY_S * 1000:.3f} ms")
        print(
            "Abstract receiver miss probability: "
            f"{self.receiver_miss_probability:.1%} (not PHY-derived)"
        )
        print(f"Group packets generated: {summary.generated_packets}")
        print(f"Complete group deliveries: {summary.complete_packets}")
        print(f"Partial group deliveries: {summary.partial_packets}")
        print(f"Unique receiver delivery ratio: {summary.receiver_delivery_ratio:.1%}")
        print(f"Group DATA frames sent: {summary.group_data_frames}")
        print(f"Block Ack feedback frames sent: {summary.block_ack_frames}")
        print(f"Group retransmissions: {summary.retransmissions}")
        print(f"Completion time after queue drain: {self.now * 1000:.3f} ms")
        print("\nPer-station group results:")

        for station in self.stations.values():
            traffic_mix = ", ".join(
                f"{category}={station.category_deliveries[category]}"
                for category in ACCESS_CATEGORIES
            )
            print(
                f"  {station.name}: unique={station.unique_packets_received}, "
                f"unrecovered={station.unrecovered_packets}, "
                f"duplicates_ignored={station.duplicate_data_frames_ignored}, "
                f"data_misses={station.data_frame_misses}, "
                f"block_acks={station.block_acks_sent}"
            )
            print(f"    delivered categories: [{traffic_mix}]")


def mode_label(mode: GroupcastMode) -> str:
    """Return a human-readable label for reports and comparisons."""
    labels = {
        GroupcastMode.LEGACY: "Legacy multicast (no feedback, one DATA copy)",
        GroupcastMode.GCR_UNSOLICITED_RETRY: (
            "GCR unsolicited retry (fixed DATA copies, no feedback)"
        ),
        GroupcastMode.GCR_BLOCK_ACK: (
            "GCR Block Ack (feedback-driven group retries)"
        ),
    }
    return labels[mode]


def run_comparison(args: argparse.Namespace) -> None:
    """Run every groupcast mode under equivalent deterministic traffic inputs."""
    summaries: list[SimulationSummary] = []
    for mode in GroupcastMode:
        simulation = Lightweight80211aaSimulation(
            mode=mode,
            duration_s=args.duration,
            packet_interval_s=args.interval,
            seed=args.seed,
            minimum_group_packets=args.minimum_group_packets,
            gcr_retries=args.gcr_retries,
            receiver_miss_probability=args.receiver_miss_probability,
        )
        simulation.start()
        summaries.append(simulation.summary())

    print("802.11aa groupcast mode comparison")
    print("=================================")
    print(
        "All modes use identical stream categories and deterministic receiver "
        "outcomes for each packet/member/copy combination."
    )
    print(
        "\n{:<17} {:>7} {:>8} {:>8} {:>10} {:>7} {:>7} {:>8}".format(
            "Mode",
            "Packets",
            "Complete",
            "Partial",
            "Delivery",
            "DATA",
            "BA",
            "Retries",
        )
    )
    for summary in summaries:
        print(
            f"{summary.mode.value:<17} "
            f"{summary.generated_packets:>7} "
            f"{summary.complete_packets:>8} "
            f"{summary.partial_packets:>8} "
            f"{summary.receiver_delivery_ratio:>9.1%} "
            f"{summary.group_data_frames:>7} "
            f"{summary.block_ack_frames:>7} "
            f"{summary.retransmissions:>8}"
        )


def parse_args() -> argparse.Namespace:
    """Parse simulation parameters; no PHY or distance parameters are exposed."""
    parser = argparse.ArgumentParser(
        description=(
            "Compare lightweight IEEE 802.11aa groupcast reliability modes "
            "with a fixed 5 ms one-way medium delay."
        )
    )
    parser.add_argument(
        "--mode",
        choices=[mode.value for mode in GroupcastMode] + ["all"],
        default="all",
        help="groupcast mode to run; default: all",
    )
    parser.add_argument("--duration", type=float, default=1.0)
    parser.add_argument("--interval", type=float, default=0.02)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument(
        "--minimum-group-packets",
        type=int,
        default=DEFAULT_MINIMUM_GROUP_PACKETS,
        help="minimum AP group packets generated, at least 5 (default: 5)",
    )
    parser.add_argument(
        "--gcr-retries",
        type=int,
        default=DEFAULT_GCR_RETRIES,
        help="extra group DATA copies allowed by GCR modes (default: 2)",
    )
    parser.add_argument(
        "--receiver-miss-probability",
        type=float,
        default=DEFAULT_RECEIVER_MISS_PROBABILITY,
        help=(
            "abstract deterministic per-copy receiver miss probability, "
            "not a PHY model (default: 0.20)"
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.mode == "all":
        run_comparison(args)
        return

    simulation = Lightweight80211aaSimulation(
        mode=GroupcastMode(args.mode),
        duration_s=args.duration,
        packet_interval_s=args.interval,
        seed=args.seed,
        minimum_group_packets=args.minimum_group_packets,
        gcr_retries=args.gcr_retries,
        receiver_miss_probability=args.receiver_miss_probability,
    )
    simulation.start()
    simulation.report()


if __name__ == "__main__":
    main()
