#!/usr/bin/env python
# -*- coding: utf-8 -*-
import io
import os
import re
import sys
import shlex
import json
from zipfile import ZipFile
from datetime import datetime
from pathlib import Path
from subprocess import check_call, call, check_output, CalledProcessError, DEVNULL

import click
import requests
from osgeo.osr import SpatialReference
from flask_cors import CORS
from pyproj import Proj, transform

from lopocs import __version__
from lopocs import create_app, greyhound, threedtiles
from lopocs.database import Session
from lopocs.potreeschema import potree_schema
from lopocs.potreeschema import potree_page
from lopocs.cesium import cesium_page


samples = {
    'airport': 'http://www.liblas.org/samples/LAS12_Sample_withRGB_Quick_Terrain_Modeler_fixed.las',
    'sthelens': 'http://www.liblas.org/samples/st-helens.las',
    'grandlyon': 'https://download.data.grandlyon.com/files/grandlyon/imagerie/mnt2015/lidar/1842_5175.zip'
}


def fatal(message):
    '''print error and exit'''
    click.echo('\nFATAL: {}'.format(message), err=True)
    sys.exit(1)


def pending(msg, nl=False):
    click.echo('[{}] {} ... '.format(
        datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        msg
    ), nl=nl)


def green(message):
    click.secho(message.replace('\n', ''), fg='green')


def ok(mess=None):
    if mess:
        click.secho('{} : '.format(mess.replace('\n', '')), nl=False)
    click.secho('ok', fg='green')


def ko(mess=None):
    if mess:
        click.secho('{} : '.format(mess.replace('\n', '')), nl=False)
    click.secho('ko', fg='red')


def download(label, url, dest):
    '''
    download url using requests and a progressbar
    '''
    r = requests.get(url, stream=True)
    length = int(r.headers['content-length'])

    chunk_size = 512
    iter_size = 0
    with io.open(dest, 'wb') as fd:
        with click.progressbar(length=length, label=label) as bar:
            for chunk in r.iter_content(chunk_size):
                fd.write(chunk)
                iter_size += chunk_size
                bar.update(chunk_size)


def print_version(ctx, param, value):
    if not value or ctx.resilient_parsing:
        return
    click.echo('LOPoCS version {}'.format(__version__))
    click.echo('')
    ctx.exit()


@click.group()
@click.option('--version', help='show version', is_flag=True, expose_value=False, callback=print_version)
def cli():
    '''lopocs command line tools'''
    pass


@cli.command()
def serve():
    '''run lopocs server (development usage)'''
    app = create_app()
    CORS(app)
    app.run()


def cmd_rt(message, command):
    '''wrapper around call function
    '''
    click.echo('{} ... '.format(message), nl=False)
    rt = call(command, shell=True)
    if rt != 0:
        ko()
        return
    ok()


def cmd_output(message, command):
    '''wrapper check_call function
    '''
    click.echo('{} ... '.format(message), nl=False)
    try:
        output = check_output(shlex.split(command)).decode()
        green(output)
    except Exception as exc:
        ko(str(exc))


def cmd_pg(message, request):
    '''wrapper around a session query
    '''
    click.echo('{} ... '.format(message), nl=False)
    try:
        result = Session.query(request)
        if not result:
            raise Exception('Not found')
        green(result[0][0])
    except Exception as exc:
        ko(str(exc))


@cli.command()
def check():
    '''check lopocs configuration and dependencies'''
    try:
        app = create_app()
    except Exception as exc:
        fatal(str(exc))

    if not app:
        fatal("it appears that you don't have any configuration file")

    # pdal
    cmd_output('Pdal', 'pdal-config --version')
    cmd_rt('Pdal plugin pgpointcloud', "test -e `pdal-config --plugin-dir`/libpdal_plugin_writer_pgpointcloud.so")
    cmd_rt('Pdal plugin revertmorton', "test -e `pdal-config --plugin-dir`/libpdal_plugin_filter_revertmorton.so")

    # postgresql and extensions
    cmd_pg('PostgreSQL', 'show server_version')
    cmd_pg('PostGIS extension', "select default_version from pg_available_extensions where name = 'postgis'")
    cmd_pg('PgPointcloud extension', "select default_version from pg_available_extensions where name = 'pointcloud'")
    cmd_pg('PgPointcloud-PostGIS extension', "select default_version from pg_available_extensions where name = 'pointcloud_postgis'")


