/* Flux SSE, réconciliation, rafale d'événements et accessibilité de base.

   Le scénario nerveux : le bus d'événements du backend LARGUE un abonné SSE
   trop lent sans fermer la connexion HTTP. Le navigateur ne voit aucune
   erreur et se croit connecté — l'écran peut donc rester figé en affichant
   « DIRECT ». Ces tests verrouillent le garde-fou : une mission réellement
   terminée ne doit jamais rester visuellement EN COURS. */
import { describe, expect, it } from 'vitest';
import { $, $$, flush, healthReport, history, mission, mount, snapshot } from './harness.js';

const RUNNING = mission({
  id: 'task_s', name: 'Analyse du marketplace', state: 'RUNNING', active: true,
  history: [{ ts: 1, kind: 'mission', label: 'Mission créée' }],
});
const FINISHED = mission({
  ...RUNNING, state: 'COMPLETED', active: false, ended_at: 2000, duration: 30,
  result: 'Rapport généré.',
  history: [
    { ts: 1, kind: 'mission', label: 'Mission créée' },
    { ts: 2, kind: 'mission', label: 'Mission terminée' },
  ],
});

function routes(current) {
  return {
    '/api/missions': () => snapshot([current.value]),
    '/api/missions/history': () => history([current.value]),
    '/api/missions/health': healthReport(),
  };
}

describe('A — flux SSE normal', () => {
  it('affiche une mission poussée par le flux', async () => {
    const current = { value: RUNNING };
    const { MC, bus } = await mount(routes(current));
    MC.show();
    await flush();
    bus.fire('mission.started', RUNNING);
    await flush();
    expect(MC.missions.get('task_s')).toBeTruthy();
    expect($('#mcName').textContent).toContain('Analyse du marketplace');
    expect($('#mcStateLabel').textContent).toBe('EN COURS');
  });

  it('suit les changements d’état en direct', async () => {
    const current = { value: RUNNING };
    const { MC, bus } = await mount(routes(current));
    MC.show();
    await flush();
    bus.fire('mission.update', { ...RUNNING, state: 'TOOL', tool: { id: 'fs.read', name: 'Lire', args: {} } });
    await flush();
    expect($('#mcStateLabel').textContent).toBe('OUTIL');
    bus.fire('mission.completed', FINISHED);
    await flush();
    expect($('#mcStateLabel').textContent).toBe('TERMINÉ');
  });

  it('tient la pastille de missions vivantes à jour', async () => {
    const current = { value: RUNNING };
    const { MC, bus } = await mount(routes(current));
    MC.show();
    await flush();
    bus.fire('mission.started', RUNNING);
    await flush();
    expect(document.documentElement.dataset.mcLive).toBe('1');
    bus.fire('mission.completed', FINISHED);
    await flush();
    expect(document.documentElement.dataset.mcLive).toBe('');
  });
});

describe('B — flux silencieux', () => {
  it('annonce HORS LIGNE quand le flux se tait alors qu’une mission tourne', async () => {
    const current = { value: RUNNING };
    const { MC, bus } = await mount(routes(current));
    MC.show();
    await flush();
    bus.fire('mission.started', RUNNING);
    await flush();
    expect($('#mcLive').textContent).toBe('DIRECT');
    // Silence prolongé : le dernier événement remonte à bien plus que le seuil.
    MC._lastEventAt = Date.now() - 60000;
    await MC.reconcile();
    await flush();
    expect($('#mcLive').textContent).toBe('HORS LIGNE');
    expect($('#mcLive').dataset.off).toBe('1');
  });

  it('ne crie pas au silence quand plus rien ne tourne', async () => {
    const current = { value: FINISHED };
    const { MC } = await mount(routes(current));
    MC.show();
    await flush();
    MC._lastEventAt = Date.now() - 60000;
    await MC.reconcile();
    await flush();
    expect($('#mcLive').textContent).toBe('DIRECT');
  });

  it('repasse en DIRECT dès qu’un événement arrive', async () => {
    const current = { value: RUNNING };
    const { MC, bus } = await mount(routes(current));
    MC.show();
    await flush();
    MC._lastEventAt = Date.now() - 60000;
    await MC.reconcile();
    expect($('#mcLive').textContent).toBe('HORS LIGNE');
    bus.fire('mission.update', RUNNING);
    await MC.reconcile();
    await flush();
    expect($('#mcLive').textContent).toBe('DIRECT');
  });
});

