import csv
import os
import sys
import asyncio
import asyncpg
from datetime import datetime

# Database URL from environment
DATABASE_URL = os.environ.get("DATABASE_URL")

async def import_rnc(csv_path):
    if not DATABASE_URL:
        print("Error: DATABASE_URL not set")
        return

    print(f"Connecting to database...")
    conn = await asyncpg.connect(DATABASE_URL)
    
    print(f"Opening CSV file: {csv_path}")
    try:
        # Use latin-1 or iso-8859-1 for DGII files
        with open(csv_path, mode='r', encoding='iso-8859-1') as f:
            reader = csv.DictReader(f)
            
            count = 0
            batch = []
            batch_size = 5000
            
            print("Importing data in batches...")
            for row in reader:
                # Map CSV columns to DB columns
                # RNC,RAZÓN SOCIAL,ACTIVIDAD ECONÓMICA,FECHA DE INICIO OPERACIONES,ESTADO,RÉGIMEN DE PAGO
                # Note: Dictionary keys might be tricky due to encoding, let's use list indexing or clean keys
                
                # Clean keys
                clean_row = {k.strip(): v for k, v in row.items()}
                
                rnc = clean_row.get('RNC', '').replace('"', '').strip()
                razon = clean_row.get('RAZÓN SOCIAL', clean_row.get('RAZN SOCIAL', '')).strip()
                actividad = clean_row.get('ACTIVIDAD ECONÓMICA', clean_row.get('ACTIVIDAD ECONMICA', '')).strip()
                fecha_str = clean_row.get('FECHA DE INICIO OPERACIONES', '').strip()
                estado = clean_row.get('ESTADO', '').strip()
                regimen = clean_row.get('RÉGIMEN DE PAGO', clean_row.get('RGIMEN DE PAGO', '')).strip()
                
                fecha = None
                if fecha_str:
                    try:
                        fecha = datetime.strptime(fecha_str, '%d/%m/%Y').date()
                    except ValueError:
                        pass

                batch.append((rnc, razon, actividad, fecha, estado, regimen))
                
                if len(batch) >= batch_size:
                    await conn.executemany("""
                        INSERT INTO public.dgii_rnc (rnc, razon_social, actividad_economica, fecha_inicio, estado, regimen_pago)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        ON CONFLICT (rnc) DO UPDATE SET
                            razon_social = EXCLUDED.razon_social,
                            estado = EXCLUDED.estado
                    """, batch)
                    count += len(batch)
                    batch = []
                    print(f"Imported {count} records...")

            # Final batch
            if batch:
                await conn.executemany("""
                    INSERT INTO public.dgii_rnc (rnc, razon_social, actividad_economica, fecha_inicio, estado, regimen_pago)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (rnc) DO UPDATE SET
                        razon_social = EXCLUDED.razon_social,
                        estado = EXCLUDED.estado
                """, batch)
                count += len(batch)
            
            print(f"Finished. Total records imported: {count}")

    except Exception as e:
        print(f"Error: {e}")
    finally:
        await conn.close()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/importar_rnc.py <path_to_csv>")
        sys.exit(1)
    
    asyncio.run(import_rnc(sys.argv[1]))
