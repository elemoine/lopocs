# -*- coding: utf-8 -*-
import json
import time
from concurrent.futures import ThreadPoolExecutor

from flask import make_response
import numpy
from lazperf import buildNumpyDescription, Decompressor

from .database import Session
from .utils import (
    list_from_str, read_in_cache,
    write_in_cache, Schema, Dimension, boundingbox_to_polygon,
    npoints_from_wkb_pcpatch, hexdata_from_wkb_pcpatch, hexa_signed_int32
)
from .conf import Config
from .stats import Stats

LOADER_GREYHOUND_MIN_DEPTH = 8


def GreyhoundInfo(table, column):
    # invoke a new db session
    session = Session(table, column)

    box = session.lopocstable.bbox
    # get object representing the stored patches format
    stored_patches = session.lopocstable.filter_stored_output()
    # number of points for the first patch
    npoints = session.approx_row_count * session.patch_size

    return {
        "baseDepth": 0,
        "bounds": [box['xmin'], box['ymin'], box['zmin'],
                   box['xmax'], box['ymax'], box['zmax']],
        "boundsConforming": [box['xmin'], box['ymin'], box['zmin'],
                             box['xmax'], box['ymax'], box['zmax']],
        "numPoints": npoints,
        "schema": stored_patches['point_schema'],
        "srs": session.srs,
        "type": "octree",
        "scale": stored_patches['scales'],
        "offset": stored_patches['offsets']
    }


def GreyhoundRead(table, column, offset, scale, bounds, depth, depthBegin, depthEnd, schema):

    session = Session(table, column)

    # we treat scales as list
    scales = [scale] * 3
    # convert string schema to a list of dict
    schema = sorted(json.loads(schema), key=lambda x: x['name'])

    if offset is None and scale is None and bounds is None:
        # normalization request from potree gives no bounds, no scale and
        # no offset, only a schema
        found = False
        for output in session.lopocstable.outputs:
            if schema == sorted(output['point_schema'], key=lambda x: x['name']):
                pcid = output['pcid']
                found = True
        if not found:
            obj = session.lopocstable.outputs[0]
            pcid = session.add_output_schema(
                session.table, session.column,
                obj['scales'][0], obj['scales'][1], obj['scales'][2],
                obj['offsets'][0], obj['offsets'][1], obj['offsets'][2],
                session.lopocstable.srid, schema)
    else:
        offset = list_from_str(offset)
        offsets = [round(off, 2) for off in offset]
        # check if schema, scale and offset exists in our db
        requested = [scales, offsets, sorted(schema, key=lambda x: x['name'])]

        pcid = None
        found = False

        for output in session.lopocstable.outputs:
            oschema = sorted(output['point_schema'], key=lambda x: x['name'])
            if requested == [output['scales'], output['offsets'], oschema]:
                pcid = output['pcid']
                found = True

        if not found:
            # insert new schem
            pcid = session.add_output_schema(
                session.table, session.column,
                scales[0], scales[1], scales[2],
                offsets[0], offsets[1], offsets[2],
                session.lopocstable.srid, schema)

    # prepare parameters
    if not bounds and depth == 0:
        bbox = [
            session.boundingbox['xmin'],
            session.boundingbox['ymin'],
            session.boundingbox['zmin'],
            session.boundingbox['xmax'],
            session.boundingbox['ymax'],
            session.boundingbox['zmax']
        ]
    else:
        bbox = list_from_str(bounds)
        # apply scale and offset to bbox for querying database
        bbox[0] = bbox[0] * scales[0] + offset[0]
        bbox[1] = bbox[1] * scales[1] + offset[1]
        bbox[2] = bbox[2] * scales[2] + offset[2]
        bbox[3] = bbox[3] * scales[0] + offset[0]
        bbox[4] = bbox[4] * scales[1] + offset[1]
        bbox[5] = bbox[5] * scales[2] + offset[2]

    if depth is not None:
        lod = 0
    else:
        lod = depthEnd - LOADER_GREYHOUND_MIN_DEPTH - 1

    # get points in database
    if Config.STATS:
        t0 = int(round(time.time() * 1000))

    [read, npoints] = get_points(session, bbox, pcid, lod)

    if Config.STATS:
        t1 = int(round(time.time() * 1000))

    # log stats
    if npoints > 0 and Config.STATS:
        stats = Stats.get()
        stats_npoints = stats['npoints'] + npoints
        Stats.set(stats_npoints, (t1 - t0) + stats['time_msec'])
        stats = Stats.get()
        print("Points/sec: ", stats['rate_sec'])

    # build flask response
    response = make_response(read)
    response.headers['content-type'] = 'application/octet-stream'
    return response


