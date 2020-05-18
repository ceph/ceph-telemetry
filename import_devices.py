#! /usr/bin/env python3
# vim: ts=4 sw=4 expandtab

import dbhelper
import psycopg2
import psycopg2.extras
import json
import sys
import time
import re
from pathlib import Path
from os import listdir
from os.path import isfile, join

# Data Source Name file
DSN = '/opt/telemetry/grafana.dsn'

# Device tables are created via db_create_device.sql (DDL file)


# Flatten
#     "crc_error_count": {
#         "normalized": 100,
#         "raw": 0
#     },
# To
#     "crc_error_count_normalized": 100,
#     "crc_error_count_raw": 0
def parse_nvme_vendor(prefix, data, result):
    if not isinstance(data, dict):
        result[prefix] = data
    else:
        for k in data.keys():
            parse_nvme_vendor(prefix + '_' + k, data[k], result)

"""
Due to prevalent inconsistencies in reporting the correct device's vendor and
model names, we must screen all new records, and make sure they can be mapped
to their correct values.
map_input() takes the original vendor and model "raw" names of a reporting
device, and returns their correct values, and the device's class ('hw raid',
'vm', 'normal', or 'unknown' otherwise).
"""
def map_input(vendor, model):
    """
    Items in 'mapping' tuples are in this order:
    (from_vendor, from_model, to_vendor, to_model, class)
    Case is ignored in the regex search itself.
    Order matters! Put strict cases tuples before looser ones.
    Otherwise, order by alphabet of the first item in each tuple.

    Keep model names uppercased, so dashboard display is consistent.

    In case we add new classes (in addition to "hw_raid", "vm", etc.),
    we need to update the corresponding enum (device.class) in the DDL file.

    Mapping example:
        map_input("HUSMR108", "CLAR800")
    Matched rule:
        ("(HUSM[MR]\d{3})", "(CLAR\d+)", "hgst", "$v0-$m0", "normal")
    Output:
        "hgst", "HUSMR108-CLAR800", "normal"
    """
    mapping = [
        # First check for HW RAID controllers:
        ("3PARdata", "VV", None, None, "hw_raid"), # This is SAN, we tag it as HW RAID for now
        ("(asr7160)", ".*", "adaptec", "$v0", "hw_raid"),
        ("avago", "SMC.*", None, None, "hw_raid"),
        ("hp", "logical_volume", None, None, "hw_raid"),
        ("ibm", "ServeRAID.*", None, None, "hw_raid"),
        ("ibm", "2145.*", None, None, "hw_raid"),
        ("ibm", "1746_FAStT.*", None, None, "hw_raid"),
        ("dell", "PERC.*", None, None, "hw_raid"),
        ("lenovo", "RAID_930.*", None, None, "hw_raid"),
        ("lsi", ".*", None, None, "hw_raid"),
        ("(servera)", "(.*)", "unknown_raid_01", "$v0-$m0", "hw_raid"), # This might be SAN

        # Then check for devices behind VMs:
        ("0EMAZ-00WJTA", "", None, None, "vm"), # This can be considered as 'invalid', we tag it as VM for now
        ("amazon", "ELASTIC_BLOCK_STORE", None, None, "vm"),
        ("centos-.*", ".*", None, None, "vm"),
        ("Generic", "DISK.*", None, None, "vm"),
        ("hc", "VOLUME", None, None, "vm"),
        ("ORCL-VBOX.*", ".*", None, None, "vm"),
        ("qemu", ".*", None, None, "vm"),
        ("virtual", "disk", None, None, "vm"),
        ("volume", ".*", None, None, "vm"),
        ("vbox", "HARDDISK", None, None, "vm"),
        ("vmware", ".*", None, None, "vm"),

        # Then the rest:
        ("3E128-TS2-550B01", "", "oracle", "3E128-TS2-550B01", "normal"),
        ("ADATA", "SX8200PNP", None, None, "normal"),
        ("APPLE", "HDD_HTS541010A9E662", None, None, "normal"),
        ("ata", "(st\d+.*)", "seagate", "$m0", "normal"),
        ("ata", "ocz.*", "ocz", None, "normal"),
        ("crucial", "ct\d.*", None, None, "normal"),
        # Cases like "CT1000MX500SSD1", "CT2000MX500SSD1", "CT240BX200SSD1"
        ("(CT\d+.*[BM]X.*SSD1)", "", "crucial", "$v0", "normal"),
        ("dell", "Express_Flash.*", None, None, "normal"),
        # Cases like v: GB1000EAFJL   m: None
        ("(GB\d{4}EAFJ[A-Z]+)", ".*", "hp", "$v0", "normal"),
        ("gigabyte", "GP-GSTFS31120GNTD", None, None, "normal"),
        # Cases like v: hgst   m: SDLL1DLR480GCAA1:
        ("hgst", "[A-Z]{4}\d[A-Z]{3}\d{3}[A-Z]{4}\d", None, None, "normal"),
        ("hgst", "h.*", None, None, "normal"),
        # Cases like "H101414SCSUN146G_000915EKHD4A_______"
        ("hitachi", ".*(H101414SCSUN146G).*", "hgst", "$m0", "normal"),
        ("hitachi", "h.*", "hgst", None, "normal"),
        ("hp", "[A-Z]{2}\d{4}[A-Z]{5}", None, None, "normal"),
        # Cases like "HUSMR1680ASS204"
        ("(HU.*\d+A[LS].*\d+)", "", "hgst", "$v0", "normal"),
        # Cases like: vendor: "HUSMR108", model: "CLAR800"
        ("(HUSM[MR]\d{3})", "(CLAR\d+)", "hgst", "$v0-$m0", "normal"),
        ("intel", "ssd.*", None, None, "normal"),
        ("JMicron", "", None, "UNKNOWN", "normal"),
        # Cases like: v: kingston   m: SHSS37A480G
        ("kingston", "S.*G", None, None, "normal"),

        # Cases like: MB4000GVYZK, MB6000GEFNB, MB8000GFECR, MB1000EAMZE, MB8000GFECR, MK0800GCTZB
        ("(M.\d{4}[A-Z]{5})", "", "hp", "$v0", "normal"),
        # Cases like
        #    v: MG06ACA10TE   m: 00YK043D7A01892LEN
        #    v: MG06ACA10TE   m: _________00YK043D7A01892LEN
        ("(MG06ACA10TE)", "_*00YK043D7A01892LEN", "toshiba", "$v0", "normal"),
        ("(MG04ACA400N)", "_________49Y6003_49Y6006LEN", "toshiba", "$v0", "normal"),
        # Cases like MKNSSDRE1TB
        ("(MKNSSDRE\d+TB)", "", "mushkin", "$v0", "normal"),
        # Cases like MT0800KEXUU
        ("(MT0800KEXUU)", "", "hp", "$v0", "normal"),
        ("(MTFDDAK480TD[CN])", "", "micron", "$v0", "normal"),
        ("(MTFDHBG800MCG-1AN1ZABYY)", "", "micron", "$v0", "normal"),
        ("(C400-MTFDDAK256M)", "", "micron", "$v0", "normal"),
        ("micron", "\d{4}_.*", None, None, "normal"),
        ("micron", "mt.*", None, None, "normal"),
        ("(MZ7LH480HAHQ0D3)", "", "dell", "$v0", "normal"),
        ("(MZ7LM240HCGR-000v3)", "00YC391_00YC394LEN.*", "samsung", "$v0", "normal"),
        ("NA", "HUA721010KLA330", "hgst", "HUA721010KLA330", "normal"),
        ("ocz", "INTREPID_3800", None, None, "normal"),
        ("(OCZ-REVODRIVE3)", "(X)", "ocz", "$v0-$m0", "normal"),
        ("sabrent", "", None, "UNKNOWN", "normal"),
        ("sata", "ssd", "phison_oem", "sata_ssd", "normal"),
        ("SAMSUNG", "843T_240GB", None, None, "normal"),
        ("samsung", "ssd.*", None, None, "normal"),
        ("samsung", "[A-Z]{2}.*", None, None, "normal"),
        ("SanDisk", "SDSSD.*", None, None, "normal"),
        ("(sdlfgd7r-480g-1ha1)", "", "sandisk", "$v0", "normal"),
        # Cases like SDLL1MLR032TCAA1
        ("(SDLL1[DM]LR\d{3}[TG]CAA1)", "", "hgst", "$v0", "normal"),
        ("seagate", "st.*", None, None, "normal"),
        # Cases like ST4000NM0024, ST4000NM0035
        ("(st\d{4}nm\d{4})", "__.*", "seagate", "$v0", "normal"),
        ("ST320LM001", "HN-M320MBB", "samsung", "ST320LM001_HN-M320MBB", "normal"),
        ("(st.*)", "", "seagate", "$v0", "normal"),
        ("(thnsf8400ccse)", "", "toshiba", "$v0", "normal"),
        ("TOSHIBA", "DT.*", None, None, "normal"),
        ("TOSHIBA", "THNSNJ480PCS3", None, None, "normal"),
        ("TOSHIBA", "HD.*", None, None, "normal"),
        ("TOSHIBA", "m.*", None, None, "normal"),
        # Cases like VK000480GWCNQ, VK000480GWJPE
        ("(VK000480GW[A-Z]{3})", "", "hp", "$v0", "normal"),
        ("WD", "WD4001FYYG-01SL3", "wdc", None, "normal"),
        ("WDC", "CL_SN720_SDAQNTW-512G-2000", None, None, "normal"),
        ("wdc", "_(w.*)", None, "$m0", "normal"),
        ("wdc", "w.*", None, None, "normal"),
        ("wdc", "cd_.*", None, None, "normal"),
    ]

    for m in mapping:
        v_res = re.search(f"^{m[0]}$", vendor, re.I)
        m_res = re.search(f"^{m[1]}$", model, re.I)

        if v_res and m_res:
            d = {}
            d.update({f"v{i}": v_res.groups()[i] for i in range(len(v_res.groups()))})
            d.update({f"m{i}": m_res.groups()[i] for i in range(len(m_res.groups()))})

            to_vendor = m[2]
            if to_vendor is not None:
                for k, v in d.items():
                    to_vendor = to_vendor.replace(f"${k}", v)
            else:
                to_vendor = vendor;

            to_model = m[3]
            if to_model is not None:
                for k, v in d.items():
                    to_model = to_model.replace(f"${k}", v)
            else:
                to_model= model;

            return to_vendor.lower(), to_model.upper(), m[4]
    """
    Didn't find any known mapping, return original vendor and model, and class "unknown":
    We lower() the vendor and upper() the model in order to prevent duplicates in device.spec table,
    which has a case sensitive composite unique key (vendor, model).
    """
    return vendor.lower(), model.upper(), "unknown"

