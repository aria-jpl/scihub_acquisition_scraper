import argparse
import json
import os
from datetime import datetime, timedelta
from hysds_commons.job_utils import submit_mozart_job

BASE_PATH = os.path.dirname(__file__)


def submit_global_ipf(spatial_extent, start_time, end_time, scihub_days_bef, release):
    params = [
        {
            "name": "AOI_name",
            "from": "value",
            "value": "Global"
        },
        {
            "name": "spatial_extent",
            "from": "value",
            "value": spatial_extent
        },
        {
            "name": "start_time",
            "from": "value",
            "value": start_time
        },
        {
            "name": "end_time",
            "from": "value",
            "value": end_time
        },
        {
            "name": "ipf_scraper_release",
            "from": "value",
            "value": release
        },
        {
            "name": "scihub_days_bef_thresh",
            "from": "value",
            "value": scihub_days_bef
        }
    ]

    rule = {
        "rule_name": "{}_ipf_scraper".format("global"),
        "queue": "factotum-job_worker-apihub_scraper_throttled",
        "priority": '5',
        "kwargs": '{}'
    }

    print('submitting jobs with params:')
    print(json.dumps(params, sort_keys=True, indent=4, separators=(',', ': ')))
    mozart_job_id = submit_mozart_job({}, rule, hysdsio={"id": "internal-temporary-wiring", "params": params,
                                                         "job-specification": "job-AOI_based_ipf_submitter:{}".format(release)},
                                      job_name='job-%s-%s-%s' % ("ipf_submitter", "global", release),
                                      enable_dedup=False)
    print("For {} , IPF Submitter Job ID: {}".format("Global", mozart_job_id))


if __name__ == "__main__":
    """
    This script will find all active AOIs. It will then submit AOI IPF scraper
    jobs for each AOI.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="PGE docker image tag (release, version, " +
                                      "or branch) to propagate",
                        default="master", required=False)
    parser.add_argument("--scihub_days_bef_thresh", help="length of time (in days) before now to query with "
                                                     "scihub instead of asf endpoint",
                        default="master", required=False)
    args = parser.parse_args()
    scihub_days_bef = args.scihub_days_bef_thresh
    tag = args.tag
    global_extent = {
              "coordinates": [
                  [[-180, -90], [-180, 90], [180, 90], [180, -90], [-180, -90]]
              ],
              "type": "polygon"
            }

    start_time = "{}Z".format((datetime.utcnow()-timedelta(days=2)).isoformat())
    end_time = "{}Z".format(datetime.utcnow().isoformat())
    submit_global_ipf(global_extent, start_time, end_time, scihub_days_bef, tag)
