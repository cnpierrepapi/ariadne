-- The feature table the workforce classifier trains and serves on.
--
-- A different decision from the income model, governed by a different statute. This
-- one estimates whether a working age person holds full time work, which is what a
-- workforce planning or candidate screening model is actually asked to do. Title
-- VII, the ADA and the ADEA apply here, not the Equal Credit Opportunity Act, and
-- they disagree with it: age is permitted in a sound credit scoring system and is
-- flatly protected for workers over forty.
--
-- Disability, race and sex are absent, as are occupation, hours and personal income.
-- The first three because they must not decide anything about a worker. The last
-- three because they are downstream of the answer this table is trying to predict,
-- and a model that scores well by reading the outcome back to itself has measured
-- nothing.
--
-- The same convention as income_features holds this together, and the same nothing
-- enforces it.
with person as (

    select * from {{ ref('dim_person') }}

)

select
    person_id,
    survey_year,

    age,
    education_level_code,
    marital_status_code,

    household_size,
    census_region,

    case when hours_worked_per_week >= 35 then 1 else 0 end as works_full_time

from person
where age between 16 and 64
