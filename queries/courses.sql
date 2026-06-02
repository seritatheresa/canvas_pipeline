-- queries/courses.sql
-- Returns course sections for the given term, ready for Canvas SIS import.
--
-- Bind variable:  :term_code  (e.g. '202620')
--
-- Output columns:
--   course_id       — unique Canvas course identifier
--   section_id      — TERM + CRN  (used in enrollments.sql too)
--   short_name      — SUBJ CRSE_NUMB SEQ_NUMB
--   long_name       — course title from catalog
--   account_id      — sub-account derived from subject code
--   term_id         — term_code (Canvas enrollment term sis_id)
--   status          — active | deleted
--   integration_id  — TERM + CRN
--   format          — on_campus | online | blended
--   start_date      — section start (ISO-8601)
--   end_date        — section end   (ISO-8601)
--
-- Replaces: 202620_enrollments.sql (the courses half), newCourses.csv workflows

SELECT DISTINCT
    -- Canvas course_id: TERM + SUBJ + CRSE + SEQ + CRN
    ssbsect.ssbsect_term_code
        || ssbsect.ssbsect_subj_code
        || ssbsect.ssbsect_crse_numb
        || ssbsect.ssbsect_seq_numb
        || ssbsect.ssbsect_crn                          AS course_id,

    -- Section id used in enrollments.csv
    ssbsect.ssbsect_term_code || ssbsect.ssbsect_crn   AS section_id,

    -- Human-readable names
    ssbsect.ssbsect_subj_code
        || ' ' || ssbsect.ssbsect_crse_numb
        || ' ' || ssbsect.ssbsect_seq_numb              AS short_name,
    scbcrse.scbcrse_title                               AS long_name,

    -- Term (Canvas uses sis_id of the enrollment term)
    ssbsect.ssbsect_term_code                           AS term_id,

    -- Sub-account mapping by subject code
    DECODE(ssbsect.ssbsect_subj_code,
        'FY',  'UC',
        'ED',  'ED',   'SE',  'ED',   'RD',  'ED',   'EC',  'ED',
        'MJ',  'MC',   'SP',  'MC',   'MC',  'MC',
        'HI',  'SS',   'SO',  'SS',   'SS',  'SS',   'PS',  'SS',
                       'PA',  'SS',   'RP',  'SS',
        'CJ',  'CJ',
        'RU',  'EN',   'AB',  'EN',   'HD',  'EN',   'SA',  'EN',
               'SK',   'UC',
        'EH',  'NSEH', 'MA',  'MCIS', 'CS',  'MCIS',
        'RE',  'HPER', 'PE',  'HPER', 'HL',  'HPER', 'PED', 'HPER',
        'BF',  'NSEH', 'BI',  'NSEH', 'PH',  'NSEH', 'CH',  'NSEH',
               'SC',   'NSEH',
        'OMP', 'BA',   'AC',  'BA',   'BA',  'BA',
        'AR',  'FA',   'TH',  'FA',   'MU',  'FA',
        'MS',  'ROTC',
        'ET',  'ET',
        'SW',  'SW',
        'EN',  'EN',
               'ROOT'   -- default
    )                                                   AS account_id,

    -- Section status
    DECODE(ssbsect.ssbsect_ssts_code,
           'A', 'active',
                'deleted')                              AS status,

    -- Integration ID: TERM + CRN
    ssbsect.ssbsect_term_code || ssbsect.ssbsect_crn   AS integration_id,

    -- Instructional format derived from section sequence number
    DECODE(ssbsect.ssbsect_seq_numb,
        'E01', 'online', 'E02', 'online', 'E03', 'online', 'E04', 'online',
        'HE1', 'blended','HE2', 'blended','HE3', 'blended',
        'DE1', 'blended','DE2', 'blended','DE3', 'blended','DE4', 'blended',
        'DE5', 'blended','DE6', 'blended','DE7', 'blended','DE8', 'blended',
        'DE9', 'blended','DE10','blended',
        'on_campus')                                    AS format,

    -- Section dates (UTC → ISO-8601)
    -- start_date: use date from Banner with time fixed at 08:00:00 UTC
    TO_CHAR(TRUNC(ssbsect.ssbsect_ptrm_start_date), 'YYYY-MM-DD') || 'T08:00:00Z'
                                                        AS start_date,
    TO_CHAR(
        CAST(ssbsect.ssbsect_ptrm_end_date AS TIMESTAMP) AT TIME ZONE 'UTC',
        'YYYY-MM-DD"T"HH24:MI:SS"Z"'
    )                                                   AS end_date

FROM saturn.ssbsect ssbsect
JOIN saturn.scbcrse scbcrse
    ON  scbcrse.scbcrse_subj_code = ssbsect.ssbsect_subj_code
    AND scbcrse.scbcrse_crse_numb = ssbsect.ssbsect_crse_numb
    AND scbcrse.scbcrse_eff_term  = (
            SELECT MAX(a.scbcrse_eff_term)
            FROM   saturn.scbcrse a
            WHERE  a.scbcrse_subj_code = ssbsect.ssbsect_subj_code
            AND    a.scbcrse_crse_numb = ssbsect.ssbsect_crse_numb
            AND    a.scbcrse_eff_term  <= :term_code
        )
WHERE ssbsect.ssbsect_term_code  = :term_code
AND   ssbsect.ssbsect_ssts_code  = 'A'
ORDER BY section_id
