import os
import datetime
import json
from typing import List, Dict, Optional, Union

from flask import request, render_template, session, current_app
from werkzeug.exceptions import BadRequest
import pandas as pd
import numpy as np
from bokeh.resources import CDN
import iso8601

import models
from models import Asset, asset_groups, Market, resolutions


# global, lazily loaded asset description
ASSETS = []
# global, lazily loaded market description
MARKETS = []
# global, lazily loaded data source, will be replaced by DB connection probably
DATA = {}


def get_assets() -> List[Asset]:
    """Return a list of all models.Asset objects that are mentioned in assets.json and have data.
    The asset list is constructed lazily (only once per app start)."""
    global ASSETS
    if len(ASSETS) == 0:
        with open("data/assets.json", "r") as assets_json:
            dict_assets = json.loads(assets_json.read())
        ASSETS = []
        for dict_asset in dict_assets:
            has_data = True
            for res in resolutions:
                if not os.path.exists("data/pickles/df_%s_res%s.pickle" % (dict_asset["name"], res)):
                    has_data = False
                    break
            if has_data:
                ASSETS.append(Asset(**dict_asset))
    return ASSETS


def get_assets_by_resource(resource: str) -> List[Asset]:
    """Gather assets which are identified by this resource name.
    The resource name is either the name of an asset group or an individual asset."""
    assets = get_assets()
    if resource in asset_groups:
        resource_assets = set()
        asset_queries = asset_groups[resource]
        for query in asset_queries:
            for asset in assets:
                if hasattr(asset, query.attr) and getattr(asset, query.attr, None) == query.val:
                    resource_assets.add(asset)
        if len(resource_assets) > 0:
            return list(resource_assets)
    for asset in assets:
        if asset.name == resource:
            return [asset]
    return []


def get_markets() -> List[Market]:
    """Return markets. Markets are loaded lazily from file."""
    global MARKETS
    if len(MARKETS) == 0:
        with open("data/markets.json", "r") as markets_json:
            dict_markets = json.loads(markets_json.read())
        MARKETS = [Market(**a) for a in dict_markets]
    return MARKETS


def get_market_by_resource(resource: str) -> Optional[Market]:
    """Find a market. TODO: support market grouping (see models.market_groups)."""
    markets = get_markets()
    for market in markets:
        if market.name == resource:
            return market


def get_data_by_resource(resource: str, start: datetime=None, end: datetime=None, resolution: str=None,
                         sum_multiple: bool=True) -> Union[pd.DataFrame, Dict[str, pd.DataFrame]]:
    """Get data for one or more assets or markets.
    If the time range parameters are None, they will be gotten from the session.
    See get_data_vor_assets for more information."""
    asset_names = []
    for asset in get_assets_by_resource(resource):
        asset_names.append(asset.name)
    market = get_market_by_resource(resource)
    if market is not None:
        asset_names.append(market.name)
    return get_data_for_assets(asset_names, start, end, resolution, sum_multiple=sum_multiple)


def get_data_for_assets(asset_names: List[str], start: datetime=None, end: datetime=None, resolution: str=None,
                        sum_multiple=True) -> Union[pd.DataFrame, Dict[str, pd.DataFrame]]:
    """Get data for one or more assets (also markets).
    We (lazily) look up by pickle, so we require a list of asset or market names.
    If the time range parameters are None, they will be gotten from the session.
    Response is a 2D data frame with the usual columns (y, yhat, ...).
    If data from multiple assets is retrieved, the results are being summed.
    If sum_multiple is False, the response will be a dictionary with asset names as keys and data frames as values.
    Response might be None if no data exists for these assets in this time range."""
    data = None
    if start is None or end is None or resolution is None and "resolution" not in session:
        set_time_range_for_session()
    if start is None:
        start = session["start_time"]
    if end is None:
        end = session["end_time"]
    if resolution is None:
        resolution = session["resolution"]
    for asset_name in asset_names:
        data_label = "%s_res%s" % (asset_name, resolution)
        global DATA
        if data_label not in DATA:
            current_app.logger.info("Loading %s data from disk ..." % data_label)
            try:
                DATA[data_label] = pd.read_pickle("data/pickles/df_%s.pickle" % data_label)
            except FileNotFoundError:
                raise BadRequest("Sorry, we cannot find any data for the resource \"%s\" ..." % data_label)
        date_mask = (DATA[data_label].index >= start) & (DATA[data_label].index <= end)

        if sum_multiple is True:  # Here we only build one data frame, summed up if necessary.
            if data is None:
                data = DATA[data_label].loc[date_mask]
            else:
                data = data + DATA[data_label].loc[date_mask]
        else:                     # Here we build a dict with data frames.
            if data is None:
                data = {asset_name: DATA[data_label].loc[date_mask]}
            else:
                data[asset_name] = DATA[data_label].loc[date_mask]
    return data


