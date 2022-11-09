#!/usr/bin/env python3
# vim: ts=4 sw=4 expandtab

"""
This program ("The Bot") syncs anonymized crash reports received via telemetry
with Ceph's Redmine bug tracking system.

To run this, use:
    - for a dry run:
        ./crashes_redmine_sync.py

    - for production (which will open / update Redmine issues, and update
      telemetry db):
        ./crashes_redmine_sync.py --prod

Log files are written to the directory where this program resides.


Flow:
-----
In each run the bot goes over all the crash signatures reported via telemetry,
looks for an existing redmine issue to update, or creates one if none exists.
Some signatures are not synced since all of their crash events are of EOL versions,
or since they are tagged as 'HeartbeatMap' events.


Background:
-----------
At first the approach was to search each signature (all sig_v1 and sig_v2) in
redmine using redminelib search api. Each search would take between 2.5 and 5
seconds, which took about 10 hours all together.

Then we improved the search time by fetching all redmine issues (all_issues)
once (using redminelib api), loading them to memory, then searching each
signature locally. This cut down the run time from 10 to ~2.5 hours.

Another thought was that we can retrieve only issues that were updated between
runs, but the problem with this was that new crash signatures reported via
telemetry need to be searched on all issues and not just on the deltas, which
made the workflow cumbersome.

Then we decided to (read only) access redmine db directly, emulating redminelib
"Issue" object API, so we can move back and forth between using redminelib and
direct db access, and use redminelib for the updates. Currently an entire run
takes about 15 minutes.


Terminology:
------------
- A crash signature / fingerprint:
  A SHA256 digest
  (e.g. 56882c6326980039ed448bc56d33307c3ee2aafa873f2601d76856e953c06ee7).

- sig_v1:
  The first version of the crash signature; created on the client side.
  sig_v1 recipe:
    - A sterilized backtrace (addresses removed from its frames).
    - If exists, add a sterilized assert message.

- sig_v2:
  The second version of the crash signature; created on telemetry backend.
  This improved version better groups crash events, hence multiple sig_v1
  are mapped to a single sig_v2.
  sig_v2 recipe:
    - A sterilized backtrace (addresses removed from its frames)
    - Filter-out certain functions from the sterilized backtrace.
    - If exists, add the assert function and assert condition.

- spec: A crash spec:
  A row from the crash.spec_mv materialized view. The entire specification
  details of a crash, including its signature, the signature's input
  (backtrace, assert function and condition), its affected versions, affected
  clusters count, crash events count, etc.

- issue: Redmine issue object. Either a redminelib or DBIssue object.

- extended_issue: An issue, together with its signatures and description and notes.
  Dictionary of: {'v1': sig_v1, 'v2': sig_v2, 'text': text, 'issue': i}
  where i is either a redminelib or DBIssue object.


Please note:
------------
- We wish tracker.ceph.com to be the source of truth for the crash
  signatures. Thus, this is a one way sync, where we update redmine with
  infomation from telemetry. This means there could be crash signatures
  reported in redmine that do not exist in the telemetry db and we are not
  aiming to sync these (they are already tracked in redmine).

- The bot syncs a crash spec according to the corresponding redmine issues statuses.
  The bot will ignore crash events with newer versions reported for the first
  time, in case the corresponding issue is not considered a Ceph bug. It will
  open a new issue and will link it to the existing closed one in case the issue is
  considered as a Ceph bug.
    These statuses imply the issue is a Ceph bug:
        "Resolved"
        "Need More Info"
        "Can't reproduce"
        "Won't Fix - EOL"
    These statuses imply the issue is not a Ceph bug:
        "Won't Fix"
    Theses statuses are inconsistent, but currently are considered as a Ceph bug:
        "Rejected"
        "Closed"


Install:
--------
  $ pip3 install --user python-redmine
  $ pip3 install --user mysql-connector-python
  $ dnf install mysql


For redminelib docs see:
    https://python-redmine.com
"""

from redminelib import Redmine
import psycopg2
import psycopg2.extras
import mysql.connector
from collections import defaultdict
import sys
import time
import re
import json
from datetime import datetime
import textwrap
import cProfile
import logging
import smtplib
from email.message import EmailMessage
from enum import Enum, auto

# TODO remove unused files once we run daily
# Data Source Name file
#PG_DSN = '/opt/telemetry/grafana.dsn'
PG_DSN = 'grafana_live.dsn'

# CEPHTRACKER_DSN file is in a json format since DSN strings are not supported in mysql connector
CEPHTRACKER_DSN = './cephtracker.dsn'
#CEPHTRACKER_DSN = '/opt/telemetry/cephtracker.dsn'

REDMINE_KEY = 'tracker.api.key.bot'
#REDMINE_KEY = '/opt/telemetry/tracker.api.key.bot'

# We need an admin redmine user to create versions
REDMINE_KEY_ADM = 'tracker.api.key.adm'
#REDMINE_KEY_ADM = '/opt/telemetry/tracker.api.key'

REDMINE_ENDPOINT = "https://tracker.ceph.com"

# Will not create issues for crashes that are EOL - older than MIN_SUPPORTED_MAJOR
MIN_SUPPORTED_MAJOR = 15

EMAIL_SUBJECT = 'Telemetry crashes'
EMAIL_FROM = 'telemetry-bot'
EMAIL_TO = 'yaarit@redhat.com' # TODO change

TRACKER_ISSUES_URL = 'https://tracker.ceph.com/issues/'
DASHBOARD_SPEC_URL = 'http://telemetry.front.sepia.ceph.com:4000/d/jByk5HaMz/crash-spec-x-ray?orgId=1&var-sig_v2='

# True - use redminelib ; False - use direct db access
USE_REDMINELIB_ALL_ISSUES = False

LOG_FILE = f"{datetime.now().strftime('%Y-%m-%d_%H:%M:%S')}_telemetry_to_redmine.log"

dry_run = True

logging.basicConfig(filename=LOG_FILE,
                level=logging.DEBUG,
                format='%(asctime)s.%(msecs)03d %(levelname)s %(module)s - %(funcName)s: %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'
                )

def dry(s):
    # Making it explicit in the logs it's a dry run
    logging.info(s)


########################## Direct Redmine DB access API ##########################

class DBIssue:
    """
    Represents a redmine issue object; designed to be compatible with redminelib issue API.

    A DBIssue is created when fetching all redmine issues via a direct redmine
    db access (as opposed to via redminelib).

    Attributes
    ----------
    id :            int, the redmine id of the issue.
    subject:        str, the subject of the issue.
    updated_on:     date str ('YYYY-MM-DD'), date when issue was last updated.
                    Remains the same even if the update was deleted (e.g. a note was
                    deleted).
    fixed_version:  str, 'v15.2.5'. The issue's target version.
    tracker:        str, the tracker's type (e.g. 'Bug', 'Backport').
    status:         str, the status name (e.g. 'Closed', 'Resolved').
    custom_fields:  dict of the issue's cf_ids to their values.
    relations:      list of tuples (issue_id, issue_to_id, relation_type) of
                    related issues.
    """
    def __init__(self, _id, _subject, _updated_on, _fixed_verison, _tracker, _status, _custom_fields):
        self.id = _id
        self.subject = _subject
        self.updated_on = _updated_on
        self.fixed_version = self._Name(_fixed_verison)
        self.tracker = self._Name(_tracker)
        self.status = self._Name(_status)
        self.custom_fields = self._Custom_fields(_custom_fields)
        self.relations = list()

    def __repr__(self):
        return repr(self.subject)

    # Helper class to support accesses like issue.tracker.name
    class _Name:
        def __init__(self, _name):
            self.name = _name

    class _Custom_fields:
        def __init__(self, _custom_fields):
            self.all_custom_fields = _custom_fields

        class _Value:
            def __init__(self, _value):
                self.value = _value

        def get(self, cf_id):
            if cf_id not in self.all_custom_fields:
                return None
            return self._Value(self.all_custom_fields[cf_id])

    class Relation:
        def __init__(self, _issue_id, _issue_to_id, _relation_type):
            self.issue_id = _issue_id   # this is 'issue_from_id' in the database; we use
                                        # 'issue_id' instead due to compatibility with redminelib
            self.issue_to_id = _issue_to_id
            self.relation_type = _relation_type

