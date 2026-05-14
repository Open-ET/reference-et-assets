import argparse
import datetime
import logging
import os
import pprint

import ee
from google.cloud import storage

# logging.getLogger('googleapiclient').setLevel(logging.ERROR)
# logging.getLogger('pydap').setLevel(logging.WARNING)
# logging.getLogger('rasterio').setLevel(logging.WARNING)
# logging.getLogger('requests').setLevel(logging.INFO)
# logging.getLogger('urllib3').setLevel(logging.INFO)

ASSET_FOLDER = 'projects/openet/assets/reference_et/utah/gridet'
PROJECT_NAME = 'openet'
BUCKET_NAME = 'openet_temp'
BUCKET_FOLDER = 'reference_et/utah/gridet/ancillary'
STORAGE_CLIENT = storage.Client(project=PROJECT_NAME)


def main(project_id, overwrite_flag=False):
    """Build and ingest CONUS404 ancillary assets into Earth Engine

    Parameters
    ----------
    project_id : str
    overwrite_flag : bool, optional
        If True, overwrite existing files (the default is False).

    """
    logging.info('\nGridET ancillary assets')

    variables = ['elevation', 'aspect', 'slope', 'nldas2_elevation', 'nldas2_aspect', 'nldas2_slope']

    band_names = {
        'elevation': 'elevation',
        'aspect': 'aspect',
        'slope': 'slope',
        'nldas2_elevation': 'elevation',
        'nldas2_aspect': 'aspect',
        'nldas2_slope': 'slope',
    }
    file_names = {
        'elevation': 'elevation',
        'aspect': 'aspect',
        'slope': 'slope',
        'nldas2_elevation': 'nldas2_elevation',
        'nldas2_aspect': 'nldas2_aspect',
        'nldas2_slope': 'nldas2_slope',
    }

    gridet_proj = (
        "PROJCS[\"NAD83(NSRS2007)/UtahCentral(ftUS)\","
        "GEOGCS[\"NAD83\",DATUM[\"North_American_Datum_1983\","
        "SPHEROID[\"GRS1980\",6378137.0,298.257222101004,AUTHORITY[\"EPSG\",\"7019\"]],"
        "AUTHORITY[\"EPSG\",\"6269\"]],"
        "PRIMEM[\"Greenwich\",0.0],UNIT[\"degree\",0.017453292519943295],"
        "AXIS[\"Longitude\",EAST],AXIS[\"Latitude\",NORTH],AUTHORITY[\"EPSG\",\"4269\"]],"
        "PROJECTION[\"Lambert_Conformal_Conic_2SP\"],PARAMETER[\"central_meridian\",-111.5],"
        "PARAMETER[\"latitude_of_origin\",38.3333333333333],PARAMETER[\"standard_parallel_1\",40.65],"
        "PARAMETER[\"false_easting\",1640416.66666667],PARAMETER[\"false_northing\",6561666.66666667],"
        "PARAMETER[\"scale_factor\",1.0],PARAMETER[\"standard_parallel_2\",39.0166666666667],"
        "UNIT[\"foot_survey_us\",0.304800609601219],AXIS[\"Easting\",EAST],AXIS[\"Northing\",NORTH]]"
    )
    gridet_transform = [1760, 0, 667040, 0, -1760, 8212160]
    gridet_shape = (1111, 1394)
    # gridet_cellsize = 1760

    nldas_proj = 'EPSG:4326'
    nldas_transform = [0.125, 0, -125, 0, -0.125, 53]
    nldas_shape = (464, 224)
    # nldas_cellsize = 0.125

    workspace = os.getcwd()
    ancillary_ws = os.path.join(workspace, 'ancillary')
    # if not os.path.isdir(ancillary_ws):
    #     os.makedirs(ancillary_ws)

    logging.info('\nInitializing Earth Engine')
    ee.Initialize(project=project_id)

    for var_name in variables:
        logging.info(f'\n{var_name}')

        tif_path = os.path.join(ancillary_ws, f'{file_names[var_name]}.tif')
        bucket_path = f'gs://{BUCKET_NAME}/{BUCKET_FOLDER}/{file_names[var_name]}.tif'
        asset_id = f'{ASSET_FOLDER}/ancillary/{file_names[var_name]}'
        logging.debug(f' {tif_path}')
        logging.debug(f' {bucket_path}')
        logging.debug(f' {asset_id}')

        if ee.data.getInfo(asset_id):
            if overwrite_flag:
                logging.info(f'Asset already exists, removing')
                # TODO: Try/Except on delete
                ee.data.deleteAsset(asset_id)
            else:
                logging.info(f'Asset already exists and overwrite is False, skipping')
                continue

        logging.info('Uploading to bucket')
        logging.debug(f'  {bucket_path}')
        bucket = STORAGE_CLIENT.bucket(BUCKET_NAME)
        blob = bucket.blob(f'{BUCKET_FOLDER}/{os.path.basename(bucket_path)}')
        blob.upload_from_filename(tif_path)

        # For now, assume the file is in the bucket
        logging.info('Ingesting into Earth Engine')
        logging.debug(f'  {asset_id}')
        task_id = ee.data.newTaskId()[0]
        logging.debug(f'  {task_id}')

        if 'nldas' in var_name:
            transform = {
                'scale_x': nldas_transform[0],
                'shear_x': nldas_transform[1],
                'translate_x': nldas_transform[2],
                'shear_y': nldas_transform[3],
                'scale_y': nldas_transform[4],
                'translate_y': nldas_transform[5],
            }
        else:
            {
                'scale_x': gridet_transform[0],
                'shear_x': gridet_transform[1],
                'translate_x': gridet_transform[2],
                'shear_y': gridet_transform[3],
                'scale_y': gridet_transform[4],
                'translate_y': gridet_transform[5],
            }

        params = {
            'name': asset_id,
            'bands': [
                {'id': band_names[var_name], 'tilesetId': 'image', 'tilesetBandIndex': 0}
            ],
            'tilesets': [{
                'id': 'image',
                'crs': gridet_proj if 'nldas' not in var_name else nldas_proj,
                'sources': [{'uris': [bucket_path]}],
                # 'sources': [{'uris': [bucket_path], 'affine_transform': transform}],
            }],
            'properties': {
                'date_ingested': datetime.datetime.today().strftime('%Y-%m-%d'),
            },
            # 'startTime': '2000-01-01T00:00:00' + '.000000000Z',
            # 'pyramidingPolicy': pyramid_policy,
            # 'missingData': {'values': [nodata_value]},
        }
        ee.data.startIngestion(task_id, params, allow_overwrite=True)

        # logging.info('Removing from bucket')
        # if blob and blob.exists():
        #     blob.delete()


    # # GridET latitude/longitude rasters
    # input('ENTER')
    # mask_img = ee.Image(f'{ASSET_FOLDER}/ancillary/elevation')
    # latitude_id = f'{ASSET_FOLDER}/ancillary/latitude'
    # longitude_id = f'{ASSET_FOLDER}/ancillary/longitude'
    #
    # if overwrite_flag or not ee.data.getInfo(latitude_id):
    #     logging.info('latitude')
    #     if ee.data.getInfo(latitude_id):
    #         ee.data.deleteAsset(latitude_id)
    #
    #     task = ee.batch.Export.image.toAsset(
    #         image=mask_img.multiply(0).add(ee.Image.pixelLonLat().select('latitude')).rename(['latitude']),
    #         description='gridet_latitude_asset',
    #         assetId=latitude_id,
    #         dimensions=gridet_shape,
    #         crs=gridet_proj,
    #         crsTransform=gridet_transform,
    #         maxPixels=10000000000,
    #     )
    #     task.start()
    #
    # if overwrite_flag or not ee.data.getInfo(longitude_id):
    #     logging.info('longitude')
    #     if ee.data.getInfo(longitude_id):
    #         ee.data.deleteAsset(longitude_id)
    #
    #     task = ee.batch.Export.image.toAsset(
    #         image=mask_img.multiply(0).add(ee.Image.pixelLonLat().select('longitude')).rename(['longitude']),
    #         description='gridet_longitude_asset',
    #         assetId=longitude_id,
    #         dimensions=gridet_shape,
    #         crs=gridet_proj,
    #         crsTransform=gridet_transform,
    #         maxPixels=10000000000,
    #     )
    #     task.start()
    #
    # # NLDAS latitude/longitude rasters
    # input('ENTER')
    # mask_img = ee.Image(f'{ASSET_FOLDER}/ancillary/nldas2_elevation')
    # latitude_id = f'{ASSET_FOLDER}/ancillary/nldas2_latitude'
    # longitude_id = f'{ASSET_FOLDER}/ancillary/nldas2_longitude'
    #
    # if overwrite_flag or not ee.data.getInfo(latitude_id):
    #     logging.info('NLDAS latitude')
    #     if ee.data.getInfo(latitude_id):
    #         ee.data.deleteAsset(latitude_id)
    #
    #     task = ee.batch.Export.image.toAsset(
    #         image=mask_img.multiply(0).add(ee.Image.pixelLonLat().select('latitude')).rename(['latitude']),
    #         description='nldas_latitude_asset',
    #         assetId=latitude_id,
    #         dimensions=nldas_shape,
    #         crs=nldas_proj,
    #         crsTransform=nldas_transform,
    #         maxPixels=10000000000,
    #     )
    #     task.start()
    #
    # if overwrite_flag or not ee.data.getInfo(longitude_id):
    #     logging.info('NLDAS longitude')
    #     if ee.data.getInfo(longitude_id):
    #         ee.data.deleteAsset(longitude_id)
    #
    #     task = ee.batch.Export.image.toAsset(
    #         image=mask_img.multiply(0).add(ee.Image.pixelLonLat().select('longitude')).rename(['longitude']),
    #         description='nldas_longitude_asset',
    #         assetId=longitude_id,
    #         dimensions=nldas_shape,
    #         crs=nldas_proj,
    #         crsTransform=nldas_transform,
    #         maxPixels=10000000000,
    #     )
    #     task.start()


def arg_parse():
    """"""
    parser = argparse.ArgumentParser(
        description='Ingest GridET ancillary assets into Earth Engine',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--project', required=True, help='Earth Engine Project ID')
    # parser.add_argument(
    #     '--zero', default=False, action='store_true',
    #     help='Set elevation nodata values to 0')
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

    main(project_id=args.project, overwrite_flag=args.overwrite)
