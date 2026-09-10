"""
Ihale kazananlarina gonderilecek taseronluk/is birligi teklifi e-posta
taslaklarini LLM ile uretir (Ollama -> Groq -> Anthropic sirasiyla dener,
ted_ingest.py'deki siniflandirma zincirinin ayni mantigi - ama burada baglam
farkli oldugu icin kendi bagimsiz fonksiyonlari var).

ONEMLI: Hicbir e-posta OTOMATIK GONDERILMEZ. Bu script sadece taslak metni
uretip email_drafts tablosuna yazar - gonderme karari ve eylemi tamamen
kullaniciya ait (dashboard'dan gozden gecirip kendi mail programindan gonderir).

Gonderen (kendi) firma bilgisi .env/secrets'ta DEGIL, DB'deki sender_profile
tablosunda tutulur - dashboard'daki "Mail Taslaklari" sekmesinden bir kere
doldurman yeterli.

Kullanim:
  python scripts/generate_drafts.py --limit 10
  python scripts/generate_drafts.py --tender-id 5 --dry-run
"""
import argparse
import os
import sys
from pathlib import Path

import psycopg2
import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
CLASSIFY_MODEL = "claude-haiku-4-5-20251001"

DE_MARKER = "### DE ###"
TR_MARKER = "### TR_OZET ###"

# Kategori kodlarinin taslak metninde kullanilacak dogal-dil aciklamasi.
CATEGORY_CONTEXT = {
    "tiefbau_kazi": "kazı, kablo döşeme ve saha inşaat işleri",
    "netzbetrieb_konzesyon": "şebeke işletimi / imtiyaz kapsamındaki teknik işler",
    "planlama": "planlama ve mühendislik destek işleri",
    "genel_yuklenici": "anahtar teslim genel yüklenicilik kapsamındaki işler",
    "pasif_altyapi": "pasif altyapı (kablo kanalı, dolap vb.) tedarik/montaj işleri",
    "cerceve_sozlesme": "çerçeve sözleşme kapsamındaki tekrarlayan işler",
}


def _print_http_error(label: str, exc: Exception, log=print) -> None:
    resp = getattr(exc, "response", None)
    if resp is not None:
        body = resp.text.strip()
        if len(body) > 300:
            body = body[:300] + "..."
        log(f"  [{label} basarisiz] {exc}\n    govde: {body}")
    else:
        log(f"  [{label} basarisiz] {exc}")


def _call_ollama(prompt: str, log=print) -> str | None:
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": OLLAMA_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"temperature": 0.4},
            },
            timeout=90,
            proxies={"http": None, "https": None},
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]
    except (requests.RequestException, KeyError, ValueError) as exc:
        _print_http_error("Ollama", exc, log)
        return None


def _call_groq(prompt: str, log=print) -> str | None:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        log("  [Groq atlandi] GROQ_API_KEY tanimli degil")
        return None
    try:
        resp = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": GROQ_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                # Siniflandirmada oldugu gibi gpt-oss modelleri once ic
                # "reasoning" harciyor - taslak (email+ozet) tek kelimelik
                # siniflandirmadan çok daha uzun oldugu icin burada daha
                # yuksek bir tavan birakiyoruz.
                "max_tokens": 2000,
                "temperature": 0.4,
            },
            timeout=45,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
        _print_http_error("Groq", exc, log)
        return None


def _call_anthropic(prompt: str, log=print) -> str | None:
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
            model=CLASSIFY_MODEL, max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text
    except Exception as exc:  # noqa: BLE001 - genis yakalama, sadece fallback icin
        log(f"  [Anthropic basarisiz] {exc}")
        return None


