import argparse
import datetime
import gzip
import logging
import os
import pprint
import re
import shutil
import sys
import time

import ee
from flask import abort, Response
from google.cloud import storage
from google.cloud import tasks_v2
import numpy as np
import rasterio
import rasterio.warp
import refet
import requests
from scipy import ndimage

if 'FUNCTION_REGION' in os.environ:
    # Assume code is deployed to a cloud function
    logging.debug(f'\nInitializing GEE using application default credentials')
    import google.auth
    credentials, project_id = google.auth.default(
        default_scopes=['https://www.googleapis.com/auth/earthengine'])
    ee.Initialize(credentials)

logging.getLogger('earthengine-api').setLevel(logging.INFO)
logging.getLogger('googleapiclient').setLevel(logging.ERROR)
logging.getLogger('requests').setLevel(logging.INFO)
logging.getLogger('rasterio').setLevel(logging.INFO)
logging.getLogger('urllib3').setLevel(logging.INFO)

ASSET_COLL_ID = 'projects/earthengine-legacy/assets/' \
                'projects/openet/reference_et/cimis/daily'
ASSET_DT_FMT = '%Y%m%d'
BUCKET_NAME = 'openet'
BUCKET_FOLDER = 'cimis/daily'
FUNCTION_URL = 'https://us-central1-openet.cloudfunctions.net'
FUNCTION_NAME = 'cimis-reference-et-daily-worker'
GEE_KEY_FILE = 'openet-gee.json'
PROJECT_NAME = 'openet'
SOURCE_URL = 'https://spatialcimis.water.ca.gov/cimis'
# This server stopped updating in 2019 but is useful for filling in missing dates
#   specifically 2003-10-01 to 2003-12-31 and 2010-11-16 to 2010-11-23
# SOURCE_URL = 'http://cimis.casil.ucdavis.edu/cimis'
STORAGE_CLIENT = storage.Client(project=PROJECT_NAME)
TASK_LOCATION = 'us-central1'
TASK_QUEUE = 'ee-single-worker'
# VARIABLES = ['eto']
VARIABLES = ['eto', 'eto_asce', 'etr_asce']
# VARIABLES = ['Tdew', 'Tx', 'Tn', 'Rnl', 'Rs', 'K', 'U2',
#              'eto', 'eto_asce', 'etr_asce']
START_DAY_OFFSET = 365
END_DAY_OFFSET = 0