@click.option('--table', required=True, help='table name to store pointclouds, considered in public schema if no prefix provided')
@click.option('--column', help="column name to store patches", default="points", type=str)
@click.option('--work-dir', type=click.Path(exists=True), required=True, help="working directory where temporary files will be saved")
@click.option('--server-url', type=str, help="server url for lopocs", default="http://localhost:5000")
@click.option('--capacity', type=int, default=400, help="number of points in a pcpatch")
@click.option('--potree', 'usewith', help="load data for use with greyhound/potree", flag_value='potree')
@click.option('--cesium', 'usewith', help="load data for use with use 3dtiles/cesium ", default=True, flag_value='cesium')
@click.argument('filename', type=click.Path(exists=True))
@cli.command()
def load(filename, table, column, work_dir, server_url, capacity, usewith):
    '''load pointclouds data using pdal and add metadata needed by lopocs'''
    _load(filename, table, column, work_dir, server_url, capacity, usewith)


def _load(filename, table, column, work_dir, server_url, capacity, usewith):
    '''load pointclouds data using pdal and add metadata needed by lopocs'''
    # intialize flask application
    app = create_app()

    filename = Path(filename)
    work_dir = Path(work_dir)
    extension = filename.suffix[1:].lower()
    basename = filename.stem
    basedir = filename.parent

    pending('Creating metadata table')
    Session.create_pointcloud_lopocs_table()
    ok()

    pending('Loading point clouds into database')
    json_path = os.path.join(
        str(work_dir.resolve()),
        '{basename}_{table}_pipeline.json'.format(**locals()))

    # tablename should be always prefixed
    if '.' not in table:
        table = 'public.{}'.format(table)

    cmd = "pdal info --summary {}".format(filename)
    try:
        output = check_output(shlex.split(cmd))
    except CalledProcessError as e:
        fatal(e)

    summary = json.loads(output.decode())['summary']

    if summary['srs']['isgeographic']:
        # geographic
        scale_x, scale_y, scale_z = (1e-6, 1e-6, 1e-2)
    else:
        # projection or geocentric
        scale_x, scale_y, scale_z = (0.01, 0.01, 0.01)

    offset_x = summary['bounds']['X']['min'] + (summary['bounds']['X']['max'] - summary['bounds']['X']['min']) / 2
    offset_y = summary['bounds']['Y']['min'] + (summary['bounds']['Y']['max'] - summary['bounds']['Y']['min']) / 2
    offset_z = summary['bounds']['Z']['min'] + (summary['bounds']['Z']['max'] - summary['bounds']['Z']['min']) / 2

    reproject = ""
    srid = proj42epsg(summary['srs']['proj4'])

    if usewith == 'cesium':
        # cesium only use epsg:4978, so we must reproject before loading into pg
        from_srid = proj42epsg(summary['srs']['proj4'])
        srid = 4978
        pini = Proj(init='epsg:{}'.format(from_srid))
        pout = Proj(init='epsg:{}'.format(srid))
        offset_x, offset_y, offset_z = transform(pini, pout, offset_x, offset_y, offset_z)
        reproject = """
        {{
           "type":"filters.reprojection",
           "in_srs":"EPSG:{from_srid}",
           "out_srs":"EPSG:{srid}"
        }},""".format(**locals())

    offset_x = round(offset_x, 2)
    offset_y = round(offset_y, 2)
    offset_z = round(offset_z, 2)

    pg_host = app.config['PG_HOST']
    pg_name = app.config['PG_NAME']
    pg_port = app.config['PG_PORT']
    pg_user = app.config['PG_USER']
    pg_password = app.config['PG_PASSWORD']
    realfilename = str(filename.resolve())
    schema, tab = table.split('.')

    json_pipeline = """
{{
"pipeline": [
    {{
        "type": "readers.{extension}",
        "filename":"{realfilename}"
    }},
    {{
        "type": "filters.chipper",
        "capacity": "{capacity}"
    }},
    {reproject}
    {{
        "type": "filters.revertmorton"
    }},
    {{
        "type":"writers.pgpointcloud",
        "connection":"dbname={pg_name} host={pg_host} port={pg_port} user={pg_user} password={pg_password}",
        "schema": "{schema}",
        "table":"{tab}",
        "compression":"none",
        "srid":"{srid}",
        "overwrite":"true",
        "column": "{column}",
        "scale_x": "{scale_x}",
        "scale_y": "{scale_y}",
        "scale_z": "{scale_z}",
        "offset_x": "{offset_x}",
        "offset_y": "{offset_y}",
        "offset_z": "{offset_z}"
    }}
]
}}""".format(**locals())

    with io.open(json_path, 'w') as json_file:
        json_file.write(json_pipeline)

    cmd = "pdal pipeline {}".format(json_path)

    try:
        check_call(shlex.split(cmd), stderr=DEVNULL, stdout=DEVNULL)
    except CalledProcessError as e:
        fatal(e)
    ok()

    pending("Creating indexes")
    Session.execute("""
        create index on {table} using gist(geometry(points));
        alter table {table} add column morton bigint;
        select Morton_Update('{table}', 'points', 'morton', 128, TRUE);
        create index on {table}(morton);
    """.format(**locals()))
    ok()

    pending("Adding metadata for lopocs")
    Session.update_metadata(
        table, column, srid, scale_x, scale_y, scale_z,
        offset_x, offset_y, offset_z
    )
    lpsession = Session(table, column)
    ok()

    # initialize range for level of details
    lod_min = 0
    lod_max = 5

    # retrieve boundingbox
    fullbbox = lpsession.boundingbox
    bbox = [
        fullbbox['xmin'], fullbbox['ymin'], fullbbox['zmin'],
        fullbbox['xmax'], fullbbox['ymax'], fullbbox['zmax']
    ]

    if usewith == 'potree':
        # add schema currently used by potree (version 1.5RC)
        Session.add_output_schema(
            table, column, 0.01, 0.01, 0.01,
            offset_x, offset_y, offset_z, srid, potree_schema
        )
        cache_file = (
            "{0}_{1}_{2}_{3}_{4}.hcy".format(
                lpsession.table,
                lpsession.column,
                lod_min,
                lod_max,
                '_'.join(str(e) for e in bbox)
            )
        )
        pending("Building greyhound hierarchy")
        new_hcy = greyhound.build_hierarchy_from_pg(
            lpsession, lod_min, lod_max, bbox
        )
        greyhound.write_in_cache(new_hcy, cache_file)
        ok()
        create_potree_page(str(work_dir.resolve()), server_url, table, column)

    if usewith == 'cesium':
        pending("Building 3Dtiles tileset")
        hcy = threedtiles.build_hierarchy_from_pg(
            lpsession, server_url, lod_max, bbox, lod_min
        )

        tileset = os.path.join(str(work_dir.resolve()), 'tileset-{}.{}.json'.format(table, column))

        with io.open(tileset, 'wb') as out:
            out.write(hcy.encode())
        ok()
        create_cesium_page(str(work_dir.resolve()), table, column)


