import argparse
from datetime import datetime, timedelta, timezone
import gzip
# import logging
import os
import pprint
import re
import shutil
import sys
import time

import ee
from flask import abort, Response
from google.cloud import storage
import numpy as np
import rasterio
import rasterio.warp
import refet
import requests
from scipy import ndimage

ASSET_COLL_ID = 'projects/openet/assets/reference_et/california/gridet/daily/v0'
ASSET_DT_FMT = '%Y%m%d'
BUCKET_NAME = 'openet'
BUCKET_FOLDER = 'cimis/daily'
# FUNCTION_URL = 'https://us-central1-openet.cloudfunctions.net'
# FUNCTION_NAME = 'gridet-reference-et-daily-v1-worker'
PROJECT_NAME = 'openet'
STORAGE_CLIENT = storage.Client(project=PROJECT_NAME)
TODAY_DT = datetime.today()
# TODAY_DT = datetime.now(timezone=timezone.utc)
VARIABLES = ['eto', 'etr']

if 'FUNCTION_REGION' in os.environ:
    # Logging is not working correctly in cloud functions for Python 3.8+
    # Following workflow suggested in this issue:
    # https://issuetracker.google.com/issues/124403972
    import google.cloud.logging
    log_client = google.cloud.logging.Client(project=PROJECT_NAME)
    log_client.setup_logging(log_level=20)
    import logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)
else:
    import logging
    # logging.basicConfig(level=logging.INFO, format='%(message)s')
    logging.getLogger('earthengine-api').setLevel(logging.INFO)
    logging.getLogger('googleapiclient').setLevel(logging.ERROR)
    logging.getLogger('requests').setLevel(logging.INFO)
    logging.getLogger('rasterio').setLevel(logging.INFO)
    logging.getLogger('urllib3').setLevel(logging.INFO)

if 'FUNCTION_REGION' in os.environ:
    # Assume code is deployed to a cloud function
    logging.debug(f'\nInitializing GEE using application default credentials')
    import google.auth
    credentials, project_id = google.auth.default(
        default_scopes=['https://www.googleapis.com/auth/earthengine']
    )
    ee.Initialize(credentials)
else:
    ee.Initialize()