def get_all_projects(cnx):
    """
    Gets all active, public redmine projects.

    Our MySQL server version does not support Common Table Expressions (let
    alone recursive CTEs), and since there are cases where parent_id > id, a
    common sql fix does not work here:
    https://stackoverflow.com/questions/20215744/how-to-create-a-mysql-hierarchical-recursive-query

    Parameters:
        cnx: A connection to redmine db.

    Returns:
        projects, a list of dictionaries with the keys {id, name, parent_id}, where:
            id: the project's id
            name: the project's name
            parent_id: the id of the project's parent
    """
    mysql_cursor = cnx.cursor(dictionary=True)
    projects = list()
    mysql_cursor.execute("""
            SELECT id, name, parent_id
            FROM cephtracker.projects
            WHERE is_public = 1 and status = 1
            """) # 'status = 1' is for active projects

    for p in mysql_cursor:
        projects.append(p)

    mysql_cursor.close()
    return projects

def find_ceph_project(projects):
    """
    Finds Ceph project id in a list of project dictionaries.

    Returns: Ceph project id, or None if not found.
    """
    for p in projects:
        if p['name'] == 'Ceph':
            return p['id']
    return None

def get_projects_tree(projects, parent_id):
    """
    Recursively builds a family tree of a parent project and all of its subprojects.

    Ceph's subprojects can have their own subprojects (e.g. Ceph -> mgr -> Dashboard).

    Parameters:
        projects: A list of dictionaries with the keys {id, name, parent_id}.
        parent_id: The id of the parent project.

    Returns:
        A list of ids of all subprojects of parent_id project (including).
    """
    ret = [parent_id]
    for child in [p for p in projects if p['parent_id'] == parent_id]:
        ret += (get_projects_tree(projects, child['id']))
    return ret

def get_all_custom_fields(cnx):
    """
    Gets all custom fields and their values of all issues.

    We select even empty custom fields as redminelib does that;
    We select all types of custom fields since we might need some
    of them in the future, and this simplifies the design.

    Returns:
        all_cf, a dict of:
        all_cf[issue_id][custom_field_id] = [value1, value2, value3]
    """
    cf_cursor = cnx.cursor(dictionary=True)
    # customized_id is issue id in this case;
    cf_cursor.execute("""
            SELECT customized_id, custom_field_id, value
            FROM cephtracker.custom_values
            """)

    all_cf = defaultdict(lambda: defaultdict(list))
    for cf in cf_cursor:
        all_cf[cf['customized_id']][cf['custom_field_id']].append(cf['value'])

    cf_cursor.close()
    return all_cf

def append_issue_notes(cnx, all_issues):
    """Appends all journal notes to 'text' key in all_issues."""
    notes_cursor = cnx.cursor(dictionary=True)
    notes_cursor.execute("""
            SELECT id, journalized_id, notes
            FROM cephtracker.journals
            WHERE LENGTH(notes) > 0
            AND private_notes = 0
            ORDER BY id
            """)

    for note in notes_cursor:
        issue_id = note['journalized_id']

        if issue_id not in all_issues:
            # logging.error(f"notes are found in a journal of issue_id {issue_id}, but the issue is not in all_issue dictionary")
            continue
        all_issues[issue_id]['text'] = f"{all_issues[issue_id]['text']}{note['notes']}\n"

    notes_cursor.close()
    return all_issues

def append_issue_relations(cnx, all_issues):
    """Appends relations to DBIssue objects in all_issues."""
    cursor = cnx.cursor(dictionary=True)
    cursor.execute("""
            SELECT id, issue_from_id, issue_to_id, relation_type
            FROM cephtracker.issue_relations
            """)

    for rel in cursor:
        issue_id = rel['issue_from_id']

        if issue_id not in all_issues:
            # logging.error(f"relation {rel['id']} found with no corresponding issue {issue_id}")
            continue
        all_issues[issue_id]['issue'].relations.append(DBIssue.Relation(rel['issue_from_id'], rel['issue_to_id'], rel['relation_type']))

    cursor.close()
    return all_issues

def _direct_db_get_all_issues(cnx, projects, since = '1970-01-01'):
    """
    Gets all public redmine issues of the specified projects which are updated
    since a given date.

    This function accesses redmine db directly.

    Parameters:
        projects: A list of redmine project ids to fetch their issues.
            By default we invoke this with Ceph and all of its recursive
            subprojects.
        since: A date str ('YYYY-MM-DD'), by default we fetch everything
            in each run, since we need to search new sigs in previously
            searched issues.

    Returns:
        all_issues: A dictionary of all redmine issues updated after 'since'.
        all_issues[issue_id] = {
                          'v1': str, value of the issue's sig_v1 field
                          'v2': str, value of the issue's sig_v2 field
                          'text': str, description + all of the issue notes
                          'issue': DBIssue Object
                          }
    """
    all_issues = {}

    all_cf = get_all_custom_fields(cnx)

    # formatting projects list for SQL query
    projects_str = ", ".join(str(p) for p in projects)
    issues_cursor = cnx.cursor(dictionary=True)
    # We're not supposed to use f-strings in the query, but we make an exception
    # this time since it's cumbersome to parse the projects list otherwise.
    issues_cursor.execute(f"""
             SELECT cephtracker.issues.*,
                    cephtracker.trackers.name tracker_name,
                    cephtracker.issue_statuses.name status_name
             FROM cephtracker.issues
             INNER JOIN cephtracker.trackers ON cephtracker.trackers.id = cephtracker.issues.tracker_id
             INNER JOIN cephtracker.issue_statuses ON cephtracker.issue_statuses.id = cephtracker.issues.status_id
             WHERE is_private = 0
             AND project_id IN ({projects_str})
             AND updated_on >= %s
             """, (since,))

    for issue in issues_cursor:
        i_id = issue['id']
        all_issues[i_id] = {'v1': None, 'v2': None, 'text': ''}

        # adds 'v1' and 'v2' sigs
        if cf_sig_v1_id in all_cf[i_id]:
            all_issues[i_id]['v1'] = all_cf[i_id][cf_sig_v1_id][0]

        if cf_sig_v2_id in all_cf[i_id]:
            all_issues[i_id]['v2'] = all_cf[i_id][cf_sig_v2_id][0]

        if issue['description'] is not None and len(issue['description']) > 0:
            all_issues[i_id]['text'] = f"{issue['description']}\n"

        # fixed_version is target version, which is not a custom field
        fixed_version = None
        if issue['fixed_version_id'] is not None:
            if str(issue['fixed_version_id']) in redmine_id_to_ceph_version:
                # To support compatibility with redminelib flow, we add 'v'
                fixed_version = 'v' + redmine_id_to_ceph_version[str(issue['fixed_version_id'])]
                # logging.debug(f"  fixed_version: {fixed_version}")
            """
            else:
                logging.warn(f"issue {i_id} has a fixed_version_id {issue['fixed_version_id']} which is not in ceph versions (probably in a sub project)")
            """
        # We add the issue object here since we need it later when updating it
        all_issues[i_id]['issue'] = DBIssue(i_id, issue['subject'], issue['updated_on'], fixed_version, issue['tracker_name'], issue['status_name'], all_cf[i_id])

    issues_cursor.close()

    all_issues = append_issue_notes(cnx, all_issues)
    all_issues = append_issue_relations(cnx, all_issues)

    return all_issues

