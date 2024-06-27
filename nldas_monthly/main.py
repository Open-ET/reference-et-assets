import argparse
from datetime import datetime, timedelta, timezone
# import logging
import os
import re
import time

from dateutil.relativedelta import relativedelta
import ee
from flask import abort, Response

# import openet.core.utils as utils

# # CONUS asset parameters
# ASSET_COLL_ID = 'projects/openet/assets/reference_et/conus/nldas/monthly/v0'
# SOURCE_COLL_ID = 'projects/openet/assets/reference_et/conus/nldas/daily/v0'
# ASSET_CRS = 'EPSG:4326'
# ASSET_GEO = [0.125, 0, -125, 0, -0.125, 53]
# ASSET_SHAPE = '464x224'

# Milk River asset parameters
ASSET_COLL_ID = 'projects/dri-milkriver/assets/reference_et/nldas/monthly'
SOURCE_COLL_ID = 'projects/dri-milkriver/assets/reference_et/nldas/daily'
ASSET_CRS = 'EPSG:4326'
ASSET_GEO = [0.125, 0, -116.5, 0, -0.125, 51.5]
ASSET_SHAPE = '105x44'

ASSET_DT_FMT = '%Y%m'
PROJECT_NAME = 'openet'
VERSION = 'v0'
START_MONTH_OFFSET = 3
END_MONTH_OFFSET = 0
TODAY_DT = datetime.today()
# TODAY_DT = datetime.now(timezone=timezone.utc)


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
    logging.getLogger('urllib3').setLevel(logging.INFO)

if 'FUNCTION_REGION' in os.environ:
    # Assume code is deployed to a cloud function
    logging.debug(f'\nInitializing GEE using application default credentials')
    import google.auth
    credentials, project_id = google.auth.default(
        default_scopes=['https://www.googleapis.com/auth/earthengine']
    )
    ee.Initialize(credentials)


def nldas_monthly_asset_export(tgt_dt, overwrite_flag=False):
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
    logging.info(f'NLDAS Monthly Bias Corrected Reference ET - '
                 f'{tgt_dt.strftime("%Y-%m-%d")}')
    # response = f'NLDAS Monthly Bias Corrected Reference ET - ' \
    #            f'{tgt_dt.strftime("%Y-%m")}'

    asset_id = f'{ASSET_COLL_ID}/{tgt_dt.strftime(ASSET_DT_FMT)}'
    export_name = f'nldas_reference_et_monthly_{VERSION}_{tgt_dt.strftime("%Y%m%d")}'
    logging.debug(f'  {SOURCE_COLL_ID}')
    logging.debug(f'  {asset_id}')
    logging.debug(f'  {export_name}')

    if ee.data.getInfo(asset_id):
        if overwrite_flag:
            try:
                ee.data.deleteAsset(asset_id)
            except Exception as e:
                return f'{export_name} - An error occurred while trying to '\
                       f'delete the existing asset, skipping\n{e}\n'
        else:
            return f'{export_name} - The asset already exists and overwrite '\
                   f'is False, skipping\n'

    source_coll = ee.ImageCollection(SOURCE_COLL_ID)\
        .filterDate(tgt_dt, tgt_dt + relativedelta(months=1))

    eto_source_versions = get_info(
        source_coll.aggregate_histogram('eto_source_data_version')
    )
    etr_source_versions = get_info(
        source_coll.aggregate_histogram('etr_source_data_version')
    )

    properties = {
        'build_date': TODAY_DT.strftime('%Y-%m-%d'),
        'date': tgt_dt.strftime("%Y-%m-%d"),
        'eto_source_data_versions': str(eto_source_versions),
        'etr_source_data_versions': str(etr_source_versions),
        'status': 'permanent',
        'units_eto': 'mm',
        'units_etr': 'mm',
        # CGM: Should we use the UTC 0 time_start or the NLDAS time_start?
        'system:time_start': ee.Date(tgt_dt.strftime('%Y-%m-%d')).millis(),
        'system:index': tgt_dt.strftime('%Y%m'),
    }

    output_img = source_coll.sum().set(properties)

    export_task = ee.batch.Export.image.toAsset(
        image=output_img,
        description=export_name,
        assetId=asset_id,
        dimensions=ASSET_SHAPE,
        crs=ASSET_CRS,
        crsTransform='[' + ','.join(list(map(str, ASSET_GEO))) + ']',
    )

    # Try to start the task a couple of times
    for i in range(1, 4):
        try:
            export_task.start()
            break
        except ee.ee_exception.EEException as e:
            logging.warning(f'EE Exception, retry {i}\n{e}')
        except Exception as e:
            logging.warning(f'Unhandled Exception: {e}')
            return f'Unhandled Exception: {e}'
        time.sleep(i ** 3)

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
    response = 'Generate Monthly Bias Corrected NLDAS Reference ET Images\n'

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
        #     start_dt, end_dt - relativedelta(months=1) + relativedelta(days=1)
        # )

        if start_dt > end_dt:
            abort(404, description='Start date must be before end date')
        # elif (end_dt - start_dt) > timedelta(days=400):
        #     abort(404, description='No more than 1 year can be processed in a single request')
        # if start_dt < datetime(1980, 1, 1):
        #     logging.debug('Start Date: {} - no NLDAS images before '
        #                   '1980-01-01'.format(start_dt.strftime('%Y-%m-%d')))
        #     start_dt = datetime(1980, 1, 1)
    else:
        abort(404, description='Both start and end date must be specified')

    response += f'Start Date: {start_dt.strftime("%Y-%m-%d")}\n'
    response += f'End Date:   {end_dt.strftime("%Y-%m-%d")}\n'

    args = {
        'start_dt': start_dt,
        'end_dt': end_dt,
    }

    for tgt_dt in nldas_monthly_dates(**args):
        logging.info(f'Date: {tgt_dt.strftime("%Y-%m-%d")}')
        # response += f'Date: {tgt_dt.strftime("%Y-%m-%d")}\n'
        response += nldas_monthly_asset_export(tgt_dt, overwrite_flag=True)

    return Response(response, mimetype='text/plain')


