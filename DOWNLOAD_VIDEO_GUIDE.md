# Téléchargement des vidéos – Guide d’intégration

Ce guide décrit comment permettre le téléchargement sécurisé des vidéos originales via les endpoints ajoutés dans `server.py`.

## Vue d’ensemble
- **Génération token download**: `GET /api/get-download-token/<user_id>/<filename>?ttl=900`
- **Téléchargement du fichier**: `GET /api/download/<user_id>/<filename>` (avec `Authorization: Bearer <token>`)
- **Sécurité**: token court-vivant, lié à `user_id` et `filename`, vérifié par `token_required_download`.

## 1) Émettre un token de téléchargement
- Route: `GET /api/get-download-token/<user_id>/<filename>?ttl=<seconds>`
- Réponse JSON:
  - `token`: JWT (HS256) avec `action=download`
  - `expires_in`: durée en secondes (par défaut 900 = 15 min)
  - `download_url`: URL directe du fichier à utiliser pour le téléchargement

Exemple de réponse:
```json
{
  "token": "<JWT>",
  "expires_in": 900,
  "download_url": "https://server.focustagency.com/api/download/123/myvideo.mp4"
}
```

## 2) Télécharger le fichier
- Envoyer une requête GET sur `download_url` en ajoutant l’en-tête `Authorization: Bearer <token>`.
- Le serveur force le téléchargement via `Content-Disposition: attachment`.

### Exemple cURL
```bash
# 1) Obtenir le token
curl "https://server.focustagency.com/api/get-download-token/123/myvideo.mp4?ttl=900"

# 2) Télécharger (avec header Authorization)
curl -H "Authorization: Bearer <JWT>" -OJL \
  "https://server.focustagency.com/api/download/123/myvideo.mp4"
```

### Exemple Flutter (http)
```dart
import 'dart:convert';
import 'package:http/http.dart' as http;

final tokenRes = await http.get(Uri.parse(
  'https://server.focustagency.com/api/get-download-token/$userId/$filename?ttl=900',
));
final data = jsonDecode(tokenRes.body);
final jwt = data['token'] as String;
final url = data['download_url'] as String;

// Skeletons pendant la requête de download
final resp = await http.get(
  Uri.parse(url),
  headers: { 'Authorization': 'Bearer $jwt' },
);
if (resp.statusCode == 200) {
  // Écrire resp.bodyBytes sur le disque (path_provider + dart:io)
  // Catcher toutes les erreurs d’I/O
} else {
  // Afficher une snackbar via vos utilitaires personnalisés
}
```

## UX & Robustesse (mobile-first)
- **Skeletons**: préférer des placeholders squelettes pendant la demande de token et le téléchargement.
- **Snackbars**: utilisez vos fonctions utilitaires depuis `utils` pour les erreurs (timeout, token expiré, stockage insuffisant).
- **Catching**: try/catch pour les appels réseau et l’écriture disque afin d’éviter tout crash.
- **Design**: boutons larges, feedback clair (progression si possible), transitions fluides.

## Sécurité
- **Token dédié**: `generate_download_token()` inclut `action=download` et est vérifié par `token_required_download`.
- **TTL court**: par défaut 900s (15 min). Ajustable via `ttl` à l’émission.
- **Binding strict**: le token est lié à `user_id` + `filename`. Toute divergence → 403.
- **Anti-aspiration**: pas de download sans en-tête Authorization valide.

## Références code (`server.py`)
- **Décorateur**: `token_required_download`
- **Génération**: `generate_download_token(user_id, filename, duration=900)`
- **Endpoints**:
  - `GET /api/get-download-token/<user_id>/<filename>` (+ OPTIONS)
  - `GET /api/download/<user_id>/<filename>`

---
Cette procédure offre un téléchargement contrôlé et robuste, respectant une expérience premium: rapide, élégante, et sécurisée. Ajustez le TTL et les messages UX selon vos besoins.
