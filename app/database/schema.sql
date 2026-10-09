CREATE TABLE leads (
    id BIGSERIAL PRIMARY KEY,
    lead_id VARCHAR(30) UNIQUE NOT NULL,
    zip_code VARCHAR(10) NOT NULL,
    service_type VARCHAR(100) NOT NULL,
    description TEXT NOT NULL,
    budget NUMERIC(12, 2),
    urgency VARCHAR(20),
    requested_bid_count INTEGER NOT NULL DEFAULT 1,
    status VARCHAR(30) NOT NULL DEFAULT 'NEW',
    customer_name VARCHAR(150),
    customer_email VARCHAR(255),
    customer_phone VARCHAR(30),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);


CREATE TABLE contractors (
    id BIGSERIAL PRIMARY KEY,
    contractor_id VARCHAR(30) UNIQUE NOT NULL,
    company_name VARCHAR(200) NOT NULL,
    contact_name VARCHAR(150),
    email VARCHAR(255) NOT NULL,
    phone VARCHAR(30),
    service_area TEXT NOT NULL,
    specialties TEXT NOT NULL,
    availability VARCHAR(30) DEFAULT 'AVAILABLE',
    status VARCHAR(30) NOT NULL DEFAULT 'ACTIVE',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);


CREATE TABLE opportunities (
    id BIGSERIAL PRIMARY KEY,
    opportunity_id VARCHAR(40) UNIQUE NOT NULL,
    lead_id BIGINT NOT NULL REFERENCES leads(id),
    contractor_id BIGINT NOT NULL REFERENCES contractors(id),
    status VARCHAR(30) NOT NULL DEFAULT 'SENT',
    match_score INTEGER,
    sent_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    responded_at TIMESTAMP,
    expires_at TIMESTAMP,
    accepted_at TIMESTAMP,
    declined_at TIMESTAMP,

    UNIQUE (lead_id, contractor_id)
);


CREATE TABLE jobs (
    id BIGSERIAL PRIMARY KEY,
    job_id VARCHAR(40) UNIQUE NOT NULL,
    lead_id BIGINT NOT NULL REFERENCES leads(id),
    contractor_id BIGINT NOT NULL REFERENCES contractors(id),
    status VARCHAR(40) NOT NULL DEFAULT 'QUOTE_IN_PROGRESS',
    quoted_amount NUMERIC(12, 2),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);


CREATE TABLE photos (
    id BIGSERIAL PRIMARY KEY,
    photo_id VARCHAR(40) UNIQUE NOT NULL,
    lead_id BIGINT NOT NULL REFERENCES leads(id),
    contractor_id BIGINT REFERENCES contractors(id),
    job_id BIGINT REFERENCES jobs(id),
    s3_key TEXT NOT NULL,
    original_filename VARCHAR(255),
    content_type VARCHAR(100),
    uploaded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);


CREATE TABLE status_history (
    id BIGSERIAL PRIMARY KEY,
    lead_id BIGINT REFERENCES leads(id),
    contractor_id BIGINT REFERENCES contractors(id),
    opportunity_id BIGINT REFERENCES opportunities(id),
    job_id BIGINT REFERENCES jobs(id),
    event_type VARCHAR(50) NOT NULL,
    previous_status VARCHAR(40),
    new_status VARCHAR(40),
    metadata JSONB,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);


CREATE INDEX idx_leads_zip_code
    ON leads(zip_code);

CREATE INDEX idx_leads_service_type
    ON leads(service_type);

CREATE INDEX idx_leads_status
    ON leads(status);

CREATE INDEX idx_contractors_status
    ON contractors(status);

CREATE INDEX idx_opportunities_lead_id
    ON opportunities(lead_id);

CREATE INDEX idx_opportunities_contractor_id
    ON opportunities(contractor_id);

CREATE INDEX idx_opportunities_status
    ON opportunities(status);

CREATE INDEX idx_jobs_lead_id
    ON jobs(lead_id);

CREATE INDEX idx_jobs_contractor_id
    ON jobs(contractor_id);

CREATE INDEX idx_status_history_lead_id
    ON status_history(lead_id);