def cimis_daily_asset_ingest(tgt_dt, variables, workspace='/tmp',
                             overwrite_flag=False):
    """Ingest CIMIS daily data into Earth Engine

    Parameters
    ----------
    tgt_dt : datetime
    variables : list
        Variables to process.  Choices are: 'depth', 'swe'.
    workspace : str
    overwrite_flag : bool, optional

    Returns
    -------
    str : response string

    Notes
    -----
    https://spatialcimis.water.ca.gov/cimis/
    http://cimis.casil.ucdavis.edu/cimis/
    CIMIS ETo data starts: 2003-03-20
    Full parameters start: 2003-10-01 (water year 2004)

    """
    tgt_date = tgt_dt.strftime('%Y-%m-%d')
    logging.info(f'Ingest CIMIS daily asset - {tgt_date}')
    # response = f'Ingest CIMIS daily asset - {tgt_date}\n'

    date_ws = os.path.join(workspace, tgt_date)
    tif_name = f'{tgt_dt.strftime(ASSET_DT_FMT)}.tif'
    upload_path = os.path.join(date_ws, tif_name)
    bucket_path = f'gs://{BUCKET_NAME}/{BUCKET_FOLDER}/{tif_name}'
    asset_id = f'{ASSET_COLL_ID}/{tgt_dt.strftime(ASSET_DT_FMT)}'
    logging.debug(f'  {upload_path}')
    logging.debug(f'  {bucket_path}')
    logging.debug(f'  {asset_id}')

    # Define which CIMIS variables are needed for each user variable
    # ASCE ETo/ETr are computed from the components
    gz_vars = {
        'eto_asce': ['Rs', 'Tdew', 'Tn', 'Tx', 'U2'],
        'etr_asce': ['Rs', 'Tdew', 'Tn', 'Tx', 'U2'],
        'eto': ['ETo'],
        'K': ['K'],
        'Rnl': ['Rnl'],
        'Rs': ['Rs'],
        'Rso': ['Rso'],
        'Tdew': ['Tdew'],
        'Tn': ['Tn'],
        'Tx': ['Tx'],
        'U2': ['U2']
    }
    # Define mapping of CIMIS variables to output file names
    # For now, these need to be identical to the user variable names
    gz_remap = {
        'ETo': 'eto',
        'K': 'K',
        'Rnl': 'Rnl',
        'Rs': 'Rs',
        'Rso': 'Rso',
        'Tdew': 'Tdew',
        'Tn': 'Tn',
        'Tx': 'Tx',
        'U2': 'U2'
    }
    gz_fmt = '{variable}.asc.gz'

    # RasterIO can't read from the bucket directly when deployed as a function
    elevation_url = 'https://storage.googleapis.com/openet/cimis/cimis_elev.tif'
    land_mask_url = 'https://storage.googleapis.com/openet/cimis/cimis_mask.tif'
    latitude_url = 'https://storage.googleapis.com/openet/cimis/cimis_lat.tif'
    # longitude_url = 'https://storage.googleapis.com/openet/cimis/cimis_lon.tif'
    elevation_path = os.path.join(date_ws, 'cimis_elev.tif')
    land_mask_path = os.path.join(date_ws, 'cimis_mask.tif')
    latitude_path = os.path.join(date_ws, 'cimis_lat.tif')
    # longitude_path = os.path.join(date_ws, 'cimis_lon.tif')

    # DEADBEEF
    # # There is only partial CIMIS data before 2003-10-01
    # if start_dt < datetime.datetime(2003, 10, 1):
    #     start_dt = datetime.datetime(2003, 10, 1)
    #     logging.info(f'Adjusting start date to: {start_dt.strftime("%Y-%m-%d")}')
    # if end_dt > today_dt:
    #     end_dt = today_dt
    #     logging.info(f'Adjusting end date to:   {end_dt.strftime("%Y-%m-%d")}\n')

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


    # CIMIS grid
    logging.debug('\nCIMIS')
    asset_height = 560
    asset_width = 510
    asset_extent = (-410000.0, -660000.0, 610000.0, 460000.0)
    # asset_height = 552
    # asset_width = 500
    # asset_extent = (-400000.0, -650000.0, 600000.0, 454000.0)
    asset_cs = 2000.0
    asset_geo = (asset_cs, 0., asset_extent[0], 0., -asset_cs, asset_extent[3])
    # asset_geo = (asset_extent[0], asset_cs, 0., asset_extent[3], 0., -asset_cs)
    logging.debug(f'Shape:  {asset_width}, {asset_height} (w, h)')
    logging.debug(f'Extent: {asset_extent}')
    logging.debug(f'Geo:    {asset_geo}')

    asset_proj = rasterio.crs.CRS.from_proj4(
        '+proj=aea +lat_1=34 +lat_2=40.5 +lat_0=0 +lon_0=-120 ' +
        '+x_0=0 +y_0=-4000000 +ellps=GRS80 +datum=NAD83 +units=m +no_defs')
    # asset_proj = 'EPSG:3310'  # NAD_1983_California_Teale_Albers
    logging.debug(f'CRS: {asset_proj}')


    logging.debug('Downloading component ASZ GZ files')
    gz_var_list = sorted(list(set(
        gz_var for v in variables for gz_var in gz_variables[v])))
    # logging.info(f'  GZ Variables: {gz_var_list}')
    for gz_var in gz_var_list:
        logging.debug(f'Variable: {gz_var}')
        gz_file = gz_fmt.format(variable=gz_var)
        gz_url = f'{SOURCE_URL}/{tgt_dt.strftime("%Y/%m/%d")}/{gz_file}'
        gz_path = os.path.join(date_ws, gz_file)
        logging.debug(f'  {gz_url}')
        logging.debug(f'  {gz_path}')
        url_download(gz_url, gz_path)

    # Only build the composite if all the input images are available
    input_vars = set(gz_name.split('.')[0] for gz_name in os.listdir(date_ws))
    if not set(gz_var_list).issubset(input_vars):
        return f'{tgt_date} - Missing input variables for composite\n'\
               f'    {", ".join(list(set(variables) - input_vars))}\n'

    # Assume the ancillary data was prepped separately
    url_download(land_mask_url, land_mask_path)
    try:
        with rasterio.open(land_mask_path) as src:
            mask_array = src.read(1)
    except Exception as e:
        logging.exception(f'Unhandled exception: {e}')
        return f'{tgt_date} - Land mask array could not be read'
    os.remove(land_mask_path)

    # DEADBEEF - Directly reading the URL above wasn't working when deployed
    # with rasterio.open(mask_url) as src:
    #     mask_array = src.read(1)

    # CGM - Process each variable sequentially to minimize space
    logging.debug('Reading variable arrays')
    daily_arrays = {}
    for gz_var in gz_var_list:
        logging.debug(f'{gz_var}')
        gz_file = gz_fmt.format(variable=gz_var)
        gz_path = os.path.join(date_ws, gz_file)
        asc_path = gz_path.replace('.gz', '')
        # logging.debug(f'  {gz_path}')
        # logging.debug(f'  {asc_path}')

        logging.debug(f'  Uncompressing ASC GZ file')
        try:
            if SOURCE_URL == 'https://spatialcimis.water.ca.gov/cimis':
                input_f = gzip.open(gz_path, 'rb')
            elif SOURCE_URL == 'http://cimis.casil.ucdavis.edu/cimis':
                # The asc.gz files on this server are not actually compressed
                input_f = open(gz_path, 'rb')
            output_f = open(asc_path, 'wb')
            output_f.write(input_f.read())
            output_f.close()
            input_f.close()
            del input_f, output_f
            os.remove(gz_path)
        except:
            logging.error(f'  Error extracting ASCII file\n  {asc_path}')
            return f'{tgt_date} - Error extracting ASCII file\n'

        logging.debug('  Reading ASCII')
        output_array, output_geo = ascii_to_array(asc_path)
        output_shape = tuple(map(int, output_array.shape))
        logging.debug(f'    Input Shape: {output_shape}')
        logging.debug(f'    Input Geo: {output_geo}')

        # In the UC Davis data some arrays have a 500m cell size
        if output_geo[0] == 500.0 and output_geo[4] == -500.0:
            logging.info(f'  Rescaling input {gz_var} array')
            output_array = ndimage.zoom(output_array, 0.25, order=1)
            output_geo = (2000.0, 0.0, output_geo[2], 0.0, -2000.0, output_geo[5])
            output_shape = tuple(map(int, output_array.shape))
            logging.debug(f'    Shape: {output_array.shape}')
            logging.debug(f'    Geo: {output_geo}')

        # Expand all CA DWR images up to the larger extent
        # In the UC Davis data some arrays have a slightly smaller extent
        if (output_geo == (2000.0, 0.0, -400000.0, 0.0, -2000.0, 454000.0) or
                output_geo == (2000.0, 0.0, -400000.0, 0.0, -2000.0, 450000.0)):
            logging.debug(f'  Padding input {gz_var} array extent')
            # Assume input extent is entirely within default CIMIS extent
            int_xi, int_yi = array_geo_offsets(asset_geo, output_geo, asset_cs)
            pad_width = (
                (int_yi, asset_height - output_shape[0] - int_yi),
                (int_xi, asset_width - output_shape[1] - int_xi))
            logging.debug(f'    Pad: {pad_width}')
            output_array = np.lib.pad(
                output_array, pad_width, 'constant', constant_values=-9999.0)
        elif output_geo != asset_geo:
            logging.warning(f'  Unexpected input {gz_var} array transform\n'
                            f'    Shape: {output_shape}\n'
                            f'    Geo: {output_geo}\n')
            return f'{tgt_date} - Unexpected input {gz_var} transform\n'

        # Mask out nodata areas from all arrays (Rs, K, and ETo)
        output_array[mask_array == 0] = np.nan

        daily_arrays[gz_remap[gz_var]] = output_array

        del output_array
        # DEADBEEF
        # os.remove(asc_path)

    if 'eto_asce' in variables or 'etr_asce' in variables:
        logging.debug('\nComputing Reference ET')
        refet_vars = {'Rs', 'Tdew', 'Tx', 'Tn', 'U2'}
        if not refet_vars.issubset(set(daily_arrays.keys())):
            logging.warning(
                '  Missing input variable(s) for computing ASCE ETo/ETr, skipping date\n'
                '    {}'.format(', '.join(list(refet_vars - input_vars))))
            return f'{tgt_date} - Missing input variable(s) for computing reference ET\n'

        # Elevation
        url_download(elevation_url, elevation_path)
        try:
            with rasterio.open(elevation_path) as src:
                elev_array = src.read(1)
        except Exception as e:
            logging.exception(f'Unhandled exception: {e}')
            return f'{tgt_date} - Elevation array could not be read'
        os.remove(elevation_path)

        # Latitude
        url_download(latitude_url, latitude_path)
        try:
            with rasterio.open(latitude_path) as src:
                lat_array = src.read(1)
        except Exception as e:
            logging.exception(f'Unhandled exception: {e}')
            return f'{tgt_date} - Latitude array could not be read'
        os.remove(latitude_path)

        # DEADBEEF - Directly reading the URL above wasn't working when deployed
        # with rasterio.open(elevation_url) as src:
        #     elev_array = src.read(1)
        # with rasterio.open(latitude_url) as src:
        #     lat_array = src.read(1)

        for variable in variables:
            if variable not in ['eto_asce', 'etr_asce']:
                continue
            logging.debug(f'{variable}')
            refet_obj = refet.Daily(
                tmin=daily_arrays['Tn'], tmax=daily_arrays['Tx'],
                # Compute Ea from Tdew
                ea=refet.calcs._sat_vapor_pressure(daily_arrays['Tdew']),
                # Force solar to be >= 0
                rs=np.maximum(daily_arrays['Rs'], 0),
                uz=daily_arrays['U2'], zw=2, elev=elev_array, lat=lat_array,
                doy=int(tgt_dt.strftime('%j')), method='asce',
                input_units={'tmax': 'C', 'tmin': 'C', 'ea': 'kPa',
                             'rs': 'MJ m-2 d-1', 'uz': 'm s-1', 'lat': 'deg'})
            output_array = refet_obj.etsz(variable.split('_')[0].lower())

            # Assume that if temp, tdew and wind are all zero
            #   the ETr/ETo should also be zero.
            # This could also be handled using the fixed ancillary mask
            # mask_array = (
            #     (tmin_array == 0) & (tmax_array == 0) &
            #     (u2_array == 0) & (tdew_array == 0))
            output_array[mask_array == 0] = -9999

            daily_arrays[variable] = output_array

            del output_array, refet_obj

        del elev_array, lat_array


    # Only build the composite if all the input images are available
    input_vars = set(daily_arrays.keys())
    if not set(variables).issubset(input_vars):
        return f'{tgt_date} - Missing input variables for composite\n'\
               f'  {", ".join(list(set(variables) - input_vars))}'

    logging.debug('\nBuilding output GeoTIFF')
    output_ds = rasterio.open(
        upload_path, 'w', driver='GTiff',
        nodata=-9999, count=len(variables), dtype=rasterio.float32,
        height=asset_height, width=asset_width,
        crs=asset_proj, transform=asset_geo,
        compress='lzw', tiled=True, blockxsize=256, blockysize=256,
        # compress='deflate', tiled=True, predictor=2,
        # compress='lzw', tiled=True, predictor=1,
    )

    logging.debug('\nWriting arrays to output GeoTIFF')
    for band_i, variable in enumerate(variables):
        # logging.debug(f'  {variable}')
        output_ds.set_band_description(band_i + 1, variable)
        data_array = daily_arrays[variable].astype(np.float32)
        data_array[np.isnan(data_array)] = -9999
        output_ds.write(data_array, band_i + 1)
        del data_array

    # output_ds.close()
    del output_ds

    # Build overviews
    dst = rasterio.open(upload_path, 'r+')
    dst.build_overviews([2, 4, 8, 16], rasterio.warp.Resampling.average)
    dst.update_tags(ns='rio_overview', resampling='average')
    dst.close()


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
        'date_ingested': f'{datetime.datetime.today().strftime("%Y-%m-%d")}',
        'source': SOURCE_URL.replace('https://', '').replace('http://', ''),
    }
    if 'eto_asce' in variables or 'etr_asce' in variables:
        properties['refet_version'] = f'{refet.__version__}'

    # NOTE: The band names are being forced to lower case here
    params = {
        'name': asset_id,
        'bands': [
            {'id': v, 'tilesetId': 'image', 'tilesetBandIndex': i}
            for i, v in enumerate(variables)
        ],
        'tilesets': [
            {
                'id': 'image',
                'sources': [{'uris': [bucket_path]}]
            }
        ],
        'properties': properties,
        'startTime': tgt_dt.isoformat() + '.000000000Z',
        # 'pyramiding_policy': 'MEAN',
        # 'missingData': {'values': [nodata_value]},
    }
    ee.data.startIngestion(task_id, params, allow_overwrite=True)

    if os.path.isdir(date_ws):
        shutil.rmtree(date_ws)

    return f'{tgt_date} - {asset_id}\n'


