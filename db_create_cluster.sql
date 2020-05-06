-- In case we need to drop the schema, make sure to use CASCADE.
-- DROP SCHEMA IF EXISTS grafana CASCADE;

/*
  We don't use 'IF NOT EXISTS' in CREATE SCHEMA
  so that an error will be thrown in case the schema
  does exist.
*/
CREATE SCHEMA grafana;

GRANT usage ON SCHEMA grafana TO grafana;
GRANT ALL ON ALL TABLES IN SCHEMA grafana TO grafana;
GRANT ALL ON ALL SEQUENCES IN SCHEMA grafana TO grafana;


CREATE TABLE grafana.ts_cluster (
    report_id           INTEGER REFERENCES public.report(id) PRIMARY KEY,
    ts                  TIMESTAMP,
    cluster_id          VARCHAR(50),
    created             TIMESTAMP, /* cluster creation date */
    channel_basic       BOOLEAN,
    channel_crash       BOOLEAN,
    channel_device      BOOLEAN,
    channel_ident       BOOLEAN,
    total_bytes         BIGINT,
    total_used_bytes    BIGINT,
    osd_count           INTEGER,
    mon_count           INTEGER,
    ipv4_addr_mons      INTEGER,
    ipv6_addr_mons      INTEGER,
    v1_addr_mons        INTEGER,
    v2_addr_mons        INTEGER,
    rbd_num_pools       INTEGER,
    fs_count            INTEGER,
    hosts_num           INTEGER,
    pools_num           INTEGER,
    pg_num              INTEGER
);

CREATE INDEX ON grafana.ts_cluster (ts);

/*
  The json report's metadata section:

    "metadata": {
      "osd": {
        "cpu": {
          "Intel(R) Xeon(R) CPU E5-2620 v4 @ 2.10GHz": 90
        },
      }
    }

  translates to:

    INSERT INTO metadata (report_id, ts, entity, attr, value, total)
      VALUES ($report_id, $ts, "osd", "cpu", "Intel(R) Xeon(R) CPU E5-2620 v4 @ 2.10GHz", 90);

  example query - number of different ceph versions per cluster:

    SELECT COUNT(DISTINCT(value)) FROM metadata WHERE report_id=$report_id AND attr='ceph_version';
*/

CREATE TABLE grafana.metadata (
    report_id       INTEGER REFERENCES public.report(id) ON DELETE CASCADE,
    ts              TIMESTAMP,
    entity          VARCHAR(16),
    attr            VARCHAR(32),
    value           VARCHAR(128),
    total           INTEGER
);

CREATE TABLE grafana.rbd_pool (
    report_id       INTEGER REFERENCES public.report(id) ON DELETE CASCADE,
    ts              TIMESTAMP,
    pool_idx        INTEGER, /* needs to derive this from the element position in the array */
    num_images      INTEGER,
    mirroring       BOOLEAN
);

CREATE TABLE grafana.pool (
    report_id                INTEGER REFERENCES public.report(id) ON DELETE CASCADE,
    ts                       TIMESTAMP,
    pool_idx                 INTEGER,
    pgp_num                  INTEGER,
    pg_num                   INTEGER,
    size                     BIGINT,
    min_size                 BIGINT,
    cache_mode               VARCHAR(32),
    target_max_objects       BIGINT,
    target_max_bytes         BIGINT,
    pg_autoscale_mode        VARCHAR(32),
    type                     VARCHAR(32),

    /* erasure_code_profile */
    ec_k                    SMALLINT,
    ec_m                    SMALLINT,
    ec_crush_failure_domain VARCHAR(32),
    ec_plugin               VARCHAR(32),
    ec_technique            VARCHAR(32)
);

-- maps major versions ("14", "15") to their name ("Nautilus", "Octopus").
-- 'version' is of type VARCHAR because there is version "Dev".
CREATE TABLE grafana.version_to_name (
    version     VARCHAR(32),
    name        VARCHAR(32)
);

INSERT INTO grafana.version_to_name(version, name)
    VALUES
        ('12', 'Luminous'),
        ('13', 'Mimic'),
        ('14', 'Nautilus'),
        ('15', 'Octopus'),
        ('16', 'Pacific'),
        ('Dev', NULL);

-- When grafana draws a graph with a resolution of a day, it expects the DB
-- query to return a data point per day. A cluster may report less frequently
-- than once a day, thus the DB query will return a spike on the day that
-- the cluster reports, and a drop on days it doesn't.
-- To normalize this, we create a materialized view that for each day holds
-- a list of all reports that occurred on the previous week - only the most
-- recent report for each cluster within that previous week.
-- We use a materialized view because it's much faster than a regular view.
-- We update the materialized view in import_clusters.py.
CREATE MATERIALIZED VIEW grafana.weekly_reports_sliding AS
    SELECT
        DISTINCT ON(daily_window, cluster_id)
        daily_window,
        report_id
        /*
        GENERATE_SERIES generates a table with a single column 'daily_window'
        which holds all the days (a row per day) between the first report
        and today. Day format is 'YYYY-MM-DD 00:00:00'.
        */
    FROM
        grafana.ts_cluster c,
        GENERATE_SERIES('2019-03-01', now()::date, interval '1' day) daily_window
    WHERE
        c.ts BETWEEN daily_window - interval '7' day AND daily_window + interval '1' day
    ORDER BY
        daily_window,
        cluster_id,
        c.ts DESC;

ALTER MATERIALIZED VIEW grafana.weekly_reports_sliding OWNER TO grafana;
