"""Tests that a meter's aux voltage round-trips through the readings table."""

from datetime import datetime

from load_test_bench.data.database import Database
from load_test_bench.data.models import Reading, TestSession


def make_reading(aux=None):
    return Reading(
        timestamp=datetime(2026, 1, 1, 10, 0, 0),
        voltage_v=3.70, current_a=1.0, power_w=3.70, energy_wh=0.5,
        capacity_mah=500.0, mosfet_temp_c=40, ext_temp_c=25,
        aux_voltage_v=aux,
    )


def make_session(db):
    session = TestSession(name="s", start_time=datetime(2026, 1, 1, 10, 0, 0))
    session.id = db.create_session(session)
    return session.id


class TestAuxVoltagePersistence:
    def test_reading_has_aux_field_defaulting_none(self):
        assert make_reading().aux_voltage_v is None
        assert make_reading(aux=3.81).aux_voltage_v == 3.81

    def test_to_dict_includes_aux(self):
        assert make_reading(aux=3.81).to_dict()["aux_voltage_v"] == 3.81

    def test_add_reading_round_trips_aux(self, tmp_path):
        db = Database(tmp_path / "tests.db")
        session_id = make_session(db)
        db.add_reading(session_id, make_reading(aux=3.812), commit=True)
        readings = db.get_readings(session_id)
        assert len(readings) == 1
        assert readings[0].aux_voltage_v == 3.812
        db.close()

    def test_null_aux_reads_back_as_none(self, tmp_path):
        db = Database(tmp_path / "tests.db")
        session_id = make_session(db)
        db.add_reading(session_id, make_reading(aux=None), commit=True)
        assert db.get_readings(session_id)[0].aux_voltage_v is None
        db.close()

    def test_batch_round_trips_aux(self, tmp_path):
        db = Database(tmp_path / "tests.db")
        session_id = make_session(db)
        db.add_readings_batch(session_id, [make_reading(aux=3.80), make_reading(aux=3.79)])
        aux = [r.aux_voltage_v for r in db.get_readings(session_id)]
        assert aux == [3.80, 3.79]
        db.close()
