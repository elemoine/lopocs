# -*- coding: utf-8 -*-
import json
import time
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import cpu_count

from flask import Response
import numpy
from lazperf import buildNumpyDescription, Decompressor

from .database import Session
from .utils import (
    decimal_default, list_from_str, read_in_cache,
    write_in_cache, Schema, Dimension, boundingbox_to_polygon,
    npoints_from_wkb_pcpatch, hexdata_from_wkb_pcpatch, hexa_signed_int32
)
from .conf import Config
from .stats import Stats

LOADER_GREYHOUND_MIN_DEPTH = 8


def GreyhoundInfo(args):
    # invoke a new db session
    session = Session(args['table'], args['column'])
    # bounding box
    if (Config.BB):
        box = Config.BB
    else:
        box = session.boundingbox()

    # number of points for the first patch
    npoints = session.approx_row_count * session.patch_size

    # srs
    srs = session.srs()

    # build the greyhound schema
    schema_json = GreyhoundInfoSchema().json()

    info = json.dumps({
        "baseDepth": 0,
        "bounds": [box['xmin'], box['ymin'], box['zmin'],
                   box['xmax'], box['ymax'], box['zmax']],
        "boundsConforming": [box['xmin'], box['ymin'], box['zmin'],
                             box['xmax'], box['ymax'], box['zmax']],
        "numPoints": npoints,
        "schema": schema_json,
        "srs": srs,
        "type": "octree"
    }, default=decimal_default)

    # build the flask response
    resp = Response(info)
    resp.headers['Content-Type'] = 'text/plain'

    return resp


def GreyhoundRead(args):

    session = Session(args['table'], args.get('column', 'pa'))

    # prepare parameters
    offset = list_from_str(args['offset'])
    box = list_from_str(args['bounds'])
    lod = args['depthEnd'] - LOADER_GREYHOUND_MIN_DEPTH - 1

    # get points in database
    if Config.STATS:
        t0 = int(round(time.time() * 1000))

    [read, npoints] = get_points(
        session, box, offset, session.output_pcid(args['scale']), lod
    )

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
    resp = Response(read)
    resp.headers['Content-Type'] = 'application/octet-stream'

    return resp


def GreyhoundHierarchy(args):

    session = Session(args['table'], args.get('column', 'pa'))

    lod_min = args['depthBegin'] - LOADER_GREYHOUND_MIN_DEPTH

    lod_max = args['depthEnd'] - LOADER_GREYHOUND_MIN_DEPTH - 1
    if lod_max > (Config.DEPTH - 1):
        lod_max = Config.DEPTH - 1

    bbox = list_from_str(args['bounds'])

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
        resp = Response(json.dumps(cached_hcy))
    else:
        new_hcy = build_hierarchy_from_pg_mp(session, lod_max, bbox, lod_min)
        write_in_cache(new_hcy, filename)
        resp = Response(json.dumps(new_hcy))

    # resp = Response(json.dumps(fake_hierarchy(0, 6, 10000)))

    resp.headers['Content-Type'] = 'text/plain'

    return resp


# -----------------------------------------------------------------------------
# schema
# -----------------------------------------------------------------------------
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
    # retrieve the number of points to select in a pcpatch
    range_min = 0
    range_max = 1

    if Config.MAX_POINTS_PER_PATCH:
        range_min = 0
        range_max = Config.MAX_POINTS_PER_PATCH
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
    if Config.MAX_PATCHS_PER_QUERY:
        sql_limit = " limit {0} ".format(Config.MAX_PATCHS_PER_QUERY)

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


