import argparse
from calendar import monthrange
from datetime import datetime, timedelta, timezone
import logging
import os
import re
import time

from dateutil.relativedelta import relativedelta
import ee
from flask import abort, Response

PROJECT_NAME = 'openet'
ASSET_COLL_ID = 'projects/earthengine-legacy/assets/' \
                'projects/openet/reference_et/cimis/monthly'
SOURCE_COLL_ID = 'projects/earthengine-legacy/assets/' \
                 'projects/openet/reference_et/cimis/daily'
ASSET_DT_FMT = '%Y%m'
START_MONTH_OFFSET = 1
END_MONTH_OFFSET = 0
TODAY_DT = datetime.today()
# TODAY_DT = datetime.now(timezone=timezone.utc)
INPUT_BANDS = ['eto', 'eto_asce', 'etr_asce']
OUTPUT_BANDS = ['eto', 'eto_asce', 'etr_asce']

if 'FUNCTION_REGION' in os.environ:
    # Logging is not working correctly in cloud functions for Python 3.8+
    # Following workflow suggested in this issue:
    # https://issuetracker.google.com/issues/124403972
    import google.cloud.logging
    log_client = google.cloud.logging.Client(project=PROJECT_NAME)
    log_client.setup_logging(log_level=20)
    import logging
    # CGM - Not sure if these lines are needed or not
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)
else:
    import logging
    # logging.basicConfig(level=logging.INFO, format='%(message)s')
    logging.getLogger('earthengine-api').setLevel(logging.INFO)
    logging.getLogger('googleapiclient').setLevel(logging.ERROR)
    logging.getLogger('requests').setLevel(logging.INFO)
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


def cimis_monthly_ingest(tgt_dt, overwrite_flag=False):
    """

    Parameters
    ----------
    tgt_dt : datetime
    overwrite_flag : bool, optional

    Returns
    -------
    str : response string

    """

    # tgt_date = tgt_dt.strftime('%Y%m%d')
    logging.info(f'CIMIS Monthly Reference ET - {tgt_dt.strftime("%Y-%m-%d")}')
    # response = f'CIMIS Monthly Reference ET - {tgt_dt.strftime("%Y-%m")}'

    asset_id = f'{ASSET_COLL_ID}/{tgt_dt.strftime(ASSET_DT_FMT)}'
    export_name = f'cimis_reference_et_monthly_{tgt_dt.strftime("%Y%m%d")}'
    logging.debug(f'  {SOURCE_COLL_ID}')
    logging.debug(f'  {asset_id}')
    logging.debug(f'  {export_name}')

    if ee.data.getInfo(asset_id):
        if overwrite_flag:
            try:
                ee.data.deleteAsset(asset_id)
            except Exception as e:
                return f'{export_name} - An error occured while trying to '\
                       f'delete the existing asset, skipping\n{e}\n'
        else:
            return f'{export_name} - The asset already exists and overwrite '\
                   f'is False, skipping\n'

    asset_extent = (-410000.0, -660000.0, 610000.0, 460000.0)
    asset_cs = 2000.0
    asset_geo = (asset_cs, 0., asset_extent[0], 0., -asset_cs, asset_extent[3])
    # asset_geo = [2000, 0, -410000.0, 0, -2000, 460000]
    asset_height = 560
    asset_width = 510
    asset_geo_str = '[' + ','.join(list(map(str, asset_geo))) + ']'

    asset_crs = ee.ImageCollection(SOURCE_COLL_ID).first().projection()
    # asset_crs = get_info(ee.ImageCollection(SOURCE_COLL_ID).first().projection())['wkt']
    # asset_proj4 = (
    #     '+proj=aea +lat_1=34 +lat_2=40.5 +lat_0=0 +lon_0=-120 ' +
    #     '+x_0=0 +y_0=-4000000 +ellps=GRS80 +datum=NAD83 +units=m +no_defs')
    # asset_proj = rasterio.crs.CRS.from_proj4(
    #         '+proj=aea +lat_1=34 +lat_2=40.5 +lat_0=0 +lon_0=-120 ' +
    #         '+x_0=0 +y_0=-4000000 +ellps=GRS80 +datum=NAD83 +units=m +no_defs')
    # asset_proj = 'EPSG:3310'  # NAD_1983_California_Teale_Albers

    source_coll = (
        ee.ImageCollection(SOURCE_COLL_ID)
        .filterDate(tgt_dt, tgt_dt + relativedelta(months=1))
        .select(INPUT_BANDS)
    )

    # TODO: Decide if the scheduler should be responsible for checking if there
    #   are enough source images
    # TODO: Wrap getInfo call in a try/except loop
    source_count = get_info(source_coll.size())
    if source_count != monthrange(tgt_dt.year, tgt_dt.month)[1]:
        return f'{export_name} - too few source images ({source_count}) for month\n'

    properties = {
        'date_ingested': TODAY_DT.strftime('%Y-%m-%d'),
        # 'scale_factor': 1.0,
        # TODO: Check if these property names should be changed to match the band names
        'scale_factor_et_reference': 1.0,
        'scale_factor_eto': 1.0,
        'scale_factor_etr': 1.0,
        'source_bands': ','.join(INPUT_BANDS),
        # CGM: Should we use the UTC 0 time_start or the CIMIS time_start?
        'system:time_start': ee.Date(tgt_dt.strftime('%Y-%m-%d')).millis(),
        'system:index': tgt_dt.strftime(ASSET_DT_FMT),
    }

    output_img = source_coll.sum().rename(OUTPUT_BANDS).set(properties)

    export_task = ee.batch.Export.image.toAsset(
        image=output_img,
        description=export_name,
        assetId=asset_id,
        dimensions=f'{asset_width}x{asset_height}',
        crs=asset_crs,
        crsTransform=asset_geo_str,
    )

    # TODO: Wrap in try/except loop
    # Start the export task
    export_task.start()

    # # Try to start the task a couple of times
    # for i in range(1, 6):
    #     try:
    #         export_task.start()
    #         break
    #     except ee.ee_exception.EEException as e:
    #         logging.warning('EE Exception, retry {}\n{}'.format(i, e))
    #     except Exception as e:
    #         logging.warning('Unhandled Exception: {}'.format(e))
    #         return 'Unhandled Exception: {}'.format(e)
    #     time.sleep(i ** 2)

    return f'{export_name} - {export_task.id}\n'


