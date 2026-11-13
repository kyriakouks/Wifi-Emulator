"""
Paper-oriented IEEE 802.11aa reliable-multicast analytical model.

The module reproduces the paper's saturation-analysis workflow without a
packet-event or physical-layer simulator. ``ModelParameters `` contains Table I
inputs plus explicit values for constants omitted by the publication.
``PaperAnalyticalModel`` solves DCF attempt/collision probabilities and then
computes reliability, multicast throughput, unicast throughput, and LogRD
utility for five multicast mechanisms. The CLI renders those values as tables
or CSV so receiver/unicast sweeps can be inspected without plotting software.

Published equation ambiguities are never hidden: each executable repair is
explained next to the corresponding helper and in ``PAPER-MODEL-GUIDE.md``.
"""

import argparse
import csv
import math
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum


MIN_STATIONS = 1
MAX_STATIONS = 30


class ModelDomainError(ValueError):
    """Raised when inputs leave the valid domain of the paper's equations."""


class Mechanism(str, Enum):
    """Reliable multicast mechanisms evaluated by the referenced paper."""

    LEGACY = "legacy"
    GCR_UR = "gcr-ur"
    DMS = "dms"
    IMMEDIATE_BA = "immediate-ba"
    DELAYED_BA = "delayed-ba"


@dataclass(frozen=True)
class ModelParameters:
    """
    Paper parameters plus explicit 802.11a defaults for omitted MAC timings.

    Table I in the paper defines rates, BERs, retry settings, contention
    windows, payload sizes, and Block Ack burst/lifetime values. It does not
    state slot/SIFS/DIFS/PLCP values or control-frame lengths, so those values
    are exposed here rather than hidden inside the analytical equations.
    """

    # Paper values: 1500-byte multicast and unicast payloads.
    multicast_payload_bytes: int = 1500
    unicast_payload_bytes: int = 1500

    # Paper Table I rate and BER settings.
    legacy_data_rate_mbps: float = 6.0
    high_data_rate_mbps: float = 54.0
    control_rate_mbps: float = 6.0
    legacy_ber: float = 1e-6
    gcr_ber: float = 1e-5
    unicast_ber: float = 0.0

    # Paper Table I retry and aggregation settings.
    ur_retries: int = 2
    dms_retries: int = 8
    unicast_retries: int = 8
    immediate_ba_burst_size: int = 10
    ba_msdu_lifetime_s: float = 0.400

    # Paper Table I contention window sizes/stages. These are window sizes,
    # not IEEE CW field values: W=16 means a 0..15 backoff range.
    multicast_window: int = 16
    multicast_max_stage: int = 0
    unicast_window: int = 32
    unicast_max_stage: int = 5

    # Paper evaluation reliability threshold for LogRD selection.
    reliability_threshold: float = 0.90 # 90% reliability threshold

    # Explicit 802.11aa defaults for values omitted by the paper.
    slot_time_s: float = 9e-6
    sifs_s: float = 16e-6
    difs_s: float = 34e-6
    plcp_time_s: float = 20e-6
    mac_header_fcs_bytes: int = 34
    ack_bytes: int = 14
    block_ack_control_bytes: int = 32

    # Numerical controls, not protocol parameters.
    solver_tolerance: float = 1e-12
    solver_max_iterations: int = 10_000
    solver_damping: float = 0.35

    @property
    def multicast_payload_bits(self) -> int:
        return self.multicast_payload_bytes * 8

    @property
    def unicast_payload_bits(self) -> int:
        return self.unicast_payload_bytes * 8

    @property
    def header_bits(self) -> int:
        return self.mac_header_fcs_bytes * 8

    @property
    def ack_bits(self) -> int:
        return self.ack_bytes * 8

    @property
    def block_ack_control_bits(self) -> int:
        return self.block_ack_control_bytes * 8


@dataclass(frozen=True)
class SlotProbabilities:
    """Probabilities of the five mutually exclusive generic MAC slot events."""

    multicast_success: float
    unicast_success: float
    unicast_collision: float
    multicast_collision: float
    empty: float
    conditional_multicast_collision: float


@dataclass(frozen=True)
class MechanismResult:
    """Paper-oriented reliability, throughput, and selection output."""

    mechanism: Mechanism
    receivers: int
    unicast_stations: int
    reliability: float
    multicast_throughput_mbps: float
    unicast_throughput_mbps: float
    utility_mbps: float
    multicast_attempt_probability: float
    unicast_attempt_probability: float
    multicast_collision_probability: float
    effective_ba_retries: int | None = None


