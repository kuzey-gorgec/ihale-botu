"""
Streamlit dashboard - ihale-botu Postgres DB'sindeki ihaleleri, kazananlari ve
bulunan kisileri gosterir. Genel/teknik olmayan kullanicilar icin sadelestirilmis,
modern bir arayuz.

Kullanim:
    pip install streamlit pandas
    streamlit run dashboard.py

Varsayilan olarak http://localhost:8501 adresinde acilir.
"""
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import psycopg2
import streamlit as st
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

# scripts/ted_ingest.py ve scripts/find_contacts.py'yi modul olarak import
# edebilmek icin (dashboard'dan tarama tetikleyebilmek amaciyla).
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import find_contacts  # noqa: E402
import ted_ingest  # noqa: E402

st.set_page_config(
    page_title="Almanya Fiber Ihale Botu",
    page_icon="🌐",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Gorsel stil - hem acik hem koyu temada calisacak sekilde yari saydam renkler
# kullaniliyor (Streamlit'in kendi tema degiskenlerine bagli kalmadan).
# ---------------------------------------------------------------------------
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

    html, body, [class*="css"] { font-family: 'Inter', sans-serif; }

    .app-header h1 { margin-bottom: 0.1rem; font-weight: 700; }
    .app-header p { opacity: 0.65; margin-top: 0; font-size: 0.95rem; }

    [data-testid="stMetric"] {
        background: rgba(127, 127, 127, 0.06);
        border: 1px solid rgba(127, 127, 127, 0.18);
        border-radius: 12px;
        padding: 1rem 1.25rem;
    }
    [data-testid="stMetricLabel"] { font-weight: 500; opacity: 0.75; }
    [data-testid="stMetricValue"] { font-weight: 700; }

    div[data-testid="stDataFrame"] { border-radius: 10px; overflow: hidden; }
    </style>
    """,
    unsafe_allow_html=True,
)

CATEGORY_LABELS = {
    "tiefbau_kazi": "🚧 Tiefbau / Kazı",
    "netzbetrieb_konzesyon": "🏢 Netzbetrieb / Konsesyon",
    "planlama": "📐 Planlama",
    "genel_yuklenici": "🏗️ Genel Yüklenici",
    "pasif_altyapi": "🔧 Pasif Altyapı",
    "cerceve_sozlesme": "📋 Çerçeve Sözleşme",
    "alakasiz": "❌ Alakasız",
}
CATEGORY_FALLBACK = "⏳ Kategorize Edilmemiş"

STATUS_LABELS = {
    "acik": "🟢 Açık",
    "sonuclandi": "✅ Sonuçlandı",
    "iptal": "🚫 İptal",
}
STATUS_FALLBACK = "❔ Bilinmiyor"

SOURCE_LABELS = {
    "linkedin": "💼 LinkedIn",
    "xing": "🔶 Xing",
    "website": "🌐 Şirket Sitesi",
}


def _is_empty(value) -> bool:
    # Postgres NULL, pandas'ta bazen None bazen float NaN olarak geliyor -
    # ikisini de "bos" sayiyoruz (pd.isna tek basina skaler olmayan/karma
    # tipte hata verebildigi icin once bool/str kisayolu deniyoruz).
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def category_label(value: str | None) -> str:
    if _is_empty(value):
        return CATEGORY_FALLBACK
    return CATEGORY_LABELS.get(value, value)


def status_label(value: str | None) -> str:
    if _is_empty(value):
        return STATUS_FALLBACK
    return STATUS_LABELS.get(value, value)


def source_label(value: str | None) -> str:
    if _is_empty(value):
        return "—"
    return SOURCE_LABELS.get(value, value)


def format_eur(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    formatted = f"{value:,.0f}"
    formatted = formatted.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{formatted} €"


# ---------------------------------------------------------------------------
# DB erisimi
# ---------------------------------------------------------------------------
def _get_database_url() -> str | None:
    # Once .env / ortam degiskeni (yerel calistirma), sonra Streamlit Cloud
    # secrets (orada .env yok, secrets.toml uzerinden gelir).
    value = os.getenv("DATABASE_URL")
    if value:
        return value
    try:
        return st.secrets.get("DATABASE_URL")
    except Exception:
        return None


def _ensure_env_vars() -> None:
    """ted_ingest.py / find_contacts.py kendi os.getenv() cagrilarini yapiyor -
    yerelde .env'den zaten geliyor ama Streamlit Cloud'da .env yok, sadece
    st.secrets var. Dashboard'dan tarama tetikleyebilmek icin bu degerleri
    calisma anindan once os.environ'a da yaziyoruz (zaten ortamda varsa
    dokunmuyoruz)."""
    try:
        secrets = st.secrets
    except Exception:
        return
    for key in ("DATABASE_URL", "GROQ_API_KEY", "GROQ_MODEL", "SERPAPI_API_KEY",
                "OLLAMA_URL", "OLLAMA_MODEL", "ANTHROPIC_API_KEY"):
        if not os.getenv(key):
            try:
                value = secrets.get(key)
            except Exception:
                value = None
            if value:
                os.environ[key] = value


def get_connection():
    # Kasitli olarak cache_resource KULLANMIYORUZ: Neon (serverless Postgres)
    # bos duran baglantilari arka planda kapatiyor (compute suspend), onbelleklenmis
    # eski bir psycopg2 baglantisi bu durumda "InterfaceError: connection already
    # closed" ile patlar. Bunun yerine her sorguda taze baglanti aciyoruz - veri
    # zaten @st.cache_data(ttl=60) ile onbelleklendigi icin bu sik olmuyor.
    database_url = _get_database_url()
    if not database_url:
        st.error(
            "DATABASE_URL tanimli degil. Yerelde .env dosyasina bak, "
            "Streamlit Cloud'da uygulama Settings > Secrets kismina ekle."
        )
        st.stop()
    return psycopg2.connect(database_url)


def _run_query(query: str) -> pd.DataFrame:
    conn = get_connection()
    try:
        return pd.read_sql(query, conn)
    finally:
        conn.close()


@st.cache_data(ttl=60)
def load_tenders() -> pd.DataFrame:
    query = """
        SELECT
            t.id, t.title, t.category, t.category_source, t.status,
            t.published_date, t.value_eur, t.ted_url,
            issuer.name AS issuer_name
        FROM tenders t
        LEFT JOIN tender_companies tc_issuer
            ON tc_issuer.tender_id = t.id AND tc_issuer.role = 'issuer'
        LEFT JOIN companies issuer ON issuer.id = tc_issuer.company_id
        ORDER BY t.published_date DESC NULLS LAST, t.id DESC
    """
    return _run_query(query)


@st.cache_data(ttl=60)
def load_winners() -> pd.DataFrame:
    query = """
        SELECT
            tc.tender_id, c.name AS winner_name, c.email, c.phone,
            c.address, c.nuts_code
        FROM tender_companies tc
        JOIN companies c ON c.id = tc.company_id
        WHERE tc.role = 'winner'
    """
    return _run_query(query)


@st.cache_data(ttl=60)
def load_contacts() -> pd.DataFrame:
    query = """
        SELECT
            ct.id, ct.full_name, ct.title, ct.email, ct.phone,
            ct.source, ct.verified, ct.created_at,
            c.name AS company_name
        FROM contacts ct
        JOIN companies c ON c.id = ct.company_id
        ORDER BY ct.verified ASC, ct.created_at DESC, ct.id DESC
    """
    return _run_query(query)


def update_contact(contact_id: int, full_name, title, email, phone, verified: bool) -> None:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE contacts
                    SET full_name = %s, title = %s, email = %s, phone = %s, verified = %s
                    WHERE id = %s
                    """,
                    (full_name, title, email, phone, verified, contact_id),
                )
    finally:
        conn.close()


