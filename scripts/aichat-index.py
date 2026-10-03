#!/usr/bin/env python3
"""Build an upstream-compatible Tantivy index from verified public evidence only."""
import importlib.util
import json
from pathlib import Path
import sys
import uuid
from datetime import datetime
import tantivy

spec = importlib.util.spec_from_file_location('alignment_memory', Path(__file__).with_name('alignment-memory.py'))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

def build(store_root, output):
    store = m.Store(store_root,catalog_path=Path(output)/'catalog.sqlite')
    output = Path(output).resolve()
    if not output.is_relative_to(store.root):
        raise ValueError('search index must remain inside evidence store')
    output.mkdir(mode=0o700,exist_ok=True)
    b = tantivy.SchemaBuilder()
    for name in ['agent','project','branch','cwd','created','modified',
                 'first_msg_role','first_msg_content','last_msg_role','last_msg_content',
                 'first_user_msg_content','derivation_type','is_sidechain','is_exec_run','custom_title']:
        b.add_text_field(name, stored=True)
    b.add_text_field('session_id',stored=True,tokenizer_name='raw')
    b.add_unsigned_field('modified_ts', stored=True, fast=True)
    b.add_integer_field('lines', stored=True)
    b.add_text_field('export_path', stored=True, tokenizer_name='raw')
    b.add_text_field('claude_home', stored=True, tokenizer_name='raw')
    b.add_text_field('content', stored=True)
    changed=json.loads((output/'update-plan.json').read_text())['changed_sources']
    previous=json.loads((output/'evidence-map.json').read_text()) if (output/'evidence-map.json').exists() else {}
    index = tantivy.Index.open(str(output)) if changed is not None else tantivy.Index(b.build(), path=str(output))
    writer = index.writer(heap_size=15000000, num_threads=1)
    mapping=dict(previous.get('mapping',{})) if changed is not None else {}
    skipped=[r for r in previous.get('skipped',[]) if r['source'] not in changed] if changed is not None else []
    updated=0
    with store.database() as db:
        for src in db.execute('SELECT * FROM sources ORDER BY key').fetchall():
            if changed is not None and src['key'] not in changed:continue
            writer.delete_documents('session_id',src['key'])
            mapping.pop(src['key'],None);updated+=1
            records = []
            for loc in db.execute('SELECT * FROM messages WHERE source=? ORDER BY offset', (src['key'],)).fetchall():
                try:
                    records.append(store.read_native(db, loc))
                except (ValueError, OSError, TypeError, KeyError) as e:
                    skipped.append({'source':loc['source'],'id':loc['id'],'reason':str(e)})
            if not records:
                continue
            # One native transcript/source is one session candidate, irrespective of message count.
            sid = src['key']
            first, last = records[0], records[-1]
            stamp = last['occurred_at'] or ''
            try: ms = int(datetime.fromisoformat(stamp.replace('Z','+00:00')).timestamp()*1000)
            except (ValueError, OverflowError): ms = 0
            doc = tantivy.Document()
            fields = {'session_id':sid,'agent':src['adapter'],'project':src['key'],
                      'branch':'','cwd':'','created':first['occurred_at'] or '',
                      'modified':stamp,'export_path':src['path'],
                      'first_msg_role':first['role'],'first_msg_content':'',
                      'last_msg_role':last['role'],'last_msg_content':'','first_user_msg_content':'',
                      'derivation_type':'original','is_sidechain':'false','is_exec_run':'false',
                      'claude_home':'','custom_title':'','content':'\n\n'.join(r['text'] for r in records)}
            for name,value in fields.items(): doc.add_text(name,value)
            doc.add_unsigned('modified_ts',ms); doc.add_integer('lines',len(records))
            writer.add_document(doc)
            try: native_id = str(uuid.UUID(src['key'].split(':',1)[-1]))
            except ValueError: native_id = None
            mapping[sid] = {'source':src['key'],'session_id':native_id,
                            'adapter':src['adapter'],'path':src['path'],'public_messages':len(records)}

        if len(mapping)>100000:
            raise ValueError('upstream candidate loader supports at most 100000 sessions')
        writer.commit(); writer.wait_merging_threads()
        catalog = [dict(r) for r in db.execute('SELECT * FROM sources ORDER BY key')]
        (output/'catalog-hash').write_text(m.sha(json.dumps(catalog,sort_keys=True)))
        (output/'evidence-map.json').write_text(json.dumps({'format':'session-candidates-v3','mapping':mapping,'skipped':skipped},ensure_ascii=False)+'\n')
    return {'indexed':len(mapping),'public_messages':sum(r['public_messages'] for r in mapping.values()),'skipped':len(skipped),'updated_sessions':updated}

if __name__ == '__main__':
    print(json.dumps(build(sys.argv[1],sys.argv[2])))
