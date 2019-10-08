#!/usr/bin/env python3
# vim: ts=4 sw=4 expandtab:
from operator import itemgetter
import os
import psycopg2
import sys

HOST = 'localhost'
DBNAME = 'telemetry'
USER = 'postgres'
PASSPATH = os.path.join(os.environ['HOME'], '.pgpass')
PASSWORD = open(PASSPATH, "r").read().strip().split(':')[-1]
CRASH_AGE = "2 days"


def plural(count, noun, suffix='es'):
    if count == 1:
        return noun
    else:
        return ''.join((noun, suffix))


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

    for sig_and_count in cur.fetchall():
        sig = sig_and_count[0]
        count = sig_and_count[1]

        # get the first of the N matching stacks
        sigcur.execute('select stack from crash where stack_sig = %s limit 1', (sig,))
        stack = sigcur.fetchone()
        stack = eval(stack[0])

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
        clusters = set()
        for clid_and_version in clid_and_version_count.keys():
            clusters.add(clid_and_version[0])
        print('Crash signature %s\n%s total %s on %d %s' %
              (sig, count, plural(count, 'instance', 's'),
              len(clusters), plural(len(clusters), 'cluster', 's'))
             )
        for clid_and_version, count in sorted(
                clid_and_version_count.items(),
                key=itemgetter(1),
                reverse=True,
            ):
            print('%d %s, cluster %s, ceph ver %s' %
                  (count, plural(count, 'instance', 's'),
                  clid_and_version[0], clid_and_version[1])
                 )
        print('stack:\n\t', '\n\t'.join(stack))
        print()

    conn.close()


if __name__ == '__main__':
    sys.exit(main())