def decide_resolution(start: datetime, end: datetime) -> str:
    """Decide on a resolution, given the length of the time period."""
    resolution = "15T"  # default is 15 minute intervals
    period_length = end - start
    if period_length > datetime.timedelta(weeks=16):
        resolution = "1w"  # So upon switching from days to weeks, you get at least 16 data points
    elif period_length > datetime.timedelta(days=14):
        resolution = "1d"  # So upon switching from hours to days, you get at least 14 data points
    elif period_length > datetime.timedelta(hours=48):
        resolution = "1h"  # So upon switching from 15min to hours, you get at least 48 data points
    return resolution


def resolution_to_hour_factor(resolution: str):
    """Return the factor with which a value needs to be multiplied in order to get the value per hour,
    e.g. 10 MW at a resolution of 15min are 2.5 MWh per time step"""
    switch = {
        "15T": 0.25,
        "1h": 1,
        "1d": 24,
        "1w": 24 * 7
    }
    return switch.get(resolution, 1)


def is_pure_consumer(resource_name: str) -> bool:
    """Return True if the assets represented by this resource are consuming but not producing.
    Currently only checks the first asset."""
    only_or_first_asset = get_assets_by_resource(resource_name)[0]
    if (only_or_first_asset is not None
            and models.asset_types[only_or_first_asset.asset_type_name].is_consumer
            and not models.asset_types[only_or_first_asset.asset_type_name].is_producer):
        return True
    else:
        return False


def is_pure_producer(resource_name: str) -> bool:
    """Return True if the assets represented by this resource are producing but not consuming.
    Currently only checks the first asset."""
    only_or_first_asset = get_assets_by_resource(resource_name)[0]
    if (only_or_first_asset is not None
            and models.asset_types[only_or_first_asset.asset_type_name].is_producer
            and not models.asset_types[only_or_first_asset.asset_type_name].is_consumer):
        return True
    else:
        return False


def get_most_recent_quarter() -> datetime:
    now = datetime.datetime.now()
    return now.replace(minute=now.minute - (now.minute % 15), second=0, microsecond=0)


def get_most_recent_hour() -> datetime:
    now = datetime.datetime.now()
    return now.replace(minute=now.minute - (now.minute % 60), second=0, microsecond=0)


def get_default_start_time() -> datetime:
    return get_most_recent_quarter() - datetime.timedelta(days=1)


def get_default_end_time() -> datetime:
    return get_most_recent_quarter()