def direct_db_get_all_issues():
    """A wrapper around _direct_db_get_all_issues."""
    with open(CEPHTRACKER_DSN, 'r') as f:
        cephtracker_dsn = json.load(f)

    cnx = mysql.connector.connect(user=cephtracker_dsn['user'],
                                password=cephtracker_dsn['password'],
                                database=cephtracker_dsn['database'],
                                host=cephtracker_dsn['host']
                                )

    # projects is a list of dictionaries with the keys {id, name, parent_id}
    projects = get_all_projects(cnx)
    ceph_id = find_ceph_project(projects)
    ceph_family = get_projects_tree(projects, ceph_id)

    all_issues = _direct_db_get_all_issues(cnx, ceph_family)
    cnx.close()

    return all_issues

# End of Direct Redmine DB access API
#################################################################################

class Action(Enum):
    """
    Action to be taken on a given spec, either:
        nothing, send email, or open a new redmine issue.
    """
    NONE = auto()
    EMAIL = auto()
    NEW_ISSUE = auto()

def redmine_custom_field_get_id(redmine, name):
    fields = redmine.custom_field.all()
    for field in fields:
        if (field.name == name):
            return field.id

def search_issues(all_issues, term, found_issues_dict):
    """
    Searches for a given term in all_issues and append results to
    found_issues_dict.

    Parameters:
        all_issues: The issues to search on.
        term: The string to search for (usually sig_v1 or sig_v2).
        found_issues_dict: A dictionary holding the search results.

    Returns:
        found_issues_dict: A dictionary holding the search results
            (same format as all_issues).
    """
    if all_issues is None:
        return found_issues_dict
    for issue_id, i in all_issues.items():
        if issue_id not in found_issues_dict:
            # sig might be in one of these fields:
            if i['v1'] and term in i['v1']:
                found_issues_dict[issue_id] = i
            if i['v2'] and term in i['v2']:
                found_issues_dict[issue_id] = i
            if i['text'] and term in i['text']:
                found_issues_dict[issue_id] = i
    return found_issues_dict

def update_spec_status(conn, spec_id, status, issue = None):
    """
    Records in telemetry db the status of the spec.

    Status is determined either by the status of the most relevant issue to the
    crash spec, or by the spec itself (EOL).
    """
    dict_cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    dict_cur.execute(
                """
                UPDATE crash.spec
                SET spec_status_id = (SELECT id FROM crash.spec_status WHERE description = %s)
                WHERE crash.spec.id = %s
                RETURNING id
                """,
                (status, spec_id)
            )
    res = dict_cur.fetchone()
    logging.debug(f"  updated spec id {spec_id} to be of status {status}; returned {res['id']}")

    # TODO change design to update status only once
    if issue is not None:
        dict_cur.execute(
                    """
                    INSERT INTO crash.spec_to_redmine_main_issue
                    (spec_id, issue_id, issue_status) VALUES (%s, %s, %s)
                    ON CONFLICT (spec_id) DO UPDATE
                    SET issue_id = %s, issue_status = %s
                    """,
                    (spec_id, issue.id, issue.status.name, issue.id, issue.status.name)
                )
        logging.debug(f"  inserted into spec_to_redmine_main_issue {spec_id}, {issue.id}, {status}")

    conn.commit()

def is_heartbeatmap(conn, spec):
    """
    Checks if a given spec is considered to be of a HeartbeatMap status, which is not a bug.

    Returns:
        True if "heartbeatmap" is found either in the backtrace, assert
        function, assert condition, assert message, or assert file.
    """
    # Maybe we already handled this spec:
    if spec['description'] == 'HeartbeatMap':
        logging.debug(f"  spec id {spec['id']} is not a bug, not opening a ticket, and not searching for references of it in redmine")
        return True

    if any('HeartbeatMap' in frame for frame in spec['stack_names']):
        logging.debug('  heartbeatmap is found in backtrace')
        return True

    # HeartbeatMap might not be in backtrace, but it is in the assert function:
    if spec['assert_func'] and 'HeartbeatMap' in spec['assert_func']:
        logging.debug('  heartbeatmap is not found in backtrace, but it is in assert_func')
        return True

    # So far there are no examples where HeartbeatMap was neither found in
    # backtrace nor in assert_func, but was found in assert_msg; however there
    # were cases where it was only in the assert_file. Including assert_msg in
    # case it is relevant in the future.
    dict_cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    dict_cur.execute(
                """
                SELECT *
                FROM crash.crash
                WHERE spec_id = %s
                AND (assert_msg ILIKE '%%heartbeatmap%%' OR assert_file ILIKE '%%heartbeatmap%%')
                """, (spec['id'], )
            )

    if dict_cur.rowcount != 0:
        logging.debug('  heartbeatmap is found in either assert_msg or assert_file')
        return True

    return False

def should_ignore_spec(conn, spec):
    """Returns True if a given spec should not be handled for various reasons."""
    if is_heartbeatmap(conn, spec):
        update_spec_status(conn, spec['id'], 'HeartbeatMap')
        return True

    majors_affected = spec['majors_affected']
    if majors_affected is None:
        logging.info("   Missing majors affected, or all versions are Development versions; value is None")
        update_spec_status(conn, spec['id'], 'NA')
        return True

    if len(majors_affected) < 1:
        logging.info("   Missing majors affected, or all versions are Development versions; length is 0")
        update_spec_status(conn, spec['id'], 'NA')
        return True

    # All crashes reported are of EOL versions:
    if int(sorted(majors_affected)[-1]) < MIN_SUPPORTED_MAJOR:
        update_spec_status(conn, spec['id'], 'EOL')
        return True

    minors_affected = spec['minors_affected']
    if minors_affected is None:
        logging.info("   Missing minors affected, or all versions are Development versions; value is None")
        update_spec_status(conn, spec['id'], 'NA')
        return True

    if len(minors_affected) < 1:
        logging.info("   Missing minors affected, or all versions are Development versions; length is 0")
        update_spec_status(conn, spec['id'], 'NA')
        return True

    return False

def add_issue_relations(extended_issue, all_issues, found_issues_dict):
    """
    Recursively adds relevant related issues of a given issue.

    The relevant relations are "duplicates" and "copied_to".
    We do not add issues with "relates" relations, since they indicate a
    different bug, not the exact similar one.

    The logic in redmine is:
        'issue_from_id' X 'duplicates' 'issue_to_id' Y.
    Meaning Y is the original issue.
        'issue_from_id' A 'copied_to' 'issue_to_id' B.
    Meaning A is the original and B is the backport.

    Due to compatibility with redminelib we renamed 'issue_from_id' to
    'issue_id'.

    Parameters:
        extended_issue: See terminology.
        all_issues: All redmine issues to be searched on.
        found_issues_dict: Subset of all_issues, appended with the search results.

    Returns:
        None; populates found_issues_dict with search results.
    """
    for rel in extended_issue['issue'].relations:
        if rel.issue_id == extended_issue['issue'].id and rel.relation_type in ['duplicates', 'copied_to']:
            if rel.issue_to_id in all_issues:
                if rel.issue_to_id in found_issues_dict:
                    return # Stop recursion
                found_issues_dict[rel.issue_to_id] = all_issues[rel.issue_to_id]    # Add the "original" or the backport issue
                add_issue_relations(all_issues[rel.issue_to_id], all_issues, found_issues_dict)
            else:
                logging.error(f"  Issue {rel.issue_to_id} not in all issues while pulling duplicate of {issue_id}")

