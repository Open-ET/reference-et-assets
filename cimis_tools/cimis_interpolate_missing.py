import argparse
import datetime
import logging
import os
import pprint
import re
import sys
import time

import ee
# from osgeo import osr

# logging.getLogger('earthengine-api').setLevel(logging.INFO)
# logging.getLogger('googleapiclient').setLevel(logging.ERROR)
logging.getLogger('requests').setLevel(logging.INFO)
logging.getLogger('urllib3').setLevel(logging.INFO)

ASSET_COLL_ID = 'projects/earthengine-legacy/assets/' \
                'projects/openet/reference_et/cimis/daily'
ASSET_DT_FMT = '%Y%m%d'
# VARIABLES = ['eto']
VARIABLES = ['eto', 'eto_asce', 'etr_asce']
# VARIABLES = ['Tdew', 'Tx', 'Tn', 'Rnl', 'Rs', 'K', 'U2', 'ETo', 'ETo_ASCE', 'ETr_ASCE']


def main(variables, overwrite_flag=False, gee_key_file=None, ingest_flag=True):
    """Interpolate missing CIMIS multi-band images

    Parameters
    ----------
    variables : str
        Variables to process.
    overwrite_flag : bool, optional
        If True, overwrite existing files (the default is False).
    gee_key_file : str, optional
        File path to an Earth Engine json key file (the default is None).
    ingest_flag : bool, optional
        If True, ingest images into Earth Engine (the default is True).

    Returns
    -------
    None

    Notes
    -----
    https://spatialcimis.water.ca.gov/cimis/
    https://cimis.casil.ucdavis.edu/cimis/ (stopped updating in 2019)
    CIMIS ETo data starts: 2003-03-20
    Full parameters start: 2003-10-01 (water year 2004)

    """
    logging.info('\nInterpolate missing CIMIS multi-band images')

    missing_dt_list = [
        # '2021-01-14',
        # '2019-04-26',
        # '2019-12-24', '2019-12-25', '2019-12-26',
        # '2019-06-13'
    ]
    missing_dt_list = [
        datetime.datetime.strptime(date, '%Y-%m-%d')
        for date in missing_dt_list]

    start_dt = datetime.datetime(2018, 1, 1)
    end_dt = datetime.datetime(2021, 12, 31)

    asset_id_re = re.compile('\w+/(?P<date>\d{8})$')
    # asset_id_re = re.compile('{}/(?P<date>\d{{8}})'.format(ASSET_COLL_ID))

    # CIMIS grid
    asset_height = 560
    asset_width = 510
    asset_extent = (-410000.0, -660000.0, 610000.0, 460000.0)
    # asset_height = 552
    # asset_width = 500
    # asset_extent = (-400000.0, -650000.0, 600000.0, 454000.0)
    asset_cs = 2000.0
    asset_geo = (asset_cs, 0., asset_extent[0], 0., -asset_cs, asset_extent[3])
    # asset_geo = (asset_extent[0], asset_cs, 0., asset_extent[3], 0., -asset_cs)
    asset_geo_str = '[' + ','.join(list(map(str, asset_geo))) + ']'

    # Spatial reference parameters
    # This WKT was build using the OSR calls commented out below
    asset_proj = (
        'PROJCS["unknown",'
        'GEOGCS["unknown",DATUM["North_American_Datum_1983",'
        'SPHEROID["GRS 1980",6378137,298.257222101,AUTHORITY["EPSG","7019"]],'
        'AUTHORITY["EPSG","6269"]],'
        'PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],'
        'UNIT["degree",0.0174532925199433,'
        'AUTHORITY["EPSG","9122"]]],'
        'PROJECTION["Albers_Conic_Equal_Area"],'
        'PARAMETER["latitude_of_center",0],'
        'PARAMETER["longitude_of_center",-120],'
        'PARAMETER["standard_parallel_1",34],'
        'PARAMETER["standard_parallel_2",40.5],'
        'PARAMETER["false_easting",0],'
        'PARAMETER["false_northing",-4000000],'
        'UNIT["metre",1,AUTHORITY["EPSG","9001"]],'
        'AXIS["Easting",EAST],AXIS["Northing",NORTH]]'
    )
    # asset_proj4 = (
    #     '+proj=aea +lat_1=34 +lat_2=40.5 +lat_0=0 +lon_0=-120 ' +
    #     '+x_0=0 +y_0=-4000000 +ellps=GRS80 +datum=NAD83 +units=m +no_defs')
    # asset_osr = osr.SpatialReference()
    # asset_osr.ImportFromProj4(asset_proj4)
    # # asset_epsg = 3310  # NAD_1983_California_Teale_Albers
    # # asset_osr = gdc.epsg_osr(cimis_epsg)
    # # asset_osr.MorphToESRI()
    # asset_proj = asset_osr.ExportToWkt()

    # Initialize Earth Engine
    logging.info('\nInitializing Earth Engine')
    if gee_key_file:
        logging.info(f'  Using service account key file: {gee_key_file}')
        # The "EE_ACCOUNT" parameter is not used if the key file is valid
        ee.Initialize(ee.ServiceAccountCredentials('', key_file=gee_key_file))
    else:
        ee.Initialize()

    logging.debug(f'Image Collection: {ASSET_COLL_ID}')

    # Get a list of assets that are already ingested
    asset_coll = ee.ImageCollection(ASSET_COLL_ID)\
        .filterDate(start_dt, end_dt + datetime.timedelta(days=1))\
        .filter(ee.Filter.neq('refet_version', '0.0.0'))
    asset_dt_list = [
        datetime.datetime.strptime(image_id, ASSET_DT_FMT)
        for image_id in asset_coll.aggregate_array('system:index').getInfo()]
    # asset_id_list = get_ee_assets(
    #     ASSET_COLL_ID, start_dt, end_dt + datetime.timedelta(days=1))
    # asset_dt_list = [
    #     datetime.datetime.strptime(match.group('date'), ASSET_DT_FMT)
    #     for asset_id in asset_id_list
    #     for match in [asset_id_re.search(asset_id)] if match]

    logging.info('\nProcessing dates')
    for upload_dt in sorted(missing_dt_list):
        logging.info(f'{upload_dt.date()}')

        asset_id = f'{ASSET_COLL_ID}/{upload_dt.strftime(ASSET_DT_FMT)}'
        logging.debug(f'  {asset_id}')

        # Get the image IDs for the bracketing images
        asset_prev_dt = [d for d in asset_dt_list if d < upload_dt][-1]
        asset_next_dt = [d for d in asset_dt_list if d > upload_dt][0]
        # This approach only works for gaps of 1 day
        # asset_prev_dt = upload_dt - datetime.timedelta(days=1)
        # asset_next_dt = upload_dt + datetime.timedelta(days=1)

        asset_prev_id = f'{ASSET_COLL_ID}/{asset_prev_dt.strftime(ASSET_DT_FMT)}'
        asset_next_id = f'{ASSET_COLL_ID}/{asset_next_dt.strftime(ASSET_DT_FMT)}'
        logging.debug(f'    {asset_prev_id}')
        logging.debug(f'    {asset_next_id}')

        if overwrite_flag and upload_dt in asset_dt_list:
            logging.info('  Removing existing asset')
            try:
                ee.data.deleteAsset(asset_id)
            except Exception as e:
                logging.exception(f'  Exception: {e}')
        # if upload_dt in task_dt_list:
        #     # Eventually stop the export task

        # Added this flag to make it possible to easily remove all the
        #   interpolated images.  Set --overwrite and --no_ingest to trigger.
        if not ingest_flag:
            continue

        # Compute the missing image as the mean of the bracketing images
        output_img = ee.Image(asset_prev_id).select(variables)\
            .add(ee.Image(asset_next_id).select(variables)
                    .subtract(ee.Image(asset_prev_id).select(variables))\
                    .multiply((upload_dt - asset_prev_dt) /
                              (asset_next_dt - asset_prev_dt)))\
            .float()\
            .set({
                # 'system:index': asset_id_fmt.format(date=upload_dt.strftime(asset_dt_fmt)),
                'system:time_start': ee.Date(upload_dt.strftime('%Y-%m-%d')).millis(),
                'date_ingested': datetime.datetime.today().strftime('%Y-%m-%d'),
                'refet_version': '0.0.0',
                'source': 'interpolated',
            })

        # This approach only works for gaps of 1 day
        # # Compute the missing image as the mean of the bracketing images
        # output_img = ee.Image(asset_prev_id).select(variables)\
        #     .add(ee.Image(asset_next_id).select(variables))\
        #     .multiply(0.5)\
        #     .float()\
        #     .set({
        #         # 'system:index': asset_id_fmt.format(date=upload_dt.strftime(asset_dt_fmt)),
        #         'system:time_start': ee.Date(upload_dt.strftime('%Y-%m-%d')).millis(),
        #         'date_ingested': datetime.datetime.today().strftime('%Y-%m-%d'),
        #         'refet_version': '0.0.0',
        #     })

        try:
            task = ee.batch.Export.image.toAsset(
                image=output_img,
                description=f'cimis_interpolate_daily_{upload_dt.strftime(ASSET_DT_FMT)}',
                assetId=asset_id,
                dimensions=f'{asset_width}x{asset_height}',
                crs=asset_proj,
                crsTransform=asset_geo_str)
        except Exception as e:
            logging.debug(f'  Export task not built, skipping\n  {e}')
            continue

        logging.info('  Starting export task')
        ee_task_start(task, n=10)


