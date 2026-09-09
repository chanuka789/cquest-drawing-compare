"""Ground-truth test harness for Phase 4 alignment (Task 4.0).

`synthetic` renders known-transform test pairs from a source PDF and records
the exact 3x3 ground-truth matrix; `benchmark` runs an alignment function
across a matrix of such pairs and reports where its verdicts lied.
"""