class PaperAnalyticalModel:
    """
    Lightweight implementation of the paper's saturation analysis.

    No path loss, SNR, fading, or waveform simulation is performed. PHY input
    is reduced to payload/control airtime and BER-to-FER conversion. Saturated
    DCF contenders and their collision probabilities are solved analytically.
    """

    def __init__(self, parameters: ModelParameters | None = None) -> None:
        self.p = parameters or ModelParameters()
        self.validate_parameters()

    def validate_parameters(self) -> None:
        """Reject configurations that make the paper's equations undefined."""
        positive_integer_fields = {
            "multicast_payload_bytes": self.p.multicast_payload_bytes,
            "unicast_payload_bytes": self.p.unicast_payload_bytes,
            "immediate_ba_burst_size": self.p.immediate_ba_burst_size,
            "multicast_window": self.p.multicast_window,
            "unicast_window": self.p.unicast_window,
            "solver_max_iterations": self.p.solver_max_iterations,
        }
        for name, value in positive_integer_fields.items():
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")

        nonnegative_integer_fields = {
            "ur_retries": self.p.ur_retries,
            "dms_retries": self.p.dms_retries,
            "unicast_retries": self.p.unicast_retries,
            "multicast_max_stage": self.p.multicast_max_stage,
            "unicast_max_stage": self.p.unicast_max_stage,
            "mac_header_fcs_bytes": self.p.mac_header_fcs_bytes,
            "ack_bytes": self.p.ack_bytes,
            "block_ack_control_bytes": self.p.block_ack_control_bytes,
        }
        for name, value in nonnegative_integer_fields.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")

        if self.p.unicast_retries < self.p.unicast_max_stage:
            raise ValueError("unicast_retries must be >= unicast_max_stage")
        if self.p.dms_retries < self.p.multicast_max_stage:
            raise ValueError("dms_retries must be >= multicast_max_stage")

        positive_float_fields = {
            "legacy_data_rate_mbps": self.p.legacy_data_rate_mbps,
            "high_data_rate_mbps": self.p.high_data_rate_mbps,
            "control_rate_mbps": self.p.control_rate_mbps,
            "ba_msdu_lifetime_s": self.p.ba_msdu_lifetime_s,
            "slot_time_s": self.p.slot_time_s,
            "plcp_time_s": self.p.plcp_time_s,
            "solver_tolerance": self.p.solver_tolerance,
        }
        for name, value in positive_float_fields.items():
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")

        nonnegative_float_fields = {
            "sifs_s": self.p.sifs_s,
            "difs_s": self.p.difs_s,
        }
        for name, value in nonnegative_float_fields.items():
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")

        for name, ber in {
            "legacy_ber": self.p.legacy_ber,
            "gcr_ber": self.p.gcr_ber,
            "unicast_ber": self.p.unicast_ber,
        }.items():
            if not math.isfinite(ber) or not 0 <= ber < 1:
                raise ValueError(f"{name} must be finite and in [0, 1)")

        if (
            not math.isfinite(self.p.reliability_threshold)
            or not 0 <= self.p.reliability_threshold <= 1
        ):
            raise ValueError("reliability_threshold must be finite and in [0, 1]")
        if not math.isfinite(self.p.solver_damping) or not 0 < self.p.solver_damping <= 1:
            raise ValueError("solver_damping must be finite and in (0, 1]")

    def evaluate(
        self,
        mechanism: Mechanism | str,
        receivers: int,
        unicast_stations: int,
    ) -> MechanismResult:
        """Evaluate one mechanism for one saturated network configuration."""
        mechanism = Mechanism(mechanism)
        if not MIN_STATIONS <= receivers <= MAX_STATIONS:
            raise ModelDomainError(
                f"receivers must be in the paper's evaluated range "
                f"[{MIN_STATIONS}, {MAX_STATIONS}]"
            )
        if not MIN_STATIONS <= unicast_stations <= MAX_STATIONS:
            raise ModelDomainError(
                f"unicast stations must be in the paper's evaluated range "
                f"[{MIN_STATIONS}, {MAX_STATIONS}]"
            )

        if mechanism is Mechanism.LEGACY:
            result = self.evaluate_legacy(receivers, unicast_stations)
        elif mechanism is Mechanism.GCR_UR:
            result = self.evaluate_ur(receivers, unicast_stations)
        elif mechanism is Mechanism.DMS:
            result = self.evaluate_dms(receivers, unicast_stations)
        elif mechanism is Mechanism.IMMEDIATE_BA:
            result = self.evaluate_immediate_ba(receivers, unicast_stations)
        else:
            result = self.evaluate_delayed_ba(receivers, unicast_stations)

        return result

    def evaluate_all(
        self,
        receivers: int,
        unicast_stations: int,
    ) -> list[MechanismResult]:
        """Evaluate every paper mechanism under the same network conditions."""
        return [
            self.evaluate(mechanism, receivers, unicast_stations)
            for mechanism in Mechanism
        ]

    def select_best(
        self, results: Iterable[MechanismResult]
    ) -> MechanismResult | None:
        """Return the best reliable mechanism, or ``None`` if none is feasible."""
        candidates = [
            result
            for result in results
            if result.reliability >= self.p.reliability_threshold
        ]
        if not candidates:
            return None

        # Input order is the deterministic tie-break rule; the paper defines
        # argmax utility but does not specify how exact ties should be handled.
        return max(candidates, key=lambda result: result.utility_mbps)

    def evaluate_legacy(self, receivers: int, unicast_stations: int) -> MechanismResult:
        t_m, t_u = self.solve_attempt_probabilities(
            Mechanism.LEGACY, receivers, unicast_stations
        )
        probabilities = self.slot_probabilities(t_m, t_u, unicast_stations)
        p_fe = self.frame_error_rate(
            self.p.legacy_ber, self.p.multicast_payload_bits
        )

        reliability = (
            1 - probabilities.conditional_multicast_collision
        ) * (1 - p_fe)
        multicast_time = self.multicast_data_time(
            self.p.legacy_data_rate_mbps, acknowledged=False
        )
        slot_time = self.generic_slot_time(probabilities, multicast_time, multicast_time)

        multicast_throughput = (
            probabilities.multicast_success
            * self.p.multicast_payload_bits
            * (1 - p_fe)
            / slot_time
        )
        unicast_throughput = self.unicast_throughput(probabilities, slot_time)
        return self.result(
            Mechanism.LEGACY,
            receivers,
            unicast_stations,
            reliability,
            multicast_throughput,
            unicast_throughput,
            t_m,
            t_u,
            probabilities.conditional_multicast_collision,
        )

    def evaluate_ur(self, receivers: int, unicast_stations: int) -> MechanismResult:
        t_m, t_u = self.solve_attempt_probabilities(
            Mechanism.GCR_UR, receivers, unicast_stations
        )
        probabilities = self.slot_probabilities(t_m, t_u, unicast_stations)
        p_fe = self.frame_error_rate(self.p.gcr_ber, self.p.multicast_payload_bits)
        attempts = self.p.ur_retries + 1
        success_per_copy = (
            1 - probabilities.conditional_multicast_collision
        ) * (1 - p_fe)
        reliability = 1 - (1 - success_per_copy) ** attempts

        multicast_time = self.multicast_data_time(
            self.p.high_data_rate_mbps, acknowledged=False
        )
        slot_time = self.generic_slot_time(probabilities, multicast_time, multicast_time)
        multicast_throughput = (
            t_m
            * self.p.multicast_payload_bits
            * reliability
            / (attempts * slot_time)
        )
        unicast_throughput = self.unicast_throughput(probabilities, slot_time)
        return self.result(
            Mechanism.GCR_UR,
            receivers,
            unicast_stations,
            reliability,
            multicast_throughput,
            unicast_throughput,
            t_m,
            t_u,
            probabilities.conditional_multicast_collision,
        )

    def evaluate_dms(self, receivers: int, unicast_stations: int) -> MechanismResult:
        t_m, t_u = self.solve_attempt_probabilities(
            Mechanism.DMS, receivers, unicast_stations
        )
        probabilities = self.slot_probabilities(t_m, t_u, unicast_stations)
        p_fe = self.frame_error_rate(self.p.gcr_ber, self.p.multicast_payload_bits)
        per_attempt_success = (
            1 - probabilities.conditional_multicast_collision
        ) * (1 - p_fe)
        attempts_allowed = self.p.dms_retries + 1
        reliability = 1 - (1 - per_attempt_success) ** attempts_allowed

        t_m_i = self.dms_per_receiver_attempt_probabilities(
            receivers,
            probabilities.conditional_multicast_collision,
            p_fe,
        )
        # DMS converts group traffic into acknowledged unicast frames. The
        # paper's prose requires ACKs, though its timing reference is ambiguous;
        # this executable interpretation includes ACK airtime.
        multicast_success_time = self.multicast_data_time(
            self.p.high_data_rate_mbps, acknowledged=True
        )
        multicast_collision_time = self.data_collision_time(
            self.p.multicast_payload_bits, self.p.high_data_rate_mbps
        )
        slot_time = self.generic_slot_time(
            probabilities,
            multicast_success_time,
            multicast_collision_time,
        )
        successful_payload_rate = sum(
            tau_i
            * self.p.multicast_payload_bits
            * (1 - probabilities.conditional_multicast_collision)
            * (1 - p_fe)
            for tau_i in t_m_i
        )
        multicast_throughput = successful_payload_rate / (receivers * slot_time)
        unicast_throughput = self.unicast_throughput(probabilities, slot_time)
        return self.result(
            Mechanism.DMS,
            receivers,
            unicast_stations,
            reliability,
            multicast_throughput,
            unicast_throughput,
            t_m,
            t_u,
            probabilities.conditional_multicast_collision,
        )

    def evaluate_immediate_ba(
        self,
        receivers: int,
        unicast_stations: int,
    ) -> MechanismResult:
        t_m, t_u = self.solve_attempt_probabilities(
            Mechanism.IMMEDIATE_BA, receivers, unicast_stations
        )
        probabilities = self.slot_probabilities(t_m, t_u, unicast_stations)
        burst_time = self.immediate_ba_burst_time(receivers)
        slot_time = self.generic_slot_time(probabilities, burst_time, burst_time)
        # The paper's lifetime interval excludes the target multicast burst
        # from T_slot,m, then adds that burst once. Using the full slot time
        # here would count every multicast burst twice.
        non_multicast_slot_time = self.non_multicast_slot_time(probabilities)
        effective_retries = self.effective_ba_retries(
            attempt_probability=t_m,
            non_attempt_slot_time=non_multicast_slot_time,
            attempt_duration=burst_time,
        )
        p_fe = self.frame_error_rate(self.p.gcr_ber, self.p.multicast_payload_bits)
        common_collision = self.ba_common_collision_probability(
            probabilities.conditional_multicast_collision
        )
        p_success = (1 - p_fe) * (1 - common_collision)
        reliability = 1 - (1 - p_success) ** (effective_retries + 1)
        expected_attempts = self.expected_group_attempts(
            receiver_frame_error_rates=[p_fe] * receivers,
            common_collision_probability=common_collision,
            maximum_attempts=effective_retries + 1,
        )

        multicast_throughput = (
            t_m
            * self.p.multicast_payload_bits
            / slot_time
            * (self.p.immediate_ba_burst_size / expected_attempts)
            * reliability
        )
        unicast_throughput = self.unicast_throughput(probabilities, slot_time)
        return self.result(
            Mechanism.IMMEDIATE_BA,
            receivers,
            unicast_stations,
            reliability,
            multicast_throughput,
            unicast_throughput,
            t_m,
            t_u,
            probabilities.conditional_multicast_collision,
            effective_retries,
        )

    def evaluate_delayed_ba(
        self,
        receivers: int,
        unicast_stations: int,
    ) -> MechanismResult:
        t_m, t_u = self.solve_attempt_probabilities(
            Mechanism.DELAYED_BA, receivers, unicast_stations
        )
        probabilities = self.slot_probabilities(t_m, t_u, unicast_stations)
        p_cm = probabilities.conditional_multicast_collision
        p_burst = 1 / (1 + (2 * receivers - 1) / max(1 - p_cm, 1e-15))
        burst_frames_time = self.delayed_ba_frames_time()
        block_ack_exchange_time = self.delayed_ba_control_exchange_time()
        # Eq. (48) can be separated into DATA-burst time and all other
        # unicast/control/empty-slot activity. That separation is also needed
        # by the MSDU-lifetime retry approximation to avoid double counting.
        non_burst_slot_time = (
            probabilities.unicast_success * self.successful_unicast_time()
            + probabilities.unicast_collision * self.unicast_collision_time()
            + t_m * (1 - p_burst) * (1 - p_cm) * block_ack_exchange_time
            + probabilities.empty * self.p.slot_time_s
            + t_m * (1 - p_burst) * p_cm * self.unicast_collision_time()
        )
        slot_time = non_burst_slot_time + t_m * p_burst * burst_frames_time

        effective_retries = self.effective_ba_retries(
            attempt_probability=t_m * p_burst,
            non_attempt_slot_time=non_burst_slot_time,
            attempt_duration=burst_frames_time,
        )
        p_fe = self.frame_error_rate(self.p.gcr_ber, self.p.multicast_payload_bits)
        common_collision = self.ba_common_collision_probability(p_cm)
        p_success = (1 - p_fe) * (1 - common_collision)
        reliability = 1 - (1 - p_success) ** (effective_retries + 1)
        expected_attempts = self.expected_group_attempts(
            receiver_frame_error_rates=[p_fe] * receivers,
            common_collision_probability=common_collision,
            maximum_attempts=effective_retries + 1,
        )

        multicast_throughput = (
            t_m
            * p_burst
            * self.p.multicast_payload_bits
            / slot_time
            * (self.p.immediate_ba_burst_size / expected_attempts)
            * reliability
        )
        unicast_throughput = self.unicast_throughput(probabilities, slot_time)
        return self.result(
            Mechanism.DELAYED_BA,
            receivers,
            unicast_stations,
            reliability,
            multicast_throughput,
            unicast_throughput,
            t_m,
            t_u,
            p_cm,
            effective_retries,
        )

    def result(
        self,
        mechanism: Mechanism,
        receivers: int,
        unicast_stations: int,
        reliability: float,
        multicast_throughput_bps: float,
        unicast_throughput_bps: float,
        t_m: float,
        t_u: float,
        p_cm: float,
        effective_ba_retries: int | None = None,
    ) -> MechanismResult:
        reliability = self.clamp_probability(reliability)
        multicast_mbps = max(multicast_throughput_bps, 0.0) / 1e6
        unicast_mbps = max(unicast_throughput_bps, 0.0) / 1e6
        utility = 0.0
        if reliability >= self.p.reliability_threshold:
            utility = min(
                multicast_mbps / (1 + math.log(receivers)),
                unicast_mbps / unicast_stations,
            )

        return MechanismResult(
            mechanism=mechanism,
            receivers=receivers,
            unicast_stations=unicast_stations,
            reliability=reliability,
            multicast_throughput_mbps=multicast_mbps,
            unicast_throughput_mbps=unicast_mbps,
            utility_mbps=utility,
            multicast_attempt_probability=t_m,
            unicast_attempt_probability=t_u,
            multicast_collision_probability=p_cm,
            effective_ba_retries=effective_ba_retries,
        )

    def solve_attempt_probabilities(
        self,
        mechanism: Mechanism,
        receivers: int,
        unicast_stations: int,
    ) -> tuple[float, float]:
        """Solve paper Eqs. (6), (19), (20), and mechanism-specific t_m."""
        t_m = 2 / (self.p.multicast_window + 1)
        t_u = 2 / (self.p.unicast_window + 1)
        p_fe = self.frame_error_rate(self.p.gcr_ber, self.p.multicast_payload_bits)

        for _ in range(self.p.solver_max_iterations):
            unicast_failure = 1 - (
                (1 - t_u) ** (unicast_stations - 1)
                * (1 - t_m)
                * (1 - self.unicast_frame_error_rate())
            )
            next_t_u = self.finite_retry_attempt_probability(
                self.p.unicast_window,
                self.p.unicast_max_stage,
                self.p.unicast_retries,
                unicast_failure,
            )
            p_cm = 1 - (1 - next_t_u) ** unicast_stations

            if mechanism is Mechanism.DMS:
                next_t_m = sum(
                    self.dms_per_receiver_attempt_probabilities(
                        receivers, p_cm, p_fe
                    )
                )
            elif mechanism is Mechanism.DELAYED_BA:
                p_burst = 1 / (
                    1 + (2 * receivers - 1) / max(1 - p_cm, 1e-15)
                )
                denominator = (1 - 2 * p_cm * p_burst) * (1 - p_cm)
                if abs(denominator) < 1e-15:
                    raise ModelDomainError(
                        "Delayed Block Ack Eq. (60) is singular for this scenario"
                    )
                next_t_m = (
                    2 / (self.p.multicast_window + 1)
                    * ((1 - 2 * p_cm) / (1 - 2 * p_cm * p_burst))
                    * ((1 - p_cm * p_burst) / (1 - p_cm))
                )
                # Eq. (60) comes from an infinite-stage stationary series.
                # Returning a clamped value outside its domain would make an
                # invalid analytical result look plausible, so fail explicitly.
                if not math.isfinite(next_t_m) or not 0 < next_t_m < 1:
                    raise ModelDomainError(
                        "Delayed Block Ack Eq. (60) produced a nonphysical "
                        "attempt probability"
                    )
            else:
                next_t_m = 2 / (self.p.multicast_window + 1)

            damp = self.p.solver_damping
            updated_t_u = t_u + damp * (next_t_u - t_u)
            updated_t_m = t_m + damp * (next_t_m - t_m)
            if max(abs(updated_t_u - t_u), abs(updated_t_m - t_m)) < self.p.solver_tolerance:
                return updated_t_m, updated_t_u
            t_u, t_m = updated_t_u, updated_t_m

        raise RuntimeError(
            f"attempt-probability solver did not converge for {mechanism.value}"
        )

    def finite_retry_attempt_probability(
        self,
        window: int,
        maximum_stage: int,
        retry_limit: int,
        failure_probability: float,
    ) -> float:
        """
        Evaluate the paper's finite-retry DCF attempt probability, Eq. (19).

        The closed form's final term includes ``(1 - 2q)``; this follows by
        multiplying the finite retry/backoff series by ``(1 - q)(1 - 2q)``.
        At q=0.5, and at q=1, that transformed expression is indeterminate.
        The mathematically equivalent renewal sum is used only at those
        singular points and in their immediate numerical neighbourhood.
        """
        q = min(max(failure_probability, 0.0), 1.0)
        if abs(1 - 2 * q) < 1e-8 or q == 1.0:
            expected_attempts = 0.0
            expected_backoff_slots = 0.0
            for stage in range(retry_limit + 1):
                reach_probability = q**stage
                stage_window = window * 2 ** min(stage, maximum_stage)
                expected_attempts += reach_probability
                expected_backoff_slots += (
                    reach_probability * (stage_window - 1) / 2
                )
            denominator = expected_attempts + expected_backoff_slots
            probability = expected_attempts / denominator
        else:
            numerator = 2 * (1 - 2 * q) * (1 - q ** (retry_limit + 1))
            first_term = (
                window
                * (1 - (2 * q) ** (maximum_stage + 1))
                * (1 - q)
            )
            second_term = (1 - 2 * q) * (1 - q ** (retry_limit + 1))
            final_term = (
                window
                * 2 ** maximum_stage
                * q ** (maximum_stage + 1)
                * (1 - q ** (retry_limit - maximum_stage))
                * (1 - 2 * q)
            )
            denominator = first_term + second_term + final_term
            probability = numerator / denominator

        if denominator == 0 or not math.isfinite(denominator):
            raise ModelDomainError("invalid finite-retry DCF renewal cycle")
        if not math.isfinite(probability) or not 0 < probability < 1:
            raise ModelDomainError("unicast attempt-probability equation produced a nonphysical value")
        return probability

    def dms_per_receiver_attempt_probabilities(
        self,
        receivers: int,
        p_cm: float,
        p_fe: float,
    ) -> list[float]:
        """Executable interpretation of paper Eqs. (27)–(30)."""
        success = max((1 - p_cm) * (1 - p_fe), 1e-15)
        failure = 1 - success

        # Reaching retry stage k has probability failure**k. Summing those
        # probabilities gives expected attempts; weighting each stage's CW
        # gives Eq. (30)'s expected backoff for arbitrary max-stage settings.
        attempts = sum(
            failure**stage for stage in range(self.p.dms_retries + 1)
        )
        backoff_slots = sum(
            failure**stage
            * (
                self.p.multicast_window
                * 2 ** min(stage, self.p.multicast_max_stage)
                - 1
            )
            / 2
            for stage in range(self.p.dms_retries + 1)
        )
        denominator = receivers * (attempts + backoff_slots)
        return [attempts / denominator] * receivers

    def slot_probabilities(
        self,
        t_m: float,
        t_u: float,
        unicast_stations: int,
    ) -> SlotProbabilities:
        no_unicast = (1 - t_u) ** unicast_stations
        one_unicast = (unicast_stations * t_u * (1 - t_u) ** (unicast_stations - 1))
        p_sm = t_m * no_unicast
        p_su = one_unicast * (1 - t_m)
        p_cu = (1 - t_m) * (1 - no_unicast - one_unicast)
        p_empty = no_unicast * (1 - t_m)
        p_cm_slot = 1 - p_sm - p_su - p_cu - p_empty
        p_cm_conditional = 1 - no_unicast
        return SlotProbabilities(
            multicast_success=self.clamp_probability(p_sm),
            unicast_success=self.clamp_probability(p_su),
            unicast_collision=self.clamp_probability(p_cu),
            multicast_collision=self.clamp_probability(p_cm_slot),
            empty=self.clamp_probability(p_empty),
            conditional_multicast_collision=self.clamp_probability(p_cm_conditional),
        )

    def generic_slot_time(
        self,
        probabilities: SlotProbabilities,
        multicast_success_time: float,
        multicast_collision_time: float,
    ) -> float:
        """Paper Eq. (11)."""
        return (
            probabilities.unicast_success * self.successful_unicast_time()
            + probabilities.unicast_collision * self.unicast_collision_time()
            + probabilities.multicast_success * multicast_success_time
            + probabilities.multicast_collision * multicast_collision_time
            + probabilities.empty * self.p.slot_time_s
        )

    def non_multicast_slot_time(
        self, probabilities: SlotProbabilities
    ) -> float:
        """
        Average generic-slot time contributed by unicast and empty slots.

        Block Ack lifetime conversion uses this quantity as T_slot,m: the time
        accumulated between multicast attempts. Multicast success/collision
        durations are intentionally excluded so the next burst is added once.
        """
        return (
            probabilities.unicast_success * self.successful_unicast_time()
            + probabilities.unicast_collision * self.unicast_collision_time()
            + probabilities.empty * self.p.slot_time_s
        )

    def successful_unicast_time(self) -> float:
        """Paper Eq. (15)."""
        return (
            self.p.plcp_time_s
            + (self.p.header_bits + self.p.unicast_payload_bits)
            / self.rate_bps(self.p.high_data_rate_mbps)
            + self.p.sifs_s
            + self.p.plcp_time_s
            + self.p.ack_bits / self.rate_bps(self.p.control_rate_mbps)
            + self.p.difs_s
        )

    def unicast_collision_time(self) -> float:
        """Paper Eq. (16)."""
        return self.data_collision_time(self.p.unicast_payload_bits, self.p.high_data_rate_mbps)

    def multicast_data_time(self, rate_mbps: float, acknowledged: bool) -> float:
        """Paper Eq. (17), extended with ACK timing for DMS."""
        data_time = (
            self.p.plcp_time_s
            + (self.p.header_bits + self.p.multicast_payload_bits)
            / self.rate_bps(rate_mbps)
        )
        if not acknowledged:
            return data_time + self.p.difs_s
        return (
            data_time
            + self.p.sifs_s
            + self.p.plcp_time_s
            + self.p.ack_bits / self.rate_bps(self.p.control_rate_mbps)
            + self.p.difs_s
        )

    def data_collision_time(self, payload_bits: int, rate_mbps: float) -> float:
        return (
            self.p.plcp_time_s
            + (self.p.header_bits + payload_bits) / self.rate_bps(rate_mbps)
            + self.p.difs_s
        )

    def immediate_ba_burst_time(self, receivers: int) -> float:
        """Paper Eq. (39), using one documented BACK size for BAR and BA."""
        n = self.p.immediate_ba_burst_size
        data_frames = n * (
            self.p.plcp_time_s
            + (self.p.header_bits + self.p.multicast_payload_bits)
            / self.rate_bps(self.p.high_data_rate_mbps)
            + self.p.sifs_s
        )
        feedback = 2 * receivers * (
            self.p.plcp_time_s
            + self.p.block_ack_control_bits
            / self.rate_bps(self.p.control_rate_mbps)
        )
        return (
            data_frames
            + feedback
            + (2 * receivers - 1) * self.p.sifs_s
            + self.p.difs_s
        )

    def delayed_ba_frames_time(self) -> float:
        """Paper Eq. (49)."""
        n = self.p.immediate_ba_burst_size
        return (
            n * (
                self.p.plcp_time_s
                + (self.p.header_bits + self.p.multicast_payload_bits)
                / self.rate_bps(self.p.high_data_rate_mbps)
                + self.p.sifs_s
            )
            + self.p.difs_s
            - self.p.sifs_s
        )

    def delayed_ba_control_exchange_time(self) -> float:
        """Paper Eq. (50)."""
        return (
            self.p.plcp_time_s
            + self.p.block_ack_control_bits
            / self.rate_bps(self.p.control_rate_mbps)
            + self.p.sifs_s
            + self.p.plcp_time_s
            + self.p.ack_bits / self.rate_bps(self.p.control_rate_mbps)
            + self.p.difs_s
        )

    def ba_common_collision_probability(self, p_cm: float) -> float:
        """Return q=(N'/N)*p_cm from paper Eqs. (33)–(35)."""
        n = self.p.immediate_ba_burst_size
        unicast_frame_time = (
            self.p.plcp_time_s
            + (self.p.header_bits + self.p.unicast_payload_bits)
            / self.rate_bps(self.p.high_data_rate_mbps)
        )
        multicast_frame_time = (
            self.p.plcp_time_s
            + (self.p.header_bits + self.p.multicast_payload_bits)
            / self.rate_bps(self.p.high_data_rate_mbps)
            + self.p.sifs_s
        )
        affected_frames = math.ceil(unicast_frame_time / multicast_frame_time)
        affected_frames = min(max(affected_frames, 0), n)
        return self.clamp_probability(affected_frames / n * p_cm)

    def effective_ba_retries(
        self,
        attempt_probability: float,
        non_attempt_slot_time: float,
        attempt_duration: float,
    ) -> int:
        """
        Convert the 400 ms MSDU lifetime into the paper's effective retry R.

        ``non_attempt_slot_time / attempt_probability`` estimates time spent
        waiting between target attempts. The target burst duration is then
        added exactly once. The paper does not specify rounding, so floor is
        used conservatively and no undocumented retry cap is applied.
        """
        if not 0 < attempt_probability < 1:
            raise ModelDomainError("Block Ack attempt probability must be in (0, 1)")
        interval = non_attempt_slot_time / attempt_probability + attempt_duration
        if not math.isfinite(interval) or interval <= 0:
            raise ModelDomainError("Block Ack retry interval must be positive")
        return max(math.floor(self.p.ba_msdu_lifetime_s / interval), 0)

    @staticmethod
    def expected_group_attempts(
        receiver_frame_error_rates: list[float], # FER of every receiver
        common_collision_probability: float, # collision that affect every recaiver
        maximum_attempts: int, # total attempts permited
    ) -> float:
        """
        Compute shortened E[T] while preserving collision correlation.

        A burst collision is common to every receiver, whereas channel errors
        are specific to every receiver. For ``t`` attempts, condition on ``k`` attempts
        that avoided the common collision, then apply independent receiver FER
        only to those ``k`` opportunities. This is an executable equivalent of
        the intent of paper Eqs. (40)–(44), whose printed indices are malformed.
        """
        if maximum_attempts < 1:
            raise ValueError("maximum_attempts must be at least 1")
        q = min(max(common_collision_probability, 0.0), 1.0)
        expected = 0.0

        # Survival-sum identity: E[T] = sum_{t=0}^{A-1} P(T > t), where A is
        # the maximum number of attempts allowed by the effective retry limit.
        for completed_attempts in range(maximum_attempts):
            completion_probability = 0.0
            for collision_free_attempts in range(completed_attempts + 1):
                common_event_probability = (
                    math.comb(completed_attempts, collision_free_attempts)
                    * (1 - q) ** collision_free_attempts
                    * q ** (completed_attempts - collision_free_attempts)
                )
                all_receivers_succeeded = math.prod(
                    1 - frame_error_rate**collision_free_attempts
                    for frame_error_rate in receiver_frame_error_rates
                )
                completion_probability += (
                    common_event_probability * all_receivers_succeeded
                )
            expected += 1 - completion_probability

        return max(expected, 1.0)

    def unicast_throughput(
        self,
        probabilities: SlotProbabilities,
        slot_time: float,
    ) -> float:
        return (
            probabilities.unicast_success
            * self.p.unicast_payload_bits
            * (1 - self.unicast_frame_error_rate())
            / slot_time
        )

    def unicast_frame_error_rate(self) -> float:
        return self.frame_error_rate(
            self.p.unicast_ber, self.p.unicast_payload_bits
        )

    @staticmethod
    def frame_error_rate(ber: float, payload_bits: int) -> float:
        """Paper Eq. (1): FER derived from payload BER."""
        return 1 - (1 - ber) ** payload_bits

    @staticmethod
    def rate_bps(rate_mbps: float) -> float:
        return rate_mbps * 1e6

    @staticmethod
    def clamp_probability(value: float) -> float:
        return min(max(value, 0.0), 1.0)


