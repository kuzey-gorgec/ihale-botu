"""
TED (Tenders Electronic Daily) Search API - kesif/probe script (v3).

v2'de netlesenler:
  - notice-type sozlugu: cn-standard = ihale ilani, can-standard = SONUC/KAZANAN
    ilani, corr = duzeltme, pin-rtl/pin-only = on bilgi ilani
  - toplam 1375 kayit var (2016'dan beri), varsayilan siralama tarihe gore
    degil - tarih filtresi eklememiz lazim
  - winner-name/winner-country/total-value alan adlari 400 vermedi (kabul
    edildi), ama ornek cn-standard tipindeydi - bos olmasi normal

v3'te: sadece can-standard (sonuc ilani) + son ~2 yil ile filtreleyip
winner-name'in gercekten dolu gelip gelmedigini goruyoruz.

Kullanim:
    python scripts/ted_probe.py
"""
import json

import requests

TED_SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"

FIBER_CPV_CODES = ["32562000", "32562300", "45232300", "45232310"]


def build_query(notice_type: str | None = None, since_date: str | None = None) -> str:
    cpv_clause = " OR ".join(f"classification-cpv={code}*" for code in FIBER_CPV_CODES)
    parts = [f"({cpv_clause})", "place-of-performance=DEU"]
    if notice_type:
        parts.append(f"notice-type={notice_type}")
    if since_date:
        # TED format bekliyor: YYYYMMDD (tiresiz) ya da today(-N)
        parts.append(f"publication-date>={since_date.replace('-', '')}")
    return " AND ".join(parts)


def post(body: dict) -> requests.Response:
    print("Gonderilen sorgu govdesi:")
    print(json.dumps(body, indent=2, ensure_ascii=False))
    print("-" * 60)
    resp = requests.post(TED_SEARCH_URL, json=body, timeout=30)
    print(f"HTTP {resp.status_code}")
    return resp


def main() -> None:
    print("=" * 60)
    print("Sadece can-standard (sonuc/kazanan ilani) + 2023 sonrasi")
    print("=" * 60)

    body = {
        "query": build_query(notice_type="can-standard", since_date="2023-01-01"),
        "fields": [
            "publication-number",
            "notice-title",
            "buyer-name",
            "winner-name",
            "winner-country",
            "total-value",
            "publication-date",
            "notice-type",
        ],
        "page": 1,
        "limit": 15,
        "scope": "ALL",
    }
    resp = post(body)
    print("-" * 60)

    if resp.status_code != 200:
        print(resp.text[:2000])
        return

    data = resp.json()
    notices = data.get("notices", [])
    total = data.get("totalNoticeCount") or data.get("total") or "?"
    print(f"Toplam eslesen: {total} | bu sayfada: {len(notices)}")
    print()

    for n in notices:
        buyer = n.get("buyer-name", {})
        buyer_name = next(iter(buyer.values()), buyer) if isinstance(buyer, dict) else buyer
        winner = n.get("winner-name", "YOK")
        value = n.get("total-value", "YOK")
        print(f"- {n.get('publication-date')} | {n.get('publication-number')}")
        print(f"    Alici       : {buyer_name}")
        print(f"    Kazanan     : {winner}")
        print(f"    Deger       : {value}")
        print()

    # Ham JSON'u da dosyaya yaz - alan isimlerini tam gormek icin
    with open("ted_probe_raw.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print("Ham cevap ted_probe_raw.json dosyasina yazildi.")


if __name__ == "__main__":
    main()
