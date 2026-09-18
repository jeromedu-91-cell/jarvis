/* ==========================================================================
   Harnais des tests d'interface Mission Control.

   Principe : on charge le FICHIER SOURCE RÉEL (ui/js/v5/spatial_mission_control.js),
   évalué comme le navigateur l'évalue. Aucune copie, aucune transformation :
   si le test passe, c'est le code livré qui passe.

   Le bus `J` de core.js est déclaré avec `const` au niveau du script : il vit
   dans la portée globale du script, pas sur `window`. On le pose donc sur
   `globalThis`, ce qui reproduit exactement la résolution lexicale que fait le
   navigateur.
   ========================================================================== */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { vi } from 'vitest';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
export const SOURCE = path.join(ROOT, 'ui', 'js', 'v5', 'spatial_mission_control.js');
export const STYLESHEET = path.join(ROOT, 'ui', 'css', 'v5', 'mission_control.css');

/* --------------------------------------------------------- faux bus `J` */
function makeBus(routes) {
  const listeners = {};
  const calls = { get: [], write: [] };
  const bus = {
    state: { connected: true },
    listeners,
    calls,
    on(type, cb) { (listeners[type] = listeners[type] || []).push(cb); },
    fire(type, payload) { (listeners[type] || []).forEach((cb) => cb(payload)); },
    async get(url) {
      calls.get.push(url);
      const pathname = url.split('?')[0];
      // On prend la route la PLUS SPÉCIFIQUE : sinon « /api/missions »
      // intercepterait « /api/missions/history » et le test mesurerait
      // autre chose que ce qu'il croit.
      const key = Object.keys(routes)
        .filter((r) => pathname === r || pathname.endsWith(r))
        .sort((a, b) => b.length - a.length)[0];
      const body = key ? routes[key] : null;
      const data = typeof body === 'function' ? body(url) : body;
      return data === null || data === undefined
        ? { ok: false, error: 'not found' }
        : { ok: true, data };
    },
    // Toute écriture est enregistrée : c'est ce qui permet de prouver qu'un
    // REPLAY ne déclenche aucune action réelle.
    async post(url, body) { calls.write.push({ method: 'POST', url, body }); return { ok: true, data: {} }; },
    async put(url, body) { calls.write.push({ method: 'PUT', url, body }); return { ok: true, data: {} }; },
    async del(url) { calls.write.push({ method: 'DELETE', url }); return { ok: true, data: {} }; },
  };
  return bus;
}

/* Le module garde des TIMERS RÉELS : ticker (1 s), debounce d'historique
   (400 ms), repli de rendu (250 ms), réconciliation (10 s), santé (20 s).
   Un test qui laisse le panneau ouvert ne les éteint pas : le callback tire
   APRÈS la fin du test — et comme `byId` interroge le `document` partagé, il
   réécrit le DOM du TEST SUIVANT avec des données périmées. C'est la cause du
   caractère non déterministe des tests SSE réconciliation/rafale : une
   assertion tombe ou non dans la fenêtre du timer selon la charge machine.
   On éteint donc l'instance au montage suivant : `hide()` arrête les
   intervalles, et les méthodes touchant le DOM deviennent des no-ops pour
   neutraliser les timeouts restés armés. */
let previousMC = null;

function disposePrevious() {
  const mc = previousMC;
  previousMC = null;
  if (!mc) return;
  try { mc.hide(); } catch (_) { /* instance déjà fermée */ }
  mc.render = () => {};
  mc.schedule = () => {};
  mc.badge = () => {};
  mc.loadHistory = async () => {};
  mc.loadHealth = async () => {};
  mc.hydrate = async () => {};
  mc.reconcile = async () => {};
}

/* Le module s'abonne à `window` (clavier). En production il est chargé UNE
   fois ; dans un fichier de test, chaque montage ajouterait un abonnement de
   plus sur la même fenêtre jsdom — et un seul Échap déclencherait alors
   plusieurs fermetures. On note donc ce que chaque montage abonne, pour le
   retirer au montage suivant. */
let mounted = [];

function trackListeners() {
  mounted.forEach(({ target, type, fn, opts }) => target.removeEventListener(type, fn, opts));
  mounted = [];
  for (const target of [window, document]) {
    if (target.__origAdd) continue;
    const original = target.addEventListener.bind(target);
    target.__origAdd = original;
    target.addEventListener = (type, fn, opts) => {
      mounted.push({ target, type, fn, opts });
      original(type, fn, opts);
    };
  }
}

/* Monte Mission Control dans le DOM de test.
   `routes` associe une fin d'URL à sa réponse (l'objet `data` de l'API). */
