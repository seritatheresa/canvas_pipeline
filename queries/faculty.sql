-- queries/faculty.sql
-- Returns every instructor assigned to a section in the given term.
--
-- Bind variable:  :term_code  (e.g. '202620')
--
-- Source: SIRASGN joined to SSBSECT — deliberately the same pair, with the
-- same primary-instructor filter, that produces the 'teacher' rows in
-- enrollments.sql.  Keeping both queries on one source is what guarantees
-- every teacher enrollment has a matching users.csv row; if they drift
-- apart, Canvas rejects those enrollments with "user not found".
--
-- Email: constructed as lower(first_name).lower(last_name)@mvsu.edu, with
-- any character that isn't a letter stripped from each name first — the
-- same convention students.sql uses.  Banner email addresses (GOREMAL) are
-- not used.
--
-- Column names match students.sql except for the SPRIDEN_ID, which is
-- user_id here rather than student_id.

SELECT DISTINCT
    sirasgn.sirasgn_pidm                                        AS integration_id,
    sirasgn.sirasgn_term_code                                   AS term_code,
    spriden.spriden_id                                          AS user_id,
    spriden.spriden_first_name                                  AS first_name,
    spriden.spriden_mi                                          AS middle_name,
    spriden.spriden_last_name                                   AS last_name,
    LOWER(REGEXP_REPLACE(spriden.spriden_first_name, '[^A-Za-z]', ''))
        || '.'
        || LOWER(REGEXP_REPLACE(spriden.spriden_last_name, '[^A-Za-z]', ''))
        || '@mvsu.edu'                                          AS email
FROM sirasgn
JOIN ssbsect
    ON  ssbsect.ssbsect_crn       = sirasgn.sirasgn_crn
    AND ssbsect.ssbsect_term_code = sirasgn.sirasgn_term_code
JOIN spriden
    ON  spriden.spriden_pidm       = sirasgn.sirasgn_pidm
    AND spriden.spriden_change_ind IS NULL
WHERE sirasgn.sirasgn_term_code = :term_code
AND   NVL(sirasgn.sirasgn_primary_ind, 'Y') = 'Y'
-- Exclude placeholder "do not use" records
AND   LOWER(spriden.spriden_last_name)  NOT LIKE '%do%not%use%'
AND   LOWER(spriden.spriden_first_name) NOT LIKE '%do%not%use%'
ORDER BY last_name, first_name
