-- queries/students.sql
-- Returns every actively-registered student for the given term.
--
-- Bind variable:  :term_code  (e.g. '202620')
--
-- Email priority: preferred MVSU email > most-recent active MVSU email >
--                 constructed lower(first_name).lower(last_name)@mvsu.edu
--
-- Personal (non-MVSU) emails are never used for login_id or email.
--
-- Replaces the per-term student files:
--   202530_students.sql, 202610_students.sql, 202620_students.sql, etc.

SELECT DISTINCT
    sfrstcr.sfrstcr_pidm                                        AS integration_id,
    sfrstcr.sfrstcr_term_code                                   AS term_code,
    spriden.spriden_id                                          AS student_id,
    spriden.spriden_first_name                                  AS first_name,
    spriden.spriden_mi                                          AS middle_name,
    spriden.spriden_last_name                                   AS last_name,
    CASE
        WHEN LOWER(pref.email)   LIKE '%@mvsu.edu' THEN LOWER(pref.email)
        WHEN LOWER(recent.email) LIKE '%@mvsu.edu' THEN LOWER(recent.email)
        ELSE LOWER(spriden.spriden_first_name)
             || '.'
             || LOWER(spriden.spriden_last_name)
             || '@mvsu.edu'
    END                                                         AS email,
    CASE
        WHEN SUM(sfrstcr.sfrstcr_credit_hr)
             OVER (PARTITION BY sfrstcr.sfrstcr_pidm,
                                sfrstcr.sfrstcr_term_code) >= 12
        THEN 'F'
        ELSE 'P'
    END                                                         AS ft_pt_status
FROM sfrstcr
JOIN spriden
    ON  spriden.spriden_pidm      = sfrstcr.sfrstcr_pidm
    AND spriden.spriden_change_ind IS NULL
JOIN stvrsts
    ON  stvrsts.stvrsts_code      = sfrstcr.sfrstcr_rsts_code
    AND stvrsts.stvrsts_incl_sect_enrl = 'Y'
-- Preferred email
LEFT JOIN (
    SELECT goremal_pidm AS pidm, goremal_email_address AS email
    FROM   goremal
    WHERE  goremal_preferred_ind = 'Y'
    AND    goremal_status_ind    = 'A'
) pref ON pref.pidm = sfrstcr.sfrstcr_pidm
-- Most-recent active email (tie-break by activity date)
LEFT JOIN (
    SELECT g.goremal_pidm AS pidm, g.goremal_email_address AS email
    FROM   goremal g
    JOIN (
        SELECT goremal_pidm, MAX(goremal_activity_date) AS max_date
        FROM   goremal
        WHERE  goremal_status_ind = 'A'
        GROUP BY goremal_pidm
    ) mx ON mx.goremal_pidm        = g.goremal_pidm
         AND mx.max_date           = g.goremal_activity_date
    WHERE g.goremal_status_ind = 'A'
) recent ON recent.pidm = sfrstcr.sfrstcr_pidm
WHERE sfrstcr.sfrstcr_term_code = :term_code
AND   sfrstcr.sfrstcr_rsts_code IS NOT NULL
-- Exclude placeholder "do not use" records
AND   LOWER(spriden.spriden_last_name)  NOT LIKE '%do%not%use%'
AND   LOWER(spriden.spriden_first_name) NOT LIKE '%do%not%use%'
