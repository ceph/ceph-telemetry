#! /usr/bin/env python3
# vim: ts=4 sw=4 expandtab

import dbhelper
import psycopg2
import psycopg2.extras
import json
import re
import sys
from os.path import isfile, join
import time
import hashlib
import copy

# Data Source Name file
DSN = '/opt/telemetry/grafana.dsn'


# Filters-out redundant functions from stack_names list.
# Returns a filtered list, or an empty list if all names have been filtered-out.
def filter_stack_names(stack_names):
    redundant_funcs = [
        '()',
        'gsignal()',
        'abort()',
        'ceph::__ceph_abort(char const*, int, char const*, std::__cxx11::basic_string<char, std::char_traits<char>, std::allocator<char> > const&)',
        'ceph::__ceph_abort(char const*, int, char const*, std::string const&)',
        'ceph::__ceph_assert_fail(char const*, char const*, int, char const*)',
        '__gnu_cxx::__verbose_terminate_handler()',
    ]

    return [i for i in stack_names if i not in redundant_funcs]

# Returns a *binary* signature
def calc_sig_v2(input):
    sig = hashlib.sha256()
    for i in input:
        sig.update(i.encode())

    # No need to convert to hex here, since we store it as binary.
    return sig.digest()

# sig_v1 compatibility:
def calc_sig_v1(bt, assert_msg):
    sig = hashlib.sha256()
    for func in sanitize_backtrace_v1(bt):
        sig.update(func.encode())
    if assert_msg:
        sig.update(sanitize_assert_msg(assert_msg).encode())

    # No need to convert to hex here, since we store it as binary.
    return sig.digest()

# sig_v1 compatibility:
def sanitize_backtrace_v1(bt):
    ret = list()
    for func_record in bt:
        # split into two fields on last space, take the first one,
        # strip off leading ( and trailing )
        func_plus_offset = func_record.rsplit(' ', 1)[0][1:-1]
        ret.append(func_plus_offset.split('+')[0])

    return ret

# sig_v1 compatibility:
def sanitize_assert_msg(msg):
    # (?s) allows matching newline.  get everything up to "thread" and
    # then after-and-including the last colon-space.  This skips the
    # thread id, timestamp, and file:lineno, because file is already in
    # the beginning, and lineno may vary.

    matchexpr = re.compile(r'(?s)(.*) thread .* time .*(: .*)\n')
    return ''.join(matchexpr.match(msg).groups())

"""
Calls in the stack usually look like:
(Finisher::finisher_thread_entry()+0x16e) [0x7f78ff116a4e]

If the offset is zero, they look like:
(ceph::__ceph_assertf_fail(char const*, char const*, int, char const*, char const*, ...)+0) [0x84fd97]

Another case:
/usr/bin/ceph-mgr() [0x51b55a]

Old sanitize_backtrace() would return:
usr/bin/ceph-mgr(

which we need to consider when we calculate the signature.
"""
# Cleans the frame from addresses and spaces, and leaves only function name and parameters.
def frame_to_name(frame):
    # split into two fields on last space, take the first one,
    # strip off leading '(' and trailing ')'.
    name = frame.rsplit(' ', 1)[0]
    if name[0] == '(':
        name = name[1:-1]

    # If no '+' is found, the entire string is returned
    return name.split('+')[0]

def bt_remove_addresses(bt):
    return [frame_to_name(x) for x in bt]

# Returns crash.spec(id). If it does not exist - creates it.
def get_spec_id(conn, crash_spec):
    cur = conn.cursor()

    # check if this sig exists:
    cur.execute("""SELECT id FROM crash.spec
                   WHERE sig_v2 = %s
                """, (crash_spec['sig_v2'],))

    spec_id = cur.fetchone()

    if spec_id:
        return spec_id[0]

    # No such sig id in crash.spec table, inserting:
    sql = """INSERT INTO crash.spec (%s) VALUES %s
             ON CONFLICT DO NOTHING
             RETURNING id"""

    dbhelper.run_insert(cur, sql, crash_spec)
    spec_id = cur.fetchone()
    # print(f"No sig id was found, inserted {spec_id[0]}")
    cur.close()

    return spec_id[0]

def gen_crash_spec(conn, bt, assert_func, assert_condition):
    crash_spec = {}

    stack_names = bt_remove_addresses(bt)
    crash_spec['stack_names'] = filter_stack_names(stack_names)

    # Calculate new crash signature:
    sig_input = copy.deepcopy(crash_spec['stack_names'])
    if assert_func:
        sig_input.append(assert_func)
        crash_spec['assert_func'] = assert_func
    if assert_condition:
        sig_input.append(assert_condition)
        crash_spec['assert_condition'] = assert_condition
    sig_v2 = calc_sig_v2(sig_input)
    crash_spec['sig_v2'] = sig_v2

    return get_spec_id(conn, crash_spec)


