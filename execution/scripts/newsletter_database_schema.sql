-- Newsletter Subscribers Database Schema
-- Sovereignty-aligned: User-owned database for subscriber data

-- PostgreSQL Schema (Recommended for Production)
CREATE TABLE IF NOT EXISTS newsletter_subscribers (
    email VARCHAR(255) PRIMARY KEY,
    survey JSONB,
    subscribed_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    status VARCHAR(50) NOT NULL DEFAULT 'subscribed',
    icp_tier VARCHAR(20),
    unsubscribed_at TIMESTAMP WITH TIME ZONE,
    confirmation_token VARCHAR(255),
    confirmed_at TIMESTAMP WITH TIME ZONE,
    source VARCHAR(100),  -- 'website', 'email_blast', 'social', etc.
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_subscribers_status ON newsletter_subscribers(status);
CREATE INDEX IF NOT EXISTS idx_subscribers_icp_tier ON newsletter_subscribers(icp_tier);
CREATE INDEX IF NOT EXISTS idx_subscribers_subscribed_at ON newsletter_subscribers(subscribed_at);

-- Unsubscribe tokens table (for secure unsubscribe links)
CREATE TABLE IF NOT EXISTS unsubscribe_tokens (
    email VARCHAR(255) PRIMARY KEY,
    token VARCHAR(255) UNIQUE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    used_at TIMESTAMP WITH TIME ZONE
);

CREATE INDEX IF NOT EXISTS idx_unsubscribe_tokens_token ON unsubscribe_tokens(token);
CREATE INDEX IF NOT EXISTS idx_unsubscribe_tokens_expires_at ON unsubscribe_tokens(expires_at);

-- Newsletter sends tracking (for analytics)
CREATE TABLE IF NOT EXISTS newsletter_sends (
    id SERIAL PRIMARY KEY,
    issue_number VARCHAR(50),
    subject VARCHAR(255),
    sent_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    total_recipients INTEGER,
    opens INTEGER DEFAULT 0,
    clicks INTEGER DEFAULT 0,
    bounces INTEGER DEFAULT 0
);

-- Individual email tracking (for open/click tracking)
CREATE TABLE IF NOT EXISTS email_events (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) NOT NULL,
    send_id INTEGER REFERENCES newsletter_sends(id),
    event_type VARCHAR(50) NOT NULL,  -- 'sent', 'delivered', 'opened', 'clicked', 'bounced', 'unsubscribed'
    event_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    metadata JSONB
);

CREATE INDEX IF NOT EXISTS idx_email_events_email ON email_events(email);
CREATE INDEX IF NOT EXISTS idx_email_events_send_id ON email_events(send_id);
CREATE INDEX IF NOT EXISTS idx_email_events_event_type ON email_events(event_type);

-- Views for analytics
CREATE OR REPLACE VIEW subscriber_stats AS
SELECT 
    status,
    icp_tier,
    COUNT(*) as count,
    COUNT(*) FILTER (WHERE subscribed_at >= NOW() - INTERVAL '30 days') as new_last_30_days
FROM newsletter_subscribers
GROUP BY status, icp_tier;

-- Function to update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

CREATE TRIGGER update_subscribers_updated_at BEFORE UPDATE ON newsletter_subscribers
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
