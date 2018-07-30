from typing import List
import pytest

import isodate
from flask_security import SQLAlchemySessionUserDatastore
from flask_security.utils import hash_password

from bvp.data.models.assets import Power
from bvp.data.models.data_sources import DataSource
from bvp.data.services.users import create_user


@pytest.fixture(scope="function", autouse=True)
def setup_api_test_data(db):
    """
    Set up data for API v1.1 tests.
    """
    print("Setting up data for API v1.1 tests on %s" % db.engine)

    from bvp.data.models.user import User, Role
    from bvp.data.models.assets import Asset, AssetType

    user_datastore = SQLAlchemySessionUserDatastore(db.session, User, Role)

    # Create a test user without a USEF role
    create_user(
        username="test user without roles",
        email="test_user@seita.nl",
        password=hash_password("testtest"),
    )

    # Create 3 test assets for the test_prosumer user
    test_prosumer = user_datastore.find_user(email="test_prosumer@seita.nl")
    test_asset_type = AssetType(name="test-type")
    db.session.add(test_asset_type)
    asset_names = ["CS 1", "CS 2", "CS 3"]
    assets: List[Asset] = []
    for asset_name in asset_names:
        asset = Asset(
            name=asset_name,
            asset_type_name="test-type",
            capacity_in_mw=1,
            latitude=100,
            longitude=100,
        )
        asset.owner = test_prosumer
        assets.append(asset)
        db.session.add(asset)

    # Add power forecasts to the assets
    cs_1 = Asset.query.filter(Asset.name == "CS 1").one_or_none()
    cs_2 = Asset.query.filter(Asset.name == "CS 2").one_or_none()
    cs_3 = Asset.query.filter(Asset.name == "CS 3").one_or_none()
    data_source = DataSource.query.filter(
        DataSource.user_id == test_prosumer.id
    ).one_or_none()
    power_forecasts = []
    for i in range(6):
        p_1 = Power(
            datetime=isodate.parse_datetime("2015-01-01T00:00:00Z")
            + i * isodate.parse_duration("PT15M"),
            horizon="PT6H",
            value=300,
            asset_id=cs_1.id,
            data_source_id=data_source.id,
        )
        p_2 = Power(
            datetime=isodate.parse_datetime("2015-01-01T00:00:00Z")
            + i * isodate.parse_duration("PT15M"),
            horizon="PT6H",
            value=300,
            asset_id=cs_2.id,
            data_source_id=data_source.id,
        )
        p_3 = Power(
            datetime=isodate.parse_datetime("2015-01-01T00:00:00Z")
            + i * isodate.parse_duration("PT15M"),
            horizon="PT6H",
            value=0,
            asset_id=cs_3.id,
            data_source_id=data_source.id,
        )
        power_forecasts.append(p_1)
        power_forecasts.append(p_2)
        power_forecasts.append(p_3)
    db.session.bulk_save_objects(power_forecasts)

    print("Done setting up data for API v1.1 tests")