describe('C — abonné perdu, D — réconciliation', () => {
  /* Le cas qui a motivé tout ce garde-fou : le backend a terminé la mission,
     mais l'écran n'a jamais reçu l'événement. */
  it('une mission réellement COMPLETED ne reste pas visuellement EN COURS', async () => {
    const current = { value: RUNNING };
    const { MC, bus } = await mount(routes(current));
    MC.show();
    await flush();
    bus.fire('mission.started', RUNNING);
    await flush();
    expect($('#mcStateLabel').textContent).toBe('EN COURS');

    // Le backend termine la mission. Aucun événement n'atteint le client :
    // l'abonné a été largué, la connexion HTTP est restée ouverte.
    current.value = FINISHED;
    await flush();
    expect($('#mcStateLabel').textContent).toBe('EN COURS');   // encore figé

    await MC.reconcile();                                       // relecture
    await flush();
    expect(MC.missions.get('task_s').state).toBe('COMPLETED');
    expect($('#mcStateLabel').textContent).toBe('TERMINÉ');
  });

  it('la réconciliation relit missions, historique et santé', async () => {
    const current = { value: RUNNING };
    const { MC, bus } = await mount(routes(current));
    MC.show();
    await flush();
    bus.calls.get.length = 0;
    await MC.reconcile();
    await flush();
    expect(bus.calls.get.some((u) => u.startsWith('/api/missions?') || u === '/api/missions')).toBe(true);
    expect(bus.calls.get.some((u) => u.includes('/history'))).toBe(true);
  });

  it('ne réconcilie pas pendant un REPLAY : l’écran raconte alors le passé', async () => {
    const current = { value: FINISHED };
    const { MC, bus } = await mount({
      ...routes(current),
      '/api/missions/task_s/timeline': {
        timeline: [{ ts: 1, offset_ms: 0, kind: 'mission', label: 'Mission créée', ok: null, duration_ms: 0, detail: '', tool: '', state: '' }],
        id: 'task_s',
      },
    });
    MC.show();
    await flush();
    await MC.toggleReplay();
    await flush();
    bus.calls.get.length = 0;
    await MC.reconcile();
    expect(bus.calls.get).toEqual([]);
  });

  it('resynchronise à la reconnexion du flux', async () => {
    const current = { value: RUNNING };
    const { MC, bus } = await mount(routes(current));
    MC.show();
    await flush();
    current.value = FINISHED;
    bus.fire('stream.close');
    await flush();
    expect($('#mcLive').textContent).toBe('HORS LIGNE');
    bus.fire('stream.open');
    await flush();
    expect($('#mcLive').textContent).toBe('DIRECT');
    expect(MC.missions.get('task_s').state).toBe('COMPLETED');
  });
});

describe('rafale d’événements', () => {
  const burst = (bus, n) => {
    for (let i = 0; i < n; i += 1) {
      bus.fire('mission.update', {
        ...RUNNING,
        history: [{ ts: i, kind: 'log', label: `Message ${i}` }],
      });
    }
  };

  it('encaisse 800 événements sans multiplier les rendus', async () => {
    const current = { value: RUNNING };
    const { MC, bus } = await mount(routes(current));
    MC.show();
    await flush();
    let renders = 0;
    const original = MC.render.bind(MC);
    MC.render = () => { renders += 1; return original(); };
    burst(bus, 800);
    await flush(6);
    // Un rendu par frame, pas un par événement.
    expect(renders).toBeLessThan(50);
    expect(renders).toBeGreaterThan(0);
  });

  it('borne le journal affiché', async () => {
    const current = { value: RUNNING };
    const { MC, bus } = await mount(routes(current));
    MC.show();
    await flush();
    const long = Array.from({ length: 800 }, (_, i) => ({ ts: i, kind: 'log', label: `Message ${i}` }));
    bus.fire('mission.update', { ...RUNNING, history: long });
    await flush(6);
    const rendered = $$('#mcHist .mc-hrow').length;
    expect(rendered).toBeLessThanOrEqual(80);
    expect(rendered).toBeGreaterThan(0);
  });

  it('ne laisse pas exploser le nombre de nœuds DOM', async () => {
    const current = { value: RUNNING };
    const { MC, bus } = await mount(routes(current));
    MC.show();
    await flush();
    const before = document.getElementsByTagName('*').length;
    burst(bus, 800);
    await flush(6);
    const after = document.getElementsByTagName('*').length;
    expect(after - before).toBeLessThan(200);
  });

  it('borne aussi l’historique gardé en mémoire', async () => {
    const current = { value: RUNNING };
    const { MC, bus } = await mount(routes(current));
    MC.show();
    await flush();
    for (let i = 0; i < 60; i += 1) {
      bus.fire('mission.update', {
        ...RUNNING,
        history: Array.from({ length: 30 }, (_, k) => ({ ts: i * 100 + k, kind: 'log', label: `L${i}-${k}` })),
      });
    }
    await flush(6);
    expect(MC.missions.get('task_s').history.length).toBeLessThanOrEqual(240);
  });

  it('finit sur l’état correct après la rafale', async () => {
    const current = { value: RUNNING };
    const { MC, bus } = await mount(routes(current));
    MC.show();
    await flush();
    burst(bus, 800);
    bus.fire('mission.completed', FINISHED);
    await flush(6);
    expect(MC.missions.get('task_s').state).toBe('COMPLETED');
    expect($('#mcStateLabel').textContent).toBe('TERMINÉ');
  });
});

