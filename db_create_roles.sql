--CREATE USER grafana WITH PASSWORD '<PASSWORD>';
--CREATE USER grafana_ro WITH PASSWORD '<PASSWORD>';
GRANT USAGE ON SCHEMA grafana TO grafana, grafana_ro;
GRANT ALL ON ALL TABLES IN SCHEMA grafana TO grafana; -- "ALL TABLES" includes views
GRANT ALL ON ALL SEQUENCES IN SCHEMA grafana TO grafana;
GRANT SELECT ON TABLE public.report, public.device_report, public.crash TO grafana;

-- Grant access to grafana on future tables & seq in schema grafana
ALTER DEFAULT PRIVILEGES
    FOR ROLE postgres
    IN SCHEMA grafana
    GRANT ALL ON TABLES TO grafana;

ALTER DEFAULT PRIVILEGES
    FOR ROLE postgres
    IN SCHEMA grafana
    GRANT ALL ON SEQUENCES TO grafana;

GRANT SELECT ON ALL TABLES IN SCHEMA grafana TO grafana_ro;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA grafana TO grafana_ro;
GRANT SELECT ON TABLE public.report, public.device_report, public.crash TO grafana_ro;

ALTER DEFAULT PRIVILEGES
    FOR ROLE postgres
    IN SCHEMA grafana
    GRANT SELECT ON TABLES TO grafana_ro;

ALTER DEFAULT PRIVILEGES
    FOR ROLE postgres
    IN SCHEMA grafana
    GRANT SELECT ON SEQUENCES TO grafana_ro;

--CREATE USER dashboard WITH PASSWORD '<PASSWORD>' NOINHERIT;
CREATE SCHEMA IF NOT EXISTS dashboard;

GRANT USAGE ON SCHEMA dashboard TO dashboard, grafana, grafana_ro;
GRANT CREATE ON SCHEMA dashboard TO grafana;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA dashboard TO dashboard, grafana_ro;

ALTER DEFAULT PRIVILEGES
    FOR ROLE grafana
    IN SCHEMA dashboard
    GRANT EXECUTE ON FUNCTIONS TO dashboard;

GRANT ALL ON ALL TABLES IN SCHEMA dashboard TO grafana;

-- Revoking 'CREATE' permissions from
-- PUBLIC read-only users on 'public' schema:
GRANT CONNECT ON DATABASE telemetry TO postgres, telemetry, grafana, grafana_ro, dashboard;
GRANT CONNECT ON DATABASE postgres TO postgres, telemetry;
GRANT USAGE ON SCHEMA public TO postgres, telemetry, grafana, grafana_ro;
GRANT CREATE ON SCHEMA public TO postgres, telemetry;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public to postgres, telemetry;
GRANT USAGE ON LANGUAGE SQL to postgres, telemetry;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE CONNECT, TEMPORARY ON DATABASE telemetry FROM PUBLIC;
REVOKE CONNECT ON DATABASE postgres FROM PUBLIC;

-- dashboard_device schema roles:
CREATE SCHEMA IF NOT EXISTS dashboard_device;

GRANT USAGE ON SCHEMA dashboard_device TO dashboard, grafana, grafana_ro;
GRANT CREATE ON SCHEMA dashboard_device TO grafana;
GRANT ALL ON ALL TABLES IN SCHEMA dashboard_device TO grafana;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA dashboard_device TO dashboard, grafana_ro;

ALTER DEFAULT PRIVILEGES
    FOR ROLE grafana
    IN SCHEMA dashboard_device
    GRANT EXECUTE ON FUNCTIONS TO dashboard;
