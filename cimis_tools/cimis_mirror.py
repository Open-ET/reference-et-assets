import argparse
import datetime as dt
import logging
import os

import ee


def main(start_dt, end_dt, overwrite_flag=False):
    logging.info('\nMirroring CIMIS asset to new collection')
    input_coll_id = 'projects/climate-engine/cimis/daily'
    output_coll_id = 'projects/openet/reference_et/cimis/daily'
    logging.info('Input:  {}'.format(input_coll_id))
    logging.info('Output: {}\n'.format(output_coll_id))

    start_date = start_dt.strftime('%Y-%m-%d')
    exclusive_end_dt = end_dt + dt.timedelta(days=1)
    exclusive_end_date = exclusive_end_dt.strftime('%Y-%m-%d')

    ee.Initialize()

    image_id_list = ee.ImageCollection(input_coll_id)\
        .filterDate(start_date, exclusive_end_date)\
        .aggregate_array('system:index').getInfo()
    output_id_list = ee.ImageCollection(output_coll_id)\
        .filterDate(start_date, exclusive_end_date)\
        .aggregate_array('system:index').getInfo()

    for copy_dt in date_range(start_dt, end_dt):
        logging.info('{}'.format(copy_dt.strftime('%Y-%m-%d')))
        image_id = copy_dt.strftime('%Y%m%d')
        input_id = f'{input_coll_id}/{image_id}'
        output_id = f'{output_coll_id}/{image_id}'
        if copy_dt.strftime('%Y%m%d') not in image_id_list:
            logging.debug('  source image does not exist - skipping date')
            continue
        if not overwrite_flag and copy_dt.strftime('%Y%m%d') in output_id_list:
            logging.debug('  target image already exists - skipping date')
            continue

        try:
            ee.data.copyAsset(input_id, output_id)
        except Exception as e:
            logging.exception('  Unhandled Exception: {}'.format(e))


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
        If True, skip leap days while incrementing (the default is False).

    Yields
    ------
    datetime

    """
    import copy
    curr_dt = copy.copy(start_dt)
    while curr_dt <= end_dt:
        if not skip_leap_days or curr_dt.month != 2 or curr_dt.day != 29:
            yield curr_dt
        curr_dt += dt.timedelta(days=days)


def valid_date(input_date):
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
        return dt.datetime.strptime(input_date, "%Y-%m-%d")
    except ValueError:
        msg = "Not a valid date: '{}'.".format(input_date)
        raise argparse.ArgumentTypeError(msg)


def arg_parse():
    """"""
    parser = argparse.ArgumentParser(
        description='Mirror CIMIS image collection',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        '-s', '--start', type=valid_date, metavar='DATE',
        default='2003-10-01', help='Start date (format YYYY-MM-DD)')
    parser.add_argument(
        '-e', '--end', type=valid_date, metavar='DATE',
        default=(dt.datetime.today()-dt.timedelta(days=0)).strftime('%Y-%m-%d'),
        help='End date (format YYYY-MM-DD)')
    # parser.add_argument(
    #     '--overwrite', default=False, action='store_true',
    #     help='Force overwrite of existing files')
    parser.add_argument(
        '--debug', default=logging.INFO, const=logging.DEBUG,
        help='Debug level logging', action='store_const', dest='loglevel')
    args = parser.parse_args()
    return args


if __name__ == '__main__':
    args = arg_parse()
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    main(start_dt=args.start, end_dt=args.end)
