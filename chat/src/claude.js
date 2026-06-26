// Spawn the local `claude` CLI as a subprocess for one conversation turn.
//
// Why the CLI and not the Agent SDK: the SDK requires ANTHROPIC_API_KEY, but
// this box is logged in via OAuth (no key). The CLI reuses that existing login.
//
// We stream newline-delimited JSON (`--output-format stream-json`) and surface
// each event (assistant text, tool use, tool result, final result) via onEvent.
// The turn runs server-side and is owned by this process, so it keeps going even
// if the browser/client disconnects.
import { spawn } from 'node:child_process';

const CLAUDE_BIN = process.env.CLAUDE_BIN || 'claude';
const TOOL_RESULT_MAX = 8000; // truncate huge tool outputs before persisting/broadcasting

/**
 * Run a single turn.
 * @param {object} o
 * @param {string} o.prompt        user message text
 * @param {string} o.cwd           working directory for the agent
 * @param {string} [o.sessionId]   uuid: assign on first turn (--session-id) ...
 * @param {boolean} [o.resume]     ... or resume an existing one (--resume)
 * @param {string} [o.model]       optional model override
 * @param {string[]} [o.addDirs]   extra allowed directories
 * @param {(evt:object)=>void} o.onEvent  called for every parsed event
 * @param {AbortSignal} [o.signal]
 * @returns {Promise<{sessionId:string|null, result:string, isError:boolean, exitCode:number, stderr:string}>}
 */
export function runClaudeTurn({ prompt, cwd, sessionId, resume, model, addDirs = [], onEvent, signal }) {
  return new Promise((resolve, reject) => {
    const args = [
      '-p', prompt,
      '--output-format', 'stream-json',
      '--verbose',
      '--dangerously-skip-permissions',
    ];
    if (resume && sessionId) args.push('--resume', sessionId);
    else if (sessionId) args.push('--session-id', sessionId);
    if (model) args.push('--model', model);
    for (const d of addDirs) args.push('--add-dir', d);

    let child;
    try {
      child = spawn(CLAUDE_BIN, args, {
        cwd,
        env: process.env,
        stdio: ['ignore', 'pipe', 'pipe'],
        signal,
      });
    } catch (err) {
      reject(err);
      return;
    }

    let resolvedSessionId = sessionId || null;
    let resultText = '';
    let isError = false;
    let stderr = '';
    let buf = '';

    const handleEvt = (evt) => {
      if (evt.session_id) resolvedSessionId = evt.session_id;
      try {
        switch (evt.type) {
          case 'system':
            if (evt.subtype === 'init') {
              onEvent?.({ kind: 'init', sessionId: evt.session_id, model: evt.model, cwd: evt.cwd, tools: evt.tools });
            }
            break;
          case 'assistant':
            for (const block of evt.message?.content || []) {
              if (block.type === 'text' && block.text) onEvent?.({ kind: 'text', text: block.text });
              else if (block.type === 'thinking' && block.thinking) onEvent?.({ kind: 'thinking', text: block.thinking });
              else if (block.type === 'tool_use') onEvent?.({ kind: 'tool_use', id: block.id, name: block.name, input: block.input });
            }
            break;
          case 'user':
            for (const block of evt.message?.content || []) {
              if (block.type === 'tool_result') {
                let content = block.content;
                if (Array.isArray(content)) content = content.map((c) => c?.text ?? '').join('');
                content = String(content ?? '');
                if (content.length > TOOL_RESULT_MAX) content = content.slice(0, TOOL_RESULT_MAX) + `\n…[truncated ${content.length - TOOL_RESULT_MAX} chars]`;
                onEvent?.({ kind: 'tool_result', id: block.tool_use_id, content, isError: !!block.is_error });
              }
            }
            break;
          case 'result':
            isError = !!evt.is_error || evt.subtype !== 'success';
            resultText = evt.result || '';
            onEvent?.({ kind: 'result', subtype: evt.subtype, result: resultText, isError, costUsd: evt.total_cost_usd, durationMs: evt.duration_ms });
            break;
        }
      } catch { /* never let a bad event kill the stream */ }
    };

    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (chunk) => {
      buf += chunk;
      let nl;
      while ((nl = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line) continue;
        let evt;
        try { evt = JSON.parse(line); } catch { continue; }
        handleEvt(evt);
      }
    });
    child.stderr.setEncoding('utf8');
    child.stderr.on('data', (c) => { stderr += c; if (stderr.length > 16000) stderr = stderr.slice(-16000); });

    child.on('error', (err) => reject(err));
    child.on('close', (code) => {
      if (buf.trim()) { try { handleEvt(JSON.parse(buf.trim())); } catch { /* ignore */ } }
      resolve({ sessionId: resolvedSessionId, result: resultText, isError: isError || code !== 0, exitCode: code ?? -1, stderr });
    });
  });
}