def gridet_daily_asset_ingest(tgt_dt, variables, workspace='/tmp', overwrite_flag=False):
    """Ingest GridET daily data into Earth Engine

    Parameters
    ----------
    tgt_dt : datetime
    variables : list
        Variables to process.  Choices are: 'eto', 'etr'.
    workspace : str
    overwrite_flag : bool, optional

    Returns
    -------
    str : response string

    """
    tgt_date = tgt_dt.strftime('%Y-%m-%d')
    logging.info(f'Ingest GridET daily asset - {tgt_date}')
    # response = f'Ingest GridET daily asset - {tgt_date}\n'

    date_ws = os.path.join(workspace, tgt_date)
    tif_name = f'{tgt_dt.strftime(ASSET_DT_FMT)}.tif'
    upload_path = os.path.join(date_ws, tif_name)
    bucket_path = f'gs://{BUCKET_NAME}/{BUCKET_FOLDER}/{tif_name}'
    asset_id = f'{ASSET_COLL_ID}/{tgt_dt.strftime(ASSET_DT_FMT)}'
    logging.debug(f'  {upload_path}')
    logging.debug(f'  {bucket_path}')
    logging.debug(f'  {asset_id}')

    input_names = {
        'eto': 'Grass',
        'etr': 'Alfalfa',
    }
    var_units = {
        'eto': 'mm',
        'etr': 'mm',
    }

    # Check that user defined variables are valid and in CIMIS
    gz_variables = {}
    for v in variables:
        try:
            gz_variables[v] = gz_vars[v]
        except:
            logging.error(f'Unsupported variable: {v}')

    # Double check if the asset already exists
    if not overwrite_flag and ee.data.getInfo(asset_id):
        return f'{tgt_date} - Asset already exists and overwrite is False\n'

    # Always overwrite temporary files if the asset doesn't exist
    if os.path.isdir(date_ws):
        shutil.rmtree(date_ws)
    if not os.path.isdir(date_ws):
        os.makedirs(date_ws)


    # GridET grid and spatial reference
    logging.debug('\nGridET')
    asset_height = 1394
    asset_width = 1111
    asset_extent = (667040.0, 5758720.0, 2622400.0, 8212160.0)
    asset_cs = 1760.0
    asset_geo = (asset_cs, 0., asset_extent[0], 0., -asset_cs, asset_extent[3])
    # asset_geo = (asset_extent[0], asset_cs, 0., asset_extent[3], 0., -asset_cs)
    logging.debug(f'Shape:  {asset_width}, {asset_height} (w, h)')
    logging.debug(f'Extent: {asset_extent}')
    logging.debug(f'Geo:    {asset_geo}')

    asset_wkt = (
        'PROJCS["NAD83(NSRS2007) / Utah Central (ftUS)",GEOGCS["NAD83",DATUM["North_American_Datum_1983",'
        'SPHEROID["GRS 1980",6378137,298.257222101004,AUTHORITY["EPSG","7019"]],AUTHORITY["EPSG","6269"]],'
        'PRIMEM["Greenwich",0],UNIT["Degree",0.0174532925199433]],'
        'PROJECTION["Lambert_Conformal_Conic_2SP"],'
        'PARAMETER["latitude_of_origin",38.3333333333333],'
        'PARAMETER["central_meridian",-111.5],'
        'PARAMETER["standard_parallel_1",39.0166666666667],'
        'PARAMETER["standard_parallel_2",40.65],'
        'PARAMETER["false_easting",1640416.66666667],'
        'PARAMETER["false_northing",6561666.66666667],'
        'UNIT["US survey foot",0.304800609601219],'
        'AXIS["Easting",EAST],'
        'AXIS["Northing",NORTH]]'
    )
    asset_proj = rasterio.crs.CRS.from_wkt(asset_wkt)
    logging.debug(f'CRS: {asset_proj}')


    # CGM - Process each variable sequentially to minimize space
    logging.debug('Reading variable arrays')
    daily_arrays = {}
    for variable in variables:
        logging.debug(f'{variable}')
        input_name = f'{input_names[variable]}_{tgt_dt.strftime("%Y-%m-%d")}.img'
        input_path = os.path.join(date_ws, input_name)
        # logging.debug(f'  {input_path}')

        input_ds = rasterio.open(input_path, 'r')
        input_array = input_ds.read(1)
        input_array[input_array == input_ds.nodata] = np.nan
        input_ds.close()

        daily_arrays[variable] = input_array

        del input_array

    # Only build the composite if all the input images are available
    input_vars = set(daily_arrays.keys())
    if not set(variables).issubset(input_vars):
        return f'{tgt_date} - Missing input variables for composite\n'\
               f'  {", ".join(list(set(variables) - input_vars))}'

    logging.debug('\nBuilding output GeoTIFF')
    output_ds = rasterio.open(
        upload_path, 'w',
        driver='COG', blocksize=256, compress='deflate',
        # driver='GTiff', compress='lzw', tiled=True, blockxsize=256, blockysize=256,
        nodata=-9999, count=len(variables), dtype=rasterio.float32,
        height=asset_height, width=asset_width,
        crs=asset_proj, transform=asset_geo,
    )

    logging.debug('\nWriting arrays to output GeoTIFF')
    for band_i, variable in enumerate(variables):
        # logging.debug(f'  {variable}')
        output_ds.set_band_description(band_i + 1, variable)
        data_array = daily_arrays[variable].astype(np.float32)
        data_array[np.isnan(data_array)] = -9999
        output_ds.write(data_array, band_i + 1)
        del data_array
        output_ds.build_overviews(OVERVIEW_LEVELS, rasterio.warp.Resampling.average)
        output_ds.update_tags(ns='rio_overview', resampling='average')
        output_ds.close()

    # output_ds.close()
    del output_ds

    # # Build overviews
    # dst = rasterio.open(upload_path, 'r+')
    # dst.build_overviews([2, 4, 8], rasterio.warp.Resampling.average)
    # dst.update_tags(ns='rio_overview', resampling='average')
    # dst.close()


    logging.debug('\nUploading to bucket')
    bucket = STORAGE_CLIENT.bucket(BUCKET_NAME)
    blob = bucket.blob(f'{BUCKET_FOLDER}/{os.path.basename(bucket_path)}')
    # blob = bucket.blob(os.path.basename(bucket_path))
    blob.upload_from_filename(upload_path)


    # DEADBEEF - For now, assume the file is in the bucket
    logging.debug('\nIngesting into Earth Engine')
    task_id = ee.data.newTaskId()[0]
    logging.debug(f'  {task_id}')

    properties = {
        'build_date': f'{TODAY_DT.strftime("%Y-%m-%d")}',
        'version': '2024.06',
        # 'source': SOURCE_URL.replace('https://', '').replace('http://', ''),
    }
    for v in variables:
        if (v in var_units.keys()) and var_units[v]:
            properties[f'units_{v}'] = var_units[v]

    # NOTE: The band names are being forced to lower case here
    params = {
        'name': asset_id,
        'bands': [
            {'id': v, 'tilesetId': 'image', 'tilesetBandIndex': i}
            for i, v in enumerate(variables)
        ],
        'tilesets': [{'id': 'image', 'sources': [{'uris': [bucket_path]}]}],
        'properties': properties,
        'startTime': tgt_dt.isoformat() + '.000000000Z',
        # 'pyramiding_policy': 'MEAN',
        # 'missingData': {'values': [nodata_value]},
    }

    # TODO: Wrap in a try/except loop
    ee.data.startIngestion(task_id, params, allow_overwrite=True)

    if os.path.isdir(date_ws):
        shutil.rmtree(date_ws)

    return f'{tgt_date} - {asset_id}\n'


