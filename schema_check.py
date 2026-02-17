def print_schema_debug():
    conn = get_conn()
    cur = conn.cursor()
    try:
        db_execute(cur, """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name='users'
              AND column_name IN ('accepted_policies','accepted_policies_at','plan')
            ORDER BY column_name
        """)
        rows = _fetchall(cur)
        print("USERS COL TYPES:", rows)

        db_execute(cur, """
            SELECT COUNT(*) FROM credit_grants
        """)
        print("credit_grants count:", _fetchone(cur)[0])
    finally:
        conn.close()