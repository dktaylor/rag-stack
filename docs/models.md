# Model management

The rag-stack downloads HuggingFace models to the host filesystem and mounts
them into the Open WebUI container read-only. The container starts with
`HF_HUB_OFFLINE=1` — it will not attempt any network calls to HuggingFace.
Models must be pre-fetched with `fetch-model` before starting the stack.

## Directory layout

```
/opt/rag-stack/
├── models.conf          ← list of configured models (source of truth)
└── models/              ← downloaded model cache (mounted into container)
    └── models--BAAI--bge-large-en-v1.5/
        └── snapshots/
            └── <hash>/  ← model weights, tokenizer, config
```

The `models/` directory is mounted into the Open WebUI container at
`/root/.cache/huggingface/hub/` — the standard HuggingFace cache location.

## Fetching models

```bash
# Download all models listed in models.conf (recommended after fresh install)
fetch-model

# Same via the rag CLI
rag fetch

# Download a specific model by ID
fetch-model BAAI/bge-large-en-v1.5
rag fetch BAAI/bge-large-en-v1.5

# List configured models and show which are cached
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
| `embedding` | `huggingface` | Downloaded by fetch-model, mounted into container |
| `reranking` | `huggingface` | Downloaded by fetch-model, optional |
| `inference` | `ollama` | Listed for reference only; pull with `ollama pull` |

## Swapping the embedding model

Changing the embedding model requires re-indexing all knowledge bases because
vector dimensions differ between models.

```bash
# 1. Edit models.conf — comment out old embedding, uncomment new one
$EDITOR /opt/rag-stack/models.conf

# 2. Download the new model
fetch-model

# 3. Restart Open WebUI to load the new model
rag restart open-webui

# 4. Re-index all projects (old vectors are now stale)
rag index ~/Projects/my-project
```

## Recommended models

| Purpose | Model | Dimensions | Size | Notes |
|---------|-------|-----------|------|-------|
| Embedding (default) | `BAAI/bge-large-en-v1.5` | 1024 | ~1.3 GB | Best quality for English retrieval |
| Embedding (fast) | `BAAI/bge-small-en-v1.5` | 384 | ~130 MB | Good quality, 10× smaller |
| Embedding (tiny) | `sentence-transformers/all-MiniLM-L6-v2` | 384 | ~90 MB | Pre-baked into Open WebUI image |
| Reranking | `BAAI/bge-reranker-base` | — | ~280 MB | Cross-encoder; improves result ordering |

## Inference models (Ollama)

Ollama models are managed separately. The `inference/ollama` entries in
`models.conf` are documentation only — `fetch-model` will list them but
will not pull them.

```bash
# Pull an Ollama model manually
ollama pull qwen2.5-coder:7b-instruct-q5_K_M

# List pulled models
ollama list
```

## Kickstart / fresh install

The `fedora-proart-kickstart` step 46 clones rag-stack and runs `install.sh`,
which creates `/opt/rag-stack/models/` but does not fetch models (network
may not be reliable during %post). After first boot:

```bash
fetch-model        # downloads models listed in models.conf
rag start          # starts the stack with models mounted
```