def cron_scheduler(request):
    """Responds to any HTTP request.

    Parameters
    ----------
    request (flask.Request): HTTP request object.

    Returns
    -------
    The response text or any set of values that can be turned into a
    Response object using
    `make_response <http://flask.pocoo.org/docs/1.0/api/#flask.Flask.make_response>`.

    """
    response = 'Generate Monthly CIMIS Reference ET Images\n'

    request_json = request.get_json(silent=True)
    request_args = request.args

    # Default start and end date to None if not set
    if request_json and 'start' in request_json:
        start_date = request_json['start']
    elif request_args and 'start' in request_args:
        start_date = request_args['start']
    else:
        start_date = None

    if request_json and 'end' in request_json:
        end_date = request_json['end']
    elif request_args and 'end' in request_args:
        end_date = request_args['end']
    else:
        end_date = None

    if not start_date and not end_date:
        start_dt = (datetime(TODAY_DT.year, TODAY_DT.month, 1) -
                    relativedelta(months=START_MONTH_OFFSET))
        end_dt = (datetime(TODAY_DT.year, TODAY_DT.month, 1) -
                  relativedelta(days=1) - relativedelta(days=END_MONTH_OFFSET))
    elif start_date and end_date:
        # Only process custom range if start and end are both set
        # Limit the end date to the last full month date
        try:
            start_dt = datetime.strptime(start_date, '%Y-%m-%d')
            end_dt = datetime.strptime(end_date, '%Y-%m-%d')
        except ValueError as e:
            response = 'Error parsing start and/or end date\n'
            response += str(e)
            abort(404, description=response)

        # Force end date to be last day of previous month
        end_dt = min(end_dt, TODAY_DT - timedelta(days=1))

        # TODO: Force start date to be at least one month before end
        # start_dt = min(
        #     start_dt,
        #     end_dt - relativedelta(months=1) + relativedelta(days=1))

        if start_dt > end_dt:
            abort(404, description='Start date must be before end date')
        # elif (end_dt - start_dt) > timedelta(days=400):
        #     abort(404, description='No more than 1 year can be processed in a single request')
        # if start_dt < datetime(1980, 1, 1):
        #     logging.debug('Start Date: {} - no CIMIS images before '
        #                   '1980-01-01'.format(start_dt.strftime('%Y-%m-%d')))
        #     start_dt = datetime(1980, 1, 1)
    else:
        abort(404, description='Both start and end date must be specified')
    response += 'Start Date: {}\n'.format(start_dt.strftime('%Y-%m-%d'))
    response += 'End Date:   {}\n'.format(end_dt.strftime('%Y-%m-%d'))

    args = {
        'start_dt': start_dt,
        'end_dt': end_dt,
    }

    for tgt_dt in cimis_monthly_dates(**args):
        logging.info(f'Date: {tgt_dt.strftime("%Y-%m-%d")}')
        # response += 'Date: {}\n'.format(tgt_dt.strftime('%Y-%m-%d'))
        response += cimis_monthly_ingest(tgt_dt, overwrite_flag=True)

    return Response(response, mimetype='text/plain')


