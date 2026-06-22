"""
Pre-migration 18.0.5.0.0: renombrar cufe → codigo_seguridad en todos los modelos.

Tablas afectadas:
  - account_move.ecf_cufe → ecf_codigo_seguridad   (account.move)
  - pos_order.ecf_cufe    → ecf_codigo_seguridad   (pos.order)
  - ecf_log.cufe          → codigo_seguridad       (ecf.log)
  - ecf_compra_recibida.cufe → codigo_seguridad    (ecf.compra.recibida)

Idempotente: solo renombra si la columna antigua existe y la nueva no.
"""


def migrate(cr, version):
    renames = [
        ("account_move",         "ecf_cufe", "ecf_codigo_seguridad"),
        ("pos_order",            "ecf_cufe", "ecf_codigo_seguridad"),
        ("ecf_log",              "cufe",     "codigo_seguridad"),
        ("ecf_compra_recibida",  "cufe",     "codigo_seguridad"),
    ]
    for table, old_col, new_col in renames:
        if _column_exists(cr, table, old_col) and not _column_exists(cr, table, new_col):
            cr.execute(f"ALTER TABLE {table} RENAME COLUMN {old_col} TO {new_col}")


def _column_exists(cr, table, column):
    cr.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = %s AND table_schema = current_schema()",
        (table, column),
    )
    return bool(cr.fetchone())