def nldas_monthly_dates(start_dt, end_dt, overwrite_flag=False):
    """"""
    logging.debug('\nBuilding NLDAS monthly date list')
    logging.debug(f'  {start_dt.strftime("%Y-%m-%d")}')
    logging.debug(f'  {end_dt.strftime("%Y-%m-%d")}')

    task_id_re = re.compile(
        'nldas_monthly_bias_corrected_reference_et_(?P<date>\d{8})'
    )

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
    #         map(lambda x: x.strftime('%Y-%m-%d'), test_dt_list))))


    # Skip dates if the existing image is based on all permanent images
    #   (and not overwrite)
    # Bump end date for filterDate() calls
    filter_end_dt = end_dt + timedelta(days=1)
    tgt_date_coll = (
        ee.ImageCollection(ASSET_COLL_ID)
        .filterDate(start_dt.strftime('%Y-%m-%d'), filter_end_dt.strftime('%Y-%m-%d'))
    )
    tgt_perm_dates = get_info(
        tgt_date_coll.filterMetadata('status', 'equals', 'permanent')
        .aggregate_array('system:index')
    )
    # tgt_perm_dates =  get_info(
    #     src_date_coll
    #     .filter(ee.Filter.Or(ee.Filter.gt('provisional', 0), ee.Filter.gt('early', 0)))
    #     .aggregate_array('system:index')
    # )

    test_dt_list = [
        dt for dt in test_dt_list
        if overwrite_flag or dt.strftime('%Y%m') not in tgt_perm_dates
    ]
    if not test_dt_list:
        logging.info('All dates were built with permanent status images')
        return []

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
        description='Generate monthly bias corrected NLDAS reference ET assets',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        '--start', type=arg_valid_date, metavar='YYYY-MM-DD',
        default=(datetime(TODAY_DT.year, TODAY_DT.month, 1) -
                 relativedelta(months=START_MONTH_OFFSET)).strftime('%Y-%m-%d'),
        help='Start date')
    parser.add_argument(
        '--end', type=arg_valid_date, metavar='YYYY-MM-DD',
        default=(datetime(TODAY_DT.year, TODAY_DT.month, 1) -
                 relativedelta(days=1) -
                 relativedelta(months=END_MONTH_OFFSET)).strftime('%Y-%m-%d'),
        help='End date (inclusive)')
    # parser.add_argument(
    #     '-v', '--variables', nargs='+', default=VARIABLES,
    #     choices=VARIABLES, metavar='VAR',
    #     help='NLDAS daily variables')
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

    # # Build the image collection if it doesn't exist
    # logging.debug(f'Image Collection: {ASSET_COLL_ID}')
    # if not ee.data.getInfo(ASSET_COLL_ID):
    #     logging.info(f'\nImage collection does not exist and will be built'
    #                  f'\n  {ASSET_COLL_ID}')
    #     input('Press ENTER to continue')
    #     ee.data.createAsset({'type': 'IMAGE_COLLECTION'}, ASSET_COLL_ID)

    ingest_dt_list = nldas_monthly_dates(
args.start, args.end, overwrite_flag=args.overwrite
    )

    for ingest_dt in sorted(ingest_dt_list, reverse=args.reverse):
        # logging.info(f'Date: {ingest_dt.strftime("%Y-%m-%d")}')
        response = nldas_monthly_asset_export(ingest_dt,  overwrite_flag=args.overwrite)
        logging.info(f'  {response}')
