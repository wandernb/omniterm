-- Knowledge schema version 2: one stored copy of transcript text and vectors as blobs.
-- Applied only by helpers/omni_compact.py (scripts/compact-knowledge.py), never by the app.
-- The app's startup schema (omni_knowledge_schema.sql) uses CREATE TRIGGER IF NOT EXISTS,
-- so every trigger here keeps its version-1 name: a launch after the migration finds the
-- names present and leaves these bodies alone.
--
-- message: records keep everything but `text`; `messages` holds the text (and feeds FTS).
-- vector: chunk records keep everything but `vector`/`vector_hex`/`chunk`; `vectors` holds the
-- vector (a blob from `vector_hex`, or legacy JSON text) and `semantic_chunks` the chunk.
-- Each projecting trigger fires only while the value still carries the field it moves, so the
-- trigger's own stripping UPDATE does not fire it again.
DROP TRIGGER IF EXISTS records_messages_insert;
DROP TRIGGER IF EXISTS records_messages_update;
DROP TRIGGER IF EXISTS records_vectors_insert;
DROP TRIGGER IF EXISTS records_vectors_update;
DROP TRIGGER IF EXISTS records_semantic_chunks_insert;
DROP TRIGGER IF EXISTS records_semantic_chunks_update;
DROP TRIGGER IF EXISTS records_vectors_strip_insert;
DROP TRIGGER IF EXISTS records_vectors_strip_update;
CREATE TRIGGER records_messages_insert AFTER INSERT ON records
WHEN substr(new.key,1,8)='message:' AND json_type(new.value,'$.text') IS NOT NULL BEGIN
  INSERT INTO messages(id,source_key,session_id,provider,workspace,source_path,source_line,part,role,kind,text,timestamp)
  VALUES(json_extract(new.value,'$.id'),json_extract(new.value,'$.source_key'),json_extract(new.value,'$.session_id'),json_extract(new.value,'$.provider'),json_extract(new.value,'$.workspace'),json_extract(new.value,'$.source_path'),json_extract(new.value,'$.source_line'),json_extract(new.value,'$.part'),json_extract(new.value,'$.role'),json_extract(new.value,'$.kind'),json_extract(new.value,'$.text'),json_extract(new.value,'$.timestamp'))
  ON CONFLICT(id) DO UPDATE SET source_key=excluded.source_key,session_id=excluded.session_id,provider=excluded.provider,workspace=excluded.workspace,source_path=excluded.source_path,source_line=excluded.source_line,part=excluded.part,role=excluded.role,kind=excluded.kind,text=excluded.text,timestamp=excluded.timestamp;
  UPDATE records SET value=json_remove(new.value,'$.text') WHERE key=new.key;
END;
CREATE TRIGGER records_messages_update AFTER UPDATE OF value ON records
WHEN substr(new.key,1,8)='message:' AND json_type(new.value,'$.text') IS NOT NULL BEGIN
  INSERT INTO messages(id,source_key,session_id,provider,workspace,source_path,source_line,part,role,kind,text,timestamp)
  VALUES(json_extract(new.value,'$.id'),json_extract(new.value,'$.source_key'),json_extract(new.value,'$.session_id'),json_extract(new.value,'$.provider'),json_extract(new.value,'$.workspace'),json_extract(new.value,'$.source_path'),json_extract(new.value,'$.source_line'),json_extract(new.value,'$.part'),json_extract(new.value,'$.role'),json_extract(new.value,'$.kind'),json_extract(new.value,'$.text'),json_extract(new.value,'$.timestamp'))
  ON CONFLICT(id) DO UPDATE SET source_key=excluded.source_key,session_id=excluded.session_id,provider=excluded.provider,workspace=excluded.workspace,source_path=excluded.source_path,source_line=excluded.source_line,part=excluded.part,role=excluded.role,kind=excluded.kind,text=excluded.text,timestamp=excluded.timestamp;
  UPDATE records SET value=json_remove(new.value,'$.text') WHERE key=new.key;
