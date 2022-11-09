/*
Note: This script DROPS 'crash' schema's tables and content ENTIRELY, then recreates it.
Use it with caution.
Other permissions are managed in db_create_roles.sql.

Run this DDL as user 'telemetry'.
*/

-- Uncomment this block when you really mean it:
/*
DROP TABLE crash.sig CASCADE;
DROP TABLE crash.spec CASCADE;
DROP TABLE crash.crash CASCADE;
DROP TABLE crash.inserter_state;
DROP TABLE crash.spec_status CASCADE;
DROP TABLE crash.sentry_not_imported CASCADE;
DROP TABLE crash.spec_to_redmine_main_issue CASCADE;
DROP TABLE crash.redmine_description_added CASCADE;
DROP TABLE crash.redmine_email_sent CASCADE;
DROP MATERIALIZED VIEW crash.spec_mv;
DROP SCHEMA IF EXISTS crash CASCADE;
*/

CREATE SCHEMA crash;

GRANT USAGE ON SCHEMA crash TO grafana;
GRANT CREATE ON SCHEMA crash TO grafana;
GRANT ALL ON ALL TABLES IN SCHEMA crash TO grafana;
GRANT ALL ON ALL SEQUENCES IN SCHEMA crash TO grafana;

GRANT USAGE ON SCHEMA crash TO grafana_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA crash TO grafana_ro;

ALTER DEFAULT PRIVILEGES
    IN SCHEMA crash
    GRANT SELECT ON TABLES TO grafana_ro;

-- Grant access to user 'grafana' on future tables & sequences in schema crash
ALTER DEFAULT PRIVILEGES
    --FOR ROLE telemetry
    IN SCHEMA crash
    GRANT ALL ON TABLES TO grafana;

ALTER DEFAULT PRIVILEGES
    --FOR ROLE telemetry
    IN SCHEMA crash
    GRANT ALL ON SEQUENCES TO grafana;

CREATE TABLE crash.spec_status (
    id                  SERIAL PRIMARY KEY,
    ignore              BOOLEAN,
    description         TEXT NOT NULL UNIQUE
);

INSERT INTO crash.spec_status
    (description, ignore)
VALUES
    ('Unknown', FALSE),
    ('New', FALSE),
    ('Triaged', FALSE),
    ('Need More Info', FALSE),
    ('In Progress', FALSE),
    ('Fix Under Review', FALSE),
    ('Pending Backport', FALSE),
    ('Resolved', TRUE),
    ('Closed', TRUE),
    ('Rejected', TRUE),
    ('Won''t Fix', TRUE),
    ('Won''t Fix - EOL', TRUE),
    ('Can''t reproduce', TRUE),
    ('Duplicate', TRUE),
    ('HeartbeatMap', TRUE),
    ('Development', TRUE),
    ('EOL', TRUE),
    ('NA', TRUE); -- Not Applicable; cases like empty stack trace, etc.

-- Holds a row per crash sig;
CREATE TABLE crash.spec (
    id                  SERIAL PRIMARY KEY,
    spec_status_id      INTEGER REFERENCES crash.spec_status(id) NOT NULL default 1,
    -- a crash signature is SHA256, thus we store it as type BYTEA.
    sig_v2              BYTEA NOT NULL UNIQUE,
    -- stack_names holds only the filtered names of the stack's functions
    -- (we filter out redundant function names)
    stack_names         TEXT[],
    assert_condition    TEXT,
    assert_func         TEXT
);

-- Holds data that is unique per crash.
-- Keep the fields here in sync with ceph/src/global/signal_handler.cc
CREATE TABLE crash.crash (
    id                  SERIAL PRIMARY KEY,
    spec_id             INTEGER REFERENCES crash.spec(id) NOT NULL,
    -- theoretically (crash_id, cluster_id) should be UNIQUE, however, it is almost impossible
    -- for 2 different clusters to generate the exact same crash_id ( = timestamp_uuid4())
    crash_id            TEXT UNIQUE NOT NULL,
    --cluster_id can be derived from report_id, no need to add this col here
    report_id           INTEGER REFERENCES public.report(id),
    ts                  TIMESTAMP,
    sig_v1              BYTEA NOT NULL, -- This is 'stack_sig' which is calculated by hashing the stack and the assert message
    backtrace           TEXT[], -- Full backtrace, not sanitized
    process_name        TEXT,
    entity_name         TEXT,
    ceph_version        TEXT,
    utsname_hostname    TEXT,
    utsname_sysname     TEXT,
    utsname_release     TEXT,
    utsname_version     TEXT,
    utsname_machine     TEXT,
    -- os-release
    os_name             TEXT,
    os_id               TEXT,
    os_version_id       TEXT,
    os_version          TEXT,
    -- assert
    --assert_condition and assert_func are part of crash.sig table since we use them to calculate the signature.
    assert_file         TEXT,
    assert_line         TEXT,
    assert_thread_name  TEXT,
    assert_msg          TEXT,
    -- eio
    io_error            TEXT,
    io_error_devname    TEXT,
    io_error_path       TEXT,
    io_error_code       TEXT,
    io_error_optype     TEXT,
    io_error_offset     TEXT,
    io_error_length     TEXT
);

