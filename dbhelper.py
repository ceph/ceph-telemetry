#! /usr/bin/env python3
# vim: ts=4 sw=4 expandtab

import psycopg2
from psycopg2.extensions import AsIs
import os
import json
from os.path import isfile, join


def run_insert(cur, sql, d, extra_vals = None):
    columns = d.keys()
    values = [d[column] for column in columns]

    if extra_vals is None:
        extra_vals = ()
    cur.execute(sql, (AsIs(','.join(columns)), tuple(values)) + extra_vals)
    #print(cur.mogrify(sql, (AsIs(','.join(columns)), tuple(values))))
