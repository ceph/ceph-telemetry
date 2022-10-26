/*
Important:
These functions must be created by 'grafana' db user, so permissions are
restricted.

These functions ('stored-procedures') were created due to Grafana's
unrestricted access to data sources.

The structure of this file is the DDL of the function's definition,
followed by a comment with the query that is used in Grafana.

In order to prevent ambiguity in variable substitution in the functions
below, always prefix variable names with 'v_', assuming there is no column
mentioned in the function which also starts with 'v_'.

While debugging pay attention that Postgres supports function overloading and
the function that is called may not be the one you expected.
*/

------- DASHBOARD: Devices / Main -------

------- VARIABLE: class -------
CREATE OR REPLACE FUNCTION dashboard_device.get_class(
    )
    RETURNS SETOF device.class
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        DISTINCT(class)
    FROM
        device.spec
    WHERE
        device.spec.class != 'unknown';
$$;

/*
-- Variable query:
-- Creates 'class' dropdown values
SELECT dashboard_device.get_class()
*/

------- VARIABLE: vendor -------
CREATE OR REPLACE FUNCTION dashboard_device.get_vendors(
        v_class TEXT[]
    )
    RETURNS SETOF TEXT
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        DISTINCT(vendor)
    FROM
        device.spec
    WHERE
        device.spec.class = ANY (v_class::device.class[]);
$$;

/*
-- Variable query:
-- Creates 'vendor' dropdown values
SELECT dashboard_device.get_vendors(ARRAY[$class]);
*/

------- VARIABLE: model -------
CREATE OR REPLACE FUNCTION dashboard_device.get_models(
        v_vendor TEXT[]
    )
    RETURNS SETOF TEXT
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        DISTINCT(model)
    FROM
        device.spec
    WHERE
        device.spec.vendor = ANY (v_vendor);
$$;

/*
-- Variable query:
-- Creates 'model' dropdown values
SELECT dashboard_device.get_models(ARRAY[$vendor]);
*/

------- PANEL Active Devices -------
CREATE OR REPLACE FUNCTION dashboard_device.active_devices(
        v_class TEXT[],
        v_vendor TEXT[],
        v_model TEXT[]
)
    RETURNS BIGINT
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        COUNT(*) total
    FROM
        device.weekly_reports_sliding w
        INNER JOIN device.device ON w.device_id = device.device.id
        INNER JOIN device.spec ON device.device.SPEC_ID = device.spec.id
    WHERE
        daily_window = (SELECT MAX(daily_window) FROM device.weekly_reports_sliding)
        AND device.spec.class = ANY (v_class::device.class[])
        AND device.spec.vendor = ANY (v_vendor)
        AND device.spec.model = ANY (v_model);
$$;

/*
-- Dashboard query:
SELECT dashboard_device.active_devices(ARRAY[$class], ARRAY[$vendor], ARRAY[$model]);
*/

------- PANEL With Valid Telemetry -------
CREATE OR REPLACE FUNCTION dashboard_device.with_valid_telemetry(
        v_class TEXT[],
        v_vendor TEXT[],
        v_model TEXT[]
)
    RETURNS BIGINT
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        COUNT(*) total
    FROM
        device.weekly_reports_sliding w
        INNER JOIN device.device ON w.device_id = device.device.id
        INNER JOIN device.spec ON device.device.SPEC_ID = device.spec.id
    WHERE
        daily_window = (SELECT MAX(daily_window) FROM device.weekly_reports_sliding)
        AND w.error IS NULL -- meaning there's data in the report
        AND device.spec.class = ANY (v_class::device.class[]) -- note the enum casting
        AND device.spec.vendor = ANY (v_vendor)
        AND device.spec.model = ANY (v_model);
$$;

/*
-- Dashboard query:
SELECT dashboard_device.with_valid_telemetry(ARRAY[$class], ARRAY[$vendor], ARRAY[$model]);
*/

