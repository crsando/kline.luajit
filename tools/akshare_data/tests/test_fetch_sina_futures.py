from datetime import date

import pandas as pd
import pytest

from fetch_sina_futures import (
    CLOSED_FLAG,
    NIGHT_FLAG,
    default_data_home,
    normalize_sina_frame,
    read_existing_kline,
    read_existing_raw,
    write_kline_days,
    write_raw_years,
)


def source_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            ["2026-07-24 09:01:00", 3200, 3202, 3199, 3201, 10, 1000],
            ["2026-07-24 21:01:00", 3210, 3212, 3209, 3211, 20, 1010],
            ["2026-07-25 00:01:00", 3220, 3221, 3219, 3220, 30, 1020],
        ],
        columns=["datetime", "open", "high", "low", "close", "volume", "hold"],
    )


def test_end_labeled_minutes_are_shifted_and_mapped_to_trading_day() -> None:
    result = normalize_sina_frame(
        source_frame(),
        period=1,
        timestamp_mode="end",
        trade_dates=[date(2026, 7, 24), date(2026, 7, 27)],
    )

    assert result["bar_time"].dt.strftime("%Y-%m-%d %H:%M:%S").tolist() == [
        "2026-07-24 09:00:00",
        "2026-07-24 21:00:00",
        "2026-07-25 00:00:00",
    ]
    assert result["trading_day"].tolist() == [20260724, 20260727, 20260727]
    assert result["flags"].tolist() == [
        CLOSED_FLAG,
        CLOSED_FLAG | NIGHT_FLAG,
        CLOSED_FLAG | NIGHT_FLAG,
    ]
    assert result["turnover"].eq(0).all()
    assert result["tick_count"].eq(0).all()
    assert result["open_interest"].tolist() == [1000, 1010, 1020]


def test_start_timestamp_mode_does_not_shift() -> None:
    result = normalize_sina_frame(
        source_frame().iloc[:1],
        period=1,
        timestamp_mode="start",
        trade_dates=[date(2026, 7, 24)],
    )
    assert result.iloc[0]["bar_time"] == pd.Timestamp("2026-07-24 09:01:00")


def test_duplicate_source_datetimes_are_rejected() -> None:
    duplicated = pd.concat([source_frame().iloc[:1], source_frame().iloc[:1]])
    with pytest.raises(ValueError, match="duplicate datetime"):
        normalize_sina_frame(
            duplicated,
            period=1,
            timestamp_mode="end",
            trade_dates=[date(2026, 7, 24)],
        )


def test_day_files_are_upserted_by_bar_time(tmp_path) -> None:
    normalized = normalize_sina_frame(
        source_frame().iloc[:1],
        period=1,
        timestamp_mode="end",
        trade_dates=[date(2026, 7, 24)],
    )
    paths = write_kline_days(
        normalized,
        tmp_path,
        symbol="RB0",
        period=1,
        timestamp_mode="end",
        price_decimals=1,
    )
    corrected = normalized.copy()
    corrected.loc[0, "close"] = 3299
    write_kline_days(
        corrected,
        tmp_path,
        symbol="RB0",
        period=1,
        timestamp_mode="end",
        price_decimals=1,
    )

    loaded = read_existing_kline(paths[0])
    assert len(loaded) == 1
    assert loaded.iloc[0]["close"] == 3299
    assert paths[0] == (tmp_path / "canonical/v1/akshare-sina/RB0/1m/2026/RB0_1m_20260724.csv")
    assert paths[0].read_text(encoding="utf-8").startswith("# symbol=RB0 period=1m")
    assert (tmp_path / "locks/canonical-v1__akshare-sina__RB0__1m__20260724.lock").exists()

    with pytest.raises(ValueError, match="source_time='end'"):
        write_kline_days(
            corrected,
            tmp_path,
            symbol="RB0",
            period=1,
            timestamp_mode="start",
            price_decimals=1,
        )


def test_raw_files_are_partitioned_and_upserted(tmp_path) -> None:
    paths = write_raw_years(source_frame(), tmp_path, symbol="RB0", period=1)
    corrected = source_frame()
    corrected.loc[0, "close"] = 3300
    write_raw_years(corrected, tmp_path, symbol="RB0", period=1)

    assert paths == [tmp_path / "raw/akshare-sina/RB0/1m/2026/RB0_1m_sina.csv"]
    loaded = read_existing_raw(paths[0])
    assert len(loaded) == 3
    assert loaded.iloc[0]["close"] == 3300
    assert (tmp_path / "locks/raw__akshare-sina__RB0__1m__2026.lock").exists()


def test_data_home_environment_precedence(monkeypatch, tmp_path) -> None:
    explicit = tmp_path / "explicit"
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("KLINE_DATA_HOME", str(explicit))
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    assert default_data_home() == explicit

    monkeypatch.delenv("KLINE_DATA_HOME")
    assert default_data_home() == xdg / "kline"
