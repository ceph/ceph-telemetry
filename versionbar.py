#!/usr/bin/env python3
# vim: ts=4 sw=4 expandtab:
import hashlib
import json
import matplotlib as mpl
import matplotlib.pyplot as plt
import os
import os.path
import psycopg2
import sys

HOST = 'localhost'
DBNAME = 'telemetry'
USER = 'postgres'
PASSPATH = os.path.join(os.environ['HOME'], '.pgpass')
PASSWORD = open(PASSPATH, "r").read().strip().split(':')[-1]


def main():
    conn = psycopg2.connect(host=HOST, dbname=DBNAME, user=USER, password=PASSWORD)

    cur = conn.cursor()
    
    cur.execute('''
    with cleanver as
      (select regexp_replace(version, 'ceph version (([0-9.]+|Dev)).*', '\\1')
       as ver from cluster_version)
    select ver, count(ver) from cleanver
    group by ver
    order by ver
    ''')

    versions = list()
    counts = list()
    for row in cur.fetchall():
        versions.append(row[0])
        counts.append(row[1])
    conn.close()
    fig, ax = plt.subplots(
        subplot_kw=dict(xlabel='Ceph version', ylabel='Number of daemons')
        )
    ax.bar(
        range(0, len(versions)),
        counts,
        tick_label=versions,
        )
    ax.tick_params(axis='x', labelrotation=90)
    fig.subplots_adjust(bottom=0.2)
    plt.savefig('versions.png')



if __name__ == '__main__':
    sys.exit(main())
