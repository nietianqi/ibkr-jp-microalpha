"""Offline research pipeline (review STR-01): labels, splits and frozen calibration.

The coordinator never trains. This package turns recorded, causally replayed
data into the frozen artifacts the coordinator consumes: robust scalers (with
clip-rate diagnostics) and a ``CalibrationTable`` of complete-policy net values
with day-block bootstrap lower bounds.
"""
from .calibration import (build_calibration_rows, calibration_table_event, day_block_lower_bound,
                          fit_scalers)
from .labels import (LabelPolicy, QuoteBaselineLabel, ReplayIntentLabel, label_intent,
                     quote_baseline_label, replay_intent_labels)
from .splits import Fold, walk_forward

__all__ = ["LabelPolicy", "label_intent", "Fold", "walk_forward", "day_block_lower_bound",
           "build_calibration_rows", "fit_scalers", "calibration_table_event",
           "QuoteBaselineLabel", "ReplayIntentLabel", "quote_baseline_label", "replay_intent_labels"]
