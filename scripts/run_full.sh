#!/bin/sh
# Start an already acquired local corpus with the verified scoped indexes.
# This command downloads nothing, indexes nothing, and preserves user overrides.
set -eu

project_directory=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_directory"

BOOKRAG_DATA_DIR=${BOOKRAG_DATA_DIR-"$project_directory/data/full"}
BOOKRAG_MODEL_CACHE=${BOOKRAG_MODEL_CACHE-"$project_directory/data/models"}
BOOKRAG_HOST=${BOOKRAG_HOST-127.0.0.1}
BOOKRAG_PORT=${BOOKRAG_PORT-8001}
BOOKRAG_GENERATION_PROFILE=${BOOKRAG_GENERATION_PROFILE-qwen3-4b-instruct-2507}
BOOKRAG_GENERATION_CONTEXT=${BOOKRAG_GENERATION_CONTEXT-8192}
BOOKRAG_SEMANTIC_OVERLAY=${BOOKRAG_SEMANTIC_OVERLAY-"$project_directory/data/semantic-overlays/ru21-live-v1/installation.json"}
BOOKRAG_SEMANTIC_BATCH_REGISTRY=${BOOKRAG_SEMANTIC_BATCH_REGISTRY-"$project_directory/data/semantic-overlays/multilingual1096-registry-v1/registry.json"}
HF_HUB_OFFLINE=${HF_HUB_OFFLINE-1}
TRANSFORMERS_OFFLINE=${TRANSFORMERS_OFFLINE-1}
PYTORCH_MPS_HIGH_WATERMARK_RATIO=${PYTORCH_MPS_HIGH_WATERMARK_RATIO-0.65}
PYTORCH_MPS_LOW_WATERMARK_RATIO=${PYTORCH_MPS_LOW_WATERMARK_RATIO-0.5}

if [ ! -f "$BOOKRAG_DATA_DIR/library.sqlite3" ]; then
    printf '%s\n' 'Acquire and index the corpus first: see docs/BULK_SOURCES.md.' >&2
    exit 1
fi
if [ ! -x "$project_directory/.venv/bin/bookrag" ]; then
    printf '%s\n' 'Install the project environment first: see README.md.' >&2
    exit 1
fi

export BOOKRAG_DATA_DIR BOOKRAG_MODEL_CACHE BOOKRAG_HOST BOOKRAG_PORT
export BOOKRAG_GENERATION_PROFILE BOOKRAG_GENERATION_CONTEXT
export BOOKRAG_SEMANTIC_OVERLAY BOOKRAG_SEMANTIC_BATCH_REGISTRY
export HF_HUB_OFFLINE TRANSFORMERS_OFFLINE
export PYTORCH_MPS_HIGH_WATERMARK_RATIO PYTORCH_MPS_LOW_WATERMARK_RATIO
exec "$project_directory/.venv/bin/bookrag" serve
