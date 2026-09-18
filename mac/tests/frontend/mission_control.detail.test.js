/* Timeline, inspecteur d'outil, FILES et missions en échec. */
import { describe, expect, it } from 'vitest';
import { $, $$, flush, healthReport, history, mission, mount, snapshot } from './harness.js';

const STEPS = [
  { key: 'a', label: 'Recherche utilisateur', state: 'done' },
  { key: 'b', label: 'Lecture marketplace', state: 'done' },
  { key: 'c', label: 'Analyse des annonces', state: 'run' },
  { key: 'd', label: 'Génération du rapport', state: 'idle' },
];

/* Historique tel que le publie le backend : les arguments sont DÉJÀ assainis
   par sanitize_args avant d'entrer ici — un secret ne doit donc jamais
   apparaître, ni dans le journal, ni dans l'inspecteur. */
const HISTORY = [
  { ts: 1000, kind: 'mission', label: 'Mission créée' },
  {
    ts: 1001, kind: 'tool', label: 'Recherche marketplace', tool: 'marketplace.search',
    args: { query: 'skins rares', limit: 50, api_key: '•••', token: '•••', password: '•••', secret: '•••' },
  },
  {
    ts: 1003, kind: 'tool_result', label: 'Recherche marketplace', tool: 'marketplace.search',
    ok: true, duration_ms: 1420, preview: '38 annonces récupérées',
  },
  {
    ts: 1005, kind: 'tool', label: 'Lire un fichier', tool: 'fs.read',
    args: { path: '/var/www/annonces.json' },
  },
  {
    ts: 1006, kind: 'tool_result', label: 'Lire un fichier', tool: 'fs.read',
    ok: false, duration_ms: 220, error: 'Fichier introuvable',
  },
];

const FILES = [
  { path: '/var/www/lu.php', action: 'read', ts: 1002 },
  { path: '/var/www/modifie.php', action: 'modified', ts: 1004 },
  { path: '/var/www/cree.php', action: 'created', ts: 1005 },
  { path: '/var/www/supprime.php', action: 'deleted', ts: 1006 },
];

function build(over = {}) {
  const m = mission({
    steps: STEPS, steps_done: 2, steps_total: 4, current_step: STEPS[2],
    history: HISTORY, files: FILES, tool_calls: 2,
    tools: ['marketplace.search', 'fs.read'],
    last_tool: {
      id: 'fs.read', name: 'Lire un fichier', ok: false, duration_ms: 220,
      args: { path: '/var/www/annonces.json' }, preview: '', error: 'Fichier introuvable',
    },
    ...over,
  });
  return {
    m,
    routes: {
      '/api/missions': snapshot([m]),
      '/api/missions/history': history([m]),
      '/api/missions/health': healthReport(),
      [`/api/missions/${m.id}`]: { mission: m },
    },
  };
}

async function open(over) {
  const { m, routes } = build(over);
  const h = await mount(routes);
  h.MC.show();
  await flush();
  return { ...h, m };
}

describe('timeline des étapes', () => {
  it('rend un glyphe par état', async () => {
    await open();
    const glyphs = $$('#mcSteps li').map((li) => `${li.dataset.st}:${li.querySelector('u').textContent}`);
    expect(glyphs).toEqual([
      'done:✓', 'done:✓', 'run:●', 'idle:○',
    ]);
  });

  it('marque l’étape en échec d’une croix', async () => {
    await open({ steps: [{ key: 'a', label: 'Envoi', state: 'err' }] });
    expect($('#mcSteps li').dataset.st).toBe('err');
    expect($('#mcSteps li u').textContent).toBe('×');
  });

  it('n’a qu’une seule étape active', async () => {
    await open();
    expect($$('#mcSteps li[data-st="run"]')).toHaveLength(1);
  });

  it('conserve l’ordre du backend et sa numérotation', async () => {
    await open();
    const labels = $$('#mcSteps li b').map((b) => b.textContent);
    expect(labels).toEqual(STEPS.map((s) => s.label));
    expect($$('#mcSteps li i').map((i) => i.textContent)).toEqual(['01', '02', '03', '04']);
  });

  it('n’invente aucune étape quand le backend n’en publie pas', async () => {
    await open({ steps: [], steps_total: 0, steps_done: 0, current_step: null });
    expect($$('#mcSteps li')).toHaveLength(0);
    expect($('#mcStepsNone').hidden).toBe(false);
  });

  it('affiche le compteur réel d’étapes', async () => {
    await open();
    expect($('#mcStepCount').textContent).toBe('2/4');
  });
});

