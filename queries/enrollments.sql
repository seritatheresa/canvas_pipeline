-- queries/enrollments.sql
-- Returns student + faculty section enrollments for Canvas SIS import.
--
-- Bind variable:  :term_code  (e.g. '202620')
--
-- Output columns map directly to Canvas SIS enrollments.csv:
--   section_id  = TERM + CRN  (matches sections.sql)
--   user_id     = ValleyPROD SPRIDEN_ID (used as canvas login_id / user_id)
--   role        = 'student' or 'teacher'
--   status      = 'active' or 'deleted'
--
-- Replaces: 202530_Enrollments.sql, 202610_enrollments.sql, etc.

-- ── Students ──────────────────────────────────────────────────────────────────
SELECT
    sfrstcr.sfrstcr_term_code || sfrstcr.sfrstcr_crn    AS section_id,
    spriden.spriden_id                                  AS user_id,
    'student'                                           AS role,
    DECODE(sfrstcr.sfrstcr_rsts_code,
           'RE', 'active',
           'RW', 'active',
                 'deleted')                             AS status
FROM sfrstcr
JOIN spriden
    ON  spriden.spriden_pidm       = sfrstcr.sfrstcr_pidm
    AND spriden.spriden_change_ind IS NULL
JOIN stvrsts
    ON  stvrsts.stvrsts_code             = sfrstcr.sfrstcr_rsts_code
    AND stvrsts.stvrsts_incl_sect_enrl   = 'Y'
WHERE sfrstcr.sfrstcr_term_code = :term_code
AND   sfrstcr.sfrstcr_rsts_code IS NOT NULL

UNION ALL

-- ── Faculty / Instructors ─────────────────────────────────────────────────────
SELECT
    ssbsect.ssbsect_term_code || ssbsect.ssbsect_crn   AS section_id,
    spriden.spriden_id                                  AS user_id,
    'teacher'                                           AS role,
    DECODE(ssbsect.ssbsect_ssts_code,
           'A', 'active',
                'deleted')                              AS status
FROM sirasgn
JOIN ssbsect
    ON  ssbsect.ssbsect_crn       = sirasgn.sirasgn_crn
    AND ssbsect.ssbsect_term_code = sirasgn.sirasgn_term_code
JOIN spriden
    ON  spriden.spriden_pidm       = sirasgn.sirasgn_pidm
    AND spriden.spriden_change_ind IS NULL
WHERE sirasgn.sirasgn_term_code   = :term_code
AND   NVL(sirasgn.sirasgn_primary_ind, 'Y') = 'Y'

ORDER BY section_id, role, user_id