def GreyhoundHierarchy(table, column, bounds, depthBegin, depthEnd, scale, offset):

    session = Session(table, column)

    lod_min = depthBegin - LOADER_GREYHOUND_MIN_DEPTH

    lod_max = depthEnd - LOADER_GREYHOUND_MIN_DEPTH - 1
    if lod_max > (Config.DEPTH - 1):
        lod_max = Config.DEPTH - 1

    bbox = list_from_str(bounds)

    if offset:
        # apply scale and offset if needed
        offset = list_from_str(offset)
        bbox[0] = bbox[0] * scale + offset[0]
        bbox[1] = bbox[1] * scale + offset[1]
        bbox[2] = bbox[2] * scale + offset[2]
        bbox[3] = bbox[3] * scale + offset[0]
        bbox[4] = bbox[4] * scale + offset[1]
        bbox[5] = bbox[5] * scale + offset[2]

    if lod_min == 0 and Config.ROOT_HCY:
        filename = Config.ROOT_HCY
    else:
        filename = ("{0}_{1}_{2}_{3}_{4}.hcy"
                    .format(session.table, session.column, lod_min, lod_max,
                            '_'.join(str(e) for e in bbox)))
    cached_hcy = read_in_cache(filename)

    if Config.DEBUG:
        print("hierarchy file: {0}".format(filename))

    if cached_hcy:
        resp = cached_hcy
    else:

        new_hcy = build_hierarchy_from_pg(session, lod_min, lod_max, bbox)
        write_in_cache(new_hcy, filename)
        resp = new_hcy

    return resp


class GreyhoundInfoSchema(Schema):

    def __init__(self):
        Schema.__init__(self)

        self.dims.append(Dimension("X", "floating", 8))
        self.dims.append(Dimension("Y", "floating", 8))
        self.dims.append(Dimension("Z", "floating", 8))
        self.dims.append(Dimension("Intensity", "unsigned", 2))
        self.dims.append(Dimension("Classification", "unsigned", 1))
        self.dims.append(Dimension("Red", "unsigned", 2))
        self.dims.append(Dimension("Green", "unsigned", 2))
        self.dims.append(Dimension("Blue", "unsigned", 2))


class GreyhoundReadSchema(Schema):

    def __init__(self):
        Schema.__init__(self)

        self.dims.append(Dimension("X", "signed", 4))
        self.dims.append(Dimension("Y", "signed", 4))
        self.dims.append(Dimension("Z", "signed", 4))
        self.dims.append(Dimension("Intensity", "unsigned", 2))
        self.dims.append(Dimension("Classification", "unsigned", 1))
        self.dims.append(Dimension("Red", "unsigned", 2))
        self.dims.append(Dimension("Green", "unsigned", 2))
        self.dims.append(Dimension("Blue", "unsigned", 2))


