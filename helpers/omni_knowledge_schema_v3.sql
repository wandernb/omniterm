-- Knowledge schema version 3: a message is stored once, in `messages`. No `records` row stays behind.
-- Applied only by helpers/omni_compact.py (scripts/compact-knowledge.py), never by the app, on a
-- version-2 index. Trigger names stay the version-1 ones, so the app's startup schema (CREATE
-- TRIGGER IF NOT EXISTS) finds them and leaves these bodies alone.
--
-- A `message:` write projects into `messages` and then deletes its own record row. A record row
-- is therefore never there to delete, so a removed message arrives as a delete marker instead:
-- a `message:` write whose value is {"_deleted": true}. The worker sends markers only to an index
-- whose knowledge_schema holds version 3. The delete trigger keeps its name with an empty body:
-- deleting a record row (the projection's own delete, or the migration's) never deletes a message.
DROP TRIGGER IF EXISTS records_messages_insert;
DROP TRIGGER IF EXISTS records_messages_update;
DROP TRIGGER IF EXISTS records_messages_delete;
CREATE TRIGGER records_messages_insert AFTER INSERT ON records
WHEN substr(new.key,1,8)='message:' BEGIN
  INSERT INTO messages(id,source_key,session_id,provider,workspace,source_path,source_line,part,role,kind,text,timestamp)
  SELECT json_extract(new.value,'$.id'),json_extract(new.value,'$.source_key'),json_extract(new.value,'$.session_id'),json_extract(new.value,'$.provider'),json_extract(new.value,'$.workspace'),json_extract(new.value,'$.source_path'),json_extract(new.value,'$.source_line'),json_extract(new.value,'$.part'),json_extract(new.value,'$.role'),json_extract(new.value,'$.kind'),json_extract(new.value,'$.text'),json_extract(new.value,'$.timestamp')
  WHERE json_type(new.value,'$._deleted') IS NULL
  ON CONFLICT(id) DO UPDATE SET source_key=excluded.source_key,session_id=excluded.session_id,provider=excluded.provider,workspace=excluded.workspace,source_path=excluded.source_path,source_line=excluded.source_line,part=excluded.part,role=excluded.role,kind=excluded.kind,text=excluded.text,timestamp=excluded.timestamp;
  DELETE FROM messages WHERE id=substr(new.key,9) AND json_type(new.value,'$._deleted') IS NOT NULL;
  DELETE FROM records WHERE key=new.key;
END;
CREATE TRIGGER records_messages_update AFTER UPDATE OF value ON records
WHEN substr(new.key,1,8)='message:' BEGIN
  DELETE FROM records WHERE key=new.key;
END;
CREATE TRIGGER records_messages_delete AFTER DELETE ON records
WHEN 0 BEGIN
  SELECT 1;
END;
