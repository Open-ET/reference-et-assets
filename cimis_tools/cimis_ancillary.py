import argparse
import datetime
import logging
import math
import os
import pprint
import shutil
import time
import zipfile

import ee
from google.cloud import storage
import numpy as np
import rasterio
import rasterio.crs
import rasterio.warp
import requests

logging.getLogger('earthengine-api').setLevel(logging.INFO)
logging.getLogger('googleapiclient').setLevel(logging.ERROR)
logging.getLogger('requests').setLevel(logging.INFO)
logging.getLogger('urllib3').setLevel(logging.INFO)

PROJECT_NAME = 'openet'
BUCKET_NAME = 'openet'
BUCKET_FOLDER = 'cimis'
STORAGE_CLIENT = storage.Client(project=PROJECT_NAME)
ASSET_FOLDER = 'projects/openet/assets/reference_et/california/cimis/ancillary'


def main(ancillary_ws, overwrite_flag=False):
    """Process CIMIS ancillary data

    Parameters
    ----------
    ancillary_ws : str
        Folder of ancillary rasters.
    overwrite_flag : bool, optional
        If True, overwrite existing files (the default is False).

    Returns
    -------
    None

    """
    logging.info('\nProcess CIMIS ancillary data')

    # Site URL
    site_url = 'http://cimis.casil.ucdavis.edu/cimis'

    # TODO: Get elevation from CASIL site
    # http://cimis.casil.ucdavis.edu/cimis/Z.asc

    elev_name = 'mn30_grd'

    temp_ws = os.path.join(ancillary_ws, 'temp')
    # TODO: Move into temp_ws once script is working
    elev_full_zip = os.path.join(ancillary_ws, f'{elev_name}.zip')
    elev_full_raster = os.path.join(temp_ws, elev_name)

    # DEM for air pressure calculation
    # http://topotools.cr.usgs.gov/gmted_viewer/gmted2010_global_grids.php
    elev_full_url = (
        'http://edcintl.cr.usgs.gov/downloads/sciweb1/shared/topo/downloads/'
        'GMTED/Grid_ZipFiles/{}.zip'.format(elev_name)
    )

    # CIMIS grid
    asset_shape = (560, 510)
    asset_extent = (-410000.0, -660000.0, 610000.0, 460000.0)
    # asset_shape = (552, 500)
    # asset_extent = (-400000.0, -650000.0, 600000.0, 454000.0)
    asset_cs = 2000.0
    # Using RasterIO transform format
    asset_geo = (asset_cs, 0., asset_extent[0], 0., -asset_cs, asset_extent[3])
    # asset_geo = (asset_extent[0], asset_cs, 0., asset_extent[3], 0., -asset_cs)

    # Spatial reference parameters
    asset_proj = rasterio.crs.CRS.from_proj4(
        '+proj=aea +lat_1=34 +lat_2=40.5 +lat_0=0 +lon_0=-120 +x_0=0 '
        '+y_0=-4000000 +ellps=GRS80 +datum=NAD83 +units=m +no_defs'
    )
    # asset_proj = 'EPSG:3310'  # NAD_1983_California_Teale_Albers
    logging.debug(f'CRS: {asset_proj}')

    # Build output workspace if it doesn't exist
    if not os.path.isdir(ancillary_ws):
        os.makedirs(ancillary_ws)

    # File paths
    # Images after ~2018-10-20 have a slightly different mask
    # Images are also setting nodata pixels to 0 (not the nodata value)
    # mask_url = site_url + '/2010/01/01/ETo.asc.gz'
    mask_url = site_url + '/2019/01/01/ETo.asc.gz'
    mask_gz = os.path.join(ancillary_ws, 'cimis_mask.asc.gz')
    mask_ascii = os.path.join(ancillary_ws, 'cimis_mask.asc')
    mask_raster = os.path.join(ancillary_ws, 'cimis_mask.tif')
    elev_raster = os.path.join(ancillary_ws, 'cimis_elev.tif')
    lat_full_raster = os.path.join(ancillary_ws, 'cimis_lat_full.tif')
    lon_full_raster = os.path.join(ancillary_ws, 'cimis_lon_full.tif')
    lat_raster = os.path.join(ancillary_ws, 'cimis_lat.tif')
    lon_raster = os.path.join(ancillary_ws, 'cimis_lon.tif')

    # Download an ETo ASCII raster to generate the mask raster
    if overwrite_flag or not os.path.isfile(mask_raster):
        logging.info('\nCIMIS mask')
        logging.debug('  Downloading')
        logging.debug(f'    {mask_url}')
        logging.debug(f'    {mask_gz}')
        # url_download(mask_url, mask_gz)
        # DEADBEEF - The files are named '.gz' but are not zipped
        logging.debug(f'    {mask_gz}'.format())
        url_download(mask_url, mask_ascii)

        # Convert the ASCII raster to a IMG raster
        logging.debug('  Computing mask')
        logging.debug(f'    {mask_raster}')
        mask_array = ascii_to_array(mask_ascii)
        # Newer CIMIS images aren't using the nodata value
        mask_array = (mask_array > 0)
        mask_array = mask_array.astype(np.uint8)
        # mask_array = np.isfinite(mask_array).astype(np.uint8)
        array_to_geotiff(
            mask_array, mask_raster,
            output_geo=asset_geo, output_proj=asset_proj,
            output_nodata=0, output_type=rasterio.uint8,
        )
        os.remove(mask_ascii)
        del mask_array

        logging.info('  Uploading to bucket')
        bucket = STORAGE_CLIENT.bucket(BUCKET_NAME)
        blob = bucket.blob(f'{BUCKET_FOLDER}/{os.path.basename(mask_raster)}')
        blob.upload_from_filename(mask_raster)

    # Compute latitude/longitude rasters
    if (overwrite_flag or
            not os.path.isfile(lat_raster) or
            not os.path.isfile(lon_raster)):
        logging.info('\nCIMIS latitude/longitude')
        logging.debug(f'  {lat_raster}')

        # Compute the GCS lat/lon grid
        gcs_cs = 0.005
        gcs_transform, gcs_cols, gcs_rows = rasterio.warp.calculate_default_transform(
            src_crs=asset_proj, dst_crs='EPSG:4326',
            width=asset_shape[1], height=asset_shape[0], resolution=gcs_cs,
            left=asset_extent[0], bottom=asset_extent[1],
            right=asset_extent[2], top=asset_extent[3],
        )

        # Snap the projected GCS transform and recompute shape
        snap_x, snap_y = 0, 0
        xmin = math.floor((gcs_transform[2]) / gcs_cs) * gcs_cs
        ymin = math.floor((gcs_transform[5] - gcs_rows * gcs_cs) / gcs_cs) * gcs_cs
        xmax = math.ceil((gcs_transform[2] + gcs_cols * gcs_cs) / gcs_cs) * gcs_cs
        ymax = math.ceil((gcs_transform[5]) / gcs_cs) * gcs_cs
        gcs_transform = [gcs_cs, 0.00, xmin, 0.00, -gcs_cs, ymax]
        gcs_cols = int(round(abs((xmin - xmax) / gcs_cs), 0))
        gcs_rows = int(round(abs((ymax - ymin) / -gcs_cs), 0))

        # Build the GCS lat/lon arrays
        # Cell lat/lon values are measured half a cell in from extent edge
        lon_full_array, lat_full_array = np.meshgrid(
            np.linspace(xmin + 0.5 * gcs_cs, xmax - 0.5 * gcs_cs, gcs_cols),
            np.linspace(ymax - 0.5 * gcs_cs, ymin + 0.5 * gcs_cs, gcs_rows)
        )

        logging.debug(f'  {lat_raster}')
        array_to_geotiff(
            lat_full_array.astype(np.float32), lat_full_raster,
            output_geo=gcs_transform, output_proj='EPSG:4326',
            output_nodata=-9999, output_type=rasterio.float32,
        )
        reproject(
            src_path=lat_full_raster, dst_path=lat_raster,
            dst_crs=asset_proj, dst_geo=asset_geo,
            dst_rows=asset_shape[0], dst_cols=asset_shape[1],
            dst_nodata=-9999, dst_type=rasterio.float32,
            dst_resample=rasterio.warp.Resampling.bilinear,
        )

        logging.debug(f'  {lon_raster}')
        array_to_geotiff(
            lon_full_array.astype(np.float32), lon_full_raster,
            output_geo=gcs_transform, output_proj='EPSG:4326',
            output_nodata=-9999, output_type=rasterio.float32,
        )
        reproject(
            src_path=lon_full_raster, dst_path=lon_raster,
            dst_crs=asset_proj, dst_geo=asset_geo,
            dst_rows=asset_shape[0], dst_cols=asset_shape[1],
            dst_nodata=-9999, dst_type=rasterio.float32,
            dst_resample=rasterio.warp.Resampling.bilinear,
        )

        logging.info('  Uploading to bucket')
        bucket = STORAGE_CLIENT.bucket(BUCKET_NAME)
        blob = bucket.blob(f'{BUCKET_FOLDER}/{os.path.basename(lat_raster)}')
        blob.upload_from_filename(lat_raster)

        bucket = STORAGE_CLIENT.bucket(BUCKET_NAME)
        blob = bucket.blob(f'{BUCKET_FOLDER}/{os.path.basename(lon_raster)}')
        blob.upload_from_filename(lon_raster)

        os.remove(lat_full_raster)
        os.remove(lon_full_raster)

    # Compute DEM raster
    if overwrite_flag or not os.path.isfile(elev_raster):
        logging.info('\nCIMIS DEM')
        if overwrite_flag:
            if os.path.isdir(temp_ws):
                logging.debug('  Removing existing elevation files')
                logging.debug(f'    {temp_ws}')
                shutil.rmtree(temp_ws)
            if os.path.isfile(elev_raster):
                logging.debug('  Removing existing raster')
                logging.debug(f'    {elev_raster}')
                os.remove(elev_raster)
        if not os.path.isdir(temp_ws):
            os.makedirs(temp_ws)

        if not os.path.isfile(elev_full_zip):
            logging.debug('  Downloading GMTED2010 DEM')
            logging.debug(f'    {elev_full_url}')
            logging.debug(f'    {elev_full_zip}')
            url_download(elev_full_url, elev_full_zip)

        if (not os.path.isfile(elev_full_raster) and
                os.path.isfile(elev_full_zip)):
            logging.debug('  Uncompressing')
            logging.debug(f'    {elev_full_raster}')
            try:
                with zipfile.ZipFile(elev_full_zip, 'r') as z:
                    z.extractall(temp_ws)
            except:
                logging.error('  ERROR EXTRACTING FILE')
                os.remove(elev_full_zip)

        if (not os.path.isfile(elev_raster) and
                os.path.isdir(elev_full_raster)):
            logging.debug('  Projecting to CIMIS grid')
            reproject(
                src_path=elev_full_raster, dst_path=elev_raster,
                dst_crs=asset_proj, dst_geo=asset_geo,
                dst_rows=asset_shape[0], dst_cols=asset_shape[1],
                dst_nodata=-9999, dst_type=rasterio.float32,
                dst_resample=rasterio.warp.Resampling.average,
            )

            # # Build overviews
            # dst = rasterio.open(elev_raster, 'r+')
            # dst.build_overviews([2, 4, 8, 16], rasterio.warp.Resampling.average)
            # dst.update_tags(ns='rio_overview', resampling='average')
            # dst.close()

            # project_raster(elev_full_raster, elev_raster,
            #                cimis_osr, cimis_cs, cimis_extent,
            #                gdal.GDT_Float32, rasterio.Resampling.average)
            # args = [
            #     'gdalwarp', '-r', 'average', '-t_srs', cimis_proj4,
            #     '-te', str(cimis_extent[0]), str(cimis_extent[1]),
            #     str(cimis_extent[2]), str(cimis_extent[3]),
            #     '-tr', str(cimis_cs), str(cimis_cs),
            #     '-of', 'GTiff', 'COMPRESS=LZW', 'TILED=YES',
            #     elev_full_raster, elev_raster]
            # subprocess.run(args, cwd=ancillary_ws, shell=True)

        logging.info('  Uploading to bucket')
        bucket = STORAGE_CLIENT.bucket(BUCKET_NAME)
        blob = bucket.blob(f'{BUCKET_FOLDER}/{os.path.basename(elev_raster)}')
        blob.upload_from_filename(elev_raster)

        if os.path.isdir(temp_ws):
            shutil.rmtree(temp_ws)


    # Ingest into Earth Engine
    # DEADBEEF - For now, assume the file is in the bucket
    logging.info('\nIngesting into Earth Engine')
    ee.Initialize()
    asset_params = [
        # [f'{asset_folder}/elevation',
        #  f'gs://{BUCKET_NAME}/{BUCKET_FOLDER}/{os.path.basename(elev_raster)}', 'elev', 'mn30_grd'],
        [f'{ASSET_FOLDER}/latitude',
         f'gs://{BUCKET_NAME}/{BUCKET_FOLDER}/{os.path.basename(lat_raster)}', 'latitude', ''],
        [f'{ASSET_FOLDER}/mask',
         f'gs://{BUCKET_NAME}/{BUCKET_FOLDER}/{os.path.basename(mask_raster)}', 'mask', ''],
    ]
    for asset_id, bucket_path, variable, source in asset_params:
        logging.info(variable)
        task_id = ee.data.newTaskId()[0]
        logging.debug(f'  {task_id}')
        params = {
            'name': asset_id,
            'bands': [{'id': variable, 'tilesetId': 'image', 'tilesetBandIndex': 0}],
            'tilesets': [{'id': 'image', 'sources': [{'uris': [bucket_path]}]}],
            'properties': {
                'date_ingested': f'{datetime.datetime.today().strftime("%Y-%m-%d")}',
            },
            # 'pyramiding_policy': 'MEAN',
            # 'missingData': {'values': [nodata_value]},
        }
        if source:
            params['properties']['source'] = source
        ee.data.startIngestion(task_id, params, allow_overwrite=True)

    logging.debug('\nScript Complete')


