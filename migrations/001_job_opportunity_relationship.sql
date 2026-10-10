BEGIN;

-- Add the relationship as nullable so existing jobs can be backfilled.
ALTER TABLE jobs
    ADD COLUMN opportunity_id BIGINT;

-- Stop the migration if any existing job cannot be linked to an
-- accepted opportunity. The transaction will roll back on this error.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM jobs j
        LEFT JOIN opportunities o
          ON o.lead_id = j.lead_id
         AND o.contractor_id = j.contractor_id
        WHERE o.id IS NULL
           OR o.status <> 'ACCEPTED'
    ) THEN
        RAISE EXCEPTION
            'Migration stopped: a job has no matching accepted opportunity.';
    END IF;
END
$$;

-- Link each existing job to the opportunity that created it.
UPDATE jobs j
SET opportunity_id = o.id
FROM opportunities o
WHERE o.lead_id = j.lead_id
  AND o.contractor_id = j.contractor_id
  AND o.status = 'ACCEPTED';

-- Require every job to have a valid, unique opportunity relationship.
ALTER TABLE jobs
    ALTER COLUMN opportunity_id SET NOT NULL;

ALTER TABLE jobs
    ADD CONSTRAINT jobs_opportunity_id_fkey
    FOREIGN KEY (opportunity_id) REFERENCES opportunities(id);

ALTER TABLE jobs
    ADD CONSTRAINT jobs_opportunity_id_key
    UNIQUE (opportunity_id);

COMMIT;