def gridet_daily_asset_dates(start_dt, end_dt, overwrite_flag=False):
    """Identify dates of missing CIMIS daily assets

    Parameters
    ----------
    start_dt : datetime
    end_dt : datetime
    overwrite_flag : bool, optional

    Returns
    -------
    list : datetimes

    """
    logging.debug('\nBuilding CIMIS daily asset ingest date list')
    logging.debug(f'  {start_dt.strftime("%Y-%m-%d")}')
    logging.debug(f'  {end_dt.strftime("%Y-%m-%d")}')

    task_id_re = re.compile(ASSET_COLL_ID.split('projects/')[-1] + '/(?P<date>\d{8})$')
    asset_id_re = re.compile(ASSET_COLL_ID.split('projects/')[-1] + '/(?P<date>\d{8})$')

    # Figure out which asset dates need to be ingested
    # Start with a list of dates to check
    # logging.debug('\nBuilding Date List')
    tgt_dt_list = list(date_range(start_dt, end_dt, skip_leap_days=False))
    if not tgt_dt_list:
        logging.info('Empty date range')
        return []
    logging.debug('\nInitial test dates: {}'.format(
        ', '.join(map(lambda x: x.strftime('%Y-%m-%d'), tgt_dt_list))
    ))

    # Check if any of the needed dates are currently being ingested
    # Check task list before checking asset list in case a task switches
    #   from running to done before the asset list is retrieved.
    logging.debug('\nChecking task list')
    task_id_list = [
        desc.replace('\nAsset ingestion: ', '')
        for desc in get_ee_tasks(states=['RUNNING', 'READY']).keys()
    ]
    task_dates = {
        datetime.strptime(m.group('date'), ASSET_DT_FMT).strftime('%Y-%m-%d')
        for task_id in task_id_list for m in [task_id_re.search(task_id)] if m
    }
    # logging.debug(f'\nTask dates: {", ".join(sorted(task_dates))}')

    # Switch date list to be dates that are missing
    tgt_dt_list = [
        dt for dt in tgt_dt_list
        if overwrite_flag or dt.strftime('%Y-%m-%d') not in task_dates
    ]
    if not tgt_dt_list:
        logging.info('No dates to process after checking ready/running tasks')
        return []
    logging.debug('\nDates (after filtering tasks): {}'.format(
        ', '.join(map(lambda x: x.strftime('%Y-%m-%d'), tgt_dt_list))
    ))

    # TODO: Check "source" parameter for images that were generated by interpolating

    # Check if the assets already exist
    # For now, assume the collection exists
    logging.debug('\nChecking existing assets')
    asset_id_list = get_ee_assets(
        ASSET_COLL_ID, start_dt, end_dt + timedelta(days=1)
    )
    asset_dates = {
        datetime.strptime(m.group('date'), ASSET_DT_FMT).strftime('%Y-%m-%d')
        for asset_id in asset_id_list for m in [asset_id_re.search(asset_id)] if m
    }
    logging.debug(f'\nAsset dates: {", ".join(sorted(asset_dates))}')

    # Switch date list to be dates that are missing
    tgt_dt_list = [
        dt for dt in tgt_dt_list
        if overwrite_flag or dt.strftime('%Y-%m-%d') not in asset_dates
    ]
    if not tgt_dt_list:
        logging.info('No dates to process after filtering existing assets')
        return []
    logging.debug('\nDates (after filtering existing assets): {}'.format(
        ', '.join(map(lambda x: x.strftime('%Y-%m-%d'), tgt_dt_list))
    ))

    # Check if the folders exist on the server
    # CGM - Most of the time there will only be one date in this list,
    #   so it is probably okay to make a separate call for each date
    # If it was expected that large date ranges will be missing, then it might
    #   make more sense to make calls to check the year and month first
    if len(tgt_dt_list) <= 64:
        logging.debug('\nChecking server folders')
        # for date_fmt in ['%Y/', '%Y/%m/', '%Y/%m/%d/']:
        for date_fmt in ['%Y/%m/%d/']:
            test_date_list = sorted(list(set(
                tgt_dt.strftime(date_fmt) for tgt_dt in tgt_dt_list)))
            for test_date in test_date_list:
                logging.debug(f'{SOURCE_URL}/{test_date}')
                date_response = requests.get(f'{SOURCE_URL}/{test_date}', timeout=10)
                if date_response.status_code != 200:
                    logging.info(f'  {test_date} - Folder does not exist, removing dates')
                    tgt_dt_list = [tgt_dt for tgt_dt in tgt_dt_list
                                   if tgt_dt.strftime(date_fmt) != test_date]

    if not tgt_dt_list:
        logging.info('No dates to process after checking server folders')
        return []
    logging.debug('\nIngest dates: {}'.format(
        ', '.join(map(lambda x: x.strftime('%Y-%m-%d'), tgt_dt_list))
    ))

    return tgt_dt_list