def cimis_daily_asset_dates(start_dt, end_dt, overwrite_flag=False):
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

    task_id_re = re.compile(
        ASSET_COLL_ID.split('projects/')[-1] + '/(?P<date>\d{8})$')
    asset_id_re = re.compile(
        ASSET_COLL_ID.split('projects/')[-1] + '/(?P<date>\d{8})$')

    # Figure out which asset dates need to be ingested
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
        for desc in get_ee_tasks(states=['RUNNING', 'READY']).keys()]
    task_dates = {
        datetime.datetime.strptime(m.group('date'), ASSET_DT_FMT).strftime('%Y-%m-%d')
        for task_id in task_id_list for m in [task_id_re.search(task_id)] if m}
    # logging.debug(f'\nTask dates: {", ".join(sorted(task_dates))}')

    # Switch date list to be dates that are missing
    tgt_dt_list = [
        dt for dt in tgt_dt_list
        if overwrite_flag or dt.strftime('%Y-%m-%d') not in task_dates]
    if not tgt_dt_list:
        logging.info('No dates to process after checking ready/running tasks')
        return []
    logging.debug('\nDates (after filtering tasks): {}'.format(
        ', '.join(map(lambda x: x.strftime('%Y-%m-%d'), tgt_dt_list))))

    # TODO: Check "source" parameter for images that were generated by interpolating

    # Check if the assets already exist
    # For now, assume the collection exists
    logging.debug('\nChecking existing assets')
    asset_id_list = get_ee_assets(
        ASSET_COLL_ID, start_dt, end_dt + datetime.timedelta(days=1))
    asset_dates = {
        datetime.datetime.strptime(m.group('date'), ASSET_DT_FMT).strftime('%Y-%m-%d')
        for asset_id in asset_id_list for m in [asset_id_re.search(asset_id)] if m}
    logging.debug(f'\nAsset dates: {", ".join(sorted(asset_dates))}')

    # Switch date list to be dates that are missing
    tgt_dt_list = [
        dt for dt in tgt_dt_list
        if overwrite_flag or dt.strftime('%Y-%m-%d') not in asset_dates]
    if not tgt_dt_list:
        logging.info('No dates to process after filtering existing assets')
        return []
    logging.debug('\nDates (after filtering existing assets): {}'.format(
        ', '.join(map(lambda x: x.strftime('%Y-%m-%d'), tgt_dt_list))))

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
        ', '.join(map(lambda x: x.strftime('%Y-%m-%d'), tgt_dt_list))))

    return tgt_dt_list


