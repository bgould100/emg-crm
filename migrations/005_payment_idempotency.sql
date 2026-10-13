BEGIN;

ALTER TABLE job_collections
    ADD COLUMN idempotency_key VARCHAR(100);

CREATE UNIQUE INDEX uq_job_collections_contractor_idempotency
    ON job_collections (contractor_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

COMMIT;
