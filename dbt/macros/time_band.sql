{# Part of the day for on-time buffers, from a local or GTFS hour. #}
{% macro time_band(hour) %}
case
    when {{ hour }} between 6 and 9 then 'am'
    when {{ hour }} between 10 and 14 then 'mid'
    when {{ hour }} between 15 and 18 then 'pm'
    else 'off'
end
{% endmacro %}
