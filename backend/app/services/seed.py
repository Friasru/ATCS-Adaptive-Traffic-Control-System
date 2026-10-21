from app import db
from app.models import Scenario


def seed_scenarios():
    if Scenario.query.count() == 0:
        db.session.add(
            Scenario(
                name="uneven",
                description="East-west heavy, north-south light",
                ew_vehs_per_hour=500,
                ns_vehs_per_hour=200,
            )
        )
        db.session.commit()
