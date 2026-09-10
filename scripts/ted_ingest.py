"""
TED -> Postgres entegrasyon prototipi.

Akis:
  1) TED Search API'den (place-of-performance=DEU, fiber CPV kodlari,
     notice-type=can-standard yani SONUC ilani) ihaleleri bul
  2) Her ilanin TED "eForms" XML export'unu (public, auth gerektirmiyor)
     cekip lot bazinda kazanan firma bilgisini (ad, adres, email,
     telefon) parse et
  3) Siniflandirma: Ollama (yerel) -> Groq (ucretsiz kota) -> Anthropic
     (varsa) sirasiyla dener: tiefbau_kazi / netzbetrieb_konzesyon /
     planlama / genel_yuklenici / pasif_altyapi / cerceve_sozlesme /
     alakasiz
  4) Postgres'e yaz: companies (alici + kazananlar), tenders,
     tender_companies (issuer/winner rolleri), classification_training_data

Bu bir PROTOTIP. TED 2024'te eski HTML formatindan "eForms" (yapisal
UBL/XML) formatina tamamen gecti - detay sayfasi artik client-side
React uygulamasi, ham HTML'de veri yok. Onun yerine her bildirimin
herkese acik /xml export'unu parse ediyoruz (ornek:
https://ted.europa.eu/en/notice/25989-2024/xml). Bu XML de TED'in
kendi sema versiyonuna gore zamanla degisebilir - dry-run ile once
kontrol et.

Kullanim:
    python scripts/ted_ingest.py --limit 50
    python scripts/ted_ingest.py --since 2024-01-01 --limit 20
    python scripts/ted_ingest.py --region bayern --limit 20

  --since         bu tarihten sonraki sonuc ilanlarini al (YYYY-MM-DD).
                  Verilmezse bugunden --lookback-days kadar geriye gidilir -
                  boylece gunluk zamanlanmis calistirmada her seferinde
                  ayni sabit pencere degil, "son N gun" taranir.
  --until         bu tarihe kadar olan ilanlari al (YYYY-MM-DD, opsiyonel).
  --lookback-days --since verilmezse kac gun geriye gidilsin (varsayilan: 60)
  --limit         kac ihale islensin (varsayilan: 100)
  --region        belirli bir Bundesland ile sinirla (orn. "bayern",
                   "nordrhein-westfalen") - verilmezse tum Almanya taranir.
                   Gecerli isimler icin BUNDESLAND_NUTS1 sozlugune bak.
  --dry-run       DB'ye yazmadan sadece ne yapacagini yazdirir

NOT (bolge filtresi): TED'in "place-of-performance" alani NUTS kod
hiyerarsisini kullaniyor (CPV kodlarindaki gibi); ulke seviyesi icin "DEU"
kullaniliyor, bolge (Bundesland) icin NUTS1 kodu (orn. Bayern -> DE2)
deneniyor. Bu sandbox'tan TED API'sine canli erisim engellendigi icin bu
davranis TED'in kendi API'sine karsi ELLE DOGRULANMADI - --dry-run ile
"toplam X eslesme" sayisinin "Tum Almanya"ya kiyasla mantikli (daha kucuk)
cikip cikmadigina bak; hic degismiyorsa filtre etkisiz demektir, bize haber
ver.

Zaten DB'de olan bir ihale (ted_publication_number ile eslesen) XML
cekmeden/siniflandirmadan atlanir - gunluk calistirmada onceden islenmis
ihaleleri bosuna yeniden isleyip zaman kaybetmemek icin (bu kontrol sadece
--dry-run KAPALIYKEN calisir, cunku dry-run'da DB baglantisi acilmiyor).

Hicbir siniflandirma saglayicisi (Ollama/Groq/Anthropic) erisilebilir
degilse siniflandirma adimi atlanir, category NULL kalir.
"""
import argparse
import os
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path

import psycopg2
import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

TED_SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"
TED_DETAIL_URL = "https://ted.europa.eu/de/notice/-/detail/{pub}"
TED_XML_URL = "https://ted.europa.eu/en/notice/{pub}/xml"

