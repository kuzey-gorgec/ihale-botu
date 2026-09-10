"""
Veritabani semasini kurar. db/schema.sql dosyasini okuyup
DATABASE_URL baglantisina karsi calistirir. Tekrar calistirmak
guvenlidir (tablolar IF NOT EXISTS ile olusturulur).

Kullanim:
    python scripts/init_db.py
"""
import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

# .env dosyasini proje kokunden yukle
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

SCHEMA_PATH = PROJECT_ROOT / "db" / "schema.sql"


def main() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        sys.exit(
            "HATA: DATABASE_URL tanimli degil.\n"
            "Proje kokunde bir .env dosyasi olustur (.env.example'i kopyalayabilirsin) "
            "ve icine su formatta bir satir ekle:\n"
            "  DATABASE_URL=postgresql://kullanici:sifre@localhost:5432/ihale_botu"
        )

    if not SCHEMA_PATH.exists():
        sys.exit(f"HATA: sema dosyasi bulunamadi: {SCHEMA_PATH}")

    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")

    # sadece host/db kismini logla, sifreyi loglama
    safe_target = database_url.split("@")[-1] if "@" in database_url else database_url
    print(f"Baglaniliyor: {safe_target}")

    conn = psycopg2.connect(database_url)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(schema_sql)
        print("Sema basariyla olusturuldu (7 tablo: companies, tenders, "
              "tender_companies, contacts, tender_contacts, email_drafts, "
              "classification_training_data).")
    except Exception as exc:
        conn.rollback()
        sys.exit(f"HATA: sema calistirilamadi: {exc}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
