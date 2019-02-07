#!/usr/bin/env python
"""
Query ApiHub (OpenSearch) for all S1 SLC scenes globally and create
acquisition datasets.
"""

import os, sys, time, re, requests, json, logging, traceback, argparse
import shutil, hashlib, getpass, tempfile, backoff
from subprocess import check_call
from datetime import datetime, timedelta
from tabulate import tabulate
from requests.packages.urllib3.exceptions import (InsecureRequestWarning,
                                                  InsecurePlatformWarning)
import ast
import shapely.wkt
from shapely.geometry import Polygon

import hysds.orchestrator
from hysds.celery import app
from hysds.dataset_ingest import ingest
from osaka.main import get

# from notify_by_email import send_email


# disable warnings for SSL verification
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
requests.packages.urllib3.disable_warnings(InsecurePlatformWarning)


# monkey patch and clean cache
# expire_after = timedelta(hours=1)
# requests_cache.install_cache('check_apihub', expire_after=expire_after)


# set logger
log_format = "[%(asctime)s: %(levelname)s/%(funcName)s] %(message)s"
logging.basicConfig(format=log_format, level=logging.INFO)

class LogFilter(logging.Filter):
    def filter(self, record):
        if not hasattr(record, 'id'): record.id = '--'
        return True

logger = logging.getLogger('scrape_apihub_opensearch')
logger.setLevel(logging.INFO)
logger.addFilter(LogFilter())


# global constants
url = "https://scihub.copernicus.eu/apihub/search?"
download_url = "https://scihub.copernicus.eu/apihub/odata/v1/Products('{}')/$value"
dtreg = re.compile(r'S1\w_.+?_(\d{4})(\d{2})(\d{2})T.*')
QUERY_TEMPLATE = 'IW AND producttype:SLC AND platformname:Sentinel-1 AND ' + \
                 'ingestiondate:[{0} TO {1}]'
                 #'ingestiondate:[{0} TO {1}] ' + \
                 #'( footprint:"Intersects(POLYGON(({2})))")'

# regexes
FOOTPRINT_RE = re.compile(r'POLYGON\s*\(\((.*)\)\)')
PLATFORM_RE = re.compile(r'S1(.+?)_')


def get_timestamp_for_filename(time):
    time = time.replace("-", "")
    time = time.replace(":", "")
    return time

