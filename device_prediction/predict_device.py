#!/usr/bin/env python3
# vim: ts=4 sw=4 expandtab

import psycopg2
import json
import psycopg2.extras
import sys
import subprocess
from enum import Enum
from datetime import timedelta, date
import time
import datetime

# Config for the current 'redhat' AI model:
REPORTS_MIN = 6
REPORTS_MAX = 12
SUPPORTED_VENDORS = ('hgst', 'seagate')
MODEL_RH = 1

# Data Source Name file
DSN = '/opt/telemetry/grafana.dsn'

def get_devices_to_predict(conn, ts_from, ts_to):
    """
    Get all distinct device_id (and their latest SMART ts) of devices which
    reported during this window. The time window is usually 24 hours.

    Returns:
    A list of dictionaries, which their keys are 'device_id' and 'ts'.
    If the query returns no results, we return [None].
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    # In device.ts_device table, ts column is the timestamp of smartctl
    # run on the client side;
    # Device reports might be received out of order, thus, sort by
    # their timestamp, not by report_id.
    cur.execute("""SELECT distinct on(device_id) device_id, ts
                   FROM device.ts_device
                   WHERE device.ts_device.ts BETWEEN %s AND %s
                   ORDER BY device_id, ts
                """, (ts_from, ts_to))

    # Copy the results from the cursor
    result = cur.fetchall()
    cur.close()

    return result

def get_prediction_input(conn, device_id, ts):
    """
    For a given 'device_id', generate prediction input before a given 'ts'.
    Input is gathered from REPORTS_MIN to REPORTS_MAX reports with a timestamp
    lower than 'ts', which have valid SMART attributes.

    Prediction input might be None if:
    - The device's vendor is not in SUPPORTED_VENDORS;
    - All reports before the provided timestamp have errors (invalid telemetry);
    - Less than REPORTS_MIN valid reports are found;
    - The device does not have SMART attributes;

    Returns:
    A (prediction_input, vendor) tuple, where:
    - 'prediction_input' is a dictionary with the keys:
      'vendor', 'model', 'capacity_bytes', 'smart_data'
      (see /device_prediction/input_samples/* for examples).
      'prediction_input' is 'None' in case no input could be gathered.
    - 'vendor' is the device's vendor, or 'Not supported'
      in case the device's vendor is not in SUPPORTED_VENDORS.
    """
    prediction_input = {}

    # Get device's vendor, model, capacity:
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("""SELECT device.spec.vendor,
                          device.spec.model,
                          device.spec.capacity
                   FROM device.device
                   INNER JOIN device.spec ON device.device.spec_id = device.spec.id
                   WHERE device.device.id = %s
                """, (device_id,))

    # res should always contain at least vendor or model
    res = cur.fetchone()
    # The model supports prediction only for SUPPORTED_VENDORS:
    if res['vendor'].lower() not in SUPPORTED_VENDORS:
        return None, 'Not supported'

    prediction_input['vendor'] = res['vendor'] if res else None
    prediction_input['model'] = res['model'] if res else None
    prediction_input['capacity_bytes'] = res['capacity'] if res else None

    rep_cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    # Find the relevant report ids.
    # We take up to REPORTS_MAX reports before 'ts' date
    # which are valid (error != NULL).
    rep_cur.execute("""SELECT report_id
                       FROM device.ts_device
                       WHERE device_id = %s
                       AND ts <= %s
                       AND error IS NULL
                       ORDER BY ts DESC
                       LIMIT %s
                    """, (device_id, ts, REPORTS_MAX))

    # print(f"found {rep_cur.rowcount} reports for {device_id}")

    if rep_cur.rowcount == 0:
        # print('Found 0 reports before {ts} for device_id {device_id}, cannot generate input for prediction model')
        return None, prediction_input['vendor']

    # For each report we found, get its SMART attributes, if they exist.
    device_smart = {}
    smart_cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    for r in rep_cur:
        report_id = r['report_id']
        smart_cur.execute("""SELECT *
                             FROM device.smart_sata
                             WHERE report_id = %s
                          """, (report_id,))

        if smart_cur.rowcount == 0:
            # print(f"there is no SMART input for device_id: {device_id}, report_id : {r['report_id']}")
            continue

        for s in smart_cur:
            date = str(s['ts'])
            if date not in device_smart:
                device_smart[date] = {}
                device_smart[date]['attr'] = {}

            device_smart[date]['attr'][s['attr_id']] = {'name': s['attr_name'], 'val_raw': s['attr_raw'], 'val_norm': s['attr_norm']}
    smart_cur.close()

    if len(device_smart) < REPORTS_MIN:
        # print(f"Not enough SMART data before {ts} for device_id {device_id}")
        return None, prediction_input['vendor']

    prediction_input['smart_data'] = device_smart

    cur.close()
    rep_cur.close()

    return prediction_input, prediction_input['vendor']

def insert_result(conn, device_id, ts, algo_id, result):
    """
    Insert into device.prediction_result table the model's (algo_id) prediction result for device_id
    in a certain point in time (ts).

    Returns: void.
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    sql = """INSERT INTO device.prediction_result
             (device_id, ts, algo_id, result)
             VALUES
             (%s, %s, %s, %s)
             ON CONFLICT (device_id, ts, algo_id) DO NOTHING"""

    cur.execute(sql, (device_id, ts, algo_id, result))

    cur.close()
    conn.commit()


def main():
    run_start_time = time.time()

    with open(DSN, 'r') as f:
        dsn_str = f.read().strip()
    conn = psycopg2.connect(dsn_str)

    # Set START_DATE
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("""SELECT MAX(ts) ts
                   FROM device.prediction_result""")

    res = cur.fetchone()
    # res might be [None]
    if res is not None and res[0] is not None:
        # res[0] is the timestamp inserted in previous runs,
        # which is of type datetime.datetime().
        START_DATE = res[0]
    else:
        # SMART data reporting started at 1/1/2020
        START_DATE = datetime.datetime(2020, 1, 1)

    END_DATE = datetime.datetime.today()
    start_date = START_DATE
    delta = datetime.timedelta(days = 1)

    device_count = 0
    prediction_count = 0
    """
    We want to generate daily prediction results data points for eligible devices,
    for a given time range (START_DATE to END_DATE).

    while the daily window is in time range:
        find all distinct devices which reported during this window;
        for each device:
            get input for prediction before this point in time;
            if no input:
                continue;
            run model on this input;
            insert result to prediction_result table
    """
    print(f"STARTING: Searching for distinct devices reporting between {start_date} and {END_DATE}")
    while start_date < END_DATE:
        end_date = start_date + delta
        print(f"searching distinct devices reporting between {start_date} and {end_date}")
        # Get all distinct devices which reported during this time window
        res = get_devices_to_predict(conn, start_date, end_date)
        if len(res) == 0:
            print(f"did not find devices reporting between {start_date} and {end_date}")
        for r in res:
            device_count += 1
            prediction_input, vendor = get_prediction_input(conn, r['device_id'], r['ts'])
            if vendor == 'Not supported':
                insert_result(conn, r['device_id'], r['ts'], MODEL_RH, 'Model does not support this vendor')
                continue
            # Either not enough reports were found in this point in time,
            # or this device does not have SMART attributes.
            if prediction_input is None:
                continue

            # Invoke the model on the input we gathered:
            prediction_input_json = json.dumps(prediction_input, indent = 4, sort_keys = True)
            # print(prediction_input_json)
            run_res = subprocess.run("./model.py", stdout=subprocess.PIPE, input=prediction_input_json, encoding='ascii')
            if run_res.returncode != 0 or run_res.stdout is None:
                prediction_result = 'Model failed'
            else:
                # Remove '\n'
                prediction_result = run_res.stdout.rstrip()
            insert_result(conn, r['device_id'], r['ts'], MODEL_RH, prediction_result)
            prediction_count += 1
            # print(f"Prediction result for {r['device_id']} {prediction_input['vendor']} is: {prediction_result}")

        start_date = end_date

    conn.close()

    run_end_time = time.time()
    run_time = int(run_end_time - run_start_time)
    print(f"Processed {device_count} devices, with {prediction_count} prediction results in {run_time} seconds.")

if __name__ == '__main__':
    sys.exit(main())