def sql_query(session, box, schema_pcid, lod):
    poly = boundingbox_to_polygon(box)
    # retrieve the number of points to select in a pcpatch
    range_min = 0
    range_max = 1

    if Config.MAX_POINTS_PER_PATCH:
        range_min = 0
        range_max = Config.MAX_POINTS_PER_PATCH
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
    if Config.MAX_PATCHS_PER_QUERY:
        sql_limit = " limit {0} ".format(Config.MAX_PATCHS_PER_QUERY)

    if Config.USE_MORTON:
        sql = """
        select
            pc_compress(
                pc_patchtransform(
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
                pc_patchtransform(
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


def get_points(session, box, offset, schema_pcid, lod):

    npoints = 0
    hexbuffer = bytearray()
    sql = sql_query(session, box, schema_pcid, lod)

    if Config.DEBUG:
        print(sql)

    try:
        pcpatch_wkb = session.query(sql)[0][0]
        # to test output from pgpointcloud : decompress(points)

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


def build_hierarchy_from_pg_mp(session, lod_max, bbox, lod):

    # extract root level
    lod = 0
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
        with ThreadPoolExecutor(max_workers=cpu_count) as e:
            futures["nwd"] = e.submit(build_hierarchy_from_pg, session, lod_max, bbox_nwd, lod)
            futures["nwu"] = e.submit(build_hierarchy_from_pg, session, lod_max, bbox_nwu, lod)
            futures["ned"] = e.submit(build_hierarchy_from_pg, session, lod_max, bbox_ned, lod)
            futures["neu"] = e.submit(build_hierarchy_from_pg, session, lod_max, bbox_neu, lod)
            futures["swd"] = e.submit(build_hierarchy_from_pg, session, lod_max, bbox_swd, lod)
            futures["swu"] = e.submit(build_hierarchy_from_pg, session, lod_max, bbox_swu, lod)
            futures["sed"] = e.submit(build_hierarchy_from_pg, session, lod_max, bbox_sed, lod)
            futures["seu"] = e.submit(build_hierarchy_from_pg, session, lod_max, bbox_seu, lod)

        for code, hier in futures.items():
            hierarchy[code] = hier.result()

    return hierarchy


def build_hierarchy_from_pg(session, lod_max, bbox, lod):
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
        h_nwd = build_hierarchy_from_pg(session, lod_max, bbox_nwd, lod)
        if h_nwd:
            hierarchy['nwd'] = h_nwd

        # nwu
        bbox_nwu = [x, y + length / 2, middle, x + width / 2, y + length, up]
        h_nwu = build_hierarchy_from_pg(session, lod_max, bbox_nwu, lod)
        if h_nwu:
            hierarchy['nwu'] = h_nwu

        # ned
        bbox_ned = [x + width / 2, y + length / 2, down, x + width, y + length, middle]
        h_ned = build_hierarchy_from_pg(session, lod_max, bbox_ned, lod)
        if h_ned:
            hierarchy['ned'] = h_ned

        # neu
        bbox_neu = [x + width / 2, y + length / 2, middle, x + width, y + length, up]
        h_neu = build_hierarchy_from_pg(session, lod_max, bbox_neu, lod)
        if h_neu:
            hierarchy['neu'] = h_neu

        # swd
        bbox_swd = [x, y, down, x + width / 2, y + length / 2, middle]
        h_swd = build_hierarchy_from_pg(session, lod_max, bbox_swd, lod)
        if h_swd:
            hierarchy['swd'] = h_swd

        # swu
        bbox_swu = [x, y, middle, x + width / 2, y + length / 2, up]
        h_swu = build_hierarchy_from_pg(session, lod_max, bbox_swu, lod)
        if h_swu:
            hierarchy['swu'] = h_swu

        # sed
        bbox_sed = [x + width / 2, y, down, x + width, y + length / 2, middle]
        h_sed = build_hierarchy_from_pg(session, lod_max, bbox_sed, lod)
        if h_sed:
            hierarchy['sed'] = h_sed

        # seu
        bbox_seu = [x + width / 2, y, middle, x + width, y + length / 2, up]
        h_seu = build_hierarchy_from_pg(session, lod_max, bbox_seu, lod)
        if h_seu:
            hierarchy['seu'] = h_seu

    return hierarchy


def decompress(points):
    """
    'points' is a pcpatch in wkb
    """

    # retrieve number of points in wkb pgpointcloud patch
    npoints = npoints_from_wkb_pcpatch(points)
    hexbuffer = hexdata_from_wkb_pcpatch(points)
    hexbuffer += hexa_signed_int32(npoints)

    # uncompress
    s = json.dumps(GreyhoundReadSchema().json()).replace("\\", "")
    dtype = buildNumpyDescription(json.loads(s))

    lazdata = bytes(hexbuffer)

    arr = numpy.fromstring(lazdata, dtype=numpy.uint8)
    d = Decompressor(arr, s)
    output = numpy.zeros(npoints * dtype.itemsize, dtype=numpy.uint8)
    decompressed = d.decompress(output)

    decompressed_str = numpy.ndarray.tostring(decompressed)

    # import struct
    # for i in range(0, npoints):
    #     point = decompressed_str[dtype.itemsize*i:dtype.itemsize*(i+1)]
    #     x = point[0:4]
    #     y = point[4:8]
    #     z = point[8:12]
    #     xd = struct.unpack("i", x)
    #     yd = struct.unpack("i", y)
    #     zd = struct.unpack("i", z)

    return [decompressed_str, dtype.itemsize]