def spec_get_related_issues(spec, all_issues):
    """
    Gets all redmine issues which are related to a given crash spec.

    Related issues are:
        - issues that contain one of the crash's sigs (v1 or v2).
        - issues that are backports of issues with sigs.
        - issues that are the "original" of a duplicated issue with sigs.

    Related issues are *not* issues that are linked with the 'relates' relation.
    Also, not to be confused with redmine's GUI "Related issues" section.

    Parameters:
        spec: crash spec.
        all_issues: The issues to search on for the crash's sigs.

    Returns:
        A list of (redminelib or DBIssue) issue objects, of issues that are
        related to the given crash spec.
    """

    # found_issues_dict is a subset of all_issues, consists of issues that were
    # found to be relevant to this spec (by either containing any of its sigs,
    # or by being the original issue of a duplicate issue that contains the
    # sigs).
    found_issues_dict = {}

    if spec['sig_v1_arr'] is not None:
        for sig_v1 in spec['sig_v1_arr']:
            logging.info(f"      sig_v1: {sig_v1}")
            found_issues_dict = search_issues(all_issues, sig_v1, found_issues_dict)

    # search by sig_v2 as well:
    sig_v2 = spec['sig_v2'].hex()
    logging.info(f"      sig_v2: {sig_v2}")
    found_issues_dict = search_issues(all_issues, sig_v2, found_issues_dict)

    # Workaround: iterate over the list of values instead of over the
    # dictionary so that when adding to found_issues (below) the size of the
    # iteration object does not change.
    for extended_issue in list(found_issues_dict.values()):
        add_issue_relations(extended_issue, all_issues, found_issues_dict) # Updates found_issues_dict

    return [extended_issue['issue'] for extended_issue in found_issues_dict.values()]

def ver_max(a, b):
    # a and b are version strings,  e.g. '15.2.1'
    if a.split('.') > b.split('.'):
        return a
    return b

def found_issues_affected_versions_maxes(found_issues):
    """
    Finds the absolute newest version, and the newest minor per major of all
    affected_versions in all found_issues.

    Returns:
        version_max, versions_major_max
        e.g.: '16.2.5', {'14': '14.2.9', '15': '15.2.3', '16': '16.2.5'}.
    """
    versions_major_max = {}     # Max minor per major: x.y.z - dictionary of x to x.max(y.z)
    version_max = '0.0.0'
    for issue in found_issues:
        issue_versions = issue_get_affected_and_target_versions(issue)
        for version in issue_versions:
            version_max = ver_max(version_max, version)
            maj = version.split('.')[0]
            if maj in versions_major_max:
                versions_major_max[maj] = ver_max(versions_major_max[maj], version)
            else:
                versions_major_max[maj] = version
    return version_max, versions_major_max

def spec_to_issues_versions_diff(spec, issues_version_global_max, issues_version_max_per_major):
    """
    Calculates the delta between telemetry crash versions and versions reported in all related issues.

    The delta contains only newer versions reported via telemetry.

    Parameters:
        spec: crash spec.
        issues_version_global_max: str, the absolute newest version reported in
            all related issues.
        issues_version_max_per_major: A dict of major versions to their max minor versions
            (of versions reported in all related issues).

    Returns:
        diff_global, diff_per_major
        Where:
          diff_global: A list of all versions that are newer than the absolute
              newest version found in all related issues.
          diff_per_major: A dict of major versions to a list of newer minors
              reported via telemetry.

    For example, for:
        spec['minors_affected'] = ['14.2.3', '15.1.2', '15.2.10', '16.2.1', '16.2.4', '16.2.5']
        issues_version_global_max = '16.2.2'
        issues_version_max_per_major = {'15': '15.2.15', '16': '16.2.2'}
        MIN_SUPPORTED_MAJOR = 15

    We return:
        diff_global = ['16.2.4', '16.2.5']
        diff_per_major = {'16': ['16.2.4', '16.2.5']}
    """
    diff_per_major = {}
    diff_global = []
    for spec_affected_version in spec['minors_affected']:
        maj = spec_affected_version.split('.')[0]
        if int(maj) < MIN_SUPPORTED_MAJOR:
            continue

        if spec_affected_version.split('.') > issues_version_global_max.split('.'):
            diff_global.append(spec_affected_version)

        if maj not in issues_version_max_per_major or spec_affected_version.split('.') > issues_version_max_per_major[maj].split('.'):
            # Older major seen for the first time, or older major with a newer
            # minor seen for the first time
            if maj not in diff_per_major:
                diff_per_major[maj] = []
            diff_per_major[maj].append(spec_affected_version)

    return diff_global, diff_per_major

def find_main_issue(found_issues, include_closed = False):
    """
    Finds the issue that best represents the spec out of all issues related to it (found_issues).

    Finds the issue that was last updated, not closed, not a duplicate and not a
    backport ("duplicate" is in redmine_closed_statuses).
    We fetch the backport issues from their original bug issues (via 'relations').

    Returns:
        If found - an issue object; None otherwise.
    """
    for issue in sorted(found_issues, key=lambda issue: issue.updated_on, reverse=True):
        # There is also issue.closed_on, but it is not cleared when an issue is reopened, thus we check the status
        if issue.tracker.name != 'Backport' and (include_closed or issue.status.name not in redmine_closed_statuses):
            return issue
    return None

def filter_versions_to_email(spec, diff_per_major):
    """
    Filters out versions we already sent email about in previous runs.

    We notify by email (vs. by opening a new issue) about telemetry crash events that have:
        - an older major seen for the first time; e.g. so far all crashes were
          of version 16, and now we see version 15.
        - a newer minor of an already seen major, when a newer major exists;
          e.g. so far reported versions were of 16.2.5 and 15.2.4, and now we
          see 15.2.8 for the first time.

    Returns:
        The filtered out list of (spec_id, ceph_version) tuples per major
        release that should be included in the next email message.
    """
    for maj in list(diff_per_major):
        tmp = list(filter(lambda version: (spec['id'], version) not in redmine_email_sent, diff_per_major[maj]))
        if tmp:
            diff_per_major[maj] = tmp
        else:
            del diff_per_major[maj]
    return diff_per_major

def send_email(content):
    msg = EmailMessage()
    msg.set_content(content)

    msg['Subject'] = EMAIL_SUBJECT
    msg['From'] = EMAIL_FROM
    msg['To'] = EMAIL_TO

    s = smtplib.SMTP('localhost')
    s.send_message(msg)
    logging.info(f"  SEND EMAIL: {email_message}")
    s.quit()

def sanitize_spec_versions(conn, spec):
    """
    Filters out versions of irregular (major and minor) formats from spec dict.

    spec['majors_affected'] can include reported versions like 'l', 'Development'.
    spec['minors_affected'] can include reported versions like '14.2.1.2'.
    Filter these out.

    Parameters:
        conn: A telemetry db connection object.
        spec: crash spec.

    Returns:
        A filtered-out spec of both nonstandard major and minor versions.
    """
    if spec['majors_affected'] and len(spec['majors_affected']) > 0:
        if spec['majors_affected'] == ['Development']:
            logging.info("   All versions are Development")

        # spec['majors_affected'] will be empty in case (spec['majors_affected'] == ['Development']),
        # we'll handle it in should_ignore_spec()
        spec['majors_affected'] = [m for m in spec['majors_affected'] if is_valid_version_major(m)]

    if spec['minors_affected'] and len(spec['minors_affected']) > 0:
        spec['minors_affected'] = [m for m in spec['minors_affected'] if is_valid_version_minor(m)]
    return spec

