BEGIN TRANSACTION;
CREATE TABLE activity_log (
	id INTEGER NOT NULL, 
	org_id INTEGER NOT NULL, 
	bug_id INTEGER, 
	entity_type VARCHAR(40) NOT NULL, 
	entity_id INTEGER, 
	actor_user_id INTEGER, 
	actor_name VARCHAR(120) NOT NULL, 
	action VARCHAR(60) NOT NULL, 
	detail TEXT NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(org_id) REFERENCES organizations (id) ON DELETE CASCADE, 
	FOREIGN KEY(bug_id) REFERENCES bugs (id) ON DELETE SET NULL, 
	FOREIGN KEY(actor_user_id) REFERENCES users (id) ON DELETE SET NULL
);
INSERT INTO "activity_log" VALUES(1,1,1,'bug',1,2,'Legacy Lead','bug_created','x','2026-10-02 09:03:46.000000');
CREATE TABLE attachments (
	id INTEGER NOT NULL, 
	bug_id INTEGER NOT NULL, 
	comment_id INTEGER, 
	uploader_user_id INTEGER, 
	uploader_name VARCHAR(120) NOT NULL, 
	filename VARCHAR(255) NOT NULL, 
	content_type VARCHAR(120) NOT NULL, 
	size_bytes INTEGER NOT NULL, 
	data BLOB NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(bug_id) REFERENCES bugs (id) ON DELETE CASCADE, 
	FOREIGN KEY(comment_id) REFERENCES comments (id) ON DELETE CASCADE, 
	FOREIGN KEY(uploader_user_id) REFERENCES users (id) ON DELETE SET NULL
);
CREATE TABLE bug_assignees (
	bug_id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	PRIMARY KEY (bug_id, user_id), 
	FOREIGN KEY(bug_id) REFERENCES bugs (id) ON DELETE CASCADE, 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);
INSERT INTO "bug_assignees" VALUES(1,3);
CREATE TABLE bug_custom_values (
	id INTEGER NOT NULL, 
	bug_id INTEGER NOT NULL, 
	field_id INTEGER NOT NULL, 
	value TEXT NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_bcv_bug_field UNIQUE (bug_id, field_id), 
	FOREIGN KEY(bug_id) REFERENCES bugs (id) ON DELETE CASCADE, 
	FOREIGN KEY(field_id) REFERENCES custom_fields (id) ON DELETE CASCADE
);
CREATE TABLE bugs (
	id INTEGER NOT NULL, 
	project_id INTEGER NOT NULL, 
	reporter_id INTEGER, 
	item_type VARCHAR(20) DEFAULT 'Bug' NOT NULL, 
	event_id INTEGER, 
	title VARCHAR(200) NOT NULL, 
	description TEXT NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	priority VARCHAR(20) NOT NULL, 
	environment VARCHAR(10) NOT NULL, 
	due_date VARCHAR(10), 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE, 
	FOREIGN KEY(reporter_id) REFERENCES users (id) ON DELETE SET NULL, 
	FOREIGN KEY(event_id) REFERENCES events (id) ON DELETE SET NULL
);
INSERT INTO "bugs" VALUES(1,1,2,'Bug',NULL,'Legacy bug','d','New','High','PROD',NULL,'2026-10-02 09:03:46.000000','2026-10-02 09:03:46.000000');
CREATE TABLE comments (
	id INTEGER NOT NULL, 
	bug_id INTEGER NOT NULL, 
	author_user_id INTEGER, 
	author_name VARCHAR(120) NOT NULL, 
	body TEXT NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(bug_id) REFERENCES bugs (id) ON DELETE CASCADE, 
	FOREIGN KEY(author_user_id) REFERENCES users (id) ON DELETE SET NULL
);
CREATE TABLE custom_fields (
	id INTEGER NOT NULL, 
	project_id INTEGER NOT NULL, 
	name VARCHAR(80) NOT NULL, 
	field_type VARCHAR(20) NOT NULL, 
	options VARCHAR(500) NOT NULL, 
	is_required BOOLEAN NOT NULL, 
	position INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_cf_project_name UNIQUE (project_id, name), 
	FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE
);
CREATE TABLE device_tokens (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	token VARCHAR(512) NOT NULL, 
	platform VARCHAR(16) NOT NULL, 
	last_seen_at DATETIME, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE, 
	UNIQUE (token)
);
INSERT INTO "device_tokens" VALUES(1,3,'legacy-fcm-token-0000000001','android','2026-10-02 09:03:46.000000','2026-10-02 09:03:46.000000');
CREATE TABLE email_change_requests (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	new_email VARCHAR(254) NOT NULL, 
	code_hash VARCHAR(64) NOT NULL, 
	expires_at DATETIME NOT NULL, 
	used_at DATETIME, 
	attempts INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);
