from transit.transform import dbt_environment


def test_dbt_configuration_stays_in_environment_and_decodes_url_password():
    env = dbt_environment("postgresql://tester:p%40ss@db.example:5544/transit?sslmode=require")
    assert env["DBT_USER"] == "tester"
    assert env["DBT_PASSWORD"] == "p@ss"
    assert env["DBT_HOST"] == "db.example"
    assert env["DBT_PORT"] == "5544"
    assert env["DBT_DATABASE"] == "transit"
    assert env["DBT_SSLMODE"] == "require"
