# API JSON de lecture pour le tableau de bord (ajout du fork)

Ajout propre au fork `r3surrection/VODUM` (branche `fork/main`), absent d'upstream.
VODUM ne documente aucune API HTTP ; ce blueprint ([`app/api/dashboard.py`](../app/api/dashboard.py))
expose uniquement des données **en lecture** pour un tableau de bord externe.
Une seule route d'écriture existe : `POST /users/<id>/gift` (voir plus bas).

## Activation et accès

| Élément | Règle |
|---|---|
| Interrupteur | Page d'administration **Dashboard API** (`/settings/dashboard-api`) : `settings.dashboard_api_enabled`. À 0, toute requête répond 404, quelle que soit la clé. |
| Clés en base | Créées sur la même page (nom libre, clé affichée une seule fois) ; stockées dans `dashboard_api_keys` sous forme de hachage SHA-256 avec un préfixe de 6 caractères, `created_at`, `last_used_at` (mis à jour au plus une fois par minute) et `revoked_at`. Révocation définitive avec confirmation. |
| Variable d'environnement | `VODUM_DASHBOARD_API_KEY` : clé d'amorçage, toujours acceptée en plus des clés en base, listée sur la page comme « Variable d'environnement (active) » et non révocable. Absente = seules les clés en base comptent. À fournir par l'environnement du stack, jamais dans le dépôt. |
| En-tête | `X-Api-Key: <clé>` (comparaison à temps constant ; clé en base comparée par hachage). |
| Clé absente, vide ou fausse | **404** (pas 401), pour ne pas révéler l'existence de l'API. |
| Méthodes | `GET` (et `HEAD`), plus `POST /users/<id>/gift`. Toute autre méthode : 404 (jamais 405 ; le garde CSRF global répond 403 à un `POST` ailleurs, comme pour toute l'application). |
| Hôte du portail | 404 sur `/api/dashboard/*`, comme pour toutes les routes d'administration ([garde globale](../app/core/global_access_guard.py)). |
| Filtre IP | Le filtre existant (`VODUM_IP_FILTER`, `VODUM_ALLOWED_NETS`) s'applique avant. Le défaut couvre `172.16.0.0/12`, donc le réseau Docker du reverse proxy. |
| Session | Pas de session ni d'authentification de portail côté application : la clé suffit (la route est classée `dashboard_api` dans [`route_access_policy.py`](../app/core/route_access_policy.py)). |
| Cache | `Cache-Control: no-store`. |

Préfixe : `/api/dashboard/v1`. Toutes les réponses sont du JSON avec
`generated_at` (UTC). Les heures sont en UTC ISO 8601 (`2026-11-23T10:00:00Z`) ;
les dates d'expiration sont des dates `YYYY-MM-DD`. Les erreurs de paramètre
authentifiées répondent `400 {"error": "..."}`.

## Données personnelles et secrets

- Jamais dans les réponses : jetons, URL de serveurs, clés, hachages.
- Adresses e-mail : absentes par défaut. `?pii=1` (avec une clé valide) ajoute
  `email` / `second_email` (utilisateurs), `email` (comptes) et `user_email` (demandes).
- Seul le nom affiché est toujours présent. Les textes d'erreur des tâches et les
  motifs de gifts sont tronqués et expurgés (valeurs ressemblant à des jetons,
  mots de passe, adresses e-mail).

## Endpoints

| Chemin | Contenu |
|---|---|
| `/health` | version, uptime, taille de la base, dernier backup, portail actif |
| `/users?status=&limit=&pii=` | utilisateurs logiques ; `status` accepte `active`, `expiring_soon`, `reminder`, `expired`, `invited`, `removed`, `suspended`, `unknown` (aussi les noms internes `pre_expired`, `unfriended` et les libellés) ; `limit` 1–5000 (défaut 1000) |
| `/users/<id>` | un utilisateur avec historique des gifts (100 derniers) et comptes détaillés |
| `/expiring?days=30` | expiration dans 0…`days` jours (`days` 0–3650), triée par date puis id |
| `/requests?state=open` | demandes de médias ; `state` = `open` (défaut), `all` ou un état exact (`pending_approval`, `approved`, `available`, `declined`, `failed`, `removed`, `queued`, `checking`, `submitting`, `done`) ; « ouvert » = `pending_approval`, `approved`, `queued`, `checking`, `submitting` |
| `/servers` | état de chaque serveur, lectures en cours, historique 7 jours |
| `/tasks` | tâches de fond (nom, dernier/prochain passage, résultat) |
| `/stats` | compteurs |
| `POST /users/<id>/gift` | prolonge un membre de 180 ou 365 jours (seule route d'écriture) |

### Modèle de données lu

Lecture seule via `isolated_read_operation` (connexion dédiée, `query_only`).

| Notion | Table(s) |
|---|---|
| Utilisateur logique | `vodum_users` (`status`, `expiration_date`, `subscription_template_id`, `renewal_*`) |
| Plan d'abonnement | `subscription_templates` (`name`, `is_lifetime`) |
| Comptes Plex/Jellyfin | `media_users` (un par serveur, `vodum_user_id`) |
| Serveurs, bibliothèques | `servers`, `libraries`, `media_user_libraries` |
| Gifts / prolongations | `subscription_gift_runs` + `subscription_gift_run_users` |
| Demandes de médias (dont Seerr) | `portal_media_request_jobs` (`source` `arr`/`seerr`, `seerr_request_id`, `seerr_state`), `portal_accounts` |
| Tâches | `tasks` |
| Lectures en cours | `media_sessions` (vues depuis moins de 120 s, `missing_count = 0`) |
| Historique de disponibilité | `arr_monitoring_samples` (voir limites) |
| Backups | fichiers `backup_*.zip` du dossier `BACKUP_DIR` (seulement `stat`, jamais ouverts) |
| Portail | `settings.portal_enabled` |

Statuts : valeur stockée → API → libellé : `active` → `active` « Active » ·
`pre_expired` → `expiring_soon` « Expiring soon » · `reminder` → `reminder` « Reminder » ·
`expired` → `expired` « Expired » · `invited` → `invited` « Invited » ·
`unfriended` → `removed` « Removed » · `suspended` → `suspended` « Suspended » ·
`unknown` ou toute autre valeur → `unknown` « Unknown ». Le statut est celui calculé
par la tâche `update_user_status` ; l'API ne le recalcule pas.

## Exemples (valeurs fictives)

`GET /api/dashboard/v1/health`

```json
{
  "generated_at": "2026-11-23T12:00:00Z",
  "status": "ok",
  "version": "26.11.19 b1507",
  "uptime_seconds": 86400,
  "db_size_bytes": 5242880,
  "last_backup_at": "2026-11-23T03:00:00Z",
  "backup_count": 10,
  "portal_enabled": true
}
```

`GET /api/dashboard/v1/users?status=expiring_soon&limit=1`

```json
{
  "generated_at": "2026-11-23T12:00:00Z",
  "count": 1,
  "total": 1,
  "users": [
    {
      "id": 2,
      "display_name": "bob",
      "status": "expiring_soon",
      "status_label": "Expiring soon",
      "plan_id": 1,
      "plan": "Monthly",
      "plan_is_lifetime": false,
      "expiration_date": "2026-12-10",
      "days_left": 17,
      "last_gift": {"run_id": 1, "at": "2026-10-01T10:00:00Z", "days_added": 30, "reason": "autumn"},
      "renewal_method": null,
      "renewal_date": null,
      "created_at": "2026-01-05T09:00:00Z",
      "status_changed_at": "2026-11-10T00:00:00Z",
      "account_count": 1,
      "accounts": [
        {"server_id": 1, "server": "Plex One", "type": "plex", "libraries": ["Movies", "Shows"]}
      ]
    }
  ]
}
```

`GET /api/dashboard/v1/users/2` : même objet sous `user`, avec `accounts[]`
détaillés (`account_id`, `username`, `role`) et `gifts[]` (`run_id`, `at`,
`days_added`, `reason`, `target_type`, du plus récent au plus ancien).

`GET /api/dashboard/v1/expiring?days=7` : `{"days": 7, "count": n, "users": [...]}`,
objets identiques à `/users`. Bornes : `days_left` de 0 (expire aujourd'hui) à `days`
inclus ; les utilisateurs déjà expirés, sans date (à vie), `invited` et `removed`
n'y figurent pas. Les jours sont comptés par rapport à la date UTC.

`GET /api/dashboard/v1/requests?state=open`

```json
{
  "generated_at": "2026-11-23T12:00:00Z",
  "state": "open",
  "count": 1,
  "requests": [
    {
      "id": "0f1e2d3c4b5a69788796a5b4c3d2e1f0",
      "title": "Example Movie",
      "type": "movie",
      "year": null,
      "user_id": 1,
      "user": "alice",
      "state": "pending_approval",
      "open": true,
      "source": "seerr",
      "job_status": "done",
      "requested_at": "2026-11-22T10:00:00Z",
      "finished_at": "2026-11-22T10:00:05Z",
      "seerr_id": 101,
      "external_id": 11,
      "is_4k": false,
      "seasons": null
    }
  ]
}
```

`state` d'une demande Seerr = dernier état Seerr connu (`seerr_state`) ; sinon
l'état du travail VODUM. Une demande est **ouverte** tant que le travail n'est pas
terminé, ou qu'une demande Seerr suivie n'est pas `available`, `declined`,
`failed` ou `deleted`.

`GET /api/dashboard/v1/servers`

```json
{
  "generated_at": "2026-11-23T12:00:00Z",
  "count": 1,
  "servers": [
    {
      "id": 1,
      "name": "Plex One",
      "type": "plex",
      "status": "up",
      "last_checked": "2026-11-23T11:59:00Z",
      "down_since": null,
      "version": "1.2.3",
      "now_playing": 2,
      "history_days": 7,
      "history_source": "none",
      "history": []
    }
  ]
}
```

`GET /api/dashboard/v1/tasks`

```json
{
  "generated_at": "2026-11-23T12:00:00Z",
  "count": 1,
  "tasks": [
    {
      "id": 1, "name": "example_task", "description": null, "enabled": true,
      "status": "idle", "result": "ok",
      "last_run": "2026-11-23T11:00:00Z", "next_run": "2026-11-23T12:00:00Z",
      "schedule": "0 * * * *", "interval_seconds": null, "retry_count": 0, "last_error": null
    }
  ]
}
```

`result` : `ok`, `error` (`last_error` rempli, expurgé), `running`, `queued`,
`disabled`, `never` (activée, jamais exécutée).

`GET /api/dashboard/v1/stats`

```json
{
  "generated_at": "2026-11-23T12:00:00Z",
  "users_total": 13,
  "users_by_status": {"active": 5, "expiring_soon": 1, "reminder": 1, "expired": 2,
                      "invited": 1, "removed": 1, "suspended": 1, "unknown": 1},
  "users_by_plan": {"(none)": 1, "Forever": 1, "Monthly": 11},
  "expiring_7d": 3,
  "expiring_30d": 6,
  "requests_total": 6,
  "requests_open": 3,
  "requests_by_state": {"approved": 1, "available": 1, "pending_approval": 1}
}
```

## Gift : `POST /api/dashboard/v1/users/<id>/gift`

Corps JSON : `{"days": 365, "note": "..."}`.

- `days` : **180** (« six mois ») ou **365** (« un an »), défaut 365. Tout autre
  valeur (y compris 182, un texte ou un booléen) : 400. Vodum enregistre les gifts
  **en jours** (`subscription_gift_runs.days_added`) ; le formulaire Gifts propose
  1, 7, 30, 90, **180** et **365** jours. Le bouton du tableau de bord utilise
  donc exactement ces deux préréglages.
- `note` : optionnelle, 200 caractères au plus (sinon 400). L'historique affiche
  le motif `Dashboard` ou `Dashboard: <note>`, et l'API le restitue avec
  `source: "Dashboard"` dans `gifts[]` / `last_gift`.
- Chemin de code : `apply_gift_run` dans [`app/api/subscriptions.py`](../app/api/subscriptions.py),
  extrait du gift de l'interface (Subscriptions > Gifts) et utilisé par les deux sans
  changement de comportement : ligne dans `subscription_gift_runs` et
  `subscription_gift_run_users`, puis `update_user_expiration` (recalcul du statut,
  réactivation, retrait de la politique « expired_subscription », modèle de mail
  « changement d'expiration »). Le gift se fait en mode « utilisateur » comme le
  gift ciblé de l'interface, qui ajoute les jours à la date d'expiration actuelle.
- Statuts autorisés : ceux que l'interface accepte pour un gift ciblé (`active`,
  `expiring_soon`, `reminder`) avec au moins un compte média. Sinon **409** avec
  `status` (notamment `removed`, `suspended`, mais aussi `expired`, `invited`,
  `unknown`, ou aucun compte). Aucune prolongation silencieuse. Utilisateur inconnu : 404.
- Idempotence : en-tête optionnel `X-Request-Id` (128 caractères au plus). Le même
  identifiant avec les mêmes paramètres dans les 10 minutes renvoie la même
  réponse (`Idempotent-Replay: true`) sans second gift ; avec d'autres paramètres :
  409. Les erreurs ne sont pas mémorisées. La mémoire est celle du processus : un
  redémarrage la vide.
- Auth : mêmes règles que le reste (clé, 404 sur l'hôte du portail). Cette route est
  exemptée du jeton CSRF de session.

Réponse 200 :

```json
{
  "generated_at": "2026-11-23T12:00:00Z",
  "user_id": 10,
  "gift_id": 3,
  "days_added": 365,
  "old_expiration_date": "2026-11-23",
  "new_expiration_date": "2027-11-23",
  "status": "active",
  "status_label": "Active"
}
```

Si la date d'expiration stockée est illisible, l'interface part d'aujourd'hui ; l'API
fait de même (comportement hérité).

## Limites connues

- **Historique des serveurs** : VODUM ne conserve aucun historique de
  disponibilité pour Plex/Jellyfin, seulement l'état courant (`status`,
  `last_checked`, `unavailable_since`). `history` n'est rempli (transitions
  up/down sur 7 jours) que pour les serveurs ARR/Seerr, depuis
  `arr_monitoring_samples` ; pour Plex, `history_source` vaut `none` et le
  tableau de bord doit construire sa propre série en interrogeant `/servers`.
- **Année des demandes** : VODUM ne stocke pas l'année d'un média ; `year` vaut
  toujours `null`.
- **Dernier backup** : date de modification du fichier `backup_*.zip` le plus récent
  dans `BACKUP_DIR` ; les archives ne sont jamais ouvertes.
- **Version** : valeur de `VODUM_VERSION` ou du fichier `INFO` ; `null` si absente.
- Pas de limitation de débit : l'accès est restreint par le réseau (voir ci-dessus).

## Déploiement

1. Image `ghcr.io/r3surrection/vodum:fork-main` (ou `:<sha7>`) construite par
   GitHub Actions depuis `fork/main`.
2. Définir `VODUM_DASHBOARD_API_KEY` dans l'environnement du stack (Portainer),
   pas dans le fichier compose du dépôt.
3. Router Traefik pour `/api/dashboard/` sur l'hôte d'administration **sans**
   authentification de portail mais avec liste d'IP autorisées du réseau interne ; accès interne
   possible via `http://vodum:5000/api/dashboard/v1/...`.
4. Aucune migration de base.

## Tests

`tests/test_dashboard_api.py` et `tests/test_dashboard_gift.py` (le dossier `tests/` est ignoré par `.gitignore` ;
le fichier est ajouté de force) construit une base à partir de `tables.sql`, enregistre
la vraie garde d'accès et couvre chaque endpoint, les huit statuts, les bornes
d'expiration, clé absente/fausse, hôte du portail, méthodes d'écriture et `pii`.

```powershell
$env:PYTHONPATH = "app"; python -m pytest tests/test_dashboard_api.py tests/test_dashboard_gift.py
```
