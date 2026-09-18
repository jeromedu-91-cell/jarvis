/* REPLAY et MODE COMPACT.

   Le point capital de ce fichier : le REPLAY est une RELECTURE. Il redonne des
   événements déjà survenus et ne doit déclencher aucune action réelle. Les
   tests échouent si la moindre requête d'écriture part pendant un replay. */
import { describe, expect, it, vi } from 'vitest';
import { $, $$, flush, healthReport, history, mission, mount, snapshot, timelineEntry } from './harness.js';

const TIMELINE = [
  timelineEntry({ offset_ms: 0, kind: 'mission', label: 'Mission créée' }),
  timelineEntry({ offset_ms: 2000, kind: 'tool', label: 'Recherche marketplace', tool: 'marketplace.search', detail: '{"query": "skins", "api_key": "•••"}' }),
  timelineEntry({ offset_ms: 4000, kind: 'tool_result', label: 'Recherche marketplace', tool: 'marketplace.search', ok: true, duration_ms: 1420, detail: '38 annonces' }),
  timelineEntry({ offset_ms: 6000, kind: 'log', label: 'Analyse en cours' }),
  timelineEntry({ offset_ms: 8000, kind: 'mission', label: 'Mission terminée' }),
];

const M = mission({
  id: 'task_r', name: 'Analyse du marketplace', state: 'COMPLETED', active: false,
  ended_at: 2000, duration: 8, tool_calls: 1, tools: ['marketplace.search'],
  steps: [
    { key: 'a', label: 'Recherche', state: 'done' },
    { key: 'b', label: 'Rapport', state: 'done' },
  ],
  result: 'Rapport généré.',
  history: [{ ts: 1000, kind: 'mission', label: 'Mission terminée' }],
});

const ROUTES = {
  '/api/missions': snapshot([M]),
  '/api/missions/history': history([M]),
  '/api/missions/health': healthReport(),
  '/api/missions/task_r': { mission: M },
  '/api/missions/task_r/timeline': { timeline: TIMELINE, id: 'task_r' },
};

async function ready() {
  const h = await mount(ROUTES);
  h.MC.show();
  await flush();
  return h;
}

describe('REPLAY — commandes', () => {
  it('démarre en lecture et affiche la barre temporelle', async () => {
    const { MC } = await ready();
    await MC.toggleReplay();
    await flush();
    expect(MC.replay).toBeTruthy();
    expect(MC.replay.playing).toBe(true);
    expect($('#mcReplayTrack').hidden).toBe(false);
    expect($('#mcReplayStop').hidden).toBe(false);
  });

  it('annonce clairement qu’on relit, pas qu’on exécute', async () => {
    const { MC } = await ready();
    await MC.toggleReplay();
    await flush();
    expect($('#v5Mission').dataset.replay).toBe('1');
    expect($('#mcName').textContent).toContain('REPLAY');
    expect($('#mcHistTitle').textContent).toBe('REPLAY');
  });

  it('met en pause puis reprend', async () => {
    const { MC } = await ready();
    await MC.toggleReplay();
    await flush();
    MC.pauseReplay();
    await flush();
    expect(MC.replay.playing).toBe(false);
    expect($('#mcReplayToggle').textContent).toContain('REPRENDRE');
    MC.resumeReplay();
    await flush();
    expect(MC.replay.playing).toBe(true);
    expect($('#mcReplayToggle').textContent).toContain('PAUSE');
  });

  it('accepte les vitesses 1x, 2x et 4x, une seule active', async () => {
    const { MC } = await ready();
    await MC.toggleReplay();
    await flush();
    for (const speed of [1, 2, 4]) {
      MC.setSpeed(speed);
      expect(MC.replay.speed).toBe(speed);
      const on = $$('.mc-speed').filter((b) => b.dataset.on === '1');
      expect(on).toHaveLength(1);
      expect(on[0].dataset.speed).toBe(String(speed));
    }
  });

  it('avance dans la timeline et fait progresser la barre', async () => {
    const { MC } = await ready();
    await MC.toggleReplay();
    await flush();
    MC.pauseReplay();
    const firstFill = $('#mcReplayFill').style.width;
    MC.replay.index = 3;
    MC.render();
    const laterFill = $('#mcReplayFill').style.width;
    expect(parseFloat(laterFill)).toBeGreaterThan(parseFloat(firstFill));
    expect($('#mcReplayNow').textContent).toContain(TIMELINE[2].label);
  });

  it('n’affiche que les événements déjà rejoués', async () => {
    const { MC } = await ready();
    await MC.toggleReplay();
    await flush();
    MC.pauseReplay();
    MC.replay.index = 2;
    MC.render();
    const labels = $$('#mcHist .mc-hrow b').map((b) => b.textContent);
    expect(labels).toContain('Mission créée');
    expect(labels).not.toContain('Mission terminée');
  });

  it('marque l’événement en cours de relecture', async () => {
    const { MC } = await ready();
    await MC.toggleReplay();
    await flush();
    MC.pauseReplay();
    MC.replay.index = 2;
    MC.render();
    expect($$('#mcHist .mc-hrow[data-now="1"]')).toHaveLength(1);
  });

  it('finit sur le verdict réel de la mission', async () => {
    const { MC } = await ready();
    await MC.toggleReplay();
    await flush();
    MC.replay.index = TIMELINE.length;
    MC.render();
    expect($('#mcStateLabel').textContent).toBe('TERMINÉ');
  });

  it('revient au direct quand on arrête', async () => {
    const { MC } = await ready();
    await MC.toggleReplay();
    await flush();
    MC.stopReplay();
    await flush();
    expect(MC.replay).toBeNull();
    expect($('#v5Mission').dataset.replay).toBe('');
    expect($('#mcReplayTrack').hidden).toBe(true);
    expect($('#mcHistTitle').textContent).toBe('HISTORIQUE');
  });

  it('refuse de rejouer une mission sans événement enregistré', async () => {
    const h = await mount({ ...ROUTES, '/api/missions/task_r/timeline': { timeline: [], id: 'task_r' } });
    h.MC.show();
    await flush();
    await h.MC.toggleReplay();
    await flush();
    expect(h.MC.replay).toBeNull();
    expect(window.toast).toHaveBeenCalled();
  });
});