------- PANEL Total Capacity -------
CREATE OR REPLACE FUNCTION dashboard_device.total_capacity(
        v_class TEXT[],
        v_vendor TEXT[],
        v_model TEXT[]
)
    RETURNS NUMERIC
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        SUM(capacity)
    FROM
        device.weekly_reports_sliding w
        INNER JOIN device.device ON w.device_id = device.device.id
        INNER JOIN device.spec ON device.device.SPEC_ID = device.spec.id
    WHERE
        daily_window = (SELECT MAX(daily_window) FROM device.weekly_reports_sliding)
        AND device.spec.class = ANY (v_class::device.class[])
        AND device.spec.vendor = ANY (v_vendor)
        AND device.spec.model = ANY (v_model);
$$;

/*
-- Dashboard query:
SELECT dashboard_device.total_capacity(ARRAY[$class], ARRAY[$vendor], ARRAY[$model]);
*/

------- PANEL Hosts - All-time -------
CREATE OR REPLACE FUNCTION dashboard_device.hosts_all_time(
        v_class TEXT[],
        v_vendor TEXT[]
)
    RETURNS BIGINT
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        COUNT(DISTINCT(host_id))
    FROM
        device.device d
    INNER JOIN device.spec s ON d.spec_id = s.id
    WHERE
        s.class = ANY (v_class::device.class[])
        AND s.vendor = ANY (v_vendor);
$$;

/*
-- Dashboard query:
SELECT dashboard_device.hosts_all_time(ARRAY[$class], ARRAY[$vendor]);
*/

------- PANEL Vendors - All-time -------
CREATE OR REPLACE FUNCTION dashboard_device.vendors_all_time(
        v_class TEXT[]
)
    RETURNS BIGINT
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        COUNT(DISTINCT(vendor))
    FROM
        device.spec
    WHERE
        device.spec.class = ANY (v_class::device.class[]);
$$;


/*
-- Dashboard query:
SELECT dashboard_device.vendors_all_time(ARRAY[$class]);
*/

------- PANEL Models - All-time -------
CREATE OR REPLACE FUNCTION dashboard_device.models_all_time(
        v_class TEXT[],
        v_vendor TEXT[]
)
    RETURNS BIGINT
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        COUNT(DISTINCT(model))
    FROM
        device.spec
    WHERE
        device.spec.class = ANY (v_class::device.class[])
        AND device.spec.vendor = ANY (v_vendor);
$$;

/*
-- Dashboard query:
SELECT dashboard_device.models_all_time(ARRAY[$class], ARRAY[$vendor]);
*/

------- PANEL Devices by Vendors -------
CREATE OR REPLACE FUNCTION dashboard_device.devices_by_vendors(
        time_from TIMESTAMP WITH TIME ZONE,
        time_to TIMESTAMP WITH TIME ZONE,
        v_class TEXT[],
        v_vendor TEXT[],
        v_model TEXT[]
)
    RETURNS TABLE (
        daily_window TIMESTAMP,
        vendor TEXT,
        total BIGINT
    )
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        w.daily_window,
        device.spec.vendor,
        COUNT(*)
    FROM
        device.weekly_reports_sliding w
        INNER JOIN device.device ON w.device_id = device.device.id
        INNER JOIN device.spec ON device.device.SPEC_ID = device.spec.id
    WHERE
        w.daily_window BETWEEN time_from AND time_to
      AND device.spec.class = ANY (v_class::device.class[])
      AND device.spec.vendor = ANY (v_vendor)
      AND device.spec.model = ANY (v_model)
    GROUP BY
        w.daily_window, device.spec.vendor
    ORDER BY
        2, w.daily_window;
$$;

/*
-- Dashboard query:
SELECT
    $__timeGroup(daily_window, '1d', 0),
    vendor as metric,
    total
FROM
    dashboard_device.devices_by_vendors(
        $__timeFrom()::timestamptz - interval '1 day',
        $__timeTo(),
        ARRAY[$class],
        ARRAY[$vendor],
        ARRAY[$model]
    );
*/

