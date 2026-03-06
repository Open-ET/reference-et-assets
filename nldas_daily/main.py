import argparse
from datetime import datetime, timedelta, timezone
# import logging
import os
# import pprint
import re
import sys
import time

import ee
from flask import abort, Response
import google.auth
import openet.refetgee

# import openet.core.utils as utils

# # CONUS asset parameters
# ASSET_COLL_ID = 'projects/openet/assets/reference_et/conus/nldas/daily/v0'
# ASSET_CRS = 'EPSG:4326'
# ASSET_GEO = [0.125, 0, -125, 0, -0.125, 53]
# ASSET_SHAPE = '464x224'
# BIAS_ETO_COLL_ID = 'projects/openet/assets/reference_et/nldas/ratios/v0/monthly/eto'
# BIAS_ETR_COLL_ID = 'projects/openet/assets/reference_et/nldas/ratios/v0/monthly/etr'

# Milk River asset parameters
ASSET_COLL_ID = 'projects/dri-milkriver/assets/reference_et/nldas/daily'
ASSET_CRS = 'EPSG:4326'
ASSET_GEO = [0.125, 0, -116.5, 0, -0.125, 51.5]
ASSET_SHAPE = '105x44'
BIAS_ETO_COLL_ID = 'projects/dri-milkriver/assets/reference_et/nldas/ratios/v0/monthly/eto'
BIAS_ETR_COLL_ID = 'projects/dri-milkriver/assets/reference_et/nldas/ratios/v0/monthly/etr'

ASSET_DT_FMT = '%Y%m%d'
PROJECT_NAME = 'openet'
SOURCE_COLL_ID = 'NASA/NLDAS/FORA0125_H002'
TODAY_DT = datetime.today()
# TODAY_DT = datetime.now(timezone=timezone.utc)
VERSION = 'v0'


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


def nldas2_daily_asset(tgt_dt, overwrite_flag=False):
    """Generate daily NLDAS-2 asset for a single date

    Parameters
    ----------
    tgt_dt : datetime
    overwrite_flag : bool, optional

    Returns
    -------
    str : response string

    """
    tgt_date = tgt_dt.strftime("%Y-%m-%d")
    logging.info(f'Export NLDAS-2 daily reference ET asset - {tgt_date}')

    export_name = f'nldas2_reference_et_daily_{VERSION}_{tgt_dt.strftime(ASSET_DT_FMT)}'
    asset_id = f'{ASSET_COLL_ID}/{tgt_dt.strftime(ASSET_DT_FMT)}'
    # logging.info(f'asset_id: {asset_id}')

    # Set start time to 6 UTC to match GRIDMET (or 7?)
    # This should help set the solar sum and tmax/tmin correctly
    start_date = ee.Date.fromYMD(tgt_dt.year, tgt_dt.month, tgt_dt.day)\
        .advance(6, 'hour')
    end_date = start_date.advance(1, 'day')

    src_coll = ee.ImageCollection(SOURCE_COLL_ID).filterDate(start_date, end_date)

    # Check if there are 24 available images
    try:
        src_count = src_coll.size().getInfo()
    except Exception as e:
        return f'{export_name} - Could not get source image count, skipping\n{e}'
    if not src_count:
        src_count = 0
    logging.debug(f'  Source image count: {src_count}')

    if src_count == 0:
        logging.info('  No source data for date')
        return f'{export_name} - No source data for date, skipping\n'
    elif src_count < 24:
        logging.info('  Less than 24 hours data for date')
        return f'{export_name} - Less than 24 hours data for date, skipping\n'

    if ee.data.getInfo(asset_id):
        try:
            ee.data.deleteAsset(asset_id)
        except Exception as e:
            return f'{export_name} - Existing asset not deleted, skipping\n{e}'

    refet_obj = openet.refetgee.Daily.nldas(src_coll)

    properties = {
        'build_date': TODAY_DT.strftime('%Y-%m-%d'),
        'date': tgt_dt.strftime('%Y-%m-%d'),
        # 'eto_source_data_versions': str(eto_source_versions),
        # 'etr_source_data_versions': str(etr_source_versions),
        # 'geerefet_version': openet.refetgee.__version__,
        'status': 'permanent',
        'units_eto_asce': 'mm',
        'units_etr_asce': 'mm',
        'system:index': tgt_dt.strftime('%Y%m%d'),
        'system:time_start': start_date.millis(),
        # 'system:time_end': end_date.millis(),
    }

    bias_eto_img = ee.Image(f'{BIAS_ETO_COLL_ID}/{tgt_dt.strftime("%b")}')
    bias_etr_img = ee.Image(f'{BIAS_ETR_COLL_ID}/{tgt_dt.strftime("%b")}')

    output_img = (
        ee.Image([
            refet_obj.eto.multiply(bias_eto_img),
            refet_obj.etr.multiply(bias_etr_img),
        ])
        .rename(['eto_asce', 'etr_asce'])
        .set(properties)
    )

    task = ee.batch.Export.image.toAsset(
        image=output_img,
        description=export_name,
        assetId=asset_id,
        dimensions=ASSET_SHAPE,
        crs=ASSET_CRS,
        crsTransform='[' + ', '.join(map(str, ASSET_GEO)) + ']',
    )

    # Try to start the task a couple of times
    for i in range(1, 6):
        try:
            task.start()
            break
        except ee.ee_exception.EEException as e:
            logging.warning(f'EE Exception, retry {i}\n{e}')
        except Exception as e:
            logging.warning(f'Unhandled Exception: {e}')
            return f'Unhandled Exception: {e}'
        time.sleep(i ** 2)

    logging.info(f'Task ID - {task.status()["id"]}')
    return f'{export_name} - {task.id}\n'