describe('inspecteur d’outil', () => {
  const rows = () => $$('#mcHist .mc-hrow[data-log-index]');
  /* Le journal va du plus récent au plus ancien : on désigne la ligne voulue
     par son contenu, jamais par sa position. */
  const marketplaceCall = () => rows().find((r) => r.dataset.kind === 'tool'
    && r.textContent.includes('Recherche marketplace'));

  it('ne rend cliquables que les événements d’outil', async () => {
    await open();
    const kinds = new Set(rows().map((r) => r.dataset.kind));
    expect(kinds.has('tool')).toBe(true);
    expect(kinds.has('tool_result')).toBe(true);
    expect(kinds.has('mission')).toBe(false);
  });

  it('reste fermé tant qu’on ne clique pas', async () => {
    await open();
    expect($('#mcInspect').hidden).toBe(true);
  });

  it('affiche outil, agent, heure, durée et statut', async () => {
    await open();
    rows().find((r) => r.dataset.kind === 'tool_result').click();
    await flush();
    expect($('#mcInspect').hidden).toBe(false);
    const labels = $$('#mcInspectBody .mci-grid dt').map((d) => d.textContent);
    expect(labels).toEqual(['Outil', 'Agent', 'Heure', 'Durée', 'Statut']);
    const values = $$('#mcInspectBody .mci-grid dd').map((d) => d.textContent);
    expect(values[1]).toBe('BrainrotFortniteAgent');
    expect(values[3]).toContain('ms');
  });

  it('affiche les arguments, le résultat et l’erreur', async () => {
    await open();
    marketplaceCall().click();
    await flush();
    const sections = $$('#mcInspectBody .mci-sec>span').map((s) => s.textContent);
    expect(sections).toContain('ARGUMENTS ASSAINIS');
    expect(sections).toContain('RÉSULTAT ASSAINI');
    const args = $$('#mcInspectBody .mci-args dt').map((d) => d.textContent);
    expect(args).toContain('query');
  });

  it('montre l’erreur d’un outil en échec', async () => {
    await open();
    const failed = rows().find((r) => r.dataset.tone === 'err');
    failed.click();
    await flush();
    expect($('#mcInspectBody').textContent).toContain('Fichier introuvable');
    expect($$('#mcInspectBody .mci-sec>span').map((s) => s.textContent)).toContain('ERREUR');
  });

  /* Le garde-fou le plus important de tout ce fichier. */
  it('ne laisse JAMAIS passer un secret en clair', async () => {
    await open();
    for (const row of rows()) {
      row.click();
      // eslint-disable-next-line no-await-in-loop
      await flush();
      const shown = $('#mcInspectBody').textContent;
      for (const secret of ['hunter2', 'sk-live', 'Bearer ', 'SECRET']) {
        expect(shown).not.toContain(secret);
      }
      expect(shown).not.toMatch(/api_key\s*[:=]\s*(?!•)/);
    }
    // Les clés restent visibles, seules les VALEURS sont masquées : on doit
    // savoir qu'une clé d'API a été transmise, sans jamais la lire.
    marketplaceCall().click();
    await flush();
    const dts = $$('#mcInspectBody .mci-args dt').map((d) => d.textContent);
    const dds = $$('#mcInspectBody .mci-args dd').map((d) => d.textContent);
    expect(dts).toContain('api_key');
    expect(dds[dts.indexOf('api_key')]).toBe('•••');
    expect(dds[dts.indexOf('password')]).toBe('•••');
    expect(dds[dts.indexOf('token')]).toBe('•••');
    expect(dds[dts.indexOf('secret')]).toBe('•••');
  });

  it('se referme avec Échap sans fermer le panneau', async () => {
    const { MC } = await open();
    $$('#mcHist .mc-hrow[data-log-index]')[0].click();
    await flush();
    expect($('#mcInspect').hidden).toBe(false);
    window.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape' }));
    await flush();
    expect($('#mcInspect').hidden).toBe(true);
    expect(MC.open).toBe(true);
  });
});

