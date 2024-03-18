from .core.config import SolverParameters, DetectorParameters
from .interpolate.track_interpolator import create_interpolator
from .core.workspace import Workspace
from .core.cluster import Cluster
from .core.estimator import Direction
from .solvers.solver_interp import solve_physics_interp, Guess
from .parallel.status_message import StatusMessage, Phase
from .core.spy_log import spyral_error, spyral_warn, spyral_info

from spyral_utils.nuclear import NuclearDataMap
from spyral_utils.nuclear.target import load_target, GasTarget
from spyral_utils.nuclear.particle_id import deserialize_particle_id, ParticleID

import h5py as h5
import polars as pl
from multiprocessing import SimpleQueue
from pathlib import Path


def phase_solve(
    run: int,
    ws: Workspace,
    solver_params: SolverParameters,
    det_params: DetectorParameters,
    nuclear_map: NuclearDataMap,
    queue: SimpleQueue,
):
    """Core loop of the solve phase

    Use the estimates generated by the estimate phase to fit a set of ODE solutions to a cluster identified to be a particle trajectory.
    All three previous phases (point cloud, cluster, estimate) must have been run before this phase is called.

    Parameters
    ----------
    run: int
        The run number to be processed
    ws: Workspace
        The project Workspace
    solver_params: SolverParameters
        Configuration parameters for this phase
    det_params: DetectorParameters
        Configuration parameters for detector characteristics
    nuclear_map: NuclearDataMap
        Map containing AMDE data
    queue: SimpleQueue
        Communication channel back to the parent process for progress updates

    """

    # Need particle ID and target to select the correct data subset/interpolation scheme
    pid: ParticleID | None = deserialize_particle_id(
        ws.get_gate_file_path(solver_params.particle_id_filename), nuclear_map
    )
    if pid is None:
        queue.put(StatusMessage(run, Phase.WAIT, 0))
        spyral_warn(
            __name__,
            f"Particle ID {solver_params.particle_id_filename} does not exist, Solver will not run!",
        )
        return
    target = load_target(Path(solver_params.gas_data_path), nuclear_map)
    if not isinstance(target, GasTarget):
        queue.put(StatusMessage(run, Phase.WAIT, 0))
        spyral_warn(
            __name__,
            f"Target {solver_params.gas_data_path} is not of the correct format, Solver will not run!",
        )
        return

    # Check the cluster phase and estimate phase data
    cluster_path = ws.get_cluster_file_path(run)
    estimate_path = ws.get_estimate_file_path_parquet(run)
    if not cluster_path.exists() or not estimate_path.exists():
        queue.put(StatusMessage(run, Phase.WAIT, 0))
        spyral_warn(
            __name__,
            f"Either clusters or esitmates do not exist for run {run} at phase 4. Skipping.",
        )
        return

    # Setup files
    result_path = ws.get_physics_file_path_parquet(run, pid.nucleus)
    cluster_file = h5.File(cluster_path, "r")
    estimate_df = pl.scan_parquet(estimate_path)

    cluster_group: h5.Group = cluster_file["cluster"]
    if not isinstance(cluster_group, h5.Group):
        spyral_error(
            __name__, f"Cluster group does not eixst for run {run} at phase 4!"
        )
        return

    # Select the particle group data, beam region of ic, convert to dictionary for row-wise operations
    # Select only the largest polar angle for a given event to avoid beam-like particles
    estimates_gated = (
        estimate_df.filter(
            pl.struct(["dEdx", "brho"]).map_batches(pid.cut.is_cols_inside)
            & (pl.col("ic_amplitude") > solver_params.ic_min_val)
            & (pl.col("ic_amplitude") < solver_params.ic_max_val)
            # & (pl.col("ic_multiplicity") < 2.0)  # For legacy data
        )
        .sort("polar", descending=True)
        .unique("event", keep="first")
        .collect()
        .to_dict()
    )

    # Check that data actually exists for given PID
    if len(estimates_gated["event"]) == 0:
        queue.put(StatusMessage(run, Phase.WAIT, 0))
        spyral_warn(__name__, f"No events within PID for run {run}!")
        return

    flush_percent = 0.01
    flush_val = int(flush_percent * (len(estimates_gated["event"])))
    count = 0
    if len(estimates_gated["event"]) < 100:
        flush_percent = 100.0 / float(len(estimates_gated["event"]))
        flush_val = 1
        count = 0

    # Result storage
    results: dict[str, list] = {
        "event": [],
        "cluster_index": [],
        "cluster_label": [],
        "vertex_x": [],
        "sigma_vx": [],
        "vertex_y": [],
        "sigma_vy": [],
        "vertex_z": [],
        "sigma_vz": [],
        "brho": [],
        "sigma_brho": [],
        "polar": [],
        "sigma_polar": [],
        "azimuthal": [],
        "sigma_azimuthal": [],
        "redchisq": [],
    }

    # load the ODE solution interpolator
    interp_path = ws.get_track_file_path(pid.nucleus, target)
    interpolator = create_interpolator(interp_path)

    # Process the data
    for row, event in enumerate(estimates_gated["event"]):
        if count > flush_val:
            count = 0
            queue.put(StatusMessage(run, Phase.SOLVE, int(flush_percent * 100.0)))
        count += 1

        event_group = cluster_group[f"event_{event}"]
        cidx = estimates_gated["cluster_index"][row]
        local_cluster: h5.Dataset = event_group[f"cluster_{cidx}"]
        cluster = Cluster(
            event, local_cluster.attrs["label"], local_cluster["cloud"][:].copy()
        )

        # Do the solver
        guess = Guess(
            estimates_gated["polar"][row],
            estimates_gated["azimuthal"][row],
            estimates_gated["brho"][row],
            estimates_gated["vertex_x"][row],
            estimates_gated["vertex_y"][row],
            estimates_gated["vertex_z"][row],
            Direction.NONE,
        )
        solve_physics_interp(
            cidx,
            cluster,
            guess,
            pid.nucleus,
            interpolator,
            det_params,
            results,
        )

    # Write out the results
    physics_df = pl.DataFrame(results)
    physics_df.write_parquet(result_path)
    spyral_info(__name__, "Phase 4 complete.")