def massage_result(res):
    """Massage result JSON into HySDS met json."""

    # set int fields
    for i in res['int']:
        if i['name'] == 'orbitnumber': res['orbitNumber'] = int(i['content'])
        elif i['name'] == 'relativeorbitnumber': res['trackNumber'] = int(i['content'])
        else: res[i['name']] = int(i['content'])
    del res['int']

    # set links
    for i in res['link']:
        if 'rel' in i:
            res[i['rel']] = i['href']
        else:
            res['download_url'] = i['href']
    del res['link']

    # set string fields
    for i in res['str']:
        if i['name'] == 'orbitdirection':
            if i['content'] == "DESCENDING": res['direction'] = "dsc"
            elif i['content'] == "ASCENDING": res['direction'] = "asc"
            else: raise RuntimeError("Failed to recognize orbit direction: %s" % i['content'])
        elif i['name'] == 'endposition': res['sensingStop'] = i['content']
        else: res[i['name']] = i['content']
    del res['str']

    # set date fields
    for i in res['date']:
        if i['name'] == 'beginposition': res['sensingStart'] = i['content'].replace('Z', '')
        elif i['name'] == 'endposition': res['sensingStop'] = i['content'].replace('Z', '')
        else: res[i['name']] = i['content']
    del res['date']

    # set data_product_name and archive filename
    res['data_product_name'] = "acquisition-%s" % res['title']
    res['archive_filename'] = "%s.zip" % res['title']

    try:
        start_time = datetime.strptime(res["sensingStart"], "%Y-%m-%dT%H:%M:%S.%fZ")
    except:
        start_time = datetime.strptime(res["sensingStart"], "%Y-%m-%dT%H:%M:%SZ")

    try:
        end_time = datetime.strptime(res["sensingStop"], "%Y-%m-%dT%H:%M:%S.%fZ")
    except:
        end_time = datetime.strptime(res["sensingStop"], "%Y-%m-%dT%H:%M:%SZ")

    if end_time < start_time:
        match_pattern = "(?P<spacecraft>S1\w)_IW_SLC__(?P<misc>.*?)_(?P<s_year>\d{4})(?P<s_month>\d{2})(?P<s_day>\d{2})T(?P<s_hour>\d{2})(?P<s_minute>\d{2})(?P<s_seconds>\d{2})_(?P<e_year>\d{4})(?P<e_month>\d{2})(?P<e_day>\d{2})T(?P<e_hour>\d{2})(?P<e_minute>\d{2})(?P<e_seconds>\d{2})(?P<misc2>.*?)$"
        m = re.match(match_pattern, res['title'])
        file_starttime = "{}-{}-{}T{}:{}:{}Z".format(m.group("s_year"), m.group("s_month"), m.group("s_day"),
                                                 m.group("s_hour"), m.group("s_minute"), m.group("s_seconds"))
        s_time = datetime.strptime(file_starttime, "%Y-%m-%dT%H:%M:%SZ")
        file_endtime = "{}-{}-{}T{}:{}:{}Z".format(m.group("e_year"), m.group("e_month"), m.group("e_day"),
                                               m.group("e_hour"), m.group("e_minute"), m.group("e_seconds"))
        e_time = datetime.strptime(file_endtime, "%Y-%m-%dT%H:%M:%SZ")
        if s_time < e_time:
            res["sensingStart"] = file_starttime
            res["sensingStop"] = file_endtime
        else:
            print "Inconsistency in filename too. Aborting correction"


    res['status'] = "ACQUIRED"
    res["source"] = "esa_scihub"
    track_number = res["trackNumber"]
    res["track_number"] = track_number
    del res['trackNumber']
    # extract footprint and save as bbox and geojson polygon
    match = FOOTPRINT_RE.search(res['footprint'])
    if not match:
        raise RuntimeError("Failed to extract footprint info: %s" % res['footprint'])
    polygon = [map(eval, coord.split()) for coord in match.group(1).split(',')]
    res['location'] = {
        "type": "polygon",
        "coordinates": [polygon],
    }
    res['bbox'] = [[i[1], i[0]] for i in polygon]

    # set platform
    match = PLATFORM_RE.search(res['title'])
    if not match:
        raise RuntimeError("Failed to extract iplatform: %s" % res['title'])
    res['platform'] = "Sentinel-1%s" % match.group(1)

    # verify track

    if res['platform'] == "Sentinel-1A":
       if res['track_number'] != (res['orbitNumber']-73)%175+1:
           logger.info("WARNING: Failed to verify S1A relative orbit number and track number. Orbit:{}, Track: {}".format(res.get('orbitNumber', ''), res.get('track_number', '')))
    if res['platform'] == "Sentinel-1B":
       if res['track_number'] != (res['orbitNumber']-27)%175+1:
           logger.info("WARNING: Failed to verify S1B relative orbit number and track number. Orbit:{}, Track: {}".format(res.get('orbitNumber', ''), res.get('track_number', '')))
    

def get_dataset_json(met, version):
    """Generated HySDS dataset JSON from met JSON."""

    return {
        "version": version,
        "label": met['id'],
        "location": met['location'],
        "starttime": met['sensingStart'],
        "endtime": met['sensingStop'],
    }