# eForms/UBL XML namespace'leri - TED'in yapisal export'unu parse etmek icin
XML_NS = {
    "efac": "http://data.europa.eu/p27/eforms-ubl-extension-aggregate-components/1",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
}

FIBER_CPV_CODES = ["32562000", "32562300", "45232300", "45232310"]

# Bundesland -> NUTS1 kodu (Eurostat/EU standardi, sabit siniflandirma).
# place-of-performance filtresinde "DEU" yerine kullanilabilir.
BUNDESLAND_NUTS1 = {
    "baden-wurttemberg": "DE1",
    "bayern": "DE2",
    "berlin": "DE3",
    "brandenburg": "DE4",
    "bremen": "DE5",
    "hamburg": "DE6",
    "hessen": "DE7",
    "mecklenburg-vorpommern": "DE8",
    "niedersachsen": "DE9",
    "nordrhein-westfalen": "DEA",
    "rheinland-pfalz": "DEB",
    "saarland": "DEC",
    "sachsen": "DED",
    "sachsen-anhalt": "DEE",
    "schleswig-holstein": "DEF",
    "thuringen": "DEG",
}


def resolve_place_code(region: str | None) -> str:
    """Bundesland adini (turkce klavyeden gelebilecek varyasyonlarla) NUTS1
    koduna cevirir; taninmayan/bos deger icin tum Almanya (DEU) doner."""
    if not region:
        return "DEU"
    key = (
        region.strip().lower()
        .replace("ü", "u").replace("ö", "o").replace("ä", "a")
        .replace(" ", "-")
    )
    return BUNDESLAND_NUTS1.get(key, "DEU")

CATEGORIES = [
    "tiefbau_kazi",
    "netzbetrieb_konzesyon",
    "planlama",
    "genel_yuklenici",
    "pasif_altyapi",
    "cerceve_sozlesme",
    "alakasiz",
]

HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (ihale-botu arastirma prototipi)"}

CLASSIFY_MODEL = "claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# 1) TED arama
# ---------------------------------------------------------------------------

def build_query(since_date: str, until_date: str | None = None, place_code: str = "DEU") -> str:
    cpv_clause = " OR ".join(f"classification-cpv={c}*" for c in FIBER_CPV_CODES)
    since_fmt = since_date.replace("-", "")
    query = (
        f"({cpv_clause}) AND place-of-performance={place_code} "
        f"AND notice-type=can-standard AND publication-date>={since_fmt}"
    )
    if until_date:
        query += f" AND publication-date<={until_date.replace('-', '')}"
    return query