describe('FILES', () => {
  it('regroupe par action réellement observée', async () => {
    await open();
    const groups = $$('#mcFiles .mc-fgroup b').map((b) => b.textContent);
    expect(groups).toEqual(expect.arrayContaining(['READ', 'MODIFIED', 'CREATED', 'DELETED']));
  });

  it('n’invente aucune catégorie', async () => {
    await open({ files: [{ path: '/a.php', action: 'read', ts: 1 }] });
    expect($$('#mcFiles .mc-fgroup b').map((b) => b.textContent)).toEqual(['READ']);
  });

  it('affiche le chemin exact et l’horodatage', async () => {
    await open();
    const paths = $$('#mcFiles [data-file-path]').map((b) => b.dataset.filePath);
    expect(paths).toEqual(expect.arrayContaining(FILES.map((f) => f.path)));
    const row = $('[data-file-path="/var/www/lu.php"]');
    expect(row.querySelector('b').textContent).toBe('/var/www/lu.php');
    expect(row.querySelector('em').textContent).toMatch(/\d{2}:\d{2}:\d{2}/);
  });

  it('compte les fichiers touchés', async () => {
    await open();
    expect($('#mcFilesCount').textContent).toBe('4');
  });

  it('dit qu’aucun fichier n’a été touché plutôt que d’afficher un vide', async () => {
    await open({ files: [] });
    expect($('#mcFiles').textContent).toContain('Aucun fichier touché');
  });

  it('n’expose que des métadonnées au clic, sans aucun accès disque', async () => {
    const { bus, fetchCalls } = await open();
    const before = bus.calls.get.length;
    $('[data-file-path="/var/www/modifie.php"]').click();
    await flush();
    const labels = $$('#mcInspectBody .mci-grid dt').map((d) => d.textContent);
    expect(labels).toEqual(['Chemin', 'Action', 'Observé à', 'Mission']);
    expect($('#mcInspectBody').textContent).toContain("ne lit ni n'écrit aucun fichier");
    expect(bus.calls.get.length).toBe(before);       // aucune requête
    expect(bus.calls.write).toHaveLength(0);         // aucune écriture
    expect(fetchCalls).toHaveLength(0);
  });
});

describe('mission en échec', () => {
  const FAILED = {
    state: 'FAILED', active: false, ended_at: 2000, duration: 42,
    error: 'Connexion SSH refusée par le serveur.',
    failed_step: 'Analyse des annonces', failed_tool: 'fs.read',
  };

  it('met l’erreur en évidence avant le contexte', async () => {
    await open(FAILED);
    expect($('#mcFail').hidden).toBe(false);
    expect($('#mcFailMsg').textContent).toBe('Connexion SSH refusée par le serveur.');
  });

  it('montre étape, agent, outil et durée avant erreur', async () => {
    await open(FAILED);
    const dt = $$('#mcFailGrid dt').map((d) => d.textContent);
    const dd = $$('#mcFailGrid dd').map((d) => d.textContent);
    expect(dt).toEqual(['Étape', 'Agent', 'Outil', 'Durée avant erreur']);
    expect(dd[0]).toBe('Analyse des annonces');
    expect(dd[1]).toBe('BrainrotFortniteAgent');
    expect(dd[2]).toBe('fs.read');
    expect(dd[3]).toBe('00:42');
  });

  it('ne montre pas le bloc d’échec pour une mission réussie', async () => {
    await open({ state: 'COMPLETED', active: false, error: '' });
    expect($('#mcFail').hidden).toBe(true);
  });
});

describe('COPIER LE DIAGNOSTIC', () => {
  const DIAG = [
    'JARVIS — DIAGNOSTIC DE MISSION', '',
    'MISSION   : Analyse du marketplace',
    'AGENT     : BrainrotFortniteAgent',
    'STATUT    : FAILED',
    'DURATION  : 00:00:42',
    'STEP      : Analyse des annonces',
    'TOOL      : fs.read', '',
    'ERROR', '  Connexion SSH refusée par le serveur.', '',
    'LAST EVENTS', '  00:01  TOOL  Recherche marketplace — {"api_key": "•••"}',
  ].join('\n');

  async function ready() {
    const { m, routes } = build({ state: 'FAILED', active: false, duration: 42 });
    const h = await mount({ ...routes, [`/api/missions/${m.id}/diagnostic`]: { diagnostic: DIAG, id: m.id } });
    h.MC.show();
    await flush();
    return h;
  }

  it('copie le diagnostic du backend dans le presse-papier', async () => {
    const { MC } = await ready();
    $('#mcCopy').click();
    await flush();
    expect(navigator.clipboard.writeText).toHaveBeenCalledTimes(1);
    const copied = navigator.clipboard.writeText.mock.calls[0][0];
    for (const section of ['MISSION', 'AGENT', 'STATUT', 'DURATION',
      'STEP', 'TOOL', 'ERROR', 'LAST EVENTS']) {
      expect(copied).toContain(section);
    }
    expect(MC.open).toBe(true);
  });

  it('ne copie aucun secret', async () => {
    await ready();
    $('#mcCopy').click();
    await flush();
    const copied = navigator.clipboard.writeText.mock.calls[0][0];
    for (const secret of ['hunter2', 'sk-live', 'Bearer ']) {
      expect(copied).not.toContain(secret);
    }
    expect(copied).toContain('•••');
  });

  it('confirme visuellement la copie', async () => {
    await ready();
    $('#mcCopy').click();
    await flush();
    expect($('#mcCopy').textContent).toContain('COPIÉ');
  });

  it('n’écrit rien côté serveur : copier n’est pas agir', async () => {
    const { bus, fetchCalls } = await ready();
    $('#mcCopy').click();
    await flush();
    expect(bus.calls.write).toHaveLength(0);
    expect(fetchCalls.filter((c) => c.method !== 'GET')).toHaveLength(0);
  });
});