def create_acq_dataset(ds, met, root_ds_dir=".", browse=False):
    """Create acquisition dataset. Return tuple of (dataset ID, dataset dir)."""

    # create dataset dir
    id = "acquisition-{}_{}_{}_{}-esa_scihub".format(met["platform"],get_timestamp_for_filename(met["sensingStart"]), met["track_number"], met["sensoroperationalmode"])
    root_ds_dir = os.path.abspath(root_ds_dir)
    ds_dir = os.path.join(root_ds_dir, id)
    if not os.path.isdir(ds_dir): os.makedirs(ds_dir, 0755)

    # append source to met
    met['query_api'] = "opensearch"
    # set IPF version to None
    met['processing_version'] = None

    # dump dataset and met JSON
    ds_file = os.path.join(ds_dir, "%s.dataset.json" % id)
    met_file = os.path.join(ds_dir, "%s.met.json" % id)
    with open(ds_file, 'w') as f:
        json.dump(ds, f, indent=2, sort_keys=True)
    with open(met_file, 'w') as f:
        json.dump(met, f, indent=2, sort_keys=True)
   
    # create browse?
    if browse:
        browse_jpg = os.path.join(ds_dir, "browse.jpg")
        browse_png = os.path.join(ds_dir, "browse.png")
        browse_small_png = os.path.join(ds_dir, "browse_small.png")
        get(met['icon'], browse_jpg)
        check_call(["convert", browse_jpg, browse_png])
        os.unlink(browse_jpg)
        check_call(["convert", "-resize", "250x250", browse_png, browse_small_png])

    return id, ds_dir


def ingest_acq_dataset(ds, met, ds_cfg, browse=False):
    """Create acquisition dataset and ingest."""

    tmp_dir = tempfile.mkdtemp()
    id, ds_dir = create_acq_dataset(ds, met, tmp_dir, browse)

    try:
        ingest(id, ds_cfg, app.conf.GRQ_UPDATE_URL, app.conf.DATASET_PROCESSED_QUEUE, ds_dir, None)
        shutil.rmtree(tmp_dir)
        return True
    except Exception:
        return False


@backoff.on_exception(backoff.expo, requests.exceptions.RequestException,
                      max_tries=8, max_value=32)
def rhead(url):
    return requests.head(url)


def get_existing_acqs(start_time, end_time, location=False):
    """
    This function would query for all the acquisitions that
    temporally and spatially overlap with the AOI
    :param location:
    :param start_time:
    :param end_time:
    :return:
    """
    index = "grq_v2.0_acquisition-s1-iw_slc"
    type = "acquisition-S1-IW_SLC"

    query = {
        "query": {
            "filtered": {
                "query": {
                    "bool": {
                        "must": [
                            {
                                "range": {
                                    "metadata.sensingStart": {
                                        "to": end_time,
                                        "from": start_time
                                    }
                                }
                            }
                        ]
                    }
                }
            }
        }
    }

    if location:
        geo_shape = {
                    "geo_shape": {
                        "location": {
                            "shape": location
                        }
                    }
                }
        query["query"]["filtered"]["filter"] = geo_shape

    acq_ids = []
    rest_url = app.conf["GRQ_ES_URL"][:-1] if app.conf["GRQ_ES_URL"].endswith('/') else app.conf["GRQ_ES_URL"]
    url = "{}/{}/_search?search_type=scan&scroll=60&size=10000".format(rest_url, index)
    r = requests.post(url, data=json.dumps(query))
    r.raise_for_status()
    scan_result = r.json()
    count = scan_result['hits']['total']
    if count == 0:
        return []
    if '_scroll_id' not in scan_result:
        print("_scroll_id not found in scan_result. Returning empty array for the query :\n%s" % query)
        return []
    scroll_id = scan_result['_scroll_id']
    hits = []
    while True:
        r = requests.post('%s/_search/scroll?scroll=60m' % rest_url, data=scroll_id)
        res = r.json()
        scroll_id = res['_scroll_id']
        if len(res['hits']['hits']) == 0:
            break
        hits.extend(res['hits']['hits'])

    for item in hits:
        acq_ids.append(item.get("_id"))

    return acq_ids


