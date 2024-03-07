import argparse
from datetime import datetime
import logging
import time

import ee
from google.cloud import storage

logging.getLogger('ee').setLevel(logging.INFO)
logging.getLogger('requests').setLevel(logging.INFO)
logging.getLogger('urllib3').setLevel(logging.INFO)


def main(overwrite_flag=False):
    """"""
    print('\nIngesting CONUS404 monthly bias correction ratio assets')

    ee.Initialize()

    variables = ['eto']
    # variables = ['eto', 'etr']

    bucket_name = 'openet'
    bucket_folder = (
        'bias_correction_gridwxcomp_testing/feb_runs/bias_outputs_conus404/'
        'spatial_interpolation_0.1_dd/gridded_eto_invdistnn_1000.0_meters/'
        'eto_invdistnn_p4_s0_maxpoints25_radius1500000m'
    )
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)

    asset_folder = (
        'projects/earthengine-legacy/assets/'
        'projects/openet/reference_et/conus/conus404/ratios/v0/monthly'
    )
    # coll_id = ''

    band_name = 'b1'
    properties = {
        'ingest_date': datetime.today().strftime('%Y-%m-%d'),
        'interp_method': 'inverse distance weighting',
        'max_points_parameter': 25,
        # 'month_abbrev': ,
        'power': 4,
        'radius_parameter': 15,
        'smoothing_parameter': 0,
        # 'source_data_version'
    }

    months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

    # Build the asset folder if it doesn't exist
    if not ee.data.getInfo(asset_folder):
        logging.info(f'\nFolder does not exist and will be built\n  {asset_folder}')
        input('Press ENTER to continue')
        ee.data.createAsset({'type': 'FOLDER'}, asset_folder)

    for variable in variables:
        print(f'{variable}')

        asset_coll_id = f'{asset_folder}/{variable}'
        print(f'  {asset_coll_id}')

        # Build the image collection if it doens't exist
        if not ee.data.getInfo(asset_coll_id):
            logging.info(f'\nImage collection does not exist and will be built\n  {asset_coll_id}')
            input('Press ENTER to continue')
            ee.data.createAsset({'type': 'IMAGE_COLLECTION'}, asset_coll_id)

            # Make the asset public
            policy = {'bindings': [{'role': 'roles/viewer', 'members': ['allUsers']}]}
            ee.data.setIamPolicy(asset_coll_id, policy)

        for month in months:
            bucket_path = f'gs://{bucket_name}/{bucket_folder}/{month}.tiff'
            asset_id = f'{asset_coll_id}/{month}'
            print(f'{month}')
            # print(f'  {bucket_path}')
            # print(f'  {asset_id}')
            properties['month_abbrev'] = month
            properties['bucket_url'] = bucket_path

            if not bucket.blob(f'{bucket_folder}/{month}.tiff').exists():
                print('  Bucket file does not exist, skipping')
                continue

            if ee.data.getInfo(asset_id):
                if overwrite_flag:
                    print('  Removing existing asset')
                    ee.data.deleteAsset(asset_id)
                    time.sleep(1)
                else:
                    print('  Asset already exists, skipping')
                    continue

            logging.info('  Ingesting into Earth Engine')
            task_id = ee.data.newTaskId()[0]
            params = {
                'name': asset_id,
                'bands': [{'id': band_name, 'tilesetId': 'image', 'tilesetBandIndex': 0}],
                'tilesets': [{'id': 'image', 'sources': [{'uris': [bucket_path]}]}],
                'properties': properties,
                # 'startTime': '2000-01-01T00:00:00' + '.000000000Z',
                # 'pyramidingPolicy': 'MEAN',
                # 'missingData': {'values': [nodata_value]},
            }
            ee.data.startIngestion(task_id, params, allow_overwrite=True)
            time.sleep(1)


def arg_parse():
    """"""
    parser = argparse.ArgumentParser(
        description='Ingest CONUS404 monthly bias correction ratio assets',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        '--overwrite', default=False, action='store_true',
        help='Force overwrite of existing files')
    parser.add_argument(
        '--debug', default=logging.INFO, const=logging.DEBUG,
        help='Debug level logging', action='store_const', dest='loglevel')
    args = parser.parse_args()

    return args


if __name__ == '__main__':
    args = arg_parse()
    logging.basicConfig(level=args.loglevel, format='%(message)s')

    main(overwrite_flag=args.overwrite)
