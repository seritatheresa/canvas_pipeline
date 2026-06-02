"""
valleyprod.py - ValleyPROD Oracle Database connector (python-oracledb thin mode).

Connects to ValleyPROD (Ellucian Banner) using python-oracledb in thin mode.
No Oracle Client software or ODBC driver installation is required — the
connection is built entirely from the host, port, service name, and credentials
in your .env file.

Usage:
    from valleyprod import ValleyPRODExtract
    with ValleyPRODExtract() as vp:
        students    = vp.get_students("202620")
        enrollments = vp.get_enrollments("202620")
        courses     = vp.get_courses("202620")
"""

import logging
from pathlib import Path

import oracledb
import pandas as pd

import config

logger = logging.getLogger(__name__)

QUERIES_DIR = Path(__file__).parent / "queries"


def _load_sql(filename):
    """Read a SQL file from the queries/ directory."""
    path = QUERIES_DIR / filename
    if not path.exists():
        raise FileNotFoundError("SQL file not found: {}".format(path))
    return path.read_text(encoding="utf-8")



class ValleyPRODExtract:
    """Context manager that holds an ODBC connection and runs term queries."""

    def __init__(self):
        self._conn = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.close()

    def connect(self):
        """Open a thin-mode oracledb connection to ValleyPROD using host/port/service from .env."""
        config.require_oracle_config()
        host    = config.VALLEYPROD_HOST
        port    = config.VALLEYPROD_PORT
        service = config.VALLEYPROD_SERVICE
        user    = config.VALLEYPROD_USER
        logger.info(
            "Connecting to ValleyPROD (%s:%s/%s) as '%s' ...",
            host, port, service, user,
        )

        dsn = "{}:{}/{}".format(host, port, service)
        self._conn = oracledb.connect(user=user, password=config.VALLEYPROD_PASSWORD, dsn=dsn)
        logger.info("ValleyPROD connection established (thin mode).")

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
            logger.info("ValleyPROD ODBC connection closed.")

    def _query(self, sql_file, term_code):
        """
        Execute a SQL file, substituting :term_code bind variables,
        and return the results as a DataFrame.
        """
        if not self._conn:
            raise RuntimeError("Not connected. Use 'with ValleyPRODExtract() as vp:'")

        sql = _load_sql(sql_file)

        logger.debug("Running %s for term %s ...", sql_file, term_code)
        cursor = self._conn.cursor()
        try:
            cursor.execute(sql, term_code=term_code)
            columns = [col[0].lower() for col in cursor.description]
            rows = cursor.fetchall()
        finally:
            cursor.close()

        df = pd.DataFrame([tuple(row) for row in rows], columns=columns)
        logger.info("%s -> %d rows for term %s", sql_file, len(df), term_code)
        return df

    def get_students(self, term_code):
        """
        Return student records for the term.
        Columns: pidm, term_code, student_id, first_name, middle_name,
                 last_name, email, ft_pt_status
        """
        return self._query("students.sql", term_code)

    def get_enrollments(self, term_code):
        """
        Return student + faculty section enrollments.
        Columns: section_id, user_id, role, status
        """
        return self._query("enrollments.sql", term_code)

    def get_courses(self, term_code):
        """
        Return course sections for the term.
        Columns: course_id, section_id, short_name, long_name, term_id,
                 account_id, status, format, start_date, end_date
        """
        return self._query("courses.sql", term_code)