def scrape(ds_es_url, ds_cfg, starttime, endtime, email_to, polygon=False, user=None, password=None,
           version="v2.0", ingest_missing=False, create_only=False, browse=False):
    """Query ApiHub (OpenSearch) for S1 SLC scenes and generate acquisition datasets."""

    # get session
    session = requests.session()
    if None not in (user, password): session.auth = (user, password)

    # set query
    query = QUERY_TEMPLATE.format(starttime, endtime)
    if polygon:
        query += ' ( footprint:"Intersects({})")'.format(convert_to_wkt(polygon))
        existing_acqs = get_existing_acqs(start_time=starttime, end_time=endtime, location=json.loads(polygon))
    else:
        existing_acqs = get_existing_acqs(start_time=starttime, end_time=endtime)
    
    # query
    prods_all = {}
    offset = 0
    loop = True
    total_results_expected = None
    ids_by_track = {}
    prods_missing = []
    prods_found = []
    while loop:
        query_params = {"q": query, "rows": 100, "format": "json", "start": offset }
        logger.info("query: %s" % json.dumps(query_params, indent=2))
        # query_url = url + "&".join(["%s=%s" % (i, query_params[i]) for i in query_params])
        # .replace("(", "%28").replace(")", "%29").replace(" ", "%20")
        # logger.info("query_url: %s" % query_url)
        response = session.get(url, params=query_params, verify=False)
        logger.info("query_url: %s" % response.url)
        if response.status_code != 200:
            logger.error("Error: %s\n%s" % (response.status_code,response.text))
        response.raise_for_status()
        results = response.json()
        if total_results_expected is None:
            total_results_expected = int(results['feed']['opensearch:totalResults'])
        entries = results['feed'].get('entry', None)
        if entries is None: break
        with open('res.json', 'w') as f:
            f.write(json.dumps(entries, indent=2))
        if isinstance(entries, dict): entries = [ entries ] # if one entry, scihub doesn't return a list
        count = len(entries)
        offset += count
        loop = True if count > 0 else False
        logger.info("Found: {0} results".format(count))
        for met in entries:
            try: massage_result(met) 
            except Exception, e:
                logger.error("Failed to massage result: %s" % json.dumps(met, indent=2, sort_keys=True))
                logger.error("Extracted entries: %s" % json.dumps(entries, indent=2, sort_keys=True))
                raise
            # logger.info(json.dumps(met, indent=2, sort_keys=True))
            ds = get_dataset_json(met, version)
            # logger.info(json.dumps(ds, indent=2, sort_keys=True))
            prods_all[met['id']] = {
                'met': met,
                'ds': ds,
            }

            #check if exists
            if met["id"] not in existing_acqs:
                prods_missing.append(met["id"])
            else:
                prods_found.append(met["id"])

            ids_by_track.setdefault(met['track_number'], []).append(met['id'])

        # don't clobber the connection
        time.sleep(3)

    # print number of products missing
    msg = "Global data availability for %s through %s:\n" % (starttime, endtime)
    table_stats = [["total on apihub", len(prods_all)],
                   ["missing products", len(prods_missing)],
                  ]
    msg += tabulate(table_stats, tablefmt="grid")

    # print counts by track
    msg += "\n\nApiHub (OpenSearch) product count by track:\n"
    msg += tabulate([(i, len(ids_by_track[i])) for i in ids_by_track], tablefmt="grid")

    # print missing products
    msg += "\n\nMissing products:\n"
    msg += tabulate([("missing", i) for i in sorted(prods_missing)], tablefmt="grid")
    msg += "\nMissing %d in %s out of %d in ApiHub (OpenSearch)\n\n" % (len(prods_missing),
                                                           ds_es_url,
                                                           len(prods_all))
    logger.info(msg)

    # error check options
    if ingest_missing and create_only:
        raise RuntimeError("Cannot specify ingest_missing=True and create_only=True.")

    # create and ingest missing datasets for ingest
    if ingest_missing and not create_only:
        for acq_id in prods_missing:
            info = prods_all[acq_id]
            if ingest_acq_dataset(info['ds'], info['met'], ds_cfg):
                logger.info("Created and ingested %s\n" % acq_id)
            else:
                logger.info("Failed to create and ingest %s\n" % acq_id)

    # just create missing datasets
    if not ingest_missing and create_only:
        for acq_id in prods_missing:
            info = prods_all[acq_id]

            id, ds_dir = create_acq_dataset(info['ds'], info['met'], browse=browse)
            logger.info("Created %s\n" % acq_id)

    # email
    # if email_to is not None:
    #     subject = "[check_apihub] %s S1 SLC count" % aoi['data_product_name']
    #     send_email(getpass.getuser(), email_to, [], subject, msg)


