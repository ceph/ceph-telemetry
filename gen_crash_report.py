#!/usr/bin/env python3
# vim: ts=4 sw=4 expandtab:
import json
from operator import itemgetter
import os
import psycopg2
import re
import sys

HOST = 'localhost'
DBNAME = 'telemetry'
USER = 'postgres'
PASSPATH = os.path.join(os.environ['HOME'], '.pgpass')
PASSWORD = open(PASSPATH, "r").read().strip().split(':')[-1]
CRASH_AGE = "14 days"


def plural(count, noun, suffix='es'):
    if count == 1:
        return noun
    else:
        return ''.join((noun, suffix))


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


def main():
    conn = psycopg2.connect(host=HOST, dbname=DBNAME, user=USER, password=PASSWORD)
    cur = conn.cursor()
    sigcur = conn.cursor()
    crashcur = conn.cursor()

    # fetch all the recent stack sigs by frequency of occurence
    cur.execute("""
        select stack_sig, count(stack_sig) from crash
        where age(timestamp) < interval %s group by stack_sig
        order by count desc""", (CRASH_AGE,))

    unique_sig_count = cur.statusmessage.split()[1]
    print('%s unique %s collected in last %s:' %
          (unique_sig_count, plural(unique_sig_count, 'crash'), CRASH_AGE))
    print()

    crashes = dict()
    for sig_and_count in cur.fetchall():
        sig = sig_and_count[0]
        count = sig_and_count[1]

        crash = dict()
        crash['count'] = count

        # grab the sig note for possible later use
        sigcur.execute('select stack_sig, tracker_id, note from stack_sig_note where stack_sig = %s', (sig,))
        row = sigcur.fetchall()
        try:
            crash['tracker_id'] = row[0][1]
        except IndexError:
            crash['tracker_id'] = None
        try:
            crash['note'] = row[0][2]
        except IndexError:
            crash['note'] = None

        # get the first of the N matching stacks
        sigcur.execute('select stack, raw_report from crash where stack_sig = %s limit 1', (sig,))
        stack_and_report = sigcur.fetchone()
        crash['stack'] = eval(stack_and_report[0])
        crash['report'] = json.loads(stack_and_report[1])
        crash['assert_msg'] = crash['report'].get('assert_msg')

        # for each sig, fetch the crash instances that match it
        sigcur.execute('select crash_id from crash where age(timestamp) < interval %s and stack_sig = %s', (CRASH_AGE, sig))
        clid_and_version_count = dict()
        for crash_id in sigcur.fetchall():
            # for each crash instance, fetch all clusters that experienced it
            # accumulate versions and counts
            crash_id = crash_id[0]
            crashcur.execute('select cluster_id, version from crash where crash_id = %s', (crash_id,))
            clids_and_versions = crashcur.fetchall()
            for clid_and_version in clids_and_versions:
                if clid_and_version in clid_and_version_count:
                    clid_and_version_count[clid_and_version] += 1
                else:
                    clid_and_version_count[clid_and_version] = 1
        crash['clid_and_version_count'] = clid_and_version_count
        crash['clusters'] = set()
        for clid_and_version in clid_and_version_count.keys():
            crash['clusters'].add(clid_and_version[0])

        # accumulate current crash in crashes
        crashes[sig] = crash


    # if only single-cluster crashes, don't report
    if all(
        (len(kv[1]['clid_and_version_count']) == 1 for kv in crashes.items())
        ):
        print('All %d crashes on one cluster only' % len(crashes))
        conn.close()
        return 0

    # sort by len(crash['clid_and_version_count']), largest first
    for sig, crash in sorted(
        crashes.items(),
        key=lambda kv: len(kv[1]['clid_and_version_count']),
        reverse=True,):
        print(
                'Crash signature %s\n%s total %s on %d %s' %
                (sig, crash['count'], plural(crash['count'], 'instance', 's'),
                len(crash['clusters']),
                plural(len(crash['clusters']), 'cluster', 's'))
             )
        # sort by count in cluster/version, largest first
        for clid_and_version, count in sorted(
            crash['clid_and_version_count'].items(),
            key=itemgetter(1),
            reverse=True,
        ):
            print('%d %s, cluster %s, ceph ver %s' %
                  (count, plural(count, 'instance', 's'),
                  clid_and_version[0], clid_and_version[1])
                 )
        if crash['assert_msg']:
            print('assert_msg: ', sanitize_assert_msg(crash['assert_msg']))
        if crash['tracker_id']:
            print('tracker_id: ', crash['tracker_id'])
        if crash['note']:
            print('note: ', crash['note'])
        print('stack:\n\t', '\n\t'.join(sanitize_backtrace(crash['stack'])))

        print()

    conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
