-- Şirketler (ihale veren, kazanan, hazırlayan — hepsi bu tabloda, rol tender bazlı ayrılıyor)
CREATE TABLE IF NOT EXISTS companies (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    website TEXT,
    created_at TIMESTAMP DEFAULT now()
);

-- İhaleler
CREATE TABLE IF NOT EXISTS tenders (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    bundesland TEXT,               -- eyalet
    region TEXT,                   -- bölge/şehir
    source_portal TEXT,            -- hangi portaldan geldi
    source_url TEXT,
    -- kategori seti: tiefbau_kazi / netzbetrieb_konzesyon / planlama /
    -- genel_yuklenici / pasif_altyapi / cerceve_sozlesme / alakasiz
    category TEXT,
    category_source TEXT,          -- 'llm' | 'human' | 'model'
    category_confidence REAL,
    status TEXT,                   -- acik / sonuclandi / iptal
    published_date DATE,
    deadline_date DATE,
    created_at TIMESTAMP DEFAULT now()
);

-- TED entegrasyonu icin ek alanlar (incremental ekleme, mevcut tabloya dokunmadan)
ALTER TABLE tenders ADD COLUMN IF NOT EXISTS ted_publication_number TEXT;
ALTER TABLE tenders ADD COLUMN IF NOT EXISTS ted_notice_type TEXT;
ALTER TABLE tenders ADD COLUMN IF NOT EXISTS value_eur NUMERIC;
ALTER TABLE tenders ADD COLUMN IF NOT EXISTS ted_url TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS tenders_ted_pub_idx ON tenders (ted_publication_number);

ALTER TABLE companies ADD COLUMN IF NOT EXISTS email TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS phone TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS address TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS nuts_code TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS companies_name_idx ON companies (name);

-- Bir ihalede şirketin rolü (veren / kazanan / hazırlayan aynı tenderda farklı şirketler olabilir)
CREATE TABLE IF NOT EXISTS tender_companies (
    id SERIAL PRIMARY KEY,
    tender_id INT REFERENCES tenders(id),
    company_id INT REFERENCES companies(id),
    role TEXT NOT NULL             -- 'issuer' | 'winner' | 'preparer'
);

-- Kişiler
CREATE TABLE IF NOT EXISTS contacts (
    id SERIAL PRIMARY KEY,
    company_id INT REFERENCES companies(id),
    full_name TEXT,
    title TEXT,                    -- pozisyon
    email TEXT,
    phone TEXT,
    source TEXT,                   -- 'linkedin' | 'xing' | 'google'
    verified BOOLEAN DEFAULT false,-- insan onayından geçti mi
    created_at TIMESTAMP DEFAULT now()
);

-- Bir kişinin hangi ihalede hangi rolle ilişkili olduğu (kişi birden fazla ihalede geçebilir)
CREATE TABLE IF NOT EXISTS tender_contacts (
    id SERIAL PRIMARY KEY,
    tender_id INT REFERENCES tenders(id),
    contact_id INT REFERENCES contacts(id),
    relevance_role TEXT            -- hangi rolle ilgili: 'issuer' | 'winner' | 'preparer'
);

-- Mail taslakları
CREATE TABLE IF NOT EXISTS email_drafts (
    id SERIAL PRIMARY KEY,
    tender_id INT REFERENCES tenders(id),
    contact_id INT REFERENCES contacts(id),
    draft_text TEXT,
    category_used TEXT,
    status TEXT DEFAULT 'draft',   -- 'draft' | 'reviewed' | 'sent'
    created_at TIMESTAMP DEFAULT now()
);

-- Sınıflandırma modeli için etiketli eğitim verisi (bootstrap'tan biriken)
CREATE TABLE IF NOT EXISTS classification_training_data (
    id SERIAL PRIMARY KEY,
    tender_id INT REFERENCES tenders(id),
    text_snapshot TEXT,            -- eğitim anındaki metin
    label TEXT,                    -- düzeltilmiş/onaylanmış etiket
    labeled_by TEXT,               -- 'human' | 'llm'
    created_at TIMESTAMP DEFAULT now()
);

-- Mail taslağı üretiminde kullanılan gönderen (kendi) firma bilgisi - tek satır
-- (id her zaman 1). Dashboard'daki "Mail Taslakları" sekmesinden düzenlenir,
-- .env/secrets'a ihtiyaç duymadan DB'de saklanır.
CREATE TABLE IF NOT EXISTS sender_profile (
    id INT PRIMARY KEY DEFAULT 1,
    company_name TEXT,
    services_de TEXT,              -- sundugun hizmetin Almanca kisa tanimi (prompt'ta kullanilir)
    contact_name TEXT,
    phone TEXT,
    email TEXT,
    updated_at TIMESTAMP DEFAULT now(),
    CHECK (id = 1)
);