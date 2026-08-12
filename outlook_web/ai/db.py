"""SQLite schema helpers for AI reply feature."""

from __future__ import annotations

from typing import Any


def ensure_ai_schema(conn) -> None:
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ai_knowledge_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL DEFAULT 'general',
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            keywords TEXT NOT NULL DEFAULT '',
            priority INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ai_knowledge_meta (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            revision INTEGER NOT NULL DEFAULT 1,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        INSERT OR IGNORE INTO ai_knowledge_meta (id, revision)
        VALUES (1, 1)
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ai_rule_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version_label TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'draft',
            keywords TEXT NOT NULL DEFAULT '',
            intents TEXT NOT NULL DEFAULT '',
            instruction TEXT NOT NULL DEFAULT '',
            forbidden_phrases TEXT NOT NULL DEFAULT '',
            risk_level TEXT NOT NULL DEFAULT 'yellow',
            priority INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            published_at TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ai_analysis_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_email TEXT NOT NULL,
            message_id TEXT NOT NULL,
            context_scope TEXT NOT NULL DEFAULT 'current',
            provider TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            input_fingerprint TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'succeeded',
            error_message TEXT,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            history_count INTEGER NOT NULL DEFAULT 0,
            contact_email TEXT NOT NULL DEFAULT '',
            result_json TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ai_drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            kind TEXT NOT NULL DEFAULT 'initial',
            reply_text TEXT NOT NULL DEFAULT '',
            reply_text_zh TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (run_id) REFERENCES ai_analysis_runs (id) ON DELETE CASCADE
        )
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_ai_analysis_runs_lookup
        ON ai_analysis_runs(account_email, message_id, created_at DESC)
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_ai_knowledge_enabled
        ON ai_knowledge_entries(enabled, priority DESC)
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_ai_rule_versions_status
        ON ai_rule_versions(status, enabled, priority DESC)
    ''')


def bump_knowledge_revision(db) -> int:
    db.execute(
        '''
        UPDATE ai_knowledge_meta
        SET revision = revision + 1, updated_at = CURRENT_TIMESTAMP
        WHERE id = 1
        '''
    )
    row = db.execute('SELECT revision FROM ai_knowledge_meta WHERE id = 1').fetchone()
    return int(row['revision'] if row else 1)


def get_knowledge_revision(db) -> int:
    row = db.execute('SELECT revision FROM ai_knowledge_meta WHERE id = 1').fetchone()
    return int(row['revision'] if row else 1)


def list_knowledge_entries(db, include_disabled: bool = True):
    if include_disabled:
        rows = db.execute(
            '''
            SELECT * FROM ai_knowledge_entries
            ORDER BY priority DESC, id DESC
            '''
        ).fetchall()
    else:
        rows = db.execute(
            '''
            SELECT * FROM ai_knowledge_entries
            WHERE enabled = 1
            ORDER BY priority DESC, id DESC
            '''
        ).fetchall()
    return [dict(row) for row in rows]


def list_published_rules(db):
    rows = db.execute(
        '''
        SELECT * FROM ai_rule_versions
        WHERE status = 'published' AND enabled = 1
        ORDER BY priority DESC, id DESC
        '''
    ).fetchall()
    return [dict(row) for row in rows]


def list_rule_versions(db):
    rows = db.execute(
        '''
        SELECT * FROM ai_rule_versions
        ORDER BY
            CASE status WHEN 'published' THEN 0 WHEN 'draft' THEN 1 ELSE 2 END,
            priority DESC,
            id DESC
        '''
    ).fetchall()
    return [dict(row) for row in rows]