export async function mount(routes = {}) {
  disposePrevious();
  trackListeners();
  document.body.innerHTML = '';
  document.documentElement.removeAttribute('data-mc-open');
  document.documentElement.removeAttribute('data-mc-compact');
  document.documentElement.removeAttribute('data-mc-live');
  delete globalThis.JarvisMissionControl;
  delete window.JarvisMissionControl;

  const bus = makeBus(routes);
  globalThis.J = bus;
  window.J = bus;

  // `fetch` est surveillé séparément : le code peut s'en servir en repli, et
  // un test doit pouvoir affirmer qu'aucune requête d'écriture n'est partie.
  const fetchCalls = [];
  globalThis.fetch = vi.fn(async (url, opts) => {
    fetchCalls.push({ url: String(url), method: (opts && opts.method) || 'GET' });
    return { json: async () => ({ ok: false, error: 'fetch non simulé' }) };
  });

  // rAF asynchrone, comme dans un vrai navigateur : un rAF synchrone
  // donnerait un ordre d'exécution que le produit ne rencontre jamais.
  globalThis.requestAnimationFrame = (cb) => setTimeout(() => cb(0), 0);
  globalThis.cancelAnimationFrame = () => {};
  if (!navigator.clipboard) {
    Object.defineProperty(navigator, 'clipboard', { value: {}, configurable: true });
  }
  navigator.clipboard.writeText = vi.fn(async () => {});
  window.toast = vi.fn();

  const source = fs.readFileSync(SOURCE, 'utf8');
  // eslint-disable-next-line no-new-func
  new Function(source)();

  const MC = window.JarvisMissionControl;
  MC.bind();
  await flush();
  previousMC = MC;
  return { MC, bus, fetchCalls };
}

/* Laisse les promesses et les timers courts se résoudre. */
export async function flush(times = 4) {
  for (let i = 0; i < times; i += 1) {
    await Promise.resolve();
    await new Promise((r) => setTimeout(r, 0));
  }
}

export const $ = (sel) => document.querySelector(sel);
export const $$ = (sel) => Array.from(document.querySelectorAll(sel));
export const text = (sel) => ($(sel) ? $(sel).textContent.trim() : null);

/* ------------------------------------------------------------- fixtures */
/* Ces objets ont la forme EXACTE de ce que publie jarvis/mission_control.py
   (`Mission.to_dict`). Si le backend change de contrat, ces tests doivent
   casser — c'est leur raison d'être. */
export function mission(over = {}) {
  const now = Date.now() / 1000;
  return {
    id: 'task_1',
    name: 'Analyse du marketplace',
    kind: 'chat',
    agent: 'BrainrotFortniteAgent',
    conversation_id: '',
    state: 'RUNNING',
    active: true,
    created_at: now - 100,
    started_at: now - 100,
    ended_at: null,
    duration: 100,
    progress: null,
    steps: [],
    steps_done: 0,
    steps_total: 0,
    current_step: null,
    tool: null,
    last_tool: null,
    tool_calls: 0,
    tools: [],
    failed_step: '',
    failed_tool: '',
    files: [],
    result: '',
    error: '',
    note: '',
    history: [],
    ...over,
  };
}

export function snapshot(missions) {
  const list = [].concat(missions);
  const live = list.filter((m) => m.active);
  const recent = list.filter((m) => !m.active);
  return {
    current: live[live.length - 1] || recent[recent.length - 1] || null,
    live,
    recent,
    states: ['WAITING', 'THINKING', 'RUNNING', 'TOOL', 'VERIFYING', 'COMPLETED', 'FAILED'],
  };
}

export function history(missions, counts) {
  const list = [].concat(missions);
  return {
    missions: list,
    counts: counts || {
      ALL: list.length,
      RUNNING: list.filter((m) => m.active).length,
      COMPLETED: list.filter((m) => m.state === 'COMPLETED').length,
      FAILED: list.filter((m) => m.state === 'FAILED').length,
    },
    status: 'ALL',
    filters: ['ALL', 'RUNNING', 'COMPLETED', 'FAILED'],
  };
}

export function healthReport(over = {}) {
  return {
    components: [
      { id: 'jarvis_core', name: 'JARVIS Core', state: 'ONLINE', detail: '2 h' },
      { id: 'llm', name: 'LLM', state: 'UNKNOWN', detail: 'Vérification en cours' },
      { id: 'event_bus', name: 'Event Bus', state: 'ONLINE', detail: '1 client SSE' },
      { id: 'task_engine', name: 'Task Engine', state: 'ONLINE', detail: '1 active' },
      { id: 'database', name: 'Database', state: 'ONLINE', detail: 'lecture en 1 ms' },
    ],
    overall: 'DEGRADED',
    checked_at: Date.now() / 1000,
    ...over,
  };
}

/* Timeline telle que la renvoie /api/missions/<id>/timeline. */
export function timelineEntry(over = {}) {
  return {
    ts: Date.now() / 1000,
    offset_ms: 0,
    kind: 'log',
    label: 'Événement',
    state: '',
    tool: '',
    ok: null,
    duration_ms: 0,
    detail: '',
    ...over,
  };
}
