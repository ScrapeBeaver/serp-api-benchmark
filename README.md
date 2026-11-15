# SERP benchmark: records

One file per run, written by the benchmark and never edited afterwards:

    raw/YYYY/MM/DD/run-HHMM.jsonl.gz

Each line is one request to one provider: what was asked, when, how long it took, which
check decided its outcome, and the site and position of every result. Addresses are stored
as a fingerprint, not in full, and no provider response is stored here.

`serp-bench verify <file>` recomputes the report of a run from its file.
The code that writes these files is on the default branch of this repository.