# We process a single report which might be empty, but this (vendor, model) device spec
# might already have complete data in the DB (retrieved from another device).
def get_device_type(error, interface, rotation_rate, d_class, r_id):
    # No smarctl data
    if error:
        return None
    interface = interface.lower() if interface else None
    if interface == "nvme":
        return "nvme"
    else:
        if rotation_rate is not None:
            # rotation_rate != 0, meaning it's a hard disk.
            if rotation_rate:
                return "hdd"
            else:
                return "ssd"
        # rotation_rate == None  and  interface != nvme
        # This is weird, unless the device is behind HW RAID or a VM.
        # Alert in case it's not.
        elif d_class == "normal":
            print(f"In report_id {r_id} device class is normal, but type could not be retrieved. Interface: {interface}, rotation_rate: {rotation_rate}.")
    return None

# Returns the device.spec['id'] or creates a new device.spec record if it does not exist.
def fetch_spec_id(conn, device_spec):
    cur = conn.cursor()
    """
    It's impossible to run "INSERT" with "ON CONFLICT (id) DO NOTHING",
    and have the exiting id returned, thus we first check if the record
    exists in device.spec before trying to insert.
    """
    dict_cur_spec = conn.cursor(name='server_side_cursor_s_id', withhold=True, cursor_factory=psycopg2.extras.DictCursor)
    dict_cur_spec.execute("""SELECT id, type, interface, capacity
                            FROM device.spec
                            WHERE vendor = %s AND model = %s
                            """, (device_spec['vendor'], device_spec['model']))

    # fetchone() returns a None object in case of no results.
    fetched_spec = dict_cur_spec.fetchone()
    dict_cur_spec.close()

    """
    The case where vendor & model exist in the DB, but other spec fields
    couldn't be retrieved since the report was nearly empty (can happen when
    smartctl version is < 7.0). In case these fields are empty, we assign
    their corresponding values from newer reports (which might still be None).
    """
    if fetched_spec is not None:
        need_update = False
        if fetched_spec['type'] is None and device_spec['type'] is not None:
            fetched_spec['type'] = device_spec['type']
            need_update = True
        if fetched_spec['interface'] is None and device_spec['interface'] is not None:
            fetched_spec['interface'] = device_spec['interface']
            need_update = True
        if fetched_spec['capacity'] is None and device_spec['capacity'] is not None:
            fetched_spec['capacity'] = device_spec['capacity']
            need_update = True

        if need_update:
            cur.execute("""UPDATE device.spec
                        SET
                            type = %s,
                            interface = %s,
                            capacity = %s
                        WHERE id = %s
                        """, (fetched_spec['type'], fetched_spec['interface'], fetched_spec['capacity'], fetched_spec['id']))

        cur.close()
        return fetched_spec['id']

    # These vendor & model are not in device_spec table, inserting:
    else:
        sql = """INSERT INTO device.spec (%s) VALUES %s
                 RETURNING id"""
        dbhelper.run_insert(cur, sql, device_spec)
        fetched_spec = cur.fetchone()

        cur.close()
        return fetched_spec[0]

