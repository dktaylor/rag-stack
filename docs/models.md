# Model management

The rag-stack uses Ollama for embeddings — the same Ollama daemon that serves
chat models. No separate model downloads are required; Ollama handles caching.

HuggingFace models can optionally be mounted into the container for reranking,
but are not needed for the default embedding setup.

## Fetching models

```bash
# Pull all models listed in models.conf (Ollama models are pulled via 'ollama pull')
fetch-model

# Same via the rag CLI
rag fetch

# Pull a specific model by ID
fetch-model nomic-embed-text
rag fetch nomic-embed-text

# List configured models and show which are available
fetch-model --list
rag fetch --list
```

## models.conf

Located at `/opt/rag-stack/models.conf` (deployed) and `~/Projects/rag-stack/models.conf` (source).

Format:

```
# comment
purpose/source   model-id
```

| Purpose | Source | Notes |
|---------|--------|-------|
| `embedding` | `ollama` | Pulled via `ollama pull`; uses host Ollama daemon |
| `embedding` | `huggingface` | Downloaded to /opt/rag-stack/models/; requires container network |
| `reranking` | `huggingface` | Optional cross-encoder reranking |
| `inference` | `ollama` | Listed for reference only |

## Swapping the embedding model

Changing the embedding model requires re-indexing all knowledge bases because
vector dimensions differ between models.

```bash
# 1. Edit models.conf — comment out old embedding, uncomment new one
$EDITOR /opt/rag-stack/models.conf

# 2. Pull/download the new model
fetch-model

# 3. Restart Open WebUI to load the new model
rag stop && rag start

# 4. Re-index all projects (old vectors are now stale)
rag index ~/Projects/my-project
```

## Recommended models

| Purpose | Model | Source | Dimensions | Notes |
|---------|-------|--------|-----------|-------|
| Embedding (default) | `nomic-embed-text` | ollama | 768 | Fast, no container network needed |
| Embedding (quality) | `BAAI/bge-large-en-v1.5` | huggingface | 1024 | Best quality; requires container internet |
| Embedding (fast) | `BAAI/bge-small-en-v1.5` | huggingface | 384 | Smaller; requires container internet |
| Reranking | `BAAI/bge-reranker-base` | huggingface | — | Cross-encoder; improves result ordering |

## Inference models (Ollama)

Ollama inference models are managed separately from embeddings. The
`inference/ollama` entries in `models.conf` are documentation only.

```bash
ollama pull qwen2.5-coder:7b-instruct-q5_K_M
ollama list
```

## Kickstart / fresh install

The `fedora-proart-kickstart` step 46 clones rag-stack and runs `install.sh`.
After first boot:

```bash
fetch-model        # pulls nomic-embed-text via Ollama
rag start          # starts Open WebUI + Qdrant
# open http://localhost:3000, create account, generate API token
# add token to /opt/rag-stack/.env as OPENWEBUI_TOKEN
rag index ~/Projects/<name>   # index a project
```
