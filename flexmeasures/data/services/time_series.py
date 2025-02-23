from __future__ import annotations

# Use | instead of Union, list instead of List and tuple instead of Tuple when FM stops supporting Python 3.9 (because of https://github.com/python/cpython/issues/86399)
from typing import Any, Callable, List, Optional, Tuple, Union
from datetime import datetime, timedelta

import inflect
from flask import current_app
import pandas as pd
from sqlalchemy.orm.query import Query
import timely_beliefs as tb
from timely_beliefs.beliefs import utils as belief_utils
import isodate

from flexmeasures.data.queries.utils import simplify_index
from flexmeasures.data.models.data_sources import DataSource
from flexmeasures.utils import time_utils


p = inflect.engine()

# Signature of a callable that build queries
QueryCallType = Callable[
    [
        Tuple[str],
        Tuple[datetime, datetime],
        Tuple[Optional[timedelta], Optional[timedelta]],
        Tuple[Optional[datetime], Optional[datetime]],
        Optional[datetime],
        Optional[Union[int, List[int]]],
        Optional[List[str]],
        Optional[List[str]],
    ],
    Query,
]


def collect_time_series_data(
    old_sensor_names: str | list[str],
    make_query: QueryCallType,
    query_window: tuple[datetime | None, datetime | None] = (None, None),
    belief_horizon_window: tuple[timedelta | None, timedelta | None] = (
        None,
        None,
    ),
    belief_time_window: tuple[datetime | None, datetime | None] = (None, None),
    belief_time: datetime | None = None,
    user_source_ids: int | list[int] = None,  # None is interpreted as all sources
    source_types: list[str] | None = None,
    exclude_source_types: list[str] | None = None,
    resolution: str | timedelta | None = None,
    sum_multiple: bool = True,
) -> tb.BeliefsDataFrame | dict[str, tb.BeliefsDataFrame]:
    """Get time series data from one or more old sensor models and rescale and re-package it to order.

    We can (lazily) look up by pickle, or load from the database.
    In the latter case, we are relying on time series data (power measurements and prices at this point) to
    have the same relevant column names (datetime, value).
    We require an old sensor model name of list thereof.
    If the time range parameters are None, they will be gotten from the session.
    Response is a 2D BeliefsDataFrame with the column event_value.
    If data from multiple assets is retrieved, the results are being summed.
    Or, if sum_multiple is False, the response will be a dictionary with asset names
    as keys, each holding a BeliefsDataFrame as its value.
    The response might be an empty data frame if no data exists for these assets
    in this time range.
    """

    # convert to tuple to support caching the query
    if isinstance(old_sensor_names, str):
        old_sensor_names = (old_sensor_names,)
    elif isinstance(old_sensor_names, list):
        old_sensor_names = tuple(old_sensor_names)

    bdf_dict = query_time_series_data(
        old_sensor_names,
        make_query,
        query_window,
        belief_horizon_window,
        belief_time_window,
        belief_time,
        user_source_ids,
        source_types,
        exclude_source_types,
        resolution,
    )

    if sum_multiple is True:
        return aggregate_values(bdf_dict)
    else:
        return bdf_dict


