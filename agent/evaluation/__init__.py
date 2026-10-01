"""
Strategy evaluation and calibration.

Both modules here spend most of their effort refusing to conclude
things.

`calibration` asks whether hypothesis strength is predictive. Until the
strength bands each hold enough trades to compare, the answer is "not
tested" - which is different from "does not work", and the distinction
matters because the second would justify removing the score.

`sweep` runs parameter comparisons and then argues against its own
results. Trying twenty settings and reporting the best produces an
apparent improvement even when every setting is identical, because the
maximum of twenty noisy measurements is positive by construction. The
module quantifies how large that selection effect should be and requires
the winner to beat it AND survive data it was not selected on.

Neither module can authorise a change. The strongest thing a sweep can
return is CANDIDATE_FOR_CHANGE.
"""
from .calibration import (
    DEFAULT_BUCKETS, MIN_BUCKET_SIZE, Bucket, MonotonicityVerdict,
    assess, bucket_by_strength, intervals_overlap,
    strength_vs_outcome_correlation,
)
from .sweep import (
    DEFAULT_HOLDOUT, MIN_TRADES_PER_ARM, Arm, Recommendation,
    expected_best_of_n, run, split_holdout,
)

__all__ = [
    "DEFAULT_BUCKETS", "DEFAULT_HOLDOUT", "MIN_BUCKET_SIZE",
    "MIN_TRADES_PER_ARM", "Arm", "Bucket", "MonotonicityVerdict",
    "Recommendation", "assess", "bucket_by_strength",
    "expected_best_of_n", "intervals_overlap", "run", "split_holdout",
    "strength_vs_outcome_correlation",
]
