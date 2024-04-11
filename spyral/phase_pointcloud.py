from .core.config import GetParameters, DetectorParameters, FribParameters
from .core.pad_map import PadMap
from .core.point_cloud import PointCloud
from .core.workspace import Workspace
from .trace.frib_event import FribEvent
from .trace.get_event import GetEvent
from .trace.frib_scalers import process_scalers
from .correction import create_electron_corrector, ElectronCorrector
from .parallel.status_message import StatusMessage, Phase
from .core.spy_log import spyral_info, spyral_error, spyral_warn

import h5py as h5
import numpy as np
from pathlib import Path
from multiprocessing import SimpleQueue


def get_event_range(trace_file: h5.File) -> tuple[int, int]:
    """
    The merger doesn't use attributes for legacy reasons, so everything is stored in datasets. Use this to retrieve the min and max event numbers.

    Parameters
    ----------
    trace_file: h5py.File
        File handle to a hdf5 file with AT-TPC traces

    Returns
    -------
    tuple[int, int]
        A pair of integers (first event number, last event number)
    """
    meta_group = trace_file.get("meta")
    meta_data = meta_group.get("meta")  # type: ignore
    return (int(meta_data[0]), int(meta_data[2]))  # type: ignore


def phase_pointcloud(
    run: int,
    ws: Workspace,
    pad_map: PadMap,
    get_params: GetParameters,
    frib_params: FribParameters,
    detector_params: DetectorParameters,
    queue: SimpleQueue,
):
    """The core loop of the pointcloud phase

    Generate point clouds from merged AT-TPC traces. Read in traces from a hdf5 file
    generated by the AT-TPC merger and convert the traces into point cloud events. This is
    the first phase of Spyral analysis.

    Parameters
    ----------
    run: int
        The run number to be processed
    ws: Workspace
        The project workspace
    pad_map: PadMap
        A map of pad number to geometry/hardware/calibrations
    get_params: GetParameters
        Configuration parameters for GET data signal analysis (AT-TPC pads)
    frib_params: FribParameters
        Configuration parameters for FRIBDAQ data signal analysis (ion chamber, silicon, etc.)
    detector_params: DetectorParameters
        Configuration parameters for physical detector properties
    queue: SimpleQueue
        Communication channel back to the parent process
    """

    # Check that the traces exist
    trace_path = ws.get_trace_file_path(run)
    if not trace_path.exists():
        spyral_warn(__name__, f"Run {run} does not exist for phase 1, skipping.")
        return

    # Open files
    point_path = ws.get_point_cloud_file_path(run)
    trace_file = h5.File(trace_path, "r")
    point_file = h5.File(point_path, "w")

    min_event, max_event = get_event_range(trace_file)

    # Load electric field correction
    corrector: ElectronCorrector | None = None
    if detector_params.do_garfield_correction:
        corr_path = ws.get_correction_file_path(
            Path(detector_params.garfield_file_path)
        )
        corrector = create_electron_corrector(corr_path)

    # Some checks for existance
    event_group: h5.Group = trace_file["get"]  # type: ignore
    if not isinstance(event_group, h5.Group):
        spyral_error(
            __name__,
            f"GET event group does not exist in run {run}, phase 1 cannot be run!",
        )
        return

    frib_group: h5.Group = trace_file["frib"]  # type: ignore
    if not isinstance(frib_group, h5.Group):
        spyral_error(
            __name__, f"FRIB group does not exist in run {run}, phase 1 cannot be run!"
        )
        return
    frib_evt_group: h5.Group = frib_group["evt"]  # type: ignore
    if not isinstance(frib_evt_group, h5.Group):
        spyral_error(
            __name__,
            f"FRIB event data group does not exist in run {run}, phase 1 cannot be run!",
        )
        return

    frib_scaler_group: h5.Group | None = frib_group["scaler"]  # type: ignore
    if not isinstance(frib_group, h5.Group):
        spyral_warn(
            __name__,
            f"FRIB scaler data group does not exist in run {run}. Spyral will continue, but scalers will not exist.",
        )
        frib_scaler_group = None
    cloud_group = point_file.create_group("cloud")
    cloud_group.attrs["min_event"] = min_event
    cloud_group.attrs["max_event"] = max_event

    flush_percent = 0.01
    flush_val = int(flush_percent * (max_event - min_event))
    count = 0

    # Process the data
    for idx in range(min_event, max_event + 1):
        if count > flush_val:
            count = 0
            queue.put(StatusMessage(run, Phase.CLOUD, 1))
        count += 1

        event_data: h5.Dataset
        try:
            event_data = event_group[f"evt{idx}_data"]  # type: ignore
        except Exception:
            continue

        event = GetEvent(event_data, idx, get_params)

        pc = PointCloud()
        pc.load_cloud_from_get_event(event, pad_map)

        pc_dataset = cloud_group.create_dataset(
            f"cloud_{pc.event_number}", shape=pc.cloud.shape, dtype=np.float64
        )

        # default IC settings
        pc_dataset.attrs["ic_amplitude"] = -1.0
        pc_dataset.attrs["ic_integral"] = -1.0
        pc_dataset.attrs["ic_centroid"] = -1.0
        pc_dataset.attrs["ic_multiplicity"] = -1.0

        # Now analyze FRIBDAQ data
        frib_data: h5.Dataset
        try:
            frib_data = frib_evt_group[f"evt{idx}_1903"]  # type: ignore
        except Exception:
            pc.calibrate_z_position(
                detector_params.micromegas_time_bucket,
                detector_params.window_time_bucket,
                detector_params.detector_length,
                corrector,
            )
            pc_dataset[:] = pc.cloud
            continue

        frib_event = FribEvent(frib_data, idx, frib_params)
        # Handle IC analysis cases
        # First check if IC correction is not on
        if frib_params.correct_ic_time:
            # IC correction is on, extract good IC peak with Si coincidence imposed
            good_ic = frib_event.get_good_ic_peak(frib_params)
            if good_ic is None:
                # There is no good IC peak, skip
                pc.calibrate_z_position(
                    detector_params.micromegas_time_bucket,
                    detector_params.window_time_bucket,
                    detector_params.detector_length,
                    corrector,
                )
                pc_dataset[:] = pc.cloud
                continue
            # Good IC found, get the peak and multiplicity
            peak = good_ic[1]
            mult = good_ic[0]
            pc_dataset.attrs["ic_amplitude"] = peak.amplitude
            pc_dataset.attrs["ic_integral"] = peak.integral
            pc_dataset.attrs["ic_centroid"] = peak.centroid
            pc_dataset.attrs["ic_multiplicity"] = mult

            ic_cor = frib_event.correct_ic_time(
                peak, frib_params, detector_params.get_frequency
            )
            # Apply IC correction to time calibration, if correction is less than the
            # total length of the GET window in TB
            if ic_cor < 512.0:
                pc.calibrate_z_position(
                    detector_params.micromegas_time_bucket,
                    detector_params.window_time_bucket,
                    detector_params.detector_length,
                    corrector,
                    ic_cor,
                )
            else:
                pc.calibrate_z_position(
                    detector_params.micromegas_time_bucket,
                    detector_params.window_time_bucket,
                    detector_params.detector_length,
                    corrector,
                )
        else:
            # No IC correction, so we calibrate z without it
            pc.calibrate_z_position(
                detector_params.micromegas_time_bucket,
                detector_params.window_time_bucket,
                detector_params.detector_length,
                corrector,
            )
            # Get triggering IC, no Si conicidence imposed
            ic_mult = frib_event.get_ic_multiplicity(frib_params)
            ic_peak = frib_event.get_triggering_ic_peak(frib_params)
            # Check multiplicity condition and existence of trigger
            if ic_mult <= frib_params.ic_multiplicity and ic_peak is not None:
                pc_dataset.attrs["ic_amplitude"] = ic_peak.amplitude
                pc_dataset.attrs["ic_integral"] = ic_peak.integral
                pc_dataset.attrs["ic_centroid"] = ic_peak.centroid
                pc_dataset.attrs["ic_multiplicity"] = ic_mult

        pc_dataset[:] = pc.cloud
    # End of event data

    # Process scaler data if it exists
    if frib_scaler_group is not None:
        process_scalers(frib_scaler_group, ws.get_scaler_file_path(run))

    spyral_info(__name__, "Phase 1 complete")