def cimis_monthly_dates(start_dt, end_dt, overwrite_flag=False):
    """"""
    logging.debug('\nBuilding CIMIS monthly date list')
    logging.debug(f'  {start_dt.strftime("%Y-%m-%d")}')
    logging.debug(f'  {end_dt.strftime("%Y-%m-%d")}')

    task_id_re = re.compile('cimis_monthly_reference_et_(?P<date>\d{8})')
    # asset_id_re = re.compile(
    #     ASSET_COLL_ID.split('projects/')[-1] + '/(?P<date>\d{6})$'
    # )

    # Figure out which asset dates need to be ingested
    # Start with a list of dates to check
    # logging.debug('\nBuilding Date List')
    test_dt_list = list(month_range(start_dt, end_dt))
    if not test_dt_list:
        logging.info('Empty date range')
        return []
    # logging.info('\nTest dates: {}'.format(
    #     ', '.join(map(lambda x: x.strftime('%Y-%m-%d'), test_dt_list))
    # ))

    # Check if any of the needed dates are currently being ingested
    # Check task list before checking asset list in case a task switches
    #   from running to done before the asset list is retrieved.
    task_id_list = [
        desc.replace('\nAsset ingestion: ', '')
        for desc in get_ee_tasks(states=['RUNNING', 'READY']).keys()
    ]
    task_dates = {
        datetime.strptime(m.group('date'), '%Y%m%d').strftime('%Y-%m-%d')
        for task_id in task_id_list for m in [task_id_re.search(task_id)] if m
    }
    # logging.debug(f'\nTask dates: {", ".join(sorted(task_dates))}')

    # Switch date list to be dates that are missing
    test_dt_list = [
        dt for dt in test_dt_list
        if overwrite_flag or dt.strftime('%Y-%m-%d') not in task_dates
    ]
    if not test_dt_list:
        logging.info('All dates are queued for export')
        return []
    # else:
    #     logging.info('\nMissing asset dates: {}'.format(', '.join(
    #         map(lambda x: x.strftime('%Y-%m-%d'), test_dt_list)
    #     )))

    # Check if the assets already exist
    # For now, assume the collection exists
    # Bump end date for filterDate() calls
    logging.debug('\nChecking existing assets')
    filter_end_dt = end_dt + timedelta(days=1)
    asset_date_coll = (
        ee.ImageCollection(ASSET_COLL_ID)
        .filterDate(start_dt.strftime('%Y-%m-%d'), filter_end_dt.strftime('%Y-%m-%d'))
    )
    asset_dates = set(get_info(asset_date_coll.aggregate_array('system:index')))
    # asset_id_list = get_ee_assets(
    #     ASSET_COLL_ID, start_dt, end_dt + timedelta(days=1))
    # asset_date_list = [
    #     datetime.strptime(m.group('date'), ASSET_DT_FMT).strftime('%Y-%m-%d')
    #     for asset_id in asset_id_list
    #     for m in [asset_id_re.search(asset_id)] if m
    # ]
    logging.debug(f'\nAsset dates: {", ".join(sorted(asset_dates))}')

    # Switch date list to be dates that are missing
    test_dt_list = [
        dt for dt in test_dt_list
        if overwrite_flag or dt.strftime(ASSET_DT_FMT) not in asset_dates
    ]
    if not test_dt_list:
        logging.info('No dates to process after filtering existing assets')
        return []
    logging.debug('\nDates (after filtering existing assets): {}'.format(
        ', '.join(map(lambda x: x.strftime('%Y-%m-%d'), test_dt_list))
    ))

    # TODO: Should the source collection be checked here to see if there are
    #   enough images?

    return test_dt_list


