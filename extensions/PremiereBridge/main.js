/**
 * Nova Premiere Bridge - panel side.
 *
 * Runs a loopback HTTP server inside the CEP panel and forwards each request
 * to the ExtendScript host. The panel is the only place where Node and
 * ExtendScript both exist, which is why the server lives here rather than in
 * Python: it removes an entire process and an entire class of "who starts
 * first" bugs.
 *
 * Contract with Python (premiere/bridge.py):
 *   GET  /health -> { ok, project_open, version, ... }
 *   POST /exec   { op, params }            -> { ok, result } | { ok:false, error }
 *   POST /batch  { ops: [...], on_error }  -> { ok, result: { results: [...] } }
 *
 * Everything below is deliberately defensive: ExtendScript returns strings,
 * throws stringly-typed errors, and can return the literal "EvalScript error."
 * when the host script failed to load at all. Each of those is turned into a
 * structured error so the Python side never has to parse English.
 */
(function () {
  'use strict';

  var PORT = 8089;
  var HOST = '127.0.0.1';
  var MAX_BODY = 32 * 1024 * 1024; // baked keyframe batches get large

  var cs = new CSInterface();
  var http, fs, path;

  try {
    http = require('http');
    fs = require('fs');
    path = require('path');
  } catch (e) {
    showFatal('Node.js is not enabled for this panel. Reinstall the extension ' +
              '(the manifest needs --enable-nodejs) and restart Premiere.');
    return;
  }

  var state = { ops: 0, errors: 0, hostReady: false, project: '' };

  // ------------------------------------------------------------------
  // UI
  // ------------------------------------------------------------------

  function $(id) { return document.getElementById(id); }

  function setRow(name, ok, text) {
    var dot = $('dot-' + name);
    var value = $('v-' + name);
    if (dot) { dot.className = 'dot ' + (ok === null ? 'warn' : (ok ? 'ok' : 'bad')); }
    if (value) { value.textContent = text; }
  }

  function log(message, isError) {
    var box = $('log');
    if (!box) { return; }
    var line = document.createElement('div');
    if (isError) { line.className = 'err'; }
    var now = new Date();
    line.textContent = '[' + pad(now.getHours()) + ':' + pad(now.getMinutes()) +
                       ':' + pad(now.getSeconds()) + '] ' + message;
    box.appendChild(line);
    while (box.childNodes.length > 200) { box.removeChild(box.firstChild); }
    box.scrollTop = box.scrollHeight;
  }

  function pad(n) { return n < 10 ? '0' + n : String(n); }

  function showFatal(message) {
    setRow('server', false, 'unavailable');
    log(message, true);
  }

  // ------------------------------------------------------------------
  // ExtendScript call
  // ------------------------------------------------------------------

  /**
   * Call one host function. `params` is JSON-encoded and passed as a single
   * string argument -- passing structured data as a string and parsing it on
   * the JSX side is the only way to move nested objects across evalScript
   * reliably.
   */
  function callHost(op, params, done) {
    var payload;
    try {
      payload = JSON.stringify({ op: op, params: params || {} });
    } catch (e) {
      done({ ok: false, error: { code: 'bad_request',
                                 message: 'params are not JSON-serialisable' } });
      return;
    }

    // Escaped so the JSON survives being embedded in an ExtendScript string
    // literal; backslashes first, then quotes, then the newlines ExtendScript
    // would otherwise treat as a statement break.
    var escaped = payload
      .replace(/\\/g, '\\\\')
      .replace(/'/g, "\\'")
      .replace(/\r/g, '\\r')
      .replace(/\n/g, '\\n');

    var script = 'NovaBridge.dispatch(\'' + escaped + '\')';

    cs.evalScript(script, function (raw) {
      done(parseHostResult(raw, op));
    });
  }

  function parseHostResult(raw, op) {
    if (raw === undefined || raw === null || raw === '') {
      return { ok: false, error: {
        code: 'host_error',
        message: 'Premiere returned nothing for "' + op + '"',
        hint: 'The host script may not have loaded. Close and reopen the panel.'
      } };
    }
    if (typeof raw === 'string' && raw.indexOf('EvalScript error') === 0) {
      return { ok: false, error: {
        code: 'host_error',
        message: 'ExtendScript failed to evaluate "' + op + '"',
        hint: 'Usually a syntax error in the host script, or the panel losing ' +
              'its host context. Reopen the panel; if it persists, reinstall.'
      } };
    }
    try {
      return JSON.parse(raw);
    } catch (e) {
      return { ok: false, error: {
        code: 'host_error',
        message: 'Premiere returned an unparseable response for "' + op + '"',
        detail: { raw: String(raw).substring(0, 500) }
      } };
    }
  }

  // ------------------------------------------------------------------
  // Node-side operations
  //
  // A few operations are better done here than in ExtendScript: file copies
  // for checkpoints (ExtendScript's File.copy is slow and silently fails on
  // large projects) and anything needing the real filesystem.
  // ------------------------------------------------------------------

  var nodeOps = {
    'project.checkpoint': function (params, done) {
      callHost('project.save', {}, function (saved) {
        if (!saved.ok) { done(saved); return; }
        var projectPath = saved.result && saved.result.path;
        if (!projectPath) {
          done({ ok: false, error: {
            code: 'host_error',
            message: 'Project has never been saved, so it cannot be checkpointed',
            hint: 'Save the project once in Premiere, then retry.'
          } });
          return;
        }
        try {
          var dir = path.join(path.dirname(projectPath), '.nova_checkpoints');
          if (!fs.existsSync(dir)) { fs.mkdirSync(dir); }
          var name = (params && params.name ? String(params.name) : 'checkpoint')
            .replace(/[^A-Za-z0-9._-]/g, '_');
          var target = path.join(dir, name + '.prproj');
          fs.writeFileSync(target, fs.readFileSync(projectPath));
          pruneCheckpoints(dir);
          done({ ok: true, result: { path: target, source: projectPath } });
        } catch (e) {
          done({ ok: false, error: { code: 'host_error',
                                     message: 'Checkpoint copy failed: ' + e.message } });
        }
      });
    },

    'project.restore': function (params, done) {
      var source = params && params.path;
      if (!source || !fs.existsSync(source)) {
        done({ ok: false, error: { code: 'host_error',
                                   message: 'Checkpoint file not found: ' + source } });
        return;
      }
      // Copy the checkpoint back over the live project, then reopen it. Doing
      // the copy here (rather than opening the checkpoint file directly) keeps
      // the user's project path stable -- reopening a file inside
      // .nova_checkpoints would silently change where future saves land.
      callHost('project.path', {}, function (info) {
        var live = info.ok && info.result && info.result.path;
        if (!live) {
          done({ ok: false, error: { code: 'host_error',
                                     message: 'Cannot determine the current project path' } });
          return;
        }
        try {
          fs.writeFileSync(live, fs.readFileSync(source));
        } catch (e) {
          done({ ok: false, error: { code: 'host_error',
                                     message: 'Restore copy failed: ' + e.message } });
          return;
        }
        callHost('project.open', { path: live }, done);
      });
    }
  };

  function pruneCheckpoints(dir) {
    // Checkpoints are whole project files; an unattended editing session would
    // otherwise fill the disk. Keep the 20 newest.
    try {
      var files = fs.readdirSync(dir)
        .filter(function (f) { return /\.prproj$/.test(f); })
        .map(function (f) {
          var full = path.join(dir, f);
          return { path: full, at: fs.statSync(full).mtime.getTime() };
        })
        .sort(function (a, b) { return b.at - a.at; });
      for (var i = 20; i < files.length; i++) { fs.unlinkSync(files[i].path); }
    } catch (e) { /* pruning is best-effort */ }
  }

  // ------------------------------------------------------------------
  // Dispatch
  // ------------------------------------------------------------------

  function dispatch(op, params, done) {
    state.ops += 1;
    setRow('ops', true, String(state.ops));
    if (nodeOps[op]) {
      nodeOps[op](params, done);
      return;
    }
    callHost(op, params, done);
  }

  /** Run a batch sequentially; evalScript is not safe to run concurrently. */
  function dispatchBatch(ops, onError, done) {
    var results = [];
    var index = 0;

    function next() {
      if (index >= ops.length) {
        done({ ok: true, result: { results: results, applied: countOk(results) } });
        return;
      }
      var entry = ops[index];
      dispatch(entry.op, entry.params, function (outcome) {
        results.push({
          op: entry.op,
          index: index,
          ok: !!outcome.ok,
          result: outcome.ok ? outcome.result : undefined,
          error: outcome.ok ? undefined : outcome.error
        });
        index += 1;
        if (!outcome.ok && onError === 'abort') {
          done({ ok: true, result: { results: results, applied: countOk(results),
                                     aborted_at: index - 1 } });
          return;
        }
        // Yield to the event loop so a long batch does not freeze the panel
        // and Premiere can service its own UI between operations.
        setTimeout(next, 0);
      });
    }
    next();
  }

  function countOk(results) {
    var n = 0;
    for (var i = 0; i < results.length; i++) { if (results[i].ok) { n += 1; } }
    return n;
  }

  // ------------------------------------------------------------------
  // HTTP server
  // ------------------------------------------------------------------

  function send(res, status, body) {
    var text = JSON.stringify(body);
    res.writeHead(status, {
      'Content-Type': 'application/json',
      'Content-Length': Buffer.byteLength(text),
      'Cache-Control': 'no-store'
    });
    res.end(text);
  }

  function readBody(req, done) {
    var chunks = [];
    var size = 0;
    req.on('data', function (chunk) {
      size += chunk.length;
      if (size > MAX_BODY) {
        req.destroy();
        done(new Error('request body too large'));
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', function () {
      try {
        done(null, JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}'));
      } catch (e) {
        done(e);
      }
    });
    req.on('error', done);
  }

  var server = http.createServer(function (req, res) {
    // Loopback only. The panel has full control of the user's project, so a
    // request from another machine is never legitimate.
    var remote = req.socket.remoteAddress || '';
    if (remote !== '127.0.0.1' && remote !== '::1' && remote !== '::ffff:127.0.0.1') {
      send(res, 403, { ok: false, error: { code: 'forbidden',
                                           message: 'loopback connections only' } });
      return;
    }

    if (req.method === 'GET' && req.url === '/health') {
      callHost('project.info', {}, function (outcome) {
        var info = outcome.ok ? (outcome.result || {}) : {};
        state.hostReady = !!outcome.ok;
        state.project = info.name || '';
        setRow('host', !!outcome.ok, outcome.ok ? 'ready' : 'not responding');
        setRow('project', !!info.name, info.name || 'no project open');
        send(res, 200, {
          ok: true,
          connected: true,
          host_ready: !!outcome.ok,
          project_open: !!info.name,
          project: info.name || null,
          version: info.version || null,
          sequence: info.sequence || null,
          operations_served: state.ops
        });
      });
      return;
    }

    if (req.method === 'POST' && (req.url === '/exec' || req.url === '/batch')) {
      var isBatch = req.url === '/batch';
      readBody(req, function (err, body) {
        if (err) {
          send(res, 400, { ok: false, error: { code: 'bad_request',
                                               message: err.message } });
          return;
        }
        if (isBatch) {
          if (!body || !(body.ops instanceof Array)) {
            send(res, 400, { ok: false, error: { code: 'bad_request',
                                                 message: '"ops" must be a list' } });
            return;
          }
          log('batch of ' + body.ops.length);
          dispatchBatch(body.ops, body.on_error || 'abort', function (outcome) {
            send(res, 200, outcome);
          });
          return;
        }
        if (!body || !body.op) {
          send(res, 400, { ok: false, error: { code: 'bad_request',
                                               message: '"op" is required' } });
          return;
        }
        dispatch(body.op, body.params, function (outcome) {
          if (!outcome.ok) {
            state.errors += 1;
            log(body.op + ' -> ' + (outcome.error && outcome.error.message), true);
          } else {
            log(body.op);
          }
          send(res, 200, outcome);
        });
      });
      return;
    }

    send(res, 404, { ok: false, error: { code: 'not_found', message: 'unknown route' } });
  });

  server.on('error', function (err) {
    if (err.code === 'EADDRINUSE') {
      showFatal('Port ' + PORT + ' is already in use. Another Premiere instance ' +
                'or panel is probably running the bridge.');
    } else {
      showFatal('Server error: ' + err.message);
    }
  });

  server.listen(PORT, HOST, function () {
    setRow('server', true, HOST + ':' + PORT);
    log('bridge listening on ' + HOST + ':' + PORT);
    callHost('project.info', {}, function (outcome) {
      setRow('host', !!outcome.ok, outcome.ok ? 'ready' : 'not responding');
      if (outcome.ok && outcome.result) {
        setRow('project', !!outcome.result.name, outcome.result.name || 'no project open');
        log('Premiere ' + (outcome.result.version || '?') + ' connected');
      } else if (!outcome.ok) {
        log((outcome.error && outcome.error.message) || 'host not responding', true);
      }
    });
  });

  window.addEventListener('beforeunload', function () {
    try { server.close(); } catch (e) { /* shutting down anyway */ }
  });
}());
