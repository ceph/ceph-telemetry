/*
    Important:
    These functions must be created by
    'grafana' db user, so permissions are restricted.
    These functions ('stored-procedures')
    were created due to grafana's unrestricted
    access to data sources.

    The format of this file is the DDL of the function definition
    followed by a comment with the query that is used in Grafana.
*/


------- DASHBOARD: TELEMETRY -------

------- VARIABLE: major -------
CREATE FUNCTION dashboard.version_to_name(
    )
    RETURNS SETOF grafana.version_to_name
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT *
    FROM
        grafana.version_to_name
$$;

/*
-- Variable query:
-- Creates 'Major' dropdown values
SELECT
    REPLACE(CONCAT(version, '  --  ', name), 'Dev  --  ', 'Dev')
            AS __text,
    version AS __value
FROM
    dashboard.version_to_name();
*/

------- VARIABLE: minor -------
CREATE FUNCTION dashboard.minor_versions(
        major TEXT[]
    )
    RETURNS SETOF TEXT
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        DISTINCT(value)
    FROM
        grafana.metadata
    WHERE
            attr='ceph_version_norm'
        AND
            SPLIT_PART(value, '.', '1') = ANY (major);
$$;

/*
-- Variable query:
-- Creates 'Minor' dropdown values
SELECT *
FROM
    dashboard.minor_versions(ARRAY[$major]);
*/

------- PANEL OSD Count -------
CREATE FUNCTION dashboard.osd_count()
    RETURNS BIGINT
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        SUM(c.osd_count)
	FROM
        grafana.weekly_reports_sliding w
	INNER JOIN
        grafana.ts_cluster c
    ON
        w.report_id = c.report_id
	WHERE
        daily_window = (SELECT MAX(daily_window) FROM grafana.weekly_reports_sliding);
$$;

/*
-- Dashboard query:
SELECT dashboard.osd_count();
*/

------- PANEL Active Clusters -------
CREATE FUNCTION dashboard.active_clusters()
    RETURNS BIGINT
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        COUNT(*) total
    FROM
        grafana.weekly_reports_sliding w
    WHERE
        daily_window = (SELECT MAX(daily_window) FROM grafana.weekly_reports_sliding);
$$;

/*
-- Dashboard query:
SELECT dashboard.active_clusters();
*/

------- PANEL TOTAL CAPACITY -------
CREATE FUNCTION dashboard.total_capacity()
    RETURNS NUMERIC
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        SUM(c.TOTAL_BYTES) total
    FROM
        grafana.weekly_reports_sliding w
    INNER JOIN
        grafana.ts_cluster c
    ON
        w.report_id = c.report_id
    WHERE
        daily_window = (SELECT MAX(daily_window) FROM grafana.weekly_reports_sliding);
$$;

/*
-- Dashboard query:
SELECT dashboard.total_capacity();
*/

------- PANEL Version by Cluster Count -------
CREATE FUNCTION dashboard.version_by_cluster_count(
        time_from TIMESTAMP WITH TIME ZONE,
        time_to TIMESTAMP WITH TIME ZONE,
        display TEXT, -- 'Major' / 'Minor'
        major TEXT[],
        minor TEXT[],
        daemons TEXT[]
    )
    RETURNS TABLE (
		daily_window TIMESTAMP WITH TIME ZONE,
		version TEXT,
        total DOUBLE PRECISION
    )
LANGUAGE SQL SECURITY DEFINER
AS $$
    WITH
    -- Get the matching metadata stats, per report
    weekly_metadata AS (
        SELECT
            daily_window,
            grafana.metadata.*
        FROM
            grafana.metadata
        INNER JOIN
            grafana.WEEKLY_REPORTS_SLIDING w
        ON
            w.report_id = grafana.metadata.report_id
        WHERE
            attr='ceph_version_norm'
    ),
    -- This retrieves total daemons per report (which is of a cluster, per week)
    weekly_cluster_daemons AS (
        SELECT
            report_id,
            SUM(total) sum_daemons
        FROM
            grafana.metadata
        WHERE
                attr='ceph_version_norm'
            AND
                entity = ANY (daemons)
        GROUP BY
            report_id
    )
    SELECT
        daily_window,
          -- This is basically wm.value
          CASE WHEN display = 'Major'
               THEN SPLIT_PART(value, '.', 1)
               ELSE value
          END AS metric,
          SUM(wm.total / CAST(cd.sum_daemons AS REAL))
    FROM
        weekly_metadata wm
    INNER JOIN
        weekly_cluster_daemons cd
    ON
        wm.report_id = cd.report_id
    WHERE
        daily_window BETWEEN time_from AND time_to
        AND
            -- Include only values in retrieved Major / Minor array
            CASE WHEN display='Major'
                 THEN SPLIT_PART(value, '.', 1)
                 ELSE value
            END
            = ANY (
                    CASE WHEN display = 'Major'
                         THEN major
                         ELSE minor
                    END
                  )
        AND
            entity = ANY (daemons)
    GROUP by
        daily_window, 2
    ORDER BY
        2, daily_window;