-- Holds the latest public.report(id) to be processed by import_crashes.py,
-- and the latest crash.crash(id) that was imported to Sentry.
CREATE TABLE crash.inserter_state (
   var_name             TEXT PRIMARY KEY,
   var_value            BIGINT, -- in case we succeed 2 billion reports...
   var_value_timestamp  TIMESTAMP
);

-- Initialize
INSERT INTO crash.inserter_state
    (var_name, var_value, var_value_timestamp)
VALUES
    ('last_processed_report_id', -1, NULL),
    ('last_imported_crash_id_to_sentry', -1, NULL),
    ('last_redmine_sync_crash_id', -1, NULL),
    ('last_redmine_sync_ts', NULL, '1970-01-01');

-- Holds the crashes that could not be imported to Sentry, and their status codes.
CREATE TABLE crash.sentry_not_imported (
    crash_id_serial     INTEGER REFERENCES crash.crash(id) NOT NULL,
    crash_id_text       TEXT REFERENCES crash.crash(crash_id )UNIQUE NOT NULL,
    details             TEXT
);

-- Holds the map between a spec id (which represents sig_v2 and its input),
-- and a redmine issue id. There could be multiple redmine issues related to multiple spec ids,
-- hence the many to many relation, and the primary key.
CREATE TABLE crash.spec_to_redmine_main_issue (
    spec_id             INTEGER PRIMARY KEY REFERENCES crash.spec(id),
    issue_id            INTEGER, -- tracker id; '-1' means there is no corresponding tracker for this spec
    issue_status        TEXT -- status of the issue ('Resolved', 'Duplicate', etc.)
);

CREATE TABLE crash.redmine_description_added (
    spec_id             INTEGER REFERENCES crash.spec(id) NOT NULL,
    issue_id            INTEGER NOT NULL,
    PRIMARY KEY(spec_id, issue_id)
);

CREATE TABLE crash.redmine_email_sent (
    spec_id             INTEGER REFERENCES crash.spec(id) NOT NULL,
    version             TEXT NOT NULL,
    PRIMARY KEY(spec_id, version)
);


CREATE MATERIALIZED VIEW crash.spec_mv AS
WITH
    crashes_agg AS (
        SELECT
            spec_id,
            count(*) as crash_count,
            count(distinct(split_part(ceph_version, '-', 1))) as minors_count,
            array_agg(distinct(split_part(ceph_version, '-', 1))) minors_affected,
            count(distinct(split_part(ceph_version, '.', 1))) as majors_count,
            array_agg(distinct(split_part(ceph_version, '.', 1))) majors_affected,
            count(distinct(grafana.ts_cluster.cluster_id)) AS clusters_count,
            min(crash.crash.ts) AS ts_first_occurrence,
            max(crash.crash.ts) AS ts_last_occurrence,
            array_agg(distinct(encode(sig_v1, 'hex'))) AS sig_v1_arr,
            array_agg(distinct(process_name)) AS daemon_arr
        FROM crash.crash
        LEFT JOIN grafana.ts_cluster ON crash.crash.report_id = grafana.ts_cluster.report_id
        GROUP BY spec_id
    )
SELECT
    crash.spec.*, crash_count, minors_count, minors_affected, majors_count, majors_affected,
    clusters_count, ts_first_occurrence, ts_last_occurrence, sig_v1_arr, daemon_arr, crash.spec_status.description,
    crash.spec_status.ignore
FROM
    crash.spec
    INNER JOIN crash.spec_status ON crash.spec.spec_status_id = crash.spec_status.id
    LEFT JOIN crashes_agg on crash.spec.id = crashes_agg.spec_id
ORDER BY
    crash.spec.id, ts_first_occurrence ASC;

-- Run this command as user 'postgres', since user 'telemetry' is not part of 'grafana' role.
ALTER MATERIALIZED VIEW crash.spec_mv OWNER TO grafana;
GRANT SELECT ON crash.spec_mv TO grafana_ro;
