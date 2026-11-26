// WombatGC structure editor: Ketcher (Apache-2.0, EPAM Systems) running fully offline.
// The desktop app talks to it through window.wg (see structure_editor.py).
import './shim.js';
import React from 'react';
import { createRoot } from 'react-dom/client';
import 'ketcher-react/dist/index.css';
import { Editor } from 'ketcher-react';
import { StandaloneStructServiceProvider } from 'ketcher-standalone';

const provider = new StandaloneStructServiceProvider();

function blobToBase64(blob) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(',')[1] || '');
    r.onerror = reject;
    r.readAsDataURL(blob);
  });
}

// Each request stores its answer in wg.results[id] for the app to collect.
window.wg = {
  ready: false,
  results: {},
  errors: [],
  run(id, fn) {
    this.results[id] = undefined;
    Promise.resolve()
      .then(fn)
      .then((v) => { this.results[id] = { ok: true, value: v }; })
      .catch((e) => { this.results[id] = { ok: false, value: String((e && e.message) || e) }; });
    return id;
  },
  take(id) {
    const r = this.results[id];
    if (r !== undefined) delete this.results[id];
    return r === undefined ? null : JSON.stringify(r);
  },
  async snapshot(withImages) {
    const k = window.ketcher;
    const ket = await k.getKet();
    const empty = JSON.parse(ket).root.nodes.length === 0;
    const out = { ket, empty, reaction: k.containsReaction() };
    if (empty) return out;
    try { out.smiles = await k.getSmiles(); } catch (e) { out.smiles = ''; }
    try { out.molfile = out.reaction ? await k.getRxn('v3000') : await k.getMolfile(); } catch (e) { out.molfile = ''; }
    try {
      const c = await k.calculate({ properties: ['gross', 'molecular-weight', 'monoisotopic-mass'] });
      out.formula = c['gross']; out.mw = c['molecular-weight']; out.mono = c['monoisotopic-mass'];
    } catch (e) { out.calcError = String(e); }
    if (withImages) {
      out.svg = await k.generateImage(ket, { outputFormat: 'svg' }).then((b) => b.text());   // transparent
      out.png = await blobToBase64(await k.generateImage(ket, { outputFormat: 'png', backgroundColor: '255, 255, 255' }));
    }
    return out;
  },
};

function App() {
  return (
    <Editor
      staticResourcesUrl={'./'}
      structServiceProvider={provider}
      disableMacromoleculesEditor={true}
      errorHandler={(m) => { window.wg.errors.push(String(m)); console.error(m); }}
      onInit={(ketcher) => { window.ketcher = ketcher; window.wg.ready = true; }}
    />
  );
}

createRoot(document.getElementById('root')).render(<App />);
