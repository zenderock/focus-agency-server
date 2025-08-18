# Lecture vidéo mobile (Flutter) – Procédure complète

Ce guide explique comment lire les vidéos HLS dans l’app mobile (Flutter) via la voie dédiée mobile mise en place dans `server.py`.

## Vue d’ensemble
- __Voie mobile HLS__: `GET /mobile/hls/<user_id>/<video_id>/output.m3u8`
- __Sécurité__: JWT court-vivant avec `platform="mobile"`, segments et clé chiffrée protégés par le même token.
- __Transport du token__: en-tête `Authorization: Bearer <jwt>` (recommandé) ou `?token=...` (fallback). La playlist mobile réécrit les URLs des segments/clé pour inclure le token.

## 1) Émettre un token mobile
- Endpoint: `GET /api/get-video-token/mobile/<user_id>/<filename>/<video_id>?ttl=<seconds>`
- Réponse:
  - `token`: JWT signé (HS256)
  - `expires_in`: TTL en secondes
  - `playlist_url`: URL prête à l’emploi de la playlist mobile

Exemple de réponse:
```json
{
  "token": "<JWT>",
  "expires_in": 1800,
  "playlist_url": "https://server.focustagency.com/mobile/hls/123/abc/output.m3u8"
}
```

Notes:
- `video_id` correspond au dossier HLS `hls/<user_id>/<video_id>/...`.
- `filename` est conservé pour compatibilité (historique de génération de token). Le binding HLS se fait avec `video_id`.
- TTL par défaut: `TOKEN_EXPIRY` (config serveur). Vous pouvez passer `ttl=900` (15 min) pour plus de sécurité.

## 2) Lire la vidéo dans Flutter
Utilisez l’URL `playlist_url` et injectez l’en-tête `Authorization` sur toutes les requêtes.

### Option A – video_player (officiel)
```dart
final jwt = '<JWT mobile>'; // récupéré depuis l’endpoint ci-dessus
final uri = Uri.parse('https://server.focustagency.com/mobile/hls/$userId/$videoId/output.m3u8');

final controller = VideoPlayerController.networkUrl(
  uri,
  httpHeaders: {'Authorization': 'Bearer $jwt'},
);
await controller.initialize();
// UI: afficher des skeletons pendant initialize(), pas de spinner
```

### Option B – Better Player (HLS avancé)
```dart
final dataSource = BetterPlayerDataSource(
  BetterPlayerDataSourceType.network,
  'https://server.focustagency.com/mobile/hls/$userId/$videoId/output.m3u8',
  headers: {'Authorization': 'Bearer $jwt'},
  useHlsSubtitles: true,
  useHlsTracks: true,
);
final controller = BetterPlayerController(
  const BetterPlayerConfiguration(autoPlay: true),
  betterPlayerDataSource: dataSource,
);
```

## 3) UX & résilience
- __Skeletons__: affichez des placeholders squelettes pendant `initialize()` et les chargements d’adaptation réseau.
- __Snacks__: en cas d’erreur (réseau/token expiré), utilisez vos fonctions Snackbar personnalisées depuis `utils`.
- __Catching__: entourez `initialize()`, `play()`, `seekTo()` de `try/catch` pour éviter tout crash. Affichez un message clair + action `Réessayer`.
- __Mobile-first__: lecture plein écran, gestes intuitifs, boutons larges, contrastes élevés, transitions fluides.

## 4) Sécurité & anti-téléchargement
- Le token inclut `platform: "mobile"` et peut inclure `user_id`/`video_id`.
- Les segments (`.ts`) et la clé (`/mobile/hls/<user_id>/<video_id>/key`) exigent le même token.
- `Cache-Control: no-store` empêche les caches locaux.
- Les aspirateurs HLS échouent s’ils n’envoient pas `Authorization` sur segments/clé.
- Recommandations:
  - TTL court (ex: 15–30 min) avec refresh silencieux dans l’app.
  - Liaison stricte `user_id` + `video_id` dans le token.

## 5) Points d’attention côté serveur
- Routes ajoutées dans `server.py`:
  - `GET /api/get-video-token/mobile/<user_id>/<filename>/<video_id>` (préflight OPTIONS inclus)
  - `GET /mobile/hls/<user_id>/<video_id>/output.m3u8` (réécriture du manifest avec token sur segments/clé)
  - `GET /mobile/hls/<user_id>/<video_id>/<segment>` (segments protégés)
  - `GET /mobile/hls/<user_id>/<video_id>/key` (clé protégée)
- Décorateurs:
  - `token_required_mobile`: pas de referrer requis, `platform=mobile` obligatoire, liaison à `user_id` et `video_id`.
  - `_get_token_from_request()`: support `Authorization` puis `?token`.
- Conversion HLS:
  - `convert_to_hls` aligne l’URL de clé sur `/hls/<user_id>/<video_id>/key`.

## 6) Flux recommandé côté app
1. __Demande token__:
   - `GET /api/get-video-token/mobile/<user_id>/<filename>/<video_id>?ttl=1800`
2. __Stockage sécurisé__ du JWT en mémoire volatile.
3. __Initialisation player__ avec:
   - `uri` = `playlist_url`
   - headers: `{ Authorization: 'Bearer <JWT>' }`
4. __UI__:
   - skeletons → lecture → erreurs gérées en Snackbar.
5. __Refresh token__:
   - à l’expiration, redemander un token et relancer la lecture si nécessaire.

## 7) Dépannage rapide
- 403 “Token is invalid or expired”: vérifiez `Authorization`, TTL, `platform=mobile`, `user_id`/`video_id`.
- 404 manifest/segment/clé: conversion pas terminée ou `video_id` incorrect.
- Pas de lecture sur mobile: assurez-vous d’utiliser la voie `/mobile/hls/...` et d’envoyer le header sur toutes les requêtes.

---
Ce setup offre une lecture fluide et premium côté mobile, tout en compliquant l’aspiration des vidéos. Adaptez TTL et UX selon vos besoins; l’app doit rester élégante, réactive et robuste aux erreurs.
