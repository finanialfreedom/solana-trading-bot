"""Regression checks that sweep arms remain distinct when .env defaults change."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from tools.interleaved_sweep import (  # noqa: E402
    AUTO_ADVANCE,
    ENTRY_SCHEDULE,
    PRESSURE_SCHEDULE,
    QUALITY_SCHEDULE,
    SCHEDULES,
)

ENTRY_KEYS = (
    "MIN_UNIQUE_BUYERS",
    "MIN_CONFIRM_BUY_SOL",
    "MIN_CONFIRM_MC_USD",
    "MIN_CONFIRM_NET_SOL",
    "MAX_CONFIRM_SELL_RATIO",
)


def number(value: object) -> float:
    return float(str(value).lower().replace("true", "1").replace("false", "0"))


def effective(schedule: list[tuple[str, dict[str, str]]]) -> list[tuple[str, dict[str, float]]]:
    return [
        (
            label,
            {
                key: number(overrides.get(key, getattr(config, key)))
                for key in ENTRY_KEYS
            },
        )
        for label, overrides in schedule
    ]


def check(label: str, condition: bool) -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {label}")
    if not condition:
        raise AssertionError(label)


def assert_unique(name: str, rows: list[tuple[str, dict[str, float]]]) -> None:
    signatures = [tuple(values[key] for key in ENTRY_KEYS) for _label, values in rows]
    check(f"{name}: no redundant effective arms", len(signatures) == len(set(signatures)))


def assert_adjacent_one_variable(
    name: str, rows: list[tuple[str, dict[str, float]]]
) -> None:
    for (left_name, left), (right_name, right) in zip(rows, rows[1:]):
        changed = [key for key in ENTRY_KEYS if left[key] != right[key]]
        check(
            f"{name}: {left_name} -> {right_name} changes one variable ({changed})",
            len(changed) == 1,
        )


def main() -> None:
    check("quality auto-advances to entry then pressure",
          AUTO_ADVANCE.get("quality") == ("entry", "pressure"))
    check("every auto-advance target is a real schedule",
          all(target in SCHEDULES for targets in AUTO_ADVANCE.values() for target in targets))

    pressure = effective(PRESSURE_SCHEDULE)
    assert_unique("pressure", pressure)
    expected_pressure = [
        (0.0, 0.0),
        (1.0, 0.0),
        (0.0, 0.5),
        (1.0, 0.5),
    ]
    check(
        "pressure: off/net-only/ratio-only/both are explicit",
        [
            (values["MIN_CONFIRM_NET_SOL"], values["MAX_CONFIRM_SELL_RATIO"])
            for _label, values in pressure
        ]
        == expected_pressure,
    )

    entry = effective(ENTRY_SCHEDULE)
    assert_unique("entry", entry)
    assert_adjacent_one_variable("entry", entry)
    first = entry[0][1]
    check("entry: A-entry-none is a true zero-filter baseline",
          all(first[key] == 0.0 for key in ENTRY_KEYS))

    quality = effective(QUALITY_SCHEDULE)
    assert_unique("quality", quality)
    assert_adjacent_one_variable("quality", quality)
    check(
        "quality: only old, buyers15, and mc5k arms remain",
        [label for label, _values in quality]
        == ["A-quality-old", "A2-quality-buyers15", "A3-quality-mc5k"],
    )

    print("\nEffective pressure settings:")
    for label, values in pressure:
        print(f"  {label}: net={values['MIN_CONFIRM_NET_SOL']:g}, "
              f"sell_ratio={values['MAX_CONFIRM_SELL_RATIO']:g}")
    print("\nEffective entry settings:")
    for label, values in entry:
        print(f"  {label}: {values}")
    print("\nEffective quality settings:")
    for label, values in quality:
        print(f"  {label}: {values}")


if __name__ == "__main__":
    main()
