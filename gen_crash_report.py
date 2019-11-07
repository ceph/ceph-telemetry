#!/usr/bin/env python3
# vim: ts=4 sw=4 expandtab:
from collections import defaultdict
import json
from operator import itemgetter
import os
import os.path
import psycopg2
import re
import requests
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


ASSERT_MATCHEXPR = re.compile(r'(?s)(.*) thread .* time .*(: .*)\n')


def sanitize_assert_msg(msg):

    # (?s) allows matching newline.  get everything up to "thread" and
    # then after-and-including the last colon-space.  This skips the
    # thread id, timestamp, and file:lineno, because file is already in
    # the beginning, and lineno may vary.

    return ''.join(ASSERT_MATCHEXPR.match(msg).groups())


def get_tracker_sig_fieldid():
    '''
    Connect to tracker to get the numeric id of the custom field
    holding crash signatures; we'll use it against the signature to
    find any related tracker issues
    '''
    keypath = os.path.join(os.environ['HOME'], '.tracker.api.key')
    with open(keypath, 'r') as keyfile:
        key = keyfile.read().strip()
    response = requests.get('https://tracker.ceph.com/custom_fields.json',
                            params={'key': key},)
    for fielddict in response.json()['custom_fields']:
        if fielddict['name'] == 'Crash signature':
            return fielddict['id']
    return -1


def get_all_trackers_with_crashsigs():
    '''
    Snarf all the tracker info with non-null crashids; way more
    efficient than querying each crashsig, at least when the crashsig
    usage is sparse.  Maybe someday we'll need to change back to
    querying for each signature if this gets too large.

    Return what we want to print: a dict keyed by crashsig, with
    tuples (id, status) holding the tracker ID number and Status fields.
    '''

    trackers_by_sig = dict()
    tracker_sig_fieldid = get_tracker_sig_fieldid()
    response = requests.get('https://tracker.ceph.com/issues.json',
                            params={'cf_%d' % tracker_sig_fieldid: '*'})
    sig_issues = response.json()
    for issue in sig_issues['issues']:
        crashid = -1
        for cf in issue['custom_fields']:
            if cf['id'] == tracker_sig_fieldid:
                crashid = cf['value']
                break
        if crashid == -1:
            continue
        if crashid not in trackers_by_sig:
            trackers_by_sig[crashid] = list()
        trackers_by_sig[crashid].append((issue['id'], issue['status']['name']))
    return trackers_by_sig


def main():

    trackers_by_sig = get_all_trackers_with_crashsigs()

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

        # get any matching tracker issues, add to crash dict
        if sig in trackers_by_sig:
            crash['trackers'] = list()
            for id_and_status in trackers_by_sig[sig]:
                crash['trackers'].append(id_and_status)

        # get the first of the N matching stacks
        sigcur.execute('select stack, raw_report from crash where stack_sig = %s limit 1', (sig,))
        stack_and_report = sigcur.fetchone()
        crash['stack'] = eval(stack_and_report[0])
        crash['report'] = json.loads(stack_and_report[1])
        crash['assert_msg'] = crash['report'].get('assert_msg')

        # for each sig, fetch the crash instances that match it
        sigcur.execute('select crash_id from crash where age(timestamp) < interval %s and stack_sig = %s', (CRASH_AGE, sig))
        clid_vers_count = defaultdict(int)
        for crash_id in sigcur.fetchall():
            # for each crash instance, fetch all clusters that experienced it
            # accumulate versions and counts
            crash_id = crash_id[0]
            crashcur.execute('select cluster_id, version from crash where crash_id = %s', (crash_id,))
            clids_and_versions = crashcur.fetchall()
            for clid_vers in clids_and_versions:
                clid_vers_count[clid_vers] += 1
        crash['clid_vers_count'] = clid_vers_count
        crash['clusters'] = set()
        for clid_vers in clid_vers_count.keys():
            crash['clusters'].add(clid_vers[0])

        # accumulate current crash in crashes
        crashes[sig] = crash

    # if only single-cluster crashes, don't report
    if all((len(kv[1]['clid_vers_count']) == 1
            for kv in crashes.items())):
        print('All %d crashes on one cluster only' % len(crashes))
        conn.close()
        return 0

    # sort by len(crash['clid_vers_count']), largest first
    for sig, crash in sorted(
            crashes.items(),
            key=lambda kv: len(kv[1]['clid_vers_count']),
            reverse=True,):
        print('Crash signature %s\n%s total %s on %d %s' %
              (sig, crash['count'], plural(crash['count'], 'instance', 's'),
               len(crash['clusters']),
               plural(len(crash['clusters']), 'cluster', 's')))
        # sort by count in cluster/version, largest first
        for clid_vers, count in sorted(
            crash['clid_vers_count'].items(),
            key=itemgetter(1),
            reverse=True,
        ):
            print('%d %s, cluster %s, ceph ver %s' %
                  (count, plural(count, 'instance', 's'),
                   clid_vers[0], clid_vers[1]))
        if crash['assert_msg']:
            print('assert_msg: ', sanitize_assert_msg(crash['assert_msg']))
        if 'trackers' in crash:
            for tracker in crash['trackers']:
                print('https://tracker.ceph.com/issues/%d' % tracker[0],
                      'status:', tracker[1])
        print('stack:\n\t', '\n\t'.join(sanitize_backtrace(crash['stack'])))

        print()

    conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