def cron_worker(request):
    """Parse JSON/request arguments and start ingest for a single date export"""
    args = {
        'variables': VARIABLES,
        'workspace': '/tmp',
    }

    request_json = request.get_json(silent=True)
    request_args = request.args

    if request_json and 'date' in request_json:
        tgt_date = request_json['date']
    elif request_args and 'date' in request_args:
        tgt_date = request_args['date']
    else:
        abort(400, description='date parameter not set')

    try:
        args['tgt_dt'] = datetime.strptime(tgt_date, '%Y-%m-%d')
    except:
        abort(400, description=f'date "{tgt_date}" could not be parsed')

    if request_json and 'overwrite' in request_json:
        overwrite_flag = request_json['overwrite']
    elif request_args and 'overwrite' in request_args:
        overwrite_flag = request_args['overwrite']
    else:
        overwrite_flag = 'false'

    if overwrite_flag.lower() in ['true', 't']:
        args['overwrite_flag'] = True
    elif overwrite_flag.lower() in ['false', 'f']:
        args['overwrite_flag'] = False
    else:
        abort(400, description=f'overwrite="{overwrite_flag}" could not be parsed')

    response = gridet_daily_asset_ingest(**args)
    return Response(response, mimetype='text/plain')


def date_range(start_dt, end_dt, days=1, skip_leap_days=False):
    """Generate dates within a range (inclusive)

    Parameters
    ----------
    start_dt : datetime
        Start date.
    end_dt : datetime
        End date (inclusive).
    days : int, optional
        Step size (the default is 1).
    skip_leap_days : bool, optional
        If True, skip leap days while incrementing (the default is True).

    Yields
    ------
    datetime

    """
    import copy
    curr_dt = copy.copy(start_dt)
    while curr_dt <= end_dt:
        if not skip_leap_days or curr_dt.month != 2 or curr_dt.day != 29:
            yield curr_dt
        curr_dt += timedelta(days=days)