def delete_contact(contact_id: int) -> None:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM tender_contacts WHERE contact_id = %s", (contact_id,))
                cur.execute("DELETE FROM contacts WHERE id = %s", (contact_id,))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Baslik
# ---------------------------------------------------------------------------
st.markdown(
    """
    <div class="app-header">
        <h1>🌐 Almanya Fiber İhale Botu</h1>
        <p>Almanya'daki fiber / tiefbau ihalelerini otomatik tarar, kategorize eder ve kazanan
        şirketlerdeki muhatap kişileri bulur. Veriler her gün otomatik güncellenir.</p>
    </div>
    """,
    unsafe_allow_html=True,
)

tenders = load_tenders()
winners = load_winners()

if tenders.empty:
    st.info(
        "DB'de henüz ihale yok. Önce `scripts/ted_ingest.py` çalıştırılmalı "
        "(veya günlük otomatik taramanın ilk çalışmasını bekle)."
    )
    st.stop()

tenders = tenders.copy()
tenders["kategori_gorunum"] = tenders["category"].apply(category_label)
tenders["durum_gorunum"] = tenders["status"].apply(status_label)
tenders["deger_gorunum"] = tenders["value_eur"].apply(format_eur)

# ---------------------------------------------------------------------------
# Sidebar - filtreler
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("🔎 Filtreler")
    all_categories = sorted(tenders["kategori_gorunum"].unique().tolist())
    selected_categories = st.multiselect(
        "Kategori", all_categories, default=all_categories
    )
    search_text = st.text_input("Başlıkta ara", placeholder="ör. Glasfaser, Tiefbau...")
    only_with_winner = st.checkbox("Sadece kazananı bulunanlar")

    st.divider()
    st.caption("Veri her 60 saniyede bir tazelenir. Değişiklik yaptıysan sayfayı yenile.")