# def update(request):
#     """Parse JSON or request arguments from cloud scheduler"""
#     request_json = request.get_json(silent=True)
#     request_args = request.args
#
#     if request_json and 'date' in request_json:
#         tgt_date = request_json['date']
#     elif request_args and 'date' in request_args:
#         tgt_date = request_args['date']
#     else:
#         abort(400, description='The date parameter not set')
#
#     try:
#         tgt_dt = datetime.datetime.strptime(tgt_date, '%Y-%m-%d')
#     except:
#         abort(400, description=f'The date {tgt_date} could not be parsed')
#
#     return Response(nldas2_daily_asset(tgt_dt), mimetype='text/plain')


def nldas2_daily_asset_dates(start_dt, end_dt, overwrite_flag=False):
    """Identify dates of missing NLDAS-2 daily assets

    Parameters
    ----------
    start_dt : datetime
        Start date
    end_dt : datetime
        End date (exclusive)
    overwrite_flag : bool, optional

    Returns
    -------
    list : datetimes

    """
    logging.info('\nBuilding NLDAS-2 daily asset ingest date list')

    task_id_re = re.compile('nldas2_daily_(?P<date>\d{8})$')
    asset_id_re = re.compile(ASSET_COLL_ID.split('projects/')[-1] + '/(?P<date>\d{8})$')

    # Figure out which asset dates nseed to be ingested
    # Start with a list of dates to check
    # logging.debug('\nBuilding Date List')
    tgt_dt_list = list(date_range(start_dt, end_dt, skip_leap_days=False))
    if not tgt_dt_list:
        logging.info('Empty date range')
        return []
    logging.debug('\nInitial test dates: {}'.format(
        ', '.join(map(lambda x: x.strftime('%Y-%m-%d'), tgt_dt_list))))

    # Check if any of the needed dates are currently being ingested
    # Check task list before checking asset list in case a task switches
    #   from running to done before the asset list is retrieved.
    logging.debug('\nChecking task list')
    task_id_list = [
        desc.replace('\nAsset ingestion: ', '')
        for desc in get_ee_tasks().keys()
    ]
    task_date_list = [
        datetime.datetime.strptime(m.group('date'), ASSET_DT_FMT).strftime('%Y-%m-%d')
        for task_id in task_id_list
        for m in [task_id_re.search(task_id)] if m
    ]
    logging.debug(f'\nTask dates: {", ".join(task_date_list)}')

    # Switch date list to be dates that are missing
    tgt_dt_list = [
        dt for dt in tgt_dt_list
        if overwrite_flag or dt.strftime('%Y-%m-%d') not in task_date_list
    ]
    if not tgt_dt_list:
        logging.info('No dates to process after checking ready/running tasks')
        return []
    logging.debug('\nDates (after filtering tasks): {}'.format(
        ', '.join(map(lambda x: x.strftime('%Y-%m-%d'), tgt_dt_list))))

    # Check if the assets already exist
    # For now, assume the collection exists
    logging.debug('\nChecking existing assets')
    asset_id_list = get_ee_assets(ASSET_COLL_ID, start_dt, end_dt)
    asset_date_list = [
        datetime.datetime.strptime(m.group('date'), ASSET_DT_FMT).strftime('%Y-%m-%d')
        for asset_id in asset_id_list
        for m in [asset_id_re.search(asset_id)] if m
    ]
    logging.debug(f'\nAsset dates: {", ".join(asset_date_list)}')

    # Switch date list to be dates that are missing
    tgt_dt_list = [
        dt for dt in tgt_dt_list
        if overwrite_flag or dt.strftime('%Y-%m-%d') not in asset_date_list
    ]
    if not tgt_dt_list:
        logging.info('No dates to process after filtering existing assets')
        return []
    logging.debug('\nDates (after filtering existing assets): {}'.format(
        ', '.join(map(lambda x: x.strftime('%Y-%m-%d'), tgt_dt_list))))

    if not tgt_dt_list:
        logging.info('No dates to process after checking server folders')
        return []
    logging.debug('\nIngest dates: {}'.format(
        ', '.join(map(lambda x: x.strftime('%Y-%m-%d'), tgt_dt_list))
    ))

    return tgt_dt_list


