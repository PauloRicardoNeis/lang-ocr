PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS capture_jobs (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    captured_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'processing', 'accepted', 'rejected', 'failed')
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    temp_image_path TEXT,
    final_image_path TEXT,
    image_sha256 TEXT,
    image_phash TEXT,
    raw_text TEXT,
    ocr_text_hash TEXT
);

CREATE INDEX IF NOT EXISTS idx_capture_jobs_status_created_at
    ON capture_jobs(status, created_at);

CREATE INDEX IF NOT EXISTS idx_capture_jobs_image_sha256
    ON capture_jobs(image_sha256);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    source_type TEXT NOT NULL CHECK (source_type IN ('image', 'text', 'manual')),
    language_code TEXT NOT NULL DEFAULT 'ja',
    ingest_mode TEXT NOT NULL DEFAULT 'manual' CHECK (
        ingest_mode IN ('manual', 'auto_capture', 'image_import')
    ),
    review_status TEXT NOT NULL DEFAULT 'reviewed' CHECK (
        review_status IN ('pending', 'reviewed', 'ignored')
    ),
    title TEXT,
    game TEXT,
    scene TEXT,
    source_path TEXT,
    raw_text TEXT,
    notes TEXT,
    captured_at TEXT,
    image_sha256 TEXT,
    ocr_text_hash TEXT,
    capture_job_id INTEGER REFERENCES capture_jobs(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sources_created_at
    ON sources(created_at);

CREATE INDEX IF NOT EXISTS idx_sources_review_status_created_at
    ON sources(review_status, created_at);

CREATE INDEX IF NOT EXISTS idx_sources_image_sha256
    ON sources(image_sha256);

CREATE TABLE IF NOT EXISTS terms (
    id INTEGER PRIMARY KEY,
    canonical_key TEXT NOT NULL UNIQUE,
    lemma TEXT NOT NULL,
    reading TEXT,
    meaning_pt TEXT,
    pos TEXT,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_terms_lemma
    ON terms(lemma);

CREATE TABLE IF NOT EXISTS term_forms (
    id INTEGER PRIMARY KEY,
    term_id INTEGER NOT NULL REFERENCES terms(id) ON DELETE CASCADE,
    form_key TEXT NOT NULL,
    surface_form TEXT NOT NULL,
    reading TEXT,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(term_id, form_key)
);

CREATE INDEX IF NOT EXISTS idx_term_forms_term_id
    ON term_forms(term_id);

CREATE TABLE IF NOT EXISTS source_terms (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    term_id INTEGER NOT NULL REFERENCES terms(id) ON DELETE CASCADE,
    term_form_id INTEGER REFERENCES term_forms(id) ON DELETE SET NULL,
    surface_form TEXT NOT NULL,
    count_in_source INTEGER NOT NULL DEFAULT 1 CHECK (count_in_source > 0),
    confidence REAL CHECK (
        confidence IS NULL OR (confidence >= 0 AND confidence <= 1)
    ),
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_id, term_id, surface_form)
);

CREATE INDEX IF NOT EXISTS idx_source_terms_term_id
    ON source_terms(term_id);

CREATE INDEX IF NOT EXISTS idx_source_terms_source_id
    ON source_terms(source_id);

CREATE TABLE IF NOT EXISTS source_segments (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    segment_key TEXT NOT NULL,
    segment_text TEXT NOT NULL,
    reading TEXT,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_id, segment_key)
);

CREATE INDEX IF NOT EXISTS idx_source_segments_source_id
    ON source_segments(source_id);

CREATE TABLE IF NOT EXISTS source_segment_terms (
    id INTEGER PRIMARY KEY,
    source_segment_id INTEGER NOT NULL REFERENCES source_segments(id) ON DELETE CASCADE,
    term_id INTEGER NOT NULL REFERENCES terms(id) ON DELETE CASCADE,
    term_form_id INTEGER REFERENCES term_forms(id) ON DELETE SET NULL,
    surface_form TEXT NOT NULL,
    count_in_segment INTEGER NOT NULL DEFAULT 1 CHECK (count_in_segment > 0),
    confidence REAL CHECK (
        confidence IS NULL OR (confidence >= 0 AND confidence <= 1)
    ),
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_segment_id, term_id, surface_form)
);