filtered = tenders[tenders["kategori_gorunum"].isin(selected_categories)]
if search_text:
    filtered = filtered[filtered["title"].str.contains(search_text, case=False, na=False)]
if only_with_winner:
    filtered = filtered[filtered["id"].isin(winners["tender_id"])]

# ---------------------------------------------------------------------------
# Sekmeler
# ---------------------------------------------------------------------------
tab_ihaleler, tab_kisiler, tab_yeni_tarama = st.tabs(
    ["📄 İhaleler", "👤 Kişiler", "🔍 Yeni Tarama"]
)

with tab_ihaleler:
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Toplam ihale", len(tenders))
    col2.metric("Kategorize edilen", int(tenders["category"].notna().sum()))
    col3.metric("Kazananı bulunan", int(tenders["id"].isin(winners["tender_id"]).sum()))
    col4.metric("Toplam tahmini değer", format_eur(tenders["value_eur"].sum()))

    st.subheader("Kategoriye göre dağılım")
    st.bar_chart(tenders["kategori_gorunum"].value_counts())

    st.subheader(f"İhaleler ({len(filtered)})")
    st.dataframe(
        filtered[[
            "title", "kategori_gorunum", "durum_gorunum", "issuer_name",
            "published_date", "deger_gorunum", "ted_url",
        ]].rename(columns={
            "title": "Başlık",
            "kategori_gorunum": "Kategori",
            "durum_gorunum": "Durum",
            "issuer_name": "İhale Veren",
            "published_date": "Yayın Tarihi",
            "deger_gorunum": "Tahmini Değer",
            "ted_url": "TED Linki",
        }),
        width="stretch",
        hide_index=True,
        column_config={
            "TED Linki": st.column_config.LinkColumn("TED Linki", display_text="Görüntüle"),
        },
    )

    st.subheader("Kazananlar / iletişim bilgileri")
    if filtered.empty:
        st.info("Filtreye uyan ihale yok.")
    else:
        options = filtered["id"].tolist()
        titles_by_id = dict(zip(filtered["id"], filtered["title"]))
        selected_tender_id = st.selectbox(
            "Detay için ihale seç",
            options=options,
            format_func=lambda tid: titles_by_id.get(tid, str(tid)),
        )
        tender_winners = winners[winners["tender_id"] == selected_tender_id]
        if tender_winners.empty:
            st.info("Bu ihale için kazanan bilgisi yok (henüz açıklanmamış ya da TED'de yer almıyor).")
        else:
            st.dataframe(
                tender_winners[["winner_name", "email", "phone", "address", "nuts_code"]].rename(columns={
                    "winner_name": "Şirket",
                    "email": "E-posta",
                    "phone": "Telefon",
                    "address": "Adres",
                    "nuts_code": "NUTS Kodu",
                }),
                width="stretch",
                hide_index=True,
            )