def date_range(start_dt, end_dt, days=1, skip_leap_days=False):
    """Generate dates within a range (inclusive)

    Parameters
    ----------
    start_dt : datetime
        Start date.
    end_dt : datetime
        End date (exclusive).
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
    while curr_dt < end_dt:
        if not skip_leap_days or curr_dt.month != 2 or curr_dt.day != 29:
            yield curr_dt
        curr_dt += datetime.timedelta(days=days)


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
            logging.info('  Collection or folder does not exist')
            raise sys.exit()
        except Exception as e:
            logging.error(
                '  Error getting asset list, retrying ({}/10)\n'
                '  {}'.format(i, e))
            time.sleep(i ** 2)

    return asset_id_list


def get_ee_tasks(states=['RUNNING', 'READY']):
    """Return current active tasks

    Parameters
    ----------
    states : list

    Returns
    -------
    dict : Task descriptions (key) and task IDs (value).

    """

    tasks = {}
    for i in range(1, 6):
        try:
            # task_list = ee.data.listOperations()
            task_list = ee.data.getTaskList()
            task_list = sorted([
                [t['state'], t['description'], t['id']]
                for t in task_list if t['state'] in states])
            tasks = {t_desc: t_id for t_state, t_desc, t_id in task_list}
            break
        except Exception as e:
            logging.info(
                '  Error getting active task list, retrying ({}/10)\n'
                '  {}'.format(i, e))
            time.sleep(i ** 2)
    return tasks


# CGM - This is a modified copy of openet.utils.delay_task()
#   It was changed to take and return the number of ready tasks
#   This change may eventually be pushed to openet.utils.delay_task()
def delay_task(ready_task_count, delay_time=0, max_ready=3000):
    """Delay script execution based on number of READY tasks

    Parameters
    ----------
    ready_task_count : int
    delay_time : float, int
        Delay time in seconds between starting export tasks or checking the
        number of queued tasks if "max_ready" is > 0.  The default is 0.
        The delay time will be set to a minimum of 10 seconds if max_ready > 0.
    max_ready : int, optional
        Maximum number of queued "READY" tasks.

    Returns
    -------
    ready_task_count

    """
    # Force delay time to be a positive value
    # (since parameter used to support negative values)
    if delay_time < 0:
        delay_time = abs(delay_time)

    if (max_ready <= 0 or max_ready >= 3000) and delay_time > 0:
        # Assume max_ready was not set and just wait the delay time
        logging.debug(f'  Pausing {delay_time} seconds')
        time.sleep(delay_time)
        ready_task_count = 0
    elif ready_task_count < max_ready:
        # Skip waiting if the number of ready tasks is below the max
        logging.debug(f'  Ready tasks: {ready_task_count}')
    else:
        # Don't continue to the next export until the number of READY tasks
        # is greater than or equal to "max_ready"

        # Force delay_time to be at least 10 seconds if max_ready is set
        #   to avoid excessive EE calls
        delay_time = max(delay_time, 10)

        # Make an initial pause before checking tasks lists to allow
        #   for previous export to start up.
        logging.debug(f'  Pausing {delay_time} seconds')
        time.sleep(delay_time)

        while True:
            ready_task_count = len(get_ee_tasks().keys())
            logging.debug(f'  Ready tasks: {ready_task_count}')
            if ready_task_count >= max_ready:
                logging.debug(f'  Pausing {delay_time} seconds')
                time.sleep(delay_time)
            else:
                logging.debug('  Continuing iteration')
                break

    return ready_task_count


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
        return datetime.datetime.strptime(input_date, '%Y-%m-%d')
    except ValueError:
        raise argparse.ArgumentTypeError(f'Not a valid date: "{input_date}"')


# def arg_valid_file(file_path):
#     """Argparse specific function for testing if file exists
#
#     Convert relative paths to absolute paths
#     """
#     if os.path.isfile(os.path.abspath(os.path.realpath(file_path))):
#         return os.path.abspath(os.path.realpath(file_path))
#         # return file_path
#     else:
#         raise argparse.ArgumentTypeError(f'{file_path} does not exist')