# def cron_scheduler(request):
#     """Parse JSON/request arguments and queue ingest tasks for a date range"""
#     logging.info('Queuing CIMIS daily asset ingest tasks')
#     response = 'Queue CIMIS daily asset ingest tasks\n'
#     args = {}
#
#     request_json = request.get_json(silent=True)
#     request_args = request.args
#
#     if request_json and 'start' in request_json:
#         start_date = request_json['start']
#     elif request_args and 'start' in request_args:
#         start_date = request_args['start']
#     else:
#         start_date = None
#         # abort(400, description='start parameter not set')
#
#     if request_json and 'end' in request_json:
#         end_date = request_json['end']
#     elif request_args and 'end' in request_args:
#         end_date = request_args['end']
#     else:
#         end_date = None
#         # abort(400, description='end parameter not set')
#
#     if start_date is None and end_date is None:
#         today_dt = datetime.datetime.today()
#         start_date = (today_dt - relativedelta(days=START_DAY_OFFSET))\
#             .strftime('%Y-%m-%d')
#         end_date = (today_dt - relativedelta(days=END_DAY_OFFSET))\
#             .strftime('%Y-%m-%d')
#     elif start_date is None or end_date is None:
#         abort(400, description='Both start and end date must be specified')
#
#     try:
#         args['start_dt'] = datetime.datetime.strptime(start_date, '%Y-%m-%d')
#     except:
#         abort(400, description=f'Start date {start_date} could not be parsed')
#     try:
#         args['end_dt'] = datetime.datetime.strptime(end_date, '%Y-%m-%d')
#         # args['end_dt'] = min(
#         #     datetime.datetime.strptime(end_date, '%Y-%m-%d'),
#         #     datetime.datetime.today()
#         # )
#     except:
#         abort(400, description=f'End date {end_date} could not be parsed')
#
#     if args['end_dt'] < args['start_dt']:
#         abort(400, description='End date must be after start date')
#     if args['start_dt'] < datetime.datetime(2003, 10, 1):
#         abort(400, description=f'Start date cannot be before 2003-10-01')
#     # if end_dt > today_dt:
#     #     end_dt = today_dt
#     #     logging.info(f'Adjusting end date to:   {end_dt.strftime("%Y-%m-%d")}\n')
#     # if (end_dt - start_dt).days > 40:
#     #     abort(400, description=f'Date range must be less than 30 days')
#
#     # CGM - For now don't allow scheduler calls to overwrite existing assets
#     # if request_json and 'overwrite' in request_json:
#     #     overwrite_flag = request_json['overwrite']
#     # elif request_args and 'overwrite' in request_args:
#     #     overwrite_flag = request_args['overwrite']
#     # else:
#     #     overwrite_flag = 'false'
#     #
#     # if overwrite_flag.lower() in ['true', 't']:
#     #     args['overwrite_flag'] = True
#     # elif overwrite_flag.lower() in ['false', 'f']:
#     #     args['overwrite_flag'] = False
#     # else:
#     #     abort(400, description=f'overwrite="{overwrite_flag}" could not be parsed')
#
#     # CGM - Should the scheduler be responsible for clearing the bucket?
#     logging.info('Clearing all files from bucket folder')
#     bucket = STORAGE_CLIENT.bucket(BUCKET_NAME)
#     blobs = bucket.list_blobs(prefix=BUCKET_FOLDER)
#     for blob in blobs:
#         blob.delete()
#
#     for tgt_dt in cimis_daily_asset_dates(**args):
#         # logging.info(f'Date: {tgt_dt.strftime("%Y-%m-%d")}')
#         # response += 'Date: {}\n'.format(tgt_dt.strftime('%Y-%m-%d'))
#         response += cimis_daily_asset_ingest(
#             tgt_dt, workspace='/tmp', variables=VARIABLES, overwrite_flag=False)
#
#     return Response(response, mimetype='text/plain')