$$;

/*
-- Dashboard query:
SELECT
    $__timeGroup(daily_window, '1d', 0),
    version AS metric,
    total
FROM
	dashboard.version_by_cluster_count(
        $__timeFrom(),
        $__timeTo(),
        -- display holds 'Major' / 'Minor';
        -- Grafana adds single quotes only to array elements, and '$display' is a string,
        -- so we add it manually here.
        '$display',
        ARRAY[$major],
        ARRAY[$minor],
        ARRAY[$daemons]
    );
*/

------- PANEL Version by Daemon Count -------
CREATE FUNCTION dashboard.version_by_daemon_count(
        time_from TIMESTAMP WITH TIME ZONE,
        time_to TIMESTAMP WITH TIME ZONE,
        display TEXT,
        major TEXT[],
        minor TEXT[],
        daemons TEXT[]
    )
    RETURNS TABLE (
		daily_window TIMESTAMP WITH TIME ZONE,
		version TEXT,
        total BIGINT
    )
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        w.daily_window,
        -- Do not change selection order, we group-by it.
        -- value is metadata.value that holds the version
        CASE
            WHEN display = 'Major'
            THEN SPLIT_PART(value, '.', 1)
            ELSE value
        END,
        SUM(total)
    FROM
        grafana.weekly_reports_sliding w
    INNER JOIN
        grafana.metadata
    ON
        w.report_id = grafana.metadata.report_id
    WHERE
            attr='ceph_version_norm'
        AND
            w.daily_window BETWEEN time_from AND time_to
        -- and value in ($major/$minor):
        AND
            CASE WHEN display = 'Major'
                 THEN SPLIT_PART(value, '.', 1)
                 ELSE value
            END
            = ANY (
                    CASE WHEN display = 'Major'
                         THEN major
                         ELSE minor
                    END
                  )
        AND
            entity = ANY (daemons)
    GROUP BY
        w.daily_window, 2
    ORDER BY
        2, w.daily_window;
$$;

/*
-- Dashboard query:
SELECT
    $__timeGroup(daily_window, '1d', 0),
    version AS metric,
    total
FROM
	dashboard.version_by_daemon_count(
        $__timeFrom(),
        $__timeTo(),
        '$display', -- Holds 'Major' / 'Minor'
        ARRAY[$major],
        ARRAY[$minor],
        ARRAY[$daemons]
    );
*/

------- PANEL Active Clusters by a Daily Sliding Window of a Week -------
CREATE FUNCTION dashboard.active_clusters_dsw(
        time_from TIMESTAMP WITH TIME ZONE,
        time_to TIMESTAMP WITH TIME ZONE
    )
    RETURNS TABLE (
		daily_window TIMESTAMP WITH TIME ZONE,
		active_clusters BIGINT
    )
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        daily_window AS time,
        COUNT(*) AS "active_clusters"
    FROM
        grafana.weekly_reports_sliding w
    INNER JOIN
        grafana.ts_cluster c
    ON
        w.report_id = c.report_id
    WHERE
        daily_window BETWEEN time_from AND time_to
    GROUP BY
        daily_window
    ORDER BY
        daily_window;
$$;

/*
-- Dashboard query:
SELECT
	daily_window AS time,
	active_clusters AS "Active Clusters"
FROM
	dashboard.active_clusters_dsw($__timeFrom(), $__timeTo());
*/

------- PANEL OSD Count by a Daily Sliding Window of a Week -------
CREATE FUNCTION dashboard.osd_count_dsw(
        time_from TIMESTAMP WITH TIME ZONE,
        time_to TIMESTAMP WITH TIME ZONE
    )
    RETURNS TABLE (
		daily_window TIMESTAMP WITH TIME ZONE,
		sum_osd_count BIGINT
    )
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        daily_window AS time,
        SUM(c.osd_count) AS "sum_osd_count"
    FROM
        grafana.weekly_reports_sliding w
    INNER JOIN
        grafana.ts_cluster c
    ON
        w.report_id = c.report_id
    WHERE
        daily_window BETWEEN time_from AND time_to
    GROUP BY
        daily_window
    ORDER BY
        daily_window;
$$;

/*
-- Dashboard query:
SELECT
	daily_window AS time,
	sum_osd_count AS "OSD Count"
FROM
	dashboard.osd_count_dsw($__timeFrom(), $__timeTo());
*/

