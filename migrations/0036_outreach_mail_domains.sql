-- The other domains a company's own site shows it sending email from, beside
-- its website's (Persona AI: site persona.ai, mail personainc.ai). Replies are
-- read from Gmail by domain (opportunity_app/outreach_inbox.py), so a reply
-- from one of these is recognized as the company's. Filled by the contact
-- search (outreach_contacts.company_mail_domains); a JSON list of domains.
ALTER TABLE outreach_targets ADD COLUMN mail_domains_json TEXT NOT NULL DEFAULT '[]';
