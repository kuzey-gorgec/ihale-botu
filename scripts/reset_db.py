"""
Test/temiz-baslangic amacli - TUM veri tablolarini bosaltir (sema/tablo
yapisi kalir, sadece icindeki satirlar silinir).

DIKKAT: Bu islem GERI ALINAMAZ. Sadece test verisini temizlemek veya
sifirdan temiz bir calistirma yapmak icin kullan.

Kullanim:
    python scripts/reset_db.py --yes
"""
import argparse
import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

TABLES = [
    "classification_training_data",
    "email_drafts",
    "tender_contacts",
    "contacts",
    "tender_companies",
    "tenders",
    "companies",
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--yes", action="store_true",
                         help="onay bayragi - bu olmadan calismaz (yanlislikla calistirmayi zorlastirmak icin)")
    args = parser.parse_args()

    if not args.yes:
        sys.exit(
            "Bu komut TUM tablolardaki veriyi GERI ALINAMAZ sekilde siler.\n"
            "Emin isen: python scripts/reset_db.py --yes"
        )

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        sys.exit("HATA: DATABASE_URL tanimli degil (.env dosyasina bak)")

    safe_target = database_url.split("@")[-1] if "@" in database_url else database_url
    print(f"Baglaniliyor: {safe_target}")

    conn = psycopg2.connect(database_url)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")
        print(f"Tum tablolar bosaltildi (sema korundu): {', '.join(TABLES)}")
    except Exception as exc:
        conn.rollback()
        sys.exit(f"HATA: bosaltma basarisiz: {exc}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
