/*
Note: This script DROPS 'device' schema's tables and content ENTIRELY, then recreates it.
Use it with caution.
Other permissions are managed in db_create_cluster.sql and db_create_roles.sql.
*/

-- Uncomment this block when you really mean it:
/*
DROP TABLE device.spec CASCADE;
DROP TABLE device.device CASCADE;
DROP TABLE device.ts_device CASCADE;
DROP TABLE device.smart_sata CASCADE;
DROP TABLE device.smart_nvme CASCADE;
DROP TABLE device.smart_nvme_vs CASCADE;
DROP TABLE device.mapping; -- Remove this when we remove 'device.mapping' table
-- These types will not be dropped if they are referred by tables in other schemas:
DROP TYPE IF EXISTS device.interface;
DROP TYPE IF EXISTS device.class;
DROP TYPE IF EXISTS device.type;
*/

CREATE SCHEMA device;

GRANT USAGE ON SCHEMA device TO grafana;
GRANT CREATE ON SCHEMA device TO grafana;
GRANT ALL ON ALL TABLES IN SCHEMA device TO grafana;
GRANT ALL ON ALL SEQUENCES IN SCHEMA device TO grafana;

GRANT USAGE ON SCHEMA device TO grafana_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA device TO grafana_ro;

ALTER DEFAULT PRIVILEGES
    IN SCHEMA device
    GRANT SELECT ON TABLES TO grafana_ro;

-- Grant access to user 'grafana' on future tables & sequences in schema device
ALTER DEFAULT PRIVILEGES
    FOR ROLE telemetry
    IN SCHEMA device
    GRANT ALL ON TABLES TO grafana;

ALTER DEFAULT PRIVILEGES
    FOR ROLE telemetry
    IN SCHEMA device
    GRANT ALL ON SEQUENCES TO grafana;

CREATE TYPE device.interface AS ENUM ('ata', 'sata', 'sas', 'scsi', 'nvme');
CREATE TYPE device.class AS ENUM ('hw_raid', 'normal', 'vm', 'unknown');

/*
We could distinguish between 'spinning' and 'flash' types, however since we want
to display distributions of (hdd, ssd, nvme) we choose to include 'nvme' here
as a type, even though it's an interface over 'flash'. This allows for easier
querying. We assume:

spinning --> hdd
                                  --> sata/sas --> ssd
                                 /
flash    --> (check interface) --
                                 \
                                  --> nvme --> nvme
*/
CREATE TYPE device.type AS ENUM ('hdd', 'ssd', 'nvme');

-- Holds a row per device vendor and model with generic information about that model.
CREATE TABLE device.spec (
    id              SERIAL PRIMARY KEY,
    vendor          VARCHAR(128),
    model           VARCHAR(128),
    type            device.type,
    interface       device.interface,
    class           device.class,
    capacity        BIGINT,
    UNIQUE          (vendor, model)
);

-- Holds data that is unique per device and doesn't change over time.
CREATE TABLE device.device (
    id              SERIAL PRIMARY KEY,
    -- vmu format: vendor_model_<uuid>. That's the original anonymized device id generated on the client side.
    vmu             VARCHAR(128) NOT NULL UNIQUE,
    spec_id         INTEGER REFERENCES device.spec(id) NOT NULL, -- No 'ON DELETE CASCADE' since spec_id is one-to-many
    host_id         VARCHAR(128)
);

-- Time series table that holds a reference of each device report.
-- Mainly used to create the weekly_reports_sliding materialized view.
CREATE TABLE device.ts_device (
    report_id   INTEGER REFERENCES public.device_report(id) NOT NULL PRIMARY KEY,
    device_id   INTEGER NOT NULL REFERENCES device.device(id) ON DELETE CASCADE,
    ts          TIMESTAMP,
    error       TEXT
);

/*
device_id is a serial id generated on inserts to device.device table.
report_id is a serial id generated on inserts to public.device_report table.
We include both columns in these tables since it's significantly easier to query this way
(reduces inevitable 'joins').
*/
CREATE TABLE device.smart_sata (
    device_id       INTEGER NOT NULL REFERENCES device.device(id) ON DELETE CASCADE,
    report_id       INTEGER REFERENCES public.device_report(id) NOT NULL,
    ts              TIMESTAMP,
    attr_id         INTEGER,
    attr_name       VARCHAR(128),
    attr_raw        BIGINT,
    attr_raw_str    VARCHAR(128),
    attr_norm       BIGINT,
    attr_worst      BIGINT
);

CREATE TABLE device.smart_nvme (
    device_id       INTEGER NOT NULL REFERENCES device.device(id) ON DELETE CASCADE,
    report_id       INTEGER REFERENCES public.device_report(id) NOT NULL,
    ts              TIMESTAMP,
    attr_name       VARCHAR(128),
    attr_val        BIGINT,
    attr_val_err    TEXT --In case attr_val could not be retrieved. This column is for monitoring,
    -- and could probably be removed at some point assuming it's empty.
);

-- Device's vendor specific extended SMART 'log page contents'
CREATE TABLE device.smart_nvme_vs (
    device_id       INTEGER NOT NULL REFERENCES device.device(id) ON DELETE CASCADE,
    report_id       INTEGER REFERENCES public.device_report(id) NOT NULL,
    ts              TIMESTAMP,
    attr_name       VARCHAR(128),
    attr_val        BIGINT,
    attr_val_err    TEXT -- Same as in device.smart_nvme
);

/*
When Grafana draws a graph with a resolution of a day, it expects the DB
query to return a data point per day. A device may report less frequently
than once a day, thus the DB query will return a spike on the day that
the device reports, and a drop on days it doesn't.
To normalize this, we create a materialized view that for each day holds
a list of all reports that occurred on the previous week - only the most
recent report for each device within that previous week.
We use a materialized view because it's much faster than a regular view.
We update the materialized view in import_devices.py, which creates it
from scratch with the most recent reports.
*/
CREATE MATERIALIZED VIEW device.weekly_reports_sliding AS
    SELECT
        DISTINCT ON(daily_window, device_id)
        daily_window,
        report_id
        /*
        GENERATE_SERIES generates a table with a single column 'daily_window'
        which holds all the days (a row per day) between the first report
        and *tomorrow*. Day format is 'YYYY-MM-DD 00:00:00'.
        */
    FROM
        device.ts_device d,
        GENERATE_SERIES('2019-03-01', now()::date + interval '1' day, interval '1' day) daily_window
    WHERE
        d.ts BETWEEN daily_window - interval '7' day AND daily_window + interval '1' day
    ORDER BY
        daily_window,
        device_id,
        d.ts DESC;

-- Run this command as user 'postgres', since user 'telemetry' is not part of 'grafana' role.
ALTER MATERIALIZED VIEW device.weekly_reports_sliding OWNER TO grafana;

GRANT SELECT ON device.weekly_reports_sliding TO grafana_ro;

-- Holds (vendor, model) mappings results - for debugging purposes only.
-- It can be removed at some point.
CREATE TABLE device.mapping (
    id                SERIAL PRIMARY KEY,
    i_vendor          VARCHAR(128), -- Input vendor
    i_model           VARCHAR(128), -- Input model
    o_vendor          VARCHAR(128), -- Output vendor
    o_model           VARCHAR(128)  -- Output model
);

CREATE TABLE device.prediction_result (
    device_id   INTEGER PRIMARY KEY REFERENCES device.device(id) ON DELETE CASCADE,
    result      TEXT
);