def cron_scheduler(request):
    """Parse JSON/request arguments and queue ingest tasks for a date range"""
    args = {}

    request_json = request.get_json(silent=True)
    request_args = request.args

    if request_json and 'days' in request_json:
        days = request_json['days']
    elif request_args and 'days' in request_args:
        days = request_args['days']
    else:
        days = START_DAY_OFFSET - END_DAY_OFFSET

    try:
        days = int(days)
    except:
        abort(400, description=f'Days parameter could not be parsed')

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

    if start_date is None and end_date is None:
        today_dt = datetime.datetime.today()
        start_date = (today_dt - datetime.timedelta(days=days))\
            .strftime('%Y-%m-%d')
        end_date = (today_dt - datetime.timedelta(days=END_DAY_OFFSET))\
            .strftime('%Y-%m-%d')
    elif start_date is None or end_date is None:
        abort(400, description='Both start and end date must be specified')

    try:
        args['start_dt'] = datetime.datetime.strptime(start_date, '%Y-%m-%d')
    except:
        abort(400, description=f'Start date {start_date} could not be parsed')
    try:
        args['end_dt'] = datetime.datetime.strptime(end_date, '%Y-%m-%d')
    except:
        abort(400, description=f'End date {end_date} could not be parsed')

    if args['end_dt'] < args['start_dt']:
        abort(400, description='End date must be after start date')
    if args['start_dt'] < datetime.datetime(2003, 10, 1):
        abort(400, description=f'Start date cannot be before 2003-10-01')
    if args['start_dt'] < datetime.datetime(2004, 1, 1):
        abort(400, description=f'Start date cannot be before 2004-01-01')
    # if args['end_dt'] > datetime.datetime.today():
    #     args['end_dt'] = datetime.datetime.today()
    #     logging.info(f'Adjusting end date to:   {end_dt.strftime("%Y-%m-%d")}\n')
    # if (args['end_dt'] - args['start_dt']).days > 40:
    #     abort(400, description=f'Date range must be less than 30 days')

    # CGM - For now don't allow scheduler calls to overwrite existing assets
    # if request_json and 'overwrite' in request_json:
    #     overwrite_flag = request_json['overwrite']
    # elif request_args and 'overwrite' in request_args:
    #     overwrite_flag = request_args['overwrite']
    # else:
    #     overwrite_flag = 'false'
    #
    # if overwrite_flag.lower() in ['true', 't']:
    #     args['overwrite_flag'] = True
    # elif overwrite_flag.lower() in ['false', 'f']:
    #     args['overwrite_flag'] = False
    # else:
    #     abort(400, description=f'overwrite "{overwrite_flag}" could not be parsed')

    # CGM - Should the scheduler be responsible for clearing the bucket?
    logging.info('Clearing all files from bucket folder')
    bucket = STORAGE_CLIENT.bucket(BUCKET_NAME)
    blobs = bucket.list_blobs(prefix=BUCKET_FOLDER)
    for blob in blobs:
        blob.delete()

    response = queue_ingest_tasks(cimis_daily_asset_dates(**args))
    return Response(response, mimetype='text/plain')


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
        args['tgt_dt'] = datetime.datetime.strptime(tgt_date, '%Y-%m-%d')
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

    response = cimis_daily_asset_ingest(**args)
    return Response(response, mimetype='text/plain')