def fetch_device_id(cur, device):
    cur.execute("""SELECT id FROM device.device
                   WHERE vmu = %s
                """, (device['vmu'],)) # The ',' in (device['vmu'],) creates a tuple and is mandatory.

    device_id = cur.fetchone()

    # No such device in device table, inserting:
    if not device_id:
        sql = """INSERT INTO device.device (%s) VALUES %s
                RETURNING id"""
        dbhelper.run_insert(cur, sql, device)
        device_id = cur.fetchone()

    return device_id[0]

"""
Some versions of smartctl report certain fields as a dictionary of their
'n'umeric and 's'tring representations:
   "blocks": {
       "n": 7814037168,
       "s": "7814037168"
   },
Newer versions of smartctl do not do that and just report:
   "blocks": 7814037168
"""
def parse_value_by_type(v):
    if isinstance(v, list):
        # FIXME: include list type values, like 'temperature_sensors'
        return None
    if isinstance(v, dict):
        if 'n' not in v:
            raise ValueError("No 'n' in dict")
        return v['n']
    else:
        return v

def populate_device_smart_nvme(cur, smart_attr_nvme, device_id, report_id, ts):
    for k, v in smart_attr_nvme.items():
        device_smart_nvme = {}
        device_smart_nvme['device_id'] = device_id
        device_smart_nvme['report_id'] = report_id
        device_smart_nvme['ts']        = ts
        device_smart_nvme['attr_name'] = k
        device_smart_nvme['attr_val']  = parse_value_by_type(v)

        if device_smart_nvme['attr_val'] is None:
            print(f"attr_name: {k} of device report id {report_id}, attr_val is of type list.\n")
        sql = 'INSERT INTO device.smart_nvme (%s) VALUES %s'
        dbhelper.run_insert(cur, sql, device_smart_nvme)

