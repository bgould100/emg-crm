-- Add append-only contractor job notes and permanent completion locks.

BEGIN;

ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS notes_locked_at TIMESTAMP;

CREATE TABLE IF NOT EXISTS job_notes (
    id BIGSERIAL PRIMARY KEY,
    note_id VARCHAR(40) UNIQUE NOT NULL,
    job_id BIGINT NOT NULL REFERENCES jobs(id),
    contractor_id BIGINT NOT NULL REFERENCES contractors(id),
    note_text VARCHAR(600) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT job_notes_text_length_check
        CHECK (
            char_length(btrim(note_text)) BETWEEN 1 AND 600
        )
);

CREATE INDEX IF NOT EXISTS idx_job_notes_job_created
    ON job_notes (job_id, created_at, id);

COMMIT;