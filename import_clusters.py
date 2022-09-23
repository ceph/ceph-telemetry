#! /usr/bin/env python3
# vim: ts=4 sw=4 expandtab

import dbhelper
import psycopg2
import psycopg2.extras
import json
import re
import sys
from pathlib import Path
from os import listdir
from os.path import isfile, join
import time

# Data Source Name file
DSN = '/opt/telemetry/grafana.dsn'

# 'j' stands for report_json
def insert_into_all_tables(conn, report_id_serial, j):
    cur = conn.cursor()
    report_timestamp = j.get('report_timestamp')

    cluster = {}
    cluster['report_id']            = report_id_serial
    cluster['cluster_id']           = j.get('report_id')
    # If a field does not exist in the json then it will be inserted as "null" to the database
    cluster['ts']                   = report_timestamp
    cluster['created']              = j.get('created')
    if cluster['created'] == '0.000000':
        print("Weird created value for cluster. Skipping\n")
        return
    cluster['channel_basic']        = 'basic' in j.get('channels', [])
    cluster['channel_crash']        = 'crash' in j.get('channels', [])
    cluster['channel_device']       = 'device' in j.get('channels', [])
    cluster['channel_ident']        = 'ident' in j.get('channels', [])

    cluster['total_bytes']          = j.get('usage', {}).get('total_bytes')
    cluster['total_used_bytes']     = j.get('usage', {}).get('total_used_bytes')

    cluster['osd_count']            = j.get('osd', {}).get('count')
    cluster['mon_count']            = j.get('mon', {}).get('count')
    cluster['ipv4_addr_mons']       = j.get('mon', {}).get('ipv4_addr_mons')
    cluster['ipv6_addr_mons']       = j.get('mon', {}).get('ipv6_addr_mons')
    cluster['v1_addr_mons']         = j.get('mon', {}).get('v1_addr_mons')
    cluster['v2_addr_mons']         = j.get('mon', {}).get('v2_addr_mons')

    cluster['rbd_num_pools']        = j.get('rbd', {}).get('num_pools')

    cluster['fs_count']             = j.get('fs', {}).get('count')
    cluster['hosts_num']            = j.get('hosts', {}).get('num')
    cluster['pools_num']            = j.get('usage', {}).get('pools')
    # Compatibility with older telemetry modules
    cluster['pg_num']               = j.get('usage', {}).get('pg_num') or j.get('usage', {}).get('pg_num:')

    sql = 'INSERT INTO grafana.ts_cluster (%s) VALUES %s'
    dbhelper.run_insert(cur, sql, cluster)

    for p in j.get('pools', []):
        pool = {}
        pool['ts']                      = report_timestamp
        pool['report_id']               = report_id_serial
        pool['pool_idx']                = p.get('pool')
        pool['pgp_num']                 = p.get('pgp_num')
        pool['pg_num']                  = p.get('pg_num')
        pool['size']                    = p.get('size')
        pool['min_size']                = p.get('min_size')
        pool['cache_mode']              = p.get('cache_mode')
        pool['target_max_objects']      = p.get('target_max_objects')
        pool['target_max_bytes']        = p.get('target_max_bytes')
        pool['pg_autoscale_mode']       = p.get('pg_autoscale_mode')
        pool['type']                    = p.get('type')
        pool['ec_k']                    = p.get('erasure_code_profile', {}).get('k')
        pool['ec_m']                    = p.get('erasure_code_profile', {}).get('m')
        pool['ec_crush_failure_domain'] = p.get('erasure_code_profile', {}).get('crush_failure_domain')
        pool['ec_plugin']               = p.get('erasure_code_profile', {}).get('plugin')
        pool['ec_technique']            = p.get('erasure_code_profile', {}).get('technique')

        sql = 'INSERT INTO grafana.pool (%s) VALUES %s'
        dbhelper.run_insert(cur, sql, pool)

    for entity, entity_val in j.get('metadata', {}).items():
        for attr, attr_val in entity_val.items():
            for value, total in attr_val.items():
                metadata = {}
                metadata['ts']          = report_timestamp
                metadata['report_id']   = report_id_serial
                metadata['entity']      = entity
                metadata['attr']        = attr
                metadata['value']       = value
                metadata['total']       = total

                sql = 'INSERT INTO grafana.metadata (%s) VALUES %s'
                dbhelper.run_insert(cur, sql, metadata)
                # Adding a normalized 'ceph_version' record
                # to 'metadata' table by extracting the numeric version part
                if attr == 'ceph_version':
                    metadata['attr'] = 'ceph_version_norm'
                    ceph_version_norm = re.match('ceph version v*([0-9.]+|Dev).*', value)
                    if ceph_version_norm is None:
                        print(f"public.report.id = {report_id_serial} contains an invalid ceph_version value ({value}), skipping this metadata attribute")
                        continue
                    metadata['value'] = ceph_version_norm.group(1)
                    dbhelper.run_insert(cur, sql, metadata)

    for i in range(j.get('rbd', {}).get('num_pools', 0)):
        rbd_pool = {}
        rbd_pool['ts']          = report_timestamp
        rbd_pool['report_id']   = report_id_serial
        rbd_pool['pool_idx']    = i # This index is internal in the db
        rbd_pool['num_images']  = j['rbd']['num_images_by_pool'][i] # FIXME Will crash in case key is missing
        rbd_pool['mirroring']   = j['rbd']['mirroring_by_pool'][i]

        sql = 'INSERT INTO grafana.rbd_pool (%s) VALUES %s'
        dbhelper.run_insert(cur, sql, rbd_pool)

    # Commiting once, so everything is one transaction
    conn.commit()