def queue_ingest_tasks(tgt_dt_list):
    """Submit ingest tasks to the queue

    Parameters
    ----------
    tgt_dt_list : list

    Returns
    -------
    str : response string

    """
    logging.info('Queuing CIMIS daily asset ingest tasks')
    response = 'Queue CIMIS daily asset ingest tasks\n'

    TASK_CLIENT = tasks_v2.CloudTasksClient()
    parent = TASK_CLIENT.queue_path(PROJECT_NAME, TASK_LOCATION, TASK_QUEUE)

    for tgt_dt in tgt_dt_list:
        logging.info(f'Date: {tgt_dt.strftime("%Y-%m-%d")}')
        # response += f'Date: {tgt_dt.strftime("%Y-%m-%d")}\n'

        # Using the default name in the request can create duplicate tasks
        # Trying out adding the timestamp to avoid this for testing/debug
        name = f'{parent}/tasks/cimis_daily_asset_{tgt_dt.strftime("%Y%m%d")}_' \
               f'{datetime.datetime.today().strftime("%Y%m%d%H%M%S")}'
        # name = f'{parent}/tasks/cimis_daily_asset_{tgt_dt.strftime("%Y%m%d")}'
        response += name + '\n'
        logging.info(name)

        # Using the json body wasn't working, switching back to URL
        # Couldn't get authentication with oidc_token to work
        # payload = {'date': tgt_dt.strftime('%Y-%m-%d')}
        task = {
            'http_request': {
                'http_method': tasks_v2.HttpMethod.POST,
                'url': '{}/{}?date={}'.format(
                    FUNCTION_URL, FUNCTION_NAME, tgt_dt.strftime('%Y-%m-%d')),
                # 'url': '{}/{}?date={}&overwrite={}'.format(
                #     FUNCTION_URL, FUNCTION_NAME, tgt_dt.strftime('%Y-%m-%d'),
                #     str(overwrite_flag).lower()),
                # 'url': '{}/{}'.format(FUNCTION_URL, FUNCTION_NAME),
                # 'headers': {'Content-type': 'application/json'},
                # 'body': json.dumps(payload).encode(),
                # 'oidc_token': {
                #     'service_account_email': SERVICE_ACCOUNT,
                #     'audience': '{}/{}'.format(FUNCTION_URL, FUNCTION_NAME)},
                # 'relative_uri': ,
            },
            'name': name,
        }
        TASK_CLIENT.create_task(request={'parent': parent, 'task': task})

        time.sleep(0.1)

    return response