CREATE INDEX IF NOT EXISTS idx_source_segment_terms_segment_id
    ON source_segment_terms(source_segment_id);

CREATE INDEX IF NOT EXISTS idx_source_segment_terms_term_id
    ON source_segment_terms(term_id);

CREATE TABLE IF NOT EXISTS source_analysis_segments (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    segment_index INTEGER NOT NULL,
    raw_text TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    reading TEXT,
    confidence REAL CHECK (
        confidence IS NULL OR (confidence >= 0 AND confidence <= 1)
    ),
    bbox_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_id, segment_index)
);

CREATE INDEX IF NOT EXISTS idx_source_analysis_segments_source_id
    ON source_analysis_segments(source_id);

CREATE TABLE IF NOT EXISTS source_analysis_candidates (
    id INTEGER PRIMARY KEY,
    source_analysis_segment_id INTEGER NOT NULL REFERENCES source_analysis_segments(id) ON DELETE CASCADE,
    candidate_type TEXT NOT NULL CHECK (
        candidate_type IN ('term', 'subterm', 'kanji')
    ),
    surface_form TEXT NOT NULL,
    dictionary_form TEXT NOT NULL,
    reading TEXT,
    pos TEXT,
    confidence REAL CHECK (
        confidence IS NULL OR (confidence >= 0 AND confidence <= 1)
    ),
    rank INTEGER NOT NULL DEFAULT 0,
    parent_candidate_id INTEGER REFERENCES source_analysis_candidates(id) ON DELETE SET NULL,
    matched_term_id INTEGER REFERENCES terms(id) ON DELETE SET NULL,
    matched_kanji_id INTEGER REFERENCES kanji(id) ON DELETE SET NULL,
    approval_status TEXT NOT NULL DEFAULT 'pending' CHECK (
        approval_status IN ('pending', 'approved', 'rejected')
    ),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_source_analysis_candidates_segment_id
    ON source_analysis_candidates(source_analysis_segment_id);

CREATE INDEX IF NOT EXISTS idx_source_analysis_candidates_approval_status
    ON source_analysis_candidates(approval_status);

CREATE TABLE IF NOT EXISTS term_stats (
    term_id INTEGER PRIMARY KEY REFERENCES terms(id) ON DELETE CASCADE,
    total_occurrences INTEGER NOT NULL DEFAULT 0,
    distinct_sources INTEGER NOT NULL DEFAULT 0,
    distinct_forms INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT,
    last_seen_at TEXT,
    priority_score REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS kanji (
    id INTEGER PRIMARY KEY,
    literal TEXT NOT NULL UNIQUE,
    meaning_pt TEXT,
    canonical_meanings TEXT,
    on_readings TEXT,
    kun_readings TEXT,
    name_readings TEXT,
    metadata_updated_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS source_kanji (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    kanji_id INTEGER NOT NULL REFERENCES kanji(id) ON DELETE CASCADE,
    count_in_source INTEGER NOT NULL DEFAULT 1 CHECK (count_in_source > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_id, kanji_id)
);

CREATE INDEX IF NOT EXISTS idx_source_kanji_kanji_id
    ON source_kanji(kanji_id);

CREATE INDEX IF NOT EXISTS idx_source_kanji_source_id
    ON source_kanji(source_id);

CREATE TABLE IF NOT EXISTS kanji_stats (
    kanji_id INTEGER PRIMARY KEY REFERENCES kanji(id) ON DELETE CASCADE,
    total_occurrences INTEGER NOT NULL DEFAULT 0,
    distinct_sources INTEGER NOT NULL DEFAULT 0,
    distinct_terms INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT,
    last_seen_at TEXT,
    priority_score REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS flashcards (
    id INTEGER PRIMARY KEY,
    term_id INTEGER NOT NULL REFERENCES terms(id) ON DELETE CASCADE,
    source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    template_name TEXT NOT NULL DEFAULT 'recognition_basic',
    deck_name TEXT NOT NULL DEFAULT 'Lang::Japanese',
    note_type TEXT NOT NULL DEFAULT 'Basic',
    front_text TEXT NOT NULL,
    back_text TEXT NOT NULL,
    tags TEXT,
    anki_note_id INTEGER,
    export_status TEXT NOT NULL DEFAULT 'pending' CHECK (
        export_status IN ('pending', 'exported')
    ),
    exported_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(term_id, template_name)
);

CREATE INDEX IF NOT EXISTS idx_flashcards_export_status
    ON flashcards(export_status);

CREATE INDEX IF NOT EXISTS idx_flashcards_anki_note_id
    ON flashcards(anki_note_id);

CREATE TABLE IF NOT EXISTS kanji_flashcards (
    id INTEGER PRIMARY KEY,
    kanji_id INTEGER NOT NULL REFERENCES kanji(id) ON DELETE CASCADE,
    source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    template_name TEXT NOT NULL DEFAULT 'kanji_basic',
    deck_name TEXT NOT NULL DEFAULT 'Lang::Japanese',
    note_type TEXT NOT NULL DEFAULT 'Basic',
    front_text TEXT NOT NULL,
    back_text TEXT NOT NULL,
    tags TEXT,
    anki_note_id INTEGER,
    export_status TEXT NOT NULL DEFAULT 'pending' CHECK (
        export_status IN ('pending', 'exported')
    ),
    exported_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(kanji_id, template_name)
);

CREATE INDEX IF NOT EXISTS idx_kanji_flashcards_export_status
    ON kanji_flashcards(export_status);

CREATE INDEX IF NOT EXISTS idx_kanji_flashcards_anki_note_id
    ON kanji_flashcards(anki_note_id);

CREATE VIEW IF NOT EXISTS v_terms_overview AS
SELECT
    t.id,
    t.lemma,
    t.reading,
    t.meaning_pt,
    t.pos,
    COALESCE(ts.total_occurrences, 0) AS total_occurrences,
    COALESCE(ts.distinct_sources, 0) AS distinct_sources,
    COALESCE(ts.distinct_forms, 0) AS distinct_forms,
    COALESCE(ts.priority_score, 0) AS priority_score,
    ts.first_seen_at,
    ts.last_seen_at
FROM terms AS t
LEFT JOIN term_stats AS ts
    ON ts.term_id = t.id;

CREATE VIEW IF NOT EXISTS v_kanji_overview AS
SELECT
    k.id,
    k.literal,
    k.meaning_pt,
    k.canonical_meanings,
    k.on_readings,
    k.kun_readings,
    k.name_readings,
    k.metadata_updated_at,
    COALESCE(ks.total_occurrences, 0) AS total_occurrences,
    COALESCE(ks.distinct_sources, 0) AS distinct_sources,
    COALESCE(ks.distinct_terms, 0) AS distinct_terms,
    COALESCE(ks.priority_score, 0) AS priority_score,
    ks.first_seen_at,
    ks.last_seen_at,
    k.created_at,
    k.updated_at
FROM kanji AS k
LEFT JOIN kanji_stats AS ks
    ON ks.kanji_id = k.id;

CREATE VIEW IF NOT EXISTS v_flashcards_overview AS
SELECT
    f.id,
    f.template_name,
    f.deck_name,
    f.note_type,
    f.front_text,
    f.back_text,
    f.tags,
    f.anki_note_id,
    f.export_status,
    f.exported_at,
    f.created_at,
    t.lemma,
    t.reading,
    t.meaning_pt,
    t.pos,
    f.source_id
FROM flashcards AS f
JOIN terms AS t
    ON t.id = f.term_id;