def search_notices(since_date: str, limit: int, until_date: str | None = None,
                    place_code: str = "DEU", log=print) -> list[dict]:
    body = {
        "query": build_query(since_date, until_date, place_code),
        "fields": [
            "publication-number", "notice-title", "buyer-name",
            "publication-date", "total-value",
        ],
        "page": 1,
        "limit": limit,
        "scope": "ALL",
    }
    resp = requests.post(TED_SEARCH_URL, json=body, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    total = data.get("totalNoticeCount")
    notices = data.get("notices", [])
    log(f"[TED arama] toplam {total} eslesme, bu calistirmada {len(notices)} tanesi islenecek")
    return notices


def count_notices(since_date: str, until_date: str | None = None, place_code: str = "DEU") -> int | None:
    """Hicbir sey islemeden/yazmadan sadece toplam eslesme sayisini doner -
    bolge filtresinin gercekten calisip calismadigini ucuza kontrol etmek
    icin (bkz. dosyanin ustundeki NOT)."""
    body = {
        "query": build_query(since_date, until_date, place_code),
        "fields": ["publication-number"],
        "page": 1,
        "limit": 1,
        "scope": "ALL",
    }
    resp = requests.post(TED_SEARCH_URL, json=body, timeout=30)
    resp.raise_for_status()
    return resp.json().get("totalNoticeCount")


def notice_title_de(notice: dict) -> str:
    titles = notice.get("notice-title") or {}
    return titles.get("deu") or next(iter(titles.values()), "") if titles else ""


def notice_buyer(notice: dict) -> str:
    buyer = notice.get("buyer-name") or {}
    if isinstance(buyer, dict):
        vals = next(iter(buyer.values()), [])
        return vals[0] if vals else ""
    return str(buyer)


# ---------------------------------------------------------------------------
# 2) eForms XML'den kazananlari parse et
# ---------------------------------------------------------------------------

def fetch_notice_xml(pub_number: str) -> str:
    url = TED_XML_URL.format(pub=pub_number)
    resp = requests.get(url, headers=HTTP_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def _tag(ns_prefix: str, local: str) -> str:
    return f"{{{XML_NS[ns_prefix]}}}{local}"


def parse_winners(xml_text: str) -> list[dict]:
    """
    TED artik eski HTML yerine yapisal "eForms" XML export'u kullaniyor.
    Kazanan tespiti icin izlenen zincir:
      efac:LotResult (TenderResultCode == 'selec-w' -> kazanan secildi)
        -> efac:LotTender.ID (kazanan teklifin ID'si)
      efac:LotTender[ID=...] -> efac:TenderingParty.ID
      efac:TenderingParty[ID=...] -> efac:Tenderer.ID (organizasyon ID'si)
      efac:Organization[ID=...] -> ad/adres/email/telefon

    NOT: TED'in XML semasi zamanla degisebilir - dry-run ile once kontrol et.
    """
    root = ET.fromstring(xml_text)

    # organizasyon ID -> iletisim bilgileri
    organizations: dict[str, dict] = {}
    for org in root.iter(_tag("efac", "Organization")):
        company = org.find("efac:Company", XML_NS)
        if company is None:
            continue
        org_id_el = company.find("cac:PartyIdentification/cbc:ID", XML_NS)
        name_el = company.find("cac:PartyName/cbc:Name", XML_NS)
        if org_id_el is None or name_el is None or not name_el.text:
            continue
        email_el = company.find("cac:Contact/cbc:ElectronicMail", XML_NS)
        phone_el = company.find("cac:Contact/cbc:Telephone", XML_NS)
        city_el = company.find("cac:PostalAddress/cbc:CityName", XML_NS)
        street_el = company.find("cac:PostalAddress/cbc:StreetName", XML_NS)
        nuts_el = company.find("cac:PostalAddress/cbc:CountrySubentityCode", XML_NS)
        address = ", ".join(
            p for p in (street_el.text if street_el is not None else None,
                        city_el.text if city_el is not None else None) if p
        )
        organizations[org_id_el.text] = {
            "name": name_el.text,
            "email": email_el.text if email_el is not None else None,
            "phone": phone_el.text if phone_el is not None else None,
            "address": address or None,
            "nuts": nuts_el.text if nuts_el is not None else None,
        }

    # TenderingParty ID -> teklifi veren organizasyon ID
    party_to_org: dict[str, str] = {}
    for party in root.iter(_tag("efac", "TenderingParty")):
        party_id_el = party.find("cbc:ID", XML_NS)
        tenderer_org_el = party.find("efac:Tenderer/cbc:ID", XML_NS)
        if party_id_el is not None and tenderer_org_el is not None:
            party_to_org[party_id_el.text] = tenderer_org_el.text

    # Teklif (tender) ID -> TenderingParty ID
    tender_to_party: dict[str, str] = {}
    for lot_tender in root.iter(_tag("efac", "LotTender")):
        tender_id_el = lot_tender.find("cbc:ID", XML_NS)
        party_id_el = lot_tender.find("efac:TenderingParty/cbc:ID", XML_NS)
        if tender_id_el is not None and party_id_el is not None:
            tender_to_party[tender_id_el.text] = party_id_el.text

    winners = []
    seen_org_ids: set[str] = set()
    for lot_result in root.iter(_tag("efac", "LotResult")):
        result_code_el = lot_result.find("cbc:TenderResultCode", XML_NS)
        if result_code_el is None or result_code_el.text != "selec-w":
            continue  # bu lot icin kazanan secilmemis (iptal/basarisiz vb.)
        tender_id_el = lot_result.find("efac:LotTender/cbc:ID", XML_NS)
        if tender_id_el is None:
            continue
        party_id = tender_to_party.get(tender_id_el.text)
        org_id = party_to_org.get(party_id) if party_id else None
        if not org_id or org_id in seen_org_ids:
            continue
        org = organizations.get(org_id)
        if org:
            winners.append(org)
            seen_org_ids.add(org_id)

    return winners


# ---------------------------------------------------------------------------
# 3) Siniflandirma - Ollama (yerel, oncelikli) -> Groq (ucretsiz kota, yedek)
#    -> Anthropic (varsa, son care)
# ---------------------------------------------------------------------------

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")

CATEGORIES_DESC = (
    "- tiefbau_kazi: kazi, kablo doseme, ev baglantisi insaat isleri\n"
    "- netzbetrieb_konzesyon: sebeke isletimi, imtiyaz, aktif cihaz/CPE\n"
    "- planlama: planlama, muhendislik, danismanlik hizmetleri\n"
    "- genel_yuklenici: anahtar teslim genel yuklenici\n"
    "- pasif_altyapi: malzeme, kablo kanali, dolap, pasif altyapi tedariki\n"
    "- cerceve_sozlesme: cerceve sozlesme / dinamik satin alma sistemi\n"
    "- alakasiz: fiber/glasfaser/breitband ile ilgisi olmayan "
    "(hastane elektrik tesisati, sunucu alimi, arac vb.)"
)


def build_classify_prompt(title: str, buyer: str) -> str:
    return (
        "Asagidaki Almanya kamu ihalesi kaydini su 7 kategoriden TAM OLARAK "
        f"BIRINE ata:\n{CATEGORIES_DESC}\n\n"
        f"Ihale basligi: {title}\n"
        f"Alici kurum: {buyer}\n\n"
        "SADECE kategori adini (orn. tiefbau_kazi) yaz, baska hicbir sey yazma."
    )


def normalize_label(raw: str) -> str:
    label = raw.strip().lower()
    # bazi modeller tirnak/nokta/aciklama ekleyebiliyor - ilk gecerli kelimeyi ara
    for cat in CATEGORIES:
        if cat in label:
            return cat
    return "alakasiz"


def _print_http_error(label: str, exc: Exception, log=print) -> None:
    """HTTPError ise sunucunun donduğu govdeyi de yazdir - sadece status kodu
    yeterli tanı bilgisi vermiyor (orn. 'model bulunamadi' vs 'gecersiz model
    id' ikisi de 404 donebiliyor). `log` verilmezse normal print() davranisi
    korunur (CLI); dashboard.py kendi log listesini verip yakalayabiliyor -
    daha once bu fonksiyon hep print() kullaniyordu ve dashboard'dan
    calistirildiginda bu hata mesajlari kullaniciya hic gorunmuyordu."""
    resp = getattr(exc, "response", None)
    if resp is not None:
        body = resp.text.strip()
        if len(body) > 300:
            body = body[:300] + "..."
        log(f"  [{label} basarisiz] {exc}\n    govde: {body}")
    else:
        log(f"  [{label} basarisiz] {exc}")


def classify_with_ollama(prompt: str, log=print) -> str | None:
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": OLLAMA_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"temperature": 0},
            },
            timeout=60,
            # Ollama her zaman yerelde calisir - sistemde tanimli bir proxy
            # (VPN/antivirus/kurumsal ag) varsa localhost istegini de oradan
            # gecirmeye calisip 405/407 gibi hatalar verebiliyor. Bu istek
            # icin proxy'yi bilerek devre disi birakiyoruz.
            proxies={"http": None, "https": None},
        )
        resp.raise_for_status()
        data = resp.json()
        return data["message"]["content"]
    except (requests.RequestException, KeyError, ValueError) as exc:
        _print_http_error("Ollama", exc, log=log)
        return None


