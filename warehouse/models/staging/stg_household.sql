-- Household records, one row per household. The person layer joins onto this for
-- context a person record cannot carry on its own: how many people share the
-- household, what it earns in total, whether it is owned or rented.
with source as (

    select * from {{ source('census_raw', 'raw_household') }}

)

select
    serialno            as household_id,
    st                  as state_code,
    puma                as puma_code,
    survey_year,
    np                  as household_size,
    hincp               as household_income_usd,
    ten                 as tenure_code,
    veh                 as vehicles_available,
    ybl                 as year_built_code,
    bdsp                as bedrooms

from source
