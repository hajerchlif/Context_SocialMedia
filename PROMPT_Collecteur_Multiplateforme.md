# PROMPT — Construire un collecteur de données de réseaux sociaux multi-plateforme (2023–2026)

> **Mode d'emploi.** Sur le serveur : `bash scripts/deploy_ubuntu.sh`. Ensuite, dans le dossier du dépôt `~/Context_SocialMedia`, lancez Claude Code et donnez-lui ce fichier. Il écrit le code dans `collector/`. Puis `docker compose up -d --build`. Remplissez d'abord la section 11 et `.env`.

---

## 0. Ton rôle

Tu es un ingénieur données senior. Tu construis, testes et déploies sur ce serveur un **collecteur de données de réseaux sociaux pour une recherche doctorale**. Cette recherche porte sur l'extraction du contexte des publications, sur les profils d'intérêts des utilisateurs, sur la découverte de domaines sémantiques ouverts et sur la détection de communautés.

Travaille de façon autonome, étape par étape. Après chaque étape :
- lance les tests ;
- mets à jour `STATUS.md` (ce qui est fait, ce qui reste, comment reprendre).

Si une API, une bibliothèque ou une limite a changé, **vérifie la documentation actuelle avant de coder**. Ne suppose jamais un nom d'endpoint, une version ou une limite de débit.

---

## 1. Matériel et système

- **Serveur :** Ubuntu Server, Intel Core i5 de 13e génération, 32 Go de RAM, GPU 16 Go de VRAM (NVIDIA).
- **La collecte n'a pas besoin du GPU.** Le GPU ne sert qu'au post-traitement optionnel (section 8 : détection de langue rapide, embeddings).
- **Déploiement en Docker** (déjà fourni dans ce dépôt : `docker-compose.yml`, `Dockerfile`, `requirements.txt`, `.env.example`, `scripts/deploy_ubuntu.sh`). Services : `db` (PostgreSQL 16), `collector` (ton code), `export`, et `ollama` (LLM local optionnel, profil `llm`, sur le GPU).
- **Ton code va dans le dossier `collector/`** (package Python lancé par `python -m collector run --all` et `python -m collector export`). Lis `DATABASE_URL` et les clés depuis l'environnement. Ne modifie `docker-compose.yml` que si c'est nécessaire, et documente le changement.
- **Python 3.11+**, base PostgreSQL 16 (conteneur `db`), plus des exports Parquet dans `/app/data`.

---

## 2. Ce que les données doivent contenir

**Période :** du **2023-01-01 au 2026-12-31** (jusqu'à aujourd'hui pour 2026). La collecte comprend :
- un **backfill historique** sur 2023–2026 ;
- une **collecte continue** (temps réel) à partir de maintenant.

Pour **chaque plateforme**, collecte uniquement les champs ci-dessous, rien d'autre (minimisation RGPD).

| Table | Champs |
|---|---|
| `posts` | `post_uid` (pseudonymisé), `platform`, `author_uid`, `text`, `created_at` (UTC), `lang`, `hashtags[]`, `mentions_uid[]`, `urls[]`, `media_type` (none/image/video), `media_alt_text`, `parent_post_uid` (si réponse), `root_post_uid` (racine du fil), `quote_of_uid`, `like_count`, `repost_count`, `reply_count`, `community` (subreddit, groupe, serveur… si applicable), `collected_at`, `source_method` (api/official-research/archive/public-page) |
| `users` | `user_uid`, `platform`, `bio`, `account_created_at`, `followers_count`, `following_count`, `posts_count`, `is_verified` (si dispo), `first_seen_at` |
| `edges` | `src_uid`, `dst_uid`, `platform`, `type` ∈ {reply, mention, repost, quote, follow, like (optionnel)}, `post_uid` (si l'arête vient d'un post), `created_at` |
| `threads` | `root_post_uid`, `platform`, `n_posts`, `depth`, `n_participants`, `first_at`, `last_at` |
| `seeds` | `seed_id`, `platform`, `domain` (politique, science…), `kind` (account/feed/subreddit/hashtag/page/channel), `value`, `added_at` |
| `link_content` (optionnel) | `url_hash`, `final_domain`, `title`, `description`, `main_text` (≤ 5 000 car.), `fetched_at`, `http_status` |
| `collection_runs` | `run_id`, `platform`, `job`, `started_at`, `ended_at`, `items`, `errors`, `cursor_state` (JSON) |

**Fils de discussion :** pour chaque post retenu, reconstruire **le fil complet** (parent → racine, puis toutes les réponses). C'est la donnée la plus importante pour la recherche.

---

## 3. Plateformes et méthode d'accès (par ordre de priorité)

Architecture : **un connecteur par plateforme** (`connectors/<plateforme>.py`) avec la même interface :