------- PANEL Active Devices (Graph) -------
CREATE OR REPLACE FUNCTION dashboard_device.active_devices_graph(
        time_from TIMESTAMP WITH TIME ZONE,
        time_to TIMESTAMP WITH TIME ZONE,
        v_class TEXT[],
        v_vendor TEXT[],
        v_model TEXT[]
)
    RETURNS TABLE (
        daily_window TIMESTAMP,
        total BIGINT,
        errors BIGINT,
        valids BIGINT
    )
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        daily_window,
        COUNT(*) total,
        COUNT(w.error) errors,
        COUNT(*) - COUNT(w.error) valids
    FROM
        device.weekly_reports_sliding w
        INNER JOIN device.device ON w.device_id = device.device.id
        INNER JOIN device.spec ON device.device.spec_id = device.spec.id
    WHERE
            daily_window BETWEEN time_from AND time_to
        AND device.spec.class = ANY (v_class::device.class[])
        AND device.spec.vendor = ANY (v_vendor)
        AND device.spec.model = ANY (v_model)
    GROUP BY
        daily_window
    ORDER BY
        daily_window
$$;

/*
-- Dashboard query:
SELECT
    $__timeGroup(daily_window, '1d', 0),
    total AS "Total",
    errors AS "Reporting Invalid Telemetry",
    valids AS "Reporting valid Telemetry"
FROM
    dashboard_device.active_devices_graph(
        $__timeFrom()::timestamptz - interval '1 day',
        $__timeTo(),
        ARRAY[$class],
        ARRAY[$vendor],
        ARRAY[$model]
    );
*/

------- PANEL Distinct Hosts - $vendor -------
CREATE OR REPLACE FUNCTION dashboard_device.distinct_hosts(
        time_from TIMESTAMP WITH TIME ZONE,
        time_to TIMESTAMP WITH TIME ZONE,
        v_class TEXT[],
        v_vendor TEXT[],
        v_model TEXT[]
)
    RETURNS TABLE (
        daily_window TIMESTAMP,
        total BIGINT
    )
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        w.daily_window,
        COUNT(DISTINCT(device.device.host_id))
    FROM
        device.weekly_reports_sliding w
        INNER JOIN device.device ON w.device_id = device.device.id
        INNER JOIN device.spec ON device.device.spec_id = device.spec.id
    WHERE
        w.daily_window BETWEEN time_from AND time_to
      AND device.spec.class = ANY (v_class::device.class[])
      AND device.spec.vendor = ANY (v_vendor)
      AND device.spec.model = ANY (v_model)
    GROUP BY
        w.daily_window
    ORDER BY
        w.daily_window;
$$;

/*
-- Dashboard query:
SELECT
    $__timeGroup(daily_window, '1d', 0),
    total AS "Distinct Hosts"
FROM
    dashboard_device.distinct_hosts(
        $__timeFrom()::timestamptz - interval '1 day',
        $__timeTo(),
        ARRAY[$class],
        ARRAY[$vendor],
        ARRAY[$model]
    );
*/

------- PANEL Distinct Models - $vendor -------
CREATE OR REPLACE FUNCTION dashboard_device.distinct_models(
        time_from TIMESTAMP WITH TIME ZONE,
        time_to TIMESTAMP WITH TIME ZONE,
        v_vendor TEXT[],
        v_model TEXT[]
    )
    RETURNS TABLE (
        daily_window TIMESTAMP,
        total BIGINT
    )
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        daily_window,
        COUNT(DISTINCT(device.spec.model))
    FROM
        device.weekly_reports_sliding w
        INNER JOIN device.device ON w.device_id = device.device.id
        INNER JOIN device.spec ON device.device.spec_id = device.spec.id
    WHERE
        w.daily_window BETWEEN time_from AND time_to
        AND device.spec.vendor = ANY (v_vendor)
        AND device.spec.model = ANY (v_model)
    GROUP BY
        w.daily_window
    ORDER BY
        w.daily_window;
$$;

/*
-- Dashboard query:
SELECT
    $__timeGroup(daily_window, '1d', 0),
    total AS "Distinct Models"
FROM
    dashboard_device.distinct_models(
        $__timeFrom()::timestamptz - interval '1 day',
        $__timeTo(),
        ARRAY[$vendor],
        ARRAY[$model]
    );
*/