def ee_task_start(task, n=10):
    """Make an exponential backoff Earth Engine request"""
    output = None
    for i in range(1, n):
        try:
            task.start()
            break
        except Exception as e:
            logging.info(f'    Resending query ({i}/10)')
            logging.debug(f'    {e}')
            time.sleep(i ** 2)
    return task


def get_ee_assets(asset_id, start_dt=None, end_dt=None):
    """Return assets IDs in a collection

    Parameters
    ----------
    asset_id : str
        A folder or image collection ID.
    start_dt : datetime, optional
        Start date (inclusive).
    end_dt : datetime, optional
        End date (exclusive, similar to .filterDate()).

    Returns
    -------
    list : Asset IDs

    """
    params = {'parent': asset_id}
    if start_dt and end_dt:
        # CGM - Do both start and end need to be set to apply filtering?
        params['startTime'] = start_dt.isoformat() + '.000000000Z'
        params['endTime'] = end_dt.isoformat() + '.000000000Z'
    asset_id_list = []
    for i in range(1, 6):
        try:
            asset_id_list = [x['id'] for x in ee.data.listImages(params)['images']]
            break
        except ValueError:
            logging.info('  Collection or folder doesn\'t exist')
            raise sys.exit()
        except Exception as e:
            logging.error(f'  Error getting asset list, retrying ({i}/6)\n  {e}')
            time.sleep(i ** 2)
    return asset_id_list


