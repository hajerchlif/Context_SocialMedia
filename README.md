# Context_SocialMedia

Collecteur de données de réseaux sociaux **2023 → aujourd'hui** pour la recherche doctorale
*contexte des posts → profils d'intérêts → domaines sémantiques ouverts → communautés*.

**100 % local, gratuit, tout dans Docker.** Aucune API payante, aucun service cloud :
le « cerveau » est **Qwen3 14B** (Ollama, sur votre GPU) qui pilote la collecte.

## Ce que fait le cerveau (Qwen 14B)

| Étape | Rôle du LLM local |
|---|---|
| 1. discover | propose des requêtes, hashtags, subreddits et chaînes pour chaque domaine de `config/domains.yaml` |
| 2. expand | lit la bio et les posts récents de chaque compte candidat et ne garde que les vrais individus actifs dans le domaine. Ces comptes sont ensuite rétro-collectés depuis le 01/01/2023, puis on fait un snowball (follows, mentions) sur 1 à 2 sauts |
| 3. threads | — (récupération des arbres de conversation complets) |
| 4. label | annote chaque post : domaine (ouvert, `new_domain`), pertinence, sujet, intention, stance, entités |

## Plateformes

| Plateforme | Accès (gratuit) | Statut |
|---|---|---|
| **Bluesky** | API publique AppView. Avec `BSKY_APP_PASSWORD` (gratuit), la recherche plein texte de posts est en plus activée | ✅ principale (testé) |
| **Mastodon** | API publique. Respecte `indexable`, `discoverable` et `#nobot` | ✅ (testé) |
| **Lemmy** (alternative à Reddit) | API publique, sans clé | ✅ (testé) |
| **Stack Exchange** (170 sites Q&R) | API gratuite : 300 requêtes/jour sans clé, 10 000 avec une clé gratuite (`STACKEXCHANGE_KEY`). Les tags de chaque utilisateur sont stockés dans `users.interest_tags` | ✅ (testé) |
| Telegram | pages publiques `t.me/s/<canal>` (sans compte), robots.txt respecté | ✅ optionnel |
| Reddit | API officielle OAuth gratuite plus dumps `.zst` pour 2023-2024 | ✅ si clés ou dumps |
| YouTube | clé API gratuite (10 000 unités par jour) | ✅ si clé |
| X, Facebook/Instagram, TikTok | **accès chercheur officiel uniquement** (pas de scraping avec connexion ni de contournement anti-bot) | ⏸ auto-désactivé |

## Démarrage (Ubuntu + GPU NVIDIA)

```bash
curl -fsSL https://raw.githubusercontent.com/hajerchlif/Context_SocialMedia/main/scripts/deploy_ubuntu.sh | bash
```

Le script fait quatre choses :
- il installe Docker et le NVIDIA Container Toolkit ;
- il clone le dépôt ;
- il crée `.env` avec des secrets aléatoires ;
- il lance `docker compose up -d --build`, ce qui démarre PostgreSQL, Ollama, le téléchargement de `qwen3:14b` (~9 Go) et le collecteur.

Il faut ensuite éditer `config/domains.yaml` (vos domaines) et `config/seeds/*.csv` (comptes, hashtags et subreddits de départ, facultatif car Qwen en propose). Pour une recherche Bluesky optimale, mettez aussi `BSKY_HANDLE` et `BSKY_APP_PASSWORD` dans `.env`.

## Tableau de bord

Ouvrez **http://<ip-du-serveur>:8080** (ex. `http://192.168.1.71:8080`) depuis n'importe quel appareil du réseau local.
Il affiche : posts, comptes, fils et graphe, posts par mois depuis 2023, jobs en cours, file d'attente du snowball, état de Qwen sur le GPU, domaines, sujets et requêtes proposées par Qwen. Il se rafraîchit toutes les 10 s.
Il ne montre que des chiffres agrégés, sans texte de post ni identifiant. Pour le protéger, ajoutez `DASHBOARD_PASSWORD=...` dans `.env` (utilisateur : `admin`).

## Commandes (toutes dans Docker)

```bash
docker compose logs -f collector                                   # suivre la collecte
docker compose run --rm collector python -m collector propose      # Qwen propose des requêtes
docker compose run --rm collector python -m collector seeds        # voir les seeds (manuels + LLM)
docker compose run --rm collector python -m collector run --all -p bluesky   # un cycle, Bluesky seul
docker compose run --rm collector python -m collector stats
docker compose run --rm collector python -m collector reddit-archives        # ingérer data/reddit_dumps/*.zst
docker compose --profile tools run --rm export                     # Parquet -> data/export/
```

Par défaut, le collecteur fait un cycle complet toutes les 6 h (`run --all --loop 6`). `PILOT_MODE=true` limite chaque cycle à 60 comptes et ~2 000 fils. Mettez `false` pour la collecte complète.

## Données et éthique

- **Pseudonymisation dès l'ingestion** : HMAC-SHA256 (`HMAC_SECRET`) sur tous les identifiants. Les @mentions du texte sont réécrites en `@user_<uid>`.
- **Tables exportées** : `posts` (Parquet partitionné plateforme/année/mois), `users`, `edges` (reply, mention, repost, quote, follow) et `llm_labels`.
- **Jamais exportés** : `crawl_queue`, `seeds` et `reid_map`, qui contiennent les identifiants bruts nécessaires aux appels API.
- **Comportement réseau** : rate-limits respectés (Retry-After, back-off), User-Agent avec contact, arrêt en cas de blocage et aucun contournement.
- Ne jamais committer `.env`.

## Développement

```bash
pip install -r requirements.txt && HMAC_SECRET=x pytest -q
```
