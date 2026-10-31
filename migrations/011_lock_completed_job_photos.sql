-- Permanently lock contractor photo management when a job is completed.
ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS photos_locked_at TIMESTAMP;