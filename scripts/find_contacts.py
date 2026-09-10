"""
Kisi/iletisim bulma katmani - ihalede rolu olan (issuer/winner) sirketler icin
LinkedIn/Xing'de rol bazli isimli kisi arar (SerpAPI uzerinden gercek Google
sonuclari, site:linkedin.com / site:xing.com ile kisitli). Insan onayina
sunulmak uzere DB'ye verified=false olarak yazar - hicbir profil sayfasi
scrape edilmez, sadece arama sonucu linki/ozeti kaydedilir; karari/
dogrulamayi kullanici yapar.

Not: Google'in kendi Custom Search JSON API'si artik yeni musterilere kapali
(resmi olarak "closed to new customers" - konfigurasyonla ilgisi yok, hicbir
yeni Google Cloud projesi kullanamiyor), bu yuzden SerpAPI gibi ucuncu parti
bir servis kullaniliyor. Ayrica sirketin resmi sitesini otomatik bulma adimi
yok - bunun yerine tek sirket icin --website ile URL verebilirsin, script o
sayfadan (+ /impressum, /kontakt) eposta/telefon cikarir.

Kurulum (ucretsiz):
  1. https://serpapi.com/users/sign_up -> ucretsiz hesap ac (ayda 250 sorgu,
     aylik yenilenir).
  2. Dashboard'daki "Your Private API Key"i SERPAPI_API_KEY olarak .env'e yaz.

Kullanim:
  python scripts/find_contacts.py --limit 10                     # kontrolsuz sirketlerden 10 tane isle
  python scripts/find_contacts.py --limit 10 --auto-website       # + her sirket icin resmi site keşfet
  python scripts/find_contacts.py --company-id 5                 # tek bir sirket, sadece rol bazli arama
  python scripts/find_contacts.py --company-id 5 --website https://firma.de   # + genel iletisim de dene
  python scripts/find_contacts.py --dry-run --limit 3            # DB'ye yazmadan sadece goster

  --auto-website  toplu modda (--company-id verilmeden) her sirket icin
                  once resmi web sitesini SerpAPI genel aramasiyla (site:
                  kisitlamasi OLMADAN) bulmayi dener, sonra oradan
                  eposta/telefon cikarir. Kisi basina isim/pozisyon disinda
                  hicbir zaman eposta/telefon donmemesi sikayeti bunun
                  icin var - normal rol aramasi (linkedin/xing) hicbir
                  zaman profil sayfasini cekmiyor, sadece bu bayrak
                  eposta/telefon getirebilir. Sirket basina +1 SerpAPI
                  sorgusu harcar (250/ay ucretsiz kotayi daha hizli tuketir).
"""
import argparse
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import psycopg2
import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

SERPAPI_URL = "https://serpapi.com/search.json"

HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ihale-botu-contact-finder/1.0)"}

# Fiber/tiefbau ihaleleri icin muhatap olabilecek roller - genislet/degistir istersen.
ROLE_KEYWORDS = ["Geschäftsführer", "Bauleiter", "Projektleiter", "Niederlassungsleiter", "Einkauf"]

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
PHONE_RE = re.compile(r"(\+49[\s/\-]?\(?0?\)?[\d][\d\s/\-]{7,}\d)|(\b0\d{2,5}[\s/\-]\d{3,}[\s/\-]?\d{0,}\b)")


def serp_search(query: str, num: int = 5) -> list[dict]:
    """SerpAPI uzerinden gercek Google arama sonucu doner (title/link/snippet).
    API anahtari her cagrida taze okunuyor (modul-seviyesi sabit degil) -
    boylece dashboard.py gibi calisma anindan sonra os.environ'a yazan bir
    cagiran da dogru anahtari kullanabiliyor."""
    api_key = os.getenv("SERPAPI_API_KEY", "")
    if not api_key:
        return []
    params = {
        "q": query, "api_key": api_key, "engine": "google",
        "num": num, "hl": "de", "gl": "de",
    }
    try:
        resp = requests.get(SERPAPI_URL, params=params, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"    [HATA] SerpAPI sorgusu basarisiz: {exc}")
        if getattr(exc, "response", None) is not None:
            print(f"    yanit: {exc.response.text[:300]}")
        return []
    data = resp.json()
    if "error" in data:
        print(f"    [HATA] SerpAPI: {data['error']}")
        return []
    return data.get("organic_results", [])


def extract_contact_from_page(url: str) -> dict:
    """Verilen sayfadan (ve olasi /impressum, /kontakt alt sayfalarindan) eposta/telefon cikarir."""
    candidates = [url, url.rstrip("/") + "/impressum", url.rstrip("/") + "/kontakt"]
    for page_url in candidates:
        try:
            resp = requests.get(page_url, headers=HTTP_HEADERS, timeout=15)
            if resp.status_code != 200:
                continue
        except requests.RequestException:
            continue
        text = resp.text
        emails = EMAIL_RE.findall(text)
        phones = [m[0] or m[1] for m in PHONE_RE.findall(text)]
        emails = [e for e in emails if not e.lower().endswith((".png", ".jpg", ".gif"))]
        if emails or phones:
            return {
                "email": emails[0] if emails else None,
                "phone": phones[0].strip() if phones else None,
                "source_url": page_url,
            }
    return {}