def mechanism_label(mechanism: Mechanism) -> str:
    labels = {
        Mechanism.LEGACY: "Legacy No-Ack",
        Mechanism.GCR_UR: "GCR UR",
        Mechanism.DMS: "DMS",
        Mechanism.IMMEDIATE_BA: "Immediate BA",
        Mechanism.DELAYED_BA: "Delayed BA",
    }
    return labels[mechanism]


def print_comparison_table(
    results: list[MechanismResult], best: MechanismResult | None
) -> None:
    """Print one paper-style comparison and mark only a feasible winner."""
    if not results:
        raise ValueError("comparison requires at least one mechanism result")
    print(
        f"Saturated comparison: Nrx={results[0].receivers}, "
        f"Nu={results[0].unicast_stations}"
    )
    print(
        "{:<16} {:>10} {:>12} {:>12} {:>11} {:>9} {:>9} {:>8}".format(
            "Mechanism",
            "Reliab.",
            "Sm Mbps",
            "Su Mbps",
            "Utility",
            "t_m",
            "p_cm",
            "BA R",
        )
    )
    for result in results:
        retry_text = "-" if result.effective_ba_retries is None else str(
            result.effective_ba_retries
        )
        marker = " *" if result is best else ""
        print(
            f"{mechanism_label(result.mechanism):<16} "
            f"{result.reliability:>9.2%} "
            f"{result.multicast_throughput_mbps:>12.3f} "
            f"{result.unicast_throughput_mbps:>12.3f} "
            f"{result.utility_mbps:>11.3f} "
            f"{result.multicast_attempt_probability:>9.4f} "
            f"{result.multicast_collision_probability:>9.2%} "
            f"{retry_text:>8}{marker}"
        )
    if best is None: # Print best mechanism for that run
        print("\nNo mechanism satisfies the configured reliability threshold.")
    else:
        print("\n* selected by the paper's reliability-constrained LogRD utility")
    print(
        "Selection uses documented executable assumptions; exact Figure 13 "
        "boundaries are not yet reproduced."
    )