def build_draft_prompt(tender_title: str, category: str, winner_name: str, sender: dict) -> str:
    category_desc = CATEGORY_CONTEXT.get(category, category)
    return f"""Sen Almanya'da faaliyet gösteren bir taşeron/alt yüklenici firmanın adına iş
geliştirme e-postası yazıyorsun. Aşağıdaki bilgilere göre KISA ve PROFESYONEL bir Almanca
iş e-postası TASLAĞI yaz, ardından Türkçe bir özetini ver.

Gönderen firma: {sender.get('company_name') or '(belirtilmedi)'}
Sunduğu hizmet: {sender.get('services_de') or '(belirtilmedi)'}
İmza bilgisi: {sender.get('contact_name') or ''}, {sender.get('phone') or ''}, {sender.get('email') or ''}

İhale bilgisi:
- İhale başlığı: {tender_title}
- Kategori: {category_desc}
- İhaleyi kazanan şirket: {winner_name}

Kurallar:
- Almanca kısım resmi "Sie" hitabıyla, "Sehr geehrte Damen und Herren," ile başlasın.
- 120-180 kelime civarı, kısa paragraflar halinde.
- Kazanan şirketin bu ihaleyi kazandığını nazikçe belirt/tebrik et.
- Gönderen firmanın bu kategorideki ({category_desc}) işlerde somut olarak nasıl destek
  olabileceğini anlat.
- Sonunda kısa bir sonraki adım öner (kısa bir görüşme/teklif talebi).
- İmza bloğu ekle (firma adı + iletişim bilgileri, verilenler neyse onları kullan).
- Türkçe kısım sadece 2-3 cümlelik bir özet olsun, birebir çeviri değil.

Çıktı TAM OLARAK şu formatta olsun, başka hiçbir şey yazma (açıklama, markdown başlığı vb. ekleme):
{DE_MARKER}
<almanca e-posta>
{TR_MARKER}
<turkce ozet>
"""


def generate_draft_text(tender_title: str, category: str, winner_name: str, sender: dict,
                         log=print) -> str | None:
    prompt = build_draft_prompt(tender_title, category, winner_name, sender)
    for call in (_call_ollama, _call_groq, _call_anthropic):
        raw = call(prompt, log=log)
        if raw and DE_MARKER in raw:
            return raw
    return None


def parse_draft_text(raw: str) -> tuple[str, str]:
    """Ham LLM ciktisini (almanca, turkce_ozet) ikilisine ayirir. Markerlar
    hiç yoksa hepsi almanca kisim sayilir (bozuk/eksik cikti durumunda bile
    bir seyler gostermek icin)."""
    if DE_MARKER not in raw:
        return raw.strip(), ""
    de_part = raw.split(DE_MARKER, 1)[-1]
    if TR_MARKER in de_part:
        de_part, tr_part = de_part.split(TR_MARKER, 1)
    else:
        tr_part = ""
    return de_part.strip(), tr_part.strip()


def combine_draft_text(de_text: str, tr_text: str) -> str:
    return f"{DE_MARKER}\n{de_text.strip()}\n\n{TR_MARKER}\n{tr_text.strip()}"


# ---------------------------------------------------------------------------
# DB erisimi
# ---------------------------------------------------------------------------

