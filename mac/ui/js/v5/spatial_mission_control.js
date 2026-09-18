/* ==========================================================================
   JARVIS_SPATIAL_OS_V5 — spatial_mission_control.js
   MISSION CONTROL : voir EN DIRECT ce que JARVIS fait, et RELIRE ce qu'il a fait.

   Sources — tout vient du backend, rien n'est fabriqué ici :
   - direct        → SSE mission.started / update / completed / failed
   - hydratation   → GET /api/missions
   - historique    → GET /api/missions/history?status=ALL|RUNNING|COMPLETED|FAILED
   - replay        → GET /api/missions/<id>/timeline   (événements DÉJÀ survenus)
   - diagnostic    → GET /api/missions/<id>/diagnostic (secrets déjà masqués)
   - santé         → GET /api/missions/health          (UNKNOWN si non sondable)

   REPLAY ne réexécute RIEN : il redonne, dans l'ordre et à la vitesse choisie,
   des événements enregistrés. Aucune action réelle n'est émise pendant un replay.

   Le seul élément calculé côté écran est le chronomètre du direct, qui prolonge
   `started_at` entre deux événements — et il se fige dès la fin de la mission.
   ========================================================================== */
(function () {
  'use strict';

  const byId = (id) => document.getElementById(id);

  /* `J` (core.js) est un `const` de portée script : `window.J` n'existe pas.
     Ce test lexical est le seul moyen fiable de savoir si le bus est là. */
  function hasBus() {
    try { return typeof J !== 'undefined' && typeof J.on === 'function'; }
    catch (_) { return false; }
  }
  const api = (path) => (hasBus() ? J.get(path) : fetch(path).then((r) => r.json()));

  const STATE_LABEL = {
    WAITING: 'EN ATTENTE', THINKING: 'RÉFLEXION', RUNNING: 'EN COURS',
    TOOL: 'OUTIL', VERIFYING: 'VÉRIFICATION', COMPLETED: 'TERMINÉ', FAILED: 'ÉCHEC',
  };
  const STATE_TONE = {
    WAITING: 'wait', THINKING: 'think', RUNNING: 'run', TOOL: 'tool',
    VERIFYING: 'check', COMPLETED: 'ok', FAILED: 'err',
  };
  const STEP_LABEL = { done: 'DONE', run: 'RUNNING', err: 'FAILED', idle: 'WAITING' };
  const FILE_LABEL = {
    read: 'lecture', modified: 'modifié', saved: 'enregistré',
    created: 'créé', deleted: 'supprimé',
  };
  const HISTORY_LABEL = {
    mission: 'MISSION', log: 'LOG', tool: 'OUTIL', tool_result: 'RÉSULTAT',
    denied: 'REFUSÉ', waiting: 'ATTENTE',
  };
  const FILTERS = [['ALL', 'TOUT'], ['RUNNING', 'EN COURS'],
    ['COMPLETED', 'TERMINÉ'], ['FAILED', 'ÉCHEC']];
  /* Glyphes de la timeline. Un seul coup d'oeil doit suffire a repérer
     l'étape active : c'est le seul symbole plein et coloré de la colonne. */
  const STEP_GLYPH = { done: '✓', run: '●', err: '×', idle: '○' };
  /* Regroupement des fichiers. On ne déduit rien : seules les actions que le
     backend a réellement publiées apparaissent. */
  const FILE_GROUPS = [['read', 'READ'], ['modified', 'MODIFIED'],
    ['saved', 'MODIFIED'], ['created', 'CREATED'], ['deleted', 'DELETED']];
  const SPEEDS = [1, 2, 4];
  const HEALTH_TONE = { ONLINE: 'ok', OFFLINE: 'err', DEGRADED: 'wait', UNKNOWN: 'unknown' };

  /* Le journal est BORNÉ. Une mission peut produire des milliers d'événements :
     en rendre une ligne par événement ferait fondre l'onglet pour une
     information que personne ne lit. On garde la fenêtre récente, seule utile. */
  const MAX_LOG_ROWS = 80;
  const MAX_LIST_ROWS = 60;
  const MAX_KEPT_HISTORY = 240;
  const HEALTH_INTERVAL = 20000;
  /* Réconciliation périodique avec le backend pendant que le panneau est
     ouvert. Ce n'est pas de la redondance : le bus d'événements LARGUE un
     abonné SSE trop lent (file pleine) sans fermer la connexion HTTP. Le
     navigateur ne voit alors aucune erreur, ne se reconnecte jamais, et
     l'écran reste figé en affichant fièrement « DIRECT ». Une relecture
     régulière de la vérité du backend est le seul garde-fou fiable. */
  const RECONCILE_INTERVAL = 10000;
  const STALE_AFTER = 25000;

  function clock(seconds) {
    const s = Math.max(0, Math.floor(Number(seconds) || 0));
    const pad = (n) => String(n).padStart(2, '0');
    const h = Math.floor(s / 3600);
    return h ? `${pad(h)}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}`
      : `${pad(Math.floor(s / 60))}:${pad(s % 60)}`;
  }
  function offsetClock(ms) {
    const s = Math.floor(Math.max(0, Number(ms) || 0) / 1000);
    return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
  }
  function hhmmss(ts) {
    if (!ts) return '—';
    return new Date(ts * 1000).toLocaleTimeString('fr-FR',
      { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  const MissionControl = {
    open: false,
    missions: new Map(),
    listed: [],
    selected: '',
    pinned: false,
    filter: 'ALL',
    counts: {},
    health: null,
    el: null,
    replay: null,   // {id, entries, index, speed, playing, timer}
    mode: 'LIVE',   // LIVE = missions en cours · HISTORY = archives
    query: '',      // recherche locale (mission / agent / outil)
    compact: false,
    _logEntries: [],

    /* ------------------------------------------------------------- montage */
    init() {
      if (this.el) return this;
      const el = document.createElement('section');
      el.className = 'v5-mc';
      el.id = 'v5Mission';
      el.hidden = true;
      el.innerHTML = `
        <header class="mc-head">
          <span class="mc-kick">MISSION CONTROL</span>
          <h2 id="mcName">Aucune mission</h2>
          <div class="mc-health" id="mcHealth" title="État réel des composants"></div>
          <span class="mc-live" id="mcLive" title="Flux temps réel">DIRECT</span>
          <button class="mc-mini" id="mcCompact" title="Mode compact — garder un œil sur la mission
tout en utilisant JARVIS">▁</button>
          <button class="mc-close" id="mcClose" title="Fermer (Échap)" aria-label="Fermer">✕</button>
        </header>

        <div class="mc-body">
          <aside class="mc-side">
            <div class="mc-modes" id="mcModes">
              <button class="mc-mode" data-mode="LIVE" data-on="1">LIVE</button>
              <button class="mc-mode" data-mode="HISTORY">HISTORIQUE</button>
            </div>
            <div class="mc-search">
              <input id="mcSearch" type="search" autocomplete="off" spellcheck="false"
                     placeholder="mission, agent, outil…" aria-label="Rechercher une mission" />
              <button class="mc-search-x" id="mcSearchClear" hidden title="Effacer">✕</button>
            </div>
            <div class="mc-filters" id="mcFilters" role="tablist"></div>
            <div class="mc-list" id="mcList"></div>
            <div class="mc-listmeta" id="mcListMeta"></div>
          </aside>

          <div class="mc-main" id="mcMain">
            <div class="mc-top">
              <div class="mc-agent">
                <span class="k">AGENT ACTIF</span>
                <b id="mcAgent">—</b>
                <em id="mcNote"></em>
              </div>
              <div class="mc-state" id="mcState" data-tone="wait">
                <i></i><b id="mcStateLabel">—</b>
              </div>
              <div class="mc-timer">
                <span class="k">DURÉE</span>
                <b id="mcDuration">00:00</b>
                <em id="mcStarted">—</em>
              </div>
            </div>

            <!-- Bandeau de lecture rapide : tout ce qu'on veut savoir sans
                 rien chercher — où on en est, avec quel outil, sur quoi. -->
            <div class="mc-facts">
              <div class="mc-fact" id="mcFactStep">
                <span class="k">ÉTAPE</span><b>—</b><em></em>
              </div>
              <div class="mc-fact" id="mcFactTool">
                <span class="k">OUTIL</span><b>—</b><em></em>
              </div>
              <div class="mc-fact" id="mcFactFiles">
                <span class="k">FICHIERS</span><b>0</b><em></em>
              </div>
              <div class="mc-fact wide" id="mcFactLast">
                <span class="k">DERNIER ÉVÉNEMENT</span><b>—</b><em></em>
              </div>
            </div>

            <div class="mc-bar" id="mcBarWrap" hidden>
              <i id="mcBar"></i><span id="mcBarVal"></span>
            </div>

            <div class="mc-fail" id="mcFail" hidden>
              <div class="mc-fail-head">
                <span>ÉCHEC</span>
                <button class="mc-copy" id="mcCopy">COPIER LE DIAGNOSTIC</button>
              </div>
              <!-- L'erreur d'abord, en grand : c'est la seule chose qu'on
                   cherche quand une mission a échoué. Le contexte vient après. -->
              <p class="mc-fail-msg" id="mcFailMsg"></p>
              <dl class="mc-fail-grid" id="mcFailGrid"></dl>
            </div>

            <div class="mc-cols">
              <section class="mc-steps-zone">
                <div class="mc-label">ÉTAPES <em id="mcStepCount"></em></div>
                <ol class="mc-steps" id="mcSteps"></ol>
                <p class="mc-none" id="mcStepsNone">Aucune étape déclarée pour l'instant.
                  Elles apparaissent à mesure que JARVIS les exécute.</p>
              </section>

              <section class="mc-detail">
                <div class="mc-label">OUTIL</div>
                <div class="mc-tool" id="mcTool"></div>
                <div class="mc-label">FILES <em id="mcFilesCount"></em></div>
                <div class="mc-files" id="mcFiles"></div>
                <div class="mc-label">SORTIE</div>
                <div class="mc-out" id="mcOut"></div>
              </section>
            </div>

            <section class="mc-hist-zone">
              <div class="mc-label">
                <span id="mcHistTitle">HISTORIQUE</span>
                <em id="mcStepsMeta"></em>
                <div class="mc-replay" id="mcReplay">
                  <button class="mc-rbtn" id="mcReplayToggle"
                          title="Rejouer visuellement la mission — aucune action n'est réexécutée">▶ REPLAY</button>
                  <span class="mc-speeds" id="mcSpeeds"></span>
                  <span class="mc-rpos" id="mcReplayPos"></span>
                  <button class="mc-rbtn" id="mcReplayStop" hidden>■ STOP</button>
                </div>
              </div>
              <!-- Barre temporelle du replay : position dans la mission, pas
                   une progression décorative. -->
              <div class="mc-rtrack" id="mcReplayTrack" hidden>
                <div class="mc-rline"><i id="mcReplayFill"></i></div>
                <span class="mc-rnow" id="mcReplayNow"></span>
              </div>
              <div class="mc-hist" id="mcHist"></div>
            </section>
          </div>

          <p class="mc-empty" id="mcEmpty">Aucune mission.
            Demande quelque chose à JARVIS : la mission s'affichera ici en direct.</p>
        </div>

        <!-- Inspecteur d'outil : lecture seule, ouvert au clic sur un
             événement. Il ne montre que ce que le backend a publié. -->
        <aside class="mc-inspect" id="mcInspect" hidden>
          <header>
            <span>INSPECTEUR OUTIL</span>
            <button class="mc-close" id="mcInspectClose" title="Fermer">✕</button>
          </header>
          <div class="mc-inspect-body" id="mcInspectBody"></div>
        </aside>`;
      document.body.appendChild(el);
      this.el = el;

      byId('mcClose').addEventListener('click', () => this.hide());
      byId('mcCopy').addEventListener('click', () => this.copyDiagnostic());
      byId('mcReplayToggle').addEventListener('click', () => this.toggleReplay());
      byId('mcReplayStop').addEventListener('click', () => this.stopReplay());

      const filters = byId('mcFilters');
      FILTERS.forEach(([key, label]) => {
        const b = document.createElement('button');
        b.className = 'mc-filter';
        b.dataset.filter = key;
        b.setAttribute('role', 'tab');
        b.innerHTML = `<b></b><em></em>`;
        b.querySelector('b').textContent = label;
        if (key === this.filter) b.dataset.on = '1';
        b.addEventListener('click', () => this.setFilter(key));
        filters.appendChild(b);
      });

      const speeds = byId('mcSpeeds');
      SPEEDS.forEach((mult) => {
        const b = document.createElement('button');
        b.className = 'mc-speed';
        b.dataset.speed = String(mult);
        b.textContent = `${mult}x`;
        if (mult === 1) b.dataset.on = '1';
        b.addEventListener('click', () => this.setSpeed(mult));
        speeds.appendChild(b);
      });

      byId('mcList').addEventListener('click', (e) => {
        const row = e.target.closest('[data-mission]');
        if (row) this.selectMission(row.dataset.mission);
      });

      byId('mcCompact').addEventListener('click', () => this.toggleCompact());
      byId('mcInspectClose').addEventListener('click', () => this.closeInspector());

      byId('mcModes').addEventListener('click', (e) => {
        const b = e.target.closest('[data-mode]');
        if (b) this.setMode(b.dataset.mode);
      });

      const search = byId('mcSearch');
      search.addEventListener('input', () => this.setQuery(search.value));
      byId('mcSearchClear').addEventListener('click', () => {
        search.value = ''; this.setQuery(''); search.focus();
      });

      // Clic sur un événement d'outil → inspecteur. Les autres lignes ne sont
      // pas cliquables : il n'y aurait rien de plus à montrer.
      byId('mcHist').addEventListener('click', (e) => {
        const row = e.target.closest('[data-log-index]');
        if (row) this.openInspector(Number(row.dataset.logIndex));
      });
      // Un fichier n'ouvre que ses métadonnées DÉJÀ connues. Aucune lecture,
      // aucune écriture : Mission Control observe, il n'agit pas.
      byId('mcFiles').addEventListener('click', (e) => {
        const row = e.target.closest('[data-file-path]');
        if (row) this.openFileInfo(row.dataset.filePath);
      });

      addEventListener('keydown', (e) => this.onKey(e));
      return this;
    },

    /* Clavier. Ctrl+M (ou Cmd+M) ouvre et ferme Mission Control depuis
       n'importe où — sauf pendant la saisie d'un texte, où l'opérateur écrit
       et n'attend pas qu'un panneau surgisse. Échap referme d'abord
       l'inspecteur, puis le panneau. */
    onKey(e) {
      const target = e.target;
      const typing = target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA'
        || target.isContentEditable);
      if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey
          && (e.key === 'm' || e.key === 'M')) {
        if (typing) return;
        e.preventDefault();
        this.toggle();
        return;
      }
      if (e.key !== 'Escape' || !this.open) return;
      // Échap dans le champ de recherche : on vide, on ne ferme pas.
      if (target === byId('mcSearch') && this.query) { this.setQuery(''); target.value = ''; return; }
      if (!byId('mcInspect').hidden) { this.closeInspector(); return; }
      this.hide();
    },

    /* --------------------------------------------------------- ouverture */
    show() {
      if (!this.el) this.init();
      this.open = true;
      this.el.hidden = false;
      document.documentElement.dataset.mcOpen = '1';
      requestAnimationFrame(() => this.el.classList.add('in'));
      this.hydrate();
      this.loadHistory();
      this.loadHealth();
      this.startTicker();
      clearInterval(this._healthTimer);
      this._healthTimer = setInterval(() => this.loadHealth(), HEALTH_INTERVAL);
      clearInterval(this._syncTimer);
      this._syncTimer = setInterval(() => this.reconcile(), RECONCILE_INTERVAL);
    },
    hide() {
      if (!this.el) return;
      this.open = false;
      document.documentElement.dataset.mcOpen = '';
      this.stopReplay();
      this.el.classList.remove('in');
      clearInterval(this._tick); this._tick = null;
      clearInterval(this._healthTimer); this._healthTimer = null;
      clearInterval(this._syncTimer); this._syncTimer = null;
      setTimeout(() => { if (!this.open) this.el.hidden = true; }, 300);
    },
    toggle() { this.open ? this.hide() : this.show(); },

    /* Mode compact : Mission Control reste visible pendant qu'on travaille
       ailleurs dans JARVIS. Même données, surface minimale. */
    toggleCompact() {
      this.compact = !this.compact;
      this.el.dataset.compact = this.compact ? '1' : '';
      document.documentElement.dataset.mcCompact = this.compact ? '1' : '';
      byId('mcCompact').textContent = this.compact ? '▣' : '▁';
      byId('mcCompact').title = this.compact
        ? 'Revenir à la vue complète'
        : 'Mode compact — garder un œil sur la mission tout en utilisant JARVIS';
      if (this.compact) this.closeInspector();
      this.schedule();
    },

    startTicker() {
      clearInterval(this._tick);
      this._tick = setInterval(() => {
        if (this.replay) return;            // en replay, l'horloge est celle du replay
        const m = this.active();
        if (!m || !m.active) return;
        const el = byId('mcDuration');
        if (el) el.textContent = clock((Date.now() - (m.started_at || m.created_at) * 1000) / 1000);
      }, 1000);
    },

    /* ------------------------------------------------------- alimentation */
    async hydrate() {
      const res = await api('/api/missions');
      if (!res || res.ok === false) {
        this.setLive(false, (res && res.error) ? String(res.error).slice(0, 80) : 'backend injoignable');
        return;
      }
      const data = res.data || res;
      [...(data.recent || []), ...(data.live || [])].forEach((m) => this.missions.set(m.id, m));
      if (data.current) this.missions.set(data.current.id, data.current);
      this.setLive(true);
      this.schedule();
    },

    async loadHistory() {
      // Deux requêtes peuvent être en vol (on clique vite d'un filtre à
      // l'autre). Sans ce jeton, une réponse LENTE arrivée après une plus
      // récente écrasait la liste et affichait le mauvais filtre.
      const seq = (this._histSeq = (this._histSeq || 0) + 1);
      const res = await api(
        `/api/missions/history?status=${encodeURIComponent(this.filter)}&limit=${MAX_LIST_ROWS}`);
      if (seq !== this._histSeq) return;      // réponse périmée : on la jette
      if (!res || res.ok === false) return;
      const data = res.data || res;
      this.counts = data.counts || {};
      this.listed = data.missions || [];
      // L'historique complète la mémoire : une mission d'hier doit pouvoir
      // être ouverte, pas seulement celles vues depuis l'ouverture du panneau.
      this.listed.forEach((m) => { if (!this.missions.has(m.id)) this.missions.set(m.id, m); });
      this.schedule();
    },

    async loadHealth() {
      const res = await api('/api/missions/health');
      this.health = (!res || res.ok === false) ? null : (res.data || res);
      this.renderHealth();
    },

    /* Resynchronisation : on relit l'état réel plutôt que de faire confiance
       à un flux qui a pu mourir en silence. Pendant un REPLAY on ne touche à
       rien — l'écran raconte alors le passé, pas le présent. */
    async reconcile() {
      if (!this.open || this.replay) return;
      await this.hydrate();
      await this.loadHistory();
      const silence = Date.now() - (this._lastEventAt || 0);
      const waiting = [...this.missions.values()].some((m) => m.active);
      // « DIRECT » ne doit se dire que si le direct fonctionne vraiment.
      if (waiting && silence > STALE_AFTER) this.setLive(false, 'flux muet — resynchronisation');
      else this.setLive(true);
    },

    ingest(mission) {
      if (!mission || !mission.id) return;
      this._lastEventAt = Date.now();
      const prev = this.missions.get(mission.id);
      if (prev && prev.history && mission.history && prev.history.length > mission.history.length) {
        const known = new Set(prev.history.map((h) => h.ts + '|' + h.label));
        mission.history = prev.history.concat(mission.history.filter((h) => !known.has(h.ts + '|' + h.label)));
      }
      // Borne dure : sans elle, une mission longue ferait croître la mémoire
      // de l'onglet indéfiniment, un événement après l'autre.
      if (mission.history && mission.history.length > MAX_KEPT_HISTORY) {
        mission.history = mission.history.slice(-MAX_KEPT_HISTORY);
      }
      const known = this.missions.has(mission.id);
      this.missions.set(mission.id, mission);
      if (!this.pinned && mission.active && !this.replay) this.selected = mission.id;
      // Naissance ou fin de mission = les compteurs changent. Les mises à jour
      // intermédiaires, elles, ne rechargent pas la liste.
      if (!known || !mission.active) this.debouncedHistory();
      this.schedule();
      this.badge();
    },

    debouncedHistory() {
      clearTimeout(this._histT);
      this._histT = setTimeout(() => { if (this.open) this.loadHistory(); }, 400);
    },

    /* Un seul rendu par frame : une rafale d'événements ne doit pas déclencher
       une rafale de reflows.

       Le garde-fou `setTimeout` n'est pas décoratif : quand la fenêtre passe
       derrière une autre, le navigateur arrête d'appeler requestAnimationFrame.
       Sans lui, un clic sur un filtre ou une mission ne redessinait rien tant
       que la page ne repeignait pas — l'écran restait sur la mission
       précédente alors que l'état interne, lui, avait bien changé. */
    schedule() {
      if (!this.open || this._pendingRender) return;
      // Le drapeau est posé AVANT d'armer le rendu : si l'ordonnanceur
      // rappelle immédiatement (cas synchrone), `run` voit un rendu en
      // attente et s'exécute, au lieu de sortir sur un drapeau pas encore
      // affecté — ce qui gelait ensuite tous les rendus suivants.
      this._pendingRender = true;
      const run = () => {
        if (!this._pendingRender) return;
        this._pendingRender = false;
        clearTimeout(this._rafT);
        this.render();
      };
      requestAnimationFrame(run);
      this._rafT = setTimeout(run, 250);
    },

    active() {
      if (this.selected && this.missions.has(this.selected)) return this.missions.get(this.selected);
      const all = [...this.missions.values()];
      const live = all.filter((m) => m.active).sort((a, b) => (a.created_at || 0) - (b.created_at || 0));
      if (live.length) return live[live.length - 1];
      return all.sort((a, b) => (a.created_at || 0) - (b.created_at || 0)).pop() || null;
    },

    async selectMission(id) {
      this.stopReplay();
      if (this.pinned && this.selected === id) { this.pinned = false; this.selected = ''; }
      else { this.pinned = true; this.selected = id; }
      if (this.pinned) {
        // Le détail complet (timeline, fichiers, étapes) n'est pas dans la
        // liste : on va le chercher, sinon on afficherait une coquille vide.
        const res = await api(`/api/missions/${encodeURIComponent(id)}`);
        const mission = res && (res.data || res).mission;
        if (mission) this.missions.set(mission.id, mission);
      }
      this.schedule();
    },

    setFilter(key) {
      this.filter = key;
      [...byId('mcFilters').children].forEach((b) => {
        b.dataset.on = b.dataset.filter === key ? '1' : '';
      });
      this.loadHistory();
    },

    setLive(ok, why) {
      const el = byId('mcLive');
      if (!el) return;
      el.dataset.off = ok ? '' : '1';
      el.textContent = ok ? 'DIRECT' : 'HORS LIGNE';
      el.title = ok ? 'Flux temps réel actif' : ('Flux interrompu : ' + (why || 'reconnexion…'));
    },

    /* ------------------------------------------------------------- REPLAY */
    /* Relecture pure : on réaffiche des événements déjà survenus. Aucun POST,
       aucune commande, rien n'est réexécuté — c'est une lecture, pas un rejeu. */
    async toggleReplay() {
      if (this.replay) { this.replay.playing ? this.pauseReplay() : this.resumeReplay(); return; }
      const mission = this.active();
      if (!mission) return;
      const res = await api(`/api/missions/${encodeURIComponent(mission.id)}/timeline`);
      const entries = (res && (res.data || res).timeline) || [];
      if (!entries.length) {
        window.toast?.('Aucun événement enregistré pour cette mission.');
        return;
      }
      const speed = Number((byId('mcSpeeds').querySelector('[data-on="1"]') || {}).dataset?.speed) || 1;
      this.replay = { id: mission.id, entries, index: 0, speed, playing: true, timer: null };
      this.el.dataset.replay = '1';
      byId('mcReplayStop').hidden = false;
      this.stepReplay();
    },

    stepReplay() {
      const r = this.replay;
      if (!r || !r.playing) return;
      if (r.index >= r.entries.length) { this.pauseReplay(); return; }
      r.index += 1;
      this.render();
      if (r.index >= r.entries.length) { this.pauseReplay(); return; }
      // La cadence suit les VRAIS écarts entre événements, divisés par la
      // vitesse choisie — on ne fabrique pas un rythme décoratif.
      const prev = r.entries[r.index - 1].offset_ms || 0;
      const next = r.entries[r.index].offset_ms || 0;
      const wait = Math.max(120, Math.min(4000, next - prev)) / r.speed;
      r.timer = setTimeout(() => this.stepReplay(), wait);
    },
    pauseReplay() {
      if (!this.replay) return;
      this.replay.playing = false;
      clearTimeout(this.replay.timer);
      this.render();
    },
    resumeReplay() {
      if (!this.replay) return;
      if (this.replay.index >= this.replay.entries.length) this.replay.index = 0;
      this.replay.playing = true;
      this.stepReplay();
    },
    stopReplay() {
      if (!this.replay) return;
      clearTimeout(this.replay.timer);
      this.replay = null;
      if (this.el) {
        this.el.dataset.replay = '';
        const stop = byId('mcReplayStop');
        if (stop) stop.hidden = true;
      }
      this.schedule();
    },
    setSpeed(mult) {
      [...byId('mcSpeeds').children].forEach((b) => {
        b.dataset.on = Number(b.dataset.speed) === mult ? '1' : '';
      });
      if (this.replay) this.replay.speed = mult;
    },

    /* --------------------------------------------------------- diagnostic */
    async copyDiagnostic() {
      const mission = this.active();
      if (!mission) return;
      const btn = byId('mcCopy');
      const res = await api(`/api/missions/${encodeURIComponent(mission.id)}/diagnostic`);
      const text = res && (res.data || res).diagnostic;
      if (!text) {
        btn.textContent = 'INDISPONIBLE';
        setTimeout(() => { btn.textContent = 'COPIER LE DIAGNOSTIC'; }, 1800);
        return;
      }
      let ok = false;
      try { await navigator.clipboard.writeText(text); ok = true; } catch (_) { ok = false; }
      if (!ok) {
        // Presse-papier refusé (contexte non sécurisé, permission) : on ne
        // perd pas le diagnostic, on l'offre dans une zone sélectionnable.
        const box = document.createElement('textarea');
        box.className = 'mc-copybox';
        box.value = text;
        byId('mcFail').appendChild(box);
        box.select();
        try { ok = document.execCommand('copy'); } catch (_) { ok = false; }
        setTimeout(() => box.remove(), ok ? 200 : 20000);
      }
      btn.textContent = ok ? 'COPIÉ ✓' : 'SÉLECTIONNE ET COPIE';
      setTimeout(() => { btn.textContent = 'COPIER LE DIAGNOSTIC'; }, 2200);
    },

    /* --------------------------------------------------------------- rendu */
    render() {
      if (!this.el) return;
      const m = this.active();
      const empty = byId('mcEmpty');
      const main = byId('mcMain');
      this.renderList(m);
      this.renderFilters();
      if (!m) {
        empty.hidden = false; main.hidden = true;
        byId('mcName').textContent = 'Aucune mission';
        return;
      }
      empty.hidden = true; main.hidden = false;

      const r = this.replay;
      const view = r ? this.replayView(m) : m;

      byId('mcName').textContent = (r ? 'REPLAY · ' : '') + (m.name || 'Mission');
      byId('mcAgent').textContent = view.agent || '—';
      byId('mcNote').textContent = view.note || '';

      const state = byId('mcState');
      state.dataset.tone = STATE_TONE[view.state] || 'wait';
      state.dataset.busy = (!r && view.active) ? '1' : '';
      byId('mcStateLabel').textContent = STATE_LABEL[view.state] || view.state || '—';

      byId('mcDuration').textContent = clock(view.duration);
      byId('mcStarted').textContent = r
        ? `${r.index}/${r.entries.length} événements`
        : 'début ' + hhmmss(m.started_at || m.created_at);

      const barWrap = byId('mcBarWrap');
      const progress = r ? (r.entries.length ? r.index / r.entries.length : 0) : view.progress;
      if (typeof progress === 'number') {
        const pct = Math.round(Math.max(0, Math.min(1, progress)) * 100);
        barWrap.hidden = false;
        byId('mcBar').style.width = pct + '%';
        byId('mcBarVal').textContent = pct + '%';
      } else { barWrap.hidden = true; }

      this.renderSteps(view);
      this.renderTool(view);
      this.renderFiles(view);
      this.renderOut(view);
      this.renderFailure(m);
      this.renderLog(m);
      this.renderFacts(view, m);
      this.renderReplayTrack();
      byId('mcStepsMeta').textContent = `${m.tool_calls || 0} appel(s) d'outil`;
      byId('mcHistTitle').textContent = r ? 'REPLAY' : 'HISTORIQUE';
      byId('mcReplayToggle').textContent = r ? (r.playing ? '❚❚ PAUSE' : '▶ REPRENDRE') : '▶ REPLAY';
      byId('mcReplayPos').textContent = r ? offsetClock(this.replayCursor()) : '';
    },

    replayCursor() {
      const r = this.replay;
      if (!r || !r.index) return 0;
      return r.entries[Math.min(r.index, r.entries.length) - 1].offset_ms || 0;
    },

    /* Reconstruit l'état de la mission tel qu'il était au curseur du replay,
       uniquement à partir des événements enregistrés. */
    replayView(mission) {
      const r = this.replay;
      const view = {
        agent: mission.agent, note: '', state: 'WAITING', duration: this.replayCursor() / 1000,
        steps: (mission.steps || []).map((s) => ({ ...s, state: 'idle' })),
        tool: null, last_tool: null, files: [], result: '', error: '',
        progress: null, active: true, tool_calls: 0,
      };
      let stepCursor = 0;
      r.entries.slice(0, r.index).forEach((e) => {
        if (e.kind === 'tool') {
          view.tool = { id: e.tool || e.label, name: e.label, args: {} };
          view.state = 'TOOL';
          view.tool_calls += 1;
        } else if (e.kind === 'tool_result') {
          view.last_tool = {
            id: e.tool || e.label, name: e.label, ok: e.ok !== false,
            duration_ms: e.duration_ms || 0, args: {},
            preview: e.ok === false ? '' : (e.detail || ''),
            error: e.ok === false ? (e.detail || '') : '',
          };
          view.tool = null;
          view.state = 'RUNNING';
          // Une étape avance quand un outil se termine : c'est le seul repère
          // honnête dont dispose la relecture.
          if (view.steps[stepCursor]) view.steps[stepCursor].state = e.ok === false ? 'err' : 'done';
          stepCursor = Math.min(stepCursor + 1, Math.max(0, view.steps.length - 1));
        } else if (e.kind === 'waiting') {
          view.state = 'WAITING'; view.note = e.label || '';
        } else if (e.kind === 'mission') {
          if (/termin/i.test(e.label || '')) { view.state = 'COMPLETED'; view.active = false; }
          else if (/échec|erreur|refus/i.test(e.label || '')) { view.state = 'FAILED'; view.active = false; }
          else view.state = 'RUNNING';
        } else if (e.kind === 'log' && view.state === 'WAITING') {
          view.state = 'RUNNING';
        }
      });
      if (r.index >= r.entries.length) {
        // Arrivé au bout, l'état affiché est le VERDICT réel de la mission.
        view.state = mission.state;
        view.active = false;
        view.error = mission.error || '';
        view.result = mission.result || '';
        view.steps = mission.steps || view.steps;
        view.files = mission.files || [];
        view.duration = mission.duration;
      }
      return view;
    },

    /* ---------------------------------------------- bandeau de lecture rapide */
    /* Les quatre faits qu'on veut sans chercher : où on en est, avec quel
       outil, sur combien de fichiers, et ce qui vient de se passer. Chaque
       case reste vide (« — ») tant que le backend n'a rien publié. */
    renderFacts(view, mission) {
      const steps = view.steps || [];
      const done = steps.filter((x) => x.state === 'done' || x.state === 'err').length;
      const current = steps.find((x) => x.state === 'run')
        || [...steps].reverse().find((x) => x.state === 'done' || x.state === 'err');
      const stepIndex = current ? steps.indexOf(current) + 1 : done;

      this.fact('mcFactStep',
        steps.length ? `Étape ${stepIndex} / ${steps.length}` : '—',
        current ? (current.label || '') : 'aucune étape déclarée');

      const tool = view.tool || view.last_tool;
      this.fact('mcFactTool',
        tool ? (tool.id || tool.name || '—') : '—',
        view.tool ? 'en cours…'
          : (tool ? `${tool.ok === false ? 'échec' : 'ok'} · ${tool.duration_ms || 0} ms` : 'aucun appel'));

      const files = view.files || [];
      this.fact('mcFactFiles', String(files.length),
        files.length ? (files[files.length - 1].path || '').split(/[\\/]/).pop() : 'aucun fichier');

      const last = (mission.history || [])[(mission.history || []).length - 1];
      this.fact('mcFactLast', last ? (last.label || '—') : '—',
        last ? `${HISTORY_LABEL[last.kind] || ''} · ${hhmmss(last.ts)}` : '');
    },

    fact(id, value, detail) {
      const host = byId(id);
      if (!host) return;
      host.querySelector('b').textContent = value;
      host.querySelector('em').textContent = detail || '';
      host.title = detail ? `${value} — ${detail}` : String(value);
    },

    /* ------------------------------------------------------ barre de replay */
    renderReplayTrack() {
      const track = byId('mcReplayTrack');
      const r = this.replay;
      if (!r) { track.hidden = true; return; }
      track.hidden = false;
      const total = r.entries[r.entries.length - 1].offset_ms || 1;
      const pct = Math.max(0, Math.min(100, (this.replayCursor() / total) * 100));
      byId('mcReplayFill').style.width = pct + '%';
      const entry = r.entries[Math.max(0, r.index - 1)];
      byId('mcReplayNow').textContent = entry
        ? `${offsetClock(entry.offset_ms)} · ${HISTORY_LABEL[entry.kind] || entry.kind} · ${entry.label || ''}`
        : '';
    },

    /* -------------------------------------------------- inspecteur d'outil */
    /* Lecture seule. Tout ce qui s'affiche ici a déjà traversé `sanitize_args`
       côté backend : les secrets sont masqués avant d'entrer dans l'historique,
       pas au moment de l'affichage. */
    openInspector(index) {
      const entry = this._logEntries[index];
      if (!entry) return;
      const mission = this.active() || {};
      const host = byId('mcInspectBody');
      host.replaceChildren();

      const args = this.entryArgs(entry);
      const ok = entry.ok;
      const rows = [
        ['Outil', entry.tool || entry.label || '—'],
        ['Agent', mission.agent || '—'],
        ['Heure', entry.ts ? hhmmss(entry.ts) : (entry.offset_ms != null ? '+' + offsetClock(entry.offset_ms) : '—')],
        ['Durée', entry.duration_ms ? `${entry.duration_ms} ms` : '—'],
        ['Statut', ok === undefined || ok === null ? 'appel' : (ok ? 'ok' : 'échec')],
      ];
      const dl = document.createElement('dl');
      dl.className = 'mci-grid';
      rows.forEach(([k, v]) => {
        const dt = document.createElement('dt'); dt.textContent = k;
        const dd = document.createElement('dd'); dd.textContent = v;
        dl.append(dt, dd);
      });
      host.appendChild(dl);

      const argKeys = Object.keys(args || {});
      host.appendChild(this.inspectSection('ARGUMENTS ASSAINIS',
        argKeys.length ? args : null, 'aucun paramètre publié'));
      const result = entry.preview || (ok === false ? '' : entry.detail) || '';
      host.appendChild(this.inspectSection('RÉSULTAT ASSAINI', result || null,
        'aucun résultat publié'));
      const error = entry.error || (ok === false ? entry.detail : '') || '';
      if (error) host.appendChild(this.inspectSection('ERREUR', error, '', 'err'));

      byId('mcInspect').hidden = false;
      this.el.dataset.inspect = '1';
    },

    inspectSection(title, value, emptyLabel, tone) {
      const box = document.createElement('section');
      box.className = 'mci-sec';
      if (tone) box.dataset.tone = tone;
      const h = document.createElement('span');
      h.textContent = title;
      box.appendChild(h);
      if (value && typeof value === 'object') {
        const dl = document.createElement('dl');
        dl.className = 'mci-args';
        Object.keys(value).forEach((k) => {
          const dt = document.createElement('dt'); dt.textContent = k;
          const dd = document.createElement('dd');
          dd.textContent = typeof value[k] === 'string' ? value[k] : JSON.stringify(value[k]);
          dl.append(dt, dd);
        });
        box.appendChild(dl);
      } else if (value) {
        const pre = document.createElement('pre');
        pre.textContent = String(value);
        box.appendChild(pre);
      } else {
        const p = document.createElement('p');
        p.className = 'mc-none';
        p.textContent = emptyLabel;
        box.appendChild(p);
      }
      return box;
    },

    /* Les arguments arrivent soit tels quels (mission vivante), soit encodés
       dans `detail` par la persistance. On lit les deux, sans jamais inventer. */
    entryArgs(entry) {
      if (entry.args && typeof entry.args === 'object') return entry.args;
      const raw = entry.detail;
      if (typeof raw === 'string' && raw.startsWith('{')) {
        try {
          const parsed = JSON.parse(raw);
          if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) return parsed;
        } catch (_) { /* ce n'était pas des arguments */ }
      }
      return {};
    },

    closeInspector() {
      byId('mcInspect').hidden = true;
      if (this.el) this.el.dataset.inspect = '';
    },

    /* Un fichier n'expose que ce que Mission Control sait déjà de lui : son
       chemin, l'action observée, l'instant. Aucune lecture du disque. */
    openFileInfo(path) {
      const mission = this.active() || {};
      const file = (mission.files || []).find((f) => f.path === path);
      if (!file) return;
      const host = byId('mcInspectBody');
      host.replaceChildren();
      const dl = document.createElement('dl');
      dl.className = 'mci-grid';
      [['Chemin', file.path], ['Action', FILE_LABEL[file.action] || file.action || '—'],
        ['Observé à', file.ts ? hhmmss(file.ts) : '—'], ['Mission', mission.name || '—']]
        .forEach(([k, v]) => {
          const dt = document.createElement('dt'); dt.textContent = k;
          const dd = document.createElement('dd'); dd.textContent = v;
          dl.append(dt, dd);
        });
      host.appendChild(dl);
      const note = document.createElement('p');
      note.className = 'mc-none';
      note.textContent = "Métadonnées observées uniquement — Mission Control ne lit ni n'écrit aucun fichier.";
      host.appendChild(note);
      byId('mcInspect').hidden = false;
      this.el.dataset.inspect = '1';
    },

    /* ------------------------------------------------ LIVE / HISTORIQUE */
    setMode(mode) {
      this.mode = mode === 'HISTORY' ? 'HISTORY' : 'LIVE';
      [...byId('mcModes').children].forEach((b) => {
        b.dataset.on = b.dataset.mode === this.mode ? '1' : '';
      });
      // LIVE = ce qui tourne maintenant ; HISTORIQUE = tout le reste, avec
      // ses filtres. On ne change pas le filtre choisi par l'opérateur.
      byId('mcFilters').hidden = this.mode === 'LIVE';
      this.loadHistory();
    },

    setQuery(value) {
      this.query = String(value || '').trim().toLowerCase();
      byId('mcSearchClear').hidden = !this.query;
      this.schedule();
    },

    /* Recherche locale : nom de mission, agent, outil. Les données sont déjà
       là, inutile de solliciter le backend pour filtrer trois champs. */
    matchesQuery(mission) {
      if (!this.query) return true;
      const haystack = [mission.name, mission.agent, mission.failed_tool,
        ...(mission.tools || []),
        ...((mission.last_tool && [mission.last_tool.id]) || []),
        ...((mission.tool && [mission.tool.id]) || [])]
        .filter(Boolean).join(' ').toLowerCase();
      return haystack.includes(this.query);
    },

    renderFilters() {
      [...byId('mcFilters').children].forEach((b) => {
        const n = this.counts ? this.counts[b.dataset.filter] : undefined;
        b.querySelector('em').textContent = (typeof n === 'number') ? String(n) : '';
      });
    },

    renderList(current) {
      const host = byId('mcList');
      let source = (this.listed && this.listed.length)
        ? this.listed
        : [...this.missions.values()].sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
      if (this.mode === 'LIVE') source = source.filter((m) => m.active);
      const total = source.length;
      source = source.filter((m) => this.matchesQuery(this.missions.get(m.id) || m));
      host.replaceChildren();
      const rows = source.slice(0, MAX_LIST_ROWS);
      const meta = byId('mcListMeta');
      if (meta) {
        meta.textContent = this.query
          ? `${source.length} / ${total} mission(s)`
          : (total > rows.length ? `${rows.length} / ${total} affichées` : '');
      }
      if (!rows.length) {
        host.innerHTML = this.query
          ? `<p class="mc-none">Aucune mission ne correspond à « ${this.query.replace(/[<>&]/g, '')} ».</p>`
          : (this.mode === 'LIVE'
            ? `<p class="mc-none">Aucune mission en cours.</p>`
            : `<p class="mc-none">Aucune mission pour ce filtre.</p>`);
        return;
      }
      const frag = document.createDocumentFragment();
      rows.forEach((row) => {
        const m = this.missions.get(row.id) || row;
        const b = document.createElement('button');
        b.className = 'mc-row';
        b.dataset.mission = m.id;
        b.dataset.tone = STATE_TONE[m.state] || 'wait';
        if (current && m.id === current.id) b.dataset.on = '1';
        if (this.pinned && this.selected === m.id) b.dataset.pin = '1';
        b.innerHTML = `<b></b><em></em><span></span>`;
        b.querySelector('b').textContent = m.name || 'Mission';
        b.querySelector('em').textContent = STATE_LABEL[m.state] || m.state || '';
        b.querySelector('span').textContent = clock(m.duration);
        b.title = (this.pinned && this.selected === m.id)
          ? 'Épinglée — cliquer pour revenir au suivi automatique'
          : 'Cliquer pour ouvrir cette mission';
        frag.appendChild(b);
      });
      host.appendChild(frag);
    },

    renderHealth() {
      const host = byId('mcHealth');
      if (!host) return;
      host.replaceChildren();
      if (!this.health) {
        const el = document.createElement('span');
        el.className = 'mc-hitem';
        el.dataset.tone = 'unknown';
        el.innerHTML = `<i></i><b>SANTÉ</b><em>UNKNOWN</em>`;
        el.title = 'État des composants indisponible';
        host.appendChild(el);
        return;
      }
      (this.health.components || []).forEach((c) => {
        const el = document.createElement('span');
        el.className = 'mc-hitem';
        el.dataset.tone = HEALTH_TONE[c.state] || 'unknown';
        el.innerHTML = `<i></i><b></b><em></em>`;
        el.querySelector('b').textContent = (c.name || '').toUpperCase();
        el.querySelector('em').textContent = c.state;
        el.title = `${c.name} — ${c.state}${c.detail ? ' · ' + c.detail : ''}`;
        host.appendChild(el);
      });
    },

    renderSteps(m) {
      const host = byId('mcSteps');
      host.replaceChildren();
      const steps = m.steps || [];
      byId('mcStepCount').textContent = steps.length
        ? `${steps.filter((s) => s.state === 'done' || s.state === 'err').length}/${steps.length}` : '';
      byId('mcStepsNone').hidden = steps.length > 0;
      const frag = document.createDocumentFragment();
      steps.forEach((s, i) => {
        const st = STEP_GLYPH[s.state] ? s.state : 'idle';
        const li = document.createElement('li');
        li.dataset.st = st;
        li.innerHTML = `<u></u><i></i><b></b><em></em>`;
        li.querySelector('u').textContent = STEP_GLYPH[st];
        li.querySelector('i').textContent = String(i + 1).padStart(2, '0');
        li.querySelector('b').textContent = s.label || s.key || '—';
        li.querySelector('em').textContent = STEP_LABEL[s.state] || 'WAITING';
        frag.appendChild(li);
      });
      host.appendChild(frag);
    },

    renderTool(m) {
      const host = byId('mcTool');
      host.replaceChildren();
      const tool = m.tool || m.last_tool;
      if (!tool) { host.innerHTML = `<p class="mc-none">Aucun outil appelé.</p>`; return; }
      const running = !!m.tool;
      const card = document.createElement('div');
      card.className = 'mc-toolcard';
      card.dataset.state = running ? 'run' : (tool.ok === false ? 'err' : 'ok');
      card.innerHTML = `<div class="mct-head"><b></b><em></em></div><dl class="mct-args"></dl>`;
      card.querySelector('b').textContent = tool.id || tool.name || '—';
      card.querySelector('em').textContent = running
        ? 'en cours…' : `${tool.ok === false ? 'échec' : 'ok'} · ${tool.duration_ms || 0} ms`;
      const dl = card.querySelector('.mct-args');
      const args = tool.args || {};
      const keys = Object.keys(args);
      if (!keys.length) dl.innerHTML = `<span class="mc-none">aucun paramètre</span>`;
      else keys.forEach((k) => {
        const dt = document.createElement('dt'); dt.textContent = k;
        const dd = document.createElement('dd');
        dd.textContent = typeof args[k] === 'string' ? args[k] : JSON.stringify(args[k]);
        dl.append(dt, dd);
      });
      host.appendChild(card);
    },

    /* FILES groupés par action RÉELLEMENT observée. Un groupe n'apparaît que
       s'il contient quelque chose : on ne montre pas « CREATED : 0 » pour
       faire joli, et on ne déduit jamais une action qu'on n'a pas vue. */
    renderFiles(m) {
      const host = byId('mcFiles');
      host.replaceChildren();
      const files = m.files || [];
      const count = byId('mcFilesCount');
      if (count) count.textContent = files.length ? String(files.length) : '';
      if (!files.length) { host.innerHTML = `<p class="mc-none">Aucun fichier touché.</p>`; return; }

      const groups = new Map();
      files.slice().reverse().forEach((f) => {
        const label = (FILE_GROUPS.find(([key]) => key === f.action) || [null, 'AUTRE'])[1];
        if (!groups.has(label)) groups.set(label, []);
        groups.get(label).push(f);
      });
      const frag = document.createDocumentFragment();
      groups.forEach((rows, label) => {
        const head = document.createElement('div');
        head.className = 'mc-fgroup';
        head.innerHTML = `<b></b><em></em>`;
        head.querySelector('b').textContent = label;
        head.querySelector('em').textContent = String(rows.length);
        frag.appendChild(head);
        rows.slice(0, 8).forEach((f) => {
          const row = document.createElement('button');
          row.className = 'mc-file';
          row.dataset.act = f.action || 'read';
          row.dataset.filePath = f.path;
          row.innerHTML = `<b></b><em></em>`;
          row.querySelector('b').textContent = f.path;
          row.querySelector('em').textContent = f.ts ? hhmmss(f.ts) : '';
          row.title = `${f.path} — ${FILE_LABEL[f.action] || f.action || ''} (métadonnées seules)`;
          frag.appendChild(row);
        });
      });
      host.appendChild(frag);
    },

    renderOut(m) {
      const host = byId('mcOut');
      host.replaceChildren();
      const parts = [];
      if (m.error) parts.push(['err', 'ERREUR', m.error]);
      if (m.result) parts.push(['ok', 'RÉSULTAT', m.result]);
      const last = m.last_tool;
      if (!parts.length && last && (last.preview || last.error)) {
        parts.push([last.ok === false ? 'err' : 'ok', 'DERNIER OUTIL', last.error || last.preview]);
      }
      if (!parts.length) { host.innerHTML = `<p class="mc-none">Rien à afficher pour l'instant.</p>`; return; }
      parts.forEach(([tone, label, text]) => {
        const box = document.createElement('div');
        box.className = 'mc-outbox';
        box.dataset.tone = tone;
        box.innerHTML = `<span></span><pre></pre>`;
        box.querySelector('span').textContent = label;
        box.querySelector('pre').textContent = text;
        host.appendChild(box);
      });
    },

    /* Un échec doit être exploitable sans fouiller : où, qui, quoi, pourquoi. */
    renderFailure(m) {
      const host = byId('mcFail');
      if (m.state !== 'FAILED') { host.hidden = true; return; }
      host.hidden = false;
      byId('mcFailMsg').textContent = m.error || 'Échec sans message.';
      const grid = byId('mcFailGrid');
      grid.replaceChildren();
      [
        ['Étape', m.failed_step || (m.current_step && m.current_step.label) || '—'],
        ['Agent', m.agent || '—'],
        ['Outil', m.failed_tool || (m.last_tool && m.last_tool.id) || '—'],
        ['Durée avant erreur', clock(m.duration)],
      ].forEach(([label, value]) => {
        const dt = document.createElement('dt'); dt.textContent = label;
        const dd = document.createElement('dd'); dd.textContent = value;
        grid.append(dt, dd);
      });
    },

    /* Journal : en direct l'historique récent, en replay les événements déjà
       joués. Toujours borné à MAX_LOG_ROWS lignes rendues. */
    renderLog(mission) {
      const host = byId('mcHist');
      host.replaceChildren();
      let rows;
      // `_logEntries` garde l'événement brut derrière chaque ligne : c'est ce
      // que l'inspecteur ouvre, sans rien redemander au backend.
      this._logEntries = [];
      const keep = (entry) => { this._logEntries.push(entry); return this._logEntries.length - 1; };
      if (this.replay) {
        const played = this.replay.entries.slice(0, this.replay.index);
        rows = played.slice(-MAX_LOG_ROWS).reverse().map((e, i) => ({
          stamp: offsetClock(e.offset_ms), kind: e.kind, label: e.label,
          extra: e.duration_ms ? `${e.duration_ms} ms` : (e.detail || e.tool || ''),
          err: e.ok === false, index: keep(e),
          now: i === 0 && this.replay.index > 0,
        }));
      } else {
        rows = (mission.history || []).slice(-MAX_LOG_ROWS).reverse().map((h) => ({
          stamp: hhmmss(h.ts), kind: h.kind, label: h.label,
          extra: h.duration_ms ? `${h.duration_ms} ms` : (h.error || h.preview || h.tool || ''),
          err: h.level === 'error' || h.ok === false || h.kind === 'denied',
          index: keep(h),
        }));
      }
      if (!rows.length) { host.innerHTML = `<p class="mc-none">Aucune action enregistrée.</p>`; return; }
      const frag = document.createDocumentFragment();
      rows.forEach((r) => {
        const row = document.createElement('div');
        row.className = 'mc-hrow';
        row.dataset.kind = r.kind || 'log';
        if (r.err) row.dataset.tone = 'err';
        if (r.now) row.dataset.now = '1';
        if (r.kind === 'tool' || r.kind === 'tool_result' || r.kind === 'denied') {
          row.dataset.logIndex = String(r.index);
          row.title = 'Ouvrir l\'inspecteur d\'outil';
        }
        row.innerHTML = `<i></i><u></u><b></b><em></em>`;
        row.querySelector('i').textContent = r.stamp;
        row.querySelector('u').textContent = HISTORY_LABEL[r.kind] || String(r.kind || '').toUpperCase();
        row.querySelector('b').textContent = r.label || '';
        row.querySelector('em').textContent = String(r.extra || '').slice(0, 160);
        frag.appendChild(row);
      });
      host.appendChild(frag);
    },

    badge() {
      const live = [...this.missions.values()].filter((m) => m.active).length;
      document.documentElement.dataset.mcLive = live ? String(live) : '';
      const btn = document.querySelector('.v5-rail button[data-mc-badge]');
      if (btn) btn.dataset.badge = live ? String(live) : '';
    },

    /* ---------------------------------------------------------- branchement */
    bind() {
      if (this._bound || !hasBus()) return;
      this._bound = true;
      ['mission.started', 'mission.update', 'mission.completed', 'mission.failed']
        .forEach((evt) => J.on(evt, (data) => this.ingest(data)));
      J.on('stream.open', () => {
        this.setLive(true);
        // Reconnexion : des événements ont pu être manqués. On resynchronise
        // sur la vérité du backend au lieu de continuer sur un état périmé.
        if (this.open) { this.hydrate(); this.loadHistory(); this.loadHealth(); }
      });
      J.on('stream.close', () => this.setLive(false, 'flux SSE coupé'));
      this.init();
      this.hydrate();
    },
  };

  window.JarvisMissionControl = MissionControl;
  if (document.readyState === 'loading') {
    addEventListener('DOMContentLoaded', () => MissionControl.bind());
  } else {
    MissionControl.bind();
  }
})();
