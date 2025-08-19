# WEB_VIDEO_GUIDE.md — Architecture backend (Next.js, côté web)

Ce document décrit comment intégrer côté web (Next.js) la nouvelle architecture d’upload et de diffusion vidéo alignée sur la hiérarchie Formation > Modules > Leçons implémentée dans `server.py`.

---

## 1) Contexte et objectifs

- __Hiérarchie__: Formation > Modules > Leçons.
- __Vidéos de présentation__:
  - Formation: 1 vidéo non chiffrée.
  - Module: 1 vidéo non chiffrée.
- __Leçons__: 1 vidéo chiffrée HLS par leçon.
- __Compatibilité__: les anciennes routes restent, mais l’usage recommandé est HLS v2 hiérarchique.

---

## 2) Arborescence fichiers (côté serveur)

- Upload temporaire: `uploads/<trainer_id>/<course_id>/<module_id>/<lesson_id>/`
- Originaux (download possible via jeton de téléchargement): `originals/<trainer_id>/<course_id>/<module_id>/<lesson_id>/`
- HLS (chiffré): `hls/<trainer_id>/<course_id>/<module_id>/<lesson_id>/`
  - `output.m3u8`, `segment_XXX.ts`, `enc.key`
- Présentations non chiffrées:
  - Formation: `presentation_videos/courses/<course_id>/presentation.<ext>`
  - Module: `presentation_videos/modules/<course_id>/<module_id>/presentation.<ext>`

---

## 3) Endpoints d’upload (backend)

- __Leçon (HLS chiffré)__
  - `POST /upload/lesson`
  - form-data requis: `trainer_id`, `course_id`, `module_id`, `lesson_id`, `video`
  - réponse: `task_id`, `lesson_id`, `hls_path` (ex: `/hls2/<trainer>/<course>/<module>/<lesson>/output.m3u8`)
  - limites: `MAX_CONTENT_LENGTH = 100 Mo`

- __Présentation Formation (non chiffrée)__
  - `POST /upload_presentation/course/<course_id>` (form-data: `video`)
  - réponse: `file_path`

- __Présentation Module (non chiffrée)__
  - `POST /upload_presentation/module/<course_id>/<module_id>` (form-data: `video`)
  - réponse: `file_path`

Remarque: À l’upload Leçon, l’original est copié dans `originals/...` et la conversion HLS est lancée en tâche asynchrone (Celery/FFmpeg).

---

## 4) Émission de jeton (lecture HLS v2)

- __Endpoint__: `GET /api/get-video-token/v2`
- __Paramètres__:
  - `user_id`: identifiant propriétaire/autorisé
  - `rel`: chemin hiérarchique `trainer_id/course_id/module_id/lesson_id`
  - `platform`: `web` (ou `mobile` pour applis natives)
  - `ttl`: durée en secondes (optionnel, défaut `TOKEN_EXPIRY`)
- __Réponse__:
  - `token`, `expires_in`, `playlist_url`
    - web: `https://server.focustagency.com/hls2/<rel>/output.m3u8`

Recommandations:
- TTL court (ex. 300–900s) sur web.
- Émettre les tokens côté serveur (Next.js route API), pas directement depuis le navigateur.

---

## 5) Lecture HLS (web)

- __Playlist__: `GET /hls2/<rel>/output.m3u8`
- __Headers requis__: `Authorization: Bearer <token>`
- __Referrer__: contrôlé côté serveur (domaines autorisés). Assurez-vous que votre domaine Next.js figure dans `FOCUST_ALLOWED_ORIGINS` et que le referrer est présent sur les requêtes.
- __Segments et clé__:
  - web: URLs internes, token vérifié par décorateur `token_required`.
  - mobile: playlist réécrite automatiquement (pour info, non utilisée côté web).

Bonnes pratiques UI (Next.js):
- Afficher des __skeletons__ pendant conversion et chargement.
- Utiliser vos __snackbars personnalisées__ (`utils`) pour les retours.
- Toujours __capter les erreurs__ réseau pour éviter un crash.

---

## 6) Téléchargement de l’original (optionnel)

- __Émission du token de téléchargement__:
  - `GET /api/get-download-token/<user_id>/<filename>?ttl=900`
  - réponse: `token`, `download_url`
- __Téléchargement__:
  - `GET /api/download/<user_id>/<filename>` avec `Authorization: Bearer <token>`

Note: Protégez l’émission du token de download côté backend (auth utilisateur/eligibilité).

---

## 7) Sécurité

- __Binding fort du chemin__: les tokens v2 contiennent `rel`, vérifié par les décorateurs.
- __Referrer (web)__: seulement depuis domaines autorisés.
- __Auth/Éligibilité__: l’émission des tokens doit être restreinte à l’utilisateur éligible (à implémenter côté Next.js et/ou service interne).
- __TTL court__ et __rate-limit__ sur l’émission de tokens.

---

## 8) Erreurs & Résilience

- Upload:
  - 400: champs manquants / type de fichier invalide
  - 202: conversion en cours (utiliser polling ou webhook externe)
- Lecture:
  - 403: token manquant/expiré/rel invalide
  - 404: playlist/segment en cours de conversion ou introuvable
- UI: afficher des __skeletons__ et une __snackbar__ claire en cas d’erreur.

---

## 9) FAQ

- __Pourquoi `/hls2` ?__
  - Pour supporter la hiérarchie complète sans casser les anciennes routes.
- __Puis-je utiliser un `rel` différent ?__
  - Respectez l’ordre: `trainer_id/course_id/module_id/lesson_id`.
- __Dois-je servir la playlist en public ?__
  - Non, la playlist requiert un token et un referrer valide côté web.

---

## 10) Checklist d’intégration (web)

- [ ] Créer une route API Next.js pour __uploader__ une leçon via `POST /upload/lesson`.
- [ ] Créer une route API Next.js pour __émettre__ un token v2 via `GET /api/get-video-token/v2` (côté serveur uniquement).
- [ ] Consommer `playlist_url` retournée et passer le header `Authorization` avec le token.
- [ ] Afficher des __skeletons__ durant conversion/lecture, et des __snackbars__ pour les retours.
- [ ] Gérer les erreurs 4xx/5xx sans crash.
- [ ] Optionnel: implémenter l’upload et la lecture des __présentations__ (formation/module).

---

## 11) Exemples d’appels (référence backend)

- Token v2 (web):
  - `GET /api/get-video-token/v2?user_id=trainer123&rel=trainer123/course456/module789/lesson001&platform=web&ttl=900`
- Playlist v2 (web):
  - `GET /hls2/trainer123/course456/module789/lesson001/output.m3u8` avec `Authorization: Bearer <token>`
- Upload leçon:
  - `POST /upload/lesson` (form-data requis)

Ces exemples servent de référence; implémentez les appels via vos routes API Next.js serveur pour garder les secrets/contrôles d’accès côté backend.