------- PANEL Total Capacity by a Daily Sliding Window of a Week -------
CREATE FUNCTION dashboard.total_capacity_dsw(
        time_from TIMESTAMP WITH TIME ZONE,
        time_to TIMESTAMP WITH TIME ZONE
    )
    RETURNS TABLE (
		daily_window TIMESTAMP WITH TIME ZONE,
		Total NUMERIC,
		Used NUMERIC
    )
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        daily_window AS time,
        SUM(c.total_bytes) AS "Total",
        SUM(c.total_used_bytes) AS "Used"
    FROM
        grafana.weekly_reports_sliding w
    INNER JOIN
        grafana.ts_cluster c
    ON
        w.report_id = c.report_id
    WHERE
        daily_window BETWEEN time_from AND time_to
    GROUP BY
        daily_window
    ORDER BY
        daily_window;
$$;

/*
-- Dashboard query:
SELECT
	daily_window AS time,
    Total AS "Total",
    Used AS "Used"
FROM
	dashboard.total_capacity_dsw($__timeFrom(), $__timeTo());
*/

------- PANEL Cluster Distribution by Total Capacity -------
CREATE FUNCTION dashboard.cluster_distribution_by_total_capacity(
        time_from TIMESTAMP WITH TIME ZONE,
        time_to TIMESTAMP WITH TIME ZONE
    )
    RETURNS TABLE (
		daily_window TIMESTAMP WITH TIME ZONE,
		total_gb BIGINT
    )
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        w.daily_window as time,
        c.total_bytes / 1024 / 1024 / 1024 gb
    FROM
        grafana.weekly_reports_sliding w
    INNER JOIN
        grafana.ts_cluster c
    ON
        w.report_id = c.report_id
    WHERE
        w.daily_window BETWEEN time_from AND time_to
    ORDER BY
        w.daily_window;
$$;

/*
-- Dashboard query:
SELECT
	daily_window AS time,
	total_gb
FROM
	dashboard.cluster_distribution_by_total_capacity($__timeFrom(), $__timeTo());
*/

------- PANEL Cluster Distribution by Total Used Capacity -------
CREATE FUNCTION dashboard.cluster_distribution_by_total_used_capacity(
        time_from TIMESTAMP WITH TIME ZONE,
        time_to TIMESTAMP WITH TIME ZONE
    )
    RETURNS TABLE (
		daily_window TIMESTAMP WITH TIME ZONE,
		total_used_gb BIGINT
    )
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        w.daily_window as time,
        c.total_used_bytes / 1024 / 1024 / 1024 gb
    FROM
        grafana.weekly_reports_sliding w
    INNER JOIN
        grafana.ts_cluster c
    ON
        w.report_id = c.report_id
    WHERE
        w.daily_window BETWEEN time_from AND time_to
    ORDER BY
        w.daily_window;
$$;

/*
-- Dashboard query:
SELECT
	daily_window AS time,
	total_used_gb
FROM
	dashboard.cluster_distribution_by_total_used_capacity($__timeFrom(), $__timeTo());
*/

------- PANEL Cluster Distribution by OSD Count -------
CREATE FUNCTION dashboard.cluster_distribution_by_osd_count(
        time_from TIMESTAMP WITH TIME ZONE,
        time_to TIMESTAMP WITH TIME ZONE
    )
	RETURNS TABLE (
		daily_window TIMESTAMP WITH TIME ZONE,
		osd_count INT
	)
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        w.daily_window,
        c.osd_count
    FROM
        grafana.weekly_reports_sliding w
    INNER JOIN
        grafana.ts_cluster c
    ON
        w.report_id = c.report_id
    WHERE
        w.daily_window BETWEEN time_from AND time_to
    ORDER BY
        w.daily_window;
$$;

/*
-- Dashboard query:
SELECT
	daily_window AS time,
	osd_count
FROM
	dashboard.cluster_distribution_by_osd_count($__timeFrom(), $__timeTo());
*/


------- DASHBOARD: CAPACITY DENSITY -------

------- PANEL Cluster / TiB - Capacity Percentiles by a Daily Sliding Window of a Week -------
CREATE FUNCTION dashboard.cluster_tib_capacity_percentiles_dsw(
        time_from TIMESTAMP WITH TIME ZONE,
        time_to TIMESTAMP WITH TIME ZONE
    )
    RETURNS TABLE (
        daily_window TIMESTAMP WITH TIME ZONE,
        p25 BIGINT,
        p50 BIGINT,
        p75 BIGINT,
        p100 BIGINT
    )
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        daily_window,
        PERCENTILE_DISC(0.25) WITHIN GROUP (ORDER BY c.total_bytes) AS p25,
        PERCENTILE_DISC(0.50) WITHIN GROUP (ORDER BY c.total_bytes) AS p50,
        PERCENTILE_DISC(0.75) WITHIN GROUP (ORDER BY c.total_bytes) AS p75,
        PERCENTILE_DISC(1)    WITHIN GROUP (ORDER BY c.total_bytes) AS p100
    FROM
        grafana.weekly_reports_sliding w
    INNER JOIN
        grafana.ts_cluster c
    ON
        w.report_id = c.report_id
    WHERE
        daily_window BETWEEN time_from AND time_to
    GROUP BY
        daily_window
    ORDER BY
        daily_window;
