#!/usr/bin/env python3
# vim: ts=4 sw=4 expandtab:
import argparse
from collections import defaultdict
from datetime import date, timedelta
import json
import os
import os.path
import psycopg2
import re
import sys

HOST = 'localhost'
DBNAME = 'telemetry'
USER = 'postgres'
PASSPATH = os.path.join(os.environ['HOME'], '.pgpass')
PASSWORD = open(PASSPATH, "r").read().strip().split(':')[-1]
DEFAULT_INTERVAL_WEEKS = 16
DEFAULT_INCREMENT_DAYS = 7


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '-i', '--interval',
        help='how many weeks old to look for cluster versions (default %(default)d)',
        default=DEFAULT_INTERVAL_WEEKS)
    parser.add_argument(
        '-s', '--step',
        help='number of days to step through interval (default %(default)d)',
        default=DEFAULT_INCREMENT_DAYS)
    parser.add_argument(
        '-d', '--debug',
        action='store_true',
        help='don''t update database, just print')
    return parser.parse_args()


def main():
    args = parse_args()
    interval = timedelta(days=int(args.interval) * 7)
    step = timedelta(days=int(args.step))
    conn = psycopg2.connect(
        host=HOST, dbname=DBNAME, user=USER, password=PASSWORD)

    cur = conn.cursor()
    ins_cur = conn.cursor()

    # clear entire table
    if not args.debug:
        cur.execute('delete from version_history_all_clusters')

    prev_date = None
    end_date = date.today()
    start_date = cur_date = end_date - interval
    while cur_date <= end_date:
        if prev_date is None:
            prev_date = cur_date
            cur_date += step
            continue

        cur.execute('''
            select distinct on (cluster_id)
            cluster_id, report_stamp, report from report
            where report_stamp > %s
            and report_stamp <= %s
            group by cluster_id, report_stamp
            order by cluster_id, report_stamp desc
            ''', (start_date, cur_date))
        versmap = defaultdict(int)
        for cluster_id, report_stamp, report in cur:
            report = json.loads(report)
            for (entity_type, info) in report.get('metadata', {}).items():
                for (version, num) in info.get('ceph_version', {}).items():
                    mo = re.match('ceph version v*([0-9.]+|Dev).*', version)
                    if mo:
                        version = mo.group(1)
                        versmap[version] += num

        for vers, num in versmap.items():
            if args.debug:
                print(cur_date, vers, num)
            else:
                ins_cur.execute(
                    'insert into version_history_all_clusters values(%s, %s, %s)',
                    (cur_date, vers, num,)
                )
        prev_date = cur_date
        cur_date += step

    if not args.debug:
        conn.commit()

    conn.close()


if __name__ == '__main__':
    sys.exit(main())
