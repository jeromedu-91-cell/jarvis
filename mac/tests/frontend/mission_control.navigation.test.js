/* LIVE / HISTORIQUE, filtres et recherche locale.
   On teste des COMPORTEMENTS observables (ce qui est listé, ce qui est actif),
   jamais des pixels. */
import { describe, expect, it } from 'vitest';
import { $, $$, flush, history, mission, mount, snapshot, healthReport } from './harness.js';

const LIVE = mission({
  id: 'task_live', name: 'Analyse du marketplace', agent: 'BrainrotFortniteAgent',
  state: 'RUNNING', active: true, tools: ['marketplace.search'],
});
const DONE = mission({
  id: 'task_done', name: 'Audit des prix', agent: 'jarvis', state: 'COMPLETED',
  active: false, ended_at: 1, duration: 42, tools: ['fs.read'],
});
const FAILED = mission({
  id: 'task_failed', name: 'Publication Discord', agent: 'jarvis', state: 'FAILED',
  active: false, ended_at: 1, duration: 7, tools: ['discord.send_message'],
  error: 'Le bot est hors ligne.', failed_tool: 'discord.send_message',
});

function routes(listed) {
  return {
    '/api/missions': snapshot([LIVE, DONE, FAILED]),
    '/api/missions/history': (url) => {
      const status = new URL(url, 'http://x').searchParams.get('status');
      const all = listed || [LIVE, DONE, FAILED];
      const kept = status === 'COMPLETED' ? all.filter((m) => m.state === 'COMPLETED')
        : status === 'FAILED' ? all.filter((m) => m.state === 'FAILED')
          : status === 'RUNNING' ? all.filter((m) => m.active) : all;
      return { ...history(kept), status };
    },
    '/api/missions/health': healthReport(),
  };
}

const names = () => $$('#mcList .mc-row b').map((b) => b.textContent);

describe('LIVE / HISTORIQUE', () => {
  it('démarre sur LIVE avec l’onglet marqué actif', async () => {
    const { MC } = await mount(routes());
    MC.show(); await flush();
    expect(MC.mode).toBe('LIVE');
    expect($('.mc-mode[data-mode="LIVE"]').dataset.on).toBe('1');
    expect($('.mc-mode[data-mode="HISTORY"]').dataset.on).toBeFalsy();
  });

  it('bascule sur HISTORIQUE et déplace l’état actif', async () => {
    const { MC } = await mount(routes());
    MC.show(); await flush();
    MC.setMode('HISTORY'); await flush();
    expect(MC.mode).toBe('HISTORY');
    expect($('.mc-mode[data-mode="HISTORY"]').dataset.on).toBe('1');
    expect($('.mc-mode[data-mode="LIVE"]').dataset.on).toBeFalsy();
  });

  it('ne montre les filtres qu’en HISTORIQUE', async () => {
    const { MC } = await mount(routes());
    MC.show(); await flush();
    MC.setMode('LIVE'); await flush();
    expect($('#mcFilters').hidden).toBe(true);
    MC.setMode('HISTORY'); await flush();
    expect($('#mcFilters').hidden).toBe(false);
  });

  it('LIVE ne liste que les missions réellement actives', async () => {
    const { MC } = await mount(routes());
    MC.show(); await flush();
    MC.setMode('LIVE'); await flush();
    expect(names()).toEqual(['Analyse du marketplace']);
  });

  it('HISTORIQUE liste tout', async () => {
    const { MC } = await mount(routes());
    MC.show(); await flush();
    MC.setMode('HISTORY'); await flush();
    expect(names()).toEqual(expect.arrayContaining(
      ['Analyse du marketplace', 'Audit des prix', 'Publication Discord']));
  });

  it('dit clairement qu’aucune mission ne tourne plutôt que d’afficher le vide', async () => {
    const { MC } = await mount({ ...routes([DONE]), '/api/missions': snapshot([DONE]) });
    MC.show(); await flush();
    MC.setMode('LIVE'); await flush();
    expect($('#mcList').textContent).toContain('Aucune mission en cours');
  });
});

describe('filtres', () => {
  it('demande bien le statut choisi au backend', async () => {
    const { MC, bus } = await mount(routes());
    MC.show(); await flush();
    MC.setMode('HISTORY'); await flush();
    for (const status of ['ALL', 'RUNNING', 'COMPLETED', 'FAILED']) {
      MC.setFilter(status); await flush();
      expect(bus.calls.get.some((u) => u.includes(`status=${status}`))).toBe(true);
    }
  });

  it('marque un seul filtre actif à la fois', async () => {
    const { MC } = await mount(routes());
    MC.show(); await flush();
    MC.setFilter('FAILED'); await flush();
    const on = $$('.mc-filter').filter((b) => b.dataset.on === '1');
    expect(on).toHaveLength(1);
    expect(on[0].dataset.filter).toBe('FAILED');
  });

  it('n’affiche que les missions du filtre', async () => {
    const { MC } = await mount(routes());
    MC.show(); await flush();
    MC.setMode('HISTORY'); await flush();
    MC.setFilter('FAILED'); await flush();
    expect(names()).toEqual(['Publication Discord']);
    MC.setFilter('COMPLETED'); await flush();
    expect(names()).toEqual(['Audit des prix']);
  });

  it('affiche les compteurs réels du backend', async () => {
    const { MC } = await mount(routes());
    MC.show(); await flush();
    MC.setMode('HISTORY'); await flush();
    const counts = {};
    $$('.mc-filter').forEach((b) => { counts[b.dataset.filter] = b.querySelector('em').textContent; });
    expect(counts.ALL).toBe('3');
    expect(counts.COMPLETED).toBe('1');
    expect(counts.FAILED).toBe('1');
  });
});

describe('recherche locale', () => {
  async function ready() {
    const h = await mount(routes());
    h.MC.show(); await flush();
    h.MC.setMode('HISTORY'); await flush();
    return h;
  }

  it('filtre par nom de mission', async () => {
    const { MC } = await ready();
    MC.setQuery('audit'); await flush();
    expect(names()).toEqual(['Audit des prix']);
  });

  it('filtre par agent', async () => {
    const { MC } = await ready();
    MC.setQuery('brainrotfortnite'); await flush();
    expect(names()).toEqual(['Analyse du marketplace']);
  });

  it('filtre par outil', async () => {
    const { MC } = await ready();
    MC.setQuery('discord.send_message'); await flush();
    expect(names()).toEqual(['Publication Discord']);
  });

  it('ignore la casse', async () => {
    const { MC } = await ready();
    MC.setQuery('AUDIT'); await flush();
    expect(names()).toEqual(['Audit des prix']);
  });

  it('annonce proprement une recherche sans résultat', async () => {
    const { MC } = await ready();
    MC.setQuery('zzzz'); await flush();
    expect(names()).toEqual([]);
    expect($('#mcList').textContent).toContain('Aucune mission ne correspond');
    expect($('#mcList').textContent).toContain('zzzz');
  });

  it('ne déclenche aucun appel backend : le filtrage est local', async () => {
    const { MC, bus } = await ready();
    const before = bus.calls.get.length;
    MC.setQuery('audit'); await flush();
    MC.setQuery('discord'); await flush();
    expect(bus.calls.get.length).toBe(before);
  });

  it('vider la recherche rend toute la liste', async () => {
    const { MC } = await ready();
    MC.setQuery('audit'); await flush();
    MC.setQuery(''); await flush();
    expect(names().length).toBe(3);
  });
});