CREATE TABLE event_managers (
	event_id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	PRIMARY KEY (event_id, user_id), 
	FOREIGN KEY(event_id) REFERENCES events (id) ON DELETE CASCADE, 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);
CREATE TABLE events (
	id INTEGER NOT NULL, 
	org_id INTEGER NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	description TEXT NOT NULL, 
	scheduled_for VARCHAR(10), 
	created_by_user_id INTEGER, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(org_id) REFERENCES organizations (id) ON DELETE CASCADE, 
	FOREIGN KEY(created_by_user_id) REFERENCES users (id) ON DELETE SET NULL
);
CREATE TABLE invitations (
	id INTEGER NOT NULL, 
	org_id INTEGER NOT NULL, 
	email VARCHAR(254) NOT NULL, 
	role VARCHAR(20) NOT NULL, 
	token_hash VARCHAR(64) NOT NULL, 
	invited_by_user_id INTEGER, 
	invited_by_name VARCHAR(120) NOT NULL, 
	initial_project_ids VARCHAR(500) NOT NULL, 
	expires_at DATETIME NOT NULL, 
	accepted_at DATETIME, 
	revoked_at DATETIME, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(org_id) REFERENCES organizations (id) ON DELETE CASCADE, 
	UNIQUE (token_hash), 
	FOREIGN KEY(invited_by_user_id) REFERENCES users (id) ON DELETE SET NULL
);
CREATE TABLE notification_preferences (
	user_id INTEGER NOT NULL, 
	mentions BOOLEAN NOT NULL, 
	assignments BOOLEAN NOT NULL, 
	activity BOOLEAN NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (user_id), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);
INSERT INTO "notification_preferences" VALUES(3,1,0,1,'2026-10-02 09:03:46.000000');
CREATE TABLE organizations (
	id INTEGER NOT NULL, 
	name VARCHAR(120) NOT NULL, 
	slug VARCHAR(80) NOT NULL, 
	description TEXT NOT NULL, 
	logo_data_url TEXT, 
	accent_color VARCHAR(16), 
	email_from_override VARCHAR(254), 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (slug)
);
INSERT INTO "organizations" VALUES(1,'Legacy Corp','legacy-corp','old tenant',NULL,NULL,NULL,'2026-10-02 09:03:46.000000','2026-10-02 09:03:46.000000');
INSERT INTO "organizations" VALUES(2,'Second Corp','second-corp','',NULL,NULL,NULL,'2026-10-02 09:03:46.000000','2026-10-02 09:03:46.000000');
CREATE TABLE password_reset_tokens (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	token_hash VARCHAR(64) NOT NULL, 
	expires_at DATETIME NOT NULL, 
	used_at DATETIME, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE, 
	UNIQUE (token_hash)
);
CREATE TABLE project_memberships (
	id INTEGER NOT NULL, 
	project_id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	role VARCHAR(20) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_pm_project_user UNIQUE (project_id, user_id), 
	FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE, 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);
