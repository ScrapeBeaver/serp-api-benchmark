# SERP benchmark: summary

`summary.json` is every figure of the results page, computed from all the records by the
benchmark's own code after each run. This branch holds only the latest one, in a single
commit that is replaced every time.

The records it is computed from are on the `benchmark-data` branch, and the code that
computes it is on the default branch: `serp-bench site --records <folder>` rebuilds it.
