from .core.config import SolverParameters, DetectorParameters
from .core.track_generator import create_interpolator
from .core.workspace import Workspace
from .core.nuclear_data import NuclearDataMap
from .core.particle_id import load_particle_id, ParticleID
from .core.cluster import Cluster
from .solvers.solver_interp import solve_physics_interp, Guess
from .parallel.status_message import StatusMessage, Phase

import h5py as h5
import polars as pl
from multiprocessing import SimpleQueue

def phase_4_interp(run: int, ws: Workspace, solver_params: SolverParameters, nuclear_map: NuclearDataMap, queue: SimpleQueue):

    pid: ParticleID | None = load_particle_id(ws.get_gate_file_path(solver_params.particle_id_filename), nuclear_map)
    if pid is None:
        queue.put(StatusMessage(run, Phase.WAIT, 100))
        return
    
    cluster_path = ws.get_cluster_file_path(run)
    estimate_path = ws.get_estimate_file_path_parquet(run)
    if not cluster_path.exists() or not estimate_path.exists():
        queue.put(StatusMessage(run, Phase.WAIT, 100))
        return
    
    result_path = ws.get_physics_file_path_parquet(run, pid.nucleus)
    cluster_file = h5.File(cluster_path, 'r')
    estimate_df = pl.scan_parquet(estimate_path)

    cluster_group: h5.Group = cluster_file.get('cluster')

    #Select the particle group data, beam region of ic, convert to dictionary for row-wise operations
    #Select only the largest polar angle for a given event to avoid beam-like particles
    estimates_gated = estimate_df.filter(
            pl.struct(['dEdx', 'brho']).map(pid.cut.is_cols_inside) & 
            (pl.col('ic_amplitude') > solver_params.ic_min_val) & 
            (pl.col('ic_amplitude') < solver_params.ic_max_val)
        ) \
        .sort('polar', descending=True) \
        .unique('event', keep='first') \
        .collect() \
        .to_dict()
    
    if len(estimates_gated['event']) == 0:
        queue.put(StatusMessage(run, Phase.WAIT, 100))
        return

    flush_percent = 0.01
    flush_val = int(flush_percent * (len(estimates_gated['event'])))
    count = 0

    results: dict[str, list] = { 
        'event': [], 
        'cluster_index': [], 
        'cluster_label': [], 
        'vertex_x': [], 
        'sigma_vx': [], 
        'vertex_y': [], 
        'sigma_vy': [], 
        'vertex_z': [], 
        'sigma_vz': [],
        'brho': [], 
        'sigma_brho': [], 
        'polar': [], 
        'sigma_polar': [], 
        'azimuthal': [], 
        'sigma_azimuthal': [], 
        'redchisq': []
    }

    interp_path = ws.get_track_file_path(solver_params.interp_file_name)
    interpolator = create_interpolator(interp_path)

    for row, event in enumerate(estimates_gated['event']):
        if count > flush_val:
            count = 0
            queue.put(StatusMessage(run, Phase.SOLVE, 1))
        count += 1

        event_group = cluster_group[f'event_{event}']
        cidx = estimates_gated['cluster_index'][row]
        local_cluster: h5.Dataset = event_group[f'cluster_{cidx}']
        cluster = Cluster(event, local_cluster.attrs['label'], local_cluster['cloud'][:].copy())

        #Do the solver
        guess = Guess(
                    estimates_gated['polar'][row],
                    estimates_gated['azimuthal'][row],
                    estimates_gated['brho'][row],
                    estimates_gated['vertex_x'][row],
                    estimates_gated['vertex_y'][row],
                    estimates_gated['vertex_z'][row]
                )
        solve_physics_interp(cidx, cluster, guess, interpolator, pid.nucleus, results)

    physics_df = pl.DataFrame(results)
    physics_df.write_parquet(result_path)