def create_potree_page(work_dir, server_url, tablename, column):
    '''Create an html demo page with potree viewer
    '''
    # get potree build
    potree = os.path.join(work_dir, 'potree')
    potreezip = os.path.join(work_dir, 'potree.zip')
    if not os.path.exists(potree):
        download('Getting potree code', 'http://3d.oslandia.com/potree.zip', potreezip)
        # unzipping content
        with ZipFile(potreezip) as myzip:
            myzip.extractall(path=work_dir)
    tablewschema = tablename.split('.')[-1]
    sample_page = os.path.join(work_dir, 'potree-{}.html'.format(tablewschema))
    abs_sample_page = str(Path(sample_page).absolute())
    pending('Creating a potree demo page : file://{}'.format(abs_sample_page))
    resource = '{}.{}'.format(tablename, column)
    server_url = server_url.replace('http://', '')
    with io.open(sample_page, 'wb') as html:
        html.write(potree_page.format(resource=resource, server_url=server_url).encode())
    ok()


def create_cesium_page(work_dir, tablename, column):
    '''Create an html demo page with cesium viewer
    '''
    cesium = os.path.join(work_dir, 'cesium')
    cesiumzip = os.path.join(work_dir, 'cesium.zip')
    if not os.path.exists(cesium):
        download('Getting cesium code', 'http://3d.oslandia.com/cesium.zip', cesiumzip)
        # unzipping content
        with ZipFile(cesiumzip) as myzip:
            myzip.extractall(path=work_dir)
    tablewschema = tablename.split('.')[-1]
    sample_page = os.path.join(work_dir, 'cesium-{}.html'.format(tablewschema))
    abs_sample_page = str(Path(sample_page).absolute())
    pending('Creating a cesium demo page : file://{}'.format(abs_sample_page))
    resource = '{}.{}'.format(tablename, column)
    with io.open(sample_page, 'wb') as html:
        html.write(cesium_page.format(resource=resource).encode())
    ok()


