"""Walk-forward splits by trading day (specification section 15).

Random splits of overlapping seconds-level samples are forbidden. Folds roll by
the test length; ``embargo`` days are dropped between segments so that labels
(waiting + holding) of one segment never reach into the next. The last
``holdout`` days are excluded from every fold and used once for the final test.
"""
from dataclasses import dataclass
from datetime import date
from typing import Iterable


@dataclass(frozen=True)
class Fold:
    train: tuple[date, ...]
    validate: tuple[date, ...]
    test: tuple[date, ...]


def walk_forward(days: Iterable[date], *, train: int = 40, validate: int = 10, test: int = 10,
                 holdout: int = 20, embargo: int = 1) -> tuple[list[Fold], tuple[date, ...]]:
    for name, value in (("train", train), ("validate", validate), ("test", test)):
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    for name, value in (("holdout", holdout), ("embargo", embargo)):
        if type(value) is not int or value < 0:
            raise ValueError(f"{name} must be a nonnegative integer")
    ordered = sorted(set(days))
    if holdout:
        usable, final = ordered[:-holdout - embargo] if len(ordered) > holdout + embargo else [], tuple(ordered[-holdout:])
    else:
        usable, final = ordered, ()
    folds = []
    start = 0
    span = train + embargo + validate + embargo + test
    while start + span <= len(usable):
        a = start
        b = a + train
        c = b + embargo
        d = c + validate
        e = d + embargo
        folds.append(Fold(tuple(usable[a:b]), tuple(usable[c:d]), tuple(usable[e:e + test])))
        start += test
    return folds, final
