#!/usr/bin/env python3
# vim: ts=4 sw=4 expandtab:
from collections import defaultdict
import json
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
    Snarf all the tracker info with non-null crashsigs; way more
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
        sigs = list()
        for cf in issue['custom_fields']:
            if cf['id'] == tracker_sig_fieldid:
                # note: value is a \r\n-separated list
                sigs = cf['value'].split()
                break
        if not sigs:
            continue
        for sig in sigs:
            if sig not in trackers_by_sig:
                trackers_by_sig[sig] = list()
            trackers_by_sig[sig].append(
                (issue['id'], issue['status']['name']))
    return trackers_by_sig


def accumulate_crashes():
    '''
    connect to postgres, get all crashes and store them away into a master
    dict that maps crashsig to entries with keys:

    'count':        number of occurrences of crashsig
    'trackers':     list of (issue_id, status_string) for all tracker issues
                    that refer to this signature, if any
    'stack':        unprocessed backtrace from crash
    'assert_msg':   assert msg from crash, if any
    'cluster_to_count':
                    dict mapping (cluster_id, ceph_version) to
                    (count, set(entity_names)); count is total count
                    of occurrences per this cluster/version pair,
                    entity_names are all the unique entity names that
                    experienced the crash
    'clusters':     set of clusters that experienced the crash

    Returns:        (count of unique crashsigs, the above dict)

    '''

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
        report = json.loads(stack_and_report[1])
        crash['assert_msg'] = report.get('assert_msg')

        # for each sig, fetch the crash instances that match it
        sigcur.execute('select crash_id from crash where age(timestamp) < interval %s and stack_sig = %s', (CRASH_AGE, sig))

        cluster_to_count = defaultdict(lambda: ([0, set()]))
        # map key (cluster_id, version) to value [count, set(entities)]
        for crash_id in sigcur.fetchall():
            # for each crash instance, fetch all clusters that experienced it
            # accumulate versions and entities that got the crash
            crash_id = crash_id[0]
            crashcur.execute('select cluster_id, version, entity_name from crash where crash_id = %s', (crash_id,))
            rows = crashcur.fetchall()
            for row in rows:
                # row is (clid, vers, entity_name); use first two for key
                key = tuple(row[0:2])
                cluster_to_count[key][0] += 1
                cluster_to_count[key][1].add(row[2].strip())
        crash['cluster_to_count'] = cluster_to_count
        crash['clusters'] = set()
        for clid_vers in cluster_to_count.keys():
            crash['clusters'].add(clid_vers[0])

        # accumulate current crash in crashes
        crashes[sig] = crash

    conn.close()
    return unique_sig_count, crashes


def main():

    unique_sig_count, crashes = accumulate_crashes()

    print('%s unique %s collected in last %s:' %
          (unique_sig_count, plural(unique_sig_count, 'crash'), CRASH_AGE))
    print()

    # if only single-cluster crashes, don't report
    if all((len(kv[1]['cluster_to_count']) == 1
            for kv in crashes.items())):
        print('All %d crashes on one cluster only' % len(crashes))
        return 0

    # each keyfunc is called with ((clid,vers), (count,entitylist))

    def entitylistlen_keyfunc(item):
        return len(item[1][1])

    def count_keyfunc(item):
        return item[1][0]

    # sort by len(crash['cluster_to_count']), largest first
    for sig, crash in sorted(
            crashes.items(),
            key=lambda kv: len(kv[1]['cluster_to_count']),
            reverse=True,):
        print('Crash signature %s\n%s total %s on %d %s' %
              (sig, crash['count'], plural(crash['count'], 'instance', 's'),
               len(crash['clusters']),
               plural(len(crash['clusters']), 'cluster', 's')))
        # sort first by sig instance count, then by len(entities),
        # in descending order.  Actually call sort by minor field
        # and then major field
        sorted_by_num_entities = \
            sorted(crash['cluster_to_count'].items(),
                   key=entitylistlen_keyfunc,
                   reverse=True,)
        for clid_vers, (count, entities) in sorted(
            sorted_by_num_entities,
            key=count_keyfunc,
            reverse=True
        ):
            print('%d %s, cluster %s, ceph ver %s\nentities:\n%s' % (
                count,
                plural(count, 'instance', 's'),
                clid_vers[0],
                clid_vers[1],
                '\n'.join(entities))
            )
        if crash['assert_msg']:
            print('assert_msg: ', sanitize_assert_msg(crash['assert_msg']))
        if 'trackers' in crash:
            for tracker in crash['trackers']:
                print('https://tracker.ceph.com/issues/%d' % tracker[0],
                      'status:', tracker[1])
        print('stack:\n\t', '\n\t'.join(sanitize_backtrace(crash['stack'])))

        print()

    return 0


if __name__ == '__main__':
    sys.exit(main())