------- PANEL Devices by Types - $vendor -------
CREATE OR REPLACE FUNCTION dashboard_device.devices_by_type(
        time_from TIMESTAMP WITH TIME ZONE,
        time_to TIMESTAMP WITH TIME ZONE,
        v_vendor TEXT[]
    )
    RETURNS TABLE (
        daily_window TIMESTAMP,
        total BIGINT,
        device_type TEXT
    )
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        w.daily_window,
        COUNT(*),
        device.spec.type::TEXT
    FROM
        device.weekly_reports_sliding w
        INNER JOIN device.device ON w.device_id = device.device.id
        INNER JOIN device.spec ON device.device.SPEC_ID = device.spec.id
    WHERE
        w.daily_window BETWEEN time_from AND time_to
        AND device.spec.type IS NOT NULL
        AND device.spec.vendor = ANY (v_vendor)
    GROUP BY
        w.daily_window, device.spec.type
    ORDER BY
        w.daily_window;
$$;

/*
-- Dashboard query:
SELECT
    $__timeGroup(daily_window, '1d', 0),
    total,
    device_type AS metric
FROM
    dashboard_device.devices_by_type(
        $__timeFrom()::timestamptz - interval '1 day',
        $__timeTo(),
        ARRAY[$vendor]
    );
*/

------- PANEL SSD Devices by Interface - $vendor -------
CREATE OR REPLACE FUNCTION dashboard_device.ssd_devices_by_interface(
        time_from TIMESTAMP WITH TIME ZONE,
        time_to TIMESTAMP WITH TIME ZONE,
        v_vendor TEXT[]
    )
    RETURNS TABLE (
        daily_window TIMESTAMP,
        interface TEXT,
        total BIGINT
    )
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        w.daily_window,
        device.spec.interface::TEXT, -- maybe text []?
        COUNT(*)
    FROM
        device.weekly_reports_sliding w
        INNER JOIN device.device ON w.device_id = device.device.id
        INNER JOIN device.spec ON device.device.SPEC_ID = device.spec.id
    WHERE
        w.daily_window BETWEEN time_from AND time_to
        AND device.spec.vendor = ANY (v_vendor)
        AND
            (device.spec.type::text = 'ssd'
            OR
            device.spec.type::text = 'nvme')
    GROUP BY
        w.daily_window, device.spec.interface
    ORDER BY
        2, w.daily_window;
$$;

/*
-- Dashboard query:
SELECT
    $__timeGroup(daily_window, '1d', 0),
    interface as metric,
    total
FROM
    dashboard_device.ssd_devices_by_interface(
        $__timeFrom()::timestamptz - interval '1 day',
        $__timeTo(),
        ARRAY[$vendor]
    );
*/

------- PANEL HDD Devices by Interface - $vendor -------
CREATE OR REPLACE FUNCTION dashboard_device.hdd_devices_by_interface(
        time_from TIMESTAMP WITH TIME ZONE,
        time_to TIMESTAMP WITH TIME ZONE,
        v_vendor TEXT[]
    )
    RETURNS TABLE (
        daily_window TIMESTAMP,
        interface TEXT,
        total BIGINT
    )
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        w.daily_window,
        device.spec.interface::TEXT,
        COUNT(*)
    FROM
        device.weekly_reports_sliding w
        INNER JOIN device.device ON w.device_id = device.device.id
        INNER JOIN device.spec ON device.device.SPEC_ID = device.spec.id
      WHERE
        w.daily_window BETWEEN time_from AND time_to
        AND device.spec.vendor = ANY (v_vendor)
        AND device.spec.type::text = 'hdd'
    GROUP BY
        w.daily_window, device.spec.interface
    ORDER BY
        w.daily_window;
$$;

/*
-- Dashboard query:
SELECT
    $__timeGroup(daily_window, '1d', 0),
    interface as metric,
    total
FROM
    dashboard_device.hdd_devices_by_interface(
        $__timeFrom()::timestamptz - interval '1 day',
        $__timeTo(),
        ARRAY[$vendor]
    );
*/