# NVMe vendor specific data:
# TODO: Support newer versions of nvme-cli here and in the telemetry client
def populate_device_smart_nvme_vs(cur, device, report, device_id, report_id, ts):
    data = report.get('nvme_smart_health_information_add_log', {})
    # 'Device stats' is found in Intel drives
    dev_stats = data.get('Device stats') if data.get('Device stats') else data
    dev_stats_parsed = {}
    if dev_stats:
        for k, v in dev_stats.items():
            parse_nvme_vendor(k, v, dev_stats_parsed)

    # No nested dictionaries at this point
    for k, v in dev_stats_parsed.items():
        device_smart_nvme_vs = {}
        device_smart_nvme_vs['device_id'] = device_id
        device_smart_nvme_vs['report_id'] = report_id
        device_smart_nvme_vs['ts']        = ts
        device_smart_nvme_vs['attr_name'] = k
        device_smart_nvme_vs['attr_val']  = v

        sql = 'INSERT INTO device.smart_nvme_vs (%s) VALUES %s'
        dbhelper.run_insert(cur, sql, device_smart_nvme_vs)

def populate_device_smart_sata(cur, sata_smart_attr, device_id, report_id, ts):
    for attr in sata_smart_attr.get('table', []):
        device_smart_sata = {}
        device_smart_sata['device_id']    = device_id
        device_smart_sata['report_id']    = report_id
        device_smart_sata['ts']           = ts
        device_smart_sata['attr_id']      = attr['id']
        device_smart_sata['attr_name']    = attr['name']
        device_smart_sata['attr_val']     = attr['raw']['value']
        device_smart_sata['attr_val_str'] = attr['raw']['string']
        device_smart_sata['attr_worst']   = attr['worst']

        sql = 'INSERT INTO device.smart_sata (%s) VALUES %s'
        dbhelper.run_insert(cur, sql, device_smart_sata)