def get_connection():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL tanimli degil (.env dosyasina ya da Streamlit secrets'a bak)")
    return psycopg2.connect(database_url)


def load_sender_profile(cur) -> dict:
    cur.execute(
        "SELECT company_name, services_de, contact_name, phone, email FROM sender_profile WHERE id = 1"
    )
    row = cur.fetchone()
    if not row:
        return {"company_name": "", "services_de": "", "contact_name": "", "phone": "", "email": ""}
    keys = ["company_name", "services_de", "contact_name", "phone", "email"]
    return dict(zip(keys, row))


def get_tenders_needing_drafts(cur, limit: int) -> list[tuple]:
    cur.execute(
        """
        SELECT t.id, t.title, t.category, c.id, c.name
        FROM tenders t
        JOIN tender_companies tc ON tc.tender_id = t.id AND tc.role = 'winner'
        JOIN companies c ON c.id = tc.company_id
        WHERE t.category IS NOT NULL AND t.category != 'alakasiz'
          AND NOT EXISTS (SELECT 1 FROM email_drafts ed WHERE ed.tender_id = t.id)
        ORDER BY t.published_date DESC NULLS LAST, t.id DESC
        LIMIT %s
        """,
        (limit,),
    )
    return cur.fetchall()


def get_tender_for_draft(cur, tender_id: int) -> tuple | None:
    cur.execute(
        """
        SELECT t.id, t.title, t.category, c.id, c.name
        FROM tenders t
        JOIN tender_companies tc ON tc.tender_id = t.id AND tc.role = 'winner'
        JOIN companies c ON c.id = tc.company_id
        WHERE t.id = %s
        LIMIT 1
        """,
        (tender_id,),
    )
    return cur.fetchone()


def get_verified_contact(cur, company_id: int) -> int | None:
    cur.execute(
        "SELECT id FROM contacts WHERE company_id = %s AND verified = true ORDER BY id LIMIT 1",
        (company_id,),
    )
    row = cur.fetchone()
    return row[0] if row else None


def save_draft(cur, tender_id: int, contact_id: int | None, draft_text: str, category: str) -> int:
    cur.execute(
        """
        INSERT INTO email_drafts (tender_id, contact_id, draft_text, category_used, status)
        VALUES (%s, %s, %s, %s, 'draft')
        RETURNING id
        """,
        (tender_id, contact_id, draft_text, category),
    )
    return cur.fetchone()[0]


def run_generate_drafts(limit: int = 10, tender_id: int | None = None, dry_run: bool = False,
                         log=print) -> dict:
    """Tum taslak-uretme akisini calistirir. CLI (main()) ve dashboard.py'nin
    "Mail Taslaklari" sekmesi tarafindan ortak kullanilir."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        sender = load_sender_profile(cur)
        if not sender["company_name"] or not sender["services_de"]:
            raise RuntimeError(
                "Once 'Mail Taslakları' sekmesindeki gönderen firma bilgilerini "
                "(firma adı + sunduğun hizmet) doldurup kaydet - taslak bunlar olmadan "
                "anlamlı olmuyor."
            )

        if tender_id:
            row = get_tender_for_draft(cur, tender_id)
            rows = [row] if row else []
        else:
            rows = get_tenders_needing_drafts(cur, limit)

        if not rows:
            log("Taslak üretilecek ihale yok (hepsi zaten yazılmış ya da kategori uygun değil/henüz kategorize edilmemiş).")
            return {"generated": 0}

        generated = 0
        for t_id, title, category, company_id, winner_name in rows:
            log(f"\n--- İhale #{t_id}: {title} ({category}) -> {winner_name}")
            raw = generate_draft_text(title, category, winner_name, sender, log=log)
            if not raw:
                log("  [HATA] Hiçbir sağlayıcıdan taslak alınamadı.")
                continue
            de_text, tr_text = parse_draft_text(raw)
            log(f"  Taslak üretildi ({len(de_text)} karakter Almanca, {len(tr_text)} karakter Türkçe özet).")
            if dry_run:
                generated += 1
                continue
            contact_id = get_verified_contact(cur, company_id)
            save_draft(cur, t_id, contact_id, combine_draft_text(de_text, tr_text), category)
            conn.commit()
            generated += 1

        log(f"\nToplam üretilen taslak: {generated}")
        return {"generated": generated}
    finally:
        cur.close()
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=10, help="kaç ihale için taslak üretilsin")
    parser.add_argument("--tender-id", type=int, default=None, help="sadece bu ihale için üret")
    parser.add_argument("--dry-run", action="store_true", help="DB'ye yazmadan sadece göster")
    args = parser.parse_args()
    try:
        run_generate_drafts(limit=args.limit, tender_id=args.tender_id, dry_run=args.dry_run)
    except RuntimeError as exc:
        sys.exit(f"HATA: {exc}")


if __name__ == "__main__":
    main()