def main():
    start_time = time.time()
    with open(DSN, 'r') as f:
        dsn_str = f.read().strip()

    conn = psycopg2.connect(dsn_str)
    iteration_cnt = 0
    report_cnt = 0

    seen_crash_ids = {}
    # sig_v1 crash signature to sig_v2 crash signature map:
    # (since newer crash signatures cover more cases than the old one)
    seen_sig_v1 = {}

    try:
        while True:
            iteration_cnt += 1
            dict_cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
            dict_cur.execute("""SELECT id, report
                                FROM public.report
                                WHERE id > (SELECT var_value
                                            FROM crash.inserter_state
                                            WHERE var_name = 'last_processed_report_id')
                                AND
                                (   --either the key does not exist in the report
                                    NOT replace(report, '\\u0000', ' ')::jsonb ? 'organization'
                                OR
                                    -- or it exists, but its value is not 'ceph-qa'
                                    replace(report, '\\u0000', ' ')::jsonb#>'{organization}' != '"ceph-qa"'
                                )
                                ORDER BY id
                                LIMIT 100""")

            # Don't run forever:
            if not dict_cur.rowcount:
                break
            try:
                for r in dict_cur:
                    report_cnt += 1

                    # Sometimes assert messages contain '\u0000', which cannot be loaded into jsonb.
                    j = json.loads(r['report'].replace('\\u0000', ' '))
                    crashes = j.get('crashes', [])

                    # Old versions of telemetry crash reports use a dict instead of a list
                    if isinstance(crashes, dict):
                        crashes = list(crashes.values())
                    #print(f"  report loop {iteration_cnt}/{report_cnt} report.id={r['id']} crashes={len(crashes)}")

                    if len(crashes) == 0:
                        continue

                    # Order of insertion into tables:
                    #   1. crash.spec
                    #   2. crash.crash     (referencing crash.spec['id'])

                    crash_cnt = 1
                    for c in crashes:
                        # print(f"in report id {r['id']}: crash {crash_cnt}/{len(crashes)}")
                        # print(f"crash loop {iteration_cnt}/{report_cnt} report.id={r['id']} crash={crash_cnt}/{len(crashes)}")
                        crash_cnt += 1

                        # Skip crashes we already handled
                        if c['crash_id'] in seen_crash_ids:
                            continue

                        seen_crash_ids[c['crash_id']] = True

                        if 'backtrace' not in c:
                            print(f"Backtrace is missing in crash id {c['crash_id']}" \
                                    f" in report id {r['id']}, skipping.")
                            continue

                        sig_v1 = calc_sig_v1(c['backtrace'], c.get('assert_msg'))
                        # spec_id is the serial id of the new crash fingerprint (crash.spec table).
                        # It is referenced by crash.crash table.
                        # We cache the result of the mapping between sig_v1 and its corresponding row in
                        # crash.spec table (spec_id).
                        spec_id = seen_sig_v1.get(sig_v1, None)
                        if spec_id is None:
                            # A matching sig_v2 to this sig_v1 was not encountered yet.
                            spec_id = gen_crash_spec(conn, c['backtrace'], c.get('assert_func'), c.get('assert_condition'))
                            seen_sig_v1[sig_v1] = spec_id

                        # Holds the values for crash.crash table:
                        crash_t = {}
                        crash_t['spec_id'] = spec_id
                        # This is 'stack_sig' which is calculated by hashing the sanitized stack and the assert message
                        crash_t['sig_v1'] = sig_v1
                        crash_t['report_id'] = r['id']
                        crash_t['ts'] = c['timestamp']
                        # Additional fields to fetch from the raw report itself:
                        columns = [
                                'crash_id',
                                'backtrace',
                                'process_name',
                                'entity_name',
                                'ceph_version',
                                'utsname_hostname',
                                'utsname_sysname',
                                'utsname_release',
                                'utsname_version',
                                'utsname_machine',
                                'os_name',
                                'os_id',
                                'os_version_id',
                                'os_version',
                                'assert_file',
                                'assert_line',
                                'assert_thread_name',
                                'assert_msg',
                                'io_error',
                                'io_error_devname',
                                'io_error_path',
                                'io_error_code',
                                'io_error_optype',
                                'io_error_offset',
                                'io_error_length',
                                ]

                        for col in columns:
                            crash_t[col] = c.get(col)

                        sql = """INSERT INTO crash.crash (%s) VALUES %s
                                 ON CONFLICT DO NOTHING"""

                        cur = conn.cursor()
                        rows_affected = dbhelper.run_insert(cur, sql, crash_t)
                        # print(f"rows affected: {rows_affected}")
                        cur.close()
                        # *********** END OF INNER CRASHES LOOP ***************

                    # We might iterate over 100 reports that none of them has a new (unseen)
                    # crash_id, meaning none of them will be inserted to crash.crash.
                    # We need to keep the last report_id we processed, since
                    #   select max(report_id) from crash.crash
                    # will return a report_id that will be lower than these 100 report ids, and the main
                    # `select` query will keep fetching the same next 100 reports.
                    cur = conn.cursor()
                    cur.execute("""UPDATE crash.inserter_state
                                   SET var_value = %s
                                   WHERE var_name = 'last_processed_report_id'""", (r['id'],))

                    cur.close()

                    # Committing all inserts from a single report
                    conn.commit()
                    print(f"      Committing report {r['id']} done")
                    # *********** END OF REPORTS LOOP ***************

            except Exception as e:
                print(f"Exception when processing public.report.id={r['id']}\n")
                print(e)
                conn.rollback()
                raise
            # *********** END OF while ***************

    finally:
        refresh_cur = conn.cursor()
        refresh_cur.execute("REFRESH MATERIALIZED VIEW crash.spec_mv")
        conn.commit()
        refresh_cur.close()

        end_time = time.time()
        time_delta = int(end_time - start_time)
        print(f"Processed {report_cnt} reports in {time_delta} seconds\n")


if __name__ == '__main__':
    sys.exit(main())

