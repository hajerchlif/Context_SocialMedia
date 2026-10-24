# Context_SocialMedia — Collecteur de données de réseaux sociaux (2023–2026)

Collecteur multi-plateforme pour une recherche doctorale : contexte des publications → profils d'intérêts → domaines sémantiques ouverts → communautés.

**Plateformes :**
- Bluesky (principale) ;
- Reddit (API officielle + archives) ;
- Mastodon ;
- YouTube, Telegram (optionnels) ;
- X, Facebook/Instagram (Meta Content Library), TikTok Research : activés uniquement avec un accès chercheur officiel.

## Démarrage rapide (Ubuntu Server + GPU)

```bash
# 1. Installer Docker (+ GPU) et récupérer le dépôt
curl -fsSL https://raw.githubusercontent.com/hajerchlif/Context_SocialMedia/main/scripts/deploy_ubuntu.sh | bash
#    (options : WITH_GPU=0 pour ignorer le GPU, WITH_LLM=1 pour démarrer Qwen 14B)

# 2. Ajouter vos clés
nano ~/Context_SocialMedia/.env
nano ~/Context_SocialMedia/config/seeds/*.csv     # domaines et comptes de départ

# 3. Générer le code du collecteur avec Claude Code (une seule fois)
cd ~/Context_SocialMedia && claude                # puis lui donner PROMPT_Collecteur_Multiplateforme.md

# 4. Lancer
docker compose up -d --build
docker compose logs -f collector
```

## Commandes utiles

| Action | Commande |
|---|---|
| État des services | `docker compose ps` |
| Logs | `docker compose logs -f collector` |
| Export Parquet | `docker compose --profile tools run --rm export` |
| LLM local (analyse) | `docker compose --profile llm up -d ollama && docker compose exec ollama ollama pull qwen3:14b` |
| Arrêter | `docker compose down` (les données restent dans le volume `pgdata`) |

## Matériel
- **Collecte :** processeur et réseau seulement. Pas besoin de GPU ni de LLM.
- **GPU 16 Go :** sert à l'analyse (Qwen 14B quantifié en 4 bits, environ 9–10 Go de VRAM ; embeddings) et au filtre de pertinence optionnel (`ENABLE_LLM_RELEVANCE`).

## Règles de collecte
- Accès officiels uniquement. Pas de connexion à un compte pour scraper, pas de contournement de CAPTCHA ou d'anti-robot.
- `robots.txt` et limites de débit respectés.
- Pseudonymisation dès l'ingestion (HMAC). Aucun identifiant réel dans les exports.
- Voir `DATA_RETENTION.md` (généré) et l'avis éthique ou le DPO de l'université.

## Contenu
- `docker-compose.yml`, `Dockerfile`, `requirements.txt`, `.env.example` : déploiement.
- `scripts/deploy_ubuntu.sh` : installation sur le serveur.
- `scripts/push_to_github.sh` : envoi vers GitHub.
- `config/seeds/*.csv` : comptes, flux et subreddits de départ par domaine (modèles à remplir).
- `PROMPT_Collecteur_Multiplateforme.md` : spécification complète que Claude Code utilise pour écrire `collector/`.