@cli.command()
@click.option('--sample', help="sample data available", default="airport", type=click.Choice(samples.keys()))
@click.option('--work-dir', type=click.Path(exists=True), required=True, help="working directory where sample files will be saved")
@click.option('--server-url', type=str, help="server url for lopocs", default="http://localhost:5000")
@click.option('--potree', 'usewith', help="load data for use with greyhound/potree", flag_value='potree')
@click.option('--cesium', 'usewith', help="load data for use with use 3dtiles/cesium ", default=True, flag_value='cesium')
def demo(sample, work_dir, server_url, usewith):
    '''
    download sample lidar data, load it into pgpointcloud
    '''
    filepath = Path(samples[sample])
    pending('Using sample data {}: {}'.format(sample, filepath.name))
    dest = os.path.join(work_dir, filepath.name)
    ok()

    if not os.path.exists(dest):
        download('Downloading sample', samples[sample], dest)

    # now load data
    _load(dest, sample, 'points', work_dir, server_url, 400, usewith)

    click.echo(
        'Now you can start lopocs with "lopocs serve"'
        .format(sample)
    )


def proj42epsg(proj4, epsg='/usr/share/proj/epsg', forceProj4=False):
    ''' Transform a WKT string to an EPSG code

    Arguments
    ---------

    proj4: proj4 string definition
    epsg: the proj.4 epsg file (defaults to '/usr/local/share/proj/epsg')
    forceProj4: whether to perform brute force proj4 epsg file check (last resort)

    Returns: EPSG code

    '''
    code = '4326'
    p_in = SpatialReference()
    s = p_in.ImportFromProj4(proj4)
    if s == 5:  # invalid WKT
        return '%s' % code
    if p_in.IsLocal() == 1:  # this is a local definition
        return p_in.ExportToWkt()
    if p_in.IsGeographic() == 1:  # this is a geographic srs
        cstype = 'GEOGCS'
    else:  # this is a projected srs
        cstype = 'PROJCS'
    an = p_in.GetAuthorityName(cstype)
    ac = p_in.GetAuthorityCode(cstype)
    if an is not None and ac is not None:  # return the EPSG code
        return '%s' % p_in.GetAuthorityCode(cstype)
    else:  # try brute force approach by grokking proj epsg definition file
        p_out = p_in.ExportToProj4()
        if p_out:
            if forceProj4 is True:
                return p_out
            f = open(epsg)
            for line in f:
                if line.find(p_out) != -1:
                    m = re.search('<(\\d+)>', line)
                    if m:
                        code = m.group(1)
                        break
            if code:  # match
                return '%s' % code
            else:  # no match
                return '%s' % code
        else:
            return '%s' % code
