"""Evaluation: asymmetric interval scorer, WER, human label files, fixture manifest.

Nothing in this package fabricates ground truth.  Label files carry their provenance
(`drafted_by`, `status`) and a label that no human has finished is never used as gold
unless the caller explicitly asks for provisional scoring.
"""