def classify_with_groq(prompt: str, log=print) -> str | None:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        log("  [Groq atlandi] GROQ_API_KEY tanimli degil (.env / Streamlit secrets / GH Actions secrets)")
        return None
    try:
        resp = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": GROQ_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                # gpt-oss modelleri "reasoning" modeli - cevabi vermeden once
                # ic diyalog icin token harciyor, dusuk max_tokens bu asamada
                # kesilip bos content donmesine sebep oluyordu.
                "max_tokens": 800,
                "temperature": 0,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
        _print_http_error("Groq", exc, log=log)
        return None


def classify_with_anthropic(prompt: str, log=print) -> str | None:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import anthropic
    except ImportError:
        return None
    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=CLASSIFY_MODEL,
            max_tokens=20,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text
    except Exception as exc:  # noqa: BLE001 - genis yakalama, sadece fallback icin
        log(f"  [Anthropic basarisiz] {exc}")
        return None


def classify_tender(title: str, buyer: str, log=print) -> tuple[str | None, str]:
    """Donen: (kategori_veya_None, kaynak). Sirasiyla Ollama -> Groq -> Anthropic dener."""
    prompt = build_classify_prompt(title, buyer)

    raw = classify_with_ollama(prompt, log=log)
    if raw:
        return normalize_label(raw), "ollama"

    raw = classify_with_groq(prompt, log=log)
    if raw:
        return normalize_label(raw), "groq"

    raw = classify_with_anthropic(prompt, log=log)
    if raw:
        return normalize_label(raw), "anthropic"

    return None, "none"


