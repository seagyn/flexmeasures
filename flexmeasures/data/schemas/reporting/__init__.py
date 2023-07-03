from marshmallow import Schema, fields

from flexmeasures.data.schemas.sensors import SensorIdField
from flexmeasures.data.schemas.sources import DataSourceIdField

from flexmeasures.data.schemas import AwareDateTimeField, DurationField


class ReporterConfigSchema(Schema):
    pass


class ReporterInputsSchema(Schema):
    sensor = SensorIdField(required=True)

    start = AwareDateTimeField(required=True)
    end = AwareDateTimeField(required=True)

    resolution = DurationField(required=False)
    belief_time = AwareDateTimeField(required=False)


class BeliefsSearchConfigSchema(Schema):
    """
    This schema implements the required fields to perform a TimedBeliefs search
    using the method flexmeasures.data.models.time_series:Sensor.search_beliefs
    """

    sensor = SensorIdField(required=True)
    alias = fields.Str()

    event_starts_after = AwareDateTimeField()
    event_ends_before = AwareDateTimeField()

    belief_time = AwareDateTimeField()

    horizons_at_least = DurationField()
    horizons_at_most = DurationField()

    source = DataSourceIdField()

    source_types = fields.List(fields.Str())
    exclude_source_types = fields.List(fields.Str())
    most_recent_beliefs_only = fields.Boolean()
    most_recent_events_only = fields.Boolean()

    one_deterministic_belief_per_event = fields.Boolean()
    one_deterministic_belief_per_event_per_source = fields.Boolean()
    resolution = DurationField()
    sum_multiple = fields.Boolean()