with tab_kisiler:
    st.subheader("👤 Bulunan kişiler")
    st.caption(
        "`find_contacts.py`'nin LinkedIn/Xing arama sonuçlarından bulduğu kişiler. "
        "Hiçbir profil sayfası otomatik çekilmedi - bilgiler doğruysa **Onaylandı** kutusunu "
        "işaretleyip kaydet, yanlışsa düzelt ya da sil."
    )

    contacts = load_contacts()
    if contacts.empty:
        st.info(
            "Henüz bulunan kişi yok. `python scripts/find_contacts.py --limit 10` çalıştırarak "
            "kazanan/ihale veren şirketler için kişi arayabilirsin."
        )
    else:
        colA, colB = st.columns(2)
        colA.metric("Toplam bulunan kişi", len(contacts))
        colB.metric("Onaylanan", int(contacts["verified"].sum()))

        show_only_unverified = st.checkbox("Sadece onay bekleyenleri göster", value=True)
        display_contacts = contacts if not show_only_unverified else contacts[~contacts["verified"]]

        if display_contacts.empty:
            st.success("Onay bekleyen kişi yok - hepsi incelendi. 🎉")
        else:
            display_contacts = display_contacts.assign(
                kaynak_gorunum=display_contacts["source"].apply(source_label)
            )
            edited = st.data_editor(
                display_contacts[[
                    "id", "company_name", "full_name", "title", "email", "phone",
                    "kaynak_gorunum", "verified",
                ]].rename(columns={
                    "id": "ID",
                    "company_name": "Şirket",
                    "full_name": "İsim",
                    "title": "Pozisyon / Başlık",
                    "email": "E-posta",
                    "phone": "Telefon",
                    "kaynak_gorunum": "Kaynak",
                    "verified": "Onaylandı",
                }),
                column_config={
                    "ID": st.column_config.NumberColumn(disabled=True),
                    "Şirket": st.column_config.TextColumn(disabled=True),
                    "Kaynak": st.column_config.TextColumn(disabled=True),
                    "Onaylandı": st.column_config.CheckboxColumn(),
                },
                hide_index=True,
                width="stretch",
                key="contacts_editor",
            )

            col_save, col_del = st.columns([1, 1])
            with col_save:
                if st.button("💾 Değişiklikleri kaydet", type="primary", width="stretch"):
                    changed = 0
                    by_id = display_contacts.set_index("id")
                    for _, row in edited.iterrows():
                        original = by_id.loc[row["ID"]]
                        new_values = (row["İsim"], row["Pozisyon / Başlık"], row["E-posta"], row["Telefon"], bool(row["Onaylandı"]))
                        old_values = (original["full_name"], original["title"], original["email"], original["phone"], bool(original["verified"]))
                        if new_values != old_values:
                            update_contact(row["ID"], *new_values)
                            changed += 1
                    if changed:
                        load_contacts.clear()
                        st.success(f"{changed} kişi güncellendi.")
                        st.rerun()
                    else:
                        st.info("Kaydedilecek bir değişiklik yok.")
            with col_del:
                to_delete = st.number_input(
                    "Silinecek kişi ID (yanlış bulunan kayıt için)",
                    min_value=0, step=1, value=0,
                )
                if st.button("🗑️ Kişiyi sil", width="stretch") and to_delete:
                    delete_contact(int(to_delete))
                    load_contacts.clear()
                    st.success(f"ID {int(to_delete)} silindi.")
                    st.rerun()

BUNDESLAND_DISPLAY = [
    "Baden-Württemberg", "Bayern", "Berlin", "Brandenburg", "Bremen",
    "Hamburg", "Hessen", "Mecklenburg-Vorpommern", "Niedersachsen",
    "Nordrhein-Westfalen", "Rheinland-Pfalz", "Saarland", "Sachsen",
    "Sachsen-Anhalt", "Schleswig-Holstein", "Thüringen",
]
TUM_ALMANYA = "🇩🇪 Tüm Almanya"

