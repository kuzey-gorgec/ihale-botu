"""
Basit Streamlit dashboard - ihale-botu Postgres DB'sindeki ihaleleri/kazananlari
gosterir. n8n'in gunluk doldurdugu tabloyu incelemek icin.

Kullanim:
    pip install streamlit pandas
    streamlit run dashboard.py

Varsayilan olarak http://localhost:8501 adresinde acilir.
"""
import os
from pathlib import Path

import pandas as pd
import psycopg2
import streamlit as st
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

st.set_page_config(page_title="Almanya Fiber Ihale Botu", layout="wide")


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


@st.cache_resource
def get_connection():
    database_url = _get_database_url()
    if not database_url:
        st.error(
            "DATABASE_URL tanimli degil. Yerelde .env dosyasina bak, "
            "Streamlit Cloud'da uygulama Settings > Secrets kismina ekle."
        )
        st.stop()
    return psycopg2.connect(database_url)


@st.cache_data(ttl=60)
def load_tenders() -> pd.DataFrame:
    conn = get_connection()
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
    return pd.read_sql(query, conn)


@st.cache_data(ttl=60)
def load_winners() -> pd.DataFrame:
    conn = get_connection()
    query = """
        SELECT
            tc.tender_id, c.name AS winner_name, c.email, c.phone,
            c.address, c.nuts_code
        FROM tender_companies tc
        JOIN companies c ON c.id = tc.company_id
        WHERE tc.role = 'winner'
    """
    return pd.read_sql(query, conn)


tenders = load_tenders()
winners = load_winners()

st.title("Almanya Fiber Ihale Botu")

if tenders.empty:
    st.info("DB'de henuz ihale yok. Once scripts/ted_ingest.py calistir "
            "(veya n8n workflow'unun ilk calismasini bekle).")
    st.stop()

tenders["category_display"] = tenders["category"].fillna("kategorize_edilmemis")

col1, col2, col3 = st.columns(3)
col1.metric("Toplam ihale", len(tenders))
col2.metric("Kategorize edilen", int(tenders["category"].notna().sum()))
col3.metric("Kazanani bulunan", int(tenders["id"].isin(winners["tender_id"]).sum()))

st.subheader("Kategoriye gore dagilim")
st.bar_chart(tenders["category_display"].value_counts())

st.subheader("Filtreler")
all_categories = sorted(tenders["category_display"].unique().tolist())
selected_categories = st.multiselect("Kategori", all_categories, default=all_categories)
search_text = st.text_input("Baslikta ara")
only_with_winner = st.checkbox("Sadece kazanani bulunanlar")

filtered = tenders[tenders["category_display"].isin(selected_categories)]
if search_text:
    filtered = filtered[filtered["title"].str.contains(search_text, case=False, na=False)]
if only_with_winner:
    filtered = filtered[filtered["id"].isin(winners["tender_id"])]

st.subheader(f"Ihaleler ({len(filtered)})")
st.dataframe(
    filtered[[
        "title", "category", "category_source", "issuer_name",
        "published_date", "value_eur", "ted_url",
    ]],
    use_container_width=True,
    hide_index=True,
)

st.subheader("Kazananlar / iletisim bilgileri")
if filtered.empty:
    st.info("Filtreye uyan ihale yok.")
else:
    options = filtered["id"].tolist()
    titles_by_id = dict(zip(filtered["id"], filtered["title"]))
    selected_tender_id = st.selectbox(
        "Detay icin ihale sec",
        options=options,
        format_func=lambda tid: titles_by_id.get(tid, str(tid)),
    )
    tender_winners = winners[winners["tender_id"] == selected_tender_id]
    if tender_winners.empty:
        st.info("Bu ihale icin kazanan bilgisi yok (henuz aciklanmamis ya da TED'de yer almiyor).")
    else:
        st.dataframe(
            tender_winners[["winner_name", "email", "phone", "address", "nuts_code"]],
            use_container_width=True,
            hide_index=True,
        )