def main():
    start_time = time.time()
    with open(DSN, 'r') as f:
        dsn_str = f.read().strip()

    conn = psycopg2.connect(dsn_str)
    # Create a named server-side cursor
    dict_cur = conn.cursor(name='server_side_cursor', withhold=True, cursor_factory=psycopg2.extras.DictCursor)
    dict_cur.itersize = 10
    # Fetch only reports which are not already in ts_cluster;
    # COALESCE returns the first non-NULL value, so '0' is
    # the returned id in case ts_cluster table is empty.
    #
    # Also, filter out test clusters so they will not appear
    # in the dashboard. Please note that 'organization' key
    # does not always exist in report (NULL); but when it does,
    # report['organization'] can have the value "null".
    #
    # We replace '\u0000' with ' ' since some reports might contain
    # it in an assert_msg of one of the reported crashes. This is interpreted by
    # Postgres as Unicode NULL and throws an error:
    #     psycopg2.DataError: unsupported Unicode escape sequence
    #     DETAIL:  \u0000 cannot be converted to text.
    dict_cur.execute("""SELECT id, report
                        FROM public.report
                        WHERE id > (SELECT COALESCE(MAX(ts_cluster.report_id), 0)
                                    FROM grafana.ts_cluster)
                        AND
                            (   --either the key does not exist in the report
                                NOT replace(report, '\\u0000', ' ')::jsonb ? 'organization'
                            OR
                                -- or it exists, but its value is not 'ceph-qa'
                                replace(report, '\\u0000', ' ')::jsonb#>'{organization}' != '"ceph-qa"'
                            )
                        ORDER BY id""")
    cnt = 0
    try:
        for r in dict_cur:
            cnt += 1
            insert_into_all_tables(conn, r['id'], json.loads(r['report']))
        dict_cur.close()
    except:
        print(f"Exception when processing public.report.id={r['id']}\n")
        conn.rollback()
        raise
    finally:
        refresh_cur = conn.cursor()
        refresh_cur.execute("REFRESH MATERIALIZED VIEW grafana.weekly_reports_sliding")
        conn.commit()
        refresh_cur.close()

    end_time = time.time()
    time_delta = int(end_time - start_time)
    print(f"Processed {cnt} reports in {time_delta} seconds\n")

if __name__ == '__main__':
    sys.exit(main())