def set_time_range_for_session():
    """Set period (start_date, end_date and resolution) on session if they are not yet set.
    Also set the forecast horizon, if given."""
    if "start_time" in request.values:
        session["start_time"] = iso8601.parse_date(request.values.get("start_time"))
    elif "start_time" not in session:
        session["start_time"] = get_default_start_time()
    if "end_time" in request.values:
        session["end_time"] = iso8601.parse_date(request.values.get("end_time"))
    elif "end_time" not in session:
        session["end_time"] = get_default_end_time()

    # TODO: For now, we have to work with the data we have, that means 2015
    session["start_time"] = session["start_time"].replace(year=2015)
    session["end_time"] = session["end_time"].replace(year=2015)

    if session["start_time"] >= session["end_time"]:
        raise BadRequest("Start time %s is not after end time %s." % (session["start_time"], session["end_time"]))

    session["resolution"] = decide_resolution(session["start_time"], session["end_time"])

    if "forecast_horizon" in request.values:
        session["forecast_horizon"] = request.values.get("forecast_horizon")
    allowed_horizons = forecast_horizons_for(session["resolution"])
    if session.get("forecast_horizon") not in allowed_horizons and len(allowed_horizons) > 0:
        session["forecast_horizon"] = allowed_horizons[0]


def freq_label_to_human_readable_label(freq_label: str) -> str:
    """Translate pandas frequency labels to human-readable labels."""
    f2h_map = {
        "15T": "15 minutes",
        "1h": "hour",
        "1d": "day",
        "1w": "week"
    }
    return f2h_map.get(freq_label, freq_label)


def forecast_horizons_for(resolution: str) -> List[str]:
    """Return a list of horizons that are supported per resolution."""
    if resolution in ("15T", "1h"):
        return ["6h", "48h"]
    elif resolution == "1d":
        return ["48h"]
    elif resolution == "1w":
        return ["1w"]
    return []


def extract_forecasts(df: pd.DataFrame) -> pd.DataFrame:
    """Extract forecast columns (given the chosen horizon) and give them the standard naming"""
    forecast_columns = ["yhat", "yhat_upper", "yhat_lower"]  # this is what the plotter expects
    horizon = session["forecast_horizon"]
    forecast_renaming = {"yhat_%s" % horizon: "yhat",
                         "yhat_%s_upper" % horizon: "yhat_upper",
                         "yhat_%s_lower" % horizon:  "yhat_lower"}
    return df.rename(forecast_renaming, axis="columns")[forecast_columns]


def mean_absolute_error(y_true, y_forecast):
    y_true, y_forecast = np.array(y_true), np.array(y_forecast)
    return np.mean(np.abs((y_true - y_forecast)))


def mean_absolute_percentage_error(y_true, y_forecast):
    y_true, y_forecast = np.array(y_true), np.array(y_forecast)
    return np.mean(np.abs((y_true - y_forecast) / y_true)) * 100


def weighted_absolute_percentage_error(y_true, y_forecast):
    y_true, y_forecast = np.array(y_true), np.array(y_forecast)
    return np.sum(np.abs((y_true - y_forecast))) / np.sum(y_true) * 100


def render_a1vpp_template(html_filename: str, **variables):
    """Render template and add all expected template variables, plus the ones given as **variables."""
    if "start_time" in session:
        variables["start_time"] = session["start_time"]
    else:
        variables["start_time"] = get_default_start_time()
    if "end_time" in session:
        variables["end_time"] = session["end_time"]
    else:
        variables["end_time"] = get_default_end_time()
    variables["page"] = html_filename.replace(".html", "")
    if "show_datepicker" not in variables:
        variables["show_datepicker"] = variables["page"] in ("analytics", "portfolio", "control")
    if "load_profile_div" in variables or "portfolio_plot_div" in variables:
        variables["contains_plots"] = True
        variables["bokeh_css_resources"] = CDN.render_css()
        variables["bokeh_js_resources"] = CDN.render_js()
    else:
        variables["contains_plots"] = False
    variables["resolution"] = session.get("resolution", "")
    variables["resolution_human"] = freq_label_to_human_readable_label(session.get("resolution", ""))
    variables["next24hours"] = [(get_most_recent_hour() + datetime.timedelta(hours=i)).strftime("%I:00 %p")
                                for i in range(1, 26)]

    # TODO: remove when we stop mocking control.html
    if variables["page"] == "control":
        variables["start_time"] = session["start_time"].replace(hour=4, minute=0, second=0)
        variables["end_time"] = variables["start_time"] + datetime.timedelta(hours=1)

    return render_template(html_filename, **variables)