def query_time_series_data(
    old_sensor_names: tuple[str],
    make_query: QueryCallType,
    query_window: tuple[datetime | None, datetime | None] = (None, None),
    belief_horizon_window: tuple[timedelta | None, timedelta | None] = (
        None,
        None,
    ),
    belief_time_window: tuple[datetime | None, datetime | None] = (None, None),
    belief_time: datetime | None = None,
    user_source_ids: int | list[int] | None = None,
    source_types: list[str] | None = None,
    exclude_source_types: list[str] | None = None,
    resolution: str | timedelta | None = None,
) -> dict[str, tb.BeliefsDataFrame]:
    """
    Run a query for time series data on the database for a tuple of assets.
    Here, we need to know that postgres only stores naive datetimes and we keep them as UTC.
    Therefore, we localize the result.
    Then, we resample the result, to fit the given resolution. *
    Returns a dictionary of asset names (as keys) and BeliefsDataFrames (as values),
    with each BeliefsDataFrame having an "event_value" column.

    * Note that we convert string resolutions to datetime.timedelta objects.
    """

    # On demo, we query older data as if it's the current year's data (we convert back below)
    if current_app.config.get("FLEXMEASURES_MODE", "") == "demo":
        query_window = convert_query_window_for_demo(query_window)

    query = make_query(
        old_sensor_names=old_sensor_names,
        query_window=query_window,
        belief_horizon_window=belief_horizon_window,
        belief_time_window=belief_time_window,
        belief_time=belief_time,
        user_source_ids=user_source_ids,
        source_types=source_types,
        exclude_source_types=exclude_source_types,
    )

    df_all_assets = pd.DataFrame(
        query.all(), columns=[col["name"] for col in query.column_descriptions]
    )
    bdf_dict: dict[str, tb.BeliefsDataFrame] = {}
    for old_sensor_model_name in old_sensor_names:

        # Select data for the given asset
        df = df_all_assets[df_all_assets["name"] == old_sensor_model_name].loc[
            :, df_all_assets.columns != "name"
        ]

        # todo: Keep the preferred data source (first look at source_type, then user_source_id if needed)
        # if user_source_ids:
        #     values_orig["source"] = values_orig["source"].astype("category")
        #     values_orig["source"].cat.set_categories(user_source_ids, inplace=True)
        #     values_orig = (
        #         values_orig.sort_values(by=["source"], ascending=True)
        #         .drop_duplicates(subset=["source"], keep="first")
        #         .sort_values(by=["datetime"])
        #     )

        # Keep the most recent observation
        # todo: this block also resolves multi-sourced data by selecting the "first" (unsorted) source; we should have a consistent policy for this case
        df = (
            df.sort_values(by=["horizon"], ascending=True)
            .drop_duplicates(subset=["datetime"], keep="first")
            .sort_values(by=["datetime"])
        )

        # Index according to time and rename columns
        # todo: this operation can be simplified after moving our time series data structures to timely-beliefs
        df.rename(
            index=str,
            columns={
                "value": "event_value",
                "datetime": "event_start",
                "DataSource": "source",
                "horizon": "belief_horizon",
            },
            inplace=True,
        )
        df.set_index("event_start", drop=True, inplace=True)

        # Convert to the FLEXMEASURES timezone
        if not df.empty:
            df.index = df.index.tz_convert(time_utils.get_timezone())

        # On demo, we query older data as if it's the current year's data (we converted above)
        if current_app.config.get("FLEXMEASURES_MODE", "") == "demo":
            df.index = df.index.map(lambda t: t.replace(year=datetime.now().year))

        sensor = find_sensor_by_name(name=old_sensor_model_name)
        bdf = tb.BeliefsDataFrame(df.reset_index(), sensor=sensor)

        # re-sample data to the resolution we need to serve
        if resolution is None:
            resolution = sensor.event_resolution
        elif isinstance(resolution, str):
            try:
                # todo: allow pandas freqstr as resolution when timely-beliefs supports DateOffsets,
                #       https://github.com/SeitaBV/timely-beliefs/issues/13
                resolution = pd.to_timedelta(resolution).to_pytimedelta()
            except ValueError:
                resolution = isodate.parse_duration(resolution)
        bdf = bdf.resample_events(
            event_resolution=resolution, keep_only_most_recent_belief=True
        )

        # Slice query window after resampling
        if query_window[0] is not None:
            bdf = bdf[bdf.index.get_level_values("event_start") >= query_window[0]]
        if query_window[1] is not None:
            bdf = bdf[bdf.index.get_level_values("event_start") < query_window[1]]

        bdf_dict[old_sensor_model_name] = bdf

    return bdf_dict


