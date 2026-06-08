import inspect
from pyannote.audio import Pipeline
import os

# Load env
env_file = ".env"
if os.path.exists(env_file):
    with open(env_file) as f:
        for line in f:
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

token = os.environ.get("HUGGINGFACE_TOKEN") or os.environ.get("HF_TOKEN")

pipeline = Pipeline.from_pretrained(
    "pyannote/speaker-diarization-3.1",
    token=token,
)

# What is pipeline.embedding (the string)?
print(f"pipeline.embedding (str): {pipeline.embedding!r}")
print(f"pipeline.legacy: {pipeline.legacy}")
print(f"pipeline.embedding_batch_size: {pipeline.embedding_batch_size}")
print(f"pipeline.embedding_exclude_overlap: {pipeline.embedding_exclude_overlap}")

print(f"\n--- get_embeddings signature ---")
print(inspect.signature(pipeline.get_embeddings))

print(f"\n--- get_embeddings docstring ---")
print(pipeline.get_embeddings.__doc__ or "(no docstring)")

# Also see source if accessible
print(f"\n--- get_embeddings source location ---")
try:
    print(inspect.getsourcefile(pipeline.get_embeddings))
except Exception as e:
    print(f"(error: {e})")