------- PANEL Total Capacity - $vendor -------
CREATE OR REPLACE FUNCTION dashboard_device.total_capacity_graph(
        time_from TIMESTAMP WITH TIME ZONE,
        time_to TIMESTAMP WITH TIME ZONE,
        v_class TEXT[],
        v_vendor TEXT[],
        v_model TEXT[]
    )
    RETURNS TABLE (
        daily_window TIMESTAMP,
        total NUMERIC
    )
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        w.daily_window,
        SUM(capacity)
    FROM
        device.weekly_reports_sliding w
        INNER JOIN device.device ON w.device_id = device.device.id
        INNER JOIN device.spec ON device.device.SPEC_ID = device.spec.id
    WHERE
        w.daily_window BETWEEN time_from AND time_to
        AND device.spec.class = ANY (v_class::device.class[])
        AND device.spec.vendor = ANY (v_vendor)
        AND device.spec.model = ANY (v_model)
    GROUP BY
        daily_window
    ORDER BY
        daily_window;
$$;

/*
-- Dashboard query:
SELECT
    $__timeGroup(daily_window, '1d', 0),
    total AS "Total Capacity"
FROM
    dashboard_device.total_capacity_graph(
        $__timeFrom()::timestamptz - interval '1 day',
        $__timeTo(),
        ARRAY[$class],
        ARRAY[$vendor],
        ARRAY[$model]
    );
*/

------- PANEL Devices by HW RAID -------
CREATE OR REPLACE FUNCTION dashboard_device.devices_by_hw_raid(
        time_from TIMESTAMP WITH TIME ZONE,
        time_to TIMESTAMP WITH TIME ZONE
    )
    RETURNS TABLE (
        daily_window TIMESTAMP,
        vendor TEXT,
        total BIGINT
    )
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        w.daily_window,
        device.spec.vendor,
        COUNT(*)
    FROM
        device.weekly_reports_sliding w
        INNER JOIN device.device ON w.device_id = device.device.id
        INNER JOIN device.spec ON device.device.SPEC_ID = device.spec.id
    WHERE
        w.daily_window BETWEEN time_from AND time_to
        AND device.spec.class = 'hw_raid'
    GROUP BY
        w.daily_window, device.spec.vendor
    ORDER BY
        2, w.daily_window;
$$;

/*
-- Dashboard query:
SELECT
    $__timeGroup(daily_window, '1d', 0),
    vendor as metric,
    total
FROM
    dashboard_device.devices_by_hw_raid(
        $__timeFrom()::timestamptz - interval '1 day',
        $__timeTo()
    );
*/

------- PANEL Devices by Class -------
CREATE OR REPLACE FUNCTION dashboard_device.devices_by_class(
        time_from TIMESTAMP WITH TIME ZONE,
        time_to TIMESTAMP WITH TIME ZONE
    )
    RETURNS TABLE (
        daily_window TIMESTAMP,
        total BIGINT,
        class device.class
    )
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        w.daily_window,
        COUNT(*),
        device.spec.class
    FROM
        device.weekly_reports_sliding w
        INNER JOIN device.device ON w.device_id = device.device.id
        INNER JOIN device.spec ON device.device.SPEC_ID = device.spec.id
    WHERE
        w.daily_window BETWEEN time_from AND time_to
        AND device.spec.class != 'unknown'
    GROUP BY
        w.daily_window, device.spec.class
    ORDER BY
        w.daily_window, device.spec.class;
$$;

/*
-- Dashboard query:
SELECT
    $__timeGroup(daily_window, '1d', 0),
    total,
    class AS metric
FROM
    dashboard_device.devices_by_class(
        $__timeFrom()::timestamptz - interval '1 day',
        $__timeTo()
    );
*/