def arg_parse():
    """"""
    parser = argparse.ArgumentParser(
        description='Ingest NLDAS-2 daily assets into Earth Engine',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        '--start', type=arg_valid_date, metavar='YYYY-MM-DD',
        help='Start date')
    parser.add_argument(
        '--end', type=arg_valid_date, metavar='YYYY-MM-DD',
        help='End date (exclusive)')
    parser.add_argument(
        '--delay', default=0, type=float,
        help='Delay (in seconds) between each export tasks')
    parser.add_argument(
        '--ready', default=2500, type=int,
        help='Maximum number of queued READY tasks')
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

    # Build the image collection if it doesn't exist
    logging.debug('Image Collection: {}'.format(ASSET_COLL_ID))
    if not ee.data.getInfo(ASSET_COLL_ID):
        logging.info('\nImage collection does not exist and will be built'
                     '\n  {}'.format(ASSET_COLL_ID))
        input('Press ENTER to continue')
        ee.data.createAsset({'type': 'IMAGE_COLLECTION'}, ASSET_COLL_ID)

    ready_tasks = len(get_ee_tasks().keys())

    ingest_dt_list = nldas2_daily_asset_dates(
        args.start, args.end, overwrite_flag=args.overwrite
    )

    for ingest_dt in sorted(ingest_dt_list, reverse=args.reverse):
        response = nldas2_daily_asset(ingest_dt, overwrite_flag=args.overwrite)
        logging.info(f'  {response}')

        ready_tasks += 1
        ready_tasks = delay_task(ready_tasks, args.delay, args.ready)
