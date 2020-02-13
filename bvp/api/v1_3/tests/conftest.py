from flask_security import SQLAlchemySessionUserDatastore
import pytest


@pytest.fixture(scope="function", autouse=True)
def setup_api_test_data(db):
    """
    Set up data for API v1.3 tests.
    """
    print("Setting up data for API v1.3 tests on %s" % db.engine)

    from bvp.data.models.user import User, Role
    from bvp.data.models.assets import Asset

    user_datastore = SQLAlchemySessionUserDatastore(db.session, User, Role)
    test_prosumer = user_datastore.find_user(email="test_prosumer@seita.nl")

    battery = Asset.query.filter(Asset.name == "Test battery").one_or_none()
    battery.owner = test_prosumer

    charging_station = Asset.query.filter(
        Asset.name == "Test charging station"
    ).one_or_none()
    charging_station.owner = test_prosumer
