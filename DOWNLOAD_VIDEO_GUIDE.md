# Téléchargement des vidéos – Guide d’intégration

Ce guide décrit comment permettre le téléchargement sécurisé des vidéos originales via les endpoints ajoutés dans `server.py`.

## Vue d’ensemble (v2 hiérarchique)
- **Émission token download v2**: `GET /api/get-download-token/v2`
- **Téléchargement (v2)**:
  - Leçon (original): `GET /download2/<rel>/<filename>`
  - Présentation formation: `GET /download2/course/<course_id>/<filename>`
  - Présentation module: `GET /download2/module/<course_id>/<module_id>/<filename>`
- **Sécurité**: token court-vivant, lié au chemin (`rel` / `course_id` / `module_id`) et au `filename`, vérifié par `token_required_download`.

## 1) Émettre un token de téléchargement (v2)
- Route: `GET /api/get-download-token/v2`
- Query selon `type`:
  - `type`: `lesson` | `course` | `module`
  - `user_id`: ID propriétaire/autorisé
  - `filename`: nom du fichier exact
  - `rel` (si `type=lesson`): `trainer_id/course_id/module_id/lesson_id`
  - `course_id` (si `type=course` ou `module`)
  - `module_id` (si `type=module`)
  - `ttl` (optionnel, défaut 900)

Exemple de réponse:
```json
{
  "token": "<JWT>",
  "expires_in": 900,
  "download_url": "https://server.focustagency.com/download2/trainer123/course456/module789/lesson001/Video.mp4"
}
```

## 2) Télécharger le fichier (v2)
- Envoyer une requête GET sur `download_url` avec `Authorization: Bearer <token>`.
- Le serveur force le téléchargement via `Content-Disposition: attachment`.

### Exemple cURL
```bash
# Leçon (original): émettre token
curl "https://server.focustagency.com/api/get-download-token/v2?type=lesson&user_id=trainer123&rel=trainer123/course456/module789/lesson001&filename=Video.mp4&ttl=900"

# Télécharger (avec header Authorization)
curl -H "Authorization: Bearer <JWT>" -OJL \
  "https://server.focustagency.com/download2/trainer123/course456/module789/lesson001/Video.mp4"

# Présentation formation
curl "https://server.focustagency.com/api/get-download-token/v2?type=course&user_id=trainer123&course_id=course456&filename=presentation.mp4"
curl -H "Authorization: Bearer <JWT>" -OJL \
  "https://server.focustagency.com/download2/course/course456/presentation.mp4"

# Présentation module
curl "https://server.focustagency.com/api/get-download-token/v2?type=module&user_id=trainer123&course_id=course456&module_id=module789&filename=presentation.mp4"
curl -H "Authorization: Bearer <JWT>" -OJL \
  "https://server.focustagency.com/download2/module/course456/module789/presentation.mp4"
```

### Exemple Flutter (http)
```dart
import 'dart:convert';
import 'package:http/http.dart' as http;

final tokenRes = await http.get(Uri.parse(
  'https://server.focustagency.com/api/get-download-token/v2?type=lesson&user_id=$userId&rel=$rel&filename=$filename&ttl=900',
));
final data = jsonDecode(tokenRes.body);
final jwt = data['token'] as String;
final url = data['download_url'] as String;

final resp = await http.get(
  Uri.parse(url),
  headers: { 'Authorization': 'Bearer $jwt' },
);
```

## UX & Robustesse (mobile-first)
- **Skeletons**: préférer des placeholders squelettes pendant la demande de token et le téléchargement.
- **Snackbars**: utilisez vos fonctions utilitaires depuis `utils` pour les erreurs (timeout, token expiré, stockage insuffisant).
- **Catching**: try/catch pour les appels réseau et l’écriture disque afin d’éviter tout crash.
- **Design**: boutons larges, feedback clair (progression si possible), transitions fluides.

## Sécurité
- **Token dédié**: v2 avec claims `type`, `filename`, et selon type: `rel` ou (`course_id`, `module_id`). Vérifié par `token_required_download`.
- **TTL court**: par défaut 900s (15 min). Ajustable via `ttl` à l’émission.
- **Binding strict**: token lié au chemin hiérarchique et au fichier. Toute divergence → 403.
- **Anti-aspiration**: pas de download sans en-tête Authorization valide.

## Références code (`server.py`)
- **Décorateur**: `token_required_download`
- **Génération**: `generate_download_token_v2(user_id, dtype, filename, duration, rel?, course_id?, module_id?)`
- **Endpoints**:
  - `GET /api/get-download-token/v2` (+ OPTIONS)
  - `GET /download2/<rel>/<filename>`
  - `GET /download2/course/<course_id>/<filename>`
  - `GET /download2/module/<course_id>/<module_id>/<filename>`

---
Cette procédure offre un téléchargement contrôlé et robuste, respectant une expérience premium: rapide, élégante, et sécurisée. Ajustez le TTL et les messages UX selon vos besoins.
