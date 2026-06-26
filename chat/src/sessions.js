// Disk-backed session store. Each session is a directory under data/sessions/<id>/
// holding meta.json (title, cwd, claude session id, timestamps) and transcript.jsonl
// (one JSON event per line). Persisting to disk means sessions + history survive a
// server restart and a client can catch up after reconnecting.
import { randomUUID } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

export class SessionStore {
  constructor(dataDir) {
    this.dir = path.join(dataDir, 'sessions');
    fs.mkdirSync(this.dir, { recursive: true });
    this.busy = new Set(); // session ids with an in-flight turn (reset on restart)
  }

  _metaPath(id) { return path.join(this.dir, id, 'meta.json'); }
  _txtPath(id) { return path.join(this.dir, id, 'transcript.jsonl'); }

  list() {
    return fs.readdirSync(this.dir)
      .filter((id) => fs.existsSync(this._metaPath(id)))
      .map((id) => this.getMeta(id))
      .filter(Boolean)
      .sort((a, b) => (b.lastActivity || 0) - (a.lastActivity || 0));
  }

  create({ title, cwd, model } = {}) {
    const id = randomUUID();
    fs.mkdirSync(path.join(this.dir, id), { recursive: true });
    const now = Date.now();
    const meta = {
      id,
      title: title || 'New chat',
      cwd: cwd || process.env.WORKSPACE || process.cwd(),
      model: model || process.env.CHAT_MODEL || null,
      claudeStarted: false, // becomes true after the first turn assigns the claude session
      createdAt: now,
      lastActivity: now,
      turns: 0,
    };
    fs.writeFileSync(this._metaPath(id), JSON.stringify(meta, null, 2));
    fs.writeFileSync(this._txtPath(id), '');
    return meta;
  }

  getMeta(id) {
    try {
      const m = JSON.parse(fs.readFileSync(this._metaPath(id), 'utf8'));
      m.busy = this.busy.has(id);
      return m;
    } catch { return null; }
  }

  updateMeta(id, patch) {
    const m = this.getMeta(id);
    if (!m) return null;
    delete m.busy;
    const next = { ...m, ...patch, lastActivity: Date.now() };
    fs.writeFileSync(this._metaPath(id), JSON.stringify(next, null, 2));
    return next;
  }

  exists(id) { return fs.existsSync(this._metaPath(id)); }

  appendEvent(id, event) {
    const rec = { t: Date.now(), ...event };
    fs.appendFileSync(this._txtPath(id), JSON.stringify(rec) + '\n');
    return rec;
  }

  transcript(id) {
    try {
      return fs.readFileSync(this._txtPath(id), 'utf8')
        .split('\n').filter(Boolean)
        .map((l) => { try { return JSON.parse(l); } catch { return null; } })
        .filter(Boolean);
    } catch { return []; }
  }

  remove(id) {
    fs.rmSync(path.join(this.dir, id), { recursive: true, force: true });
    this.busy.delete(id);
  }

  setBusy(id, v) { if (v) this.busy.add(id); else this.busy.delete(id); }
  isBusy(id) { return this.busy.has(id); }
}