def sql_hierarchy(session, box, lod):
    poly = boundingbox_to_polygon(box)

    maxpp_patch = session.lopocstable.max_points_per_patch
    maxpp_query = session.lopocstable.max_patches_per_query

    # retrieve the number of points to select in a pcpatch
    range_min = 0
    range_max = 1

    if maxpp_patch:
        range_min = 0
        range_max = maxpp_patch
    else:
        beg = 0
        for i in range(0, lod):
            beg = beg + pow(4, i)

        end = 0
        for i in range(0, lod + 1):
            end = end + pow(4, i)

        range_min = beg
        range_max = end - beg

    # build the sql query
    sql_limit = ""
    if maxpp_query:
        sql_limit = " limit {0} ".format(maxpp_query)

    if Config.USE_MORTON:
        sql = """
        select
            pc_union(pc_filterbetween(pc_range({0}, {4}, {5}), 'Z', {6}, {7} ))
        from
            (
                select {0} from {1}
                where pc_intersects(
                    {0},
                    st_geomfromtext('polygon (({2}))',{3})
                ) order by morton {8}
            )_
        """.format(session.column, session.table, poly, session.srsid,
                   range_min, range_max, box[2], box[5], sql_limit)
    else:
        sql = """
        select
            pc_union(pc_filterbetween(pc_range({0}, {4}, {5}), 'Z', {6}, {7} ))
        from
           (
                select {0} from {1}
                where pc_intersects(
                    {0},
                    st_geomfromtext('polygon (({2}))',{3})
                ) {8}
            )_
        """.format(session.column, session.table, poly, session.srsid,
                   range_min, range_max, box[2], box[5], sql_limit)
    return sql


def get_points_query(session, box, schema_pcid, lod):
    poly = boundingbox_to_polygon(box)

    # retrieve the number of points to select in a pcpatch
    range_min = 0
    range_max = 1

    maxppp = session.lopocstable.max_points_per_patch

    if maxppp:
        range_min = 0
        range_max = maxppp
    else:
        # adapted to midoc filter
        beg = 0
        for i in range(0, lod):
            beg = beg + pow(4, i)

        end = 0
        for i in range(0, lod + 1):
            end = end + pow(4, i)

        range_min = beg
        range_max = end - beg

    # build the sql query
    sql_limit = ""
    maxppq = session.lopocstable.max_patches_per_query
    if maxppq:
        sql_limit = " limit {0} ".format(maxppq)

    if Config.USE_MORTON:
        sql = """
        select
            pc_compress(
                pc_setpcid(
                    pc_union(
                        pc_filterbetween(
                            pc_range({0}, {4}, {5}), 'Z', {6}, {7})
                        ), {9}
                    ), 'laz'
                )
        from
            (
                select {0} from {1}
                where pc_intersects(
                    {0},
                    st_geomfromtext('polygon (({2}))',{3})
                ) order by morton {8}
            )_
        """.format(session.column, session.table, poly, session.srsid,
                   range_min, range_max, box[2], box[5], sql_limit, schema_pcid)

    else:
        sql = """
        select
            pc_compress(
                pc_setpcid(
                    pc_union(
                        pc_filterbetween(
                            pc_range({0}, {4}, {5}), 'Z', {6}, {7} )
                        ), {9}
                    ), 'laz'
            )
        from
           (
                select {0} from {1}
                where pc_intersects(
                    {0},
                    st_geomfromtext('polygon (({2}))',{3})
                ) {8}
            )_
        """.format(session.column, session.table, poly, session.srsid,
                   range_min, range_max, box[2], box[5], sql_limit, schema_pcid)
    return sql


def get_points(session, box, schema_pcid, lod):

    npoints = 0
    hexbuffer = bytearray()
    sql = get_points_query(session, box, schema_pcid, lod)

    if Config.DEBUG:
        print(sql)

    try:
        pcpatch_wkb = session.query(sql)[0][0]
        # to test output from pgpointcloud :

        # get json schema representation
        # schema = session.lopocstable.point_schema
        # decompress(pcpatch_wkb, schema)

        # retrieve number of points in wkb pgpointcloud patch
        npoints = npoints_from_wkb_pcpatch(pcpatch_wkb)

        # extract data
        hexbuffer = hexdata_from_wkb_pcpatch(pcpatch_wkb)

        # add number of points
        hexbuffer += hexa_signed_int32(npoints)
    except:
        hexbuffer.extend(hexa_signed_int32(0))

    if Config.DEBUG:
        print("LOD: ", lod)
        print("DEPTH: ", Config.DEPTH)
        print("NUM POINTS RETURNED: ", npoints)

    return [hexbuffer, npoints]