INSERT INTO "project_memberships" VALUES(1,1,2,'lead','2026-10-02 09:03:46.000000');
INSERT INTO "project_memberships" VALUES(2,1,3,'member','2026-10-02 09:03:46.000000');
CREATE TABLE projects (
	id INTEGER NOT NULL, 
	org_id INTEGER NOT NULL, 
	name VARCHAR(120) NOT NULL, 
	"key" VARCHAR(16) NOT NULL, 
	description TEXT NOT NULL, 
	color VARCHAR(20) NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_projects_org_name UNIQUE (org_id, name), 
	CONSTRAINT uq_projects_org_key UNIQUE (org_id, "key"), 
	FOREIGN KEY(org_id) REFERENCES organizations (id) ON DELETE CASCADE
);
INSERT INTO "projects" VALUES(1,1,'Billing','','','#112233','2026-10-02 09:03:46.000000','2026-10-02 09:03:46.000000');
INSERT INTO "projects" VALUES(2,2,'Billing','','','#445566','2026-10-02 09:03:46.000000','2026-10-02 09:03:46.000000');
CREATE TABLE saved_views (
	id INTEGER NOT NULL, 
	org_id INTEGER NOT NULL, 
	owner_user_id INTEGER NOT NULL, 
	name VARCHAR(80) NOT NULL, 
	filters_json TEXT NOT NULL, 
	shared_with_org BOOLEAN NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(org_id) REFERENCES organizations (id) ON DELETE CASCADE, 
	FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE CASCADE
);
CREATE TABLE sessions (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	jti VARCHAR(64) NOT NULL, 
	user_agent VARCHAR(400) NOT NULL, 
	ip_address VARCHAR(64) NOT NULL, 
	created_at DATETIME NOT NULL, 
	last_seen_at DATETIME NOT NULL, 
	expires_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE, 
	UNIQUE (jti)
);
CREATE TABLE totp_recovery_codes (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	code_hash VARCHAR(64) NOT NULL, 
	used_at DATETIME, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE, 
	UNIQUE (code_hash)
);
CREATE TABLE users (
	id INTEGER NOT NULL, 
	org_id INTEGER NOT NULL, 
	name VARCHAR(120) NOT NULL, 
	email VARCHAR(254) NOT NULL, 
	role VARCHAR(20) NOT NULL, 
	is_active BOOLEAN NOT NULL, 
	password_hash VARCHAR(120), 
	session_version INTEGER NOT NULL, 
	totp_secret VARCHAR(64), 
	totp_enabled BOOLEAN NOT NULL, 
	totp_enrolled_at DATETIME, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(org_id) REFERENCES organizations (id) ON DELETE CASCADE, 
	UNIQUE (email)
);
INSERT INTO "users" VALUES(1,1,'Legacy Admin','admin@legacy.test','admin',1,'$2b$10$LGbgsLcYSWtlHFgfGXzRQ.LBhWqT0kH0EMWmqTo8ZhBHCu8wMvgey',0,NULL,0,NULL,'2026-10-02 09:03:46.000000','2026-10-02 09:03:46.000000');
INSERT INTO "users" VALUES(2,1,'Legacy Lead','lead@legacy.test','member',1,'$2b$10$ixpS4/J1B1mc43YSxkNiAuiNmFQfyjJ.tLa/xCDXyq2SkJZLk0bAS',0,NULL,0,NULL,'2026-10-02 09:03:46.000000','2026-10-02 09:03:46.000000');
INSERT INTO "users" VALUES(3,1,'Legacy Member','member@legacy.test','member',1,'$2b$10$eKeE.d5Wx.STvEZpvZM1muWC7yYKHT.F6IVEzjI2CxfA6NqGnb0B6',0,NULL,0,NULL,'2026-10-02 09:03:46.000000','2026-10-02 09:03:46.000000');
INSERT INTO "users" VALUES(4,2,'Second Admin','admin@second.test','admin',1,'$2b$10$GVvaihNxxgBq3kjBDds1E..Rfyqo6wF5gy2SeWfpN8Ge6QAB9rgaW',0,NULL,0,NULL,'2026-10-02 09:03:46.000000','2026-10-02 09:03:46.000000');
CREATE TABLE webhooks (
	id INTEGER NOT NULL, 
	org_id INTEGER NOT NULL, 
	name VARCHAR(80) NOT NULL, 
	url VARCHAR(500) NOT NULL, 
	secret VARCHAR(80) NOT NULL, 
	events VARCHAR(500) NOT NULL, 
	is_active BOOLEAN NOT NULL, 
	consecutive_failures INTEGER NOT NULL, 
	last_delivered_at DATETIME, 
	last_status_code INTEGER, 
	last_error VARCHAR(500), 
	created_by_user_id INTEGER, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(org_id) REFERENCES organizations (id) ON DELETE CASCADE, 
	FOREIGN KEY(created_by_user_id) REFERENCES users (id) ON DELETE SET NULL
);
CREATE INDEX idx_orgs_slug ON organizations (slug);
CREATE INDEX idx_users_org_id ON users (org_id);
CREATE INDEX idx_users_email ON users (email);
CREATE INDEX idx_projects_org_id ON projects (org_id);
CREATE INDEX idx_prt_token_hash ON password_reset_tokens (token_hash);
CREATE INDEX idx_ecr_user ON email_change_requests (user_id);
CREATE INDEX idx_invites_email ON invitations (email);
CREATE INDEX idx_invites_org_id ON invitations (org_id);
CREATE INDEX idx_invites_token_hash ON invitations (token_hash);
CREATE INDEX idx_pm_project_id ON project_memberships (project_id);
CREATE INDEX idx_pm_user_id ON project_memberships (user_id);
CREATE INDEX idx_events_scheduled ON events (scheduled_for);
CREATE INDEX idx_events_org_id ON events (org_id);
CREATE INDEX idx_sessions_user_id ON sessions (user_id);
CREATE INDEX idx_sessions_expires_at ON sessions (expires_at);
CREATE INDEX idx_sessions_jti ON sessions (jti);
CREATE INDEX idx_trc_hash ON totp_recovery_codes (code_hash);
CREATE INDEX idx_trc_user ON totp_recovery_codes (user_id);
CREATE INDEX idx_views_owner ON saved_views (owner_user_id);
CREATE INDEX idx_views_org ON saved_views (org_id);
CREATE INDEX idx_webhooks_active ON webhooks (is_active);
CREATE INDEX idx_webhooks_org ON webhooks (org_id);
CREATE INDEX idx_cf_project ON custom_fields (project_id);
CREATE INDEX idx_device_tokens_user ON device_tokens (user_id);
CREATE INDEX idx_device_tokens_token ON device_tokens (token);
CREATE INDEX idx_bugs_project_id ON bugs (project_id);
CREATE INDEX idx_bugs_item_type ON bugs (item_type);
CREATE INDEX idx_bugs_event_id ON bugs (event_id);
CREATE INDEX idx_bugs_reporter_id ON bugs (reporter_id);
CREATE INDEX idx_bugs_status ON bugs (status);
CREATE INDEX idx_bugs_priority ON bugs (priority);
CREATE INDEX idx_bugs_environment ON bugs (environment);
CREATE INDEX idx_bugs_project_status ON bugs (project_id, status);
CREATE INDEX idx_bugs_status_priority ON bugs (status, priority);
CREATE INDEX idx_bugs_updated_at ON bugs (updated_at);
CREATE INDEX idx_bugs_created_at ON bugs (created_at);
CREATE INDEX idx_comments_bug_id ON comments (bug_id);
CREATE INDEX idx_activity_org_id ON activity_log (org_id);
CREATE INDEX idx_activity_entity ON activity_log (entity_type, entity_id);
CREATE INDEX idx_activity_created ON activity_log (created_at);
CREATE INDEX idx_activity_bug_id ON activity_log (bug_id);
CREATE INDEX idx_bcv_field ON bug_custom_values (field_id);
CREATE INDEX idx_bcv_bug ON bug_custom_values (bug_id);
CREATE INDEX idx_attachments_bug_id ON attachments (bug_id);
CREATE INDEX idx_attachments_bug_comment ON attachments (bug_id, comment_id);
CREATE INDEX idx_attachments_comment_id ON attachments (comment_id);
COMMIT;