END;
CREATE TRIGGER records_vectors_insert AFTER INSERT ON records
WHEN substr(new.key,1,7)='vector:' AND json_extract(new.value,'$.kind')='chunk'
  AND (json_type(new.value,'$.vector_hex')='text' OR json_type(new.value,'$.vector')='array') BEGIN
  INSERT INTO vectors(key,model,dimensions,content_hash,vector)
  VALUES(new.key,json_extract(new.value,'$.model'),json_extract(new.value,'$.dimensions'),json_extract(new.value,'$.content_hash'),
         CASE WHEN json_type(new.value,'$.vector_hex')='text' THEN unhex(json_extract(new.value,'$.vector_hex')) ELSE json_extract(new.value,'$.vector') END)
  ON CONFLICT(key) DO UPDATE SET model=excluded.model,dimensions=excluded.dimensions,content_hash=excluded.content_hash,vector=excluded.vector;
END;
CREATE TRIGGER records_vectors_update AFTER UPDATE OF value ON records
WHEN substr(new.key,1,7)='vector:' AND json_extract(new.value,'$.kind')='chunk'
  AND (json_type(new.value,'$.vector_hex')='text' OR json_type(new.value,'$.vector')='array') BEGIN
  INSERT INTO vectors(key,model,dimensions,content_hash,vector)
  VALUES(new.key,json_extract(new.value,'$.model'),json_extract(new.value,'$.dimensions'),json_extract(new.value,'$.content_hash'),
         CASE WHEN json_type(new.value,'$.vector_hex')='text' THEN unhex(json_extract(new.value,'$.vector_hex')) ELSE json_extract(new.value,'$.vector') END)
  ON CONFLICT(key) DO UPDATE SET model=excluded.model,dimensions=excluded.dimensions,content_hash=excluded.content_hash,vector=excluded.vector;
END;
CREATE TRIGGER records_semantic_chunks_insert AFTER INSERT ON records
WHEN substr(new.key,1,7)='vector:' AND json_extract(new.value,'$.kind')='chunk' AND json_type(new.value,'$.chunk') IS NOT NULL BEGIN
  INSERT INTO semantic_chunks(key,corpus,record_id,chunk_index,chunk)
  VALUES(new.key,json_extract(new.value,'$.corpus'),json_extract(new.value,'$.record_id'),json_extract(new.value,'$.chunk_index'),json_extract(new.value,'$.chunk'))
  ON CONFLICT(key) DO UPDATE SET corpus=excluded.corpus,record_id=excluded.record_id,chunk_index=excluded.chunk_index,chunk=excluded.chunk;
END;
CREATE TRIGGER records_semantic_chunks_update AFTER UPDATE OF value ON records
WHEN substr(new.key,1,7)='vector:' AND json_extract(new.value,'$.kind')='chunk' AND json_type(new.value,'$.chunk') IS NOT NULL BEGIN
  INSERT INTO semantic_chunks(key,corpus,record_id,chunk_index,chunk)
  VALUES(new.key,json_extract(new.value,'$.corpus'),json_extract(new.value,'$.record_id'),json_extract(new.value,'$.chunk_index'),json_extract(new.value,'$.chunk'))
  ON CONFLICT(key) DO UPDATE SET corpus=excluded.corpus,record_id=excluded.record_id,chunk_index=excluded.chunk_index,chunk=excluded.chunk;
END;
-- Stripping runs in its own triggers: NEW is the row as written, so each projecting trigger
-- above reads its field whatever order SQLite fires them in.
CREATE TRIGGER records_vectors_strip_insert AFTER INSERT ON records
WHEN substr(new.key,1,7)='vector:' AND json_extract(new.value,'$.kind')='chunk'
  AND (json_type(new.value,'$.vector_hex') IS NOT NULL OR json_type(new.value,'$.vector') IS NOT NULL OR json_type(new.value,'$.chunk') IS NOT NULL) BEGIN
  UPDATE records SET value=json_remove(new.value,'$.vector','$.vector_hex','$.chunk') WHERE key=new.key;
END;
CREATE TRIGGER records_vectors_strip_update AFTER UPDATE OF value ON records
WHEN substr(new.key,1,7)='vector:' AND json_extract(new.value,'$.kind')='chunk'
  AND (json_type(new.value,'$.vector_hex') IS NOT NULL OR json_type(new.value,'$.vector') IS NOT NULL OR json_type(new.value,'$.chunk') IS NOT NULL) BEGIN
  UPDATE records SET value=json_remove(new.value,'$.vector','$.vector_hex','$.chunk') WHERE key=new.key;
END;
