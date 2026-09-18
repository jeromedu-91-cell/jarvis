"""Accès SQLite (WAL, une connexion par thread) + migrations de schéma."""
from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from .config import BACKUP_DIR, DB_PATH, ensure_dirs

SCHEMA_VERSION = 12

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL);

CREATE TABLE IF NOT EXISTS connectors (
  id TEXT PRIMARY KEY, type TEXT NOT NULL, name TEXT NOT NULL,
  config TEXT NOT NULL DEFAULT '{}', permissions TEXT NOT NULL DEFAULT '["read"]',
  enabled INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'unknown', status_detail TEXT DEFAULT '',
  last_test_at REAL, last_connected_at REAL, created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS secrets (
  id TEXT PRIMARY KEY, connector_id TEXT NOT NULL, field TEXT NOT NULL, blob TEXT NOT NULL,
  created_at REAL, updated_at REAL, UNIQUE(connector_id, field)
);

CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY, scope TEXT NOT NULL, content TEXT NOT NULL,
  importance INTEGER NOT NULL DEFAULT 2, pinned INTEGER NOT NULL DEFAULT 0,
  source TEXT DEFAULT '', tags TEXT DEFAULT '[]', related TEXT DEFAULT '[]',
  project TEXT DEFAULT '', conversation_id TEXT DEFAULT '', task_id TEXT DEFAULT '',
  created_at REAL, updated_at REAL, embedding BLOB
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(content, tags, content='memories', content_rowid='rowid');
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid, content, tags) VALUES (new.rowid, new.content, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, content, tags) VALUES('delete', old.rowid, old.content, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, content, tags) VALUES('delete', old.rowid, old.content, old.tags);
  INSERT INTO memories_fts(rowid, content, tags) VALUES (new.rowid, new.content, new.tags);
END;

CREATE TABLE IF NOT EXISTS knowledge (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL, source TEXT DEFAULT '',
  kind TEXT DEFAULT 'note', tags TEXT DEFAULT '[]', project TEXT DEFAULT '', created_at REAL, updated_at REAL,
  confidence_score REAL NOT NULL DEFAULT 0.5, validation_count INTEGER NOT NULL DEFAULT 0,
  failure_count INTEGER NOT NULL DEFAULT 0, last_validated_at REAL, status TEXT NOT NULL DEFAULT 'active',
  tools TEXT DEFAULT '[]', verification_method TEXT DEFAULT ''
);
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(title, content, tags, content='knowledge', content_rowid='rowid');
CREATE TRIGGER IF NOT EXISTS knowledge_ai AFTER INSERT ON knowledge BEGIN
  INSERT INTO knowledge_fts(rowid, title, content, tags) VALUES (new.rowid, new.title, new.content, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS knowledge_ad AFTER DELETE ON knowledge BEGIN
  INSERT INTO knowledge_fts(knowledge_fts, rowid, title, content, tags) VALUES('delete', old.rowid, old.title, old.content, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS knowledge_au AFTER UPDATE ON knowledge BEGIN
  INSERT INTO knowledge_fts(knowledge_fts, rowid, title, content, tags) VALUES('delete', old.rowid, old.title, old.content, old.tags);
  INSERT INTO knowledge_fts(rowid, title, content, tags) VALUES (new.rowid, new.title, new.content, new.tags);
END;

CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT 'Nouvelle conversation',
  context TEXT NOT NULL DEFAULT '{}', archived INTEGER NOT NULL DEFAULT 0,
  created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
  meta TEXT NOT NULL DEFAULT '{}', created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, created_at);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'chat',
  status TEXT NOT NULL DEFAULT 'queued', progress REAL NOT NULL DEFAULT 0,
  agent TEXT DEFAULT 'jarvis', tools TEXT NOT NULL DEFAULT '[]', conversation_id TEXT DEFAULT '',
  plan TEXT NOT NULL DEFAULT '[]', result TEXT DEFAULT '', error TEXT DEFAULT '', meta TEXT NOT NULL DEFAULT '{}',
  created_at REAL, started_at REAL, completed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, created_at);
CREATE TABLE IF NOT EXISTS task_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, ts REAL, level TEXT, message TEXT, data TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_logs ON task_logs(task_id, id);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, user TEXT, agent TEXT, tool TEXT, connector_id TEXT,
  action TEXT, status TEXT, duration_ms INTEGER, task_id TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);