# def get_ee_assets(asset_id):
#     """Return assets IDs in a collection
#
#     Parameters
#     ----------
#     asset_id : str
#         A folder or image collection ID.
#
#     Returns
#     -------
#     list : Asset IDs
#
#     """
#     asset_id_list = []
#     for i in range(1, 6):
#         try:
#             asset_id_list = ee.data.getList({'id': asset_id})
#             asset_id_list = [x['id'] for x in asset_id_list
#                              if x['type'] == 'Image']
#             break
#         except Exception as e:
#             logging.error(f'  Error getting asset list, retrying ({i}/10)\n  {e}')
#             time.sleep(i ** 2)
#         except ValueError:
#             logging.info('  Collection or folder doesn\'t exist')
#             raise sys.exit()
#     return asset_id_list


# def get_ee_tasks(states=['RUNNING', 'READY']):
#     """Return current active tasks
#
#     Parameters
#     ----------
#     states : list
#
#     Returns
#     -------
#     dict : Task descriptions (key) and task IDs (value).
#
#     """
#
#     logging.debug('  Active Tasks')
#     tasks = {}
#     for i in range(1, 6):
#         try:
#             task_list = ee.data.getTaskList()
#             task_list = sorted([
#                 [t['state'], t['description'], t['id']]
#                 for t in task_list if t['state'] in states])
#             tasks = {t_desc: t_id for t_state, t_desc, t_id in task_list}
#             break
#         except Exception as e:
#             logging.info(
#                 '  Error getting active task list, retrying ({}/10)\n'
#                 '  {}'.format(i, e))
#             sleep(i ** 2)
#     return tasks


def arg_valid_file(file_path):
    """Argparse specific function for testing if file exists

    Convert relative paths to absolute paths
    """
    if os.path.isfile(os.path.abspath(os.path.realpath(file_path))):
        return os.path.abspath(os.path.realpath(file_path))
        # return file_path
    else:
        raise argparse.ArgumentTypeError(f'{file_path} does not exist')


def arg_parse():
    """"""
    parser = argparse.ArgumentParser(
        description='Ingest CIMIS daily data into Earth Engine',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        '-v', '--variables', nargs='+', metavar='VAR',
        default=VARIABLES, choices=VARIABLES, help='CIMIS daily variables')
    parser.add_argument(
        '--overwrite', default=False, action='store_true',
        help='Force overwrite of existing files')
    # The default values shows up as True for these which is confusing
    parser.add_argument(
        '--no-ingest', action='store_false', dest='ingest',
        help='Don\'t ingest images into Earth Engine')
    parser.add_argument(
        '--key', type=arg_valid_file, metavar='FILE',
        help='JSON key file')
    parser.add_argument(
        '--debug', default=logging.INFO, const=logging.DEBUG,
        help='Debug level logging', action='store_const', dest='loglevel')
    args = parser.parse_args()

    return args


if __name__ == '__main__':
    args = arg_parse()
    logging.basicConfig(level=args.loglevel, format='%(message)s')

    main(variables=args.variables, overwrite_flag=args.overwrite,
         gee_key_file=args.key, ingest_flag=args.ingest,
    )