def reproject(src_path, dst_path, dst_crs, dst_geo, dst_rows, dst_cols,
              dst_nodata, dst_type, dst_resample):
    """https://rasterio.readthedocs.io/en/latest/topics/reproject.html"""
    with rasterio.open(src_path) as src:
        # transform, width, height = rasterio.warp.calculate_default_transform(
        #     src.crs, dst_crs, src.width, src.height, *src.bounds)
        kwargs = src.meta.copy()
        kwargs.update({
            'driver': 'GTiff',
            'crs': dst_crs,
            'transform': dst_geo,
            'width': dst_cols,
            'height': dst_rows,
            'compress': 'deflate',
            'tiled': True,
            'nodata': dst_nodata,
            'dtype': dst_type,
        })

        with rasterio.open(dst_path, 'w', **kwargs) as dst:
            # for i in range(1, src.count + 1):
            rasterio.warp.reproject(
                source=rasterio.band(src, 1),
                destination=rasterio.band(dst, 1),
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=dst_geo,
                dst_crs=dst_crs,
                resampling=dst_resample,
            )


def ascii_to_array(input_ascii, input_type=np.float32):
    """Convert an ASCII raster to a different file format

    Parameters
    ----------
    input_ascii : str
    input_type

    """
    with open(input_ascii, 'r') as input_f:
        input_header = input_f.readlines()[:6]
    # input_cols = float(input_header[0].strip().split()[-1])
    # input_rows = float(input_header[1].strip().split()[-1])
    # DEADBEEF - I need to check cell corner vs. cell center here
    # input_xmin = float(input_header[2].strip().split()[-1])
    # input_ymin = float(input_header[3].strip().split()[-1])
    # input_cs = float(input_header[4].strip().split()[-1])
    input_nodata = float(input_header[5].strip().split()[-1])
    # input_geo = (
    #     input_cs, 0., input_xmin,
    #     0., -input_cs, input_ymin + input_cs * input_rows
    # )

    output_array = np.genfromtxt(input_ascii, dtype=input_type, skip_header=6)
    output_array[output_array == input_nodata] = np.nan

    return output_array
    # return output_array, input_geo