def print_sweep_table(
    model: PaperAnalyticalModel,
    axis: str,
    start: int,
    end: int,
    receivers: int,
    unicast_stations: int,
    selection_only: bool,
    output_format: str,
) -> None:
    """Print curve data as rows so it can be inspected or exported without plots."""
    rows: list[tuple[int, int, MechanismResult | None, bool]] = []
    for value in range(start, end + 1):
        nrx = value if axis == "receivers" else receivers
        nu = value if axis == "unicast" else unicast_stations
        pointresults = model.evaluate_all(nrx, nu)
        best = model.select_best(pointresults)
        if selection_only:
            # Preserve a visible row even if no mechanism is feasible.
            rows.append((nrx, nu, best, best is not None))
        else:
            rows.extend(
                (nrx, nu, result, best is result) for result in pointresults
            )

    header = [
        "Nrx",  # Number of multicast receivers
        "Nu",   # Number of unicast stations
        "Mechanism",
        "Reliability",
        "Sm_Mbps",
        "Su_Mbps",
        "Utility_Mbps",
        "Selected",
    ]
    values: list[list[object]] = []
    for nrx, nu, result, selected in rows:
        if result is None:
            values.append([nrx, nu, "none", "", "", "", "", "no-feasible"])
            continue
        values.append(
            [
                nrx,
                nu,
                result.mechanism.value,
                f"{result.reliability:.6f}",
                f"{result.multicast_throughput_mbps:.6f}",
                f"{result.unicast_throughput_mbps:.6f}",
                f"{result.utility_mbps:.6f}",
                "yes" if selected else "no",
            ]
        )

    if output_format == "csv":
        # Explicit LF prevents Windows text translation from producing CRCRLF
        # when output is redirected to a file.
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(values)
        return

    widths = [
        max(len(str(row[index])) for row in [header, *values])
        for index in range(len(header))
    ]
    print(" ".join(f"{name:<{widths[index]}}" for index, name in enumerate(header)))
    print(" ".join("-" * width for width in widths))
    for row in values:
        print(
            " ".join(
                f"{value!s:<{widths[index]}}"
                for index, value in enumerate(row)
            )
        )