describe('accessibilité de base', () => {
  async function ready() {
    const current = { value: RUNNING };
    const h = await mount(routes(current));
    h.MC.show();
    await flush();
    return h;
  }

  it('les commandes principales sont de vrais boutons, donc au clavier', async () => {
    await ready();
    for (const sel of ['#mcClose', '#mcCompact', '#mcCopy', '#mcReplayToggle',
      '#mcReplayStop', '#mcSearchClear', '.mc-mode', '.mc-filter', '.mc-speed']) {
      expect($(sel), sel).toBeTruthy();
      expect($(sel).tagName, sel).toBe('BUTTON');
    }
  });

  it('les entrées de la liste de missions sont des boutons', async () => {
    const { MC, bus } = await ready();
    bus.fire('mission.started', RUNNING);
    await flush();
    const rows = $$('#mcList .mc-row');
    expect(rows.length).toBeGreaterThan(0);
    rows.forEach((r) => expect(r.tagName).toBe('BUTTON'));
  });

  it('l’état sélectionné est exposé dans le DOM, pas seulement en couleur', async () => {
    const { MC } = await ready();
    MC.setMode('HISTORY');
    await flush();
    expect($('.mc-mode[data-mode="HISTORY"]').dataset.on).toBe('1');
    MC.setFilter('FAILED');
    await flush();
    expect($('.mc-filter[data-filter="FAILED"]').dataset.on).toBe('1');
    expect($$('.mc-filter[role="tab"]').length).toBe(4);
  });

  it('les commandes portent un intitulé lisible', async () => {
    await ready();
    expect($('#mcClose').getAttribute('aria-label') || $('#mcClose').title).toBeTruthy();
    expect($('#mcCompact').title).toBeTruthy();
    expect($('#mcSearch').getAttribute('aria-label')).toBeTruthy();
    expect($('#mcReplayToggle').title).toContain('aucune action');
  });

  it('Ctrl+M ouvre et ferme Mission Control', async () => {
    const { MC } = await ready();
    MC.hide();
    await flush();
    expect(MC.open).toBe(false);
    window.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'm', ctrlKey: true }));
    await flush();
    expect(MC.open).toBe(true);
    window.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'm', ctrlKey: true }));
    await flush();
    expect(MC.open).toBe(false);
  });

  it('Cmd+M fonctionne aussi', async () => {
    const { MC } = await ready();
    MC.hide();
    await flush();
    window.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'M', metaKey: true }));
    await flush();
    expect(MC.open).toBe(true);
  });

  it('Ctrl+M ne surgit pas pendant qu’on écrit', async () => {
    const { MC } = await ready();
    MC.hide();
    await flush();
    const input = document.createElement('input');
    document.body.appendChild(input);
    const event = new window.KeyboardEvent('keydown', { key: 'm', ctrlKey: true, bubbles: true });
    input.dispatchEvent(event);
    await flush();
    expect(MC.open).toBe(false);
  });

  it('Échap dans la recherche vide le champ sans fermer le panneau', async () => {
    const { MC } = await ready();
    MC.setQuery('audit');
    await flush();
    const search = $('#mcSearch');
    search.value = 'audit';
    search.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    await flush();
    expect(MC.query).toBe('');
    expect(MC.open).toBe(true);
  });

  it('respecte prefers-reduced-motion pour l’étape active', async () => {
    // La pulsation de l'étape active est purement CSS : on vérifie que la
    // feuille livrée la neutralise bien, faute de pouvoir l'observer en jsdom.
    const fs = await import('node:fs');
    const { STYLESHEET } = await import('./harness.js');
    const css = fs.readFileSync(STYLESHEET, 'utf8').replace(/\s+/g, ' ');
    expect(css).toContain('@media (prefers-reduced-motion:reduce)');
    expect(css).toMatch(/@media \(prefers-reduced-motion:reduce\)\{[^}]*animation:none/);
  });
});
