# Scénarios

Un **scénario** décrit un cas d'usage métier : le prompt système qui le pilote, le
message d'accueil, et le flux de conversation attendu.

## Ce qu'un scénario n'est pas

Le prompt réellement chargé à l'exécution est
[`src/assistant_vocal/prompt_fr.txt`](../../src/assistant_vocal/prompt_fr.txt), et lui
seul : c'est la source unique du dépôt, lue par `read_prompt()` et posée dans la
bibliothèque par la rustine 3 de
[`patches.py`](../../src/assistant_vocal/patches.py).

Les fichiers de ce dossier ne sont **pas** chargés par le code. Ce sont des documents
de conception. Pour faire tourner un scénario, il faut reporter son prompt dans
`prompt_fr.txt` — et l'adapter, voir ci-dessous.

## Ce qu'il faut adapter avant de faire tourner un scénario

### 1. La règle d'étiquette d'émotion

`prompt_fr.txt` impose au modèle de langue de préfixer chaque réplique d'une étiquette
(`[joie]`, `[empathie]`…). Un prompt de scénario écrit pour une autre plateforme ne la
contient pas : sans elle, `EmotionTracker` avertit « aucune étiquette d'émotion » à la
première réplique.

Le défaut du dépôt est cependant `VOICE_EMOTIONS=false`, parce que le clonage de voix
et les émotions sont mutuellement exclusifs — voir le README principal. L'étiquette
n'est donc plus qu'un reliquat tant que le clonage est en place.

### 2. Les contraintes de l'oral

`prompt_fr.txt` borne les réponses à « une à deux phrases, moins de vingt-cinq mots »,
et fait écrire les nombres et sigles en mots. Un prompt conçu pour du texte produit des
réponses trop longues, que la synthèse lira intégralement.

### 3. Les appels d'outils ne sont pas câblés

**C'est la limite qui bloque vraiment.** Un scénario qui écrit dans un CRM ou envoie un
courriel a besoin d'appels d'outils. Or ce dépôt n'en câble aucun :
`build_pipeline_args()` ne passe aucun drapeau d'outil, et il n'existe pas de registre
d'outils dans `src/assistant_vocal/`.

La bibliothèque amont, elle, connaît la notion (`tool_call` apparaît dans ses handlers
de modèle de langue). Le travail à faire est donc côté projet, pas côté amont — mais il
reste entier.

## Scénarios présents

| Dossier | Cas d'usage | État |
|---|---|---|
| [`technitoit/`](technitoit/) | Accueil téléphonique : qualification d'une demande, orientation vers devis / suivi de chantier / SAV | Conception. Nécessite deux outils non câblés |
