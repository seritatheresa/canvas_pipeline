-- queries/students.sql
-- Returns every actively-registered student for the given term.
--
-- Bind variable:  :term_code  (e.g. '202620')
--
-- Email: constructed as lower(first_name).lower(last_name)@mvsu.edu,
-- with any character that isn't a letter (spaces, apostrophes, hyphens,
-- etc.) stripped from each name first.
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
    LOWER(REGEXP_REPLACE(spriden.spriden_first_name, '[^A-Za-z]', ''))
        || '.'
        || LOWER(REGEXP_REPLACE(spriden.spriden_last_name, '[^A-Za-z]', ''))
        || '@mvsu.edu'                                          AS email,
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
WHERE sfrstcr.sfrstcr_term_code = :term_code
AND   sfrstcr.sfrstcr_rsts_code IS NOT NULL
-- Exclude placeholder "do not use" records
AND   LOWER(spriden.spriden_last_name)  NOT LIKE '%do%not%use%'
AND   LOWER(spriden.spriden_first_name) NOT LIKE '%do%not%use%'
