from typing import List

from flask import Blueprint, request, session
from werkzeug.exceptions import BadRequest
import pandas as pd
from bokeh.embed import components
from bokeh.util.string import encode_utf8

from utils import (set_time_range_for_session, render_a1vpp_template, get_assets, get_data,
                   freq_label_to_human_readable_label, mean_absolute_error, mean_absolute_percentage_error,
                   weighted_absolute_percentage_error, resolution_to_hour_factor, get_assets_by_resource,
                   is_pure_consumer, forecast_horizons_for, get_most_recent_quarter,
                   extract_forecasts)
import plotting
import models


# The views in this module can as blueprint be registered with the Flask app (see app.py)
a1_views = Blueprint('a1_views', __name__,  static_folder='public', template_folder='templates')


# TODO: replace these mock helpers when we have real auth & user groups
def is_prosumer_mock() -> bool:
    """Return whether we are showing the mocked version for a onshore wind prosumer.
    Set this in the session, as well."""
    if "prosumer_mock" in request.values:
        if request.values.get("prosumer_mock") == "1":
            session["prosumer_mock"] = True
        else:
            session["prosumer_mock"] = False
    return session.get("prosumer_mock") is True \
        or ("prosumer_mock" in request.values and request.values.get("prosumer_mock") == "1")


def filter_mock_prosumer_assets(assets: List[models.Asset]) -> List[models.Asset]:
    return [a for a in assets if a.name in ("ss-onshore", "sd-onshore")]


# Dashboard and main landing page
@a1_views.route('/')
@a1_views.route('/dashboard')
def dashboard_view():
    msg = ""
    if "clear-session" in request.values:
        session.clear()
        msg = "Your session was cleared."

    asset_counts = {}
    prosumer_mock = is_prosumer_mock()
    for asset_type in ("solar", "wind", "vehicles", "buildings"):
        assets = get_assets_by_resource(asset_type)
        if prosumer_mock:
            assets = filter_mock_prosumer_assets(assets)
        asset_counts[asset_type] = len(assets)

    # Todo: switch from this mock-up function for asset counts to a proper implementation of battery assets
    asset_counts["battery"] = asset_counts["solar"]

    return render_a1vpp_template('dashboard.html', show_map=True, message=msg,
                                 asset_counts=asset_counts,
                                 prosumer_mock=prosumer_mock)


# Portfolio view
@a1_views.route('/portfolio', methods=['GET', 'POST'])
def portfolio_view():
    set_time_range_for_session()
    assets = get_assets()
    if is_prosumer_mock():
        assets = filter_mock_prosumer_assets(assets)
    revenues = dict.fromkeys([a.name for a in assets])
    generation = dict.fromkeys([a.name for a in assets])
    consumption = dict.fromkeys([a.name for a in assets])
    prices_data = get_data("epex_da", session["start_time"], session["end_time"], session["resolution"])
    for asset in assets:
        load_data = get_data(asset.name, session["start_time"], session["end_time"], session["resolution"])
        revenues[asset.name] = pd.Series(load_data.y * prices_data.y, index=load_data.index).sum()
        if is_pure_consumer(asset.name):
            generation[asset.name] = 0
            consumption[asset.name] = -1 * pd.Series(load_data.y).sum()
        else:
            generation[asset.name] = pd.Series(load_data.y).sum()
            consumption[asset.name] = 0
    return render_a1vpp_template("portfolio.html", assets=assets, prosumer_mock=is_prosumer_mock(),
                                 revenues=revenues, generation=generation, consumption=consumption)