def update_main_issue(redmine, conn, spec, issue):
    """
    Updates a given redmine issue with a given spec data.

    Returns: None
    """
    logging.info(f"  Found main ISSUE: {issue.id}")
    # By default we access redmine directly (and not through redminelib), thus
    # we need to create a redminelib object since updating the issue is done
    # via redminelib api only.
    if not USE_REDMINELIB_ALL_ISSUES:
        issue = redmine.issue.get(issue.id)
    redmine_update_issue(redmine, issue, spec)

    if not is_redmine_description_added(conn, issue.id):
        # issue does not have this spec description
        issue.notes = generate_issue_description(conn, spec)
        logging.debug(f"  Adding spec description to notes")
    if not dry_run:
        issue.save()
        insert_to_redmine_description_added(conn, spec['id'], issue.id)
        update_spec_status(conn, spec['id'], issue.status.name, issue)

def handle_closed_bug(conn, found_issues, spec):
    """
    Concludes the most suitable action to take on a given closed bug.

    This determines what action to take on a closed issue which is considered a
    bug according to its status, in one three ways:
        - either do nothing
        - create a new issue and link the two
        - send an email to notify about version diff

    Returns:
        Action.
    """
    global email_message
    global new_issue_message

    # Gather all affected versions from all issues; gather "target version"
    # from backports; check if the spec has a version which is newer than all
    # the gathered versions. If so, create a new issue.
    issues_version_global_max, issues_version_max_per_major = found_issues_affected_versions_maxes(found_issues)

    logging.debug(f"  found_issues versions: global_max={issues_version_global_max} max_per_major={issues_version_max_per_major}")
    diff_global, diff_per_major = spec_to_issues_versions_diff(spec, issues_version_global_max, issues_version_max_per_major)
    logging.debug(f"  diff versions: global_max={diff_global} max_per_major={diff_per_major}")

    # Newer major or minor is reported via telemetry, not seen yet:
    if len(diff_global):
        new_issue_message += f"*New crash events were reported via Telemetry with newer versions ({diff_global})" \
                f" than encountered in Tracker ({issues_version_global_max}).*\n"
        return Action.NEW_ISSUE
    else:
        # Filter diff_per_major, remove versions we already reported on in past emails
        logging.debug(f"  diff_per_major before filter_versions_to_email: {diff_per_major}")
        diff_per_major = filter_versions_to_email(spec, diff_per_major)
        logging.debug(f"  diff_per_major after filter_versions_to_email: {diff_per_major}")
        if not diff_per_major:
            logging.debug(f"  No versions to send email of")
            return Action.NONE

        tmp_message = ''
        for maj in diff_per_major:
            # older major seen for the first time, or older major with a newer minor seen for the first time
            if maj not in issues_version_max_per_major:
                tmp_message += f"New crash events were reported via Telemetry with an older major '{maj}' ({diff_per_major[maj]})" \
                        f" that is reported for the first time.\n"
            else:
                tmp_message += f"New crash events were reported via Telemetry with newer versions ({diff_per_major[maj]})" \
                        f" than encountered in Tracker ({issues_version_max_per_major[maj]}.)\n"

            for version in diff_per_major[maj]:
                insert_to_redmine_email_sent(conn, spec['id'], version)

        logging.debug(f"  email text: {tmp_message}")
        email_message += tmp_message
        return Action.EMAIL

def handle_spec(conn, redmine, spec, all_issues):
    """
    Handles a crash spec received via telemetry.

    This function concludes for a given spec whether it represents an active bug
    and should be handled (otherwise it is tagged with a suitable status and ignored).
    If a spec should be handled, we then search its signatures (v1 and v2) in all_issues.
    If there are search results, we examine these issues and decide how to sync the spec
    with them.

    Parameters:
        conn: A telemetry db connection object.
        redmine: A redminelib db connection object.
        spec: crash spec - A dict containing all keys relevant to
            the crash spec (e.g. 'sig_v2', 'sig_v1_arr', 'minors_affected').
        all_issues: A dict contains all redmine issues to search on for this
            spec signatures.

    Returns:
        None
    """
    logging.debug(f"\n***************************************************************************\n")
    logging.info(f"handle_spec {spec['id']}")
    logging.debug(f"  {int(time.time())} {int(time.time()) - start_time} handle spec")

    # Remove nonstandard major and minor versions from the crash's spec; this
    # way we do not create nonstandard versions in redmine db, and do not try
    # to update issues depending on these versions.
    spec = sanitize_spec_versions(conn, spec)

    # if it's not a bug no need to open a redmine issue
    if should_ignore_spec(conn, spec):
        logging.info(f"   NOT A BUG / spec id {spec['id']} should be ignored")
        return

    found_issues = spec_get_related_issues(spec, all_issues)
    logging.debug(f"  Found {len(found_issues)} relevant issues:")
    for i in found_issues:
        logging.debug(f"    {i.id} {i}")

    action = Action.NONE
    global email_message
    global new_issue_message
    new_issue_message = ''

    # Here we decide what Action should be taken for the spec to be synced:
    if found_issues:
        # Sanity checks on found_issues
        if all(i.tracker.name == 'Backport' or i.status.name == 'Duplicate' for i in found_issues):
            logging.error(f"  All issues in found_issues are either backports or duplicates")

        last_updated_open_issue = find_main_issue(found_issues)

        if last_updated_open_issue:
            # found_issues contain at least one *open* issue (which is considered a bug unless classified otherwise)
            update_main_issue(redmine, conn, spec, last_updated_open_issue)
        else:
            # there are no open issues for this spec but there might be closed ones
            logging.info(f"  No open main issue found.")

            # Check if this found_issues group classify this spec as a bug or
            # not - If all issues in found_issues are closed with a status that
            # is not a bug ['Closed', 'Rejected', "Won't Fix"] then we won't
            # open a new issue. If one of the issues is closed with a status
            # that is a bug ['Resolved', "Can't reproduce"] then we open a new
            # issue, after comparing the affected versions. If the status is
            # 'Duplicate' we assume that the original is in found_issues and we
            # ignore the duplicate.

            # Check if all issues are not considered a bug
            # TODO: We might need to add back ['Closed', 'Rejected'] to the list of 'not a bug' statuses here.
            if all(i.tracker.name == 'Backport' or i.status.name in ["Won't Fix", 'Duplicate'] for i in found_issues):
                logging.debug(f"  all found_issues are either backports or classified as not bugs, no need to create a new issue")
                # Find the most recent 'updated_on' issue, which is not a backport;
                # Its status should be of not a bug ('Closed', 'Rejected', "Won't Fix", 'Duplicate')
                last_updated_closed_issue = find_main_issue(found_issues, True)
                # if last_updated_closed_issue is None now, it means it's a backport;
                # In this case we mark the signature's status as 'Closed' on telemetry's backend
                closed_status = last_updated_closed_issue.status.name if last_updated_closed_issue else 'Closed'
                update_spec_status(conn, spec['id'], closed_status, last_updated_closed_issue)
                action = Action.NONE
            else:
                # all issues are closed, but at least one of them is considered a bug
                action = handle_closed_bug(conn, found_issues, spec)
    else:
        # no found_issues (found_issues is empty)
        logging.info(f"  No issues found.")
        action = Action.NEW_ISSUE


    # Here we take that Action:
    if action == Action.NEW_ISSUE:
        # Either:
        #   - no issues were found
        #   - issues were found, but all of them are closed: at least one of
        #     them is closed as a bug (status is "Resolved", "Can't reproduce",
        #     "Closed", "Rejected", "Won't Fix - EOL"), and a new crash event
        #     is now reporting an absolute newer version.
        logging.info(f"  OPEN ISSUE: {new_issue_message}")
        new_issue = create_issue(conn, redmine, spec, generate_issue_description(conn, spec, new_issue_message))
        if not dry_run:
            insert_to_redmine_description_added(conn, spec['id'], new_issue.id)
        redmine_update_issue(redmine, new_issue, spec)
        if found_issues:
            issue_add_relations(redmine, new_issue, found_issues)
        if not dry_run:
            new_issue.save()
            update_spec_status(conn, spec['id'], new_issue.status.name, new_issue)
            logging.info(f"    Created issue {new_issue.id} : {new_issue}")
    elif action == Action.EMAIL:     # elif because action can be both Action.NEW_ISSUE and Action.EMAIL, and in this case we prefer new issue
        # logging.info(f"  SEND EMAIL: {email_message}")
        last_updated_closed_issue = find_main_issue(found_issues, True)
        # if last_updated_closed_issue is None now, it means it's a backport;
        # In this case we mark the signature's status as 'Closed' on telemetry's backend
        closed_status = last_updated_closed_issue.status.name if last_updated_closed_issue else 'Closed'
        update_spec_status(conn, spec['id'], closed_status, last_updated_closed_issue)

        # Concatenate links to the email message
        issue_url = f"{TRACKER_ISSUES_URL}{last_updated_closed_issue.id}"
        spec_url = f"{DASHBOARD_SPEC_URL}{spec['sig_v2'].hex()}"
        email_message += f"See:\n{issue_url}\n{spec_url}\n\n"

