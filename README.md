# church-meeting-assistant

## Known limitations (v1)

### Theme repetition across chunks
Long topics (>10 minutes of discussion) may appear as separate headings
in 2-3 chunks because the 25-min chunking with 5-min overlap can't merge
them automatically.

Example: "Великдень" discussion spans chunks 00, 01, 02.

**Workaround for v1:** users see all variants and mentally merge.
**Future v2:** add smart topic deduplication (fuzzy matching or
two-pass Gemma approach).

### Stuttering speakers may have transcript gaps
Whisper drops content for speakers with stuttering (logoneurosis).
Affects ~1-2% of total speaking time. Specifically observed with one
team member.

### Some chunks may need sub-chunking
Approximately 1 in 10 chunks fails on first Gemma pass and requires
sub-chunking fallback (5-min sub-chunks). Total processing adds ~5-10 min.