```
fetch_user(uid) · fetch_user_posts(uid, since, until) · fetch_thread(post_id)
fetch_followers/following(uid) · search(query, since, until) · stream()
```

Chaque connecteur déclare ses capacités (`supports_threads`, `supports_follow_graph`, `supports_history`, `supports_stream`). Le pipeline s'adapte à ce qui est disponible.

| # | Plateforme | Méthode **obligatoire** | Historique 2023–2026 | Fils | Graphe social | Notes |
|---|---|---|---|---|---|---|
| 1 | **Bluesky** | API publique AT Protocol (SDK Python `atproto`) + **Jetstream** (flux temps réel) | Oui, via les flux d'auteur (plateforme ouverte au public depuis 2024) | Oui (`getPostThread`) | Oui (follows, reposts, likes) | **Plateforme principale** : la plus complète et la plus ouverte |
| 2 | **Reddit** | API officielle (PRAW, application « script », usage non commercial) **+ archives publiques mensuelles** (dumps type Pushshift / Arctic Shift sur Academic Torrents) pour l'historique | Oui (archives) | Oui (commentaires imbriqués) | Partiel (réponses, pas d'abonnements) | Les communautés = subreddits |
| 3 | **Mastodon** | API publique de chaque instance (`Mastodon.py`), sans compte ou avec un compte de lecture | Oui (par compte, par hashtag) | Oui (`context`) | Oui (followers/following publics) | Respecter les règles de chaque instance |
| 4 | **YouTube** (commentaires) | YouTube Data API v3 (clé gratuite, quota journalier) | Oui | Oui (réponses aux commentaires) | Non | Optionnel |
| 5 | **Telegram** (canaux publics) | API officielle MTProto via `Telethon` (compte développeur) | Oui | Partiel | Non | Optionnel |
| 6 | **X / Twitter** | **API officielle X** (le niveau d'abonnement détermine l'accès à l'historique) | Selon le niveau d'accès | Oui (conversation_id) | Oui | Le connecteur doit lire les clés dans `.env` et **désactiver X proprement** s'il n'y a pas de clé |
| 7 | **Facebook + Instagram** | **Meta Content Library / API** (accès chercheur, demande via l'université) | Oui (contenus publics) | Partiel | Partiel | Même logique : connecteur prêt, activé seulement si les identifiants sont présents |
| 8 | **TikTok** | **TikTok Research API** (accès chercheur) | Oui | Commentaires | Partiel | Même logique |

### Règles strictes du collecteur (à coder dans le pipeline)
- **Pas de connexion à un compte pour scraper**, pas de faux comptes, pas de rotation de comptes.
- **Pas de contournement de CAPTCHA, de murs de connexion ou de protections anti-robot**, et pas de proxys résidentiels pour échapper aux blocages. Si une page renvoie un CAPTCHA, un mur de connexion ou un 403/429 persistant : le connecteur **s'arrête pour ce domaine**, note l'événement et passe à la suite.
- Respecter `robots.txt` et les en-têtes de limite de débit (`ratelimit-remaining`, `retry-after`, `x-rate-limit-*`) : backoff exponentiel avec jitter, plafond de requêtes configurable par plateforme.
- User-Agent explicite et honnête, par exemple `ResearchCollector/1.0 (+contact: <EMAIL_DU_PROJET>)`.

Si l'accès chercheur à X, Meta ou TikTok n'est pas encore obtenu, le reste du système doit fonctionner normalement sans ces plateformes. Prépare un fichier `ACCES_A_DEMANDER.md` qui liste, pour chacune, le programme d'accès, le lien, les pièces à fournir (affiliation, protocole, avis éthique) et les délais typiques. **Vérifie en ligne que ces informations sont à jour.**

---

## 4. Scrapling (optionnel) : enrichissement des liens

Bibliothèque : https://github.com/D4Vinci/Scrapling. Installe-la en suivant le **README actuel** du dépôt (`pip install`, puis l'installation des navigateurs si une commande d'installation est fournie).

Usage autorisé dans ce projet :
- **récupérer le contenu des pages web publiques citées dans les posts** (articles de presse, blogs) pour remplir `link_content` : titre, description, texte principal tronqué ;
- uniquement des pages accessibles sans compte, en respectant `robots.txt` ;
- utiliser le **fetcher HTTP simple** par défaut, le navigateur seulement pour les pages publiques qui exigent JavaScript ;
- **désactiver toute option « furtive » ou de contournement anti-robot.** Une page bloquée est ignorée et journalisée.

Limites :
- au maximum N requêtes par seconde et par domaine (configurable, défaut 0,5/s) ;
- un cache par `url_hash` pour ne jamais récupérer deux fois la même page.

---

## 5. Échantillonnage (pour obtenir de vraies communautés)

Configurable dans `config/sampling.yaml` :

1. **Domaines :** 5 à 8 domaines distincts, avec quelques chevauchements (exemples : politique, science/IA, santé, sport, climat, culture/cinéma, jeux vidéo, économie).
2. **Graines :** 20 à 30 graines par domaine et par plateforme (comptes actifs, flux Bluesky, subreddits, hashtags, chaînes, instances Mastodon).
3. **Boule de neige sur 1 à 2 sauts :** ajouter les utilisateurs qui **répondent à**, **repartagent** ou **mentionnent** les comptes graines, ou qui **sont suivis** par eux.
   - Plafond par saut (exemple : 2 000 utilisateurs ajoutés par domaine au saut 1, 5 000 au saut 2).
   - Priorité aux utilisateurs reliés à plusieurs graines.
4. **Filtre de qualité :**
   - au moins **20 posts sur la période** ;
   - langue cible (configurable : `en`, `fr` ou les deux, séparées) ;
   - exclusion des comptes très probablement automatisés (heuristiques : fréquence > X posts/heure, ratio de repartages > 95 %, bio vide + compte récent). Garder un indicateur `bot_score` plutôt que supprimer.
5. **Volume cible de l'étude principale :** 5 000 à 20 000 utilisateurs et 1 à 2 millions de posts.
   - Mode **pilote** activable : environ 2 000 fils, quelques jours de collecte.

---

## 6. Pseudonymisation et RGPD (dès l'ingestion, pas après)

- Chaque identifiant d'utilisateur, handle et identifiant de post devient un `*_uid` = **HMAC-SHA256 (clé secrète, plateforme + identifiant)**, tronqué à 24 caractères hexadécimaux.
- La clé est stockée dans `.env` (droits 600) et n'est **jamais** exportée.
- Les **@mentions dans le texte** sont remplacées par `@user_<uid>` avant stockage. Les URL de profils sont réécrites de la même façon.
- Ne pas stocker les photos de profil, les noms affichés, la géolocalisation précise ni les messages privés.
- Une table de correspondance (identifiant réel ↔ uid) n'est créée **que si** `KEEP_REID_TABLE=true`, et dans un schéma séparé à accès restreint.
- `DATA_RETENTION.md` documente la finalité, la base légale (recherche scientifique), la durée de conservation et la procédure de suppression sur demande. Fournis un script `delete_user.py <uid>`.

---

## 7. Fonctionnement en production

- **Jobs :**
  - `seed_expand` (boule de neige) ;
  - `backfill` (historique 2023 → aujourd'hui, par utilisateur, de manière incrémentale) ;
  - `threads` (reconstruction des fils) ;
  - `graph` (abonnements et interactions) ;
  - `stream` (temps réel : Jetstream pour Bluesky, streaming Mastodon) ;
  - `links` (Scrapling, optionnel) ;
  - `export`.
- **Reprise sur erreur :** curseurs sauvegardés dans `collection_runs.cursor_state`, insertions idempotentes (`ON CONFLICT DO NOTHING/UPDATE`), aucun doublon après redémarrage.
- **Parallélisme :** `asyncio` avec sémaphores par plateforme. Respect strict des limites de débit.
- **Planification dans le conteneur** (pas de systemd) : `python -m collector run --all` lance la collecte continue (stream) et relance le backfill toutes les heures (APScheduler ou boucle asyncio) ; l'export Parquet tourne chaque nuit. Le conteneur redémarre seul (`restart: unless-stopped`).
- **Surveillance :** `stats.py` affiche les posts et utilisateurs par plateforme et par mois, les erreurs, le taux de 429, l'espace disque. Alerte dans les logs si la collecte s'arrête depuis plus de 30 minutes.
- **Disque :** estime l'espace nécessaire (texte seulement : environ 1 à 2 Ko par post). Ne télécharge pas les médias par défaut.

---

## 7 bis. Filtre de pertinence par LLM (optionnel, désactivé par défaut)

- La collecte **n'utilise pas de LLM**.
- Si `ENABLE_LLM_RELEVANCE=true`, un job séparé (`python -m collector relevance --sample N`) envoie un **échantillon** de posts au LLM local (Ollama, `OLLAMA_URL`, modèle `OLLAMA_MODEL`, Qwen 14B quantifié) avec une consigne courte : « Ce post relève-t-il du domaine X ? oui/non/incertain + domaine proposé ».
- Il écrit `relevance_label` et `relevance_conf` dans une table à part, **sans jamais supprimer de données**.
- Il sert aussi à proposer des mots-clés et des comptes de départ par domaine (`python -m collector suggest-seeds --domain climat`). Les suggestions sont validées à la main avant d'être ajoutées à `config/seeds/`.
- Débit attendu : quelques posts par seconde sur un GPU de 16 Go. Ne jamais l'appliquer à tout le corpus pendant la collecte.

## 8. Post-traitement (utilise le GPU)

- **Langue :** détection par `fasttext` (modèle lid.176) ou `lingua`. Écrire `lang_detected` et la confiance.
- **Déduplication :** posts identiques ou quasi identiques (MinHash).
- **Embeddings optionnels :** un modèle sentence-transformers multilingue, par lots sur le GPU (16 Go de VRAM), sauvegardés en Parquet (`post_uid`, vecteur). À lancer uniquement sur commande (`embed.py`).
- **Export :** Parquet partitionné par `platform/année/mois` pour posts, users, edges et threads, plus un `DATASET_CARD.md` généré automatiquement (sources, période réelle couverte par plateforme, volumes, champs, limites, règles de collecte).

---

## 9. Jeu d'annotation et données de comparaison

- `make_annotation_sample.py` produit un échantillon **stratifié** par domaine et par plateforme : **400 posts** (avec leur fil) et **80 utilisateurs** (avec 20 posts chacun). Export en CSV et au format **Label Studio**.
  - Colonnes prévues pour 2 annotateurs : contexte du post, domaine(s), domaine(s) principaux de l'utilisateur.
- **Event2012 / Event2018** (bancs d'essai de détection d'événements utilisés par les meilleures méthodes publiées) : **ne pas les re-collecter.**
  - Écris `get_benchmarks.md` qui explique où trouver les versions prétraitées publiées avec les codes de recherche (dépôts HISEvent, KPGNN, etc.).
  - Écris un script qui les charge dans le même schéma (`platform='twitter_benchmark'`). Vérifie les liens en ligne.

---

## 10. Livrables, tests et critères d'acceptation

**Arborescence attendue :**
```
sm-collector/
  connectors/ (bluesky.py, reddit.py, reddit_archive.py, mastodon.py, youtube.py, telegram.py, x_api.py, meta_cl.py, tiktok_research.py, web_links.py)
  pipeline/ (sampling.py, backfill.py, threads.py, graph.py, stream.py, pseudonymize.py, quality.py)
  db/ (schema.sql, migrations/)
  config/ (settings.yaml, sampling.yaml, seeds/*.csv)
  collector/ (__main__.py — CLI : run, export, relevance, suggest-seeds, stats)
  scripts/ (deploy_ubuntu.sh [fourni], push_to_github.sh [fourni], stats.py, embed.py, make_annotation_sample.py, delete_user.py)
  tests/
  .env.example · README.md · STATUS.md · ACCES_A_DEMANDER.md · DATA_RETENTION.md · get_benchmarks.md
```

**Tests :**
- tests unitaires de la pseudonymisation (stable, non réversible sans clé, mentions réécrites) ;
- tests de reconstruction de fil sur des fixtures ;
- tests de reprise après interruption (pas de doublon) ;
- test du respect des limites de débit (mock d'en-têtes 429).

**Critères d'acceptation :**
1. `bash scripts/deploy_ubuntu.sh`, puis `docker compose up -d --build`, démarrent tout sur un Ubuntu Server propre.
2. Le **mode pilote** sur Bluesky produit, en moins de 48 h : au moins 2 000 fils complets, le graphe des interactions entre leurs participants, et un export Parquet valide avec son `DATASET_CARD.md`.
3. Reddit (archives + API) et Mastodon fonctionnent sur au moins 2 domaines.
4. X, Meta et TikTok se désactivent proprement sans clés et s'activent dès que les identifiants sont ajoutés dans `.env`.
5. Aucun identifiant réel n'apparaît dans les exports. Vérifie-le avec un test qui cherche les handles des graines dans les fichiers Parquet.

**Ordre de travail :**
1. Squelette et schéma.
2. Pseudonymisation.
3. Bluesky de bout en bout.
4. Mode pilote.
5. Reddit.
6. Mastodon.
7. Connecteurs optionnels.
8. Scrapling (liens).
9. Post-traitement.
10. Annotation.
11. Docker : vérifier `docker compose up -d --build`, puis un redémarrage du serveur.
12. Documentation.

---

## 11. À remplir avant de lancer

- **Langue(s) cible(s) :** `en` / `fr` / les deux
- **Domaines (5 à 8) :** …
- **Graines par domaine et par plateforme** (fichiers `config/seeds/<plateforme>.csv` : `domain,kind,value`) : …
- **Période :** 2023-01-01 → aujourd'hui (+ collecte continue jusqu'à : …)
- **LLM :** filtre de pertinence activé ? (`ENABLE_LLM_RELEVANCE`, défaut false) · modèle (`OLLAMA_MODEL`, défaut qwen3:14b)
- **Clés disponibles :** Bluesky (identifiant + mot de passe d'application) · Reddit (client_id/secret) · YouTube · Telegram · X · Meta Content Library · TikTok Research
- **E-mail de contact du projet** (pour le User-Agent) : …
- **Avis éthique ou DPO de l'université obtenu :** oui / en cours
