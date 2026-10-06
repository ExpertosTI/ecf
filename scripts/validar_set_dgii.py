#!/usr/bin/env python3
"""Valida el Excel del set de datos DGII contra los XSD locales.

No envía comprobantes. Un rechazo en el portal reinicia el set, así que
este script es el paso previo obligatorio.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ecf_core.set_datos import cargar_set


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xlsx", type=Path, help="Excel descargado del portal de certificación")
    args = parser.parse_args()
    if not args.xlsx.is_file():
        print(f"No existe {args.xlsx}", file=sys.stderr)
        return 2

    ecf, rfce = cargar_set(args.xlsx)
    fallos = [f for f in ecf + rfce if f.errores]
    recepcion = sum(1 for f in ecf if f.destino == "recepcion")
    menor = sum(1 for f in ecf if f.destino == "consumo_menor")
    print(
        f"ECF {len(ecf) - sum(1 for f in ecf if f.errores)}/{len(ecf)}  "
        f"recepción={recepcion}  consumo menor={menor}  "
        f"RFCE {len(rfce) - sum(1 for f in rfce if f.errores)}/{len(rfce)}"
    )
    for fila in fallos:
        print(f"{fila.destino} tipo {fila.tipo} {fila.encf}")
        for error in fila.errores[:4]:
            print(f"  {error}")
    if fallos:
        return 1
    print("Set válido en local. No se envió nada a la DGII.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