# Analytics view
@a1_views.route('/analytics', methods=['GET', 'POST'])
def analytics_view():
    set_time_range_for_session()
    groups_with_assets = [group for group in models.asset_groups if len(get_assets_by_resource(group)) > 0]

    if "resource" not in session:  # set some default, if possible
        if "solar" in groups_with_assets:
            session["resource"] = "solar"
        elif "wind" in groups_with_assets:
            session["resource"] = "wind"
        elif "vehicles" in groups_with_assets:
            session["resource"] = "vehicles"
        elif len(get_assets()) > 0:
            session["resource"] = get_assets()[0].name
    if "resource" in request.form:  # set by user
        session["resource"] = request.form['resource']

    assets = get_assets()
    if is_prosumer_mock():
        groups_with_assets = []
        assets = filter_mock_prosumer_assets(assets)
        if len(assets) > 0:
            session["resource"] = assets[0].name

    # If we show purely consumption assets, we'll want to adapt the sign of the data and labels.
    showing_pure_consumption_data = is_pure_consumer(session["resource"])

    # loads
    load_data = get_data(session["resource"], session["start_time"], session["end_time"], session["resolution"])
    if load_data is None or load_data.size == 0:
        raise BadRequest("Not enough data available for resource \"%s\" in the time range %s to %s"
                         % (session["resource"], session["start_time"], session["end_time"]))
    if showing_pure_consumption_data:
        load_data *= -1
    load_hover = plotting.create_hover_tool("MW", session.get("resolution"))
    load_data_to_show = load_data.loc[load_data.index < get_most_recent_quarter().replace(year=2015)]
    load_forecast_data = extract_forecasts(load_data)
    load_fig = plotting.create_graph(load_data_to_show.y,
                                     forecasts=load_forecast_data,
                                     title="Electricity load on %s" % session["resource"],
                                     x_label="Time (sampled by %s)  "
                                     % freq_label_to_human_readable_label(session["resolution"]),
                                     y_label="Load (in MW)",
                                     show_y_floats=True,
                                     hover_tool=load_hover)
    load_script, load_div = components(load_fig)

    load_hour_factor = resolution_to_hour_factor(session["resolution"])

    # prices
    prices_data = get_data("epex_da", session["start_time"], session["end_time"], session["resolution"])
    prices_hover = plotting.create_hover_tool("KRW/MWh", session.get("resolution"))
    prices_data_to_show = prices_data.loc[prices_data.index < get_most_recent_quarter().replace(year=2015)]
    prices_forecast_data = extract_forecasts(prices_data)
    prices_fig = plotting.create_graph(prices_data_to_show.y,
                                       forecasts=prices_forecast_data,
                                       title="(Day-ahead) Market Prices",
                                       x_label="Time (sampled by %s)  "
                                       % freq_label_to_human_readable_label(session["resolution"]),
                                       y_label="Prices (in KRW/MWh)",
                                       hover_tool=prices_hover)
    prices_script, prices_div = components(prices_fig)

    # metrics
    realised_load_in_mwh = pd.Series(load_data.y * load_hour_factor).values
    expected_load_in_mwh = pd.Series(load_forecast_data.yhat * load_hour_factor).values
    mae_load_in_mwh = mean_absolute_error(realised_load_in_mwh, expected_load_in_mwh)
    mae_unit_price = mean_absolute_error(prices_data.y, prices_forecast_data.yhat)
    mape_load = mean_absolute_percentage_error(realised_load_in_mwh, expected_load_in_mwh)
    mape_unit_price = mean_absolute_percentage_error(prices_data.y, prices_forecast_data.yhat)
    wape_load = weighted_absolute_percentage_error(realised_load_in_mwh, expected_load_in_mwh)
    wape_unit_price = weighted_absolute_percentage_error(prices_data.y, prices_forecast_data.yhat)

    # revenues/costs
    rev_cost_data = pd.Series(load_data.y * prices_data.y, index=load_data.index)
    rev_cost_forecasts = pd.DataFrame(index=load_data.index, columns=["yhat", "yhat_upper", "yhat_lower"])
    wape_factor_rev_costs = (wape_load / 100. + wape_unit_price / 100.) / 2.  # there might be a better heuristic here
    rev_cost_forecasts.yhat = load_forecast_data.yhat * prices_forecast_data.yhat
    wape_span_rev_costs = rev_cost_forecasts.yhat * wape_factor_rev_costs
    rev_cost_forecasts.yhat_upper = rev_cost_forecasts.yhat + wape_span_rev_costs
    rev_cost_forecasts.yhat_lower = rev_cost_forecasts.yhat - wape_span_rev_costs
    rev_cost_str = "Revenues"
    if showing_pure_consumption_data:
        rev_cost_str = "Costs"
    rev_cost_hover = plotting.create_hover_tool("KRW", session.get("resolution"))
    rev_costs_data_to_show = rev_cost_data.loc[rev_cost_data.index < get_most_recent_quarter().replace(year=2015)]
    rev_cost_fig = plotting.create_graph(rev_costs_data_to_show,
                                         forecasts=rev_cost_forecasts,
                                         title="%s for %s (priced on DA market)" % (rev_cost_str, session["resource"]),
                                         x_label="Time (sampled by %s)  "
                                         % freq_label_to_human_readable_label(session["resolution"]),
                                         y_label="%s (in KRW)" % rev_cost_str,
                                         hover_tool=rev_cost_hover)
    rev_cost_script, rev_cost_div = components(rev_cost_fig)

    return render_a1vpp_template("analytics.html",
                                 load_profile_div=encode_utf8(load_div),
                                 load_profile_script=load_script,
                                 prices_series_div=encode_utf8(prices_div),
                                 prices_series_script=prices_script,
                                 revenues_costs_series_div=encode_utf8(rev_cost_div),
                                 revenues_costs_series_script=rev_cost_script,
                                 realised_load_in_mwh=realised_load_in_mwh.sum(),
                                 realised_unit_price=prices_data.y.mean(),
                                 realised_revenues_costs=rev_cost_data.values.sum(),
                                 expected_load_in_mwh=expected_load_in_mwh.sum(),
                                 expected_unit_price=prices_forecast_data.yhat.mean(),
                                 mae_load_in_mwh=mae_load_in_mwh,
                                 mae_unit_price=mae_unit_price,
                                 mape_load=mape_load,
                                 mape_unit_price=mape_unit_price,
                                 wape_load=wape_load,
                                 wape_unit_price=wape_unit_price,
                                 assets=assets,
                                 asset_groups=groups_with_assets,
                                 resource=session["resource"],
                                 prosumer_mock=is_prosumer_mock(),
                                 forecast_horizons=forecast_horizons_for(session["resolution"]),
                                 active_forecast_horizon=session["forecast_horizon"])


# Control view
@a1_views.route('/control', methods=['GET', 'POST'])
def control_view():
    return render_a1vpp_template("control.html", prosumer_mock=is_prosumer_mock())


# Upload view
@a1_views.route('/upload')
def upload_view():
    return render_a1vpp_template("upload.html")


# Test view
@a1_views.route('/test')
def test_view():
    """Used to test UI elements"""
    return render_a1vpp_template("test.html")
