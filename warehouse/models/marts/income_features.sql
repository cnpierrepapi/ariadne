-- The feature table the income classifier trains and serves on.
--
-- Modelled on the canonical ACSIncome task, minus the protected attributes that
-- task ships with. The published benchmark feature list includes SEX and RAC1P;
-- this table deliberately does not, because a model that decides something about a
-- person's income must not reason from a prohibited basis.
--
-- Nothing enforces that. It is a convention held in place by whoever last edited
-- this file, which is exactly why it needs watching from outside.
with person as (

    select * from {{ ref('dim_person') }}

)

select
    person_id,
    survey_year,

    age,
    class_of_worker_code,
    education_level_code,
    marital_status_code,
    occupation_code,
    hours_worked_per_week,

    household_size,
    household_income_usd,
    tenure_code,

    census_region,

    case when personal_income_usd > 50000 then 1 else 0 end as income_above_50k

from person
where age >= 16
  and hours_worked_per_week is not null
  and personal_income_usd is not null