$$;

/*
-- Dashboard query:
SELECT
    $__timeGroup(daily_window, '1d', 0),
    p25,
    p50,
    p75,
    p100
FROM
    dashboard.cluster_tib_capacity_percentiles_dsw($__timeFrom()::DATE - INTERVAL '1' DAY, $__timeTo());

-- Grafana expects consecutive data points, and since we have daily data points
-- (single point per day), it sometimes shows the oldest point of the graph
-- with zeros, rather than true values. We fix this by fetching results for one
-- day before the actual selected time range (by casting to date and
-- subtracting 1 day).
*/

------- PANEL Cluster / TiB < 1 PiB - Capacity Percentiles by a Daily Sliding Window of a Week -------
CREATE FUNCTION dashboard.cluster_tib_lt_1_pib_capacity_percentiles_dsw(
        time_from TIMESTAMP WITH TIME ZONE,
        time_to TIMESTAMP WITH TIME ZONE
    )
    RETURNS TABLE (
        daily_window TIMESTAMP WITH TIME ZONE,
        p25 BIGINT,
        p50 BIGINT,
        p75 BIGINT,
        p100 BIGINT
    )
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        daily_window,
        PERCENTILE_DISC(0.25) WITHIN GROUP (ORDER BY c.total_bytes) AS p25,
        PERCENTILE_DISC(0.50) WITHIN GROUP (ORDER BY c.total_bytes) AS p50,
        PERCENTILE_DISC(0.75) WITHIN GROUP (ORDER BY c.total_bytes) AS p75,
        PERCENTILE_DISC(1)    WITHIN GROUP (ORDER BY c.total_bytes) AS p100
    FROM
        grafana.weekly_reports_sliding w
    INNER JOIN
        grafana.ts_cluster c
    ON
        w.report_id = c.report_id
    WHERE
            c.total_bytes / 1024 / 1024 / 1024 / 1024 / 1024::REAL < 1
        AND
            daily_window BETWEEN time_from AND time_to
    GROUP BY
        daily_window
    ORDER BY
        daily_window;
$$;

/*
-- Dashboard query:
SELECT
    $__timeGroup(daily_window, '1d', 0),
    p25,
    p50,
    p75,
    p100
FROM
    dashboard.cluster_tib_lt_1_pib_capacity_percentiles_dsw($__timeFrom()::DATE - INTERVAL '1' DAY, $__timeTo());
*/

------- PANEL Cluster / TiB > 1 PiB - Capacity Percentiles by a Daily Sliding Window of a Week -------
CREATE FUNCTION dashboard.cluster_tib_gt_1_pib_capacity_percentiles_dsw(
        time_from TIMESTAMP WITH TIME ZONE,
        time_to TIMESTAMP WITH TIME ZONE
    )
    RETURNS TABLE (
        daily_window TIMESTAMP WITH TIME ZONE,
        p25 BIGINT,
        p50 BIGINT,
        p75 BIGINT,
        p100 BIGINT
    )
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        daily_window,
        PERCENTILE_DISC(0.25) WITHIN GROUP (ORDER BY c.total_bytes) AS p25,
        PERCENTILE_DISC(0.50) WITHIN GROUP (ORDER BY c.total_bytes) AS p50,
        PERCENTILE_DISC(0.75) WITHIN GROUP (ORDER BY c.total_bytes) AS p75,
        PERCENTILE_DISC(1)    WITHIN GROUP (ORDER BY c.total_bytes) AS p100
    FROM
        grafana.weekly_reports_sliding w
    INNER JOIN
        grafana.ts_cluster c
    ON
        w.report_id = c.report_id
    WHERE
            c.total_bytes / 1024 / 1024 / 1024 / 1024 / 1024::REAL > 1
        AND
            daily_window BETWEEN time_from AND time_to
    GROUP BY
        daily_window
    ORDER BY
        daily_window;
$$;

/*
-- Dashboard query:
SELECT
    $__timeGroup(daily_window, '1d', 0),
    p25,
    p50,
    p75,
    p100
FROM
    dashboard.cluster_tib_gt_1_pib_capacity_percentiles_dsw($__timeFrom()::DATE - INTERVAL '1' DAY, $__timeTo());
*/

