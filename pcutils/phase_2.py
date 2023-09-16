from .core.config import ClusterParameters, DetectorParameters
from .core.point_cloud import PointCloud
from .core.clusterize import clusterize, join_clusters, cleanup_clusters
import h5py as h5
from pathlib import Path
from time import time

def phase_2(point_path: Path, cluster_path: Path, cluster_params: ClusterParameters):

    start = time()

    point_file = h5.File(point_path, 'r')
    cluster_file = h5.File(cluster_path, 'w')

    cloud_group: h5.Group = point_file.get('cloud')
    min_event = cloud_group.attrs['min_event']
    max_event = cloud_group.attrs['max_event']
    cluster_group: h5.Group = cluster_file.create_group('cluster')
    cluster_group.attrs['min_event'] = min_event
    cluster_group.attrs['max_event'] = max_event

    print(f'Clustering point clouds in file {point_path} over events {min_event} to {max_event}')

    flush_percent = 0.01
    flush_val = int(flush_percent * (max_event - min_event))
    flush_count = 0
    count = 0

    for idx in range(min_event, max_event+1):

        if count > flush_val:
            count = 0
            flush_count += 1
            print(f'\rPercent of data processed: {int(flush_count * flush_percent * 100)}%', end='')
        count += 1

        cloud_data: h5.Dataset | None = None
        try:
            cloud_data = cloud_group.get(f'cloud_{idx}')
        except:
            continue

        if cloud_data is None:
            continue

        cloud = PointCloud()
        cloud.load_cloud_from_hdf5_data(cloud_data[:].copy(), idx)
        if len(cloud.cloud) < cluster_params.min_write_size:
            continue

        clusters = clusterize(cloud, cluster_params)
        joined = join_clusters(clusters, cluster_params)
        cleaned = cleanup_clusters(joined, cluster_params)

        #Write the clusters, but only if the size of the cluster exceeds the inputed value
        cluster_event_group = cluster_group.create_group(f'event_{idx}')
        cluster_event_group.attrs['nclusters'] = len(cleaned)
        for cidx, cluster in enumerate(cleaned):
            local_group = cluster_event_group.create_group(f'cluster_{cidx}')
            local_group.attrs['label'] = cluster.label
            local_group.create_dataset('cloud', data=cluster.point_cloud.cloud)


    stop = time()
    print(f'\nProcessing complete. Duration: {stop - start}s')



