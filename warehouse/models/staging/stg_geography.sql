-- The state reference dimension. Small, boring, and the kind of table that is never
-- documented and always joined.
select
    state_code,
    state_name,
    census_region
from {{ source('census_raw', 'raw_geography') }}