------- PANEL OSD / Host - Capacity per Hosts Percentiles by a Daily Sliding Window of a Week -------
CREATE FUNCTION dashboard.osd_host_capacity_per_hosts_percentiles_dsw(
        time_from TIMESTAMP WITH TIME ZONE,
        time_to TIMESTAMP WITH TIME ZONE
    )
    RETURNS TABLE (
        daily_window TIMESTAMP WITH TIME ZONE,
        p25  DOUBLE PRECISION,
        p50  DOUBLE PRECISION,
        p75  DOUBLE PRECISION,
        p100 DOUBLE PRECISION
    )
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        daily_window,
        PERCENTILE_DISC(0.25) WITHIN GROUP (ORDER BY c.osd_count / c.hosts_num::REAL) AS p25,
        PERCENTILE_DISC(0.50) WITHIN GROUP (ORDER BY c.osd_count / c.hosts_num::REAL) AS p50,
        PERCENTILE_DISC(0.75) WITHIN GROUP (ORDER BY c.osd_count / c.hosts_num::REAL) AS p75,
        PERCENTILE_DISC(1)    WITHIN GROUP (ORDER BY c.osd_count / c.hosts_num::REAL) AS p100
    FROM
        grafana.weekly_reports_sliding w
    INNER JOIN
        grafana.ts_cluster c
    ON
        w.report_id = c.report_id
    WHERE
        daily_window BETWEEN time_from AND time_to
    GROUP BY
        daily_window
    ORDER BY
        daily_window;
$$;

/*
-- Dashboard query:
SELECT
    $__timeGroup(daily_window, '1d', 0),
    p25,
    p50,
    p75,
    p100
FROM
    dashboard.osd_host_capacity_per_hosts_percentiles_dsw($__timeFrom()::DATE - INTERVAL '1' DAY, $__timeTo());
*/

------- PANEL OSD / Host < 1 PiB - Capacity per Hosts Percentiles by a Daily Sliding Window of a Week -------
CREATE FUNCTION dashboard.osd_host_lt_1_pib_capacity_per_hosts_percentiles_dsw(
        time_from TIMESTAMP WITH TIME ZONE,
        time_to TIMESTAMP WITH TIME ZONE
    )
    RETURNS TABLE (
        daily_window TIMESTAMP WITH TIME ZONE,
        p25  DOUBLE PRECISION,
        p50  DOUBLE PRECISION,
        p75  DOUBLE PRECISION,
        p100 DOUBLE PRECISION
    )
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        daily_window,
        PERCENTILE_DISC(0.25) WITHIN GROUP (ORDER BY c.osd_count / c.hosts_num::REAL) AS p25,
        PERCENTILE_DISC(0.50) WITHIN GROUP (ORDER BY c.osd_count / c.hosts_num::REAL) AS p50,
        PERCENTILE_DISC(0.75) WITHIN GROUP (ORDER BY c.osd_count / c.hosts_num::REAL) AS p75,
        PERCENTILE_DISC(1)    WITHIN GROUP (ORDER BY c.osd_count / c.hosts_num::REAL) AS p100
    FROM
        grafana.weekly_reports_sliding w
    INNER JOIN
        grafana.ts_cluster c
    ON
        w.report_id = c.report_id
    WHERE
            c.total_bytes / 1024 / 1024 / 1024 / 1024 / 1024::REAL < 1
        AND
            daily_window BETWEEN time_from AND time_to
    GROUP BY
        daily_window
    ORDER BY
        daily_window;
$$;

/*
-- Dashboard query:
SELECT
    $__timeGroup(daily_window, '1d', 0),
    p25,
    p50,
    p75,
    p100
FROM
    dashboard.osd_host_lt_1_pib_capacity_per_hosts_percentiles_dsw($__timeFrom()::DATE - INTERVAL '1' DAY, $__timeTo());
*/

------- PANEL OSD / Host > 1 PiB - Capacity per Hosts Percentiles by a Daily Sliding Window of a Week -------
CREATE FUNCTION dashboard.osd_host_gt_1_pib_capacity_per_hosts_percentiles_dsw(
        time_from TIMESTAMP WITH TIME ZONE,
        time_to TIMESTAMP WITH TIME ZONE
    )
    RETURNS TABLE (
        daily_window TIMESTAMP WITH TIME ZONE,
        p25  DOUBLE PRECISION,
        p50  DOUBLE PRECISION,
        p75  DOUBLE PRECISION,
        p100 DOUBLE PRECISION
    )
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        daily_window,
        PERCENTILE_DISC(0.25) WITHIN GROUP (ORDER BY c.osd_count / c.hosts_num::REAL) AS p25,
        PERCENTILE_DISC(0.50) WITHIN GROUP (ORDER BY c.osd_count / c.hosts_num::REAL) AS p50,
        PERCENTILE_DISC(0.75) WITHIN GROUP (ORDER BY c.osd_count / c.hosts_num::REAL) AS p75,
        PERCENTILE_DISC(1)    WITHIN GROUP (ORDER BY c.osd_count / c.hosts_num::REAL) AS p100
    FROM
        grafana.weekly_reports_sliding w
    INNER JOIN
        grafana.ts_cluster c
    ON
        w.report_id = c.report_id
    WHERE
            c.total_bytes / 1024 / 1024 / 1024 / 1024 / 1024::REAL > 1
        AND
            daily_window BETWEEN time_from AND time_to
    GROUP BY
        daily_window
    ORDER BY
        daily_window;