def array_geo_offsets(full_geo, sub_geo, cs):
    """Return x/y offset of a geotransform based on another geotransform

    Parameters
    ----------
    full_geo :
        larger geotransform from which the offsets should be calculated
    sub_geo :
        smaller form

    Returns
    -------
    x_offset: number of cells of the offset in the x direction
    y_offset: number of cells of the offset in the y direction

    """
    # Return UPPER LEFT array coordinates of sub_geo in full_geo
    # If portion of sub_geo is outside full_geo, only return interior portion
    x_offset = int(round((sub_geo[2] - full_geo[2]) / cs, 0))
    y_offset = int(round((sub_geo[5] - full_geo[5]) / -cs, 0))
    # Force offsets to be greater than zero
    x_offset, y_offset = max(x_offset, 0), max(y_offset, 0)
    return x_offset, y_offset


def ascii_to_array(input_ascii, input_type=np.float32):
    """Convert an ASCII raster to a different file format

    """
    with open(input_ascii, 'r') as input_f:
        input_header = input_f.readlines()[:6]
    input_cols = float(input_header[0].strip().split()[-1])
    input_rows = float(input_header[1].strip().split()[-1])
    # DEADBEEF - I need to check cell corner vs. cell center here
    input_xmin = float(input_header[2].strip().split()[-1])
    input_ymin = float(input_header[3].strip().split()[-1])
    input_cs = float(input_header[4].strip().split()[-1])
    input_nodata = float(input_header[5].strip().split()[-1])
    # Using RasterIO transform format
    input_geo = (
        input_cs, 0., input_xmin,
        0., -input_cs, input_ymin + input_cs * input_rows)
    # input_geo = (
    #     input_xmin, input_cs, 0.,
    #     input_ymin + input_cs * input_rows, 0., -input_cs)

    output_array = np.genfromtxt(
        input_ascii, dtype=input_type, skip_header=6)
    output_array[output_array == input_nodata] = -9999
    # output_array[output_array == input_nodata] = np.nan

    return output_array, input_geo


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
            logging.error(f'  Error getting asset list, retrying ({i}/6)\n  {e}')
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
            logging.info(f'  Error getting active task list, retrying ({i}/6)\n  {e}')
            time.sleep(i ** 2)
    return tasks