def find_sensor_by_name(name: str):
    """
    Helper function: Find a sensor by name.
    TODO: make obsolete when we switched to collecting sensor data by sensor id rather than name
    """
    # importing here to avoid circular imports, deemed okay for temp. solution
    from flexmeasures.data.models.time_series import Sensor

    sensor = Sensor.query.filter(Sensor.name == name).one_or_none()
    if sensor is None:
        raise Exception("Unknown sensor: %s" % name)
    return sensor


def drop_non_unique_ids(a: int | list[int], b: int | list[int]) -> list[int]:
    """Removes all elements from B that are already in A."""
    a_l = a if type(a) == list else [a]
    b_l = b if type(b) == list else [b]
    return list(set(b_l).difference(a_l))  # just the unique ones


def convert_query_window_for_demo(
    query_window: tuple[datetime, datetime]
) -> tuple[datetime, datetime]:
    demo_year = current_app.config.get("FLEXMEASURES_DEMO_YEAR", None)
    if demo_year is None:
        return query_window
    try:
        start = query_window[0].replace(year=demo_year)
    except ValueError as e:
        # Expand the query_window in case a leap day was selected
        if "day is out of range for month" in str(e):
            start = (query_window[0] - timedelta(days=1)).replace(year=demo_year)
        else:
            start = query_window[0]
    try:
        end = query_window[-1].replace(year=demo_year)
    except ValueError as e:
        # Expand the query_window in case a leap day was selected
        if "day is out of range for month" in str(e):
            end = (query_window[-1] + timedelta(days=1)).replace(year=demo_year)
        else:
            end = query_window[-1]

    if start > end:
        start, end = (end, start)
    return start, end


def aggregate_values(bdf_dict: dict[Any, tb.BeliefsDataFrame]) -> tb.BeliefsDataFrame:

    # todo: test this function rigorously, e.g. with empty bdfs in bdf_dict
    # todo: consider 1 bdf with beliefs from source A, plus 1 bdf with beliefs from source B -> 1 bdf with sources A+B
    # todo: consider 1 bdf with beliefs from sources A and B, plus 1 bdf with beliefs from source C. -> 1 bdf with sources A+B and A+C
    # todo: consider 1 bdf with beliefs from sources A and B, plus 1 bdf with beliefs from source C and D. -> 1 bdf with sources A+B, A+C, B+C and B+D
    # Relevant issue: https://github.com/SeitaBV/timely-beliefs/issues/33

    # Nothing to aggregate
    if len(bdf_dict) == 1:
        return list(bdf_dict.values())[0]

    unique_source_ids: list[int] = []
    for bdf in bdf_dict.values():
        unique_source_ids.extend(bdf.lineage.sources)
        if not bdf.lineage.unique_beliefs_per_event_per_source:
            current_app.logger.warning(
                "Not implemented: only aggregation of deterministic uni-source beliefs (1 per event) is properly supported"
            )
        if bdf.lineage.number_of_sources > 1:
            current_app.logger.warning(
                "Not implemented: aggregating multi-source beliefs about the same sensor."
            )
    if len(set(unique_source_ids)) > 1:
        current_app.logger.warning(
            f"Not implemented: aggregating multi-source beliefs. Source {unique_source_ids[1:]} will be treated as if source {unique_source_ids[0]}"
        )

    data_as_bdf = tb.BeliefsDataFrame()
    for k, v in bdf_dict.items():
        if data_as_bdf.empty:
            data_as_bdf = v.copy()
        elif not v.empty:
            data_as_bdf["event_value"] = data_as_bdf["event_value"].add(
                simplify_index(v.copy())["event_value"],
                fill_value=0,
                level="event_start",
            )  # we only look at the event_start index level and sum up duplicates that level
    return data_as_bdf


def set_bdf_source(bdf: tb.BeliefsDataFrame, source_name: str) -> tb.BeliefsDataFrame:
    """
    Set the source of the BeliefsDataFrame.
    We do this by re-setting the index (as source probably is part of the BeliefsDataFrame multi index),
    setting the source, then restoring the (multi) index.
    """
    index_cols = bdf.index.names
    bdf = bdf.reset_index()
    bdf["source"] = DataSource(source_name)
    return bdf.set_index(index_cols)


