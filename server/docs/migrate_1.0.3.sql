-- DedSec auth server 1.0.3 schema changes (SQLite).
-- FOR REFERENCE ONLY: the server applies these itself on start
--   (app/db.py add_missing_columns() + Base.metadata.create_all), idempotently.
-- Run by hand only if you want to pre-migrate; each ALTER fails harmlessly if the column exists.
-- All new columns are nullable, so 1.0.2 server code keeps working on the migrated DB (rollback-safe).

ALTER TABLE users ADD COLUMN email_2fa_enabled BOOLEAN;
ALTER TABLE users ADD COLUMN email_otp_hash VARCHAR(64);
ALTER TABLE users ADD COLUMN email_otp_expires DATETIME;
ALTER TABLE users ADD COLUMN email_otp_attempts INTEGER;
ALTER TABLE users ADD COLUMN email_otp_purpose VARCHAR(16);

ALTER TABLE login_challenges ADD COLUMN method VARCHAR(10);
ALTER TABLE login_challenges ADD COLUMN code_hash VARCHAR(64);
ALTER TABLE login_challenges ADD COLUMN resends INTEGER;

CREATE TABLE IF NOT EXISTS password_resets (
	id INTEGER NOT NULL,
	user_id VARCHAR(36) NOT NULL,
	created_at DATETIME NOT NULL,
	code_hash VARCHAR(64) NOT NULL,
	code_expires_at DATETIME NOT NULL,
	attempts INTEGER NOT NULL,
	code_used BOOLEAN NOT NULL,
	link_token_hash VARCHAR(64),
	link_expires_at DATETIME,
	used BOOLEAN NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_password_resets_link_token_hash ON password_resets (link_token_hash);
CREATE INDEX IF NOT EXISTS ix_password_resets_user_id ON password_resets (user_id);
