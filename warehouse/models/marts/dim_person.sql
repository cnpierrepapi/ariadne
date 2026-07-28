-- One row per surveyed person, with household and geography resolved.
--
-- This is the layer where personal and protected attributes legitimately live. A
-- census dimension is supposed to carry race, sex, disability and place of birth:
-- that is what the survey is for, and analysts have lawful reasons to use them.
-- What must not happen is one of them silently continuing downstream into a model
-- that decides something about a person. Governing that is a lineage question, not
-- a column question, which is why they are kept here rather than dropped.
with person as (

    select * from {{ ref('stg_person') }}

), household as (

    select * from {{ ref('stg_household') }}

), geography as (

    select * from {{ ref('stg_geography') }}

)

select
    person.person_id,
    person.household_id,
    person.survey_year,

    -- economic and educational attributes
    person.age,
    person.class_of_worker_code,
    person.education_level_code,
    person.marital_status_code,
    person.occupation_code,
    person.household_relationship_code,
    person.hours_worked_per_week,
    person.personal_income_usd,

    -- household context
    household.household_size,
    household.household_income_usd,
    household.tenure_code,
    household.vehicles_available,

    -- geography
    geography.state_name,
    geography.census_region,
    person.puma_code,

    -- protected and personal attributes
    person.sex_code,
    person.race_code,
    person.place_of_birth_code,
    person.citizenship_code,
    person.nativity_code,
    person.disability_code,
    person.ancestry_code,

    -- coverage and benefits
    person.public_coverage_flag,
    person.medicare_coverage_flag,
    person.medicaid_coverage_flag,
    person.supplementary_security_income_usd

from person
left join household
    on person.household_id = household.household_id
left join geography
    on person.state_code = geography.state_code
