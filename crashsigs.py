#!/usr/bin/env python3
# vim: ts=4 sw=4 expandtab:
import argparse
import hashlib
import json
import psycopg2
import re
import sys

conn = None

def sanitize_backtrace(bt):
    ret = list()
    for func_record in bt:
        # split into two fields on last space, take the first one,
        # strip off leading ( and trailing )
        func_plus_offset = func_record.rsplit(' ', 1)[0][1:-1]
        ret.append(func_plus_offset.split('+')[0])

    return ret

def sanitize_assert_msg(msg):

    # (?s) allows matching newline.  get everything up to "thread" and
    # then after-and-including the last colon-space.  This skips the
    # thread id, timestamp, and file:lineno, because file is already in
    # the beginning, and lineno may vary.

    matchexpr = re.compile(r'(?s)(.*) thread .* time .*(: .*)\n')
    return ''.join(matchexpr.match(msg).groups())



def calc_sig(bt, assert_msg):
    sig = hashlib.sha256()
    for func in sanitize_backtrace(bt):
        sig.update(func.encode())
    if assert_msg:
        sig.update(sanitize_assert_msg(assert_msg).encode())
    return ''.join('%02x' % c for c in sig.digest())


def update_crash(cluster_id, report):
    cur = conn.cursor()
    crashes = report.get('crashes', [])
    if isinstance(crashes, dict):
        tmp = []
        for c in crashes.values():
            tmp.append(c)
        crashes = tmp

    print('Cluster %s has %d crashes' % (cluster_id, len(crashes)))
    if len(crashes) == 0:
        return False
    for crash in crashes:
        crash_id = crash.get('crash_id')
        if not crash_id:
            continue
        stack = crash.get('backtrace')
        assert_msg = crash.get('assert_msg')
        sig = calc_sig(stack, assert_msg)
        cur.execute(
            "INSERT INTO crash (crash_id, cluster_id, raw_report, timestamp, entity_name, version, stack_sig, stack) values (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (crash_id) DO UPDATE SET stack_sig=%s",
            (crash_id,
             cluster_id,
             json.dumps(crash, indent=4),
             crash.get('timestamp'),
             crash.get('entity_name'),
             crash.get('ceph_version'),
             sig,
             str(stack),
             sig,
            ))


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("-a", "--all", action='store_true', help="do all crash history (default is just latest report)")
    parser.add_argument("-c", "--clusters", help="list of cluster IDs to update", nargs='*')
    return parser.parse_args()

    
def load_and_call_update(cid, ts, report):
    try:
        report = json.loads(report)
    except TypeError:
        print('cluster %s ts %s has malformed report' % (cid, ts))
        return
    update_crash(cid, report)


def main():

    f = open('/opt/telemetry/pg_pass.txt', 'r')
    password = f.read().strip()
    f.close()

    global conn
    conn = psycopg2.connect(
        host='localhost',
        database='telemetry',
        user='telemetry',
        password=password
    )

    ccur = conn.cursor()
    rcur = conn.cursor()


    args = parse_args()
    
    cluster_count = update_count = 0
    ccur.execute("SELECT DISTINCT cluster_id from report")

    if args.clusters is None:
        clusters = ccur.fetchall()
    else:
        clusters = args.clusters
    for cid in clusters:
        if isinstance(cid, tuple):
            cid = cid[0]
        if args.all: 
            rcur.execute("SELECT report_stamp, report FROM report WHERE cluster_id=%s", (cid,))
            for ts, report in rcur.fetchall():
                load_and_call_update(cid, ts, report)
        else:
            rcur.execute("SELECT report_stamp, report FROM report WHERE cluster_id=%s ORDER BY report_stamp DESC LIMIT 1", (cid,))
            ts, report = rcur.fetchone()
            load_and_call_update(cid, ts, report)
        cluster_count += 1

    print('Processed %d cluster ids' % cluster_count)
    conn.commit()


if __name__ == '__main__':
    sys.exit(main())