def station_count(value: str) -> int: # Checks if stations are in range 1-30
    """Argparse converter for the paper's evaluated 1..30 station range."""
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if not MIN_STATIONS <= parsed <= MAX_STATIONS:
        raise argparse.ArgumentTypeError(
            f"must be between {MIN_STATIONS} and {MAX_STATIONS}"
        )
    return parsed


def probability(value: str) -> float: # Checks if probability is float in range 0-1
    """Argparse converter for finite probabilities, including endpoints 0..1 range."""
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not math.isfinite(parsed) or not 0 <= parsed <= 1:
        raise argparse.ArgumentTypeError("must be finite and between 0 and 1")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Paper-oriented analytical comparison of reliable multicast "
            "mechanisms in IEEE 802.11aa WLANs."
        )
    )
    parser.add_argument(
        "--receivers",
        type=station_count,
        default=10,
        help="Nrx (default: 10)"
    )
    parser.add_argument(
        "--unicast-stations",
        type=station_count,
        default=10,
        help="Nu (default: 10)",
    )
    parser.add_argument(
        "--sweep",
        choices=("none", "receivers", "unicast"),
        default="none",
        help="print curve data as a table rather than one comparison",
    )
    parser.add_argument(
        "--sweep-start",
        type=station_count,
        default=1
    )
    parser.add_argument(
        "--sweep-end",
        type=station_count,
        default=30
    )
    parser.add_argument(
        "--selection-only",
        action="store_true",
        help="during a sweep, print only the LogRD-selected mechanism per point",
    )
    parser.add_argument(
        "--format",
        choices=("table", "csv"),
        default="table",
        help="sweep output format (default: table)",
    )
    parser.add_argument(
        "--reliability-threshold",
        type=probability,
        default=0.90,
        help="minimum reliability for LogRD selection (default: 0.90)",
    )
    args = parser.parse_args(argv)
    if args.sweep_start > args.sweep_end:
        parser.error("--sweep-start cannot be greater than --sweep-end")
    if args.sweep == "none" and args.selection_only:
        parser.error("--selection-only requires --sweep")
    if args.sweep == "none" and args.format != "table":
        parser.error("--format applies only when --sweep is enabled")
    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    parameters = ModelParameters(reliability_threshold=args.reliability_threshold)
    model = PaperAnalyticalModel(parameters)

    try:
        if args.sweep == "none":
            results = model.evaluate_all(args.receivers, args.unicast_stations)
            print_comparison_table(results, model.select_best(results))
            return

        print_sweep_table(
            model=model,
            axis=args.sweep,
            start=args.sweep_start,
            end=args.sweep_end,
            receivers=args.receivers,
            unicast_stations=args.unicast_stations,
            selection_only=args.selection_only,
            output_format=args.format,
        )
    except ModelDomainError as error:
        raise SystemExit(f"model domain error: {error}") from error


if __name__ == "__main__":
    main()