def import_report(conn, r):
    cur = conn.cursor()
    # vmu stands for vendor_model_uuid
    # (that's the original anonymized device id generated on the client side)
    vmu = r['device_id']
    # ts represents SMART scraping time
    ts = r['report_stamp']
    rep = json.loads(r['report'])
    report_id = r['id']
    error = rep.get('error')

    """
    Order of insertion into tables:
      1. device.spec
      2. device.device     (referencing device.spec['id'])
      3. device.ts_device  (referencing device.device['id])
    """
    device_spec = {}
    device = {}
    ts_device = {}

    device['vmu'] = vmu
    device['host_id'] = rep.get('host_id', None)
    # verify vmu.count('_') > 1 ?
    device_spec['vendor'] = vmu[: vmu.find('_')]
    device_spec['model'] = vmu[vmu.find('_') + 1 : vmu.rfind('_')]
    device_spec['capacity'] = parse_value_by_type(rep.get('user_capacity', {}).get('bytes'))
    ts_device['report_id'] = report_id
    ts_device['ts'] = ts
    ts_device['error'] = error

    """
    In device.spec table a device's class can be 'normal' (which means we
    recognize its vendor and model, and it's not reporting behind a VM or a HW
    RAID controller). Yet, we may not know its complete spec, because all
    devices by this spec are reporting invalid telemetry (due to an old version
    of smartctl, sudoers issues, etc.). In this scenario the record in
    device.spec table will contain values only for vendor, model, and class,
    while either type, interface, capacity can be NULL.
    """
    new_vendor, new_model, new_class = map_input(device_spec['vendor'], device_spec['model'])

    # Creating a record of the (vendor, model) mapping result for debugging:
    mapping = {}
    mapping['i_vendor'] = device_spec['vendor']
    mapping['i_model'] = device_spec['model']
    mapping['o_vendor'] = new_vendor
    mapping['o_model'] = new_model

    sql = """INSERT INTO device.mapping (%s) VALUES %s"""
    dbhelper.run_insert(cur, sql, mapping)

    # Assigning the new values
    device_spec['vendor'] = new_vendor
    device_spec['model'] = new_model
    device_spec['class'] = new_class

    rotation_rate = rep.get('rotation_rate')
    interface = rep.get('device', {}).get('protocol')
    device_spec['interface'] = interface.lower() if interface else None

    """
    device.spec table holds unique (vendor, model) records and their specifications;
    There are multiple devices with the same (vendor, model). Some devices report invalid smartctl
    output - we can derive the correct spec from these which report valid stats alone.
    """
    device_spec['type'] = get_device_type(error, device_spec['interface'], rotation_rate, device_spec['class'], report_id)
    device['spec_id'] = fetch_spec_id(conn, device_spec)
    device_id = fetch_device_id(cur, device)
    ts_device['device_id'] = device_id

    sql = """INSERT INTO device.ts_device (%s) VALUES %s"""
    dbhelper.run_insert(cur, sql, ts_device)

    smart_attr_nvme = rep.get('nvme_smart_health_information_log')
    if smart_attr_nvme:
        populate_device_smart_nvme(cur, smart_attr_nvme, device_id, report_id, ts)

    # Device's Vendor Specific extended SMART log page contents.
    # Currently all records accidentally have this key, filtering nvme only:
    if device_spec['interface'] == 'nvme' and 'nvme_smart_health_information_add_log' in rep:
        populate_device_smart_nvme_vs(cur, device, rep, device_id, report_id, ts)

    sata_smart_attr = rep.get('ata_smart_attributes')
    if sata_smart_attr:
        populate_device_smart_sata(cur, sata_smart_attr, device_id, report_id, ts)

    # Committing everything as a single transaction
    conn.commit()
    cur.close()