def drop_unchanged_beliefs(bdf: tb.BeliefsDataFrame) -> tb.BeliefsDataFrame:
    """Drop beliefs that are already stored in the database with an earlier belief time.

    Also drop beliefs that are already in the data with an earlier belief time.

    Quite useful function to prevent cluttering up your database with beliefs that remain unchanged over time.
    """
    if bdf.empty:
        return bdf

    # Save the oldest ex-post beliefs explicitly, even if they do not deviate from the most recent ex-ante beliefs
    ex_ante_bdf = bdf[bdf.belief_horizons > timedelta(0)]
    ex_post_bdf = bdf[bdf.belief_horizons <= timedelta(0)]
    if not ex_ante_bdf.empty and not ex_post_bdf.empty:
        # We treat each part separately to avoid the ex-post knowledge would be lost
        ex_ante_bdf = drop_unchanged_beliefs(ex_ante_bdf)
        ex_post_bdf = drop_unchanged_beliefs(ex_post_bdf)
        bdf = pd.concat([ex_ante_bdf, ex_post_bdf])
        return bdf

    # Remove unchanged beliefs from within the new data itself
    index_names = bdf.index.names
    bdf = (
        bdf.sort_index()
        .reset_index()
        .drop_duplicates(
            ["event_start", "source", "cumulative_probability", "event_value"],
            keep="first",
        )
        .set_index(index_names)
    )

    # Remove unchanged beliefs with respect to what is already stored in the database
    return (
        bdf.convert_index_from_belief_horizon_to_time()
        .groupby(level=["belief_time", "source"], group_keys=False, as_index=False)
        .apply(_drop_unchanged_beliefs_compared_to_db)
    )


def _drop_unchanged_beliefs_compared_to_db(
    bdf: tb.BeliefsDataFrame,
) -> tb.BeliefsDataFrame:
    """Drop beliefs that are already stored in the database with an earlier belief time.

    Assumes a BeliefsDataFrame with a unique belief time and unique source,
    and either all ex-ante beliefs or all ex-post beliefs.

    It is preferable to call the public function drop_unchanged_beliefs instead.
    """
    if bdf.belief_horizons[0] > timedelta(0):
        # Look up only ex-ante beliefs (horizon > 0)
        kwargs = dict(horizons_at_least=timedelta(0))
    else:
        # Look up only ex-post beliefs (horizon <= 0)
        kwargs = dict(horizons_at_most=timedelta(0))
    previous_beliefs_in_db = bdf.sensor.search_beliefs(
        event_starts_after=bdf.event_starts[0],
        event_ends_before=bdf.event_ends[-1],
        beliefs_before=bdf.lineage.belief_times[0],  # unique belief time
        source=bdf.lineage.sources[0],  # unique source
        most_recent_beliefs_only=False,
        **kwargs,
    )
    # todo: delete next line and set most_recent_beliefs_only=True when this is resolved: https://github.com/SeitaBV/timely-beliefs/pull/117
    previous_most_recent_beliefs_in_db = belief_utils.select_most_recent_belief(
        previous_beliefs_in_db
    )

    compare_fields = ["event_start", "source", "cumulative_probability", "event_value"]
    a = bdf.reset_index().set_index(compare_fields)
    b = previous_most_recent_beliefs_in_db.reset_index().set_index(compare_fields)
    bdf = a.drop(
        b.index,
        errors="ignore",
        axis=0,
    )

    # Keep whole probabilistic beliefs, not just the parts that changed
    c = bdf.reset_index().set_index(["event_start", "source"])
    d = a.reset_index().set_index(["event_start", "source"])
    bdf = d[d.index.isin(c.index)]

    bdf = bdf.reset_index().set_index(
        ["event_start", "belief_time", "source", "cumulative_probability"]
    )
    return bdf