def url_download(download_url, output_path, verify=True):
    """Download file from a URL using requests module

    Parameters
    ----------
    download_url : str
    output_path : str
    verify : bool, optional

    Returns
    -------
    None

    """
    for i in range(1, 6):
        try:
            response = requests.get(download_url, stream=True, verify=verify)
        except Exception as e:
            logging.info(f'  Exception: {e}')
            return False

        logging.debug(f'  HTTP Status: {response.status_code}')
        if response.status_code == 200:
            pass
        elif response.status_code == 404:
            logging.debug('  Skipping')
            return False
        else:
            logging.info(f'  HTTPError: {response.status_code}')
            logging.info(f'  Retry attempt: {i}')
            time.sleep(i ** 2)
            continue

        logging.debug('  Beginning download')
        try:
            with (open(output_path, 'wb')) as output_f:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:  # filter out keep-alive new chunks
                        output_f.write(chunk)
            logging.debug('  Download complete')
            return True
        except Exception as e:
            logging.info(f'  Exception: {e}')
            return False


# def url_download(input_url, output_path):
#     download_response = requests.get(input_url)
#     if download_response.status_code != 200:
#         logging.debug(f'  HTTP Status: {download_response.status_code}')
#         return False
#     with open(output_path, 'wb') as output_f:
#         output_f.write(download_response.content)
#     time.sleep(0.1)
#     return True


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
    today = datetime.date.today()

    parser = argparse.ArgumentParser(
        description='Ingest CIMIS daily assets into Earth Engine',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        '--workspace', metavar='PATH',
        default=os.path.dirname(os.path.abspath(__file__)),
        help='Set the current working directory')
    parser.add_argument(
        '--start', type=arg_valid_date, metavar='DATE',
        default=(datetime.datetime.today() -
                 datetime.timedelta(days=START_DAY_OFFSET)).strftime('%Y-%m-%d'),
        help='Start date (format YYYY-MM-DD)')
    parser.add_argument(
        '--end', type=arg_valid_date, metavar='DATE',
        default=(datetime.datetime.today() -
                 datetime.timedelta(days=END_DAY_OFFSET)).strftime('%Y-%m-%d'),
        help='End date (format YYYY-MM-DD)')
    parser.add_argument(
        '-v', '--variables', nargs='+', default=VARIABLES,
        choices=VARIABLES, metavar='VAR',
        help='CIMIS daily variables')
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

    # Convert relative paths to absolute paths
    if args.workspace and os.path.isdir(os.path.abspath(args.workspace)):
        args.workspace = os.path.abspath(args.workspace)

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
    #     logging.info('\nImage collection does not exist and will be built'
    #                  '\n  {}'.format(ASSET_COLL_ID))
    #     input('Press ENTER to continue')
    #     ee.data.createAsset({'type': 'IMAGE_COLLECTION'}, ASSET_COLL_ID)

    ingest_dt_list = cimis_daily_asset_dates(
        args.start, args.end, overwrite_flag=args.overwrite)
    # print(ingest_dt_list)
    # input('ENTER')

    for ingest_dt in sorted(ingest_dt_list, reverse=args.reverse):
        response = cimis_daily_asset_ingest(
            ingest_dt, variables=args.variables, workspace=args.workspace,
            overwrite_flag=args.overwrite)
        logging.info(f'  {response}')

    # queue_ingest_tasks(ingest_dt_list)