def get_ee_assets(asset_id, start_dt=None, end_dt=None, retries=4):
    """Return assets IDs in a collection

    Parameters
    ----------
    asset_id : str
        A folder or image collection ID.
    start_dt : datetime, optional
        Start date (inclusive).
    end_dt : datetime, optional
        End date (exclusive, similar to .filterDate()).
    retries : int, optional
        The number of times to retry the call (the default is 4).

    Returns
    -------
    list : Asset IDs

    """
    # # CGM - There is a bug in earthengine-api>=0.1.326 that causes listImages()
    # #   to return an empty list if the startTime and endTime parameters are set
    # # Switching to a .aggregate_array(system:index).getInfo() approach for now
    # #   since getList is flagged for deprecation
    coll = ee.ImageCollection(asset_id)
    if start_dt and end_dt:
        coll = coll.filterDate(start_dt.strftime('%Y-%m-%d'), end_dt.strftime('%Y-%m-%d'))
    # params = {'parent': asset_id}
    # if start_dt and end_dt:
    #     # CGM - Do both start and end need to be set to apply filtering?
    #     params['startTime'] = start_dt.isoformat() + '.000000000Z'
    #     params['endTime'] = end_dt.isoformat() + '.000000000Z'

    asset_id_list = []
    for i in range(1, retries):
        try:
            asset_id_list = coll.aggregate_array('system:index').getInfo()
            asset_id_list = [f'{asset_id}/{id}' for id in asset_id_list]
            # asset_id_list = [x['id'] for x in ee.data.listImages(params)['images']]
            break
        except ValueError:
            logging.info('  Collection or folder does not exist')
            raise sys.exit()
        except Exception as e:
            logging.error(f'  Error getting asset list, retrying ({i}/{retries})\n  {e}')
            time.sleep(i ** 3)

    return asset_id_list


def get_ee_tasks(states=['RUNNING', 'READY'], verbose=False, retries=4):
    """Return current active tasks

    Parameters
    ----------
    states : list, optional
        List of task states to check (the default is ['RUNNING', 'READY']).
    verbose : bool, optional
        This parameter is deprecated and is no longer being used.
        To get verbose logging of the active tasks use utils.print_ee_tasks().
    retries : int, optional
        The number of times to retry getting the task list if there is an error.

    Returns
    -------
    dict : task descriptions (key) and full task info dictionary (value)

    """
    logging.debug('\nRequesting Task List')
    task_list = None
    for i in range(1, retries):
        try:
            # TODO: getTaskList() is deprecated, switch to listOperations()
            task_list = ee.data.getTaskList()
            # task_list = ee.data.listOperations()
            break
        except Exception as e:
            logging.warning(f'  Error getting task list, retrying ({i}/{retries})\n  {e}')
            time.sleep(i ** 3)
    if task_list is None:
        raise Exception('\nUnable to retrieve task list, exiting')

    task_list = sorted(
        [task for task in task_list if task['state'] in states],
        key=lambda t: (t['state'], t['description'], t['id'])
    )
    # task_list = sorted([
    #     [t['state'], t['description'], t['id']] for t in task_list
    #     if t['state'] in states
    # ])

    # Convert the task list to a dictionary with the task name as the key
    return {task['description']: task for task in task_list}


# def get_info(ee_obj, max_retries=4):
#     """Make an exponential back off getInfo call on an Earth Engine object"""
#     # output = ee_obj.getInfo()
#     output = None
#     for i in range(1, max_retries):
#         try:
#             output = ee_obj.getInfo()
#         except ee.ee_exception.EEException as e:
#             if ('Earth Engine memory capacity exceeded' in str(e) or
#                     'Earth Engine capacity exceeded' in str(e) or
#                     'Too many concurrent aggregations' in str(e) or
#                     'Computation timed out.' in str(e)):
#                 # TODO: Maybe add 'Connection reset by peer'
#                 logging.info(f'    Resending query ({i}/{max_retries})')
#                 logging.info(f'    {e}')
#             else:
#                 # TODO: What should happen for unhandled EE exceptions?
#                 logging.info('    Unhandled Earth Engine exception')
#                 logging.info(f'    {e}')
#         except Exception as e:
#             logging.info(f'    Resending query ({i}/{max_retries})')
#             logging.debug(f'    {e}')
#
#         if output:
#             break
#         else:
#             time.sleep(i ** 3)
#
#     return output