def issue_add_relations(redmine, issue, found_issues):
    """
    Adds all related issues to the newly created issue.

    Currently only works when 'issue' is a newly created issue that is not in found_issues.
    """
    if dry_run:
        dry(f"  issue_add_relations: Adding relation for issue IDs={[i.id for i in found_issues]}")
        return

    for related_issue in found_issues:
        # Redmine function will fail when trying to add duplicate relation or relating an issue to itself.
        redmine.issue_relation.create(
                issue_id = issue.id,
                issue_to_id = related_issue.id,
                relation_type = 'relates'
        )

def get_updated_issues(redmine, since):
    """
    Gets all issues that were updated after 'since', using redminelib api.

    Parameters:
        redmine: A redminelib db connection object.
        since: str, format as 2021-12-19T17:41:01Z.

    Returns:
        'all_issues' dictionary, contains all redmine issues that were updated
        since a certain date ('since').
        The dictionary maps issue_id to a dictionary with keys 'v1', 'v2',
        'text', 'issue'. 'v1', 'v2', 'text' are extracted from the issue object
        and are easier to search on. 'issue' is the original issue object.
    """
    issues = redmine.issue.filter(
         project_id='ceph',
         #cf_22='~foo',
         #sort='category:desc'
         #updated_on='><2021-12-01|2021-12-28',
         updated_on=f'>={since}',
         status_id ='*',
         include=['journals', 'relations']
     )
    all_issues = {}
    for i in issues:
        text = ''
        if 'description' in dir(i):
            text = f"{text}{i.description}\n"
        for j in i.journals:
            if 'notes' in dir(j):
                text = f"{text}{j.notes}\n"

        sig_v1 = i.custom_fields.get(cf_sig_v1_id)
        if sig_v1 is not None:
            sig_v1 = sig_v1.value

        sig_v2 = i.custom_fields.get(cf_sig_v2_id)
        if sig_v2 is not None:
            sig_v2 = sig_v2.value

        all_issues[i.id] = {'v1': sig_v1, 'v2': sig_v2, 'text': text, 'issue': i}
        if len(all_issues) % 100 == 0:
            logging.debug(f"    {len(all_issues)} issues")
    logging.debug(f"get_updated_issues(): Found {len(all_issues)} issues")
    return all_issues

# TODO remove LIMIT 10. Currently it's here so we won't run the bot by mistake before we refine sig_v2.
def get_spec_from_db(conn):
    """
    Fetches all telemetry crash specs.

    Fetches only specs that have at least 2 frames in their sanitized backtrace.

    Parameters:
        conn: A telemetry db connection object.

    Returns:
        dict_cur: The results cursor.
    """
    dict_cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    dict_cur.execute(
                """
                SELECT *
                FROM crash.spec_mv
                WHERE sig_v1_arr IS NOT NULL
                AND stack_names != '{}'
                AND ARRAY_LENGTH(stack_names, 1) > 1
                ORDER BY id
                LIMIT 10
                """
            )

    return dict_cur

def is_redmine_description_added(conn, issue_id):
    dict_cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    dict_cur.execute(
                """
                SELECT 1
                FROM crash.redmine_description_added
                WHERE issue_id = %s
                """, (issue_id, )
            )
    return dict_cur.rowcount != 0

def insert_to_redmine_description_added(conn, spec_id, issue_id):
    """
    Records in telemetry db that a given spec description was added to a given redmine issue.

    Primary key of crash.redmine_description_added table is (spec_id, issue_id).
    """
    cur = conn.cursor()
    cur.execute(""" INSERT INTO crash.redmine_description_added
                (spec_id, issue_id) VALUES (%s, %s)
                ON CONFLICT DO NOTHING""",
                (spec_id, issue_id))
    conn.commit()
    logging.debug(f"  insert into crash.redmine_description_added spec_id: {spec_id}, issue_id: {issue_id}")

def get_redmine_email_sent(conn):
    dict_cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    dict_cur.execute(
                """
                SELECT *
                FROM crash.redmine_email_sent
                """
            )
    res = set()
    for r in dict_cur:
        res.add( (r['spec_id'], r['version']) )

    return res

def insert_to_redmine_email_sent(conn, spec_id, version):
    """
    Records in telemetry db that an email was sent to alert about new crash events of older versions.

    Primary key of crash.redmine_email_sent table is (spec_id, version).
    """
    logging.info(f"  insert into crash.redmine_email_sent spec_id {spec_id}, version {version}")

    if dry_run:
        return

    cur = conn.cursor()
    cur.execute(""" INSERT INTO crash.redmine_email_sent
                (spec_id, version) VALUES (%s, %s)
                ON CONFLICT DO NOTHING""",
                (spec_id, version))
    conn.commit()

def get_redmine_closed_statuses(redmine):
    """
    Returns:
        A list of all closed redmine statuses names (strings).
    """
    ret = []
    for status in redmine.issue_status.all():
        if 'is_closed' in dir(status) and status.is_closed:
            # The native redminelib.resources.standard.IssueStatus type is not
            # easily comparable. Save it as string instead for easier comparisons.
            ret.append(str(status))
    return ret

def is_in_backtrace(backtrace, term):
    # We always ignore case
    return True if term.lower() in ', '.join(backtrace).lower() else False

def get_all_redminelib_projects(redmine):
    projects = redmine.project.all()
    all_projects = {}
    for p in projects:
        all_projects[p.identifier] = p.id
    return all_projects

