"""Diagnostic: check if pyannote 4.x exposes speaker embeddings."""
import os
from pathlib import Path
from pyannote.audio import Pipeline

# Load .env file if present (no python-dotenv dep)
env_file = Path(".env")
if env_file.exists():
    for line in env_file.read_text().splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

token = os.environ.get("HUGGINGFACE_TOKEN") or os.environ.get("HF_TOKEN")
if not token:
    print("❌ No HUGGINGFACE_TOKEN or HF_TOKEN found in .env or environment")
    exit(1)

print(f"✓ Token loaded: {token[:8]}...")

pipeline = Pipeline.from_pretrained(
    "pyannote/speaker-diarization-3.1",
    token=token,
)

# Use a short test file if available, else full audio
audio_paths = [
    "data/short_test.m4a",
    "data/test_baseline.m4a",
]
audio_path = next((p for p in audio_paths if Path(p).exists()), None)
if not audio_path:
    print(f"❌ No audio file found. Tried: {audio_paths}")
    exit(1)

print(f"\nRunning pyannote on {audio_path}...")
print("(this may take a few minutes for full audio)")

# Try with return_embeddings=True
try:
    output = pipeline(audio_path, return_embeddings=True)
    print(f"\n✓ Got output with return_embeddings=True")
except TypeError as e:
    print(f"\n⚠ return_embeddings=True not supported: {e}")
    print("Falling back to default call...")
    output = pipeline(audio_path)

print(f"\nOutput type: {type(output)}")
print(f"Output attributes: {[x for x in dir(output) if not x.startswith('_')]}")

# Check for embeddings under various names
for attr_name in ['speaker_embeddings', 'embeddings', 'speakers']:
    if hasattr(output, attr_name):
        attr = getattr(output, attr_name)
        print(f"\n✓ Found .{attr_name}")
        print(f"  Type: {type(attr)}")
        if hasattr(attr, 'shape'):
            print(f"  Shape: {attr.shape}")
        elif isinstance(attr, dict):
            print(f"  Dict keys: {list(attr.keys())[:5]}")
            first_val = next(iter(attr.values()), None)
            if first_val is not None and hasattr(first_val, 'shape'):
                print(f"  First value shape: {first_val.shape}")

# Try accessing diarization itself
print(f"\nDiarization access:")
if hasattr(output, 'speaker_diarization'):
    print(f"  ✓ output.speaker_diarization works")
    diar = output.speaker_diarization
    print(f"    Type: {type(diar)}")
elif hasattr(output, '__iter__'):
    print(f"  output is iterable (probably the diarization itself)")

# Check ordering: which embedding goes with which speaker?
print(f"\n--- Speaker label ↔ embedding mapping ---")

# Get unique speaker labels from diarization
labels = output.speaker_diarization.labels()
print(f"Diarization labels: {labels}")

# Embeddings shape
print(f"Embeddings shape: {output.speaker_embeddings.shape}")

# pyannote convention: embeddings are ordered by label sort order
# Verify: number of labels should == number of embeddings
if len(labels) == output.speaker_embeddings.shape[0]:
    print(f"✓ Matches: {len(labels)} labels, {output.speaker_embeddings.shape[0]} embeddings")
    print(f"\nMapping (assumed alphabetical):")
    for i, label in enumerate(sorted(labels)):
        emb = output.speaker_embeddings[i]
        print(f"  {label} → embedding[{i}], first 5 values: {emb[:5]}")
else:
    print(f"❌ Mismatch: {len(labels)} labels but {output.speaker_embeddings.shape[0]} embeddings")