def month_range(start_dt, end_dt):
    """Generate month dates within a range (inclusive)

    Parameters
    ----------
    start_dt : datetime
        Start date.
    end_dt : datetime
        End date.

    Yields
    ------
    datetime

    """
    import copy
    curr_dt = copy.copy(datetime(start_dt.year, start_dt.month, 1))
    while curr_dt <= end_dt:
        yield curr_dt
        curr_dt += relativedelta(months=1)


def get_ee_tasks(states=['RUNNING', 'READY'], verbose=False, retries=6):
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


def get_info(ee_obj, max_retries=4):
    """Make an exponential back off getInfo call on an Earth Engine object"""
    # output = ee_obj.getInfo()
    output = None
    for i in range(1, max_retries):
        try:
            output = ee_obj.getInfo()
        except ee.ee_exception.EEException as e:
            if ('Earth Engine memory capacity exceeded' in str(e) or
                    'Earth Engine capacity exceeded' in str(e) or
                    'Too many concurrent aggregations' in str(e) or
                    'Computation timed out.' in str(e)):
                # TODO: Maybe add 'Connection reset by peer'
                logging.info(f'    Resending query ({i}/{max_retries})')
                logging.info(f'    {e}')
            else:
                # TODO: What should happen for unhandled EE exceptions?
                logging.info('    Unhandled Earth Engine exception')
                logging.info(f'    {e}')
        except Exception as e:
            logging.info(f'    Resending query ({i}/{max_retries})')
            logging.debug(f'    {e}')

        if output:
            break
        else:
            time.sleep(i ** 3)

    return output


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
        return datetime.strptime(input_date, "%Y-%m-%d")
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
        description='Generate monthly CIMIS reference ET assets',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        '--start', type=arg_valid_date, metavar='DATE',
        default=(datetime(TODAY_DT.year, TODAY_DT.month, 1) -
                 relativedelta(months=START_MONTH_OFFSET)).strftime('%Y-%m-%d'),
        help='Start date (format YYYY-MM-DD)')
    parser.add_argument(
        '--end', type=arg_valid_date, metavar='DATE',
        default=(datetime(TODAY_DT.year, TODAY_DT.month, 1) -
                 relativedelta(days=1) -
                 relativedelta(months=END_MONTH_OFFSET)).strftime('%Y-%m-%d'),
        help='End date (format YYYY-MM-DD)')
    # parser.add_argument(
    #     '-v', '--variables', nargs='+', default=VARIABLES,
    #     choices=VARIABLES, metavar='VAR',
    #     help='CIMIS daily variables')
    parser.add_argument(
        '--key', type=arg_valid_file, metavar='FILE',
        help='Earth Engine service account JSON key file')
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

    return args


if __name__ == '__main__':
    args = arg_parse()
    logging.basicConfig(level=args.loglevel, format='%(message)s')

    # if args.key and 'FUNCTION_REGION' not in os.environ:
    if args.key:
        logging.info(f'\nInitializing GEE using user key file: {args.key}')
        try:
            ee.Initialize(ee.ServiceAccountCredentials('_', key_file=args.key))
        except ee.ee_exception.EEException:
            raise Exception('Unable to initialize GEE using user key file')
    else:
        logging.info('\nInitializing Earth Engine using user credentials')
        ee.Initialize()

    # Build the image collection if it doesn't exist
    logging.debug('Image Collection: {}'.format(ASSET_COLL_ID))
    if not ee.data.getInfo(ASSET_COLL_ID):
        logging.info('\nImage collection does not exist and will be built'
                     '\n  {}'.format(ASSET_COLL_ID))
        input('Press ENTER to continue')
        ee.data.createAsset({'type': 'IMAGE_COLLECTION'}, ASSET_COLL_ID)

    ingest_dt_list = cimis_monthly_dates(
        args.start, args.end, overwrite_flag=args.overwrite
    )

    for ingest_dt in sorted(ingest_dt_list, reverse=args.reverse):
        # logging.info(f'Date: {ingest_dt.strftime("%Y-%m-%d")}')
        response = cimis_monthly_ingest(ingest_dt,  overwrite_flag=args.overwrite)
        logging.info(f'  {response}')
