"""Offline research pipeline (review STR-01): labels, splits and frozen calibration.

The coordinator never trains. This package turns recorded, causally replayed
data into the frozen artifacts the coordinator consumes: robust scalers (with
clip-rate diagnostics) and a ``CalibrationTable`` of complete-policy net values
with day-block bootstrap lower bounds.
"""
from .calibration import (build_calibration_rows, calibration_table_event, day_block_lower_bound,
                          fit_scalers)
from .labels import LabelPolicy, label_intent
from .splits import Fold, walk_forward

__all__ = ["LabelPolicy", "label_intent", "Fold", "walk_forward", "day_block_lower_bound",
           "build_calibration_rows", "fit_scalers", "calibration_table_event"]