describe('REPLAY — strictement passif', () => {
  /* Si ce test tombe, c'est qu'un replay a déclenché une action réelle.
     C'est la garantie la plus importante de la fonctionnalité. */
  it('n’émet AUCUNE requête d’écriture pendant toute la relecture', async () => {
    const { MC, bus, fetchCalls } = await ready();
    await MC.toggleReplay();
    await flush();
    for (const speed of [1, 2, 4]) {
      MC.setSpeed(speed);
      MC.pauseReplay();
      MC.resumeReplay();
      // eslint-disable-next-line no-await-in-loop
      await flush(6);
    }
    MC.replay.index = TIMELINE.length;
    MC.render();
    MC.stopReplay();
    await flush();

    expect(bus.calls.write).toEqual([]);
    expect(fetchCalls.filter((c) => c.method !== 'GET')).toEqual([]);
  });

  it('ne lance aucun outil et ne crée aucune mission', async () => {
    const { MC, bus } = await ready();
    const before = [...MC.missions.keys()];
    await MC.toggleReplay();
    await flush(8);
    MC.stopReplay();
    await flush();
    expect([...MC.missions.keys()]).toEqual(before);
    // Les seuls appels tolérés sont des lectures, et uniquement sur les
    // routes de consultation.
    bus.calls.get.forEach((url) => {
      expect(url).toMatch(/^\/api\/missions/);
    });
    expect(bus.calls.get.some((u) => u.includes('/timeline'))).toBe(true);
  });

  it('ne modifie pas la mission relue', async () => {
    const { MC } = await ready();
    const before = JSON.stringify(MC.missions.get('task_r'));
    await MC.toggleReplay();
    await flush(6);
    MC.replay.index = TIMELINE.length;
    MC.render();
    MC.stopReplay();
    await flush();
    expect(JSON.stringify(MC.missions.get('task_r'))).toBe(before);
  });
});

describe('MODE COMPACT', () => {
  it('bascule FULL → COMPACT', async () => {
    const { MC } = await ready();
    expect(MC.compact).toBe(false);
    MC.toggleCompact();
    await flush();
    expect(MC.compact).toBe(true);
    expect($('#v5Mission').dataset.compact).toBe('1');
    expect(document.documentElement.dataset.mcCompact).toBe('1');
  });

  it('garde agent, mission, statut, outil et durée', async () => {
    const withTool = mission({
      ...M,
      last_tool: { id: 'marketplace.search', name: 'Recherche', ok: true, duration_ms: 1420, args: {} },
    });
    const h = await mount({
      ...ROUTES,
      '/api/missions': snapshot([withTool]),
      '/api/missions/history': history([withTool]),
    });
    h.MC.show();
    await flush(8);
    h.MC.toggleCompact();
    await flush(8);
    expect($('#mcAgent').textContent).toBe('BrainrotFortniteAgent');
    expect($('#mcName').textContent).toContain('Analyse du marketplace');
    expect($('#mcStateLabel').textContent).toBe('TERMINÉ');
    expect($('#mcFactTool b').textContent).toBe('marketplace.search');
    expect($('#mcDuration').textContent).toMatch(/^\d{2}:\d{2}/);
  });

  it('revient en FULL sans perdre la mission affichée', async () => {
    const { MC } = await ready();
    MC.selectMission('task_r');
    await flush();
    const before = $('#mcName').textContent;
    MC.toggleCompact();
    await flush();
    MC.toggleCompact();
    await flush();
    expect(MC.compact).toBe(false);
    expect($('#v5Mission').dataset.compact).toBe('');
    expect($('#mcName').textContent).toBe(before);
    expect(MC.active().id).toBe('task_r');
  });

  it('referme l’inspecteur en passant en compact', async () => {
    const h = await mount({
      ...ROUTES,
      '/api/missions': snapshot([mission({ ...M, history: [{ ts: 1, kind: 'tool', label: 'T', tool: 'x.y', args: { a: 1 } }] })]),
    });
    h.MC.show();
    await flush();
    $('#mcHist .mc-hrow[data-log-index]').click();
    await flush();
    expect($('#mcInspect').hidden).toBe(false);
    h.MC.toggleCompact();
    await flush();
    expect($('#mcInspect').hidden).toBe(true);
  });

  it('arrête le replay quand on ferme le panneau', async () => {
    const { MC } = await ready();
    await MC.toggleReplay();
    await flush();
    MC.hide();
    await flush();
    expect(MC.replay).toBeNull();
  });
});