# ---------------------------------------------------------------------------
# 4) Postgres'e yazma
# ---------------------------------------------------------------------------

def get_connection():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL tanimli degil (.env dosyasina veya Streamlit secrets'a bak)")
    return psycopg2.connect(database_url)


def upsert_company(cur, name: str, email: str | None = None, phone: str | None = None,
                    address: str | None = None, nuts: str | None = None) -> int:
    cur.execute(
        """
        INSERT INTO companies (name, email, phone, address, nuts_code)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (name) DO UPDATE SET
            email = COALESCE(EXCLUDED.email, companies.email),
            phone = COALESCE(EXCLUDED.phone, companies.phone),
            address = COALESCE(EXCLUDED.address, companies.address),
            nuts_code = COALESCE(EXCLUDED.nuts_code, companies.nuts_code)
        RETURNING id
        """,
        (name, email, phone, address, nuts),
    )
    return cur.fetchone()[0]


def tender_exists(cur, pub_number: str) -> bool:
    cur.execute("SELECT 1 FROM tenders WHERE ted_publication_number = %s", (pub_number,))
    return cur.fetchone() is not None


def upsert_tender(cur, notice: dict, category: str | None, category_source: str) -> tuple[int, bool]:
    pub = notice["publication-number"]
    title = notice_title_de(notice)
    value = notice.get("total-value")
    pub_date = notice.get("publication-date")
    url = TED_DETAIL_URL.format(pub=pub)

    cur.execute("SELECT id FROM tenders WHERE ted_publication_number = %s", (pub,))
    existing = cur.fetchone()
    if existing:
        return existing[0], False

    cur.execute(
        """
        INSERT INTO tenders (
            title, source_portal, source_url, category, category_source,
            status, published_date, ted_publication_number, ted_notice_type,
            value_eur, ted_url
        ) VALUES (%s, 'TED', %s, %s, %s, 'sonuclandi', %s, %s, 'can-standard', %s, %s)
        RETURNING id
        """,
        (title, url, category, category_source,
         pub_date[:10] if pub_date else None, pub, value, url),
    )
    return cur.fetchone()[0], True


def link_company_role(cur, tender_id: int, company_id: int, role: str) -> None:
    cur.execute(
        """
        INSERT INTO tender_companies (tender_id, company_id, role)
        SELECT %s, %s, %s
        WHERE NOT EXISTS (
            SELECT 1 FROM tender_companies
            WHERE tender_id = %s AND company_id = %s AND role = %s
        )
        """,
        (tender_id, company_id, role, tender_id, company_id, role),
    )


def save_training_label(cur, tender_id: int, text_snapshot: str, label: str, labeled_by: str) -> None:
    cur.execute(
        """
        INSERT INTO classification_training_data (tender_id, text_snapshot, label, labeled_by)
        VALUES (%s, %s, %s, %s)
        """,
        (tender_id, text_snapshot, label, labeled_by),
    )


# ---------------------------------------------------------------------------
# Ana akis
# ---------------------------------------------------------------------------

def run_ingest(since: str | None = None, until: str | None = None, lookback_days: int = 60,
               limit: int = 100, place_code: str = "DEU", dry_run: bool = False,
               log=print) -> dict:
    """Tum ingest akisini calistirir. CLI (main()) ve dashboard.py'nin
    "Yeni Tarama" ekrani tarafindan ortak kullanilir. `log` ile cagiran taraf
    ciktiyi kendi istedigi yere yonlendirebilir (dashboard'da bir liste'ye
    biriktirmek gibi) - varsayilan olarak normal print() davranisini korur."""
    since = since or (date.today() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    notices = search_notices(since, limit, until_date=until, place_code=place_code, log=log)
    summary = {"since": since, "until": until, "place_code": place_code,
               "total_matches": None, "processed": 0, "skipped": 0}
    if not notices:
        log("Eslesen ihale yok.")
        return summary

    conn = None if dry_run else get_connection()
    cur = conn.cursor() if conn else None

    processed = 0
    skipped = 0
    try:
        for notice in notices:
            pub = notice["publication-number"]
            title = notice_title_de(notice)
            buyer = notice_buyer(notice)
            log(f"\n--- {pub} | {title} | Alici: {buyer}")

            if cur is not None and tender_exists(cur, pub):
                log("  Zaten DB'de var, atlaniyor.")
                skipped += 1
                continue

            try:
                xml_text = fetch_notice_xml(pub)
            except requests.RequestException as exc:
                log(f"  [HATA] xml cekilemedi: {exc}")
                continue

            try:
                winners = parse_winners(xml_text)
            except ET.ParseError as exc:
                log(f"  [HATA] xml parse edilemedi: {exc}")
                winners = []
            log(f"  Bulunan kazanan sayisi: {len(winners)}")
            for w in winners:
                log(f"    - {w.get('name')} | {w.get('email', 'e-posta yok')} | {w.get('phone', 'tel yok')}")

            category, category_source = classify_tender(title, buyer, log=log)
            if category:
                log(f"  Kategori ({category_source}): {category}")
            else:
                log("  Kategori: atlandi (hicbir saglayici erisilebilir degil)")

            if dry_run:
                processed += 1
                time.sleep(0.3)
                continue

            buyer_company_id = upsert_company(cur, buyer) if buyer else None
            tender_id, is_new = upsert_tender(cur, notice, category, category_source)

            if buyer_company_id:
                link_company_role(cur, tender_id, buyer_company_id, "issuer")

            for w in winners:
                winner_id = upsert_company(
                    cur, w["name"],
                    email=w.get("email"), phone=w.get("phone"),
                    address=w.get("address"), nuts=w.get("nuts"),
                )
                link_company_role(cur, tender_id, winner_id, "winner")

            if category:
                save_training_label(cur, tender_id, f"{title} | {buyer}", category, category_source)

            conn.commit()
            processed += 1
            time.sleep(0.5)  # TED sunucusuna nazik davranalim
    finally:
        if conn:
            cur.close()
            conn.close()

    log(f"\nToplam islenen ihale: {processed} (atlanan/zaten vardi: {skipped})")
    summary["processed"] = processed
    summary["skipped"] = skipped
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default=None,
                         help="YYYY-MM-DD, bu tarihten sonrasi (varsayilan: bugunden --lookback-days kadar geri)")
    parser.add_argument("--until", default=None, help="YYYY-MM-DD, bu tarihe kadar (opsiyonel)")
    parser.add_argument("--lookback-days", type=int, default=60,
                         help="--since verilmezse kac gun geriye gidilsin (varsayilan: 60)")
    parser.add_argument("--limit", type=int, default=100, help="kac ihale islensin")
    parser.add_argument("--region", default=None,
                         help="Bundesland adi (orn. bayern, nordrhein-westfalen) - verilmezse tum Almanya")
    parser.add_argument("--dry-run", action="store_true", help="DB'ye yazmadan sadece goster")
    args = parser.parse_args()

    place_code = resolve_place_code(args.region)
    try:
        run_ingest(
            since=args.since, until=args.until, lookback_days=args.lookback_days,
            limit=args.limit, place_code=place_code, dry_run=args.dry_run,
        )
    except RuntimeError as exc:
        sys.exit(f"HATA: {exc}")


if __name__ == "__main__":
    main()