def fake_hierarchy(begin, end, npatchs):
    p = {}
    begin = begin + 1

    if begin != end:
        p['n'] = npatchs

        if begin != (end - 1):
            p['nwu'] = fake_hierarchy(begin, end, npatchs)
            p['nwd'] = fake_hierarchy(begin, end, npatchs)
            p['neu'] = fake_hierarchy(begin, end, npatchs)
            p['ned'] = fake_hierarchy(begin, end, npatchs)
            p['swu'] = fake_hierarchy(begin, end, npatchs)
            p['swd'] = fake_hierarchy(begin, end, npatchs)
            p['seu'] = fake_hierarchy(begin, end, npatchs)
            p['sed'] = fake_hierarchy(begin, end, npatchs)

    return p


def build_hierarchy_from_pg(session, lod, lod_max, bbox):

    # pcid is needed to get max attributes
    sql = sql_hierarchy(session, bbox, lod)
    pcpatch_wkb = session.query(sql)[0][0]

    hierarchy = {}
    if lod <= lod_max and pcpatch_wkb:
        npoints = npoints_from_wkb_pcpatch(pcpatch_wkb)
        hierarchy['n'] = npoints

    lod += 1

    # run leaf in threads
    if lod <= lod_max:
        # width / length / height
        width = bbox[3] - bbox[0]
        length = bbox[4] - bbox[1]
        height = bbox[5] - bbox[2]

        up = bbox[5]
        middle = up - height / 2
        down = bbox[2]

        x = bbox[0]
        y = bbox[1]

        # build bboxes for leaf
        bbox_nwd = [x, y + length / 2, down, x + width / 2, y + length, middle]
        bbox_nwu = [x, y + length / 2, middle, x + width / 2, y + length, up]
        bbox_ned = [x + width / 2, y + length / 2, down, x + width, y + length, middle]
        bbox_neu = [x + width / 2, y + length / 2, middle, x + width, y + length, up]
        bbox_swd = [x, y, down, x + width / 2, y + length / 2, middle]
        bbox_swu = [x, y, middle, x + width / 2, y + length / 2, up]
        bbox_sed = [x + width / 2, y, down, x + width, y + length / 2, middle]
        bbox_seu = [x + width / 2, y, middle, x + width, y + length / 2, up]

        # run leaf in threads
        futures = {}
        with ThreadPoolExecutor(max_workers=Session.pool.maxconn) as e:
            futures["nwd"] = e.submit(build_hierarchy_from_pg_single, session, lod, lod_max, bbox_nwd)
            futures["nwu"] = e.submit(build_hierarchy_from_pg_single, session, lod, lod_max, bbox_nwu)
            futures["ned"] = e.submit(build_hierarchy_from_pg_single, session, lod, lod_max, bbox_ned)
            futures["neu"] = e.submit(build_hierarchy_from_pg_single, session, lod, lod_max, bbox_neu)
            futures["swd"] = e.submit(build_hierarchy_from_pg_single, session, lod, lod_max, bbox_swd)
            futures["swu"] = e.submit(build_hierarchy_from_pg_single, session, lod, lod_max, bbox_swu)
            futures["sed"] = e.submit(build_hierarchy_from_pg_single, session, lod, lod_max, bbox_sed)
            futures["seu"] = e.submit(build_hierarchy_from_pg_single, session, lod, lod_max, bbox_seu)

        for code, hier in futures.items():
            hierarchy[code] = hier.result()

    return hierarchy