$$;

/*
-- Dashboard query:
SELECT
    $__timeGroup(daily_window, '1d', 0),
    p25,
    p50,
    p75,
    p100
FROM
    dashboard.osd_host_gt_1_pib_capacity_per_hosts_percentiles_dsw($__timeFrom()::DATE - INTERVAL '1' DAY, $__timeTo());
*/

------- PANEL TiB / OSD - Capacity per OSD Count Percentiles by a Daily Sliding Window of a Week -------
CREATE FUNCTION dashboard.tib_osd_capacity_per_osd_count_percentiles_dsw(
        time_from TIMESTAMP WITH TIME ZONE,
        time_to TIMESTAMP WITH TIME ZONE
    )
    RETURNS TABLE (
        daily_window TIMESTAMP WITH TIME ZONE,
        p25  DOUBLE PRECISION,
        p50  DOUBLE PRECISION,
        p75  DOUBLE PRECISION,
        p100 DOUBLE PRECISION
    )
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        daily_window,
        PERCENTILE_DISC(0.25) WITHIN GROUP (ORDER BY c.total_bytes / c.osd_count::REAL) AS p25,
        PERCENTILE_DISC(0.50) WITHIN GROUP (ORDER BY c.total_bytes / c.osd_count::REAL) AS p50,
        PERCENTILE_DISC(0.75) WITHIN GROUP (ORDER BY c.total_bytes / c.osd_count::REAL) AS p75,
        PERCENTILE_DISC(1)    WITHIN GROUP (ORDER BY c.total_bytes / c.osd_count::REAL) AS p100
    FROM
        grafana.weekly_reports_sliding w
    INNER JOIN
        grafana.ts_cluster c
    ON
        w.report_id = c.report_id
    WHERE
            c.osd_count > 0
        AND
            daily_window BETWEEN time_from AND time_to
    GROUP BY
        daily_window
    ORDER BY
        daily_window;
$$;

/*
-- Dashboard query:
SELECT
    $__timeGroup(daily_window, '1d', 0),
    p25,
    p50,
    p75,
    p100
FROM
    dashboard.tib_osd_capacity_per_osd_count_percentiles_dsw($__timeFrom()::DATE - INTERVAL '1' DAY, $__timeTo());
*/

------- PANEL TiB / OSD < 1 PiB - Capacity per OSD Count Percentiles by a Daily Sliding Window of a Week -------
CREATE FUNCTION dashboard.tib_osd_lt_1_pib_capacity_per_osd_count_percentiles_dsw(
        time_from TIMESTAMP WITH TIME ZONE,
        time_to TIMESTAMP WITH TIME ZONE
    )
    RETURNS TABLE (
        daily_window TIMESTAMP WITH TIME ZONE,
        p25  DOUBLE PRECISION,
        p50  DOUBLE PRECISION,
        p75  DOUBLE PRECISION,
        p100 DOUBLE PRECISION
    )
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        daily_window,
        PERCENTILE_DISC(0.25) WITHIN GROUP (ORDER BY c.total_bytes / c.osd_count::REAL) AS p25,
        PERCENTILE_DISC(0.50) WITHIN GROUP (ORDER BY c.total_bytes / c.osd_count::REAL) AS p50,
        PERCENTILE_DISC(0.75) WITHIN GROUP (ORDER BY c.total_bytes / c.osd_count::REAL) AS p75,
        PERCENTILE_DISC(1)    WITHIN GROUP (ORDER BY c.total_bytes / c.osd_count::REAL) AS p100
    FROM
        grafana.weekly_reports_sliding w
    INNER JOIN
        grafana.ts_cluster c
    ON
        w.report_id = c.report_id
    WHERE
            c.osd_count > 0
        AND
            c.total_bytes / 1024 / 1024 / 1024 / 1024 / 1024::REAL < 1
        AND
            daily_window BETWEEN time_from AND time_to
    GROUP BY
        daily_window
    ORDER BY
        daily_window;
$$;

/*
-- Dashboard query:
SELECT
    $__timeGroup(daily_window, '1d', 0),
    p25,
    p50,
    p75,
    p100
FROM
    dashboard.tib_osd_lt_1_pib_capacity_per_osd_count_percentiles_dsw($__timeFrom()::DATE - INTERVAL '1' DAY, $__timeTo());
*/