def convert_geojson(input_geojson):
    '''Attempts to convert the input geojson into a polygon object. Returns the object.'''
    if type(input_geojson) is str:
        try:
            input_geojson = json.loads(input_geojson)
        except:
            try:
                input_geojson = ast.literal_eval(input_geojson)
            except:
                raise Exception('unable to parse input geojson string: {0}'.format(input_geojson))
    #attempt to parse the coordinates to ensure a valid geojson
    #print('input_geojson: {}'.format(input_geojson))
    depth = lambda L: isinstance(L, list) and max(map(depth, L))+1
    d = depth(input_geojson)
    try:
        # if it's a full geojson
        if d is False and 'coordinates' in input_geojson.keys():
            polygon = Polygon(input_geojson['coordinates'][0])
            return polygon
        else: # it's a list of coordinates
            polygon = Polygon(input_geojson)
            return polygon
    except:
        raise Exception('unable to parse geojson: {0}'.format(input_geojson))

def convert_to_wkt(input_obj):
    '''converts a polygon object from shapely into a wkt string for querying'''
    return shapely.wkt.dumps(convert_geojson(input_obj))



if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ds_es_url", help="ElasticSearch URL for acquisition dataset, e.g. " +
                         "http://aria-products.jpl.nasa.gov:9200/grq_v2.0_acquisition-s1-iw_slc/acquisition-S1-IW_SLC")
    parser.add_argument("datasets_cfg", help="HySDS datasets.json file, e.g. " +
                         "/home/ops/verdi/etc/datasets.json")
    parser.add_argument("starttime", help="Start time in ISO8601 format", nargs='?',
                        default="%sZ" % (datetime.utcnow()-timedelta(days=1)).isoformat())
    parser.add_argument("endtime", help="End time in ISO8601 format", nargs='?',
                        default="%sZ" % datetime.utcnow().isoformat())
    parser.add_argument("--polygon", help="Geojson polygon constraint", default=False, required=False)
    parser.add_argument("--dataset_version", help="dataset version",
                        default="v2.0", required=False)
    parser.add_argument("--user", help="SciHub user", default=None, required=False)
    parser.add_argument("--password", help="SciHub password", default=None, required=False)
    parser.add_argument("--email", help="email addresses to send email to", 
                        nargs='+', required=False)
    parser.add_argument("--browse", help="create browse images", action='store_true')
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--ingest", help="create and ingest missing datasets",
                       action='store_true')
    group.add_argument("--create_only", help="only create missing datasets",
                       action='store_true')
    args = parser.parse_args()
    try:
        scrape(args.ds_es_url, args.datasets_cfg, args.starttime, args.endtime,
               args.email, args.polygon, args.user, args.password, args.dataset_version,
               args.ingest, args.create_only, args.browse)
    except Exception as e:
        with open('_alt_error.txt', 'a') as f:
            f.write("%s\n" % str(e))
        with open('_alt_traceback.txt', 'a') as f:
            f.write("%s\n" % traceback.format_exc())
        raise
