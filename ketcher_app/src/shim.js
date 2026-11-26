// ketcher-react's ES build still calls require('raphael') at load time; give it the real module.
import Raphael from 'raphael';
const mods = { raphael: Raphael };
if (typeof window.require === 'undefined') {
  window.require = (name) => {
    if (name in mods) return mods[name];
    throw new Error(`Cannot find module '${name}'`);
  };
}
