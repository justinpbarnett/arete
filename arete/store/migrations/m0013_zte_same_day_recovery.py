PHASE = "expand"

SQL = """
DROP INDEX one_daily_zte_attempt;
CREATE UNIQUE INDEX one_daily_zte_attempt ON zte_deliveries (SUBSTR(created, 1, 10))
    WHERE json_type(data, '$.same_day_recovery') IS NOT 'true';
CREATE UNIQUE INDEX one_daily_zte_recovery ON zte_deliveries (SUBSTR(created, 1, 10))
    WHERE json_type(data, '$.same_day_recovery') IS 'true';
"""
