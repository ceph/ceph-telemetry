#!/usr/bin/env python3
# vim: ts=4 sw=4 expandtab:
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


def update_cluster(cluster_id, latest_report_ts, report):
    cur = conn.cursor()
    cur.execute("SELECT cluster_id,latest_report_stamp FROM cluster WHERE cluster_id=%s", (cluster_id,))
    row = cur.fetchone()
    if row:
        latest_cluster_report_stamp = row[1]

        if latest_cluster_report_stamp >= latest_report_ts:
            return False

    num_pgs = 0
    for pool in report.get('pools', []):
        num_pgs += pool.get('pg_num', 0)
    cur.execute(
        "INSERT INTO cluster (cluster_id, latest_report_stamp, num_mon, num_osd, num_pools, num_pgs, total_bytes, total_used_bytes) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (cluster_id) DO UPDATE SET latest_report_stamp=%s, num_mon=%s, num_osd=%s, num_pools=%s, num_pgs=%s, total_bytes=%s, total_used_bytes=%s",
        (cluster_id,
         latest_report_ts,
         report.get('mon', {}).get('count', 0),
         report.get('osd', {}).get('count', 0),
         report.get('usage', {}).get('pools', 0),
         num_pgs,
         report.get('usage', {}).get('total_bytes', 0),
         report.get('usage', {}).get('total_used_bytes', 0),

         latest_report_ts,
         report.get('mon', {}).get('count', 0),
         report.get('osd', {}).get('count', 0),
         report.get('usage', {}).get('pools', 0),
         num_pgs,
         report.get('usage', {}).get('total_bytes', 0),
         report.get('usage', {}).get('total_used_bytes', 0)),)
    return True


def update_cluster_version(cluster_id, latest_report_ts, report):
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM cluster_version WHERE cluster_id=%s",
        (cluster_id,)
    )
    for (entity_type, info) in report.get('metadata', {}).items():
        for (version, num) in info.get('ceph_version', {}).items():
            cur.execute(
                "INSERT INTO cluster_version (cluster_id, entity_type, version, num_daemons) VALUES (%s, %s, %s, %s)",
                (cluster_id,
                 entity_type,
                 version,
                 num,))


def update_crash(cluster_id, latest_report_ts, report):
    cur = conn.cursor()
    crashes = report.get('crashes', [])
    if isinstance(crashes, dict):
        tmp = []
        for c in crashes.values():
            tmp.append(c)
        crashes = tmp

    update_count = 0
    for crash in crashes:
        crash_id = crash.get('crash_id')
        if not crash_id:
            continue
        stack = crash.get('backtrace')
        assert_msg = crash.get('assert_msg')
        sig = calc_sig(stack, assert_msg)
        cur.execute(
            "INSERT INTO crash (crash_id, cluster_id, raw_report, timestamp, entity_name, version, stack_sig, stack) values (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (crash_id,
             cluster_id,
             json.dumps(crash, indent=4),
             crash.get('timestamp'),
             crash.get('entity_name'),
             crash.get('ceph_version'),
             sig,
             str(stack),
            ))
        update_count += int(cur.statusmessage.split()[2])
    return len(crashes), update_count


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

    cluster_count = update_count = crash_count = crash_update_count = 0
    ccur.execute("SELECT DISTINCT cluster_id from report")
    for cid in ccur.fetchall():
        cid = cid[0]
        rcur.execute("SELECT report_stamp, report FROM report WHERE cluster_id=%s ORDER BY report_stamp DESC LIMIT 1", (cid,))
        latest_report_ts, report = rcur.fetchone()
        try:
            report = json.loads(report)
        except TypeError:
            print('cluster %s ts %s has malformed report' % (cid, latest_report_ts))
            continue
        cluster_count += 1
        if update_cluster(cid, latest_report_ts, report):
            update_count += 1
        update_cluster_version(cid, latest_report_ts, report)
        visited, updated = update_crash(cid, latest_report_ts, report)
        crash_count += visited
        crash_update_count += updated
    print('updated %d/%d clusters, updated %d/%d crashes' % (update_count, cluster_count, crash_update_count, crash_count))
    conn.commit()


if __name__ == '__main__':
    sys.exit(main())