# site: kisitlamasi olmadan genel arama yapinca Google'in genelde dondugu,
# sirketin KENDI sitesi olmayan yaygin alan adlari - resmi site keşfinde elenir.
NON_OFFICIAL_DOMAINS = {
    "linkedin.com", "xing.com", "facebook.com", "instagram.com", "twitter.com",
    "x.com", "youtube.com", "wikipedia.org", "northdata.de", "northdata.com",
    "bundesanzeiger.de", "handelsregister.de", "unternehmensregister.de",
    "kununu.com", "indeed.com", "glassdoor.com", "glassdoor.de",
    "dastelefonbuch.de", "gelbeseiten.de", "wlw.de", "europages.de",
    "europages.com", "opencorporates.com", "google.com", "bing.com",
    "firmenwissen.de", "creditreform.de", "dnb.com", "yelp.de", "yelp.com",
}


def _looks_like_official_site(url: str) -> bool:
    if not url:
        return False
    netloc = urlparse(url).netloc.lower().removeprefix("www.")
    if "." not in netloc:
        return False
    # tam eslesme yetmiyor: Google cogu zaman "de.linkedin.com",
    # "en.wikipedia.org" gibi ulke/dil alt-alan adlari donuyor - bunlari da
    # yakalamak icin subdomain'leri de kontrol ediyoruz.
    return not any(netloc == blocked or netloc.endswith(f".{blocked}") for blocked in NON_OFFICIAL_DOMAINS)


def discover_official_website(company_name: str) -> str | None:
    """Sirketin resmi sitesini site: kisitlamasi OLMADAN genel bir Google
    aramasiyla (SerpAPI uzerinden) bulmaya calisir - ilk 'resmi site gibi
    gorunen' sonucu doner, bulamazsa None."""
    for item in serp_search(f'"{company_name}" impressum OR kontakt', num=5):
        link = item.get("link", "")
        if _looks_like_official_site(link):
            parsed = urlparse(link)
            return f"{parsed.scheme}://{parsed.netloc}"
    return None


NAME_SEPARATOR_RE = re.compile(r"\s[-–|]\s")


def _is_valid_profile_url(url: str, path_marker: str) -> bool:
    """Sonucun gercekten hedeflenen sitede bir profil sayfasi olup olmadigini
    dogrular - Google bazen site: kisitlamasini yok sayip alakasiz sonuc
    donebiliyor (youtube/facebook/northdata vb.), bunlari eliyoruz."""
    if not url:
        return False
    netloc = urlparse(url).netloc.lower()
    path = urlparse(url).path.lower()
    if path_marker == "linkedin.com/in":
        return "linkedin.com" in netloc and path.startswith("/in/")
    if path_marker == "xing.com/profile":
        return "xing.com" in netloc and path.startswith("/profile/")
    return False


def find_role_contacts(company_name: str) -> list[dict]:
    """LinkedIn/Xing'de rol bazli isimli kisi arar - profil sayfasi cekilmez, sadece arama sonucu kullanilir."""
    role_filter = " OR ".join(ROLE_KEYWORDS)
    results = []
    for site, source_label in (("linkedin.com/in", "linkedin"), ("xing.com/profile", "xing")):
        query = f'site:{site} "{company_name}" ({role_filter})'
        for item in serp_search(query, num=5):
            link = item.get("link", "")
            if not _is_valid_profile_url(link, site):
                continue
            title = item.get("title", "")
            full_name = NAME_SEPARATOR_RE.split(title)[0].strip()
            results.append({
                "full_name": full_name,
                "title": title,
                "url": link,
                "snippet": item.get("snippet"),
                "source": source_label,
            })
        time.sleep(1)
    return results


def get_companies_needing_contacts(cur, limit: int) -> list[tuple]:
    cur.execute(
        """
        SELECT DISTINCT c.id, c.name
        FROM companies c
        JOIN tender_companies tc ON tc.company_id = c.id
        LEFT JOIN contacts ct ON ct.company_id = c.id
        WHERE ct.id IS NULL
        ORDER BY c.id DESC
        LIMIT %s
        """,
        (limit,),
    )
    return cur.fetchall()


def get_company_by_id(cur, company_id: int) -> tuple | None:
    cur.execute("SELECT id, name FROM companies WHERE id = %s", (company_id,))
    return cur.fetchone()