def arg_valid_date(input_date):
    """Check that a date string is ISO format (YYYY-MM-DD)

    This function is used to check the format of dates entered as command
      line arguments.
    DEADBEEF - It would probably make more sense to have this function
      parse the date using dateutil parser (http://labix.org/python-dateutil)
      and return the ISO format string

    Parameters
    ----------
    input_date : string

    Returns
    -------
    datetime

    Raises
    ------
    ArgParse ArgumentTypeError

    """
    try:
        return datetime.strptime(input_date, '%Y-%m-%d')
    except ValueError:
        raise argparse.ArgumentTypeError(f'Not a valid date: "{input_date}"')


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
        description='Ingest CIMIS daily assets into Earth Engine',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        '--workspace', metavar='PATH',
        default=os.path.dirname(os.path.abspath(__file__)),
        help='Set the current working directory')
    parser.add_argument(
        '--start', type=arg_valid_date, metavar='DATE', default='1979-01-02',
        # default=(TODAY_DT - timedelta(days=START_DAY_OFFSET)).strftime('%Y-%m-%d'),
        help='Start date (format YYYY-MM-DD)')
    parser.add_argument(
        '--end', type=arg_valid_date, metavar='DATE', default='2023-12-31',
        # default=(TODAY_DT - timedelta(days=END_DAY_OFFSET)).strftime('%Y-%m-%d'),
        help='End date (format YYYY-MM-DD)')
    parser.add_argument(
        '-v', '--variables', nargs='+', default=VARIABLES,
        choices=VARIABLES, metavar='VAR',
        help='CIMIS daily variables')
    parser.add_argument('--project', type=str, help='Earth Engine project ID')
    # parser.add_argument(
    #     '--key', type=arg_valid_file, metavar='FILE',
    #     help='Earth Engine service account JSON key file')
    parser.add_argument(
        '--overwrite', default=False, action='store_true',
        help='Force overwrite of existing files')
    parser.add_argument(
        '--reverse', default=False, action='store_true',
        help='Process dates in reverse order')
    parser.add_argument(
        '--debug', default=logging.INFO, const=logging.DEBUG,
        help='Debug level logging', action='store_const', dest='loglevel')
    args = parser.parse_args()

    # Convert relative paths to absolute paths
    if args.workspace and os.path.isdir(os.path.abspath(args.workspace)):
        args.workspace = os.path.abspath(args.workspace)

    return args


if __name__ == '__main__':
    args = arg_parse()
    logging.basicConfig(level=args.loglevel, format='%(message)s')

    # # if args.key and 'FUNCTION_REGION' not in os.environ:
    # if args.key:
    #     logging.info(f'\nInitializing GEE using user key file: {args.key}')
    #     try:
    #         ee.Initialize(ee.ServiceAccountCredentials('_', key_file=args.key))
    #     except ee.ee_exception.EEException:
    #         raise Exception('Unable to initialize GEE using user key file')
    # else:
    #     logging.info('\nInitializing Earth Engine using user credentials')
    #     ee.Initialize()

    # # Build the image collection if it doesn't exist
    # logging.debug(f'Image Collection: {ASSET_COLL_ID}')
    # if not ee.data.getInfo(ASSET_COLL_ID):
    #     logging.info('\nImage collection does not exist and will be built'
    #                  '\n  {}'.format(ASSET_COLL_ID))
    #     input('Press ENTER to continue')
    #     ee.data.createAsset({'type': 'IMAGE_COLLECTION'}, ASSET_COLL_ID)

    ingest_dt_list = gridet_daily_asset_dates(
        args.start, args.end, overwrite_flag=args.overwrite
    )
    logging.info(ingest_dt_list)

    for ingest_dt in sorted(ingest_dt_list, reverse=args.reverse):
        response = gridet_daily_asset_ingest(
            ingest_dt, variables=args.variables, workspace=args.workspace,
            overwrite_flag=args.overwrite
        )
        logging.info(f'  {response}')
