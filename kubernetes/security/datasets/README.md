# Security adviser dataset contract

Site-owned training data is not stored in this repository. A dataset contains
one immutable `manifest.json` and one JSONL records file. Validate it before a
GPU is allocated:

```bash
./kubernetes/tools/ai-workflow model validate-dataset \
  --manifest /path/to/dataset/manifest.json \
  --dataset-root /path/to/dataset \
  --output /path/to/evidence/dataset-validation.json
```

Each JSONL record must include one final assistant response, an explicit
license and confirmed permission, target and split identities, evidence IDs,
allowed and forbidden operations, and a digest of the canonical record before
the `record_digest` field is added. Split by fixture and vulnerability lineage,
not by prompt text alone.

The public tool intentionally supplies the schema and validator but no private
findings, credentials, customer evidence, undisclosed issues, or site-owned
training corpus.
