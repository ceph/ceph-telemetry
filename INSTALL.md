# Ceph Telemetry Installation

## Minimum requirements
- RHEL 8 based OS
- PostgreSQL version 10.0 and up. Tested up to 14.2
- Grafana open source 8.1 and up
- Apache HTTP Server 2.4 and up
- 16 GB RAM
- 4 cores processor for 2500 reporting clusters
- Disk space - On average 430 KB per cluster per day.

## Clone the Telemetry git repository
We will clone the repository into the user's home directory and will reference this path later in the installation
```bash
cd ~
git clone https://github.com/ceph/ceph-telemetry.git
```

## Install PostgreSQL and Grafana
You can install Postgres and Grafana from RPM or as containers. Below is how to install as containers

1. Create a directories for the grafana container persistent storage, and populate them. In this example we'll use the base dir /opt/telemetry_grafana.
```bash
sudo mkdir /opt/telemetry_grafana
sudo mkdir -p /opt/telemetry_grafana/var/lib/grafana
sudo chmod a+rwx /opt/telemetry_grafana/var/lib/grafana
sudo mkdir -p /opt/telemetry_grafana/etc/grafana/provisioning/dashboards
sudo mkdir -p /opt/telemetry_grafana/etc/grafana_dashboards

sudo cp ~/ceph-telemetry/install/grafana_dashboards_ini.yml /opt/telemetry_grafana/etc/grafana/provisioning/dashboards
sudo cp -a ~/ceph-telemetry/dashboard/private/* /opt/telemetry_grafana/etc/grafana_dashboards
sudo find /opt/telemetry_grafana/etc/grafana_dashboards/ -name "*.json" -exec sed -i "s/\${DS_POSTGRESQL}/PostgreSQL/g" {} \;
```

2. Edit the docker-compose template `install/docker-compose.yml`. Change the following:
   - <postgres_password> : Choose a password for the database "postgres" user, which is the PostgreSQL super user
   - <postgres_host_storage_path> : persistent storage for the database files
   - <telemetry_server_FQDN> : FQDN of the server on which Grafana is running
   - /opt/telemetry_grafana : persistent storage basedir for the Grafana database files
3. Run `cd install; docker-compose up -d`

### Provision database
#### Create roles, passwords, and data source names (DSN)
```bash
sudo mkdir -p /opt/telemetry # Stores database passwords and DSNs (connection strings)

# Create passwords for the various database users
uuidgen -r | sudo tee /opt/telemetry/pg_pass_telemetry
uuidgen -r | sudo tee /opt/telemetry/pg_pass_grafana
uuidgen -r | sudo tee /opt/telemetry/pg_pass_grafana_ro
uuidgen -r | sudo tee /opt/telemetry/pg_pass_dashboard
echo host=127.0.0.1 dbname=telemetry user=grafana password=$(cat /opt/telemetry/pg_pass_grafana) |sudo tee /opt/telemetry/grafana.dsn

```
Run in `psql`, replacing the $PG_PASS* with the corresponding passwords generated above
```SQL
CREATE USER telemetry WITH PASSWORD '$PG_PASS_TELEMETRY';
CREATE USER grafana WITH PASSWORD '$PG_PASS_GRAFANA';
CREATE USER grafana_ro WITH PASSWORD '$PG_PASS_GRAFANA_RO';
CREATE USER dashboard WITH PASSWORD '$PG_PASS_DASHBOARD' NOINHERIT;
CREATE DATABASE telemetry OWNER telemetry;
```

#### Import DDLs
```bash
cd ~/ceph-telemetry
psql -v ON_ERROR_STOP=1 -b -h 127.0.0.1 -U telemetry telemetry < tables.txt
psql -v ON_ERROR_STOP=1 -b -h 127.0.0.1 -U postgres telemetry < db_create_cluster.sql
psql -v ON_ERROR_STOP=1 -b -h 127.0.0.1 -U telemetry telemetry < db_create_device.sql
psql -v ON_ERROR_STOP=1 -b -h 127.0.0.1 -U postgres telemetry < db_create_roles.sql
psql -v ON_ERROR_STOP=1 -b -h 127.0.0.1 -U grafana telemetry < db_create_dashboard.sql
psql -v ON_ERROR_STOP=1 -b -h 127.0.0.1 -U grafana telemetry < db_create_dashboard_device.sql
```
### Configure Grafana
1. Login to Grafana via a browser (port 3000) with the default username 'admin' and password 'admin'.
2. Configure a data source of the postgres server
   1. Use `grafana_ro` as the database user, with the password that is saved in `/opt/telemetry/pg_pass_grafana_ro`
   2. You may need to use the host's IP address (not localhost)

## Install Apache HTTP server
1. run:
```bash
sudo dnf install -y httpd python3-mod_wsgi mod_ssl mod_evasive openssl python3-requests python3-flask python3-flask-restful python3-psycopg2 lz4
sudo cp ~/ceph-telemetry/install/telemetry-ssl.conf /etc/httpd/conf.d/
```
2. Generate web server certificates for the telemetry server's public FQDN. Below instructions are of how to generate self-signed certificates that should not be used in production
```bash
sudo mkdir -p /etc/telemetry/ssl
sudo openssl req -x509 -nodes -newkey rsa:2048 -keyout /etc/telemetry/ssl/telemetry.key -out /etc/telemetry/ssl/telemetry.crt
```
3. Edit `/etc/httpd/conf.d/telemetry-ssl.conf` and change the following to match your environment:
   - ServerName
   - SSLCertificateFile, SSLCertificateKeyFile
4. You may need to configure SELinux to allow httpd access to the telemetry wsgi using `semanage permissive -a httpd_t`
5. run:
```bash
sudo systemctl enable --now httpd
```

## Install the Telemetry server
run:
```bash
cd ~/ceph-telemetry
sudo cp -a server /opt/telemetry/
sudo cp import_crashes.py import_clusters.py import_devices.py compress_raw_reports_telemetry.sh dbhelper.py compress_raw_reports_telemetry.sh /opt/telemetry/
cd /opt/telemetry
sudo ln -s pg_pass_telemetry pg_pass.txt
sudo mkdir log
sudo chown apache log
sudo mkdir raw
sudo chmod a+rwx raw
```
#### Add Telemetry importers to cron
Create a "telemetry" user unix account and then run:
```bash
sudo crontab -u telemetry install/crontab_telemetry
```

# Backing up the Telemetry server
Backing up the server involves backing up the PostgreSQL database by running
```bash
PGPASSWORD=<postgres_password> pg_dumpall postgres | lz4 -c > telemetry_server.sql.lz4
```
and then copying the resultant file out of the telemetry server host.
Please note that <postgres_password> is the same as in the docker-compose.yml
file.
