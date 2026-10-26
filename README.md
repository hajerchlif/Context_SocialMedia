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
| Telegram | Sans compte : pages publiques `t.me/s/<canal>` + snowball (canaux cités ou transférés). **Recommandé** : API officielle gratuite (`TELEGRAM_API_ID`/`HASH`) → recherche par mots-clés, groupes publics (vrais utilisateurs), commentaires, historique complet | ✅ (API si configurée) |
| Reddit | API officielle OAuth gratuite plus dumps `.zst` pour 2023-2024 | ✅ si clés ou dumps |
| YouTube | clé API gratuite (10 000 unités par jour) | ✅ si clé |
| **X, Weibo, Reddit, Facebook, Instagram (jeux de données publiés)** | Qwen cherche sur Hugging Face et Zenodo, lit chaque fiche, garde les jeux sous licence ouverte avec le texte des posts, importe les lignes 2023→aujourd'hui. Tout est tracé dans `dataset_imports` | ✅ (automatique) |
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
docker compose run --rm collector python -m collector datasets -p x -p weibo  # Qwen cherche et importe des jeux X/Weibo
docker compose run --rm collector python -m collector datasets-status        # importés / rejetés et pourquoi
docker compose run --rm -it collector python -m collector telegram-login     # connexion API Telegram (une seule fois)
```

### Telegram : API officielle (gratuite, conseillée)

1. Sur https://my.telegram.org → « API development tools » → créez une app → notez `api_id` et `api_hash`.
2. Mettez-les dans `.env` : `TELEGRAM_API_ID=...` et `TELEGRAM_API_HASH=...`.
3. `docker compose run --rm -it collector python -m collector telegram-login` (numéro de téléphone + code reçu sur Telegram). La session est gardée dans `data/telegram.session`.
4. `docker compose up -d --build`. Qwen propose alors des **mots-clés**, la recherche de Telegram trouve les canaux et groupes publics, et la collecte lit l'historique depuis 2023 et les commentaires.

Le collecteur reste en lecture seule : il ne rejoint aucun groupe, n'écrit rien et ne contacte personne. Utilisez un compte dédié à la recherche.

### X et Weibo : jeux de données publiés

À chaque cycle (`DATASETS_AUTO=true`), Qwen propose des requêtes par domaine, puis le collecteur :
- cherche sur Hugging Face et Zenodo ;
- écarte les jeux à accès restreint, sans licence ouverte (`DATASETS_ALLOW_UNKNOWN_LICENSE=false`) ou collectés par scraping/réseaux de minage comme Bittensor (`DATASETS_ALLOW_SCRAPED=false`) ;
- fait lire la fiche à Qwen : vraie plateforme, texte présent, domaine utile ;
- importe les posts datés entre `DATE_FROM` et aujourd'hui (`DATASETS_RESPECT_DATES=true`), avec auteurs pseudonymisés, dans `posts` (plateforme `x` ou `weibo`, `source_method = dataset:hf:<id>`).

Autres plateformes possibles : `DATASETS_PLATFORMS=x,weibo,reddit,facebook,instagram`. Pour Reddit, la politique de Reddit (Responsible Builder Policy) considère comme non conforme la recherche sur des données Reddit obtenues hors du programme Reddit for Researchers : vérifiez avec votre comité d'éthique avant d'utiliser ces jeux.

Chaque jeu vu est enregistré dans la table `dataset_imports` (licence, provenance, raison de Qwen, lignes importées) : c'est la liste à citer dans la thèse. Vérifiez la licence de chaque jeu importé avant publication.

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
