import { defineConfig } from 'vitest/config';

/* Tests de l'interface JARVIS.
   Le produit ne passe par aucun bundler : les scripts de `ui/js` sont servis
   tels quels au navigateur. Les tests les chargent donc comme le navigateur le
   fait — en évaluant le fichier source réel — plutôt que d'en importer une
   copie transformée qui ne prouverait rien sur le code livré. */
export default defineConfig({
  test: {
    environment: 'jsdom',
    include: ['tests/frontend/**/*.test.js'],
    globals: false,
    restoreMocks: true,
    reporters: 'default',
  },
});