def build_hierarchy_from_pg_single(session, lod, lod_max, bbox):
    # run sql
    sql = sql_hierarchy(session, bbox, lod)
    pcpatch_wkb = session.query(sql)[0][0]
    hierarchy = {}
    if lod <= lod_max and pcpatch_wkb:
        npoints = npoints_from_wkb_pcpatch(pcpatch_wkb)
        hierarchy['n'] = npoints

    lod += 1

    if lod <= lod_max:
        # width  /  length  /  height
        width = bbox[3] - bbox[0]
        length = bbox[4] - bbox[1]
        height = bbox[5] - bbox[2]

        up = bbox[5]
        middle = up - height / 2
        down = bbox[2]

        x = bbox[0]
        y = bbox[1]

        # nwd
        bbox_nwd = [x, y + length / 2, down, x + width / 2, y + length, middle]
        h_nwd = build_hierarchy_from_pg_single(session, lod, lod_max, bbox_nwd)
        if h_nwd:
            hierarchy['nwd'] = h_nwd

        # nwu
        bbox_nwu = [x, y + length / 2, middle, x + width / 2, y + length, up]
        h_nwu = build_hierarchy_from_pg_single(session, lod, lod_max, bbox_nwu)
        if h_nwu:
            hierarchy['nwu'] = h_nwu

        # ned
        bbox_ned = [x + width / 2, y + length / 2, down, x + width, y + length, middle]
        h_ned = build_hierarchy_from_pg_single(session, lod, lod_max, bbox_ned)
        if h_ned:
            hierarchy['ned'] = h_ned

        # neu
        bbox_neu = [x + width / 2, y + length / 2, middle, x + width, y + length, up]
        h_neu = build_hierarchy_from_pg_single(session, lod, lod_max, bbox_neu)
        if h_neu:
            hierarchy['neu'] = h_neu

        # swd
        bbox_swd = [x, y, down, x + width / 2, y + length / 2, middle]
        h_swd = build_hierarchy_from_pg_single(session, lod, lod_max, bbox_swd)
        if h_swd:
            hierarchy['swd'] = h_swd

        # swu
        bbox_swu = [x, y, middle, x + width / 2, y + length / 2, up]
        h_swu = build_hierarchy_from_pg_single(session, lod, lod_max, bbox_swu)
        if h_swu:
            hierarchy['swu'] = h_swu

        # sed
        bbox_sed = [x + width / 2, y, down, x + width, y + length / 2, middle]
        h_sed = build_hierarchy_from_pg_single(session, lod, lod_max, bbox_sed)
        if h_sed:
            hierarchy['sed'] = h_sed

        # seu
        bbox_seu = [x + width / 2, y, middle, x + width, y + length / 2, up]
        h_seu = build_hierarchy_from_pg_single(session, lod, lod_max, bbox_seu)
        if h_seu:
            hierarchy['seu'] = h_seu

    return hierarchy


def decompress(points, schema):
    """
    'points' is a pcpatch in wkb
    """

    # retrieve number of points in wkb pgpointcloud patch
    npoints = npoints_from_wkb_pcpatch(points)
    hexbuffer = hexdata_from_wkb_pcpatch(points)
    hexbuffer += hexa_signed_int32(npoints)

    # uncompress
    s = json.dumps(schema).replace("\\", "")
    # s = json.dumps(GreyhoundReadSchema().json()).replace("\\", "")
    dtype = buildNumpyDescription(json.loads(s))

    lazdata = bytes(hexbuffer)

    arr = numpy.fromstring(lazdata, dtype=numpy.uint8)
    d = Decompressor(arr, s)
    output = numpy.zeros(npoints * dtype.itemsize, dtype=numpy.uint8)
    decompressed = d.decompress(output)
    print('point size', dtype.itemsize)
    print('X: ', decompressed['X'][0], 'Y: ', decompressed['Y'][0], 'Z: ', decompressed['Z'][0])

    decompressed_str = numpy.ndarray.tostring(decompressed)
    return [decompressed_str, dtype.itemsize]