def save_contact(cur, company_id: int, full_name: str | None, title: str | None,
                  email: str | None, phone: str | None, source: str) -> int:
    cur.execute(
        """
        INSERT INTO contacts (company_id, full_name, title, email, phone, source, verified)
        VALUES (%s, %s, %s, %s, %s, %s, false)
        RETURNING id
        """,
        (company_id, full_name, title, email, phone, source),
    )
    return cur.fetchone()[0]


def link_contact_to_company_tenders(cur, contact_id: int, company_id: int) -> None:
    cur.execute("SELECT tender_id, role FROM tender_companies WHERE company_id = %s", (company_id,))
    for tender_id, role in cur.fetchall():
        cur.execute(
            "INSERT INTO tender_contacts (tender_id, contact_id, relevance_role) VALUES (%s, %s, %s)",
            (tender_id, contact_id, role),
        )


def process_company(cur, company_id: int, company_name: str, dry_run: bool,
                     website: str | None = None, auto_discover_website: bool = False,
                     log=print) -> int:
    log(f"\n--- {company_name} (id={company_id})")
    saved = 0

    if not website and auto_discover_website:
        website = discover_official_website(company_name)
        if website:
            log(f"  Resmi site bulundu: {website}")
        else:
            log("  Resmi site bulunamadi (genel arama sonucsuz ya da hep bilinen 3.parti siteler).")
        time.sleep(1)

    if website:
        general = extract_contact_from_page(website)
        if general:
            log(f"  Genel iletisim ({website}): {general.get('email')} / {general.get('phone')}")
            if not dry_run:
                cid = save_contact(cur, company_id, None, None, general.get("email"),
                                    general.get("phone"), "website")
                link_contact_to_company_tenders(cur, cid, company_id)
                saved += 1
        else:
            log(f"  Verilen sitede ({website}) eposta/telefon bulunamadi.")

    role_contacts = find_role_contacts(company_name)
    if not role_contacts:
        log("  LinkedIn/Xing'de role uyan sonuc bulunamadi.")
    for rc in role_contacts:
        log(f"  [{rc['source']}] {rc['full_name']} - {rc['url']}")
        if not dry_run:
            cid = save_contact(cur, company_id, rc["full_name"], rc["title"], None, None, rc["source"])
            link_contact_to_company_tenders(cur, cid, company_id)
            saved += 1

    return saved


def run_find_contacts(limit: int = 10, company_id: int | None = None, website: str | None = None,
                       dry_run: bool = False, auto_discover_website: bool = False, log=print) -> dict:
    """Tum kisi-bulma akisini calistirir. CLI (main()) ve dashboard.py'nin
    "Yeni Tarama" ekrani tarafindan ortak kullanilir."""
    if website and not company_id:
        raise RuntimeError("--website sadece --company-id ile birlikte kullanilabilir.")

    if not os.getenv("SERPAPI_API_KEY"):
        raise RuntimeError(
            "SERPAPI_API_KEY tanimli degil. Bu dosyanin ustundeki docstring'de ucretsiz "
            "kurulum adimlari var - .env dosyana (ya da Streamlit secrets'a) ekleyip tekrar dene."
        )

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL tanimli degil (.env dosyasina ya da Streamlit secrets'a bak)")

    conn = psycopg2.connect(database_url)
    cur = conn.cursor()

    try:
        if company_id:
            row = get_company_by_id(cur, company_id)
            companies = [row] if row else []
        else:
            companies = get_companies_needing_contacts(cur, limit)

        if not companies:
            log("Islenecek sirket yok (hepsi zaten kontrol edilmis ya da id bulunamadi).")
            return {"companies_processed": 0, "total_saved": 0}

        total_saved = 0
        for cid, company_name in companies:
            total_saved += process_company(
                cur, cid, company_name, dry_run, website,
                auto_discover_website=auto_discover_website, log=log,
            )
            if not dry_run:
                conn.commit()
            time.sleep(1)

        log(f"\nToplam kaydedilen iletisim: {total_saved} (hepsi verified=false, dashboard'dan onay bekliyor)")
        return {"companies_processed": len(companies), "total_saved": total_saved}
    finally:
        cur.close()
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=10, help="kac sirket islensin (varsayilan: 10)")
    parser.add_argument("--company-id", type=int, default=None, help="sadece bu sirket id'sini isle")
    parser.add_argument("--website", default=None,
                         help="--company-id ile birlikte: sirketin resmi sitesi, genel iletisim icin denenir")
    parser.add_argument("--auto-website", action="store_true",
                         help="toplu modda her sirket icin resmi siteyi otomatik kesfetmeyi dener")
    parser.add_argument("--dry-run", action="store_true", help="DB'ye yazmadan sadece goster")
    args = parser.parse_args()

    try:
        run_find_contacts(
            limit=args.limit, company_id=args.company_id, website=args.website,
            dry_run=args.dry_run, auto_discover_website=args.auto_website,
        )
    except RuntimeError as exc:
        sys.exit(f"HATA: {exc}")


if __name__ == "__main__":
    main()
