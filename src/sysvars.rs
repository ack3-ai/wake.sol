//! Typed, read-friendly views of the cluster sysvars, exposed on `svm`.
//!
//! Reads go through litesvm's `get_sysvar`, writes through `set_sysvar` (which
//! updates the cached sysvar the runtime actually uses during execution — not
//! just the backing account). Scalar sysvars (`Clock`, `Rent`, `EpochSchedule`,
//! `LastRestartSlot`) get partial kwarg setters on `PyLiteSVM`; the
//! collection/derived ones (`SlotHashes`, `EpochRewards`) are read-only for now.

use pyo3::prelude::*;
use pyo3::types::PyBytes;

use solana_clock::Clock;
use solana_epoch_rewards::EpochRewards;
use solana_epoch_schedule::EpochSchedule;
use solana_rent::Rent;

#[pyclass(name = "Clock", module = "wake_sol._native", frozen, get_all)]
pub struct PyClock {
    pub slot: u64,
    pub epoch_start_timestamp: i64,
    pub epoch: u64,
    pub leader_schedule_epoch: u64,
    pub unix_timestamp: i64,
}

impl From<Clock> for PyClock {
    fn from(c: Clock) -> Self {
        Self {
            slot: c.slot,
            epoch_start_timestamp: c.epoch_start_timestamp,
            epoch: c.epoch,
            leader_schedule_epoch: c.leader_schedule_epoch,
            unix_timestamp: c.unix_timestamp,
        }
    }
}

#[pymethods]
impl PyClock {
    fn __repr__(&self) -> String {
        format!(
            "Clock(slot={}, epoch={}, unix_timestamp={}, epoch_start_timestamp={}, \
             leader_schedule_epoch={})",
            self.slot, self.epoch, self.unix_timestamp, self.epoch_start_timestamp,
            self.leader_schedule_epoch,
        )
    }
}

#[pyclass(name = "Rent", module = "wake_sol._native", frozen, get_all)]
pub struct PyRent {
    pub lamports_per_byte_year: u64,
    pub exemption_threshold: f64,
    pub burn_percent: u8,
}

impl From<Rent> for PyRent {
    #[allow(deprecated)]  // rent fields are deprecated upstream but still the wire layout
    fn from(r: Rent) -> Self {
        Self {
            lamports_per_byte_year: r.lamports_per_byte_year,
            exemption_threshold: r.exemption_threshold,
            burn_percent: r.burn_percent,
        }
    }
}

#[pymethods]
impl PyRent {
    fn __repr__(&self) -> String {
        format!(
            "Rent(lamports_per_byte_year={}, exemption_threshold={}, burn_percent={})",
            self.lamports_per_byte_year, self.exemption_threshold, self.burn_percent,
        )
    }
}

#[pyclass(name = "EpochSchedule", module = "wake_sol._native", frozen, get_all)]
pub struct PyEpochSchedule {
    pub slots_per_epoch: u64,
    pub leader_schedule_slot_offset: u64,
    pub warmup: bool,
    pub first_normal_epoch: u64,
    pub first_normal_slot: u64,
}

impl From<EpochSchedule> for PyEpochSchedule {
    fn from(e: EpochSchedule) -> Self {
        Self {
            slots_per_epoch: e.slots_per_epoch,
            leader_schedule_slot_offset: e.leader_schedule_slot_offset,
            warmup: e.warmup,
            first_normal_epoch: e.first_normal_epoch,
            first_normal_slot: e.first_normal_slot,
        }
    }
}

#[pymethods]
impl PyEpochSchedule {
    fn __repr__(&self) -> String {
        format!(
            "EpochSchedule(slots_per_epoch={}, leader_schedule_slot_offset={}, warmup={}, \
             first_normal_epoch={}, first_normal_slot={})",
            self.slots_per_epoch, self.leader_schedule_slot_offset, self.warmup,
            self.first_normal_epoch, self.first_normal_slot,
        )
    }
}

#[pyclass(name = "EpochRewards", module = "wake_sol._native", frozen)]
pub struct PyEpochRewards {
    distribution_starting_block_height: u64,
    num_partitions: u64,
    parent_blockhash: [u8; 32],
    total_points: u128,
    total_rewards: u64,
    distributed_rewards: u64,
    active: bool,
}

impl From<EpochRewards> for PyEpochRewards {
    fn from(e: EpochRewards) -> Self {
        Self {
            distribution_starting_block_height: e.distribution_starting_block_height,
            num_partitions: e.num_partitions,
            parent_blockhash: e.parent_blockhash.to_bytes(),
            total_points: e.total_points,
            total_rewards: e.total_rewards,
            distributed_rewards: e.distributed_rewards,
            active: e.active,
        }
    }
}

#[pymethods]
impl PyEpochRewards {
    #[getter]
    fn distribution_starting_block_height(&self) -> u64 {
        self.distribution_starting_block_height
    }
    #[getter]
    fn num_partitions(&self) -> u64 {
        self.num_partitions
    }
    #[getter]
    fn parent_blockhash<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.parent_blockhash)
    }
    #[getter]
    fn total_points(&self) -> u128 {
        self.total_points
    }
    #[getter]
    fn total_rewards(&self) -> u64 {
        self.total_rewards
    }
    #[getter]
    fn distributed_rewards(&self) -> u64 {
        self.distributed_rewards
    }
    #[getter]
    fn active(&self) -> bool {
        self.active
    }
    fn __repr__(&self) -> String {
        format!(
            "EpochRewards(active={}, num_partitions={}, total_rewards={}, distributed_rewards={})",
            self.active, self.num_partitions, self.total_rewards, self.distributed_rewards,
        )
    }
}
