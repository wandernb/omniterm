-- Trusted bundled projection schema. Only OmniTerm's native application executes it.
-- Provider transcripts remain read-only; every projection changes with records atomically.
PRAGMA recursive_triggers=ON;
CREATE TABLE IF NOT EXISTS knowledge_schema(version INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS sources(
  key TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  path TEXT NOT NULL,
  workspace TEXT,
  started REAL,
  updated REAL,
  archived INTEGER,
  offset INTEGER,
  line INTEGER,
  size INTEGER,
  mtime INTEGER,
  inode TEXT,
  anchor TEXT,
  title TEXT,
  parent_session_id TEXT,
  malformed INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages(
  id TEXT PRIMARY KEY,
  source_key TEXT NOT NULL,
  session_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  workspace TEXT,
  source_path TEXT NOT NULL,
  source_line INTEGER NOT NULL,
  part INTEGER NOT NULL,
  role TEXT NOT NULL,
  kind TEXT NOT NULL,
  text TEXT NOT NULL,
  timestamp TEXT
);
CREATE TABLE IF NOT EXISTS memories(
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  sources TEXT NOT NULL,
  tags TEXT NOT NULL,
  revision INTEGER NOT NULL,
  created REAL,
  updated REAL
);
CREATE TABLE IF NOT EXISTS vectors(
  key TEXT PRIMARY KEY,
  model TEXT NOT NULL,
  dimensions INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  vector TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS semantic_chunks(
  key TEXT PRIMARY KEY,
  corpus TEXT NOT NULL,
  record_id TEXT NOT NULL,
  chunk_index INTEGER NOT NULL,
  chunk TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS source_sessions ON sources(session_id);
CREATE INDEX IF NOT EXISTS source_providers ON sources(provider,session_id);
CREATE INDEX IF NOT EXISTS message_sessions ON messages(session_id,source_path,source_line,part,id);
CREATE INDEX IF NOT EXISTS message_sources ON messages(source_key,id);
CREATE INDEX IF NOT EXISTS message_source_lines ON messages(source_key,source_line,id);
-- Stable identity of a Codex record mirrored by JSONL and the provider database.
CREATE INDEX IF NOT EXISTS message_positions ON messages(session_id,source_line,part);
CREATE INDEX IF NOT EXISTS message_scope ON messages(provider,workspace,id);
CREATE INDEX IF NOT EXISTS semantic_records ON semantic_chunks(corpus,record_id,chunk_index);
CREATE INDEX IF NOT EXISTS vector_models ON vectors(model,dimensions,key);
CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(text,content='messages',content_rowid='rowid');
CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
  INSERT INTO message_fts(rowid,text) VALUES(new.rowid,new.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
  INSERT INTO message_fts(message_fts,rowid,text) VALUES('delete',old.rowid,old.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE ON messages BEGIN
  INSERT INTO message_fts(message_fts,rowid,text) VALUES('delete',old.rowid,old.text);
  INSERT INTO message_fts(rowid,text) VALUES(new.rowid,new.text);
END;
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(title,body,content='memories',content_rowid='rowid');
CREATE TRIGGER IF NOT EXISTS memories_fts_insert AFTER INSERT ON memories BEGIN
  INSERT INTO memory_fts(rowid,title,body) VALUES(new.rowid,new.title,new.body);
END;
CREATE TRIGGER IF NOT EXISTS memories_fts_delete AFTER DELETE ON memories BEGIN
  INSERT INTO memory_fts(memory_fts,rowid,title,body) VALUES('delete',old.rowid,old.title,old.body);
END;
CREATE TRIGGER IF NOT EXISTS memories_fts_update AFTER UPDATE ON memories BEGIN
  INSERT INTO memory_fts(memory_fts,rowid,title,body) VALUES('delete',old.rowid,old.title,old.body);
  INSERT INTO memory_fts(rowid,title,body) VALUES(new.rowid,new.title,new.body);
END;
CREATE TRIGGER IF NOT EXISTS records_sources_insert AFTER INSERT ON records
WHEN substr(new.key,1,7)='source:' BEGIN
  INSERT INTO sources(key,session_id,provider,path,workspace,started,updated,archived,offset,line,size,mtime,inode,anchor,title,parent_session_id,malformed)
  VALUES(new.key,json_extract(new.value,'$.session_id'),json_extract(new.value,'$.provider'),json_extract(new.value,'$.path'),json_extract(new.value,'$.workspace'),json_extract(new.value,'$.started'),json_extract(new.value,'$.updated'),json_extract(new.value,'$.archived'),json_extract(new.value,'$.offset'),json_extract(new.value,'$.line'),json_extract(new.value,'$.size'),json_extract(new.value,'$.mtime'),json_extract(new.value,'$.inode'),json_extract(new.value,'$.anchor'),json_extract(new.value,'$.title'),json_extract(new.value,'$.parent_session_id'),json_extract(new.value,'$.malformed'))
  ON CONFLICT(key) DO UPDATE SET session_id=excluded.session_id,provider=excluded.provider,path=excluded.path,workspace=excluded.workspace,started=excluded.started,updated=excluded.updated,archived=excluded.archived,offset=excluded.offset,line=excluded.line,size=excluded.size,mtime=excluded.mtime,inode=excluded.inode,anchor=excluded.anchor,title=excluded.title,parent_session_id=excluded.parent_session_id,malformed=excluded.malformed;
END;
CREATE TRIGGER IF NOT EXISTS records_sources_update AFTER UPDATE OF value ON records
WHEN substr(new.key,1,7)='source:' BEGIN
  INSERT INTO sources(key,session_id,provider,path,workspace,started,updated,archived,offset,line,size,mtime,inode,anchor,title,parent_session_id,malformed)
  VALUES(new.key,json_extract(new.value,'$.session_id'),json_extract(new.value,'$.provider'),json_extract(new.value,'$.path'),json_extract(new.value,'$.workspace'),json_extract(new.value,'$.started'),json_extract(new.value,'$.updated'),json_extract(new.value,'$.archived'),json_extract(new.value,'$.offset'),json_extract(new.value,'$.line'),json_extract(new.value,'$.size'),json_extract(new.value,'$.mtime'),json_extract(new.value,'$.inode'),json_extract(new.value,'$.anchor'),json_extract(new.value,'$.title'),json_extract(new.value,'$.parent_session_id'),json_extract(new.value,'$.malformed'))
  ON CONFLICT(key) DO UPDATE SET session_id=excluded.session_id,provider=excluded.provider,path=excluded.path,workspace=excluded.workspace,started=excluded.started,updated=excluded.updated,archived=excluded.archived,offset=excluded.offset,line=excluded.line,size=excluded.size,mtime=excluded.mtime,inode=excluded.inode,anchor=excluded.anchor,title=excluded.title,parent_session_id=excluded.parent_session_id,malformed=excluded.malformed;
END;
CREATE TRIGGER IF NOT EXISTS records_sources_delete AFTER DELETE ON records
WHEN substr(old.key,1,7)='source:' BEGIN
  DELETE FROM sources WHERE key=old.key;
END;
CREATE TRIGGER IF NOT EXISTS records_messages_insert AFTER INSERT ON records
WHEN substr(new.key,1,8)='message:' BEGIN
  INSERT INTO messages(id,source_key,session_id,provider,workspace,source_path,source_line,part,role,kind,text,timestamp)
  VALUES(json_extract(new.value,'$.id'),json_extract(new.value,'$.source_key'),json_extract(new.value,'$.session_id'),json_extract(new.value,'$.provider'),json_extract(new.value,'$.workspace'),json_extract(new.value,'$.source_path'),json_extract(new.value,'$.source_line'),json_extract(new.value,'$.part'),json_extract(new.value,'$.role'),json_extract(new.value,'$.kind'),json_extract(new.value,'$.text'),json_extract(new.value,'$.timestamp'))
  ON CONFLICT(id) DO UPDATE SET source_key=excluded.source_key,session_id=excluded.session_id,provider=excluded.provider,workspace=excluded.workspace,source_path=excluded.source_path,source_line=excluded.source_line,part=excluded.part,role=excluded.role,kind=excluded.kind,text=excluded.text,timestamp=excluded.timestamp;
END;
CREATE TRIGGER IF NOT EXISTS records_messages_update AFTER UPDATE OF value ON records
WHEN substr(new.key,1,8)='message:' BEGIN
  INSERT INTO messages(id,source_key,session_id,provider,workspace,source_path,source_line,part,role,kind,text,timestamp)
  VALUES(json_extract(new.value,'$.id'),json_extract(new.value,'$.source_key'),json_extract(new.value,'$.session_id'),json_extract(new.value,'$.provider'),json_extract(new.value,'$.workspace'),json_extract(new.value,'$.source_path'),json_extract(new.value,'$.source_line'),json_extract(new.value,'$.part'),json_extract(new.value,'$.role'),json_extract(new.value,'$.kind'),json_extract(new.value,'$.text'),json_extract(new.value,'$.timestamp'))
  ON CONFLICT(id) DO UPDATE SET source_key=excluded.source_key,session_id=excluded.session_id,provider=excluded.provider,workspace=excluded.workspace,source_path=excluded.source_path,source_line=excluded.source_line,part=excluded.part,role=excluded.role,kind=excluded.kind,text=excluded.text,timestamp=excluded.timestamp;
END;
CREATE TRIGGER IF NOT EXISTS records_messages_delete AFTER DELETE ON records
WHEN substr(old.key,1,8)='message:' BEGIN
  DELETE FROM messages WHERE id=json_extract(old.value,'$.id');
END;
CREATE TRIGGER IF NOT EXISTS records_memories_insert AFTER INSERT ON records
WHEN substr(new.key,1,7)='memory:' BEGIN
  INSERT INTO memories(id,title,body,sources,tags,revision,created,updated)
  VALUES(json_extract(new.value,'$.id'),json_extract(new.value,'$.title'),json_extract(new.value,'$.body'),json_extract(new.value,'$.sources'),json_extract(new.value,'$.tags'),json_extract(new.value,'$.revision'),json_extract(new.value,'$.created'),json_extract(new.value,'$.updated'))
  ON CONFLICT(id) DO UPDATE SET title=excluded.title,body=excluded.body,sources=excluded.sources,tags=excluded.tags,revision=excluded.revision,created=excluded.created,updated=excluded.updated;
END;
CREATE TRIGGER IF NOT EXISTS records_memories_update AFTER UPDATE OF value ON records
WHEN substr(new.key,1,7)='memory:' BEGIN
  INSERT INTO memories(id,title,body,sources,tags,revision,created,updated)
  VALUES(json_extract(new.value,'$.id'),json_extract(new.value,'$.title'),json_extract(new.value,'$.body'),json_extract(new.value,'$.sources'),json_extract(new.value,'$.tags'),json_extract(new.value,'$.revision'),json_extract(new.value,'$.created'),json_extract(new.value,'$.updated'))
  ON CONFLICT(id) DO UPDATE SET title=excluded.title,body=excluded.body,sources=excluded.sources,tags=excluded.tags,revision=excluded.revision,created=excluded.created,updated=excluded.updated;
END;
CREATE TRIGGER IF NOT EXISTS records_memories_delete AFTER DELETE ON records
WHEN substr(old.key,1,7)='memory:' BEGIN
  DELETE FROM memories WHERE id=json_extract(old.value,'$.id');
END;
CREATE TRIGGER IF NOT EXISTS records_vectors_insert AFTER INSERT ON records
WHEN substr(new.key,1,7)='vector:' AND json_extract(new.value,'$.kind')='chunk' BEGIN
  INSERT INTO vectors(key,model,dimensions,content_hash,vector)
  VALUES(new.key,json_extract(new.value,'$.model'),json_extract(new.value,'$.dimensions'),json_extract(new.value,'$.content_hash'),json_extract(new.value,'$.vector'))
  ON CONFLICT(key) DO UPDATE SET model=excluded.model,dimensions=excluded.dimensions,content_hash=excluded.content_hash,vector=excluded.vector;
END;
CREATE TRIGGER IF NOT EXISTS records_vectors_update AFTER UPDATE OF value ON records
WHEN substr(new.key,1,7)='vector:' AND json_extract(new.value,'$.kind')='chunk' BEGIN
  INSERT INTO vectors(key,model,dimensions,content_hash,vector)
  VALUES(new.key,json_extract(new.value,'$.model'),json_extract(new.value,'$.dimensions'),json_extract(new.value,'$.content_hash'),json_extract(new.value,'$.vector'))
  ON CONFLICT(key) DO UPDATE SET model=excluded.model,dimensions=excluded.dimensions,content_hash=excluded.content_hash,vector=excluded.vector;
END;
CREATE TRIGGER IF NOT EXISTS records_vectors_delete AFTER DELETE ON records
WHEN substr(old.key,1,7)='vector:' BEGIN
  DELETE FROM vectors WHERE key=old.key;
END;
CREATE TRIGGER IF NOT EXISTS records_semantic_chunks_insert AFTER INSERT ON records
WHEN substr(new.key,1,7)='vector:' AND json_extract(new.value,'$.kind')='chunk' BEGIN
  INSERT INTO semantic_chunks(key,corpus,record_id,chunk_index,chunk)
  VALUES(new.key,json_extract(new.value,'$.corpus'),json_extract(new.value,'$.record_id'),json_extract(new.value,'$.chunk_index'),json_extract(new.value,'$.chunk'))
  ON CONFLICT(key) DO UPDATE SET corpus=excluded.corpus,record_id=excluded.record_id,chunk_index=excluded.chunk_index,chunk=excluded.chunk;
END;
CREATE TRIGGER IF NOT EXISTS records_semantic_chunks_update AFTER UPDATE OF value ON records
WHEN substr(new.key,1,7)='vector:' AND json_extract(new.value,'$.kind')='chunk' BEGIN
  INSERT INTO semantic_chunks(key,corpus,record_id,chunk_index,chunk)
  VALUES(new.key,json_extract(new.value,'$.corpus'),json_extract(new.value,'$.record_id'),json_extract(new.value,'$.chunk_index'),json_extract(new.value,'$.chunk'))
  ON CONFLICT(key) DO UPDATE SET corpus=excluded.corpus,record_id=excluded.record_id,chunk_index=excluded.chunk_index,chunk=excluded.chunk;
END;
CREATE TRIGGER IF NOT EXISTS records_semantic_chunks_delete AFTER DELETE ON records
WHEN substr(old.key,1,7)='vector:' BEGIN
  DELETE FROM semantic_chunks WHERE key=old.key;
END;
-- Populate earlier document-only databases once. Future writes use the triggers.
-- The guard is a key range, so a migrated database seeks the primary key and
-- touches no rows; a plain WHERE NOT EXISTS(...) still scans every record,
-- which on a large index made each cold launch read the whole file.
UPDATE records SET value=value WHERE key>=(CASE WHEN EXISTS(SELECT 1 FROM knowledge_schema WHERE version=1) THEN NULL ELSE '' END);
INSERT OR IGNORE INTO knowledge_schema(version) VALUES(1);