------- PANEL TiB / OSD > 1 PiB - Capacity per OSD Count Percentiles by a Daily Sliding Window of a Week -------
CREATE FUNCTION dashboard.tib_osd_gt_1_pib_capacity_per_osd_count_percentiles_dsw(
        time_from TIMESTAMP WITH TIME ZONE,
        time_to TIMESTAMP WITH TIME ZONE
    )
    RETURNS TABLE (
        daily_window TIMESTAMP WITH TIME ZONE,
        p25  DOUBLE PRECISION,
        p50  DOUBLE PRECISION,
        p75  DOUBLE PRECISION,
        p100 DOUBLE PRECISION
    )
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        daily_window,
        PERCENTILE_DISC(0.25) WITHIN GROUP (ORDER BY c.total_bytes / c.osd_count::REAL) AS p25,
        PERCENTILE_DISC(0.50) WITHIN GROUP (ORDER BY c.total_bytes / c.osd_count::REAL) AS p50,
        PERCENTILE_DISC(0.75) WITHIN GROUP (ORDER BY c.total_bytes / c.osd_count::REAL) AS p75,
        PERCENTILE_DISC(1)    WITHIN GROUP (ORDER BY c.total_bytes / c.osd_count::REAL) AS p100
    FROM
        grafana.weekly_reports_sliding w
    INNER JOIN
        grafana.ts_cluster c
    ON
        w.report_id = c.report_id
    WHERE
            c.osd_count > 0
        AND
            c.total_bytes / 1024 / 1024 / 1024 / 1024 / 1024::REAL > 1
        AND
            daily_window BETWEEN time_from AND time_to
    GROUP BY
        daily_window
    ORDER BY
        daily_window;
$$;

/*
-- Dashboard query:
SELECT
    $__timeGroup(daily_window, '1d', 0),
    p25,
    p50,
    p75,
    p100
FROM
    dashboard.tib_osd_gt_1_pib_capacity_per_osd_count_percentiles_dsw($__timeFrom()::DATE - INTERVAL '1' DAY, $__timeTo());
*/

------- PANEL TiB / Host - Capacity per Hosts Percentiles by a Daily Sliding Window of a Week -------
CREATE FUNCTION dashboard.tib_host_capacity_per_hosts_percentiles_dsw(
        time_from TIMESTAMP WITH TIME ZONE,
        time_to TIMESTAMP WITH TIME ZONE
    )
    RETURNS TABLE (
        daily_window TIMESTAMP WITH TIME ZONE,
        p25  DOUBLE PRECISION,
        p50  DOUBLE PRECISION,
        p75  DOUBLE PRECISION,
        p100 DOUBLE PRECISION
    )
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        daily_window,
        PERCENTILE_DISC(0.25) WITHIN GROUP (ORDER BY c.total_bytes / c.hosts_num::REAL) AS p25,
        PERCENTILE_DISC(0.50) WITHIN GROUP (ORDER BY c.total_bytes / c.hosts_num::REAL) AS p50,
        PERCENTILE_DISC(0.75) WITHIN GROUP (ORDER BY c.total_bytes / c.hosts_num::REAL) AS p75,
        PERCENTILE_DISC(1)    WITHIN GROUP (ORDER BY c.total_bytes / c.hosts_num::REAL) AS p100
    FROM
        grafana.weekly_reports_sliding w
    INNER JOIN
        grafana.ts_cluster c
    ON
        w.report_id = c.report_id
    WHERE
        daily_window BETWEEN time_from AND time_to
    GROUP BY
        daily_window
    ORDER BY
        daily_window;
$$;

/*
-- Dashboard query:
SELECT
    $__timeGroup(daily_window, '1d', 0),
    p25,
    p50,
    p75,
    p100
FROM
    dashboard.tib_host_capacity_per_hosts_percentiles_dsw($__timeFrom()::DATE - INTERVAL '1' DAY, $__timeTo());
*/

------- PANEL TiB / Host < 1 PiB - Capacity per Hosts Percentiles by a Daily Sliding Window of a Week -------
CREATE FUNCTION dashboard.tib_host_lt_1_pib_capacity_per_hosts_percentiles_dsw(
        time_from TIMESTAMP WITH TIME ZONE,
        time_to TIMESTAMP WITH TIME ZONE
    )
    RETURNS TABLE (
        daily_window TIMESTAMP WITH TIME ZONE,
        p25  DOUBLE PRECISION,
        p50  DOUBLE PRECISION,
        p75  DOUBLE PRECISION,
        p100 DOUBLE PRECISION
    )
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        daily_window,
        PERCENTILE_DISC(0.25) WITHIN GROUP (ORDER BY c.total_bytes / c.hosts_num::REAL) AS p25,
        PERCENTILE_DISC(0.50) WITHIN GROUP (ORDER BY c.total_bytes / c.hosts_num::REAL) AS p50,
        PERCENTILE_DISC(0.75) WITHIN GROUP (ORDER BY c.total_bytes / c.hosts_num::REAL) AS p75,
        PERCENTILE_DISC(1)    WITHIN GROUP (ORDER BY c.total_bytes / c.hosts_num::REAL) AS p100
    FROM
        grafana.weekly_reports_sliding w
    INNER JOIN
        grafana.ts_cluster c
    ON
        w.report_id = c.report_id
    WHERE
            c.total_bytes / 1024 / 1024 / 1024 / 1024 / 1024::REAL < 1
        AND
            daily_window BETWEEN time_from AND time_to
    GROUP BY
        daily_window
    ORDER BY
        daily_window;