with tab_yeni_tarama:
    st.subheader("🔍 Bölge / tarih bazlı yeni tarama")
    st.caption(
        "Burada seçtiğin bölge ve tarih aralığı için TED'den ihale çeker, sınıflandırır "
        "ve istersen kazanan/ihale veren şirketler için kişi de arar. Bu, günlük otomatik "
        "taramaya ek olarak - istediğin an elle tetikleyebileceğin bir tarama."
    )

    with st.expander("ℹ️ Bölge filtresi hakkında bilinmesi gereken bir şey"):
        st.markdown(
            "TED'in arama alanı NUTS bölge koduna göre filtreleniyor (ör. Bayern → `DE2`). "
            "Bu davranış TED'in kendi API'sine karşı canlı test edilemedi (bu ortamdan o "
            "API'ye erişim yok). **Güvenmeden önce aşağıdaki '🔎 Sadece say' butonuyla "
            "'Tüm Almanya' ile seçtiğin bölgenin eşleşme sayılarını karşılaştır** - bölge "
            "sayısı belirgin şekilde küçükse filtre çalışıyor demektir; ikisi aynıysa bana haber ver."
        )

    col_region, col_since, col_until = st.columns(3)
    with col_region:
        region_choice = st.selectbox("Bölge (Bundesland)", [TUM_ALMANYA] + BUNDESLAND_DISPLAY)
    with col_since:
        since_date = st.date_input("Başlangıç tarihi", value=date.today() - timedelta(days=60))
    with col_until:
        until_date = st.date_input("Bitiş tarihi", value=date.today())

    tender_limit = st.slider("Kaç ihale işlensin (üst sınır)", min_value=5, max_value=300, value=50, step=5)

    place_code = ted_ingest.resolve_place_code(None if region_choice == TUM_ALMANYA else region_choice)

    if st.button("🔎 Sadece say (TED'e yazmadan test et)"):
        _ensure_env_vars()
        try:
            total_all = ted_ingest.count_notices(str(since_date), str(until_date), "DEU")
            total_region = (
                total_all if region_choice == TUM_ALMANYA
                else ted_ingest.count_notices(str(since_date), str(until_date), place_code)
            )
        except Exception as exc:
            st.error(f"TED'e ulaşılamadı: {exc}")
        else:
            c1, c2 = st.columns(2)
            c1.metric("Tüm Almanya'da eşleşme", total_all if total_all is not None else "—")
            c2.metric(f"{region_choice}'de eşleşme", total_region if total_region is not None else "—")
            if region_choice != TUM_ALMANYA and total_region == total_all and total_all:
                st.warning(
                    "Bölge sayısı Tüm Almanya ile birebir aynı çıktı - bölge filtresi bu "
                    "seçim için etkisiz kalmış olabilir, bana haber ver."
                )

    st.divider()

    do_contacts = st.checkbox("İhalelerden sonra kişi araması da yap", value=True)
    if do_contacts:
        contact_limit = st.number_input(
            "Kişi araması için en fazla kaç şirket işlensin", min_value=1, max_value=100, value=20,
        )
        auto_website = st.checkbox(
            "Şirket sitesinden e-posta/telefon da bul (fazladan SerpAPI sorgusu kullanır)",
            value=True,
            help="Kapalıysa sadece LinkedIn/Xing'den isim/pozisyon bulunur, e-posta/telefon hiç gelmez.",
        )
    else:
        contact_limit, auto_website = 0, False

    if st.button("🚀 Taramayı Başlat", type="primary"):
        _ensure_env_vars()
        logs: list[str] = []
        ingest_summary = contacts_summary = None
        with st.spinner("İhaleler TED'den çekiliyor, XML parse ediliyor ve sınıflandırılıyor..."):
            try:
                ingest_summary = ted_ingest.run_ingest(
                    since=str(since_date), until=str(until_date), limit=int(tender_limit),
                    place_code=place_code, dry_run=False, log=logs.append,
                )
            except RuntimeError as exc:
                st.error(f"İhale taraması başarısız: {exc}")

        if ingest_summary is not None and do_contacts:
            with st.spinner("Kazanan/ihale veren şirketler için kişi aranıyor..."):
                try:
                    contacts_summary = find_contacts.run_find_contacts(
                        limit=int(contact_limit), dry_run=False,
                        auto_discover_website=auto_website, log=logs.append,
                    )
                except RuntimeError as exc:
                    st.error(f"Kişi araması başarısız: {exc}")

        st.session_state["last_scan_log"] = "\n".join(logs)
        st.session_state["last_scan_summary"] = {
            "ingest": ingest_summary, "contacts": contacts_summary,
        }
        load_tenders.clear()
        load_winners.clear()
        load_contacts.clear()
        st.rerun()

    if "last_scan_summary" in st.session_state:
        summary = st.session_state["last_scan_summary"]
        ingest_summary = summary.get("ingest")
        contacts_summary = summary.get("contacts")
        if ingest_summary:
            st.success(
                f"Son tarama: {ingest_summary['processed']} yeni ihale işlendi, "
                f"{ingest_summary['skipped']} tanesi zaten DB'de vardı (atlandı)."
            )
        if contacts_summary:
            st.success(
                f"Kişi araması: {contacts_summary['companies_processed']} şirket işlendi, "
                f"{contacts_summary['total_saved']} yeni kişi/iletişim kaydı bulundu."
            )
        with st.expander("İşlem günlüğü (son tarama)"):
            st.code(st.session_state.get("last_scan_log", ""), language=None)
