BEGIN;

CREATE TABLE sources (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    original_filename TEXT NOT NULL,
    source_kind       TEXT NOT NULL
                      CHECK (source_kind IN ('json', 'txt', 'api', 'manual')),
    storage_path      TEXT NOT NULL,
    sha256            CHAR(64) NOT NULL UNIQUE,
    byte_size         BIGINT CHECK (byte_size >= 0),
    imported_at       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE raw_records (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id         BIGINT NOT NULL REFERENCES sources(id),
    record_index      INTEGER NOT NULL CHECK (record_index > 0),
    source_record_key TEXT,
    line_start        INTEGER CHECK (line_start > 0),
    line_end          INTEGER CHECK (line_end > 0),
    raw_content       TEXT NOT NULL,
    parsed_payload    JSONB,
    parse_status      TEXT NOT NULL DEFAULT 'pending'
                      CHECK (parse_status IN ('pending', 'parsed', 'needs_review', 'failed')),
    parse_confidence  NUMERIC(5,4) CHECK (parse_confidence BETWEEN 0 AND 1),
    parse_error       TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (source_id, record_index),
    CHECK (line_end IS NULL OR line_start IS NULL OR line_end >= line_start)
);

CREATE TABLE interviews (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    raw_record_id     BIGINT UNIQUE REFERENCES raw_records(id),
    external_id       TEXT,
    interview_date    DATE,
    client            TEXT,
    vendor            TEXT,
    interviewer       TEXT,
    candidate         TEXT,
    position          TEXT,
    interview_round   TEXT,
    employment_type   TEXT,
    notes             TEXT,
    feedback          TEXT,
    source_created_by TEXT,
    source_created_at TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE questions (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    canonical_text_en TEXT NOT NULL CHECK (BTRIM(canonical_text_en) <> ''),
    normalized_key_en TEXT NOT NULL UNIQUE CHECK (BTRIM(normalized_key_en) <> ''),
    needs_review      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE question_variants (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    question_id        BIGINT NOT NULL REFERENCES questions(id),
    original_text      TEXT NOT NULL CHECK (BTRIM(original_text) <> ''),
    language           TEXT NOT NULL CHECK (language IN ('en', 'zh', 'mixed', 'unknown')),
    translated_text_en TEXT,
    normalized_text_en TEXT,
    text_sha256        CHAR(64) NOT NULL,
    match_method       TEXT NOT NULL
                       CHECK (match_method IN ('new', 'exact', 'normalized', 'translated', 'semantic', 'manual')),
    match_confidence   NUMERIC(5,4) CHECK (match_confidence BETWEEN 0 AND 1),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (question_id, language, text_sha256)
);

CREATE TABLE question_occurrences (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    interview_id        BIGINT NOT NULL REFERENCES interviews(id),
    variant_id          BIGINT NOT NULL REFERENCES question_variants(id),
    source_question_key TEXT,
    sequence_no         INTEGER NOT NULL CHECK (sequence_no > 0),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (interview_id, sequence_no)
);

CREATE TABLE tags (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    slug       TEXT NOT NULL UNIQUE,
    name_en    TEXT NOT NULL,
    name_zh    TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE question_tags (
    question_id BIGINT NOT NULL REFERENCES questions(id),
    tag_id      BIGINT NOT NULL REFERENCES tags(id),
    is_primary  BOOLEAN NOT NULL DEFAULT FALSE,
    assigned_by TEXT NOT NULL DEFAULT 'manual'
                CHECK (assigned_by IN ('rule', 'model', 'manual')),
    confidence  NUMERIC(5,4) CHECK (confidence BETWEEN 0 AND 1),
    PRIMARY KEY (question_id, tag_id)
);

CREATE UNIQUE INDEX one_primary_tag_per_question
    ON question_tags (question_id) WHERE is_primary = TRUE;
CREATE INDEX question_variants_question_idx ON question_variants (question_id);
CREATE INDEX question_occurrences_interview_idx ON question_occurrences (interview_id);
CREATE INDEX question_occurrences_variant_idx ON question_occurrences (variant_id);
CREATE INDEX question_tags_tag_idx ON question_tags (tag_id);
CREATE INDEX interviews_date_idx ON interviews (interview_date DESC);

CREATE VIEW question_frequency AS
SELECT
    q.id AS question_id,
    q.canonical_text_en,
    COUNT(DISTINCT qo.interview_id) AS interview_frequency,
    COUNT(qo.id) AS total_mentions,
    COUNT(DISTINCT qv.id) AS variant_count
FROM questions q
LEFT JOIN question_variants qv ON qv.question_id = q.id
LEFT JOIN question_occurrences qo ON qo.variant_id = qv.id
GROUP BY q.id, q.canonical_text_en;

CREATE VIEW question_interview_history AS
SELECT
    q.id AS question_id,
    q.canonical_text_en,
    qo.id AS occurrence_id,
    i.id AS interview_id,
    i.external_id,
    i.interview_date,
    i.client,
    i.vendor,
    i.position,
    i.interview_round,
    i.interviewer,
    i.candidate,
    qv.original_text,
    qv.language,
    qv.translated_text_en
FROM questions q
JOIN question_variants qv ON qv.question_id = q.id
JOIN question_occurrences qo ON qo.variant_id = qv.id
JOIN interviews i ON i.id = qo.interview_id;

COMMIT;
