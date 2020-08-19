#!/usr/bin/env python3
# vim: ts=4 sw=4 expandtab

import psycopg2
import json
import psycopg2.extras
import sys
import subprocess
from enum import Enum

REPORTS_LIMIT = 12

# TODO add error checks

# Data Source Name file
DSN = '/opt/telemetry/grafana.dsn'


def get_prediction_input(conn, device_id):
    prediction_input = {}

    # Get device's capacity
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("""SELECT device.spec.capacity
                   FROM device.device
                   INNER join device.spec on device.device.spec_id = device.spec.id
                   WHERE device.device.id = %s
                """, (device_id,))

    capacity = cur.fetchone();
    prediction_input['capacity_bytes'] = capacity[0] if capacity else None

    # Get device's SMART attributes
    device_smart = {}
    rep_cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    rep_cur.execute("""SELECT report_id
                       FROM device.ts_device
                       WHERE device_id = %s
                       ORDER BY ts DESC
                       LIMIT %s
                    """, (device_id, REPORTS_LIMIT))

    for r in rep_cur:
        report_id = r['report_id']
        smart_cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        smart_cur.execute("""SELECT *
                             FROM device.smart_sata
                             WHERE report_id = %s
                          """, (report_id,))

        for s in smart_cur:
            date = s['ts'].__str__()
            if date not in device_smart:
                device_smart[date] = {}
                device_smart[date]['attr'] = {}

            device_smart[date]['attr'][s['attr_id']] = {'name' : s['attr_name'], 'val_raw' : s['attr_raw'], 'val_norm': s['attr_norm']}
        smart_cur.close()

    prediction_input['smart_data'] = device_smart
    cur.close()
    rep_cur.close()

    return prediction_input

def insert_result(conn, device_id, result):
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    sql = """INSERT INTO device.prediction_result
             (device_id, result)
             VALUES
             (%s, %s)
             ON CONFLICT (device_id) DO NOTHING;"""

    cur.execute(sql, (device_id, result))

    cur.close()
    conn.commit()

def main():
    with open(DSN, 'r') as f:
        dsn_str = f.read().strip()
    conn = psycopg2.connect(dsn_str)
    if sys.argv[1:]:
        device_id = sys.argv[1]
    else:
        print(f"Usage: {sys.argv[0]} <device_id>", file=sys.stderr)
        sys.exit()
    prediction_input = get_prediction_input(conn, device_id)
    prediction_input_json = json.dumps(prediction_input, indent=4, sort_keys=True)
    #print(prediction_input_json)
    run_res = subprocess.run("./model.py", stdout=subprocess.PIPE, input=prediction_input_json, encoding='ascii')
    prediction_result = run_res.stdout
    print(f"Prediction result for {device_id} is: {prediction_result}")
    if prediction_result is not None:
        insert_result(conn, device_id, prediction_result)

    conn.close()


if __name__ == '__main__':
    sys.exit(main())
