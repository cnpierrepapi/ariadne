-- Person-level census records, typed and named for analysts rather than for the
-- Census Bureau's codebook. The PUMS codes are preserved as the source of truth
-- upstream; this is the first layer where a column name means something on sight.
with source as (

    select * from {{ source('census_raw', 'raw_person') }}

)

select
    serialno                                    as household_id,
    serialno || '-' || lpad(sporder::text, 2, '0') as person_id,
    sporder                                     as person_number_in_household,
    st                                          as state_code,
    puma                                        as puma_code,
    survey_year,

    -- what the income model is allowed to reason from
    agep                                        as age,
    cow                                         as class_of_worker_code,
    schl                                        as education_level_code,
    mar                                         as marital_status_code,
    occp                                        as occupation_code,
    relp                                        as household_relationship_code,
    wkhp                                        as hours_worked_per_week,
    pincp                                       as personal_income_usd,

    -- personal and protected attributes. carried through the staging layer because
    -- the warehouse is the system of record for them, and deliberately governed
    -- downstream rather than dropped here: you cannot enforce a rule about data you
    -- have thrown away, and analysts have legitimate uses for these.
    sex                                         as sex_code,
    rac1p                                       as race_code,
    pobp                                        as place_of_birth_code,
    cit                                         as citizenship_code,
    nativity                                    as nativity_code,
    dis                                         as disability_code,
    anc1p                                       as ancestry_code,

    -- coverage and benefits. an operational block: which public programmes a person
    -- is covered by, and what they received. a benefits team needs these and has
    -- every right to them. none of them is a disability field, and under 65 all of
    -- them are close to one.
    pubcov                                      as public_coverage_flag,
    hins3                                       as medicare_coverage_flag,
    hins4                                       as medicaid_coverage_flag,
    ssip                                        as supplementary_security_income_usd

from source
where agep is not null
