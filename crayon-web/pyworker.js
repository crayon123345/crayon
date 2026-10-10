// Runs Python (Pyodide) inside the visitor's browser. Nothing here touches the server or its keys.
let py = null, out = [];
async function boot() {
  if (py) return;
  importScripts('https://cdn.jsdelivr.net/pyodide/v0.26.4/full/pyodide.js');
  py = await loadPyodide();
  py.setStdout({batched: (s) => out.push(s)});
  py.setStderr({batched: (s) => out.push(s)});
  try { py.FS.mkdir('/work'); } catch (e) {}
}
function listWork() {
  const m = {};
  for (const n of py.FS.readdir('/work')) {
    if (n === '.' || n === '..') continue;
    const st = py.FS.stat('/work/' + n);
    if (py.FS.isDir(st.mode)) continue;
    m[n] = st.size + ':' + st.mtime.getTime();
  }
  return m;
}
self.onmessage = async (e) => {
  const {id, code, files} = e.data;
  out = [];
  try {
    self.postMessage({id, status: 'Starting the computer...'});
    await boot();
    for (const [n, buf] of Object.entries(files || {})) py.FS.writeFile('/work/' + n, new Uint8Array(buf));
    const before = listWork();
    self.postMessage({id, status: 'Running...'});
    await py.loadPackagesFromImports(code);
    py.runPython("import os; os.chdir('/work')");
    let err = '';
    try { await py.runPythonAsync(code); } catch (ex) { err = String(ex.message || ex).split('\n').slice(-12).join('\n'); }
    const after = listWork(), changed = [];
    for (const n of Object.keys(after)) if (before[n] !== after[n]) changed.push({name: n, data: py.FS.readFile('/work/' + n).buffer});
    self.postMessage({id, done: true, stdout: out.join('\n'), error: err, files: changed}, changed.map((f) => f.data));
  } catch (ex) {
    self.postMessage({id, done: true, stdout: out.join('\n'), error: 'Could not start the computer: ' + String(ex.message || ex), files: []});
  }
};