def pick_project_id_by_daemons_or_frames(daemon_arr, backtrace):
    # KernelDevice in the stacktrace -> bluestore
    if is_in_backtrace(backtrace, 'KernelDevice'):
        return redmine_projects['bluestore']

    # if it's not obvious what project to choose, we assign the crash to rados
    rados_project_id = redmine_projects['rados']

    # 'process_name' is not always reported, or might be reported as 'null' or '[null]'
    if daemon_arr is None:
        return rados_project_id

    # remove empty daemons
    daemon_arr = [d for d in daemon_arr if d is not None and d != [None]]

    # Either the list is now empty, or there can be different daemons reporting the same crash;
    # In this case we assign the issue to rados, unless all daemons are related to a certain subproject.

    # rbd, rbd-mirror, rbd-nbd
    if all('rbd' in d for d in daemon_arr):
        return redmine_projects['rbd']

    if len(daemon_arr) != 1:
        return rados_project_id

    process_name = daemon_arr[0];

    # radosgw, radosgw-admin
    if 'radosgw' in process_name:
        return redmine_projects['rgw']
    # cephfs-mirror, cephfs-journal-, cephfs-data-sca
    if 'cephfs' in process_name or process_name in ['ganesha.nfsd', 'ceph-fuse', 'ceph_mds']:
        return redmine_projects['cephfs']
    if process_name == 'ceph-mgr':
        if is_in_backtrace(backtrace, 'rbd'):
            return redmine_projects['rbd']
        else:
            return redmine_projects['mgr']

    # this should not happen
    if backtrace is None:
        logging.error(f"  backtrace is empty")
        return rados_project_id

    if 'blue' in process_name or is_in_backtrace(backtrace, 'bluefs') or is_in_backtrace(backtrace, 'bluestore'):
        return redmine_projects['bluestore']

    # the rest is assigned to rados
    return rados_project_id

def generate_issue_subject(spec):
    subject = "crash: "
    if spec['assert_func']:
        subject += f"{spec['assert_func']}: "
        if spec['assert_condition'] == 'abort':
            subject += 'abort'
        else:
            subject += f"assert({spec['assert_condition']})"
    else:
        subject += spec['stack_names'][0]
    return subject[:255] # Avoid 'Subject is too long (maximum is 255 characters)' error

def generate_issue_description(conn, spec, description_preamble = None):
    desc = "\n"
    if description_preamble is not None and len(description_preamble) > 0:
        desc += f"{description_preamble}\n"
    desc += f"http://telemetry.front.sepia.ceph.com:4000/d/jByk5HaMz/crash-spec-x-ray?orgId=1&var-sig_v2={spec['sig_v2'].hex()}\n"
    if spec['assert_func'] is not None:
        # When assert_func is present then so is assert_condition
        desc += f"\nAssert condition: {spec['assert_condition']}\n"
        desc += f"Assert function: {spec['assert_func']}\n"
    desc += "\nSanitized backtrace:\n"
    desc += "<pre>"
    desc += "    " + "\n    ".join(spec['stack_names']) + "\n"
    desc += "</pre>"
    desc += "\nCrash dump sample:\n" + "<pre>" + json.dumps(get_most_recent_crash_event(conn, spec), indent=4, sort_keys=True) + "</pre>"
    return desc

def get_most_recent_crash_event(conn, spec):
    """
    Returns the most recently reported crash event of a given spec.

    This does not necessarily return the most recent Ceph version.
    """
    dict_cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    dict_cur.execute(
                """
                WITH crash_info AS (
                    SELECT report_id, crash_id
                    FROM crash.crash
                    WHERE spec_id = %s
                    ORDER BY ts DESC
                    LIMIT 1
                )
                SELECT
                    --JSONB_PRETTY(a.single_crash::JSONB) AS raw_crash
                    a.single_crash AS raw_crash
                FROM (
                    SELECT
                        JSONB_ARRAY_ELEMENTS(REPLACE(report, '\\u0000', '')::JSONB->'crashes') AS single_crash
                    FROM public.report, crash_info
                    WHERE id = crash_info.report_id
                ) a, crash_info
                WHERE a.single_crash::JSONB->>'crash_id' = crash_info.crash_id;
                """,
                (spec['id'],)
            )
    res = dict_cur.fetchone()

    crash = res['raw_crash']
    # We split by 'src' since 'build' is not always present:
    if 'assert_msg' in crash:
        crash['assert_msg'] = '\n'.join([re.split(r'\/src\/', line, flags=re.IGNORECASE)[-1] for line in crash['assert_msg'].splitlines()])
    if 'assert_file' in crash:
        crash['assert_file'] = '\n'.join([re.split(r'\/src\/', line, flags=re.IGNORECASE)[-1] for line in crash['assert_file'].splitlines()])

    return crash

def create_issue(conn, redmine, spec, description):
    project_id = pick_project_id_by_daemons_or_frames(spec['daemon_arr'], spec['stack_names'])
    subject = generate_issue_subject(spec)

    if dry_run:
        dry(f"  create_issue:")
        dry(f"    project_id={project_id}")
        dry(f"    subject={subject}")
        dry(f"    description=\n{textwrap.indent(description, '        ')}\n")
        return None

    issue = redmine.issue.new()
    issue.project_id = project_id
    issue.tracker_id = 1 # for 'Bug' tracker
    issue.subject = subject
    issue.description = description
    # issue.status_id = 1 # for 'New'
    # issue.priority_id = 7 # for 'Normal'
    issue.custom_fields = [{'id': cf_source_id, 'value': 'Telemetry', 'label': 'Telemetry'}]

    issue.save()
    logging.info(f"    Creted issue: #{issue.id} {issue}")
    return issue

def is_valid_version_major(version):
    """
    Returns:
        True if version is of a format of '%d', otherwise False.
        We wish to filter out cases like 'Development', and other nonstandard
        versions (e.g. 'l').
    """
    res = re.search(f"^\d+$", version, re.I)
    return bool(res)

def is_valid_version_minor(version):
    """
    Returns:
        True if version is of a format of '%d.%d.%d', otherwise False.
        This is necessary since we create a new version in redmine (with a
        'closed' status) in case a version reported via telemetry does not
        exist in redmine database. We wish to avoid cases like '14.2.1.1',
        'Development', '16.1.0-944-ge53ee8bd', etc.
    """
    res = re.search(f"^\d+\.[012]+\.\d+$", version, re.I)
    return bool(res)

def create_version(redmine, version_text):
    """
    Adds a version to the list of Ceph project versions in redmine.

    Version is created with status 'closed'.

    Parameters:
        redmine: A redminelib db connection object.
        version_text: str, e.g. '14.2.2'. Should be a valid version format.

    Returns:
        The newly created redmine version id.
    """
    # TODO we need to adjust permissions
    with open(REDMINE_KEY_ADM, 'r') as f:
                redmine_key_adm = f.read().strip()

    redmine_adm = Redmine(REDMINE_ENDPOINT, key=redmine_key_adm)

    logging.info(f" !! Creating new Ceph version {version_text} in Redmine")
    version = redmine_adm.version.create(
                    project_id = 'Ceph',
                    name = f"v{version_text}",
                    status = 'closed'
                    )
    return version.id