def array_to_geotiff(output_array, output_path, output_geo, output_proj,
                     output_nodata, output_type=rasterio.float32):
    """Save NumPy array as a geotiff

    Parameters
    ----------
    output_array : np.array
    output_path : str
        GeoTIFF file path.
    output_shape : tuple or list of ints
        Image shape (rows, cols).
    output_geo : tuple or list of floats
        Geo-transform (xmin, cs, 0, ymax, 0, -cs).
    output_proj : str
        Projection Well Known Text (WKT) string.
    output_nodata : float
        GeoTIFF nodata value.
    output_type : str
        RasterIO data type (the default is float32).

    Returns
    -------
    None

    Notes
    -----
    There is no checking of the output_path file extension or that the
    output_array is 2d (1 band).

    """
    output_ds = rasterio.open(
        output_path, 'w', driver='GTiff', nodata=output_nodata,
        width=output_array.shape[1], height=output_array.shape[0], count=1,
        dtype=output_type, crs=output_proj, transform=output_geo,
        compress='deflate', tiled=True,
        # compress='deflate', tiled=True, predictor=2,
        # compress='lzw', tiled=True, predictor=1,
    )
    output_ds.write(output_array, 1)
    output_ds.close()


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


def arg_parse():
    """"""
    parser = argparse.ArgumentParser(
        description='Download/prep CIMIS ancillary data',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        '--ancillary', default=os.path.join(os.getcwd(), 'ancillary'),
        metavar='PATH', help='Ancillary raster folder path')
    parser.add_argument(
        '--overwrite', default=False, action='store_true',
        help='Force overwrite of existing files')
    parser.add_argument(
        '--debug', default=logging.INFO, const=logging.DEBUG,
        help='Debug level logging', action='store_const', dest='loglevel')
    args = parser.parse_args()

    # Convert relative paths to absolute paths
    if args.ancillary and os.path.isdir(os.path.abspath(args.ancillary)):
        args.ancillary = os.path.abspath(args.ancillary)

    return args


if __name__ == '__main__':
    args = arg_parse()
    logging.basicConfig(level=args.loglevel, format='%(message)s')

    main(ancillary_ws=args.ancillary, overwrite_flag=args.overwrite)