------- PANEL Invalid Telemetry Reports by Device Class -------
CREATE OR REPLACE FUNCTION dashboard_device.invalid_reports_by_device_class(
        time_from TIMESTAMP WITH TIME ZONE,
        time_to TIMESTAMP WITH TIME ZONE,
        v_class TEXT[]
    )
    RETURNS TABLE (
        daily_window TIMESTAMP,
        total BIGINT,
        class device.class
    )
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        w.daily_window,
        COUNT(*),
        device.spec.class
    FROM
        device.weekly_reports_sliding w
        INNER JOIN device.device ON w.device_id = device.device.id
        INNER JOIN device.spec ON device.device.SPEC_ID = device.spec.id
    WHERE
        w.daily_window BETWEEN time_from AND time_to
        AND device.spec.class = ANY (v_class::device.class[])
        AND w.error IS NOT NULL
    GROUP BY
        w.daily_window, device.spec.class
    ORDER BY
        w.daily_window, device.spec.class;
$$;

/*
-- Dashboard query:
SELECT
    $__timeGroup(daily_window, '1d', 0),
    total AS "Empty Reports",
    class AS metric
FROM
    dashboard_device.invalid_reports_by_device_class(
        $__timeFrom()::timestamptz - interval '1 day',
        $__timeTo(),
        ARRAY[$class]
    );
*/


------- DASHBOARD: Devices / Distinct Model Count -------

/*
------- VARIABLE: class -------
Same as Devices / Main

------- VARIABLE: vendor -------
Same as Devices / Main
*/

------- PANEL Distinct Models Count per Vendor - All-time -------
CREATE OR REPLACE FUNCTION dashboard_device.models_count_per_vendor(
        v_vendor TEXT[]
    )
    RETURNS TABLE (
        vendor TEXT,
        models BIGINT,
        devices NUMERIC
    )
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        a.vendor vendor,
        COUNT(a.model) models,
        SUM(a.devices) devices
    FROM (
        SELECT
            device.spec.vendor,
            device.spec.model,
            device.spec.type,
            device.spec.interface,
            device.spec.class,
            COUNT(device.id) devices
        FROM
            device.spec
            INNER JOIN device.device ON device.device.spec_id = device.spec.id
        GROUP BY
            device.spec.id
        ORDER BY
            devices desc
        ) a
    WHERE
        a.vendor = ANY (v_vendor)
    GROUP BY
        a.vendor
    ORDER BY
        models DESC;
$$;

/*
-- Dashboard query:
SELECT
    vendor,
    models,
    devices
FROM
    dashboard_device.models_count_per_vendor(
        ARRAY[$vendor]
    );
*/


------- DASHBOARD: Devices / Models per Vendor -------

/*
------- VARIABLE: class -------
Same as Devices / Main

------- VARIABLE: vendor -------
Same as Devices / Main

------- VARIABLE: model -------
Same as Devices / Main
*/

------- PANEL Models per Vendor -------
CREATE OR REPLACE FUNCTION dashboard_device.models_per_vendor(
        v_class TEXT[],
        v_vendor TEXT[],
        v_model TEXT[]
    )
    RETURNS TABLE (
        vendor TEXT,
        model TEXT,
        type device.type, -- enum
        interface device.interface, -- enum
        class device.class, -- enum
        capacity BIGINT,
        devices BIGINT
    )
LANGUAGE SQL SECURITY DEFINER
AS $$
    SELECT
        device.spec.vendor vendor,
        device.spec.model model,
        device.spec.type,
        device.spec.interface,
        device.spec.class,
        device.spec.capacity,
        COUNT(device.id) devices
    FROM
        device.spec
        INNER JOIN device.device ON device.device.spec_id = device.spec.id
    WHERE
            device.spec.class = ANY (v_class::device.class[])
        AND device.spec.vendor = ANY (v_vendor)
        AND device.spec.model = ANY (v_model)
    GROUP BY
        device.spec.id
    ORDER BY
        vendor, model;
$$;

/*
-- Dashboard query:
SELECT
    vendor,
    model,
    type,
    interface,
    class,
    capacity,
    devices
FROM
    dashboard_device.models_per_vendor(
        ARRAY[$class],
        ARRAY[$vendor],
        ARRAY[$model]
    );
*/
