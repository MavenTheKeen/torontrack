{% macro generate_schema_name(custom_schema_name, node) -%}
    {# This single-user project isolates CI with a separate DATABASE, not schema prefixes. #}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
