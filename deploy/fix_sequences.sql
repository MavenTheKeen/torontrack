-- After restoring a dump from a logical-replication subscriber: replication copies rows
-- but not sequence positions, so move every owned sequence past its column's maximum.
DO $$
DECLARE r record; n bigint;
BEGIN
  FOR r IN
    SELECT s.oid::regclass AS seq, c.oid::regclass AS tbl, a.attname AS col
    FROM pg_class s
    JOIN pg_depend d ON d.objid = s.oid AND d.deptype IN ('a', 'i')
    JOIN pg_class c ON c.oid = d.refobjid
    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = d.refobjsubid
    WHERE s.relkind = 'S'
  LOOP
    EXECUTE format('SELECT coalesce(max(%I), 0) FROM %s', r.col, r.tbl) INTO n;
    PERFORM setval(r.seq, greatest(n, 1), n > 0);
  END LOOP;
END $$;