def redmine_update_issue(redmine, issue, spec):
    """
    Updates a given redmine issue with its crash spec data.

    Parameters:
        redmine: A redminelib db connection object.
        issue: Either a DBIssue or redminelib issue object
        spec: crash spec.

    Returns:
        None
    """
    if dry_run and issue is None:
        dry(f"  redmine_update_issue new issue: adding sig_v1, sig_v2, versions=({spec['minors_affected']})")
        return

    issue_sig_v2 = issue.custom_fields.get(cf_sig_v2_id)
    # TODO might need to fix that, since if the issue we wish to update is the
    # 'original' of an issue the bot opened and was marked as 'duplicate', the
    # 'original' might not have the crash_signature fields populated at all.
    if issue_sig_v2 is None:
        raise RuntimeError(f"issue_sig_v2 is None for issue: {issue.id} {issue}")

    # Check if sig_v2 of this spec appears in the field's text
    if spec['sig_v2'].hex() not in issue_sig_v2.value:
        if dry_run:
            dry(f"  redmine_update_issue {issue.id}: appending to sig_v2: {spec['sig_v2'].hex()}")
        else:
            custom_field_set(issue, cf_sig_v2_id, append_in_new_line(issue_sig_v2.value, spec['sig_v2'].hex()))

    issue_sig_v1 = issue.custom_fields.get(cf_sig_v1_id)
    if issue_sig_v1 is None:
        raise RuntimeError(f"issue_sig_v1 is None for issue: {issue.id} {issue}")

    new_sig_v1 = issue_sig_v1.value
    for sig_v1 in spec['sig_v1_arr']:
        if sig_v1 not in issue_sig_v1.value:
            new_sig_v1 = append_in_new_line(new_sig_v1, sig_v1);
    if dry_run:
        dry(f"  redmine_update_issue {issue.id}: setting sig_v1: {new_sig_v1}")
    else:
        custom_field_set(issue, cf_sig_v1_id, new_sig_v1)

    issue_affected_versions = issue.custom_fields.get(cf_affected_versions_id)
    if issue_affected_versions is None:
        raise RuntimeError(f"issue_affected_versions is None for issue: {issue.id} {issue}")

    if dry_run:
        dry(f"  redmine_update_issue {issue.id}: setting affected_versions: {spec['minors_affected']}")
    else:
        new_versions = issue_affected_versions.value
        for version in spec['minors_affected']:
            if version not in ceph_version_to_redmine_id:
                version_id = create_version(redmine, version);
                ceph_version_to_redmine_id[version] = version_id
                redmine_id_to_ceph_version[version_id] = version
            if ceph_version_to_redmine_id[version] not in issue_affected_versions.value:
                logging.info(f"    Adding version {version} to the issue's versions list ({issue_affected_versions.value})")
                new_versions.append(ceph_version_to_redmine_id[version])
        custom_field_set(issue, cf_affected_versions_id, new_versions)

    return

def append_in_new_line(orig, addition):
    """
    Appends text in a new line.

    When adding sig_v1 or sig_v2 to the issue's custom field, we want to add
    them in a new line if the field is already populated.

    Parameters:
        orig: The original text to append to.
        addition: The text to append.

    Returns:
        The concatenated text with a new line, or just the new addition in case
        the original text is empty.
    """
    if len(orig) == 0:
        return addition
    return f"{orig}\n{addition}"

def custom_field_set(issue, cf_id, value):
    new_cf = []
    for cf in issue.custom_fields.values():
        if cf['id'] == cf_id:
            cf['value'] = value
        new_cf.append(cf)
    issue.custom_fields = new_cf


def redmine_get_project_versions(redmine, project_name):
    """
    Gets a mapping of a redmine project version names to ids, and vice versa.

    We currently care only for Ceph project versions (vs linux kernel versions, etc.).
    Strips the 'v' from the start of the version name.
    Please note: The redmine version id is a string.

    Parameters:
        redmine: A redminelib db connection object.
        project_name: str, the name of the redmine project.

    Returns:
        name_to_id: A dict of version_name_str to redmine_id
        id_to_name: A dict of redmine_id string to version_name_str

        For example: { '14.2.1': '532'}, {'532': '14.2.1'}
    """
    project = redmine.project.get(project_name)
    name_to_id = {}
    id_to_name = {}
    for v in project.versions:
        # Remove the 'v' from the start of the version string
        # (in redmine it's "v14.2.1").
        # Redmine returns the IDs of the versions as string and not int.
        name_to_id[v.name[1:]] = str(v.id)
        id_to_name[str(v.id)] = v.name[1:]

    return name_to_id, id_to_name

def issue_get_affected_and_target_versions(issue):
    """
    Returns a list of affected versions and target version of a given issue
    ( e.g. ['17.0.0', '15.2.1', '15.2.2']).
    """
    versions = []
    if 'fixed_version' in dir(issue) and issue.fixed_version.name is not None:
        versions.append(issue.fixed_version.name[1:]) # target version is called fixed_version in redmine api
    affected_versions = issue.custom_fields.get(cf_affected_versions_id)
    if affected_versions:
        for v in affected_versions.value:
            if v is not None and len(v) > 0:
                if v in redmine_id_to_ceph_version:
                    versions.append(redmine_id_to_ceph_version[v])
                else:
                    logging.warn(f"issue {issue.id} has an affected_version_id {v}" \
                            f" which is not in ceph versions (probably in a sub project)")
    return versions

def main():
    global ceph_version_to_redmine_id
    global cf_source_id
    global cf_sig_v1_id
    global cf_sig_v2_id
    global cf_affected_versions_id
    global dry_run
    global redmine_closed_statuses
    global redmine_email_sent
    global redmine_id_to_ceph_version
    global redmine_projects
    global start_time

    start_time = int(time.time())

    if len(sys.argv) == 2 and sys.argv[1] == "--prod":
        dry_run = False

    with open(PG_DSN, 'r') as f:
        pg_dsn_str = f.read().strip()

    conn = psycopg2.connect(pg_dsn_str)

    with open(REDMINE_KEY, 'r') as f:
        redmine_key = f.read().strip()

    redmine = Redmine(REDMINE_ENDPOINT, key=redmine_key)

    redmine_closed_statuses = get_redmine_closed_statuses(redmine)
    redmine_projects = get_all_redminelib_projects(redmine)
    ceph_version_to_redmine_id, redmine_id_to_ceph_version = redmine_get_project_versions(redmine, 'Ceph') # '14.2.3' to '603'
    # Custom fields are hardcoded because only Administrator can query them.
    # See https://www.redmine.org/issues/18875
    cf_source_id = 1 #redmine_custom_field_get_id(redmine, 'Source')
    cf_sig_v1_id = 24 #redmine_custom_field_get_id(redmine, 'Crash signature (v1)')
    cf_sig_v2_id = 25 #redmine_custom_field_get_id(redmine, 'Crash signature (v2)')
    cf_affected_versions_id = 9 #redmine_custom_field_get_id(redmine, 'Affected Versions')

    redmine_email_sent = get_redmine_email_sent(conn)

    # Get all relevant redmine issues to search on for crash signatures.
    # We build a data structure (all_issues) that contains all the relevant data
    # fields to search on (e.g. description, journal notes, custom fields).
    # Using redminelib api to fetch all issues takes a long time,
    # and it's better to use the direct DB access option, but we
    # allow this anyway.
    if USE_REDMINELIB_ALL_ISSUES:
        # Fetch everything:
        since = '1970-01-01T00:00:00Z'
        all_issues = get_updated_issues(redmine, since)
    else:
        all_issues = direct_db_get_all_issues()

    # an example of cf output:
    # issue = all_issues[49138]['issue']
    # logging.debug(issue.custom_fields.all_custom_fields)
    logging.info(f"{int(time.time())} {int(time.time()) - start_time} Got all_issues")

    # fetch all telemetry crash specs
    crash_specs_dict_cur = get_spec_from_db(conn)
    logging.info(f"{int(time.time())} {int(time.time()) - start_time} Got all specs")

    if crash_specs_dict_cur.rowcount == 0:
        logging.error("Fetched 0 crash specs, existing")
        raise RuntimeError("No results fetched, existing")

    logging.info(f"Processing {crash_specs_dict_cur.rowcount} specs")

    global email_message
    email_message = ''

    for spec in crash_specs_dict_cur:
        handle_spec(conn, redmine, spec, all_issues)

    if len(email_message) > 0:
        send_email(email_message)

    logging.info(f"data fetched in {int(time.time() - start_time)} seconds")

if __name__ == '__main__':
    # Uncomment for perf stats:
    # cProfile.run('sys.exit(main())')
    sys.exit(main())