"""
Update records of class "unknown" in device.spec.
Re-run logic that identifies vendor, model, and class of devices that were
previously identified as 'unknown' and update them in the DB.
"""
def update_unknown_spec(conn):
    dict_cur_update = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    dict_cur_update.execute("""SELECT vendor, model, id
                               FROM device.spec
                               WHERE class = 'unknown'
                               ORDER BY id""")

    for r in dict_cur_update:
        new_vendor, new_model, new_class = map_input(r['vendor'], r['model'])
        # Nothing to do if class is still unknown:
        if new_class == "unknown":
            continue
        try:
            cur = conn.cursor()
            # Update the spec record:
            cur.execute("""UPDATE device.spec
                        SET
                            vendor = %s,
                            model = %s,
                            class = %s
                        WHERE id = %s
                        """ , (new_vendor.lower(), new_model.upper(), new_class, r['id']))

            conn.commit()
            cur.close()

        except Exception as e:
            # These messages help to manually fix any conflict.
            # We prefer to examine each case rather than auto fix it.
            print("\n\n")
            print(f"Exception when updating device.spec.id={r['id']} from vendor={r['vendor']}, model={r['model']}, class=unknown" \
                    f" to vendor={new_vendor}, model={new_model}, class={new_class}")
            print(e)
            print(f"# SELECT * FROM device.spec WHERE id={r['id']} OR (vendor='{new_vendor}' AND model='{new_model}');")
            new_spec_id = f"(SELECT id FROM device.spec WHERE vendor='{new_vendor}' AND model='{new_model}')"
            print(f"# old->new # UPDATE device.device SET spec_id={new_spec_id} WHERE spec_id={r['id']};\n" \
                  f"#   INSERT INTO device.spec_deleted select * from device.spec WHERE id={r['id']};\n" \
                  f"#   DELETE FROM device.spec WHERE id={r['id']};")
            print(f"# new->old # UPDATE device.device SET spec_id={r['id']} WHERE spec_id={new_spec_id};\n" \
                  f"#   INSERT INTO device.spec_deleted select * from device.spec WHERE id={new_spec_id};\n" \
                  f"#   DELETE FROM device.spec WHERE id={new_spec_id};")

            # We rollback here to clean the connection from possible exceptions
            conn.rollback()

    dict_cur_update.close()

def main():
    start_time = time.time()
    with open(DSN, 'r') as f:
        dsn_str = f.read().strip()

    conn = psycopg2.connect(dsn_str)

    update_unknown_spec(conn)

    # Create a named server-side cursor
    dict_cur = conn.cursor(name='server_side_cursor', withhold=True, cursor_factory=psycopg2.extras.DictCursor)

    dict_cur.itersize = 10

    """
    Fetch only reports which are not in ts_device;
    COALESCE returns the first non-NULL value, so '0' is
    the returned id in case ts_device table is empty.
    This SELECT query is outside of the 'try' block, since we prefer
    not to refresh materialized view (via finally block) in case this query fails
    (even though it's okay to do so, it's just not the correct flow).
    """
    dict_cur.execute("""SELECT device_id, report_stamp, report, id
                        FROM public.device_report
                        WHERE device_report.id > (SELECT COALESCE(MAX(ts_device.report_id), 0)
                                    FROM device.ts_device)
                        ORDER BY id""")
    cnt = 0
    try:
        for r in dict_cur:
            cnt += 1
            import_report(conn, r)
    except:
        print(f"Exception when processing public.device_report.id={r['id']}\n")
        # Since we insert / update multiple tables in each import_report() call,
        # we rollback all pending actions to avoid any inconsistencies.
        conn.rollback()
        raise
    finally:
        # Creating a new cursor in case something happened to the existing one:
        refresh_cur = conn.cursor()
        refresh_cur.execute("REFRESH MATERIALIZED VIEW device.weekly_reports_sliding")
        conn.commit()
        refresh_cur.close()

        dict_cur.close()
        end_time = time.time()
        time_delta = int(end_time - start_time)
        conn.close()
        print(f"Processed {cnt} reports in {time_delta} seconds\n")


if __name__ == '__main__':
    sys.exit(main())