$$;

/*
-- Dashboard query:
SELECT
    $__timeGroup(daily_window, '1d', 0),
    p25,
    p50,
    p75,
    p100
FROM
    dashboard.tib_host_lt_1_pib_capacity_per_hosts_percentiles_dsw($__timeFrom()::DATE - INTERVAL '1' DAY, $__timeTo());
*/

------- PANEL TiB / Host > 1 PiB - Capacity per Hosts Percentiles by a Daily Sliding Window of a Week -------
CREATE FUNCTION dashboard.tib_host_gt_1_pib_capacity_per_hosts_percentiles_dsw(
        time_from TIMESTAMP WITH TIME ZONE,
        time_to TIMESTAMP WITH TIME ZONE
    )
    RETURNS TABLE (
        daily_window TIMESTAMP WITH TIME ZONE,
        p25  DOUBLE PRECISION,
        p50  DOUBLE PRECISION,
        p75  DOUBLE PRECISION,
        p100 DOUBLE PRECISION
    )
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        daily_window,
        PERCENTILE_DISC(0.25) WITHIN GROUP (ORDER BY c.total_bytes / c.hosts_num::REAL) AS p25,
        PERCENTILE_DISC(0.50) WITHIN GROUP (ORDER BY c.total_bytes / c.hosts_num::REAL) AS p50,
        PERCENTILE_DISC(0.75) WITHIN GROUP (ORDER BY c.total_bytes / c.hosts_num::REAL) AS p75,
        PERCENTILE_DISC(1)    WITHIN GROUP (ORDER BY c.total_bytes / c.hosts_num::REAL) AS p100
    FROM
        grafana.weekly_reports_sliding w
    INNER JOIN
        grafana.ts_cluster c
    ON
        w.report_id = c.report_id
    WHERE
            c.total_bytes / 1024 / 1024 / 1024 / 1024 / 1024::REAL > 1
        AND
            daily_window BETWEEN time_from AND time_to
    GROUP BY
        daily_window
    ORDER BY
        daily_window;
$$;

/*
-- Dashboard query:
SELECT
    $__timeGroup(daily_window, '1d', 0),
    p25,
    p50,
    p75,
    p100
FROM
    dashboard.tib_host_gt_1_pib_capacity_per_hosts_percentiles_dsw($__timeFrom()::DATE - INTERVAL '1' DAY, $__timeTo());
*/


------- DASHBOARD: X-ray -------

------- PANEL Cluster ID -------
CREATE FUNCTION dashboard.get_cluster_id(uuid TEXT)
    RETURNS TEXT
LANGUAGE plpgsql SECURITY DEFINER
AS $BODY$
DECLARE
    cluster_id_res TEXT := NULL;
BEGIN
	IF uuid IS NULL OR uuid = ''
	THEN
	    RETURN
            '<span>Please enter your cluster ID.'
            '<br><br>You can find it with:&nbsp;&nbsp;''ceph telemetry show | grep report_id''</span>';
	ELSE
		SELECT cluster_id INTO cluster_id_res
			FROM grafana.ts_cluster
			WHERE cluster_id = uuid
			LIMIT 1;
		IF cluster_id_res IS NOT NULL
		THEN
			RETURN uuid;
		ELSE
	        RETURN 'Invalid cluster ID.';
		END IF;
	END IF;
END;
$BODY$;

/*
-- Dashboard query:
SELECT dashboard.get_cluster_id('$c_id');
*/

------- PANEL - General query for x-ray panel -------
-- This query was generalized to work
-- with all of cluster x-ray panels.
-- It's okay because 'uuid' cannot be guessed.
CREATE FUNCTION dashboard.get_ts_cluster(
        time_from TIMESTAMP WITH TIME ZONE,
        time_to TIMESTAMP WITH TIME ZONE,
        uuid TEXT
)
    RETURNS SETOF grafana.ts_cluster
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT *
    FROM
        grafana.ts_cluster c
    WHERE
            c.cluster_id = uuid
        AND
            c.ts BETWEEN time_from AND time_to
$$;

/*
-- Dashboard query:
SELECT *
FROM
    dashboard.get_ts_cluster($__timeFrom(), $__timeTo(), '$c_id');
*/