CREATE TABLE IF NOT EXISTS feed (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, level TEXT, kind TEXT, title TEXT, detail TEXT,
  source TEXT, meta TEXT, read INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_feed_ts ON feed(ts);

CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, type TEXT, payload TEXT);

-- CRM local. Table additive : `CREATE TABLE IF NOT EXISTS` est rejoué à chaque
-- ouverture, donc les bases existantes la reçoivent sans migration.
CREATE TABLE IF NOT EXISTS crm_contacts (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  company TEXT NOT NULL DEFAULT '',
  email TEXT NOT NULL DEFAULT '',
  phone TEXT NOT NULL DEFAULT '',
  address TEXT NOT NULL DEFAULT '',
  vat_number TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  created_at REAL,
  updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_crm_name ON crm_contacts(name);
CREATE INDEX IF NOT EXISTS idx_crm_company ON crm_contacts(company);

-- CRM étendu : sociétés, opportunités, interactions. `crm_contacts` reste la
-- table pivot (les fiches existantes ne sont pas déplacées) ; `company_id` la
-- relie à une société quand elle est connue, sans rendre le lien obligatoire.
CREATE TABLE IF NOT EXISTS crm_companies (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  domain TEXT NOT NULL DEFAULT '',
  industry TEXT NOT NULL DEFAULT '',
  size TEXT NOT NULL DEFAULT '',
  website TEXT NOT NULL DEFAULT '',
  address TEXT NOT NULL DEFAULT '',
  vat_number TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  tags TEXT NOT NULL DEFAULT '[]',
  created_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_crm_companies_name ON crm_companies(name);
CREATE INDEX IF NOT EXISTS idx_crm_companies_domain ON crm_companies(domain);

CREATE TABLE IF NOT EXISTS crm_deals (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  contact_id TEXT NOT NULL DEFAULT '',
  company_id TEXT NOT NULL DEFAULT '',
  stage TEXT NOT NULL DEFAULT 'nouveau',
  status TEXT NOT NULL DEFAULT 'open',            -- open | won | lost
  amount REAL NOT NULL DEFAULT 0,
  currency TEXT NOT NULL DEFAULT 'EUR',
  probability INTEGER NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  expected_close_at REAL, closed_at REAL,
  created_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_crm_deals_contact ON crm_deals(contact_id);
CREATE INDEX IF NOT EXISTS idx_crm_deals_status ON crm_deals(status, stage);

-- Historique d'activité. Alimenté à la main ET par les agents : `agent` et
-- `tool` disent qui a écrit la ligne, `source_ref` pointe la pièce d'origine
-- (id de mail, de tâche, de document) pour pouvoir remonter à la source.
CREATE TABLE IF NOT EXISTS crm_interactions (
  id TEXT PRIMARY KEY,
  contact_id TEXT NOT NULL DEFAULT '',
  company_id TEXT NOT NULL DEFAULT '',
  deal_id TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'note',              -- email | call | meeting | task | note | document
  direction TEXT NOT NULL DEFAULT '',             -- in | out | ''
  subject TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  intent TEXT NOT NULL DEFAULT '',
  intent_confidence REAL NOT NULL DEFAULT 0,
  sentiment TEXT NOT NULL DEFAULT '',
  agent TEXT NOT NULL DEFAULT '',
  tool TEXT NOT NULL DEFAULT '',
  source_ref TEXT NOT NULL DEFAULT '',
  meta TEXT NOT NULL DEFAULT '{}',
  occurred_at REAL, created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_crm_inter_contact ON crm_interactions(contact_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_crm_inter_occurred ON crm_interactions(occurred_at DESC);

-- ==========================================================================
-- Coffre-fort d'identifiants.
-- Cette table ne contient AUCUN secret : uniquement les métadonnées d'un
-- profil de connexion. Les valeurs sensibles (mot de passe, clé, jetons,
-- cookies, secret TOTP) vivent dans la table `secrets` déjà chiffrée en
-- AES-256-GCM par SecretVault, sous l'identité `cred:<credential_id>`.
-- ==========================================================================
CREATE TABLE IF NOT EXISTS vault_credentials (
  id TEXT PRIMARY KEY,
  service_name TEXT NOT NULL,
  service_url TEXT NOT NULL DEFAULT '',
  allowed_domains TEXT NOT NULL DEFAULT '[]',     -- domaines où l'injection est permise
  auth_type TEXT NOT NULL DEFAULT 'basic',        -- basic | api_key | oauth2 | cookies
  username_preview TEXT NOT NULL DEFAULT '',      -- jamais le mot de passe, jamais la clé
  connector_id TEXT NOT NULL DEFAULT '',          -- lien facultatif vers un connecteur existant
  has_totp INTEGER NOT NULL DEFAULT 0,
  totp_digits INTEGER NOT NULL DEFAULT 6,
  totp_period INTEGER NOT NULL DEFAULT 30,
  totp_algorithm TEXT NOT NULL DEFAULT 'SHA1',
  status TEXT NOT NULL DEFAULT 'active',          -- active | expired | revoked
  expires_at REAL,
  rotation_days INTEGER NOT NULL DEFAULT 0,       -- 0 = pas de rotation planifiée
  last_rotated_at REAL, last_used_at REAL,
  use_count INTEGER NOT NULL DEFAULT 0,
  notes TEXT NOT NULL DEFAULT '',
  tags TEXT NOT NULL DEFAULT '[]',
  created_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_vault_cred_service ON vault_credentials(service_name);
CREATE INDEX IF NOT EXISTS idx_vault_cred_status ON vault_credentials(status);

-- Moindre privilège : sans ligne ici, un agent n'obtient RIEN. Une habilitation
-- est nominative (agent_id), limitée dans ses usages (scopes), et peut être
-- bornée dans le temps ou en nombre d'utilisations.
CREATE TABLE IF NOT EXISTS vault_grants (
  id TEXT PRIMARY KEY,
  credential_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  scopes TEXT NOT NULL DEFAULT '["login"]',       -- login | read | rotate
  task_pattern TEXT NOT NULL DEFAULT '',          -- restriction facultative sur le contexte de tâche
  max_uses INTEGER NOT NULL DEFAULT 0,            -- 0 = illimité
  used INTEGER NOT NULL DEFAULT 0,
  expires_at REAL,
  revoked_at REAL,
  reason TEXT NOT NULL DEFAULT '',
  created_by TEXT NOT NULL DEFAULT 'jerome',
  created_at REAL, updated_at REAL,
  UNIQUE(credential_id, agent_id)
);
CREATE INDEX IF NOT EXISTS idx_vault_grants_agent ON vault_grants(agent_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);

CREATE TABLE IF NOT EXISTS workflows (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT DEFAULT '',
  trigger TEXT NOT NULL DEFAULT '{}', steps TEXT NOT NULL DEFAULT '[]', enabled INTEGER NOT NULL DEFAULT 1,
  source TEXT DEFAULT 'user', created_at REAL, updated_at REAL, last_run_at REAL, next_run_at REAL,
  last_status TEXT DEFAULT '', run_count INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS workflow_runs (
  id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, started_at REAL, finished_at REAL,
  status TEXT, output TEXT, task_id TEXT
);

-- Tâches Discord récurrentes (cf. jarvis/discord_scheduler.py). Table distincte
-- de `workflows` : une planification Discord porte un salon cible et un outil,
-- là où un workflow porte une suite d'étapes d'agent.
CREATE TABLE IF NOT EXISTS discord_schedules (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, trigger TEXT NOT NULL DEFAULT '{}',
  target_channel_id TEXT DEFAULT '', tool_to_call TEXT NOT NULL, params TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'ACTIVE', allow_destructive INTEGER NOT NULL DEFAULT 0,
  source TEXT DEFAULT 'user', created_at REAL, updated_at REAL, next_run_at REAL, last_run_at REAL,
  last_status TEXT DEFAULT '', last_output TEXT DEFAULT '',
  run_count INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_discord_schedules_due ON discord_schedules(status, next_run_at);

CREATE TABLE IF NOT EXISTS discord_schedule_runs (
  id TEXT PRIMARY KEY, schedule_id TEXT NOT NULL, ts REAL, status TEXT, duration_ms INTEGER DEFAULT 0,
  reason TEXT DEFAULT '', tool_to_call TEXT DEFAULT '', output TEXT DEFAULT '', data TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_discord_schedule_runs ON discord_schedule_runs(schedule_id, ts);

CREATE TABLE IF NOT EXISTS calendar_events (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, start_at REAL NOT NULL, end_at REAL, all_day INTEGER DEFAULT 0,
  source TEXT DEFAULT 'local', description TEXT DEFAULT '', meta TEXT DEFAULT '{}', created_at REAL
);

CREATE TABLE IF NOT EXISTS agents_state (
  id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'standby', last_activity_at REAL,
  current_task_id TEXT DEFAULT '', current_action TEXT DEFAULT '', last_error TEXT DEFAULT '',
  runs INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, client_id TEXT, started_at REAL, last_seen_at REAL,
  greeted INTEGER NOT NULL DEFAULT 0, greeted_at REAL, ended_at REAL
);

CREATE TABLE IF NOT EXISTS brain_nodes (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, family TEXT NOT NULL, label TEXT NOT NULL,
  ref_type TEXT DEFAULT '', ref_id TEXT DEFAULT '', meta TEXT DEFAULT '{}',
  weight REAL NOT NULL DEFAULT 1.0, created_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_brain_nodes_family ON brain_nodes(family);
CREATE INDEX IF NOT EXISTS idx_brain_nodes_ref ON brain_nodes(ref_type, ref_id);

CREATE TABLE IF NOT EXISTS brain_edges (
  id TEXT PRIMARY KEY, source TEXT NOT NULL, target TEXT NOT NULL, kind TEXT DEFAULT 'relation',
  weight REAL NOT NULL DEFAULT 1.0, created_at REAL, meta TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_brain_edges_source ON brain_edges(source);
CREATE INDEX IF NOT EXISTS idx_brain_edges_target ON brain_edges(target);

CREATE TABLE IF NOT EXISTS image_jobs (
  id TEXT PRIMARY KEY, conversation_id TEXT DEFAULT '', message_id TEXT DEFAULT '',
  mode TEXT NOT NULL DEFAULT 'generate', prompt TEXT NOT NULL DEFAULT '',
  negative_prompt TEXT DEFAULT '', backend TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'queued',
  stage TEXT DEFAULT 'queued', progress REAL NOT NULL DEFAULT 0,
  width INTEGER DEFAULT 0, height INTEGER DEFAULT 0, steps INTEGER DEFAULT 0, seed INTEGER DEFAULT 0,
  file_path TEXT DEFAULT '', source_job_id TEXT DEFAULT '', error TEXT DEFAULT '',
  meta TEXT DEFAULT '{}', created_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_image_jobs_conv ON image_jobs(conversation_id, created_at);

CREATE TABLE IF NOT EXISTS media_history (
  id TEXT PRIMARY KEY, job_id TEXT NOT NULL, media_type TEXT NOT NULL DEFAULT 'image',
  original_prompt TEXT DEFAULT '', final_prompt TEXT DEFAULT '', negative_prompt TEXT DEFAULT '',
  model TEXT DEFAULT '', workflow TEXT DEFAULT '', seed INTEGER DEFAULT 0,
  width INTEGER DEFAULT 0, height INTEGER DEFAULT 0, steps INTEGER DEFAULT 0,
  cfg REAL DEFAULT 0, sampler TEXT DEFAULT '', scheduler TEXT DEFAULT '',
  output TEXT DEFAULT '', quality_score TEXT DEFAULT '', meta TEXT DEFAULT '{}', created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_media_history_created ON media_history(created_at);

CREATE TABLE IF NOT EXISTS blender_jobs (
  id TEXT PRIMARY KEY, conversation_id TEXT DEFAULT '', message_id TEXT DEFAULT '',
  project_id TEXT DEFAULT '', tool TEXT DEFAULT '', action TEXT DEFAULT '',
  title TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'queued', stage TEXT DEFAULT 'queued',
  progress REAL NOT NULL DEFAULT 0, output_dir TEXT DEFAULT '', outputs TEXT DEFAULT '[]',
  meta TEXT DEFAULT '{}', error TEXT DEFAULT '', settings TEXT DEFAULT '{}',
  created_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_blender_jobs_conv ON blender_jobs(conversation_id, created_at);

CREATE TABLE IF NOT EXISTS blender_projects (
  id TEXT PRIMARY KEY, conversation_id TEXT DEFAULT '', name TEXT DEFAULT '',
  prompt TEXT DEFAULT '', blend_path TEXT DEFAULT '', last_job_id TEXT DEFAULT '',
  last_export TEXT DEFAULT '', last_render TEXT DEFAULT '', last_preview TEXT DEFAULT '',
  meta TEXT DEFAULT '{}', history TEXT DEFAULT '[]', created_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_blender_projects_conv ON blender_projects(conversation_id, updated_at);

CREATE TABLE IF NOT EXISTS avatar_references (
  id TEXT PRIMARY KEY, source_path TEXT NOT NULL, original_name TEXT DEFAULT '',
  reference_type TEXT NOT NULL DEFAULT 'mixed', tags TEXT DEFAULT '[]',
  extracted_features TEXT DEFAULT '{}', conversation_id TEXT DEFAULT '',
  uploaded_at REAL
);
CREATE INDEX IF NOT EXISTS idx_avatar_ref_conv ON avatar_references(conversation_id, uploaded_at);

CREATE TABLE IF NOT EXISTS avatar_revisions (
  id TEXT PRIMARY KEY, reference_id TEXT NOT NULL, blend_path TEXT DEFAULT '',
  preview_front TEXT DEFAULT '', preview_side TEXT DEFAULT '',
  preview_34 TEXT DEFAULT '', preview_full TEXT DEFAULT '',
  evaluation TEXT DEFAULT '{}', accepted INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 0, created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_avatar_rev_ref ON avatar_revisions(reference_id, created_at);

CREATE TABLE IF NOT EXISTS activity_trace (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT NOT NULL, title TEXT NOT NULL,
  detail TEXT DEFAULT '', state TEXT DEFAULT '', meta TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity_trace(ts);

CREATE TABLE IF NOT EXISTS tool_usage (
  tool_id TEXT PRIMARY KEY, total_calls INTEGER NOT NULL DEFAULT 0,
  success_calls INTEGER NOT NULL DEFAULT 0, failed_calls INTEGER NOT NULL DEFAULT 0,
  total_duration_ms INTEGER NOT NULL DEFAULT 0, last_used_at REAL,
  last_error TEXT DEFAULT '', error_categories TEXT DEFAULT '{}',
  importance_score REAL NOT NULL DEFAULT 0.5, expertise_score REAL NOT NULL DEFAULT 0.0,
  docs_checked_at REAL, docs_version TEXT DEFAULT '', updated_at REAL,
  display_name TEXT DEFAULT '', aliases TEXT DEFAULT '[]',
  first_used_at REAL, avg_duration_ms REAL NOT NULL DEFAULT 0,
  last_failure_at REAL, last_failure_error TEXT DEFAULT '',
  expertise_components TEXT DEFAULT '{}', knowledge_count INTEGER NOT NULL DEFAULT 0,
  coverage_total INTEGER NOT NULL DEFAULT 0, coverage_mastered INTEGER NOT NULL DEFAULT 0,
  last_learning_at REAL
);
CREATE TABLE IF NOT EXISTS learning_queue (
  id TEXT PRIMARY KEY, tool_id TEXT DEFAULT '', topic TEXT NOT NULL,
  kind TEXT DEFAULT 'documentation', priority REAL NOT NULL DEFAULT 0.0,
  curriculum_level INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'queued',
  source_version TEXT DEFAULT '', created_at REAL, updated_at REAL, completed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_learning_queue ON learning_queue(status, priority DESC);
CREATE TABLE IF NOT EXISTS learning_sessions (
  id TEXT PRIMARY KEY, status TEXT NOT NULL, started_at REAL, finished_at REAL,
  last_activity_at REAL, tool_id TEXT DEFAULT '', topic TEXT DEFAULT '', progress REAL DEFAULT 0,
  pages_read INTEGER DEFAULT 0, searches INTEGER DEFAULT 0, knowledge_created INTEGER DEFAULT 0,
  error TEXT DEFAULT '', meta TEXT DEFAULT '{}',
  knowledge_updated INTEGER DEFAULT 0, knowledge_validated INTEGER DEFAULT 0,
  duplicates_merged INTEGER DEFAULT 0, deprecated_marked INTEGER DEFAULT 0,
  tests_run INTEGER DEFAULT 0, tests_passed INTEGER DEFAULT 0,
  expertise_before REAL DEFAULT 0, expertise_after REAL DEFAULT 0, result TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS learning_migrations (
  name TEXT PRIMARY KEY, completed_at REAL, backup TEXT, result TEXT
);
CREATE TABLE IF NOT EXISTS learning_sources (
  id TEXT PRIMARY KEY, session_id TEXT, tool_id TEXT, topic TEXT,
  url TEXT, content_hash TEXT, checked_at REAL, version TEXT, status TEXT,
  UNIQUE(session_id, url)
);

CREATE TABLE IF NOT EXISTS self_upgrades (
  id TEXT PRIMARY KEY, prompt TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'auto',
  status TEXT NOT NULL DEFAULT 'queued', branch TEXT DEFAULT '', workspace_path TEXT DEFAULT '',
  version_before TEXT DEFAULT '', version_after TEXT DEFAULT '',
  plan TEXT DEFAULT '{}', files_changed TEXT DEFAULT '[]', git_diff TEXT DEFAULT '',
  tests_result TEXT DEFAULT '{}', health_status TEXT DEFAULT '', install_status TEXT DEFAULT '',
  rollback_status TEXT DEFAULT '', error TEXT DEFAULT '',
  candidate_port INTEGER DEFAULT 0, created_at REAL, started_at REAL, completed_at REAL,
  promoted_at REAL, rolled_back_at REAL, meta TEXT DEFAULT '{}', updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_self_upgrades_status ON self_upgrades(status, created_at);

CREATE TABLE IF NOT EXISTS self_upgrade_files (
  id INTEGER PRIMARY KEY AUTOINCREMENT, upgrade_id TEXT NOT NULL, path TEXT NOT NULL,
  action TEXT NOT NULL DEFAULT 'modified', created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_self_upgrade_files ON self_upgrade_files(upgrade_id);

-- Journal éditorial (JARVIS_BLOG_PUBLISHER_V1). Source de vérité pour
-- l'idempotence : une notification Discord déjà émise pour un article ne peut
-- pas l'être une seconde fois, même après redémarrage.
CREATE TABLE IF NOT EXISTS blog_publication_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  article_id INTEGER NOT NULL,
  slug TEXT NOT NULL,
  title TEXT DEFAULT '',
  url TEXT DEFAULT '',
  status TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'publish',
  published_at REAL,
  discord_message_id TEXT DEFAULT '',
  discord_channel_id TEXT DEFAULT '',
  sources TEXT DEFAULT '[]',
  content_hash TEXT DEFAULT '',
  verification TEXT DEFAULT '{}',
  detail TEXT DEFAULT '',
  created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_blog_pub_events_article
  ON blog_publication_events(article_id, kind, status);
CREATE INDEX IF NOT EXISTS idx_blog_pub_events_slug ON blog_publication_events(slug);

-- Mission Control V1 (porté depuis la branche Windows). Historique de
-- SUPERVISION : qui a fait quoi, avec quels outils, en combien de temps.
-- Les gros payloads n'y entrent jamais (aperçus tronqués seulement).
CREATE TABLE IF NOT EXISTS missions (
  id TEXT PRIMARY KEY,
  name TEXT DEFAULT '',
  kind TEXT DEFAULT 'chat',
  agent TEXT DEFAULT 'jarvis',
  status TEXT DEFAULT 'RUNNING',
  conversation_id TEXT DEFAULT '',
  created_at REAL,
  started_at REAL,
  completed_at REAL,
  duration REAL DEFAULT 0,
  tools TEXT DEFAULT '[]',
  tool_calls INTEGER DEFAULT 0,
  steps_total INTEGER DEFAULT 0,
  steps_done INTEGER DEFAULT 0,
  steps TEXT DEFAULT '[]',
  files TEXT DEFAULT '[]',
  error TEXT DEFAULT '',
  result TEXT DEFAULT '',
  failed_step TEXT DEFAULT '',
  failed_tool TEXT DEFAULT '',
  note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_missions_status ON missions(status, created_at);

CREATE TABLE IF NOT EXISTS mission_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mission_id TEXT NOT NULL,
  ts REAL,
  offset_ms INTEGER DEFAULT 0,
  kind TEXT DEFAULT 'log',
  label TEXT DEFAULT '',
  state TEXT DEFAULT '',
  tool TEXT DEFAULT '',
  ok INTEGER,
  duration_ms INTEGER DEFAULT 0,
  detail TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_mission_events_mission ON mission_events(mission_id, id);
"""


def new_id(prefix: str = "") -> str:
    raw = uuid.uuid4().hex[:12]
    return f"{prefix}_{raw}" if prefix else raw


class Database:
    def __init__(self, path: Path | str = DB_PATH) -> None:
        ensure_dirs()
        self.path = Path(path)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._init_schema()

    # -- connexions -------------------------------------------------------
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA foreign_keys=ON")
            self._local.conn = c
        return c

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    # -- helpers ----------------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._write_lock:
            return self.conn().execute(sql, tuple(params))

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self._write_lock:
            self.conn().executemany(sql, [tuple(r) for r in rows])

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn().execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn().execute(sql, tuple(params)).fetchone()

    def scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        row = self.one(sql, params)
        return None if row is None else row[0]

    @contextmanager
    def transaction(self):
        with self._write_lock:
            c = self.conn()
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise

    # -- schéma -----------------------------------------------------------
    def _init_schema(self) -> None:
        c = self.conn()
        with self._write_lock:
            c.executescript(SCHEMA)
            current = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            version = int(current[0]) if current else 0
            if version < SCHEMA_VERSION:
                self._migrate(version)
                c.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(SCHEMA_VERSION),),
                )

    def _migrate(self, from_version: int) -> None:
        # Les migrations sont additives (ALTER TABLE ADD COLUMN) pour ne jamais perdre de données.
        c = self.conn()

        def has_column(table: str, col: str) -> bool:
            return any(r[1] == col for r in c.execute(f"PRAGMA table_info({table})"))

        if from_version < 2 and not has_column("tasks", "plan"):
            c.execute("ALTER TABLE tasks ADD COLUMN plan TEXT NOT NULL DEFAULT '[]'")
        if from_version < 3 and not has_column("workflows", "state"):
            c.execute("ALTER TABLE workflows ADD COLUMN state TEXT NOT NULL DEFAULT '{}'")
        # Migration 4 : champs auto-learning sur la table knowledge.
        if from_version < 4:
            for col, ddl in (
                ("confidence_score", "REAL NOT NULL DEFAULT 0.5"),
                ("validation_count", "INTEGER NOT NULL DEFAULT 0"),
                ("failure_count", "INTEGER NOT NULL DEFAULT 0"),
                ("last_validated_at", "REAL"),
                ("status", "TEXT NOT NULL DEFAULT 'active'"),
                ("tools", "TEXT DEFAULT '[]'"),
                ("verification_method", "TEXT DEFAULT ''"),
            ):
                if not has_column("knowledge", col):
                    c.execute(f"ALTER TABLE knowledge ADD COLUMN {col} {ddl}")
# Migration 6 : atelier 3D Blender (jobs + projets persistants).
        if from_version < 6 and not has_column("blender_jobs", "project_id"):
            c.execute("ALTER TABLE blender_jobs ADD COLUMN project_id TEXT DEFAULT ''")
        if from_version < 7:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS tool_usage (
              tool_id TEXT PRIMARY KEY, total_calls INTEGER NOT NULL DEFAULT 0,
              success_calls INTEGER NOT NULL DEFAULT 0, failed_calls INTEGER NOT NULL DEFAULT 0,
              total_duration_ms INTEGER NOT NULL DEFAULT 0, last_used_at REAL,
              last_error TEXT DEFAULT '', error_categories TEXT DEFAULT '{}',
              importance_score REAL NOT NULL DEFAULT 0.5, expertise_score REAL NOT NULL DEFAULT 0.0,
              docs_checked_at REAL, docs_version TEXT DEFAULT '', updated_at REAL);
            CREATE TABLE IF NOT EXISTS learning_queue (
              id TEXT PRIMARY KEY, tool_id TEXT DEFAULT '', topic TEXT NOT NULL,
              kind TEXT DEFAULT 'documentation', priority REAL NOT NULL DEFAULT 0.0,
              curriculum_level INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'queued',
              source_version TEXT DEFAULT '', created_at REAL, updated_at REAL, completed_at REAL);
            CREATE INDEX IF NOT EXISTS idx_learning_queue ON learning_queue(status, priority DESC);
            CREATE TABLE IF NOT EXISTS learning_sessions (
              id TEXT PRIMARY KEY, status TEXT NOT NULL, started_at REAL, finished_at REAL,
              last_activity_at REAL, tool_id TEXT DEFAULT '', topic TEXT DEFAULT '', progress REAL DEFAULT 0,
              pages_read INTEGER DEFAULT 0, searches INTEGER DEFAULT 0, knowledge_created INTEGER DEFAULT 0,
              error TEXT DEFAULT '', meta TEXT DEFAULT '{}');
            """)
        # Migration 8 : identité canonique des outils (tool_id unique) et
        # champs d'expertise / résultats de sessions. La déduplication des
        # données est faite par IdleLearningEngine._migrate_data (avec backup).
        if from_version < 8:
            for table, cols in (
                ("tool_usage", (
                    ("display_name", "TEXT DEFAULT ''"),
                    ("aliases", "TEXT DEFAULT '[]'"),
                    ("first_used_at", "REAL"),
                    ("avg_duration_ms", "REAL NOT NULL DEFAULT 0"),
                    ("last_failure_at", "REAL"),
                    ("last_failure_error", "TEXT DEFAULT ''"),
                    ("expertise_components", "TEXT DEFAULT '{}'"),
                    ("knowledge_count", "INTEGER NOT NULL DEFAULT 0"),
                    ("coverage_total", "INTEGER NOT NULL DEFAULT 0"),
                    ("coverage_mastered", "INTEGER NOT NULL DEFAULT 0"),
                    ("last_learning_at", "REAL"),
                )),
                ("learning_sessions", (
                    ("knowledge_updated", "INTEGER NOT NULL DEFAULT 0"),
                    ("knowledge_validated", "INTEGER NOT NULL DEFAULT 0"),
                    ("duplicates_merged", "INTEGER NOT NULL DEFAULT 0"),
                    ("deprecated_marked", "INTEGER NOT NULL DEFAULT 0"),
                    ("tests_run", "INTEGER NOT NULL DEFAULT 0"),
                    ("tests_passed", "INTEGER NOT NULL DEFAULT 0"),
                    ("expertise_before", "REAL DEFAULT 0"),
                    ("expertise_after", "REAL DEFAULT 0"),
                    ("result", "TEXT DEFAULT '{}'"),
                )),
            ):
                for col, ddl in cols:
                    if not has_column(table, col):
                        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")

        if not has_column("knowledge", "evidence"):
            c.execute("ALTER TABLE knowledge ADD COLUMN evidence TEXT DEFAULT '{}'")
        if not has_column("tool_usage", "tool_version"):
            c.execute("ALTER TABLE tool_usage ADD COLUMN tool_version TEXT DEFAULT ''")

        if from_version < 11:
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_self_upgrade_files ON self_upgrade_files(upgrade_id)")
        if from_version < 11 and not has_column("self_upgrades", "updated_at"):
            try:
                c.execute("ALTER TABLE self_upgrades ADD COLUMN updated_at REAL")
            except Exception:
                pass
        # Migration 12 : CRM étendu. Les colonnes sont ajoutées à la table pivot
        # `crm_contacts` — les fiches existantes sont conservées telles quelles
        # et héritent simplement des valeurs par défaut.
        if from_version < 12:
            for col, ddl in (
                ("company_id", "TEXT NOT NULL DEFAULT ''"),
                ("role", "TEXT NOT NULL DEFAULT ''"),
                ("status", "TEXT NOT NULL DEFAULT 'lead'"),      # lead | prospect | client | inactive
                ("tags", "TEXT NOT NULL DEFAULT '[]'"),
                ("score", "INTEGER NOT NULL DEFAULT 0"),
                ("score_detail", "TEXT NOT NULL DEFAULT '{}'"),
                ("intent", "TEXT NOT NULL DEFAULT ''"),
                ("intent_confidence", "REAL NOT NULL DEFAULT 0"),
                ("owner", "TEXT NOT NULL DEFAULT ''"),
                ("source", "TEXT NOT NULL DEFAULT ''"),
                ("last_interaction_at", "REAL"),
            ):
                if not has_column("crm_contacts", col):
                    c.execute(f"ALTER TABLE crm_contacts ADD COLUMN {col} {ddl}")

    # -- sauvegarde ---------------------------------------------------------
    def backup(self, label: str = "") -> Path:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = BACKUP_DIR / f"jarvis-{stamp}{('-' + label) if label else ''}.db"
        with self._write_lock:
            dest = sqlite3.connect(str(target))
            try:
                self.conn().backup(dest)
            finally:
                dest.close()
        return target


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def loads(raw: Any, default: Any = None) -> Any:
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def row_to_dict(row: sqlite3.Row | None, json_fields: Iterable[str] = ()) -> dict[str, Any] | None:
    if row is None:
        return None
    d = {k: row[k] for k in row.keys()}
    for f in json_fields:
        if f in d:
            d[f] = loads(d[f], {} if f in {"config", "meta", "context", "trigger", "state"} else [])
    return d


def copy_file_backup(src: Path, label: str = "") -> Path | None:
    if not src.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = BACKUP_DIR / f"{src.name}.{stamp}{('.' + label) if label else ''}.bak"
    shutil.copy2(src, target)
    return target
